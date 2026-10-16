#pragma once

#include "command_policy.hpp"
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
constexpr std::uint8_t kCommandSession = 3, kCommandRequest = 4, kCommandResult = 5;
constexpr std::uint8_t kStateSnapshot=6, kLossReport=7, kLossAck=8;
constexpr std::uint8_t kSensorSample=9, kMultisensorSample=9;
inline std::uint32_t payload_size(std::uint8_t type) {
    switch(type) { case 1:return 44;case 2:return 20;case 3:return 24;case 4:return 40;case 5:return 32;case 6:return 64;case 7:return 48;case 8:return 20;case 9:return 76;default:throw std::runtime_error("unsupported message type"); }
}
constexpr std::uint16_t kHeaderLength = 28;
constexpr std::uint32_t kMaxPayloadLength = 4096;
constexpr std::uint32_t kTelemetrySamplePayloadLength = 44;
constexpr std::uint32_t kTelemetryAckPayloadLength = 20;

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
    std::uint16_t category{}, schema{1};
    std::uint32_t quality{};
    std::array<std::int32_t,8> values{};
};

struct TelemetryAck {
    std::uint32_t spacecraft_id{};
    std::uint64_t flight_boot_id{};
    std::uint64_t telemetry_sequence{};
};

struct LossReport { std::uint32_t spacecraft_id{}; std::uint64_t flight_boot_id{}, report_id{}, first{}, last{}, count{}; std::uint32_t reason{1}; };
struct LossAck { std::uint32_t spacecraft_id{}; std::uint64_t flight_boot_id{}, report_id{}; };

