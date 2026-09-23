#include "orbit_protocol.hpp"
#include "command_control.hpp"
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
#include <openssl/err.h>
#include <openssl/rand.h>
#include <openssl/ssl.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <deque>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

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
    std::string host;
    std::uint16_t port{};
    std::uint64_t count{100};
    std::uint64_t interval_ms{100};
    std::size_t capacity{1024};
    std::uint64_t connect_timeout_ms{500};
    std::uint64_t io_timeout_ms{500};
    std::uint64_t drain_ms{3000};
    std::size_t max_frames_per_iteration{16};
    std::uint64_t boot_id{0x0102030405060708ULL};
    bool boot_id_explicit{false};
    int ready_fd{-1};
    std::string ca_file;
    std::string client_cert;
    std::string client_key;
    std::string server_name;
    std::string command_authorized_server_name;
    std::uint64_t command_execution_delay_ms{0};
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
    std::mutex command_mutex;
    std::deque<std::pair<std::uint64_t, orbit::CommandAuthorization>> session_resets;
    std::deque<orbit::CommandRequest> command_requests;
    std::deque<orbit::CommandResult> command_results;
    static constexpr std::size_t kCommandCapacity = 16;
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
    Socket() = default;
    explicit Socket(int fd) : fd_(fd) {}
    ~Socket() { reset(); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    Socket(Socket&& other) noexcept : fd_(other.fd_) { other.fd_ = -1; }
    Socket& operator=(Socket&& other) noexcept {
        if (this != &other) {
            reset();
            fd_ = other.fd_;
            other.fd_ = -1;
        }
        return *this;
    }
    int get() const { return fd_; }
    explicit operator bool() const { return fd_ >= 0; }
    void reset() {
        if (fd_ >= 0) ::close(fd_);
        fd_ = -1;
    }

  private:
    int fd_{-1};
};

int wait_fd(int fd, short events, Clock::time_point deadline);
Socket connect_with_deadline(const Config& config);

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
        const std::string raw_value = argv[i + 1];
        if (option == "--ca-file") config.ca_file = raw_value;
        else if (option == "--client-cert") config.client_cert = raw_value;
        else if (option == "--client-key") config.client_key = raw_value;
        else if (option == "--server-name") config.server_name = raw_value;
        else if (option == "--command-authorized-server-name")
            config.command_authorized_server_name = raw_value;
        else {
        const auto value = parse_u64(raw_value, option.c_str());
        if (option == "--count") config.count = value;
        else if (option == "--interval-ms") config.interval_ms = value;
        else if (option == "--capacity") config.capacity = static_cast<std::size_t>(value);
        else if (option == "--connect-timeout-ms") config.connect_timeout_ms = value;
        else if (option == "--io-timeout-ms") config.io_timeout_ms = value;
        else if (option == "--drain-ms") config.drain_ms = value;
        else if (option == "--command-execution-delay-ms") config.command_execution_delay_ms = value;
        else if (option == "--max-frames-per-iteration")
            config.max_frames_per_iteration = static_cast<std::size_t>(value);
        else if (option == "--boot-id") {
            config.boot_id = value;
            config.boot_id_explicit = true;
        }
        else if (option == "--ready-fd") {
            if (value > static_cast<std::uint64_t>(std::numeric_limits<int>::max()))
                throw std::invalid_argument("ready-fd is too large");
            config.ready_fd = static_cast<int>(value);
        }
        else throw std::invalid_argument("unknown option: " + option);
        }
    }
    if (config.capacity == 0) throw std::invalid_argument("capacity must be positive");
    if (config.max_frames_per_iteration == 0)
        throw std::invalid_argument("max-frames-per-iteration must be positive");
    if (config.connect_timeout_ms == 0 || config.io_timeout_ms == 0)
        throw std::invalid_argument("network timeouts must be positive");
    if (config.ca_file.empty() || config.client_cert.empty() || config.client_key.empty() ||
        config.server_name.empty()) {
        throw std::invalid_argument("TLS is mandatory: pass --ca-file, --client-cert, --client-key, and --server-name");
    }
    config.drain_ms = std::min<std::uint64_t>(config.drain_ms, 3000);
    return config;
}

