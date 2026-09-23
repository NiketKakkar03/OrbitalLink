#include "../orbit_protocol.hpp"

#include <cassert>
#include <cctype>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr const char* kGoldenFrameHex =
    "4f4c4e4b01010000001c00000000002c00000000000000010000000000000001"
    "0102030405060708000000000000002a000000070000006300000000499602d2"
    "ffffeb7e00000ce4";
constexpr const char* kGoldenAckHex =
    "4f4c4e4b01020000001c000000000014000000000000000200000000"
    "000000010102030405060708000000000000002a";
constexpr const char* kGoldenCommandRequestHex =
    "4f4c4e4b01030000001c000000000030000000000000000a000000000000000000abcdef0000000000000001"
    "000000000000006300000003000000000000271000000000000000fa00000001";
constexpr const char* kGoldenCommandResultHex =
    "4f4c4e4b01040000001c000000000038000000000000000b000000000000000000abcdef0000000000000001"
    "000000000000006300000003000000000000000000000000000000070000000000000008000000fa";
constexpr const char* kGoldenCommandSessionHex =
    "4f4c4e4b01050000001c000000000020000000000000000c000000000000000000abcdef0102030405060708"
    "00000000000030390000000100000010";

std::uint8_t hex_value(char ch) {
    if (ch >= '0' && ch <= '9') {
        return static_cast<std::uint8_t>(ch - '0');
    }
    if (ch >= 'a' && ch <= 'f') {
        return static_cast<std::uint8_t>(ch - 'a' + 10);
    }
    if (ch >= 'A' && ch <= 'F') {
        return static_cast<std::uint8_t>(ch - 'A' + 10);
    }
    throw std::invalid_argument("invalid hex character");
}

std::vector<std::uint8_t> from_hex(const std::string& hex) {
    if (hex.size() % 2 != 0) {
        throw std::invalid_argument("hex input has odd length");
    }

    std::vector<std::uint8_t> bytes;
    bytes.reserve(hex.size() / 2);
    for (std::size_t i = 0; i < hex.size(); i += 2) {
        bytes.push_back(static_cast<std::uint8_t>((hex_value(hex[i]) << 4) | hex_value(hex[i + 1])));
    }
    return bytes;
}

std::string to_hex(const std::vector<std::uint8_t>& bytes) {
    constexpr char kHex[] = "0123456789abcdef";
    std::string out;
    out.reserve(bytes.size() * 2);
    for (const auto byte : bytes) {
        out.push_back(kHex[(byte >> 4) & 0x0f]);
        out.push_back(kHex[byte & 0x0f]);
    }
    return out;
}

void assert_protocol_error(const std::vector<std::uint8_t>& frame, const std::string& expected_message) {
    try {
        (void)orbit::decode_frame(frame);
        assert(false && "expected ProtocolError");
    } catch (const orbit::ProtocolError& error) {
        assert(std::string(error.what()) == expected_message);
    }
}

orbit::TelemetrySample golden_sample() {
    return orbit::TelemetrySample{
        1,
        0x0102030405060708ULL,
        42,
        7,
        99,
        1234567890ULL,
        -5250,
        3300,
    };
}

void test_decode_golden_telemetry_sample() {
    const auto frame = from_hex(kGoldenFrameHex);
    const auto decoded = orbit::decode_frame(frame);

    assert(decoded.header.message_type == 1);
    assert(decoded.header.payload_length == 44);
    assert(decoded.header.message_sequence == 1);

    assert(decoded.sample.spacecraft_id == 1);
    assert(decoded.sample.flight_boot_id == 0x0102030405060708ULL);
    assert(decoded.sample.telemetry_sequence == 42);
    assert(decoded.sample.device_generation == 7);
    assert(decoded.sample.device_sequence == 99);
    assert(decoded.sample.timestamp_monotonic_ns == 1234567890ULL);
    assert(decoded.sample.temperature_millideg_c == -5250);
    assert(decoded.sample.voltage_millivolts == 3300);
}

void test_encode_golden_telemetry_sample() {
    const auto encoded = orbit::encode_telemetry_sample(golden_sample(), 1);
    assert(to_hex(encoded) == kGoldenFrameHex);
}

void test_rejects_bad_magic() {
    auto frame = from_hex(kGoldenFrameHex);
    frame[0] = 'B';
    frame[1] = 'A';
    frame[2] = 'D';
    frame[3] = '!';
    assert_protocol_error(frame, "bad magic");
}

void test_rejects_unsupported_version() {
    auto frame = from_hex(kGoldenFrameHex);
    frame[4] = 2;
    assert_protocol_error(frame, "unsupported version");
}

