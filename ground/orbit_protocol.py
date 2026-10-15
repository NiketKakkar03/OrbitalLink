from dataclasses import dataclass
import struct


MAGIC = b"OLNK"
VERSION = 1
HEADER_LENGTH = 28
MAX_PAYLOAD_LENGTH = 4096

TELEMETRY_SAMPLE = 1
TELEMETRY_ACK = 2
COMMAND_SESSION = 3
COMMAND_REQUEST = 4
COMMAND_RESULT = 5
STATE_SNAPSHOT = 6
LOSS_REPORT = 7
LOSS_ACK = 8
TELEMETRY_SAMPLE_PAYLOAD_LENGTH = 44
TELEMETRY_ACK_PAYLOAD_LENGTH = 20

HEADER_STRUCT = struct.Struct("!4sBBHHHIQI")
TELEMETRY_SAMPLE_STRUCT = struct.Struct("!IQQIIQiI")
TELEMETRY_ACK_STRUCT = struct.Struct("!IQQ")


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


@dataclass(frozen=True)
class TelemetryAck:
    spacecraft_id: int
    flight_boot_id: int
    telemetry_sequence: int


@dataclass(frozen=True)
class LossReport:
    spacecraft_id: int
    flight_boot_id: int
    report_id: int
    first: int
    last: int
    count: int
    reason: int = 1


def validate_loss(loss):
    if not loss.report_id or not loss.count or loss.reason not in (1,2) or (loss.reason == 1 and (not loss.first or loss.last < loss.first or loss.count > loss.last-loss.first+1)) or (loss.reason == 2 and (loss.flight_boot_id or loss.first or loss.last)):
        raise ProtocolError("invalid loss report")


def encode_loss_report(loss):
    validate_loss(loss)
    return encode_control(LOSS_REPORT, struct.pack('!IQQQQQI', loss.spacecraft_id, loss.flight_boot_id, loss.report_id, loss.first, loss.last, loss.count, loss.reason), loss.report_id)


def encode_loss_ack(loss):
    return encode_control(LOSS_ACK, struct.pack('!IQQ',loss.spacecraft_id,loss.flight_boot_id,loss.report_id),loss.report_id)


@dataclass(frozen=True)
class ControlMessage:
    payload: bytes


def encode_control(kind: int, payload: bytes, message_sequence: int = 1) -> bytes:
    expected = {COMMAND_SESSION: 24, COMMAND_REQUEST: 40, COMMAND_RESULT: 32, STATE_SNAPSHOT: 64, LOSS_REPORT: 48, LOSS_ACK: 20}
    if kind not in expected or len(payload) != expected[kind]:
        raise ProtocolError("bad command frame")
    return HEADER_STRUCT.pack(MAGIC, VERSION, kind, 0, HEADER_LENGTH, 0,
                              len(payload), message_sequence, 0) + payload


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


def encode_telemetry_ack(ack: TelemetryAck, message_sequence: int = 1) -> bytes:
    payload = TELEMETRY_ACK_STRUCT.pack(
        ack.spacecraft_id,
        ack.flight_boot_id,
        ack.telemetry_sequence,
    )

    header = HEADER_STRUCT.pack(
        MAGIC,
        VERSION,
        TELEMETRY_ACK,
        0,
        HEADER_LENGTH,
        0,
        len(payload),
        message_sequence,
        0,
    )

    return header + payload


