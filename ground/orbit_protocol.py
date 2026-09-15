from dataclasses import dataclass
import struct


MAGIC = b"OLNK"
VERSION = 1
HEADER_LENGTH = 28
MAX_PAYLOAD_LENGTH = 4096

TELEMETRY_SAMPLE = 1
TELEMETRY_SAMPLE_PAYLOAD_LENGTH = 44

HEADER_STRUCT = struct.Struct("!4sBBHHHIQI")
TELEMETRY_SAMPLE_STRUCT = struct.Struct("!IQQIIQiI")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Header:
    message_type: int
    payload_length: int
    message_sequence: int


@dataclass(frozen=True)
class TelemetrySample:
    spacecraft_id: int
    flight_boot_id: int
    telemetry_sequence: int
    device_generation: int
    device_sequence: int
    timestamp_monotonic_ns: int
    temperature_millideg_c: int
    voltage_millivolts: int


def encode_telemetry_sample(sample: TelemetrySample, message_sequence: int = 1) -> bytes:
    payload = TELEMETRY_SAMPLE_STRUCT.pack(
        sample.spacecraft_id,
        sample.flight_boot_id,
        sample.telemetry_sequence,
        sample.device_generation,
        sample.device_sequence,
        sample.timestamp_monotonic_ns,
        sample.temperature_millideg_c,
        sample.voltage_millivolts,
    )

    header = HEADER_STRUCT.pack(
        MAGIC,
        VERSION,
        TELEMETRY_SAMPLE,
        0,
        HEADER_LENGTH,
        0,
        len(payload),
        message_sequence,
        0,
    )

    return header + payload


def decode_frame(frame: bytes) -> tuple[Header, TelemetrySample]:
    if len(frame) < HEADER_LENGTH:
        raise ProtocolError("truncated header")

    (
        magic,
        version,
        message_type,
        flags,
        header_length,
        reserved_a,
        payload_length,
        message_sequence,
        reserved_b,
    ) = HEADER_STRUCT.unpack(frame[:HEADER_LENGTH])

    if magic != MAGIC:
        raise ProtocolError("bad magic")
    if version != VERSION:
        raise ProtocolError("unsupported version")
    if message_type != TELEMETRY_SAMPLE:
        raise ProtocolError("unsupported message type")
    if flags != 0:
        raise ProtocolError("unsupported flags")
    if header_length != HEADER_LENGTH:
        raise ProtocolError("bad header length")
    if reserved_a != 0 or reserved_b != 0:
        raise ProtocolError("reserved field is not zero")
    if payload_length > MAX_PAYLOAD_LENGTH:
        raise ProtocolError("payload too large")
    if payload_length != TELEMETRY_SAMPLE_PAYLOAD_LENGTH:
        raise ProtocolError("bad telemetry payload length")

    expected_frame_length = HEADER_LENGTH + payload_length
    if len(frame) < expected_frame_length:
        raise ProtocolError("truncated payload")
    if len(frame) > expected_frame_length:
        raise ProtocolError("trailing bytes")

    payload = frame[HEADER_LENGTH:expected_frame_length]

    values = TELEMETRY_SAMPLE_STRUCT.unpack(payload)

    header = Header(
        message_type=message_type,
        payload_length=payload_length,
        message_sequence=message_sequence,
    )

    sample = TelemetrySample(
        spacecraft_id=values[0],
        flight_boot_id=values[1],
        telemetry_sequence=values[2],
        device_generation=values[3],
        device_sequence=values[4],
        timestamp_monotonic_ns=values[5],
        temperature_millideg_c=values[6],
        voltage_millivolts=values[7],
    )

    return header, sample