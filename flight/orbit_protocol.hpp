#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace orbit {

constexpr std::array<std::uint8_t, 4> kMagic = {'O', 'L', 'N', 'K'};
constexpr std::uint8_t kVersion = 1;
constexpr std::uint8_t kTelemetrySample = 1;
constexpr std::uint16_t kHeaderLength = 28;
constexpr std::uint32_t kMaxPayloadLength = 4096;
constexpr std::uint32_t kTelemetrySamplePayloadLength = 44;

struct Header {
    std::uint8_t message_type{};
    std::uint32_t payload_length{};
    std::uint64_t message_sequence{};
};

struct TelemetrySample {
    std::uint32_t spacecraft_id{};
    std::uint64_t flight_boot_id{};
    std::uint64_t telemetry_sequence{};
    std::uint32_t device_generation{};
    std::uint32_t device_sequence{};
    std::uint64_t timestamp_monotonic_ns{};
    std::int32_t temperature_millideg_c{};
    std::uint32_t voltage_millivolts{};
};

struct DecodedFrame {
    Header header;
    TelemetrySample sample;
};

class ProtocolError : public std::runtime_error {
  public:
    explicit ProtocolError(const std::string& message) : std::runtime_error(message) {}
};

inline void append_u16(std::vector<std::uint8_t>& out, std::uint16_t value) {
    out.push_back(static_cast<std::uint8_t>((value >> 8) & 0xff));
    out.push_back(static_cast<std::uint8_t>(value & 0xff));
}

inline void append_u32(std::vector<std::uint8_t>& out, std::uint32_t value) {
    out.push_back(static_cast<std::uint8_t>((value >> 24) & 0xff));
    out.push_back(static_cast<std::uint8_t>((value >> 16) & 0xff));
    out.push_back(static_cast<std::uint8_t>((value >> 8) & 0xff));
    out.push_back(static_cast<std::uint8_t>(value & 0xff));
}

inline void append_i32(std::vector<std::uint8_t>& out, std::int32_t value) {
    append_u32(out, static_cast<std::uint32_t>(value));
}

inline void append_u64(std::vector<std::uint8_t>& out, std::uint64_t value) {
    for (int shift = 56; shift >= 0; shift -= 8) {
        out.push_back(static_cast<std::uint8_t>((value >> shift) & 0xff));
    }
}

inline std::uint16_t read_u16(const std::vector<std::uint8_t>& data, std::size_t offset) {
    return static_cast<std::uint16_t>((static_cast<std::uint16_t>(data.at(offset)) << 8) |
                                      static_cast<std::uint16_t>(data.at(offset + 1)));
}

inline std::uint32_t read_u32(const std::vector<std::uint8_t>& data, std::size_t offset) {
    return (static_cast<std::uint32_t>(data.at(offset)) << 24) |
           (static_cast<std::uint32_t>(data.at(offset + 1)) << 16) |
           (static_cast<std::uint32_t>(data.at(offset + 2)) << 8) |
           static_cast<std::uint32_t>(data.at(offset + 3));
}

inline std::int32_t read_i32(const std::vector<std::uint8_t>& data, std::size_t offset) {
    return static_cast<std::int32_t>(read_u32(data, offset));
}

inline std::uint64_t read_u64(const std::vector<std::uint8_t>& data, std::size_t offset) {
    std::uint64_t value = 0;
    for (std::size_t i = 0; i < 8; ++i) {
        value = (value << 8) | static_cast<std::uint64_t>(data.at(offset + i));
    }
    return value;
}

inline std::vector<std::uint8_t> encode_telemetry_sample(const TelemetrySample& sample,
                                                         std::uint64_t message_sequence = 1) {
    std::vector<std::uint8_t> out;
    out.reserve(kHeaderLength + kTelemetrySamplePayloadLength);

    out.insert(out.end(), kMagic.begin(), kMagic.end());
    out.push_back(kVersion);
    out.push_back(kTelemetrySample);
    append_u16(out, 0);
    append_u16(out, kHeaderLength);
    append_u16(out, 0);
    append_u32(out, kTelemetrySamplePayloadLength);
    append_u64(out, message_sequence);
    append_u32(out, 0);

    append_u32(out, sample.spacecraft_id);
    append_u64(out, sample.flight_boot_id);
    append_u64(out, sample.telemetry_sequence);
    append_u32(out, sample.device_generation);
    append_u32(out, sample.device_sequence);
    append_u64(out, sample.timestamp_monotonic_ns);
    append_i32(out, sample.temperature_millideg_c);
    append_u32(out, sample.voltage_millivolts);

    return out;
}

