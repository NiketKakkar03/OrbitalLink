#include "orbit_protocol.hpp"

#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>
#include <openssl/ssl.h>

#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

class SampleSource {
  public:
    virtual ~SampleSource() = default;
    virtual orbit::TelemetrySample sample(std::uint64_t index) const = 0;
};

class DeterministicSampleSource final : public SampleSource {
  public:
    orbit::TelemetrySample sample(std::uint64_t index) const override {
        return orbit::TelemetrySample{
            1,
            0x0102030405060708ULL,
            index + 1,
            7,
            static_cast<std::uint32_t>(index),
            1'000'000'000ULL + index * 100'000'000ULL,
            static_cast<std::int32_t>(-20'000 + (index % 80'001)),
            static_cast<std::uint32_t>(3'000 + (index % 401)),
        };
    }
};

class Socket {
  public:
    explicit Socket(int fd) : fd_(fd) {}
    ~Socket() { if (fd_ >= 0) ::close(fd_); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    Socket(Socket&& other) noexcept : fd_(other.fd_) { other.fd_ = -1; }
    Socket& operator=(Socket&& other) noexcept {
        if (this != &other) {
            if (fd_ >= 0) ::close(fd_);
            fd_ = other.fd_;
            other.fd_ = -1;
        }
        return *this;
    }
    int get() const { return fd_; }

  private:
    int fd_;
};

class TlsConnection {
  public:
    TlsConnection(Socket socket, SSL* ssl) : socket_(std::move(socket)), ssl_(ssl) {}
    ~TlsConnection() {
        if (ssl_ != nullptr) {
            (void)SSL_shutdown(ssl_);
            SSL_free(ssl_);
        }
    }
    TlsConnection(const TlsConnection&) = delete;
    TlsConnection& operator=(const TlsConnection&) = delete;
    SSL* ssl() const { return ssl_; }

  private:
    Socket socket_;
    SSL* ssl_;
};

std::runtime_error system_error(const std::string& operation) {
    return std::runtime_error(operation + ": " + std::strerror(errno));
}

Socket connect_to(const std::string& host, const std::string& port) {
    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* raw_results = nullptr;
    const int status = ::getaddrinfo(host.c_str(), port.c_str(), &hints, &raw_results);
    if (status != 0) throw std::runtime_error(std::string("getaddrinfo: ") + gai_strerror(status));
    std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> results(raw_results, ::freeaddrinfo);

    for (auto* address = results.get(); address != nullptr; address = address->ai_next) {
        const int fd = ::socket(address->ai_family, address->ai_socktype, address->ai_protocol);
        if (fd < 0) continue;
        if (::connect(fd, address->ai_addr, address->ai_addrlen) == 0) return Socket(fd);
        ::close(fd);
    }
    throw system_error("connect");
}

void send_all(SSL* ssl, const std::vector<std::uint8_t>& bytes) {
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const int sent = SSL_write(ssl, bytes.data() + offset, static_cast<int>(bytes.size() - offset));
        if (sent <= 0) throw std::runtime_error("TLS write failed");
        offset += static_cast<std::size_t>(sent);
    }
}

std::vector<std::uint8_t> receive_exact(SSL* ssl, std::size_t size) {
    std::vector<std::uint8_t> bytes(size);
    std::size_t offset = 0;
    while (offset < size) {
        const int received = SSL_read(ssl, bytes.data() + offset, static_cast<int>(size - offset));
        if (received == 0) throw std::runtime_error("connection closed before ACK");
        if (received < 0) throw std::runtime_error("TLS read failed");
        offset += static_cast<std::size_t>(received);
    }
    return bytes;
}

void send_and_require_ack(SSL* ssl, const orbit::TelemetrySample& sample, std::uint64_t message_sequence) {
    send_all(ssl, orbit::encode_telemetry_sample(sample, message_sequence));
    auto frame = receive_exact(ssl, orbit::kHeaderLength);
    const auto payload_length = orbit::read_u32(frame, 12);
    if (payload_length > orbit::kMaxPayloadLength) throw std::runtime_error("ACK payload too large");
    const auto payload = receive_exact(ssl, payload_length);
    frame.insert(frame.end(), payload.begin(), payload.end());
    const auto decoded = orbit::decode_frame(frame);
    if (decoded.header.message_type != orbit::kTelemetryAck ||
        decoded.ack.spacecraft_id != sample.spacecraft_id ||
        decoded.ack.flight_boot_id != sample.flight_boot_id ||
        decoded.ack.telemetry_sequence != sample.telemetry_sequence) {
        throw std::runtime_error("ACK identity does not match sent sample");
    }
}

TlsConnection connect_tls(const std::string& host, const std::string& port, const std::string& ca_file,
                          const std::string& cert_file, const std::string& key_file,
                          const std::string& server_name) {
    SSL_library_init();
    auto socket = connect_to(host, port);
    SSL_CTX* ctx = SSL_CTX_new(TLS_client_method());
    if (ctx == nullptr) throw std::runtime_error("SSL_CTX_new failed");
    SSL_CTX_set_min_proto_version(ctx, TLS1_3_VERSION);
    SSL_CTX_set_max_proto_version(ctx, TLS1_3_VERSION);
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, nullptr);
    if (SSL_CTX_load_verify_locations(ctx, ca_file.c_str(), nullptr) != 1)
        throw std::runtime_error("failed to load CA");
    if (SSL_CTX_use_certificate_file(ctx, cert_file.c_str(), SSL_FILETYPE_PEM) != 1)
        throw std::runtime_error("failed to load client certificate");
    if (SSL_CTX_use_PrivateKey_file(ctx, key_file.c_str(), SSL_FILETYPE_PEM) != 1)
        throw std::runtime_error("failed to load client key");
    SSL* ssl = SSL_new(ctx);
    SSL_CTX_free(ctx);
    if (ssl == nullptr) throw std::runtime_error("SSL_new failed");
    SSL_set_fd(ssl, socket.get());
    SSL_set_tlsext_host_name(ssl, server_name.c_str());
    if (SSL_set1_host(ssl, server_name.c_str()) != 1)
        throw std::runtime_error("failed to configure server identity");
    if (SSL_connect(ssl) != 1) throw std::runtime_error("TLS handshake failed");
    return TlsConnection(std::move(socket), ssl);
}

std::uint64_t parse_count(const char* text, const char* name) {
    std::size_t parsed = 0;
    const auto value = std::stoull(text, &parsed);
    if (text[parsed] != '\0') throw std::invalid_argument(std::string("invalid ") + name);
    return value;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 8 || argc > 9) {
        std::cerr << "usage: send_sample HOST PORT COUNT CA_FILE CLIENT_CERT CLIENT_KEY SERVER_NAME [DUPLICATE_COUNT]\n";
        return 2;
    }
    try {
        const auto count = parse_count(argv[3], "count");
        const auto duplicates = argc == 9 ? parse_count(argv[8], "duplicate count") : 0;
        if (duplicates > count) throw std::invalid_argument("duplicate count exceeds count");
        auto connection = connect_tls(argv[1], argv[2], argv[4], argv[5], argv[6], argv[7]);
        DeterministicSampleSource source;
        std::uint64_t message_sequence = 1;
        for (std::uint64_t index = 0; index < count; ++index) {
            send_and_require_ack(connection.ssl(), source.sample(index), message_sequence++);
        }
        for (std::uint64_t index = 0; index < duplicates; ++index) {
            send_and_require_ack(connection.ssl(), source.sample(index), message_sequence++);
        }
        std::cout << "acknowledged " << (count + duplicates) << " samples\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "send_sample: " << error.what() << '\n';
        return 1;
    }
}
