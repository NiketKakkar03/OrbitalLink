import json
from pathlib import Path

import pytest

from ground.orbit_protocol import (
    FrameParser,
    ProtocolError,
    TelemetryAck,
    TelemetrySample,
    decode_frame,
    encode_telemetry_ack,
    encode_telemetry_sample,
)


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "telemetry_sample_v1.json"
ACK_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "telemetry_ack_v1.json"


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text())


def load_ack_fixture():
    return json.loads(ACK_FIXTURE_PATH.read_text())


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

@pytest.mark.parametrize('name', ['command_session','command_request','command_result','state_snapshot','loss_ack'])
def test_command_golden_frames_and_fragmentation(name):
    from ground.orbit_protocol import encode_control, ControlMessage
    fixture=json.loads((FIXTURE_PATH.parent / f'{name}_v1.json').read_text())
    frame=bytes.fromhex(fixture['frame_hex'])
    assert encode_control(fixture['message_type'],bytes.fromhex(fixture['payload_hex']))==frame
    parser=FrameParser()
    decoded=[]
    for byte in frame:
        decoded.extend(parser.feed(bytes([byte])))
    parser.finish()
    assert len(decoded)==1
    header,message=decoded[0]
    assert header.message_type==fixture['message_type']
    assert isinstance(message,ControlMessage)
    assert message.payload.hex()==fixture['payload_hex']
    corrupt=bytearray(frame);corrupt[15]+=1
    with pytest.raises(ProtocolError): decode_frame(corrupt)