struct DecodedFrame {
    Header header;
    TelemetrySample sample;
    TelemetryAck ack;
    CommandRequest command;
    LossReport loss;
    LossAck loss_ack;
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

inline void validate_sensor(const TelemetrySample& sample) {
    if(sample.category>6 || sample.schema!=1 || sample.quality>1)throw ProtocolError("invalid sensor category/schema/quality");
    constexpr unsigned used[]={0,6,3,5,6,3,4};
    for(unsigned i=used[sample.category];i<8;++i)if(sample.values[i])throw ProtocolError("unused sensor slots must be zero");
    int boolean=-1;
    switch(sample.category){case 1:boolean=5;break;case 2:boolean=2;break;case 3:boolean=4;break;case 5:boolean=2;break;}
    if(boolean>=0 && (sample.values[boolean]<0 || sample.values[boolean]>1))throw ProtocolError("invalid sensor boolean");
    if(sample.category==6 && (sample.values[0]<1 || sample.values[0]>8 || sample.values[1]<0 || sample.values[1]>2 || sample.values[2]<1 || sample.values[2]>6 || sample.values[3]<1))throw ProtocolError("invalid sensor event");
    if(!sample.category && sample.quality)throw ProtocolError("legacy quality cannot be encoded");
}

inline std::vector<std::uint8_t> encode_telemetry_sample(const TelemetrySample& sample,
                                                         std::uint64_t message_sequence = 1) {
    validate_sensor(sample);
    const auto type=sample.category?kSensorSample:kTelemetrySample;
    std::vector<std::uint8_t> out;
    out.reserve(kHeaderLength + payload_size(type));

    out.insert(out.end(), kMagic.begin(), kMagic.end());
    out.push_back(kVersion);
    out.push_back(type);
    append_u16(out, 0);
    append_u16(out, kHeaderLength);
    append_u16(out, 0);
    append_u32(out, payload_size(type));
    append_u64(out, message_sequence);
    append_u32(out, 0);

    append_u32(out, sample.spacecraft_id);
    append_u64(out, sample.flight_boot_id);
    append_u64(out, sample.telemetry_sequence);
    append_u32(out, sample.device_generation);
    append_u32(out, sample.device_sequence);
    append_u64(out, sample.timestamp_monotonic_ns);
    if(sample.category) {
        append_u16(out,sample.category);append_u16(out,sample.schema);append_u32(out,sample.quality);
        for(auto value:sample.values)append_i32(out,value);
    } else {
        append_i32(out, sample.temperature_millideg_c);
        append_u32(out, sample.voltage_millivolts);
    }

    return out;
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

inline std::vector<std::uint8_t> encode_control(std::uint8_t type, const std::vector<std::uint8_t>& payload, std::uint64_t sequence=1) {
    if (type < 3 || type > 8 || payload.size()!=payload_size(type)) throw ProtocolError("bad command frame");
    std::vector<std::uint8_t> out(kMagic.begin(),kMagic.end());
    out.push_back(kVersion);out.push_back(type);append_u16(out,0);append_u16(out,kHeaderLength);append_u16(out,0);
    append_u32(out,payload.size());append_u64(out,sequence);append_u32(out,0);out.insert(out.end(),payload.begin(),payload.end());return out;
}
inline std::vector<std::uint8_t> encode_command_result(const CommandResult& result) {
    std::vector<std::uint8_t> out;append_u64(out,result.request.session);append_u64(out,result.request.sequence);
    append_u64(out,result.request.id);append_u32(out,static_cast<std::uint32_t>(result.status));
    if(result.snapshot) {
        const auto& state=*result.snapshot;
        append_u32(out,0);append_u32(out,state.enabled);append_u32(out,state.period_ms);append_u32(out,state.generation);
        append_u32(out,state.fault);append_u64(out,state.effects);append_u64(out,result.monotonic_ns);
        return encode_control(kStateSnapshot,out,result.request.sequence);
    }
    append_u32(out,0);
    return encode_control(kCommandResult,out,result.request.sequence);
}

inline std::vector<std::uint8_t> encode_loss_report(const LossReport& loss) {
    if (!loss.report_id || !loss.count || (loss.reason==1 && (!loss.first || loss.last<loss.first || loss.count>loss.last-loss.first+1)) || (loss.reason==2 && (loss.flight_boot_id || loss.first || loss.last)) || (loss.reason!=1 && loss.reason!=2)) throw ProtocolError("invalid loss report");
    std::vector<std::uint8_t> p;append_u32(p,loss.spacecraft_id);append_u64(p,loss.flight_boot_id);append_u64(p,loss.report_id);append_u64(p,loss.first);append_u64(p,loss.last);append_u64(p,loss.count);append_u32(p,loss.reason);return encode_control(kLossReport,p,loss.report_id);
}
inline std::vector<std::uint8_t> encode_loss_ack(const LossAck& ack) {
    std::vector<std::uint8_t> p;append_u32(p,ack.spacecraft_id);append_u64(p,ack.flight_boot_id);append_u64(p,ack.report_id);return encode_control(kLossAck,p,ack.report_id);
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

    if (message_type < kTelemetrySample || message_type > kSensorSample) {
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
    const auto expected_payload_length = payload_size(message_type);
    if (payload_length != expected_payload_length) {
        if (message_type == kTelemetrySample) {
            throw ProtocolError("bad telemetry payload length");
        }
        throw ProtocolError("bad telemetry ACK payload length");
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
    } else if(message_type==kSensorSample) {
        auto& sample=decoded.sample;
        sample.spacecraft_id=read_u32(frame,28);sample.flight_boot_id=read_u64(frame,32);sample.telemetry_sequence=read_u64(frame,40);
        sample.device_generation=read_u32(frame,48);sample.device_sequence=read_u32(frame,52);sample.timestamp_monotonic_ns=read_u64(frame,56);
        sample.category=read_u16(frame,64);sample.schema=read_u16(frame,66);sample.quality=read_u32(frame,68);
        for(unsigned i=0;i<8;++i)sample.values[i]=read_i32(frame,72+i*4);
        if(!sample.category)throw ProtocolError("sensor frame requires category");
        validate_sensor(sample);
        if(sample.category==2)sample.temperature_millideg_c=sample.values[0];
        if(sample.category==1 && sample.values[0]>=0)sample.voltage_millivolts=static_cast<std::uint32_t>(sample.values[0]);
    } else if (message_type == kTelemetryAck) {
        decoded.ack = TelemetryAck{
            read_u32(frame, 28),
            read_u64(frame, 32),
            read_u64(frame, 40),
        };
    }
    if (message_type == kCommandRequest) decoded.command = CommandRequest{read_u64(frame,28),read_u64(frame,36),read_u64(frame,44),read_u64(frame,52),read_u32(frame,60),read_u32(frame,64)};
    if(message_type==kStateSnapshot && (read_u32(frame,52)!=0 || read_u32(frame,56)!=0 || read_u32(frame,60)>1 || read_u32(frame,64)>60000 || read_u32(frame,72)>1)) throw ProtocolError("invalid state snapshot");
    if(message_type==kLossReport) {
        decoded.loss={read_u32(frame,28),read_u64(frame,32),read_u64(frame,40),read_u64(frame,48),read_u64(frame,56),read_u64(frame,64),read_u32(frame,72)};
        encode_loss_report(decoded.loss);
    }
    if(message_type==kLossAck) decoded.loss_ack={read_u32(frame,28),read_u64(frame,32),read_u64(frame,40)};
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
            if (message_type < kTelemetrySample || message_type > kSensorSample) {
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
            const auto expected_payload_length = payload_size(message_type);
            if (payload_length != expected_payload_length) {
                if (message_type == kTelemetrySample) {
                    throw ProtocolError("bad telemetry payload length");
                }
                throw ProtocolError("bad telemetry ACK payload length");
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