void test_rejects_unsupported_message_type() {
    auto frame = from_hex(kGoldenFrameHex);
    frame[5] = 99;
    assert_protocol_error(frame, "unsupported message type");
}

void test_rejects_unsupported_flags() {
    auto frame = from_hex(kGoldenFrameHex);
    frame[7] = 1;
    assert_protocol_error(frame, "unsupported flags");
}

void test_rejects_truncated_header() {
    assert_protocol_error(std::vector<std::uint8_t>{'O', 'L', 'N', 'K'}, "truncated header");
}

void test_rejects_truncated_payload() {
    auto frame = from_hex(kGoldenFrameHex);
    frame.pop_back();
    assert_protocol_error(frame, "truncated payload");
}

void test_rejects_bad_payload_length() {
    auto frame = from_hex(kGoldenFrameHex);
    frame[15] = 43;
    assert_protocol_error(frame, "bad telemetry payload length");
}

void test_rejects_nonzero_reserved_field() {
    auto frame = from_hex(kGoldenFrameHex);
    frame[11] = 1;
    assert_protocol_error(frame, "reserved field is not zero");
}

void test_rejects_oversized_payload_length() {
    auto frame = from_hex(kGoldenFrameHex);
    frame[14] = 0x10;
    frame[15] = 0x01;
    assert_protocol_error(frame, "payload too large");
}

void test_rejects_trailing_bytes() {
    auto frame = from_hex(kGoldenFrameHex);
    frame.push_back(0);
    assert_protocol_error(frame, "trailing bytes");
}

void test_parser_accepts_split_chunks() {
    const auto frame = from_hex(kGoldenFrameHex);
    orbit::FrameParser parser;

    assert(parser.feed(std::vector<std::uint8_t>(frame.begin(), frame.begin() + 3)).empty());
    assert(parser.feed(std::vector<std::uint8_t>(frame.begin() + 3, frame.begin() + 28)).empty());
    const auto decoded = parser.feed(std::vector<std::uint8_t>(frame.begin() + 28, frame.end()));

    assert(decoded.size() == 1);
    assert(decoded[0].sample.temperature_millideg_c == -5250);
}

void test_parser_accepts_multiple_frames() {
    const auto frame = from_hex(kGoldenFrameHex);
    std::vector<std::uint8_t> doubled = frame;
    doubled.insert(doubled.end(), frame.begin(), frame.end());

    orbit::FrameParser parser;
    const auto decoded = parser.feed(doubled);

    assert(decoded.size() == 2);
    assert(decoded[0].header.message_sequence == 1);
    assert(decoded[1].header.message_sequence == 1);
}

void test_parser_accepts_every_single_split_point_for_all_golden_frames() {
    for (const auto* hex : {kGoldenFrameHex, kGoldenAckHex, kGoldenCommandRequestHex,
                            kGoldenCommandResultHex, kGoldenCommandSessionHex}) {
        const auto frame = from_hex(hex);
        for (std::size_t split = 1; split < frame.size(); ++split) {
            orbit::FrameParser parser;
            assert(parser.feed(std::vector<std::uint8_t>(frame.begin(), frame.begin() + split)).empty());
            const auto decoded = parser.feed(
                std::vector<std::uint8_t>(frame.begin() + split, frame.end()));
            assert(decoded.size() == 1);
            parser.finish();
        }
    }
}

void test_parser_rejects_invalid_header_before_waiting_for_payload() {
    const auto frame = from_hex(kGoldenFrameHex);
    orbit::FrameParser parser;
    std::vector<std::uint8_t> header(frame.begin(), frame.begin() + 28);
    header[4] = 2;

    try {
        (void)parser.feed(header);
        assert(false && "expected ProtocolError");
    } catch (const orbit::ProtocolError& error) {
        assert(std::string(error.what()) == "unsupported version");
    }
}

void test_parser_rejects_eof_during_frame() {
    auto frame = from_hex(kGoldenFrameHex);
    frame.pop_back();
    orbit::FrameParser parser;
    assert(parser.feed(frame).empty());
    try {
        parser.finish();
        assert(false && "expected ProtocolError");
    } catch (const orbit::ProtocolError& error) {
        assert(std::string(error.what()) == "truncated frame at end of stream");
    }
}

void test_parser_accepts_clean_eof() {
    orbit::FrameParser parser;
    assert(parser.feed(from_hex(kGoldenFrameHex)).size() == 1);
    parser.finish();
}