class TlsContext {
  public:
    explicit TlsContext(const Config& config) {
        SSL_library_init();
        SSL_load_error_strings();
        ctx_ = SSL_CTX_new(TLS_client_method());
        if (ctx_ == nullptr) throw std::runtime_error("SSL_CTX_new failed");
        SSL_CTX_set_min_proto_version(ctx_, TLS1_3_VERSION);
        SSL_CTX_set_max_proto_version(ctx_, TLS1_3_VERSION);
        SSL_CTX_set_verify(ctx_, SSL_VERIFY_PEER, nullptr);
        if (SSL_CTX_load_verify_locations(ctx_, config.ca_file.c_str(), nullptr) != 1)
            throw std::runtime_error("failed to load CA file");
        if (SSL_CTX_use_certificate_file(ctx_, config.client_cert.c_str(), SSL_FILETYPE_PEM) != 1)
            throw std::runtime_error("failed to load client certificate");
        if (SSL_CTX_use_PrivateKey_file(ctx_, config.client_key.c_str(), SSL_FILETYPE_PEM) != 1)
            throw std::runtime_error("failed to load client key");
        if (SSL_CTX_check_private_key(ctx_) != 1)
            throw std::runtime_error("client key does not match certificate");
    }
    ~TlsContext() { SSL_CTX_free(ctx_); }
    SSL_CTX* get() const { return ctx_; }
    TlsContext(const TlsContext&) = delete;
    TlsContext& operator=(const TlsContext&) = delete;

  private:
    SSL_CTX* ctx_{nullptr};
};

class TlsConnection {
  public:
    TlsConnection(Socket socket, SSL* ssl) : socket_(std::move(socket)), ssl_(ssl) {}
    ~TlsConnection() { reset(); }
    TlsConnection(const TlsConnection&) = delete;
    TlsConnection& operator=(const TlsConnection&) = delete;
    TlsConnection(TlsConnection&& other) noexcept : socket_(std::move(other.socket_)), ssl_(other.ssl_) {
        other.ssl_ = nullptr;
    }
    TlsConnection& operator=(TlsConnection&& other) noexcept {
        if (this != &other) {
            reset();
            socket_ = std::move(other.socket_);
            ssl_ = other.ssl_;
            other.ssl_ = nullptr;
        }
        return *this;
    }
    explicit operator bool() const { return ssl_ != nullptr && static_cast<bool>(socket_); }
    int fd() const { return socket_.get(); }
    SSL* ssl() const { return ssl_; }
    void reset() {
        if (ssl_ != nullptr) {
            (void)SSL_shutdown(ssl_);
            SSL_free(ssl_);
            ssl_ = nullptr;
        }
        socket_.reset();
    }

  private:
    Socket socket_;
    SSL* ssl_{nullptr};
};

bool wait_ssl_retry(int fd, int error, Clock::time_point deadline) {
    if (error == SSL_ERROR_WANT_READ) return wait_fd(fd, POLLIN, deadline) == 1;
    if (error == SSL_ERROR_WANT_WRITE) return wait_fd(fd, POLLOUT, deadline) == 1;
    return false;
}

