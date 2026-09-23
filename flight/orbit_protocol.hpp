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
constexpr std::uint8_t kTelemetryAck = 2;
constexpr std::uint8_t kCommandRequest = 3;
constexpr std::uint8_t kCommandResult = 4;
constexpr std::uint8_t kCommandSession = 5;
constexpr std::uint16_t kHeaderLength = 28;
constexpr std::uint32_t kMaxPayloadLength = 4096;
constexpr std::uint32_t kTelemetrySamplePayloadLength = 44;
constexpr std::uint32_t kTelemetryAckPayloadLength = 20;
constexpr std::uint32_t kCommandRequestPayloadLength = 48;
constexpr std::uint32_t kCommandResultPayloadLength = 56;
constexpr std::uint32_t kCommandSessionPayloadLength = 32;

constexpr std::uint32_t kCommandEnable = 1;
constexpr std::uint32_t kCommandDisable = 2;
constexpr std::uint32_t kCommandSetPeriod = 3;
constexpr std::uint32_t kCommandReset = 4;
constexpr std::uint32_t kCommandClearFault = 5;

constexpr std::uint32_t kResultAccepted = 0;
constexpr std::uint32_t kResultRejected = 1;
constexpr std::uint32_t kResultOutcomeUnknown = 2;

constexpr std::uint32_t kRejectUnauthorized = 1;
constexpr std::uint32_t kRejectBadSession = 2;
constexpr std::uint32_t kRejectBadSequence = 3;
constexpr std::uint32_t kRejectExpired = 4;
constexpr std::uint32_t kRejectBadArguments = 5;
constexpr std::uint32_t kRejectQueueFull = 6;

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

struct TelemetryAck {
    std::uint32_t spacecraft_id{};
    std::uint64_t flight_boot_id{};
    std::uint64_t telemetry_sequence{};
};

struct CommandRequest {
    std::uint64_t command_session_id{};
    std::uint64_t command_sequence{};
    std::uint64_t command_id{};
    std::uint32_t command_type{};
    std::uint64_t deadline_monotonic_ms{};
    std::uint64_t argument{};
    std::uint32_t role{1};
};

struct CommandResult {
    std::uint64_t command_session_id{};
    std::uint64_t command_sequence{};
    std::uint64_t command_id{};
    std::uint32_t command_type{};
    std::uint32_t status{};
    std::uint32_t reason{};
    std::uint64_t effect_counter{};
    std::uint64_t device_generation{};
    std::uint32_t current_period_ms{};
};

struct CommandSession {
    std::uint64_t command_session_id{};
    std::uint64_t flight_boot_id{};
    std::uint64_t flight_monotonic_ms{};
    std::uint32_t authorized_role{};
    std::uint32_t command_queue_capacity{};
};