void test_encode_and_decode_telemetry_ack() {
    const orbit::TelemetryAck ack{1, 0x0102030405060708ULL, 42};
    const auto frame = orbit::encode_telemetry_ack(ack, 2);
    assert(to_hex(frame) == kGoldenAckHex);

    const auto decoded = orbit::decode_frame(frame);
    assert(decoded.header.message_type == orbit::kTelemetryAck);
    assert(decoded.header.payload_length == 20);
    assert(decoded.header.message_sequence == 2);
    assert(decoded.ack.spacecraft_id == 1);
    assert(decoded.ack.flight_boot_id == 0x0102030405060708ULL);
    assert(decoded.ack.telemetry_sequence == 42);
}

void test_parser_accepts_sample_followed_by_ack() {
    auto frames = from_hex(kGoldenFrameHex);
    const auto ack = from_hex(kGoldenAckHex);
    frames.insert(frames.end(), ack.begin(), ack.end());

    orbit::FrameParser parser;
    const auto decoded = parser.feed(frames);
    assert(decoded.size() == 2);
    assert(decoded[0].header.message_type == orbit::kTelemetrySample);
    assert(decoded[1].header.message_type == orbit::kTelemetryAck);
    assert(decoded[1].ack.telemetry_sequence == 42);
}

void test_rejects_bad_ack_payload_length() {
    auto frame = from_hex(kGoldenAckHex);
    frame[15] = 19;
    assert_protocol_error(frame, "bad telemetry ACK payload length");
}

void test_encode_and_decode_command_request_and_result() {
    const orbit::CommandRequest request{0xabcdefULL, 1, 99, orbit::kCommandSetPeriod, 10'000, 250, 1};
    const auto request_frame = orbit::encode_command_request(request, 10);
    assert(to_hex(request_frame) == kGoldenCommandRequestHex);
    const auto decoded_request = orbit::decode_frame(request_frame);
    assert(decoded_request.header.message_type == orbit::kCommandRequest);
    assert(decoded_request.command_request.command_session_id == request.command_session_id);
    assert(decoded_request.command_request.argument == 250);

    const orbit::CommandResult result{0xabcdefULL, 1, 99, orbit::kCommandSetPeriod, 0, 0, 7, 8, 250};
    const auto result_frame = orbit::encode_command_result(result, 11);
    assert(to_hex(result_frame) == kGoldenCommandResultHex);
    const auto decoded_result = orbit::decode_frame(result_frame);
    assert(decoded_result.header.message_type == orbit::kCommandResult);
    assert(decoded_result.command_result.command_id == 99);
    assert(decoded_result.command_result.current_period_ms == 250);
}

void test_encode_decode_and_stream_command_session() {
    const orbit::CommandSession session{0xabcdefULL, 0x0102030405060708ULL, 12'345, 1, 16};
    const auto frame = orbit::encode_command_session(session, 12);
    assert(to_hex(frame) == kGoldenCommandSessionHex);
    const auto decoded = orbit::decode_frame(frame);
    assert(decoded.header.message_type == orbit::kCommandSession);
    assert(decoded.header.payload_length == orbit::kCommandSessionPayloadLength);
    assert(decoded.command_session.command_session_id == session.command_session_id);
    assert(decoded.command_session.flight_boot_id == session.flight_boot_id);
    assert(decoded.command_session.flight_monotonic_ms == session.flight_monotonic_ms);
    assert(decoded.command_session.authorized_role == 1);
    assert(decoded.command_session.command_queue_capacity == 16);

    orbit::FrameParser parser;
    assert(parser.feed(std::vector<std::uint8_t>(frame.begin(), frame.begin() + 31)).empty());
    const auto parsed = parser.feed(std::vector<std::uint8_t>(frame.begin() + 31, frame.end()));
    assert(parsed.size() == 1);
    assert(parsed.front().command_session.command_session_id == session.command_session_id);
}

}  // namespace

int main() {
    test_decode_golden_telemetry_sample();
    test_encode_golden_telemetry_sample();
    test_rejects_bad_magic();
    test_rejects_unsupported_version();
    test_rejects_unsupported_message_type();
    test_rejects_unsupported_flags();
    test_rejects_truncated_header();
    test_rejects_truncated_payload();
    test_rejects_bad_payload_length();
    test_rejects_nonzero_reserved_field();
    test_rejects_oversized_payload_length();
    test_rejects_trailing_bytes();
    test_parser_accepts_split_chunks();
    test_parser_accepts_multiple_frames();
    test_parser_accepts_every_single_split_point_for_all_golden_frames();
    test_parser_rejects_invalid_header_before_waiting_for_payload();
    test_parser_rejects_eof_during_frame();
    test_parser_accepts_clean_eof();
    test_encode_and_decode_telemetry_ack();
    test_parser_accepts_sample_followed_by_ack();
    test_rejects_bad_ack_payload_length();
    test_encode_and_decode_command_request_and_result();
    test_encode_decode_and_stream_command_session();

    std::cout << "C++ protocol tests passed\n";
    return 0;
}