TlsConnection connect_tls_with_deadline(const Config& config, TlsContext& context) {
    auto socket = connect_with_deadline(config);
    SSL* ssl = SSL_new(context.get());
    if (ssl == nullptr) throw std::runtime_error("SSL_new failed");
    TlsConnection connection(std::move(socket), ssl);
    SSL_set_fd(ssl, connection.fd());
    SSL_set_tlsext_host_name(ssl, config.server_name.c_str());
    if (SSL_set1_host(ssl, config.server_name.c_str()) != 1)
        throw std::runtime_error("failed to configure server identity verification");
    const auto deadline = Clock::now() + Milliseconds(config.connect_timeout_ms);
    while (true) {
        const int result = SSL_connect(ssl);
        if (result == 1) return connection;
        const int error = SSL_get_error(ssl, result);
        if (!wait_ssl_retry(connection.fd(), error, deadline))
            throw std::runtime_error("TLS handshake failed");
    }
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

std::uint64_t monotonic_ms() {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<Milliseconds>(Clock::now().time_since_epoch()).count());
}

std::uint64_t random_nonzero_u64() {
    std::uint64_t value = 0;
    do {
        if (RAND_bytes(reinterpret_cast<unsigned char*>(&value), sizeof(value)) != 1)
            throw std::runtime_error("secure random generation failed");
    } while (value == 0);
    return value;
}

void process_command_work(const Config& config, Shared& shared, orbit::FakeSourceControl& source,
                          std::unique_ptr<orbit::CommandDispatcher>& dispatcher,
                          orbit::CommandAuthorization& authorization) {
    if (config.command_execution_delay_ms != 0) {
        bool has_requests = false;
        {
            std::lock_guard<std::mutex> lock(shared.command_mutex);
            has_requests = !shared.command_requests.empty();
        }
        if (has_requests)
            std::this_thread::sleep_for(Milliseconds(config.command_execution_delay_ms));
    }
    std::deque<std::pair<std::uint64_t, orbit::CommandAuthorization>> resets;
    std::deque<orbit::CommandRequest> requests;
    std::lock_guard<std::mutex> lock(shared.command_mutex);
    resets.swap(shared.session_resets);
    requests.swap(shared.command_requests);
    for (const auto& [session_id, session_authorization] : resets) {
        authorization = session_authorization;
        dispatcher = std::make_unique<orbit::CommandDispatcher>(
            session_id, Shared::kCommandCapacity, 64);
    }
    if (!dispatcher) return;

    std::deque<orbit::CommandResult> results;
    for (const auto& request : requests) {
        const auto admission = dispatcher->enqueue(
            request, monotonic_ms(), authorization, source);
        if (admission.result) results.push_back(*admission.result);
    }
    while (const auto result = dispatcher->execute_one(monotonic_ms(), source)) {
        results.push_back(*result);
        if (config.command_execution_delay_ms != 0)
            std::this_thread::sleep_for(Milliseconds(config.command_execution_delay_ms));
    }
    if (!results.empty()) {
        for (const auto& result : results) shared.command_results.push_back(result);
    }
}

void acquisition_loop(const Config& config, Shared& shared) {
    orbit::FakeSourceControl source(config.interval_ms);
    std::unique_ptr<orbit::CommandDispatcher> dispatcher;
    orbit::CommandAuthorization authorization{};
    auto next = Clock::now();
    std::uint64_t index = 0;
    while (index < config.count && !shared.stop_requested.load()) {
        process_command_work(config, shared, source, dispatcher, authorization);
        if (!source.state().enabled) {
            std::this_thread::sleep_for(Milliseconds(5));
            next = Clock::now();
            continue;
        }
        auto sample = make_sample(index, config.boot_id);
        sample.device_generation = static_cast<std::uint32_t>(source.state().generation);
        shared.counters.generated.fetch_add(1);
        const auto displaced = shared.retained.push(sample);
        if (displaced) {
            shared.counters.dropped_oldest.fetch_add(1);
            shared.gaps.record_dropped(displaced->telemetry_sequence);
        }
        ++index;
        const auto period_ms = source.state().period_ms;
        next += Milliseconds(period_ms);
        while (period_ms != 0 && Clock::now() < next && !shared.stop_requested.load()) {
            process_command_work(config, shared, source, dispatcher, authorization);
            std::this_thread::sleep_for(std::min(Milliseconds(10),
                                                 std::chrono::duration_cast<Milliseconds>(next - Clock::now())));
        }
    }
    process_command_work(config, shared, source, dispatcher, authorization);
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

bool send_tls_bytes(TlsConnection& connection, const std::vector<std::uint8_t>& bytes,
                    Clock::time_point deadline) {
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const int written = SSL_write(connection.ssl(), bytes.data() + offset,
                                      static_cast<int>(bytes.size() - offset));
        if (written > 0) {
            offset += static_cast<std::size_t>(written);
            continue;
        }
        const int error = SSL_get_error(connection.ssl(), written);
        if (wait_ssl_retry(connection.fd(), error, deadline)) continue;
        return false;
    }
    return true;
}

