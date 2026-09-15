import json
from pathlib import Path

import pytest

from ground.orbit_protocol import (
    ProtocolError,
    TelemetrySample,
    decode_frame,
    encode_telemetry_sample,
)


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "telemetry_sample_v1.json"


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text())


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