def decode_frame(frame: bytes) -> tuple[Header, TelemetrySample | TelemetryAck]:
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
    if message_type not in (TELEMETRY_SAMPLE, TELEMETRY_ACK, COMMAND_SESSION, COMMAND_REQUEST, COMMAND_RESULT, STATE_SNAPSHOT, LOSS_REPORT, LOSS_ACK):
        raise ProtocolError("unsupported message type")
    if flags != 0:
        raise ProtocolError("unsupported flags")
    if header_length != HEADER_LENGTH:
        raise ProtocolError("bad header length")
    if reserved_a != 0 or reserved_b != 0:
        raise ProtocolError("reserved field is not zero")
    if payload_length > MAX_PAYLOAD_LENGTH:
        raise ProtocolError("payload too large")
    expected_payload_length = {
        TELEMETRY_SAMPLE: TELEMETRY_SAMPLE_PAYLOAD_LENGTH,
        TELEMETRY_ACK: TELEMETRY_ACK_PAYLOAD_LENGTH,
        COMMAND_SESSION: 24, COMMAND_REQUEST: 40, COMMAND_RESULT: 32, STATE_SNAPSHOT: 64, LOSS_REPORT: 48, LOSS_ACK: 20,
    }[message_type]
    if payload_length != expected_payload_length:
        if message_type == TELEMETRY_SAMPLE:
            raise ProtocolError("bad telemetry payload length")
        raise ProtocolError("bad telemetry ACK payload length")

    expected_frame_length = HEADER_LENGTH + payload_length
    if len(frame) < expected_frame_length:
        raise ProtocolError("truncated payload")
    if len(frame) > expected_frame_length:
        raise ProtocolError("trailing bytes")

    payload = frame[HEADER_LENGTH:expected_frame_length]

    header = Header(
        message_type=message_type,
        payload_length=payload_length,
        message_sequence=message_sequence,
    )

    if message_type == TELEMETRY_SAMPLE:
        values = TELEMETRY_SAMPLE_STRUCT.unpack(payload)
        message = TelemetrySample(
            spacecraft_id=values[0],
            flight_boot_id=values[1],
            telemetry_sequence=values[2],
            device_generation=values[3],
            device_sequence=values[4],
            timestamp_monotonic_ns=values[5],
            temperature_millideg_c=values[6],
            voltage_millivolts=values[7],
        )
    elif message_type == TELEMETRY_ACK:
        values = TELEMETRY_ACK_STRUCT.unpack(payload)
        message = TelemetryAck(
            spacecraft_id=values[0],
            flight_boot_id=values[1],
            telemetry_sequence=values[2],
        )
    elif message_type == LOSS_REPORT:
        message = LossReport(*struct.unpack("!IQQQQQI",payload))
        validate_loss(message)
    else:
        if message_type == STATE_SNAPSHOT:
            session,sequence,identifier,status,reserved,enabled,period,generation,fault,effects,now=struct.unpack('!QQQIIIIIIQQ',payload)
            if status or reserved or enabled not in (0,1) or fault not in (0,1) or not 0 <= period <= 60000:
                raise ProtocolError('invalid state snapshot')
        message = ControlMessage(payload)

    return header, message


class FrameParser:
    """Collect stream chunks and return complete decoded frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[Header, TelemetrySample | TelemetryAck]]:
        self._buffer.extend(chunk)
        frames: list[tuple[Header, TelemetrySample | TelemetryAck]] = []

        while True:
            if len(self._buffer) < HEADER_LENGTH:
                return frames

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
            ) = HEADER_STRUCT.unpack(self._buffer[:HEADER_LENGTH])

            if magic != MAGIC:
                raise ProtocolError("bad magic")
            if version != VERSION:
                raise ProtocolError("unsupported version")
            if message_type not in (TELEMETRY_SAMPLE, TELEMETRY_ACK, COMMAND_SESSION, COMMAND_REQUEST, COMMAND_RESULT, STATE_SNAPSHOT, LOSS_REPORT, LOSS_ACK):
                raise ProtocolError("unsupported message type")
            if flags != 0:
                raise ProtocolError("unsupported flags")
            if header_length != HEADER_LENGTH:
                raise ProtocolError("bad header length")
            if reserved_a != 0 or reserved_b != 0:
                raise ProtocolError("reserved field is not zero")
            if payload_length > MAX_PAYLOAD_LENGTH:
                raise ProtocolError("payload too large")
            expected_payload_length = {
                TELEMETRY_SAMPLE: TELEMETRY_SAMPLE_PAYLOAD_LENGTH,
                TELEMETRY_ACK: TELEMETRY_ACK_PAYLOAD_LENGTH,
                COMMAND_SESSION: 24, COMMAND_REQUEST: 40, COMMAND_RESULT: 32, STATE_SNAPSHOT: 64, LOSS_REPORT: 48, LOSS_ACK: 20,
            }[message_type]
            if payload_length != expected_payload_length:
                if message_type == TELEMETRY_SAMPLE:
                    raise ProtocolError("bad telemetry payload length")
                raise ProtocolError("bad telemetry ACK payload length")

            frame_length = HEADER_LENGTH + payload_length
            if len(self._buffer) < frame_length:
                return frames

            frame = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            frames.append(decode_frame(frame))

    def finish(self) -> None:
        """Validate that EOF did not arrive in the middle of a frame."""
        if self._buffer:
            raise ProtocolError("truncated frame at end of stream")
