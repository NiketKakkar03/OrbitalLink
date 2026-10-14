#include "orbit_protocol.hpp"
#include "gap_tracker.hpp"
#include "retention_queue.hpp"
#include "retry_policy.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
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
#include <openssl/err.h>
#include <openssl/x509v3.h>
#endif

namespace {

using Clock = std::chrono::steady_clock;
using Milliseconds = std::chrono::milliseconds;

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
    std::string ca, certificate, key, server_name;
    std::string host;
    std::uint16_t port{};
    std::uint64_t count{100};
    std::uint64_t interval_ms{100};
    std::size_t capacity{1024};
    std::uint64_t connect_timeout_ms{500};
    std::uint64_t io_timeout_ms{500};
    std::uint64_t drain_ms{3000};
    std::size_t max_frames_per_iteration{16};
    std::uint64_t boot_id{[] {
        std::random_device entropy;
        std::uniform_int_distribution<std::uint64_t> distribution;
        return distribution(entropy);
    }()};
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
    explicit Shared(std::size_t capacity) : retained(capacity), gaps(16) {}
    orbit::RetentionQueue<orbit::TelemetrySample> retained;
    orbit::GapTracker gaps;
    std::atomic<bool> acquisition_done{false};
    std::atomic<bool> stop_requested{false};
    std::atomic<bool> drain_active{false};
    std::atomic<ConnectionState> state{ConnectionState::disconnected};
    Counters counters;
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
        if (option == "--ca") { config.ca = argv[i + 1]; continue; }
        if (option == "--cert") { config.certificate = argv[i + 1]; continue; }
        if (option == "--key") { config.key = argv[i + 1]; continue; }
        if (option == "--server-name") { config.server_name = argv[i + 1]; continue; }
        const auto value = parse_u64(argv[i + 1], option.c_str());
        if (option == "--count") config.count = value;
        else if (option == "--interval-ms") config.interval_ms = value;
        else if (option == "--capacity") config.capacity = static_cast<std::size_t>(value);
        else if (option == "--connect-timeout-ms") config.connect_timeout_ms = value;
        else if (option == "--io-timeout-ms") config.io_timeout_ms = value;
        else if (option == "--drain-ms") config.drain_ms = value;
        else if (option == "--max-frames-per-iteration")
            config.max_frames_per_iteration = static_cast<std::size_t>(value);
        else if (option == "--boot-id") config.boot_id = value;
        else if (option == "--ready-fd") {
            if (value > static_cast<std::uint64_t>(std::numeric_limits<int>::max()))
                throw std::invalid_argument("ready-fd is too large");
            config.ready_fd = static_cast<int>(value);
        }
        else throw std::invalid_argument("unknown option: " + option);
    }
    if (config.capacity == 0) throw std::invalid_argument("capacity must be positive");
    if (config.max_frames_per_iteration == 0)
        throw std::invalid_argument("max-frames-per-iteration must be positive");
    if (config.connect_timeout_ms == 0 || config.io_timeout_ms == 0)
        throw std::invalid_argument("network timeouts must be positive");
    config.drain_ms = std::min<std::uint64_t>(config.drain_ms, 3000);
#ifndef ORBIT_TEST_PLAINTEXT
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