struct DecodedFrame {
    Header header;
    TelemetrySample sample;
    TelemetryAck ack;
    CommandRequest command_request;
    CommandResult command_result;
    CommandSession command_session;
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

inline void append_header(std::vector<std::uint8_t>& out, std::uint8_t message_type,
                          std::uint32_t payload_length, std::uint64_t message_sequence) {
    out.insert(out.end(), kMagic.begin(), kMagic.end());
    out.push_back(kVersion);
    out.push_back(message_type);
    append_u16(out, 0);
    append_u16(out, kHeaderLength);
    append_u16(out, 0);
    append_u32(out, payload_length);
    append_u64(out, message_sequence);
    append_u32(out, 0);
}

inline std::vector<std::uint8_t> encode_telemetry_ack(const TelemetryAck& ack,
                                                      std::uint64_t message_sequence = 1) {
    std::vector<std::uint8_t> out;
    out.reserve(kHeaderLength + kTelemetryAckPayloadLength);

    out.insert(out.end(), kMagic.begin(), kMagic.end());
    out.push_back(kVersion);
    out.push_back(kTelemetryAck);
    append_u16(out, 0);
    append_u16(out, kHeaderLength);
    append_u16(out, 0);
    append_u32(out, kTelemetryAckPayloadLength);
    append_u64(out, message_sequence);
    append_u32(out, 0);

    append_u32(out, ack.spacecraft_id);
    append_u64(out, ack.flight_boot_id);
    append_u64(out, ack.telemetry_sequence);
    return out;
}

inline std::vector<std::uint8_t> encode_command_request(const CommandRequest& command,
                                                        std::uint64_t message_sequence = 1) {
    std::vector<std::uint8_t> out;
    out.reserve(kHeaderLength + kCommandRequestPayloadLength);
    append_header(out, kCommandRequest, kCommandRequestPayloadLength, message_sequence);
    append_u64(out, command.command_session_id);
    append_u64(out, command.command_sequence);
    append_u64(out, command.command_id);
    append_u32(out, command.command_type);
    append_u64(out, command.deadline_monotonic_ms);
    append_u64(out, command.argument);
    append_u32(out, command.role);
    return out;
}

inline std::vector<std::uint8_t> encode_command_result(const CommandResult& result,
                                                       std::uint64_t message_sequence = 1) {
    std::vector<std::uint8_t> out;
    out.reserve(kHeaderLength + kCommandResultPayloadLength);
    append_header(out, kCommandResult, kCommandResultPayloadLength, message_sequence);
    append_u64(out, result.command_session_id);
    append_u64(out, result.command_sequence);
    append_u64(out, result.command_id);
    append_u32(out, result.command_type);
    append_u32(out, result.status);
    append_u32(out, result.reason);
    append_u64(out, result.effect_counter);
    append_u64(out, result.device_generation);
    append_u32(out, result.current_period_ms);
    return out;
}

inline std::vector<std::uint8_t> encode_command_session(const CommandSession& session,
                                                        std::uint64_t message_sequence = 1) {
    std::vector<std::uint8_t> out;
    out.reserve(kHeaderLength + kCommandSessionPayloadLength);
    append_header(out, kCommandSession, kCommandSessionPayloadLength, message_sequence);
    append_u64(out, session.command_session_id);
    append_u64(out, session.flight_boot_id);
    append_u64(out, session.flight_monotonic_ms);
    append_u32(out, session.authorized_role);
    append_u32(out, session.command_queue_capacity);
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

    if (message_type != kTelemetrySample && message_type != kTelemetryAck &&
        message_type != kCommandRequest && message_type != kCommandResult &&
        message_type != kCommandSession) {
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
    std::uint32_t expected_payload_length = 0;
    if (message_type == kTelemetrySample) expected_payload_length = kTelemetrySamplePayloadLength;
    else if (message_type == kTelemetryAck) expected_payload_length = kTelemetryAckPayloadLength;
    else if (message_type == kCommandRequest) expected_payload_length = kCommandRequestPayloadLength;
    else if (message_type == kCommandResult) expected_payload_length = kCommandResultPayloadLength;
    else expected_payload_length = kCommandSessionPayloadLength;
    if (payload_length != expected_payload_length) {
        if (message_type == kTelemetrySample) {
            throw ProtocolError("bad telemetry payload length");
        }
        if (message_type == kTelemetryAck) throw ProtocolError("bad telemetry ACK payload length");
        if (message_type == kCommandRequest) throw ProtocolError("bad command request payload length");
        if (message_type == kCommandResult) throw ProtocolError("bad command result payload length");
        throw ProtocolError("bad command session payload length");
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
    if (message_type == kTelemetrySample) {
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
    } else if (message_type == kTelemetryAck) {
        decoded.ack = TelemetryAck{
            read_u32(frame, 28),
            read_u64(frame, 32),
            read_u64(frame, 40),
        };
    } else if (message_type == kCommandRequest) {
        decoded.command_request = CommandRequest{
            read_u64(frame, 28),
            read_u64(frame, 36),
            read_u64(frame, 44),
            read_u32(frame, 52),
            read_u64(frame, 56),
            read_u64(frame, 64),
            read_u32(frame, 72),
        };
    } else if (message_type == kCommandResult) {
        decoded.command_result = CommandResult{
            read_u64(frame, 28),
            read_u64(frame, 36),
            read_u64(frame, 44),
            read_u32(frame, 52),
            read_u32(frame, 56),
            read_u32(frame, 60),
            read_u64(frame, 64),
            read_u64(frame, 72),
            read_u32(frame, 80),
        };
    } else {
        decoded.command_session = CommandSession{
            read_u64(frame, 28),
            read_u64(frame, 36),
            read_u64(frame, 44),
            read_u32(frame, 52),
            read_u32(frame, 56),
        };
    }
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
            const auto message_type = buffer_.at(5);
            if (message_type != kTelemetrySample && message_type != kTelemetryAck &&
                message_type != kCommandRequest && message_type != kCommandResult &&
                message_type != kCommandSession) {
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
            std::uint32_t expected_payload_length = 0;
            if (message_type == kTelemetrySample) expected_payload_length = kTelemetrySamplePayloadLength;
            else if (message_type == kTelemetryAck) expected_payload_length = kTelemetryAckPayloadLength;
            else if (message_type == kCommandRequest) expected_payload_length = kCommandRequestPayloadLength;
            else if (message_type == kCommandResult) expected_payload_length = kCommandResultPayloadLength;
            else expected_payload_length = kCommandSessionPayloadLength;
            if (payload_length != expected_payload_length) {
                if (message_type == kTelemetrySample) {
                    throw ProtocolError("bad telemetry payload length");
                }
                if (message_type == kTelemetryAck) throw ProtocolError("bad telemetry ACK payload length");
                if (message_type == kCommandRequest) throw ProtocolError("bad command request payload length");
                if (message_type == kCommandResult) throw ProtocolError("bad command result payload length");
                throw ProtocolError("bad command session payload length");
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

    void finish() const {
        if (!buffer_.empty()) {
            throw ProtocolError("truncated frame at end of stream");
        }
    }

  private:
    std::vector<std::uint8_t> buffer_;
};

}  // namespace orbit
