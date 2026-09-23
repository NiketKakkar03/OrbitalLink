from dataclasses import dataclass
import struct


MAGIC = b"OLNK"
VERSION = 1
HEADER_LENGTH = 28
MAX_PAYLOAD_LENGTH = 4096

TELEMETRY_SAMPLE = 1
TELEMETRY_ACK = 2
COMMAND_REQUEST = 3
COMMAND_RESULT = 4
COMMAND_SESSION = 5
TELEMETRY_SAMPLE_PAYLOAD_LENGTH = 44
TELEMETRY_ACK_PAYLOAD_LENGTH = 20
COMMAND_REQUEST_PAYLOAD_LENGTH = 48
COMMAND_RESULT_PAYLOAD_LENGTH = 56
COMMAND_SESSION_PAYLOAD_LENGTH = 32

HEADER_STRUCT = struct.Struct("!4sBBHHHIQI")
TELEMETRY_SAMPLE_STRUCT = struct.Struct("!IQQIIQiI")
TELEMETRY_ACK_STRUCT = struct.Struct("!IQQ")
COMMAND_REQUEST_STRUCT = struct.Struct("!QQQIQQI")
COMMAND_RESULT_STRUCT = struct.Struct("!QQQIIIQQI")
COMMAND_SESSION_STRUCT = struct.Struct("!QQQII")

COMMAND_ENABLE = 1
COMMAND_DISABLE = 2
COMMAND_SET_PERIOD = 3
COMMAND_RESET = 4
COMMAND_CLEAR_FAULT = 5

RESULT_ACCEPTED = 0
RESULT_REJECTED = 1
RESULT_OUTCOME_UNKNOWN = 2

REJECT_UNAUTHORIZED = 1
REJECT_BAD_SESSION = 2
REJECT_BAD_SEQUENCE = 3
REJECT_EXPIRED = 4
REJECT_BAD_ARGUMENTS = 5
REJECT_QUEUE_FULL = 6


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
class CommandRequest:
    command_session_id: int
    command_sequence: int
    command_id: int
    command_type: int
    deadline_monotonic_ms: int
    argument: int
    role: int = 1


@dataclass(frozen=True)
class CommandResult:
    command_session_id: int
    command_sequence: int
    command_id: int
    command_type: int
    status: int
    reason: int
    effect_counter: int
    device_generation: int
    current_period_ms: int


@dataclass(frozen=True)
class CommandSession:
    command_session_id: int
    flight_boot_id: int
    flight_monotonic_ms: int
    authorized_role: int
    command_queue_capacity: int


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


def _encode_header(message_type: int, payload_length: int, message_sequence: int) -> bytes:
    return HEADER_STRUCT.pack(
        MAGIC,
        VERSION,
        message_type,
        0,
        HEADER_LENGTH,
        0,
        payload_length,
        message_sequence,
        0,
    )


def encode_command_request(command: CommandRequest, message_sequence: int = 1) -> bytes:
    payload = COMMAND_REQUEST_STRUCT.pack(
        command.command_session_id,
        command.command_sequence,
        command.command_id,
        command.command_type,
        command.deadline_monotonic_ms,
        command.argument,
        command.role,
    )
    return _encode_header(COMMAND_REQUEST, len(payload), message_sequence) + payload


def encode_command_result(result: CommandResult, message_sequence: int = 1) -> bytes:
    payload = COMMAND_RESULT_STRUCT.pack(
        result.command_session_id,
        result.command_sequence,
        result.command_id,
        result.command_type,
        result.status,
        result.reason,
        result.effect_counter,
        result.device_generation,
        result.current_period_ms,
    )
    return _encode_header(COMMAND_RESULT, len(payload), message_sequence) + payload


def encode_command_session(session: CommandSession, message_sequence: int = 1) -> bytes:
    payload = COMMAND_SESSION_STRUCT.pack(
        session.command_session_id,
        session.flight_boot_id,
        session.flight_monotonic_ms,
        session.authorized_role,
        session.command_queue_capacity,
    )
    return _encode_header(COMMAND_SESSION, len(payload), message_sequence) + payload


def decode_frame(frame: bytes) -> tuple[Header, TelemetrySample | TelemetryAck | CommandRequest | CommandResult | CommandSession]:
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
    if message_type not in (TELEMETRY_SAMPLE, TELEMETRY_ACK, COMMAND_REQUEST, COMMAND_RESULT, COMMAND_SESSION):
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
        COMMAND_REQUEST: COMMAND_REQUEST_PAYLOAD_LENGTH,
        COMMAND_RESULT: COMMAND_RESULT_PAYLOAD_LENGTH,
        COMMAND_SESSION: COMMAND_SESSION_PAYLOAD_LENGTH,
    }[message_type]
    if payload_length != expected_payload_length:
        if message_type == TELEMETRY_SAMPLE:
            raise ProtocolError("bad telemetry payload length")
        if message_type == TELEMETRY_ACK:
            raise ProtocolError("bad telemetry ACK payload length")
        if message_type == COMMAND_REQUEST:
            raise ProtocolError("bad command request payload length")
        if message_type == COMMAND_RESULT:
            raise ProtocolError("bad command result payload length")
        raise ProtocolError("bad command session payload length")

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
    elif message_type == COMMAND_REQUEST:
        values = COMMAND_REQUEST_STRUCT.unpack(payload)
        message = CommandRequest(*values)
    elif message_type == COMMAND_RESULT:
        values = COMMAND_RESULT_STRUCT.unpack(payload)
        message = CommandResult(*values)
    else:
        values = COMMAND_SESSION_STRUCT.unpack(payload)
        message = CommandSession(*values)

    return header, message


class FrameParser:
    """Collect stream chunks and return complete decoded frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[Header, TelemetrySample | TelemetryAck | CommandRequest | CommandResult | CommandSession]]:
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
            if message_type not in (TELEMETRY_SAMPLE, TELEMETRY_ACK, COMMAND_REQUEST, COMMAND_RESULT, COMMAND_SESSION):
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
                COMMAND_REQUEST: COMMAND_REQUEST_PAYLOAD_LENGTH,
                COMMAND_RESULT: COMMAND_RESULT_PAYLOAD_LENGTH,
                COMMAND_SESSION: COMMAND_SESSION_PAYLOAD_LENGTH,
            }[message_type]
            if payload_length != expected_payload_length:
                if message_type == TELEMETRY_SAMPLE:
                    raise ProtocolError("bad telemetry payload length")
                if message_type == TELEMETRY_ACK:
                    raise ProtocolError("bad telemetry ACK payload length")
                if message_type == COMMAND_REQUEST:
                    raise ProtocolError("bad command request payload length")
                if message_type == COMMAND_RESULT:
                    raise ProtocolError("bad command result payload length")
                raise ProtocolError("bad command session payload length")

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
