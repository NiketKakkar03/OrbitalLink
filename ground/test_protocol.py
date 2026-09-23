import json
from pathlib import Path

import pytest

from ground.orbit_protocol import (
    COMMAND_REQUEST,
    COMMAND_RESULT,
    COMMAND_SESSION,
    FrameParser,
    ProtocolError,
    CommandRequest,
    CommandResult,
    CommandSession,
    TelemetryAck,
    TelemetrySample,
    decode_frame,
    encode_command_request,
    encode_command_result,
    encode_command_session,
    encode_telemetry_ack,
    encode_telemetry_sample,
)


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "telemetry_sample_v1.json"
ACK_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "telemetry_ack_v1.json"
COMMAND_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "command_v1.json"


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text())


def load_ack_fixture():
    return json.loads(ACK_FIXTURE_PATH.read_text())


def load_command_fixture():
    return json.loads(COMMAND_FIXTURE_PATH.read_text())


def test_decode_golden_telemetry_sample():
    fixture = load_fixture()
    frame = bytes.fromhex(fixture["frame_hex"])

    header, sample = decode_frame(frame)

    assert header.message_type == 1
    assert header.payload_length == 44
    assert header.message_sequence == 1

    assert sample.spacecraft_id == 1
    assert sample.flight_boot_id == int("0102030405060708", 16)
    assert sample.telemetry_sequence == 42
    assert sample.device_generation == 7
    assert sample.device_sequence == 99
    assert sample.timestamp_monotonic_ns == 1234567890
    assert sample.temperature_millideg_c == -5250
    assert sample.voltage_millivolts == 3300


def test_encode_golden_telemetry_sample():
    fixture = load_fixture()

    sample = TelemetrySample(
        spacecraft_id=1,
        flight_boot_id=int("0102030405060708", 16),
        telemetry_sequence=42,
        device_generation=7,
        device_sequence=99,
        timestamp_monotonic_ns=1234567890,
        temperature_millideg_c=-5250,
        voltage_millivolts=3300,
    )

    assert encode_telemetry_sample(sample, message_sequence=1).hex() == fixture["frame_hex"]


def test_rejects_bad_magic():
    fixture = load_fixture()
    frame = bytearray(bytes.fromhex(fixture["frame_hex"]))
    frame[0:4] = b"BAD!"

    with pytest.raises(ProtocolError, match="bad magic"):
        decode_frame(bytes(frame))


def test_rejects_unsupported_version():
    fixture = load_fixture()
    frame = bytearray(bytes.fromhex(fixture["frame_hex"]))
    frame[4] = 2

    with pytest.raises(ProtocolError, match="unsupported version"):
        decode_frame(bytes(frame))


def test_rejects_unsupported_message_type():
    fixture = load_fixture()
    frame = bytearray(bytes.fromhex(fixture["frame_hex"]))
    frame[5] = 99

    with pytest.raises(ProtocolError, match="unsupported message type"):
        decode_frame(bytes(frame))


def test_rejects_unsupported_flags():
    fixture = load_fixture()
    frame = bytearray(bytes.fromhex(fixture["frame_hex"]))
    frame[7] = 1

    with pytest.raises(ProtocolError, match="unsupported flags"):
        decode_frame(bytes(frame))


def test_rejects_truncated_header():
    with pytest.raises(ProtocolError, match="truncated header"):
        decode_frame(b"OLNK")


def test_rejects_truncated_payload():
    fixture = load_fixture()
    frame = bytes.fromhex(fixture["frame_hex"])

    with pytest.raises(ProtocolError, match="truncated payload"):
        decode_frame(frame[:-1])


def test_rejects_bad_payload_length():
    fixture = load_fixture()
    frame = bytearray(bytes.fromhex(fixture["frame_hex"]))

    frame[15] = 43

    with pytest.raises(ProtocolError, match="bad telemetry payload length"):
        decode_frame(bytes(frame))


def test_rejects_nonzero_reserved_field():
    fixture = load_fixture()
    frame = bytearray(bytes.fromhex(fixture["frame_hex"]))
    frame[11] = 1

    with pytest.raises(ProtocolError, match="reserved field is not zero"):
        decode_frame(bytes(frame))


def test_rejects_oversized_payload_length():
    fixture = load_fixture()
    frame = bytearray(bytes.fromhex(fixture["frame_hex"]))
    frame[12:16] = (4097).to_bytes(4, "big")

    with pytest.raises(ProtocolError, match="payload too large"):
        decode_frame(bytes(frame))


def test_rejects_trailing_bytes():
    fixture = load_fixture()
    frame = bytes.fromhex(fixture["frame_hex"])

    with pytest.raises(ProtocolError, match="trailing bytes"):
        decode_frame(frame + b"\x00")


def test_parser_accepts_split_chunks():
    fixture = load_fixture()
    frame = bytes.fromhex(fixture["frame_hex"])
    parser = FrameParser()

    assert parser.feed(frame[:3]) == []
    assert parser.feed(frame[3:28]) == []
    decoded = parser.feed(frame[28:])

    assert len(decoded) == 1
    header, sample = decoded[0]
    assert header.message_sequence == 1
    assert sample.temperature_millideg_c == -5250