inline DecodedFrame decode_frame(const std::vector<std::uint8_t>& frame) {
    if (frame.size() < kHeaderLength) {
        throw ProtocolError("truncated header");
    }

    if (std::array<std::uint8_t, 4>{frame.at(0), frame.at(1), frame.at(2), frame.at(3)} != kMagic) {
        throw ProtocolError("bad magic");
    }
    if (frame.at(4) != kVersion) {
        throw ProtocolError("unsupported version");
    }

    const auto message_type = frame.at(5);
    const auto flags = read_u16(frame, 6);
    const auto header_length = read_u16(frame, 8);
    const auto reserved_a = read_u16(frame, 10);
    const auto payload_length = read_u32(frame, 12);
    const auto message_sequence = read_u64(frame, 16);
    const auto reserved_b = read_u32(frame, 24);

    if (message_type != kTelemetrySample) {
        throw ProtocolError("unsupported message type");
    }
    if (flags != 0) {
        throw ProtocolError("unsupported flags");
    }
    if (header_length != kHeaderLength) {
        throw ProtocolError("bad header length");
    }
    if (reserved_a != 0 || reserved_b != 0) {
        throw ProtocolError("reserved field is not zero");
    }
    if (payload_length > kMaxPayloadLength) {
        throw ProtocolError("payload too large");
    }
    if (payload_length != kTelemetrySamplePayloadLength) {
        throw ProtocolError("bad telemetry payload length");
    }

    const auto expected_frame_length = kHeaderLength + payload_length;
    if (frame.size() < expected_frame_length) {
        throw ProtocolError("truncated payload");
    }
    if (frame.size() > expected_frame_length) {
        throw ProtocolError("trailing bytes");
    }

    DecodedFrame decoded;
    decoded.header = Header{message_type, payload_length, message_sequence};
    decoded.sample = TelemetrySample{
        read_u32(frame, 28),
        read_u64(frame, 32),
        read_u64(frame, 40),
        read_u32(frame, 48),
        read_u32(frame, 52),
        read_u64(frame, 56),
        read_i32(frame, 64),
        read_u32(frame, 68),
    };
    return decoded;
}

class FrameParser {
  public:
    std::vector<DecodedFrame> feed(const std::vector<std::uint8_t>& chunk) {
        buffer_.insert(buffer_.end(), chunk.begin(), chunk.end());
        std::vector<DecodedFrame> frames;

        while (true) {
            if (buffer_.size() < kHeaderLength) {
                return frames;
            }

            if (std::array<std::uint8_t, 4>{buffer_.at(0), buffer_.at(1), buffer_.at(2), buffer_.at(3)} !=
                kMagic) {
                throw ProtocolError("bad magic");
            }

            if (buffer_.at(4) != kVersion) {
                throw ProtocolError("unsupported version");
            }
            if (buffer_.at(5) != kTelemetrySample) {
                throw ProtocolError("unsupported message type");
            }
            if (read_u16(buffer_, 6) != 0) {
                throw ProtocolError("unsupported flags");
            }
            if (read_u16(buffer_, 8) != kHeaderLength) {
                throw ProtocolError("bad header length");
            }
            if (read_u16(buffer_, 10) != 0 || read_u32(buffer_, 24) != 0) {
                throw ProtocolError("reserved field is not zero");
            }

            const auto payload_length = read_u32(buffer_, 12);
            if (payload_length > kMaxPayloadLength) {
                throw ProtocolError("payload too large");
            }
            if (payload_length != kTelemetrySamplePayloadLength) {
                throw ProtocolError("bad telemetry payload length");
            }

            const auto frame_length = kHeaderLength + payload_length;
            if (buffer_.size() < frame_length) {
                return frames;
            }

            std::vector<std::uint8_t> frame(buffer_.begin(), buffer_.begin() + frame_length);
            buffer_.erase(buffer_.begin(), buffer_.begin() + frame_length);
            frames.push_back(decode_frame(frame));
        }
    }

  private:
    std::vector<std::uint8_t> buffer_;
};

}  // namespace orbit