bool receive_tls_bytes(TlsConnection& connection, std::vector<std::uint8_t>& bytes, std::size_t size,
                       Clock::time_point deadline) {
    bytes.resize(size);
    std::size_t offset = 0;
    while (offset < size) {
        const int received = SSL_read(connection.ssl(), bytes.data() + offset,
                                      static_cast<int>(size - offset));
        if (received > 0) {
            offset += static_cast<std::size_t>(received);
            continue;
        }
        const int error = SSL_get_error(connection.ssl(), received);
        if (wait_ssl_retry(connection.fd(), error, deadline)) continue;
        return false;
    }
    return true;
}

enum class AckResult { valid, receive_failure, invalid };

bool receive_frame(TlsConnection& connection, orbit::DecodedFrame& decoded,
                   Clock::time_point deadline) {
    std::vector<std::uint8_t> header;
    if (!receive_tls_bytes(connection, header, orbit::kHeaderLength, deadline)) return false;
    const auto payload_length = orbit::read_u32(header, 12);
    if (payload_length > orbit::kMaxPayloadLength) throw orbit::ProtocolError("payload too large");
    std::vector<std::uint8_t> payload;
    if (!receive_tls_bytes(connection, payload, payload_length, deadline)) return false;
    header.insert(header.end(), payload.begin(), payload.end());
    decoded = orbit::decode_frame(header);
    return true;
}

void queue_command_request(Shared& shared, const orbit::CommandRequest& request) {
    std::lock_guard<std::mutex> lock(shared.command_mutex);
    if (shared.command_requests.size() < Shared::kCommandCapacity) {
        shared.command_requests.push_back(request);
        return;
    }
    shared.command_results.push_back(orbit::CommandResult{
        request.command_session_id, request.command_sequence, request.command_id,
        request.command_type, orbit::kResultRejected, orbit::kRejectQueueFull, 0, 0, 0});
}

bool send_command_results(TlsConnection& connection, Shared& shared,
                          std::uint64_t& message_sequence, Clock::time_point deadline) {
    while (true) {
        std::optional<orbit::CommandResult> result;
        {
            std::lock_guard<std::mutex> lock(shared.command_mutex);
            if (shared.command_results.empty()) return true;
            result = shared.command_results.front();
            shared.command_results.pop_front();
        }
        if (!send_tls_bytes(connection, orbit::encode_command_result(*result, message_sequence++), deadline))
            return false;
    }
}

AckResult require_ack(TlsConnection& connection, const orbit::TelemetrySample& sample, std::uint64_t message_sequence,
                      std::uint64_t timeout_ms, Shared& shared,
                      std::uint64_t& outbound_message_sequence) {
    const auto deadline = Clock::now() + Milliseconds(timeout_ms);
    while (Clock::now() < deadline) {
        try {
            orbit::DecodedFrame decoded;
            if (!receive_frame(connection, decoded, deadline)) return AckResult::receive_failure;
            if (decoded.header.message_type == orbit::kCommandRequest) {
                queue_command_request(shared, decoded.command_request);
                if (!send_command_results(connection, shared, outbound_message_sequence, deadline))
                    return AckResult::receive_failure;
                continue;
            }
        if (decoded.header.message_type != orbit::kTelemetryAck ||
            decoded.header.message_sequence != message_sequence ||
            decoded.ack.spacecraft_id != sample.spacecraft_id ||
            decoded.ack.flight_boot_id != sample.flight_boot_id ||
            decoded.ack.telemetry_sequence != sample.telemetry_sequence) {
            return AckResult::invalid;
        }
            return AckResult::valid;
        } catch (const std::exception&) {
            return AckResult::invalid;
        }
    }
    return AckResult::receive_failure;
}

