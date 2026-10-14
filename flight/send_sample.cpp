#include "orbit_protocol.hpp"

#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>

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
    int get() const { return fd_; }

  private:
    int fd_;
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

void send_all(int fd, const std::vector<std::uint8_t>& bytes) {
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const auto sent = ::send(fd, bytes.data() + offset, bytes.size() - offset, 0);
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) throw system_error("send");
        offset += static_cast<std::size_t>(sent);
    }
}

std::vector<std::uint8_t> receive_exact(int fd, std::size_t size) {
    std::vector<std::uint8_t> bytes(size);
    std::size_t offset = 0;
    while (offset < size) {
        const auto received = ::recv(fd, bytes.data() + offset, size - offset, 0);
        if (received < 0 && errno == EINTR) continue;
        if (received == 0) throw std::runtime_error("connection closed before ACK");
        if (received < 0) throw system_error("recv");
        offset += static_cast<std::size_t>(received);
    }
    return bytes;
}

void send_and_require_ack(int fd, const orbit::TelemetrySample& sample, std::uint64_t message_sequence) {
    send_all(fd, orbit::encode_telemetry_sample(sample, message_sequence));
    auto frame = receive_exact(fd, orbit::kHeaderLength);
    const auto payload_length = orbit::read_u32(frame, 12);
    if (payload_length > orbit::kMaxPayloadLength) throw std::runtime_error("ACK payload too large");
    const auto payload = receive_exact(fd, payload_length);
    frame.insert(frame.end(), payload.begin(), payload.end());
    const auto decoded = orbit::decode_frame(frame);
    if (decoded.header.message_type != orbit::kTelemetryAck ||
        decoded.ack.spacecraft_id != sample.spacecraft_id ||
        decoded.ack.flight_boot_id != sample.flight_boot_id ||
        decoded.ack.telemetry_sequence != sample.telemetry_sequence) {
        throw std::runtime_error("ACK identity does not match sent sample");
    }
}

std::uint64_t parse_count(const char* text, const char* name) {
    std::size_t parsed = 0;
    const auto value = std::stoull(text, &parsed);
    if (text[parsed] != '\0') throw std::invalid_argument(std::string("invalid ") + name);
    return value;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 4 || argc > 5) {
        std::cerr << "usage: send_sample HOST PORT COUNT [DUPLICATE_COUNT]\n";
        return 2;
    }
    try {
        const auto count = parse_count(argv[3], "count");
        const auto duplicates = argc == 5 ? parse_count(argv[4], "duplicate count") : 0;
        if (duplicates > count) throw std::invalid_argument("duplicate count exceeds count");
        auto connection = connect_to(argv[1], argv[2]);
        DeterministicSampleSource source;
        std::uint64_t message_sequence = 1;
        for (std::uint64_t index = 0; index < count; ++index) {
            send_and_require_ack(connection.get(), source.sample(index), message_sequence++);
        }
        for (std::uint64_t index = 0; index < duplicates; ++index) {
            send_and_require_ack(connection.get(), source.sample(index), message_sequence++);
        }
        std::cout << "acknowledged " << (count + duplicates) << " samples\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "send_sample: " << error.what() << '\n';
        return 1;
    }
}
