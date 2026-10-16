#include "orbit_protocol.hpp"
#include "gap_tracker.hpp"
#include "retention_queue.hpp"
#include "durable_outbox.hpp"
#include "device_source.hpp"
#include "run_metrics.hpp"
#include "scenario_source.hpp"
#include "retry_policy.hpp"
#include "command_policy.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <memory>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#ifndef ORBIT_TEST_PLAINTEXT
#include <openssl/ssl.h>
#include <openssl/rand.h>
#include <openssl/err.h>
#include <openssl/x509v3.h>
#endif

namespace {

using Clock = std::chrono::steady_clock;
using Milliseconds = std::chrono::milliseconds;

std::uint64_t monotonic_ns() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count());
}

enum class ConnectionState { disconnected, connecting, authenticating, active, backoff, stopping };

const char* state_name(ConnectionState state) {
    switch (state) {
        case ConnectionState::disconnected: return "disconnected";
        case ConnectionState::connecting: return "connecting";
        case ConnectionState::authenticating: return "authenticating";
        case ConnectionState::active: return "active";
        case ConnectionState::backoff: return "backoff";
        case ConnectionState::stopping: return "stopping";
    }
    return "stopping";
}

struct Config {
    std::string ca, certificate, key, server_name, authorized_command_server;
    std::string host;
    std::string outbox_path;
    std::string source{"fake"};
    std::string device_path{"/dev/orbit_telemetry"};
    std::uint64_t outbox_bytes{64*1024*1024};
    std::size_t send_window{1};
    std::size_t storage_batch{1};
    bool priority_scheduling{};
    std::uint16_t port{};
    std::uint64_t scenario_seed{1};
    std::uint64_t count{100};
    std::uint64_t interval_ms{100};
    std::size_t capacity{
#ifdef ORBIT_TEST_PLAINTEXT
        1024
#else
        std::numeric_limits<std::size_t>::max()
#endif
    };
    std::uint64_t connect_timeout_ms{500};
    std::uint64_t io_timeout_ms{500};
    std::uint64_t drain_ms{3000};
    std::size_t max_frames_per_iteration{16};
    std::uint64_t boot_id{[] {
        std::random_device entropy;
        std::uniform_int_distribution<std::uint64_t> distribution;
        return distribution(entropy);
    }()};
    bool fake_fault{};
    bool explicit_boot_id{};
    bool explicit_device{};
    int ready_fd{-1};
};

struct Counters {
    std::atomic<std::uint64_t> generated{};
    std::atomic<std::uint64_t> acknowledged{};
    std::atomic<std::uint64_t> dropped_oldest{};
    std::atomic<std::uint64_t> discarded_shutdown{};
    std::atomic<std::uint64_t> connect_attempts{};
    std::atomic<std::uint64_t> connect_failures{};
    std::atomic<std::uint64_t> reconnects{};
    std::atomic<std::uint64_t> send_failures{};
    std::atomic<std::uint64_t> receive_failures{};
    std::atomic<std::uint64_t> invalid_acks{};
    std::atomic<std::uint64_t> transmission_attempts{};
    std::atomic<std::uint64_t> retransmissions{};
};

struct Shared {
    explicit Shared(const Config& config) : retained(config.capacity), gaps(16), source_label(config.source), storage_path(config.outbox_path) {
        if(!config.outbox_path.empty()) durable=std::make_unique<orbit::DurableOutbox>(config.outbox_path,config.outbox_bytes,config.capacity,config.priority_scheduling);
        configured_window=config.send_window;priority_enabled=config.priority_scheduling;configured_storage_batch=config.storage_batch;
        if(config.source=="sensors") {
            sensors=std::make_unique<orbit::SensorSource>(orbit::open_sensor_device(config.device_path));
            sensor_clock_initial=sensors->clock_info();
            commands.device_backend([this](const orbit::CommandRequest& request) {
                bool applied=false;
                switch(request.opcode) {
                    case 1: applied=sensors->enable();break;
                    case 2: applied=sensors->disable();break;
                    case 3: applied=sensors->set_rate(request.argument);break;
                    case 4: applied=sensors->reset();break;
                    case 5: applied=sensors->clear_fault();break;
                    case 6: applied=true;break;
                    default:return orbit::DeviceCommandOutcome{orbit::CommandStatus::invalid_argument,{}};
                }
                if(!applied)return orbit::DeviceCommandOutcome{orbit::CommandStatus::outcome_unknown,{}};
                try {
                    const auto actual=sensors->state();
                    return orbit::DeviceCommandOutcome{orbit::CommandStatus::applied,
                        orbit::FakeDeviceState{!!actual.enabled,actual.period_ms,actual.generation,
                            actual.fault_category!=0,actual.command_effects}};
                } catch(...) {return orbit::DeviceCommandOutcome{orbit::CommandStatus::outcome_unknown,{}};}
            });
        }
        if(config.source=="device") {
            device=std::make_unique<orbit::DeviceSource>(orbit::open_device(config.device_path));
            commands.device_backend([this](const orbit::CommandRequest& request) {
                if(request.opcode!=3)return orbit::DeviceCommandOutcome{orbit::CommandStatus::invalid_argument,{}};
                return orbit::DeviceCommandOutcome{device->set_rate(request.argument)?orbit::CommandStatus::applied:orbit::CommandStatus::outcome_unknown,{}};
            });
        }
    }
    std::unique_ptr<orbit::DurableOutbox> durable;
    std::unique_ptr<orbit::DeviceSource> device;
    std::unique_ptr<orbit::SensorSource> sensors;
    orbit_stats driver_stats{};
    orbit_sensor_clock sensor_clock_initial{}, sensor_clock_current{};
    orbit_sensor_events sensor_events{};
    std::atomic<bool> storage_failed{false};
    orbit::RetentionQueueSnapshot retention_snapshot() const { return durable?durable->snapshot():retained.snapshot(); }
    orbit::RetentionQueue<orbit::TelemetrySample> retained;
    orbit::GapTracker gaps;
    std::atomic<bool> acquisition_done{false};
    std::atomic<bool> stop_requested{false};
    std::atomic<bool> drain_active{false};
    std::atomic<ConnectionState> state{ConnectionState::disconnected};
    Counters counters;
    orbit::RunMetrics metrics;
    std::string source_label, storage_path;
    std::uint64_t started_ns{monotonic_ns()};
    std::array<std::uint64_t,7> category_counts{};
    std::uint64_t scenario_events{}, generation_late_max_ns{}, sensor_fault_reports{}, event_coalesced{}, event_overflow{};
    std::size_t configured_storage_batch{1};
    std::size_t configured_window{};bool priority_enabled{};
    orbit::CommandDispatcher commands;
    std::atomic<bool> network_done{false};
};

std::atomic<bool>* signal_stop = nullptr;
std::atomic<bool>* signal_drain = nullptr;
std::atomic<bool> early_stop_requested{false};