void network_loop(const Config& config, Shared& shared) {
    TlsContext tls_context(config);
    TlsConnection socket(Socket(), nullptr);
    std::uint64_t message_sequence = 1;
    std::uint64_t command_session_id = 0;
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
            if (socket) {
                const auto deadline = Clock::now() + Milliseconds(10);
                if (!send_command_results(socket, shared, message_sequence, deadline)) {
                    socket.reset();
                    shared.counters.reconnects.fetch_add(1);
                    continue;
                }
                if (SSL_pending(socket.ssl()) > 0 || wait_fd(socket.fd(), POLLIN, deadline) == 1) {
                    try {
                        orbit::DecodedFrame decoded;
                        if (!receive_frame(socket, decoded, deadline) ||
                            decoded.header.message_type != orbit::kCommandRequest) {
                            socket.reset();
                            shared.counters.reconnects.fetch_add(1);
                            continue;
                        }
                        queue_command_request(shared, decoded.command_request);
                    } catch (const std::exception&) {
                        socket.reset();
                        shared.counters.reconnects.fetch_add(1);
                        continue;
                    }
                }
            }
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
                socket = connect_tls_with_deadline(connect_config, tls_context);
                shared.state.store(ConnectionState::authenticating);
                command_session_id = random_nonzero_u64();
                {
                    std::lock_guard<std::mutex> lock(shared.command_mutex);
                    shared.command_requests.clear();
                    shared.command_results.clear();
                    shared.session_resets.clear();
                    const bool may_control = !config.command_authorized_server_name.empty() &&
                                             config.server_name == config.command_authorized_server_name;
                    shared.session_resets.emplace_back(
                        command_session_id, orbit::CommandAuthorization{may_control});
                }
                const bool may_control = !config.command_authorized_server_name.empty() &&
                                         config.server_name == config.command_authorized_server_name;
                const orbit::CommandSession session{
                    command_session_id, config.boot_id, monotonic_ms(), may_control ? 1U : 0U,
                    static_cast<std::uint32_t>(Shared::kCommandCapacity)};
                if (!send_tls_bytes(socket, orbit::encode_command_session(session, message_sequence++),
                                    Clock::now() + Milliseconds(connect_config.io_timeout_ms))) {
                    throw std::runtime_error("failed to establish command session");
                }
                shared.state.store(ConnectionState::active);
            } catch (const std::exception&) {
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
            if (!send_command_results(
                    socket, shared, message_sequence,
                    Clock::now() + Milliseconds(config.io_timeout_ms))) {
                shared.counters.send_failures.fetch_add(1);
                socket.reset();
                shared.counters.reconnects.fetch_add(1);
                break;
            }
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
            if (!send_tls_bytes(socket, frame, send_deadline)) {
                shared.counters.send_failures.fetch_add(1);
                socket.reset();
                shared.counters.reconnects.fetch_add(1);
                shared.state.store(ConnectionState::backoff);
                wait_backoff(retry.next_delay());
                break;
            }
            const auto ack = require_ack(
                socket, sample, message_sequence,
                static_cast<std::uint64_t>(operation_timeout.count()), shared,
                message_sequence);
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
    try {
        auto config = parse_args(argc, argv);
        if (!config.boot_id_explicit) config.boot_id = random_nonzero_u64();
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