def test_parser_accepts_multiple_frames_in_one_chunk():
    fixture = load_fixture()
    frame = bytes.fromhex(fixture["frame_hex"])
    parser = FrameParser()

    decoded = parser.feed(frame + frame)

    assert len(decoded) == 2
    assert decoded[0][1].telemetry_sequence == 42
    assert decoded[1][1].telemetry_sequence == 42


def test_parser_accepts_every_single_split_point_for_all_golden_frames():
    fixture = load_command_fixture()
    frames = [
        bytes.fromhex(load_fixture()["frame_hex"]),
        bytes.fromhex(load_ack_fixture()["frame_hex"]),
        bytes.fromhex(fixture["request_frame_hex"]),
        bytes.fromhex(fixture["result_frame_hex"]),
        bytes.fromhex(fixture["session_frame_hex"]),
    ]
    for frame in frames:
        for split in range(1, len(frame)):
            parser = FrameParser()
            assert parser.feed(frame[:split]) == []
            decoded = parser.feed(frame[split:])
            assert len(decoded) == 1
            parser.finish()


def test_parser_rejects_invalid_header_before_waiting_for_payload():
    fixture = load_fixture()
    frame = bytearray(bytes.fromhex(fixture["frame_hex"]))
    frame[0:4] = b"BAD!"
    parser = FrameParser()

    with pytest.raises(ProtocolError, match="bad magic"):
        parser.feed(bytes(frame[:28]))


def test_parser_rejects_eof_during_frame():
    frame = bytes.fromhex(load_fixture()["frame_hex"])
    parser = FrameParser()

    assert parser.feed(frame[:-1]) == []
    with pytest.raises(ProtocolError, match="truncated frame at end of stream"):
        parser.finish()


def test_parser_accepts_clean_eof():
    parser = FrameParser()
    parser.feed(bytes.fromhex(load_fixture()["frame_hex"]))

    parser.finish()


def test_encode_and_decode_telemetry_ack():
    fixture = load_ack_fixture()
    ack = TelemetryAck(
        spacecraft_id=fixture["ack"]["spacecraft_id"],
        flight_boot_id=int(fixture["ack"]["flight_boot_id_hex"], 16),
        telemetry_sequence=fixture["ack"]["telemetry_sequence"],
    )

    frame = encode_telemetry_ack(ack, message_sequence=2)
    header, decoded = decode_frame(frame)

    assert frame.hex() == fixture["frame_hex"]
    assert header.message_type == 2
    assert header.payload_length == 20
    assert header.message_sequence == 2
    assert decoded == ack


def test_parser_accepts_sample_followed_by_ack():
    sample_frame = bytes.fromhex(load_fixture()["frame_hex"])
    ack = TelemetryAck(1, int("0102030405060708", 16), 42)
    ack_frame = encode_telemetry_ack(ack, message_sequence=2)
    parser = FrameParser()

    decoded = parser.feed(sample_frame + ack_frame)

    assert len(decoded) == 2
    assert isinstance(decoded[0][1], TelemetrySample)
    assert decoded[1][1] == ack


def test_rejects_bad_ack_payload_length():
    frame = bytearray(encode_telemetry_ack(TelemetryAck(1, 2, 3)))
    frame[15] = 19

    with pytest.raises(ProtocolError, match="bad telemetry ACK payload length"):
        decode_frame(bytes(frame))


def test_encode_and_decode_command_request_and_result():
    request = CommandRequest(0xABCDEF, 1, 99, 3, 10_000, 250, 1)
    result = CommandResult(0xABCDEF, 1, 99, 3, 0, 0, 7, 8, 250)

    request_frame = encode_command_request(request, message_sequence=10)
    result_frame = encode_command_result(result, message_sequence=11)
    fixture = load_command_fixture()
    assert request_frame.hex() == fixture["request_frame_hex"]
    assert result_frame.hex() == fixture["result_frame_hex"]
    request_header, decoded_request = decode_frame(request_frame)
    result_header, decoded_result = decode_frame(result_frame)

    assert request_header.message_type == COMMAND_REQUEST
    assert decoded_request == request
    assert result_header.message_type == COMMAND_RESULT
    assert decoded_result == result


def test_encode_decode_and_stream_command_session():
    session = CommandSession(0xABCDEF, 0x0102030405060708, 12_345, 1, 16)
    frame = encode_command_session(session, message_sequence=12)
    assert frame.hex() == load_command_fixture()["session_frame_hex"]
    header, decoded = decode_frame(frame)

    assert header.message_type == COMMAND_SESSION
    assert header.payload_length == 32
    assert decoded == session

    parser = FrameParser()
    assert parser.feed(frame[:31]) == []
    assert parser.feed(frame[31:]) == [(header, session)]


def test_rejects_bad_command_session_payload_length():
    frame = bytearray(encode_command_session(CommandSession(1, 2, 3, 1, 16)))
    frame[15] = 31
    with pytest.raises(ProtocolError, match="bad command session payload length"):
        decode_frame(bytes(frame))