extern "C" void request_stop(int) {
    early_stop_requested.store(true);
    if (signal_stop != nullptr) signal_stop->store(true);
}

class Socket {
  public:
#ifndef ORBIT_TEST_PLAINTEXT
    SSL* tls{};
#endif
    Socket() = default;
    explicit Socket(int fd) : fd_(fd) {}
    ~Socket() { reset(); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    Socket(Socket&& other) noexcept { *this = std::move(other); }
    Socket& operator=(Socket&& other) noexcept {
        if (this != &other) {
            reset();
            fd_ = other.fd_;
            other.fd_ = -1;
#ifndef ORBIT_TEST_PLAINTEXT
            tls = other.tls;
            other.tls = nullptr;
#endif
        }
        return *this;
    }
    int get() const { return fd_; }
    explicit operator bool() const { return fd_ >= 0; }
    void reset() {
#ifndef ORBIT_TEST_PLAINTEXT
        if (tls) SSL_free(tls);
        tls = nullptr;
#endif
        if (fd_ >= 0) ::close(fd_);
        fd_ = -1;
    }

  private:
    int fd_{-1};
};

std::uint64_t parse_u64(const std::string& text, const char* name) {
    if (text.empty() || text.front() == '-')
        throw std::invalid_argument(std::string("invalid ") + name);
    std::size_t parsed = 0;
    const auto value = std::stoull(text, &parsed, 0);
    if (parsed != text.size()) throw std::invalid_argument(std::string("invalid ") + name);
    return value;
}

Config parse_args(int argc, char** argv) {
    if (argc < 3) throw std::invalid_argument("usage: flight_service HOST PORT [OPTIONS]");
    Config config;
    config.host = argv[1];
    const auto port = parse_u64(argv[2], "port");
    if (port == 0 || port > 65535) throw std::invalid_argument("port must be in 1..65535");
    config.port = static_cast<std::uint16_t>(port);
    for (int i = 3; i < argc; i += 2) {
        if (i + 1 >= argc) throw std::invalid_argument(std::string("missing value for ") + argv[i]);
        const std::string option = argv[i];
        if (option == "--source") { config.source=argv[i+1]; continue; }
        if (option == "--device") { config.device_path=argv[i+1];config.explicit_device=true; continue; }
        if (option == "--outbox") { config.outbox_path=argv[i+1]; continue; }
        if (option == "--authorized-command-server") { config.authorized_command_server=argv[i+1]; continue; }
        if (option == "--ca") { config.ca = argv[i + 1]; continue; }
        if (option == "--cert") { config.certificate = argv[i + 1]; continue; }
        if (option == "--key") { config.key = argv[i + 1]; continue; }
        if (option == "--server-name") { config.server_name = argv[i + 1]; continue; }
        const auto value = parse_u64(argv[i + 1], option.c_str());
        if (option == "--storage-batch") config.storage_batch=static_cast<std::size_t>(value);
        else if (option == "--send-window") config.send_window=static_cast<std::size_t>(value);
        else if (option == "--priority-scheduling") {if(value>1)throw std::invalid_argument("priority-scheduling must be0or1");config.priority_scheduling=value!=0;}
        else if (option == "--scenario-seed") config.scenario_seed=value;
        else if (option == "--outbox-bytes") config.outbox_bytes=value;
        else if (option == "--fake-fault") config.fake_fault = value != 0;
        else if (option == "--count") config.count = value;
        else if (option == "--interval-ms") config.interval_ms = value;
        else if (option == "--capacity") config.capacity = static_cast<std::size_t>(value);
        else if (option == "--connect-timeout-ms") config.connect_timeout_ms = value;
        else if (option == "--io-timeout-ms") config.io_timeout_ms = value;
        else if (option == "--drain-ms") config.drain_ms = value;
        else if (option == "--max-frames-per-iteration")
            config.max_frames_per_iteration = static_cast<std::size_t>(value);
        else if (option == "--boot-id") {config.boot_id = value;config.explicit_boot_id=true;}
        else if (option == "--ready-fd") {
            if (value > static_cast<std::uint64_t>(std::numeric_limits<int>::max()))
                throw std::invalid_argument("ready-fd is too large");
            config.ready_fd = static_cast<int>(value);
        }
        else throw std::invalid_argument("unknown option: " + option);
    }
    if(config.source!="fake" && config.source!="device" && config.source!="scenario" && config.source!="sensors")throw std::invalid_argument("source must be fake, device, scenario or sensors");
    if(config.source=="sensors" && !config.explicit_device)config.device_path="/dev/orbit_sensors";
    if(config.scenario_seed>UINT32_MAX)throw std::invalid_argument("scenario seed exceeds uint32");
    if(config.source=="scenario" && (config.interval_ms<1 || config.interval_ms>60000))throw std::invalid_argument("scenario period must be1..60000ms");
    if((config.source=="device" || config.source=="sensors") && (config.interval_ms<1 || config.interval_ms>60000 || config.fake_fault))
        throw std::invalid_argument("device source requires period1..60000ms and does not support fake-fault");
    if(config.storage_batch<1 || config.storage_batch>16)throw std::invalid_argument("storage-batch must be1..16");
    if(config.storage_batch>1 && (config.send_window<2 || config.outbox_path.empty()))throw std::invalid_argument("storage-batch requires durable outbox and window>1");
    if(config.send_window<1 || config.send_window>64)throw std::invalid_argument("send-window must be1..64");
    if((config.send_window>1 || config.priority_scheduling) && config.outbox_path.empty())throw std::invalid_argument("sending window requires durable --outbox");
    if (config.capacity == 0) throw std::invalid_argument("capacity must be positive");
    if (config.max_frames_per_iteration == 0)
        throw std::invalid_argument("max-frames-per-iteration must be positive");
    if (config.connect_timeout_ms == 0 || config.io_timeout_ms == 0)
        throw std::invalid_argument("network timeouts must be positive");
    config.drain_ms = std::min<std::uint64_t>(config.drain_ms, 3000);
#ifndef ORBIT_TEST_PLAINTEXT
    if (config.explicit_boot_id) throw std::invalid_argument("--boot-id is only allowed in the plaintext test fixture; durable production uses a fresh boot identity");
    if (config.outbox_path.empty()) throw std::invalid_argument("TLS flight service requires --outbox PATH for durable retention");
    if (config.ca.empty() || config.certificate.empty() || config.key.empty() || config.server_name.empty())
        throw std::invalid_argument("TLS requires --ca --cert --key --server-name");
    const auto validation = std::unique_ptr<SSL_CTX, decltype(&SSL_CTX_free)>(SSL_CTX_new(TLS_client_method()), SSL_CTX_free);
    if (!validation || SSL_CTX_load_verify_locations(validation.get(), config.ca.c_str(), nullptr) != 1 ||
        SSL_CTX_use_certificate_chain_file(validation.get(), config.certificate.c_str()) != 1 ||
        SSL_CTX_use_PrivateKey_file(validation.get(), config.key.c_str(), SSL_FILETYPE_PEM) != 1 ||
        SSL_CTX_check_private_key(validation.get()) != 1)
        throw std::invalid_argument("invalid TLS credentials");
#endif
    return config;
}

orbit::TelemetrySample make_sample(std::uint64_t index, std::uint64_t boot_id) {
    return orbit::TelemetrySample{
        1,
        boot_id,
        index + 1,
        7,
        static_cast<std::uint32_t>(index),
        1'000'000'000ULL + index * 100'000'000ULL,
        static_cast<std::int32_t>(-20'000 + (index % 80'001)),
        static_cast<std::uint32_t>(3'000 + (index % 401)),
    };
}

void retain_sample(const orbit::TelemetrySample& sample, Shared& shared) {
        shared.counters.generated.fetch_add(1);
        if(sample.category<shared.category_counts.size())++shared.category_counts[sample.category];
        if(sample.quality&1)++shared.sensor_fault_reports;
        if(shared.durable) {
            const auto displaced=shared.durable->push(sample);
            shared.counters.dropped_oldest.fetch_add(displaced.size());
            for(const auto& lost:displaced) shared.gaps.record_dropped(lost.telemetry_sequence);
        } else {
            const auto displaced=shared.retained.push(sample);
            if(displaced) {shared.counters.dropped_oldest.fetch_add(1);shared.gaps.record_dropped(displaced->telemetry_sequence);}
        }
}

void acquisition_loop(const Config& config, Shared& shared) {
    shared.commands.configure(static_cast<std::uint32_t>(config.interval_ms), config.fake_fault);
    if(shared.sensors) {
        if(!shared.sensors->set_rate(static_cast<std::uint32_t>(config.interval_ms)))throw std::runtime_error("sensor SET_RATE failed");
        shared.driver_stats=shared.sensors->stats();
        auto reported_driver_drops=shared.driver_stats.dropped;
        std::uint64_t acquired=0;
        while(acquired<config.count && !shared.stop_requested.load()) {
            shared.commands.execute(monotonic_ns());
            const auto records=shared.sensors->read(static_cast<std::size_t>(std::min<std::uint64_t>(16,config.count-acquired)));
            for(const auto& record:records) {
                retain_sample(orbit::SensorSource::telemetry(record,config.boot_id,++acquired),shared);
                if(record.category==6)++shared.scenario_events;
            }
            shared.driver_stats=shared.sensors->stats();
            if(shared.driver_stats.dropped>reported_driver_drops) {
                if(shared.durable)shared.durable->record_aggregate_loss(shared.driver_stats.dropped-reported_driver_drops);
                reported_driver_drops=shared.driver_stats.dropped;
            }
        }
        shared.acquisition_done.store(true);
        while(!shared.network_done.load() && !shared.stop_requested.load()) {shared.commands.execute(monotonic_ns());std::this_thread::sleep_for(Milliseconds(1));}
        shared.sensor_clock_current=shared.sensors->clock_info(true);
        shared.sensor_events=shared.sensors->events_info();
        shared.event_coalesced=shared.sensor_events.coalesced;shared.event_overflow=shared.sensor_events.overflow;
        return;
    }
    if(config.source=="scenario") {
        auto next=Clock::now();std::uint64_t sequence=0;std::uint32_t device_sequence=0;
        orbit_event_accumulator pending_events{};
        std::uint32_t scenario_generation=7;std::uint64_t scenario_offset=0;
        for(std::uint64_t tick=0;tick<config.count && !shared.stop_requested.load();) {
            shared.commands.execute(monotonic_ns());
            const auto state=shared.commands.state();
            if(!state.enabled) {next=Clock::now();std::this_thread::sleep_for(Milliseconds(1));continue;}
            if(Clock::now()<next) {std::this_thread::sleep_for(Milliseconds(1));continue;}
            const auto now=Clock::now();
            const auto late=std::chrono::duration_cast<std::chrono::nanoseconds>(now-next).count();
            shared.generation_late_max_ns=std::max(shared.generation_late_max_ns,static_cast<std::uint64_t>(std::max<std::int64_t>(0,late)));
            orbit_scenario_record routine{},event{};
            const bool reset=state.generation!=scenario_generation;
            if(reset) {scenario_generation=state.generation;device_sequence=0;scenario_offset=tick;}
            const auto has_event=orbit_scenario_tick(tick-scenario_offset,static_cast<std::uint32_t>(config.scenario_seed),&routine,&event);
            if(reset) {
                orbit_scenario_record reset_event{6,0,{7,0,4,1,0,0,0,0}};
                if(!orbit_event_push(&pending_events,&reset_event))throw std::runtime_error("reset event accumulator full");
            }
            if(routine.category==4) {
                routine.values[0]=static_cast<std::int32_t>(((monotonic_ns()-shared.started_ns)/1'000'000'000ULL)&0x7fffffffULL);
                routine.values[1]=static_cast<std::int32_t>(state.generation>=7?state.generation-7:0);
                // Source snapshot includes this generated record, staged here
                // before its handoff to retention; earlier reports are delivered.
                routine.values[2]=1;
                routine.values[3]=static_cast<std::int32_t>((sequence+1)&0x7fffffffULL);
                routine.values[4]=static_cast<std::int32_t>(sequence&0x7fffffffULL);
            }
            if(state.fault)routine.quality=1;
            retain_sample(orbit::scenario_sample(routine,config.boot_id,++sequence,device_sequence++,monotonic_ns(),state.generation),shared);
            if(has_event && !orbit_event_push(&pending_events,&event))throw std::runtime_error("native event accumulator full");
            while(orbit_event_pop(&pending_events,&event)) {retain_sample(orbit::scenario_sample(event,config.boot_id,++sequence,device_sequence++,monotonic_ns(),state.generation),shared);++shared.scenario_events;}
            shared.event_coalesced=pending_events.coalesced;shared.event_overflow=pending_events.overflow;
            ++tick; next+=Milliseconds(state.period_ms);
        }
        shared.acquisition_done.store(true);
        while(!shared.network_done.load() && !shared.stop_requested.load()) {
            shared.commands.execute(monotonic_ns());std::this_thread::sleep_for(Milliseconds(1));
        }
        return;
    }
    if(shared.device) {
        if(!shared.device->set_rate(static_cast<std::uint32_t>(config.interval_ms)))
            throw std::runtime_error("initial device SET_RATE failed");
        shared.driver_stats=shared.device->stats();
        std::uint64_t acquired=0;
        while(acquired<config.count && !shared.stop_requested.load()) {
            shared.commands.execute(monotonic_ns());
            const auto records=shared.device->read(static_cast<std::size_t>(std::min<std::uint64_t>(16,config.count-acquired)));
            for(const auto& record:records)retain_sample(orbit::DeviceSource::telemetry(record,config.boot_id,++acquired),shared);
            shared.driver_stats=shared.device->stats();
        }
        shared.acquisition_done.store(true);
        while(!shared.network_done.load() && !shared.stop_requested.load()) {
            shared.commands.execute(monotonic_ns());std::this_thread::sleep_for(Milliseconds(1));
        }
        return;
    }
    auto next = Clock::now();
    std::uint32_t device_sequence=0, generation=7;
    std::uint64_t index=0;
    while (index < config.count && !shared.stop_requested.load()) {
        shared.commands.execute(monotonic_ns());
        const auto device=shared.commands.state();
        if (device.generation != generation) { generation=device.generation; device_sequence=0; }
        if (!device.enabled || Clock::now()<next) { std::this_thread::sleep_for(Milliseconds(1));continue; }
        auto sample = make_sample(index++, config.boot_id);
        sample.device_generation=generation; sample.device_sequence=device_sequence++;
        sample.timestamp_monotonic_ns=monotonic_ns();
        if(device.fault) sample.temperature_millideg_c=200000;
        retain_sample(sample,shared);
        next=Clock::now()+Milliseconds(device.period_ms);
    }
    shared.acquisition_done.store(true);
    while (!shared.network_done.load() && !shared.stop_requested.load()) {
        shared.commands.execute(monotonic_ns());
        std::this_thread::sleep_for(Milliseconds(1));
    }
}

int wait_fd(int fd, short events, Clock::time_point deadline) {
    while (true) {
        if (signal_stop != nullptr && signal_stop->load() &&
            (signal_drain == nullptr || !signal_drain->load())) {
            return 0;
        }
        const auto now = Clock::now();
        if (now >= deadline) return 0;
        const auto remaining = std::chrono::duration_cast<Milliseconds>(deadline - now).count();
        pollfd descriptor{fd, events, 0};
        const auto poll_slice = std::min<std::int64_t>(25, std::max<std::int64_t>(1, remaining));
        const int result = ::poll(&descriptor, 1, static_cast<int>(poll_slice));
        if (result < 0 && errno == EINTR) continue;
        if (result < 0) return result;
        if (result == 0) continue;
        if ((descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) return -1;
        if ((descriptor.revents & events) != 0) return 1;
    }
}

Socket connect_with_deadline(const Config& config) {
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(config.port);
    const std::string numeric_host = config.host == "localhost" ? "127.0.0.1" : config.host;
    if (::inet_pton(AF_INET, numeric_host.c_str(), &address.sin_addr) != 1)
        throw std::runtime_error("HOST must be an IPv4 address or localhost");

    Socket socket(::socket(AF_INET, SOCK_STREAM, 0));
    if (!socket) throw std::runtime_error("socket failed");
    const int current_flags = ::fcntl(socket.get(), F_GETFL, 0);
    if (current_flags < 0 || ::fcntl(socket.get(), F_SETFL, current_flags | O_NONBLOCK) < 0)
        throw std::runtime_error("failed to make socket nonblocking");
#ifdef SO_NOSIGPIPE
    int enabled = 1;
    (void)::setsockopt(socket.get(), SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled));
#endif
    const int result = ::connect(socket.get(), reinterpret_cast<sockaddr*>(&address), sizeof(address));
    if (result != 0 && errno != EINPROGRESS) throw std::runtime_error("connect failed");
    if (result != 0) {
        const auto deadline = Clock::now() + Milliseconds(config.connect_timeout_ms);
        if (wait_fd(socket.get(), POLLOUT, deadline) != 1) throw std::runtime_error("connect timed out");
        int error = 0;
        socklen_t length = sizeof(error);
        if (::getsockopt(socket.get(), SOL_SOCKET, SO_ERROR, &error, &length) != 0 || error != 0)
            throw std::runtime_error("connect failed");
    }
    return socket;
}

bool send_bytes(Socket& socket, const std::vector<std::uint8_t>& bytes, Clock::time_point deadline) {
    const int fd = socket.get();
    std::size_t offset = 0;
    while (offset < bytes.size()) {
#ifndef ORBIT_TEST_PLAINTEXT
        ERR_clear_error();
        const auto sent = SSL_write(socket.tls, bytes.data() + offset, static_cast<int>(bytes.size() - offset));
        if (sent <= 0) {
            const auto error = SSL_get_error(socket.tls, sent);
            if (error == SSL_ERROR_WANT_READ || error == SSL_ERROR_WANT_WRITE) {
                if (wait_fd(fd, error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT, deadline) == 1) continue;
            }
            return false;
        }
#elif defined(MSG_NOSIGNAL)
        const auto sent = ::send(fd, bytes.data() + offset, bytes.size() - offset, MSG_NOSIGNAL);
#else
        const auto sent = ::send(fd, bytes.data() + offset, bytes.size() - offset, 0);
#endif
        if (sent > 0) {
            offset += static_cast<std::size_t>(sent);
            continue;
        }
        if (sent < 0 && errno == EINTR) continue;
        if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (wait_fd(fd, POLLOUT, deadline) == 1) continue;
        }
        return false;
    }
    return true;
}

bool receive_bytes(Socket& socket, std::vector<std::uint8_t>& bytes, std::size_t size,
                   Clock::time_point deadline) {
    const int fd = socket.get();
    bytes.resize(size);
    std::size_t offset = 0;
    while (offset < size) {
#ifndef ORBIT_TEST_PLAINTEXT
        ERR_clear_error();
        const auto received = SSL_read(socket.tls, bytes.data() + offset, static_cast<int>(size - offset));
        if (received <= 0) {
            const auto error = SSL_get_error(socket.tls, received);
            if (error == SSL_ERROR_WANT_READ || error == SSL_ERROR_WANT_WRITE) {
                if (wait_fd(fd, error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT, deadline) == 1) continue;
            }
            return false;
        }
#else
        const auto received = ::recv(fd, bytes.data() + offset, size - offset, 0);
#endif
        if (received > 0) {
            offset += static_cast<std::size_t>(received);
            continue;
        }
        if (received < 0 && errno == EINTR) continue;
        if (received < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (wait_fd(fd, POLLIN, deadline) == 1) continue;
        }
        return false;
    }
    return true;
}

enum class AckResult { valid, receive_failure, invalid };

bool receive_frame(Socket& socket, std::vector<std::uint8_t>& frame, Clock::time_point deadline) {
    if (!receive_bytes(socket,frame,orbit::kHeaderLength,deadline)) return false;
    auto size=orbit::read_u32(frame,12);
    if (size>orbit::kMaxPayloadLength) throw orbit::ProtocolError("payload too large");
    std::vector<std::uint8_t> payload;
    if (!receive_bytes(socket,payload,size,deadline)) return false;
    frame.insert(frame.end(),payload.begin(),payload.end());return true;
}

bool dispatch_command(Socket& socket, const orbit::DecodedFrame& frame, Shared& shared, bool authorized, Clock::time_point deadline) {
    if(frame.header.message_type!=orbit::kCommandRequest) return false;
    auto status=shared.commands.submit(frame.command,authorized,monotonic_ns());
    if(status!=orbit::CommandStatus::applied) return send_bytes(socket,orbit::encode_command_result({frame.command,status}),deadline);
    while(Clock::now()<deadline) {
        if(auto result=shared.commands.pop_result()) return send_bytes(socket,orbit::encode_command_result(*result),deadline);
        if(shared.stop_requested.load()) return false;
        std::this_thread::sleep_for(Milliseconds(1));
    }
    return false;
}

AckResult require_ack(Socket& socket, const orbit::TelemetrySample& sample, std::uint64_t message_sequence,
                      std::uint64_t timeout_ms, Shared& shared, bool authorized) {
    const auto deadline=Clock::now()+Milliseconds(timeout_ms);
    try {
        for (std::size_t count=0;count<17;++count) {
            std::vector<std::uint8_t> bytes;
            if(!receive_frame(socket,bytes,deadline)) return AckResult::receive_failure;
            const auto decoded=orbit::decode_frame(bytes);
            if(decoded.header.message_type==orbit::kCommandRequest) {
                if(!dispatch_command(socket,decoded,shared,authorized,deadline)) return AckResult::receive_failure;
                continue;
            }
            if(decoded.header.message_type!=orbit::kTelemetryAck || decoded.header.message_sequence!=message_sequence ||
               decoded.ack.spacecraft_id!=sample.spacecraft_id || decoded.ack.flight_boot_id!=sample.flight_boot_id ||
               decoded.ack.telemetry_sequence!=sample.telemetry_sequence) return AckResult::invalid;
            return AckResult::valid;
        }
    } catch(const std::exception&) { return AckResult::invalid; }
    return AckResult::invalid;
}

void network_loop(const Config& config, Shared& shared) {
#ifndef ORBIT_TEST_PLAINTEXT
    const auto context = std::unique_ptr<SSL_CTX, decltype(&SSL_CTX_free)>(SSL_CTX_new(TLS_client_method()), SSL_CTX_free);
    if (!context || SSL_CTX_set_min_proto_version(context.get(), TLS1_3_VERSION) != 1 ||
        SSL_CTX_load_verify_locations(context.get(), config.ca.c_str(), nullptr) != 1 ||
        SSL_CTX_use_certificate_chain_file(context.get(), config.certificate.c_str()) != 1 ||
        SSL_CTX_use_PrivateKey_file(context.get(), config.key.c_str(), SSL_FILETYPE_PEM) != 1 ||
        SSL_CTX_check_private_key(context.get()) != 1) {
        // Configuration failures are fatal rather than silently downgrading transport.
        shared.stop_requested.store(true);
        return;
    }
    SSL_CTX_set_verify(context.get(), SSL_VERIFY_PEER, nullptr);
#endif
    struct Completion { Shared& shared; ~Completion() { shared.commands.close(); shared.network_done.store(true); } } completion{shared};
    bool command_authorized=!config.authorized_command_server.empty() && config.authorized_command_server==config.server_name;
    Socket socket;
    std::uint64_t message_sequence = 1;
    std::uint64_t attempts_for_front = 0;
    std::mt19937_64 random(config.boot_id);
    orbit::RetryPolicy retry(Milliseconds(250), Milliseconds(5000), Milliseconds(62),
                             [&random](Milliseconds bound) {
                                 const auto span = static_cast<std::uint64_t>(bound.count() * 2 + 1);
                                 return Milliseconds(static_cast<std::int64_t>(random() % span) - bound.count());
                             });
    Clock::time_point drain_deadline = Clock::time_point::max();
    bool drain_started = false;
    std::optional<orbit::TelemetrySample> in_flight;
    struct PendingFrame {orbit::TelemetrySample sample;std::uint64_t sequence,sent_ns;Clock::time_point deadline;};
    std::vector<PendingFrame> window;
    std::vector<orbit::TelemetrySample> replayed;
    const auto same_identity=[](const auto& a,const auto& b){return a.spacecraft_id==b.spacecraft_id && a.flight_boot_id==b.flight_boot_id && a.telemetry_sequence==b.telemetry_sequence;};
    const auto wait_backoff = [&](Milliseconds delay) {
        const auto deadline = Clock::now() + delay;
        while (Clock::now() < deadline &&
               (!shared.stop_requested.load() || drain_started)) {
            if (drain_started && Clock::now() >= drain_deadline) break;
            auto slice = std::min(Milliseconds(25),
                                  std::chrono::duration_cast<Milliseconds>(deadline - Clock::now()));
            if (drain_started) {
                slice = std::min(slice, std::chrono::duration_cast<Milliseconds>(drain_deadline - Clock::now()));
            }
            if (slice > Milliseconds(0)) std::this_thread::sleep_for(slice);
        }
    };

    while (true) {
        if(!socket && !window.empty()) {
            replayed.clear();for(const auto& item:window)replayed.push_back(item.sample);
            window.clear();shared.durable->release_claims();
        }
        if ((shared.acquisition_done.load() || shared.stop_requested.load()) && !drain_started) {
            drain_started = true;
            shared.drain_active.store(true);
            // Leave scheduling/cleanup margin inside the externally observable
            // three-second shutdown contract while still making a real drain attempt.
            const auto budget = shared.stop_requested.load()
                                    ? std::min<std::uint64_t>(config.drain_ms, 2500)
                                    : config.drain_ms;
            drain_deadline = Clock::now() + Milliseconds(budget);
        }
        const auto queue_snapshot = shared.retention_snapshot();
        if (!in_flight && window.empty() && queue_snapshot.size == 0 && (!shared.durable || shared.durable->losses().empty())) {
            // Acquisition is the only producer. Do not close the queue until it
            // has explicitly finished: it may have passed its stop check and be
            // immediately about to perform its final push.
            if (shared.acquisition_done.load()) break;
#ifndef ORBIT_TEST_PLAINTEXT
            if(socket) {
                pollfd descriptor{socket.get(),POLLIN,0};
                if(SSL_pending(socket.tls)>0 || (::poll(&descriptor,1,0)>0 && descriptor.revents)) {
                    try {
                        std::vector<std::uint8_t> bytes;
                        auto deadline=Clock::now()+Milliseconds(config.io_timeout_ms);
                        if(!receive_frame(socket,bytes,deadline) || !dispatch_command(socket,orbit::decode_frame(bytes),shared,command_authorized,deadline)) { socket.reset();shared.commands.close(); }
                    } catch(const std::exception&) { socket.reset();shared.commands.close(); }
                }
            }
#endif
            std::this_thread::sleep_for(Milliseconds(1));
            if(socket) continue;
        }
        if (drain_started && Clock::now() >= drain_deadline) {
            if (shared.acquisition_done.load()) break;
            std::this_thread::sleep_for(Milliseconds(1));
            continue;
        }

        if (!socket) {
            shared.state.store(ConnectionState::connecting);
            shared.counters.connect_attempts.fetch_add(1);
            try {
                auto connect_config = config;
                if (drain_started) {
                    const auto remaining = std::max(
                        Milliseconds(1),
                        std::chrono::duration_cast<Milliseconds>(drain_deadline - Clock::now()));
                    connect_config.connect_timeout_ms = std::min<std::uint64_t>(
                        config.connect_timeout_ms,
                        static_cast<std::uint64_t>(remaining.count()));
                }
                socket = connect_with_deadline(connect_config);
                shared.state.store(ConnectionState::authenticating);
#ifndef ORBIT_TEST_PLAINTEXT
                socket.tls = SSL_new(context.get());
                if (!socket.tls || SSL_set_fd(socket.tls, socket.get()) != 1 ||
                    SSL_set1_host(socket.tls, config.server_name.c_str()) != 1 ||
                    SSL_set_tlsext_host_name(socket.tls, config.server_name.c_str()) != 1)
                    throw std::runtime_error("TLS setup failed");
                SSL_set_hostflags(socket.tls, X509_CHECK_FLAG_NEVER_CHECK_SUBJECT);
                const auto deadline = Clock::now() + Milliseconds(connect_config.connect_timeout_ms);
                while (true) {
                    ERR_clear_error();
                    const int result = SSL_connect(socket.tls);
                    if (result == 1) break;
                    const int error = SSL_get_error(socket.tls, result);
                    if ((error != SSL_ERROR_WANT_READ && error != SSL_ERROR_WANT_WRITE) ||
                        wait_fd(socket.get(), error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT, deadline) != 1)
                        throw std::runtime_error("TLS authentication failed");
                }
#endif
#ifndef ORBIT_TEST_PLAINTEXT
                // Role authorization uses an exact DNS SAN, independently of TLS hostname matching.
                command_authorized=false;
                X509* peer=SSL_get1_peer_certificate(socket.tls);
                if(peer) {
                    auto* names=static_cast<GENERAL_NAMES*>(X509_get_ext_d2i(peer,NID_subject_alt_name,nullptr,nullptr));
                    if(names) {
                        for(int i=0;i<sk_GENERAL_NAME_num(names);++i) {
                            auto* name=sk_GENERAL_NAME_value(names,i);
                            if(name->type==GEN_DNS) {
                                auto* dns=name->d.dNSName;
                                const std::string identity(reinterpret_cast<const char*>(ASN1_STRING_get0_data(dns)),ASN1_STRING_length(dns));
                                if(!config.authorized_command_server.empty() && identity==config.authorized_command_server) command_authorized=true;
                            }
                        }
                        GENERAL_NAMES_free(names);
                    }
                    X509_free(peer);
                }
                std::uint64_t session=0;
                if(RAND_bytes(reinterpret_cast<unsigned char*>(&session),sizeof(session))!=1 || !session) throw std::runtime_error("session entropy failed");
                shared.commands.open(session);
                std::vector<std::uint8_t> announcement;
                orbit::append_u64(announcement,session);orbit::append_u64(announcement,config.boot_id);orbit::append_u64(announcement,monotonic_ns());
                if(!send_bytes(socket,orbit::encode_control(orbit::kCommandSession,announcement),Clock::now()+Milliseconds(config.io_timeout_ms))) throw std::runtime_error("session send failed");
#endif
                shared.state.store(ConnectionState::active);
            } catch (const std::exception&) {
                socket.reset(); shared.commands.close();
                shared.counters.connect_failures.fetch_add(1);
                shared.state.store(ConnectionState::backoff);
                const auto delay = retry.next_delay();
                const auto remaining = drain_started
                                           ? std::chrono::duration_cast<Milliseconds>(drain_deadline - Clock::now())
                                           : delay;
                wait_backoff(std::max(Milliseconds(0), std::min(delay, remaining)));
                continue;
            }
        }

        if(shared.durable && window.empty()) {
            if(auto loss=shared.durable->claim_loss()) {
                orbit::LossReport report{1,loss->boot_id,loss->id,loss->first,loss->last,loss->count,loss->detail_complete?1U:2U};
                auto deadline=Clock::now()+Milliseconds(config.io_timeout_ms);
                if(drain_started) deadline=std::min(deadline,drain_deadline);
                bool accepted=false;
                try {
                    if(send_bytes(socket,orbit::encode_loss_report(report),deadline)) {
                        for(unsigned n=0;n<17;++n) {
                            std::vector<std::uint8_t> bytes;
                            if(!receive_frame(socket,bytes,deadline)) break;
                            auto decoded=orbit::decode_frame(bytes);
                            if(decoded.header.message_type==orbit::kCommandRequest) {
                                if(!dispatch_command(socket,decoded,shared,command_authorized,deadline)) break;
                                continue;
                            }
                            accepted=decoded.header.message_type==orbit::kLossAck && decoded.header.message_sequence==report.report_id && decoded.loss_ack.spacecraft_id==report.spacecraft_id && decoded.loss_ack.flight_boot_id==report.flight_boot_id && decoded.loss_ack.report_id==report.report_id;
                            break;
                        }
                    }
                } catch(const orbit::ProtocolError&) { accepted=false; }
                if(!accepted) {
                    socket.reset();shared.commands.close();shared.counters.reconnects.fetch_add(1);
                    shared.state.store(ConnectionState::backoff);wait_backoff(retry.next_delay());continue;
                }
                shared.durable->acknowledge_loss(loss->id);retry.reset();
            }
        }

        if(config.send_window>1) {
            bool failed=false,invalid=false;
            const bool drain_for_loss=!window.empty() && !shared.durable->losses().empty();
            while(!drain_for_loss && window.size()<config.send_window) {
                auto next=shared.durable->claim_next(config.send_window);
                if(!next)break;
                auto deadline=Clock::now()+Milliseconds(config.io_timeout_ms);
                if(!window.empty())deadline=std::min(deadline,window.front().deadline);
                if(drain_started)deadline=std::min(deadline,drain_deadline);
                const auto sent_ns=monotonic_ns();
                const auto frame=orbit::encode_telemetry_sample(*next,message_sequence++);
                shared.counters.transmission_attempts.fetch_add(1);
                if(std::any_of(replayed.begin(),replayed.end(),[&](const auto& prior){return same_identity(prior,*next);}))shared.counters.retransmissions.fetch_add(1);
                window.push_back({*next,message_sequence-1,sent_ns,Clock::now()+Milliseconds(config.io_timeout_ms)});
                if(!send_bytes(socket,frame,deadline)){shared.counters.send_failures.fetch_add(1);failed=true;break;}
                shared.metrics.sent(frame.size());
            }
            if(!failed && !window.empty()) {
                struct ReceivedAck {orbit::TelemetrySample sample;std::uint64_t latency,source_latency;};
                std::vector<ReceivedAck> acknowledged;
                try {
                    for(unsigned frames=0;frames<config.storage_batch+16 && !window.empty();++frames) {
                        if(!acknowledged.empty()) {
                            pollfd ready{socket.get(),POLLIN,0};
#ifndef ORBIT_TEST_PLAINTEXT
                            const bool buffered=SSL_pending(socket.tls)>0;
#else
                            const bool buffered=false;
#endif
                            if(!buffered && !(::poll(&ready,1,0)>0 && ready.revents))break;
                        }
                        auto deadline=window.front().deadline;
                        if(drain_started)deadline=std::min(deadline,drain_deadline);
                        std::vector<std::uint8_t> bytes;
                        if(!receive_frame(socket,bytes,deadline)){failed=true;break;}
                        const auto decoded=orbit::decode_frame(bytes);
                        if(decoded.header.message_type==orbit::kCommandRequest) {
                            if(!dispatch_command(socket,decoded,shared,command_authorized,deadline)){failed=true;break;}
                            continue;
                        }
                        auto found=std::find_if(window.begin(),window.end(),[&](const auto& item){return decoded.header.message_type==orbit::kTelemetryAck && decoded.header.message_sequence==item.sequence && same_identity(decoded.ack,item.sample);});
                        if(found==window.end()){invalid=true;failed=true;break;}
                        const auto now=monotonic_ns();
                        acknowledged.push_back({found->sample,now-found->sent_ns,found->sample.flight_boot_id==config.boot_id && found->sample.timestamp_monotonic_ns<=now?now-found->sample.timestamp_monotonic_ns:0});
                        window.erase(found);
                        if(acknowledged.size()>=config.storage_batch)break;
                    }
                }catch(const orbit::ProtocolError&){failed=true;invalid=true;}
                if(!acknowledged.empty()) {
                    std::vector<orbit::TelemetrySample> samples;
                    for(const auto& item:acknowledged)samples.push_back(item.sample);
                    shared.durable->acknowledge_batch(samples);
                    for(const auto& item:acknowledged)shared.metrics.acked(item.latency,item.source_latency);
                    shared.counters.acknowledged.fetch_add(samples.size());retry.reset();
                }else failed=true;
            }
            if(failed) {
                if(invalid)shared.counters.invalid_acks.fetch_add(1);else shared.counters.receive_failures.fetch_add(1);
                socket.reset();shared.commands.close();shared.counters.reconnects.fetch_add(1);
                shared.state.store(ConnectionState::backoff);wait_backoff(retry.next_delay());
            }
            continue;
        }

        std::size_t processed = 0;
        while (socket && processed < config.max_frames_per_iteration) {
            if (!in_flight) {
                in_flight = shared.durable?shared.durable->claim():shared.retained.try_pop();
            }
            if (!in_flight) break;
            const auto& sample = *in_flight;
            const auto frame = orbit::encode_telemetry_sample(sample, message_sequence);
            shared.counters.transmission_attempts.fetch_add(1);
            if (attempts_for_front > 0) shared.counters.retransmissions.fetch_add(1);
            ++attempts_for_front;
            auto operation_timeout = Milliseconds(config.io_timeout_ms);
            if (drain_started) {
                operation_timeout = std::min(
                    operation_timeout,
                    std::max(Milliseconds(1),
                             std::chrono::duration_cast<Milliseconds>(drain_deadline - Clock::now())));
            }
            const auto send_deadline = Clock::now() + operation_timeout;
            const auto sent_ns=monotonic_ns();
            if (!send_bytes(socket, frame, send_deadline)) {
                shared.counters.send_failures.fetch_add(1);
                socket.reset(); shared.commands.close();
                shared.counters.reconnects.fetch_add(1);
                shared.state.store(ConnectionState::backoff);
                wait_backoff(retry.next_delay());
                break;
            }
            shared.metrics.sent(frame.size());
            const auto ack = require_ack(
                socket, sample, message_sequence,
                static_cast<std::uint64_t>(operation_timeout.count()), shared, command_authorized);
            if (ack != AckResult::valid) {
                if (ack == AckResult::invalid) shared.counters.invalid_acks.fetch_add(1);
                else shared.counters.receive_failures.fetch_add(1);
                socket.reset(); shared.commands.close();
                shared.counters.reconnects.fetch_add(1);
                shared.state.store(ConnectionState::backoff);
                wait_backoff(retry.next_delay());
                break;
            }

            const auto ack_ns=monotonic_ns();
            if(shared.durable) shared.durable->acknowledge(sample);
            shared.metrics.acked(ack_ns-sent_ns,sample.flight_boot_id==config.boot_id && sample.timestamp_monotonic_ns<=ack_ns?ack_ns-sample.timestamp_monotonic_ns:0);
            shared.counters.acknowledged.fetch_add(1);
            retry.reset();
            in_flight.reset();
            attempts_for_front = 0;
            ++message_sequence;
            ++processed;
        }
    }

    socket.reset(); shared.commands.close();
    if(shared.durable)shared.durable->release_claims();
    shared.state.store(ConnectionState::stopping);
    std::uint64_t discarded = shared.durable?0:(in_flight ? 1 : 0);
    if(!shared.durable) discarded += shared.retained.discard(std::numeric_limits<std::size_t>::max());
    shared.counters.discarded_shutdown.fetch_add(discarded);
    shared.retained.close();
    shared.drain_active.store(false);
}

void print_summary(const Shared& shared) {
    const auto& c = shared.counters;
    const auto retained = shared.retention_snapshot();
    const auto gaps = shared.gaps.snapshot();
    const auto device = shared.commands.state();
    std::cout << "{\"state\":\"" << state_name(shared.state.load())
              << "\",\"generated\":" << c.generated.load()
              << ",\"acknowledged\":" << c.acknowledged.load()
              << ",\"dropped_oldest\":" << c.dropped_oldest.load()
              << ",\"discarded_shutdown\":" << c.discarded_shutdown.load()
              << ",\"connect_attempts\":" << c.connect_attempts.load()
              << ",\"connect_failures\":" << c.connect_failures.load()
              << ",\"reconnects\":" << c.reconnects.load()
              << ",\"send_failures\":" << c.send_failures.load()
              << ",\"receive_failures\":" << c.receive_failures.load()
              << ",\"invalid_acks\":" << c.invalid_acks.load()
              << ",\"transmission_attempts\":" << c.transmission_attempts.load()
              << ",\"retransmissions\":" << c.retransmissions.load()
              << ",\"queue_high_watermark\":" << retained.high_watermark
              << ",\"gap_total_dropped\":" << gaps.total_dropped
              << ",\"gap_omitted_dropped\":" << gaps.omitted_dropped
              << ",\"gap_detail_complete\":" << (gaps.detail_complete ? "true" : "false")
              << ",\"command_effects\":" << device.effects
              << ",\"source\":\"" << shared.source_label << "\"";
    if(shared.device || shared.sensors) {
        std::cout << ",\"driver_generated\":" << shared.driver_stats.generated
                  << ",\"driver_enqueued\":" << shared.driver_stats.enqueued
                  << ",\"driver_dropped\":" << shared.driver_stats.dropped
                  << ",\"driver_delivered\":" << shared.driver_stats.delivered
                  << ",\"driver_queued\":" << shared.driver_stats.queued;
    } else {
        std::cout << ",\"device_enabled\":" << (device.enabled ? "true" : "false")
                  << ",\"device_period_ms\":" << device.period_ms
                  << ",\"device_generation\":" << device.generation
                  << ",\"device_fault\":" << (device.fault ? "true" : "false");
    }
    std::cout
              << ",\"storage_failed\":" << (shared.storage_failed.load() ? "true" : "false")
              << ",\"durable\":" << (shared.durable ? "true" : "false")
              << ",\"retention_capacity\":" << retained.capacity
              << ",\"gap_ranges\":[";
    for (std::size_t index = 0; index < gaps.detailed_ranges.size(); ++index) {
        if (index != 0) std::cout << ',';
        std::cout << "[" << gaps.detailed_ranges[index].first << ','
                  << gaps.detailed_ranges[index].last << "]";
    }
    std::cout << "],\"retained\":" << retained.size;
    if (shared.durable) {
        const auto totals = shared.durable->accounting();
        std::cout << ",\"outbox_database_limit_bytes\":" << shared.durable->database_limit_bytes()
                  << ",\"durable_generated\":" << totals.generated
                  << ",\"durable_acknowledged\":" << totals.acknowledged
                  << ",\"durable_dropped\":" << totals.dropped
                  << ",\"durable_retained\":" << totals.retained
                  << ",\"durable_loss_acknowledged\":" << totals.loss_acknowledged;
    }
    std::cout<<",\"send_window\":"<<shared.configured_window<<",\"priority_scheduling\":"<<(shared.priority_enabled?"true":"false")
             <<",\"storage_batch\":"<<shared.configured_storage_batch
             <<",\"sensor_fault_reports\":"<<shared.sensor_fault_reports
             <<",\"event_coalesced\":"<<shared.event_coalesced<<",\"event_overflow\":"<<shared.event_overflow
             <<",\"reset_count\":"<<((shared.device||shared.sensors)?shared.driver_stats.reserved[1]:(device.generation>=7?device.generation-7:0));
    if(shared.sensors)std::cout<<",\"event_observations\":"<<shared.sensor_events.observations<<",\"event_pending\":"<<shared.sensor_events.pending
        <<",\"sensor_clock_offset_ns\":"<<shared.sensor_clock_initial.offset_ns
        <<",\"sensor_clock_uncertainty_ns\":"<<shared.sensor_clock_initial.uncertainty_ns
        <<",\"sensor_clock_current_offset_ns\":"<<shared.sensor_clock_current.offset_ns
        <<",\"sensor_clock_current_uncertainty_ns\":"<<shared.sensor_clock_current.uncertainty_ns;
    shared.metrics.json(std::cout);
    const auto elapsed=monotonic_ns()-shared.started_ns;
    struct rusage resources{};const bool have_usage=::getrusage(RUSAGE_SELF,&resources)==0;
    struct statvfs storage{};const bool have_storage=::statvfs(shared.storage_path.empty()?".":shared.storage_path.c_str(),&storage)==0;
    std::cout<<",\"run_uptime_ns\":"<<elapsed<<",\"scenario_events\":"<<shared.scenario_events
             <<",\"generation_late_max_ns\":"<<shared.generation_late_max_ns<<",\"category_generated\":[";
    for(std::size_t i=0;i<shared.category_counts.size();++i) {if(i)std::cout<<',';std::cout<<shared.category_counts[i];}
    std::cout<<"],\"storage_free_bytes\":";
    if(have_storage)std::cout<<static_cast<std::uint64_t>(storage.f_bavail)*storage.f_frsize;else std::cout<<"null";
    std::cout<<",\"process_peak_rss_bytes\":";
    if(have_usage) {
#ifdef __APPLE__
        std::cout<<resources.ru_maxrss;
#else
        std::cout<<static_cast<std::uint64_t>(resources.ru_maxrss)*1024;
#endif
    } else std::cout<<"null";
    std::cout<<",\"process_cpu_us\":";
    if(have_usage)std::cout<<static_cast<std::uint64_t>(resources.ru_utime.tv_sec+resources.ru_stime.tv_sec)*1000000+resources.ru_utime.tv_usec+resources.ru_stime.tv_usec;else std::cout<<"null";
    std::cout << "}\n";
}

}  // namespace

int main(int argc, char** argv) {
    early_stop_requested.store(false);
    ::signal(SIGINT, request_stop);
    ::signal(SIGTERM, request_stop);
    ::signal(SIGPIPE, SIG_IGN);
    try {
        const auto config = parse_args(argc, argv);
        Shared shared(config);
        signal_stop = &shared.stop_requested;
        signal_drain = &shared.drain_active;
        if (early_stop_requested.load()) shared.stop_requested.store(true);
        auto guarded=[&](auto loop) {
            try { loop(config,shared); }
            catch(const std::exception& error) {
                std::cerr<<"flight_service: retention/network failure: "<<error.what()<<"\n";
                shared.storage_failed.store(true);shared.stop_requested.store(true);shared.acquisition_done.store(true);
            }
        };
        std::thread acquisition([&]{guarded(acquisition_loop);});
        std::thread network([&]{guarded(network_loop);});
        if (config.ready_fd >= 0) {
            const char ready = 'R';
            ssize_t notified;
            do { notified=::write(config.ready_fd, &ready, 1); } while(notified<0 && errno==EINTR);
            if(notified!=1) {
                std::cerr<<"flight_service: ready notification failed\n";
                shared.storage_failed.store(true);shared.stop_requested.store(true);
            }
            (void)::close(config.ready_fd);
        }
        acquisition.join();
        network.join();
        signal_stop = nullptr;
        signal_drain = nullptr;
        print_summary(shared);
        return shared.storage_failed.load()?2:0;
    } catch (const std::exception& error) {
        std::cerr << "flight_service: " << error.what() << '\n';
        return 2;
    }
}