void acquisition_loop(const Config& config, Shared& shared) {
    auto next = Clock::now();
    for (std::uint64_t index = 0; index < config.count && !shared.stop_requested.load(); ++index) {
        auto sample = make_sample(index, config.boot_id);
        shared.counters.generated.fetch_add(1);
        const auto displaced = shared.retained.push(sample);
        if (displaced) {
            shared.counters.dropped_oldest.fetch_add(1);
            shared.gaps.record_dropped(displaced->telemetry_sequence);
        }
        next += Milliseconds(config.interval_ms);
        while (config.interval_ms != 0 && Clock::now() < next && !shared.stop_requested.load()) {
            std::this_thread::sleep_for(std::min(Milliseconds(10),
                                                 std::chrono::duration_cast<Milliseconds>(next - Clock::now())));
        }
    }
    shared.acquisition_done.store(true);
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

AckResult require_ack(Socket& socket, const orbit::TelemetrySample& sample, std::uint64_t message_sequence,
                      std::uint64_t timeout_ms) {
    const auto deadline = Clock::now() + Milliseconds(timeout_ms);
    std::vector<std::uint8_t> header;
    if (!receive_bytes(socket, header, orbit::kHeaderLength, deadline)) return AckResult::receive_failure;
    const auto payload_length = orbit::read_u32(header, 12);
    if (payload_length > orbit::kMaxPayloadLength) return AckResult::invalid;
    std::vector<std::uint8_t> payload;
    if (!receive_bytes(socket, payload, payload_length, deadline)) return AckResult::receive_failure;
    header.insert(header.end(), payload.begin(), payload.end());
    try {
        const auto decoded = orbit::decode_frame(header);
        if (decoded.header.message_type != orbit::kTelemetryAck ||
            decoded.header.message_sequence != message_sequence ||
            decoded.ack.spacecraft_id != sample.spacecraft_id ||
            decoded.ack.flight_boot_id != sample.flight_boot_id ||
            decoded.ack.telemetry_sequence != sample.telemetry_sequence) {
            return AckResult::invalid;
        }
    } catch (const std::exception&) {
        return AckResult::invalid;
    }
    return AckResult::valid;
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
        const auto queue_snapshot = shared.retained.snapshot();
        if (!in_flight && queue_snapshot.size == 0) {
            // Acquisition is the only producer. Do not close the queue until it
            // has explicitly finished: it may have passed its stop check and be
            // immediately about to perform its final push.
            if (shared.acquisition_done.load()) break;
            std::this_thread::sleep_for(Milliseconds(10));
            continue;
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
                shared.state.store(ConnectionState::active);
            } catch (const std::exception&) {
                socket.reset();
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

        std::size_t processed = 0;
        while (socket && processed < config.max_frames_per_iteration) {
            if (!in_flight) {
                in_flight = shared.retained.try_pop();
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
            if (!send_bytes(socket, frame, send_deadline)) {
                shared.counters.send_failures.fetch_add(1);
                socket.reset();
                shared.counters.reconnects.fetch_add(1);
                shared.state.store(ConnectionState::backoff);
                wait_backoff(retry.next_delay());
                break;
            }
            const auto ack = require_ack(
                socket, sample, message_sequence,
                static_cast<std::uint64_t>(operation_timeout.count()));
            if (ack != AckResult::valid) {
                if (ack == AckResult::invalid) shared.counters.invalid_acks.fetch_add(1);
                else shared.counters.receive_failures.fetch_add(1);
                socket.reset();
                shared.counters.reconnects.fetch_add(1);
                shared.state.store(ConnectionState::backoff);
                wait_backoff(retry.next_delay());
                break;
            }

            shared.counters.acknowledged.fetch_add(1);
            retry.reset();
            in_flight.reset();
            attempts_for_front = 0;
            ++message_sequence;
            ++processed;
        }
    }

    socket.reset();
    shared.state.store(ConnectionState::stopping);
    std::uint64_t discarded = in_flight ? 1 : 0;
    discarded += shared.retained.discard(std::numeric_limits<std::size_t>::max());
    shared.counters.discarded_shutdown.fetch_add(discarded);
    shared.retained.close();
    shared.drain_active.store(false);
}

void print_summary(const Shared& shared) {
    const auto& c = shared.counters;
    const auto retained = shared.retained.snapshot();
    const auto gaps = shared.gaps.snapshot();
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
              << ",\"gap_ranges\":[";
    for (std::size_t index = 0; index < gaps.detailed_ranges.size(); ++index) {
        if (index != 0) std::cout << ',';
        std::cout << "[" << gaps.detailed_ranges[index].first << ','
                  << gaps.detailed_ranges[index].last << "]";
    }
    std::cout << "],\"retained\":" << retained.size << "}\n";
}

}  // namespace

int main(int argc, char** argv) {
    early_stop_requested.store(false);
    ::signal(SIGINT, request_stop);
    ::signal(SIGTERM, request_stop);
    ::signal(SIGPIPE, SIG_IGN);
    try {
        const auto config = parse_args(argc, argv);
        Shared shared(config.capacity);
        signal_stop = &shared.stop_requested;
        signal_drain = &shared.drain_active;
        if (early_stop_requested.load()) shared.stop_requested.store(true);
        std::thread acquisition(acquisition_loop, std::cref(config), std::ref(shared));
        std::thread network(network_loop, std::cref(config), std::ref(shared));
        if (config.ready_fd >= 0) {
            const char ready = 'R';
            (void)::write(config.ready_fd, &ready, 1);
            (void)::close(config.ready_fd);
        }
        acquisition.join();
        network.join();
        signal_stop = nullptr;
        signal_drain = nullptr;
        print_summary(shared);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "flight_service: " << error.what() << '\n';
        return 2;
    }
}
