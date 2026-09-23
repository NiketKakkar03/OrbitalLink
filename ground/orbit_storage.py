"""Durable SQLite storage for decoded OrbitalLink telemetry samples."""

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ground.orbit_protocol import CommandResult, TelemetrySample


_UINT32_MAX = (1 << 32) - 1
_UINT64_MAX = (1 << 64) - 1
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1
MAX_EVENT_DETAILS_LENGTH = 1024
MAX_RESULT_DETAILS_LENGTH = 1024


@dataclass(frozen=True)
class StoredTelemetry:
    sample: TelemetrySample
    received_at_unix_ns: int
    physically_valid: bool
    validity_reason: str | None


@dataclass(frozen=True)
class StoredCommand:
    command_id: int
    command_type: int
    argument: int
    validity_ms: int
    state: str
    created_at_unix_ns: int
    command_session_id: int | None
    command_sequence: int | None


@dataclass(frozen=True)
class StoredCommandResult:
    result: CommandResult
    received_at_unix_ns: int


def _validate_integer(name: str, value: int, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _encode_uint64(value: int) -> str:
    """Represent an unsigned 64-bit value without SQLite signed overflow."""
    return f"{value:016x}"


def _decode_uint64(value: str) -> int:
    return int(value, 16)


def _validate_sample(sample: TelemetrySample) -> None:
    if not isinstance(sample, TelemetrySample):
        raise TypeError("sample must be a TelemetrySample")

    _validate_integer("spacecraft_id", sample.spacecraft_id, 0, _UINT32_MAX)
    _validate_integer("flight_boot_id", sample.flight_boot_id, 0, _UINT64_MAX)
    _validate_integer("telemetry_sequence", sample.telemetry_sequence, 0, _UINT64_MAX)
    _validate_integer("device_generation", sample.device_generation, 0, _UINT32_MAX)
    _validate_integer("device_sequence", sample.device_sequence, 0, _UINT32_MAX)
    _validate_integer(
        "timestamp_monotonic_ns", sample.timestamp_monotonic_ns, 0, _UINT64_MAX
    )
    _validate_integer(
        "temperature_millideg_c",
        sample.temperature_millideg_c,
        _INT32_MIN,
        _INT32_MAX,
    )
    _validate_integer("voltage_millivolts", sample.voltage_millivolts, 0, _UINT32_MAX)


class TelemetryStorage:
    """Store telemetry and report success only after SQLite commits it."""

    def __init__(self, database_path: str | Path):
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = threading.RLock()
        try:
            self._create_table()
        except Exception:
            self.connection.close()
            raise

    def _create_table(self) -> None:
        # SQLite INTEGER is signed 64-bit. Protocol uint64 fields are fixed-width
        # hexadecimal TEXT so every value through 2**64 - 1 is preserved exactly.
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_samples (
                spacecraft_id INTEGER NOT NULL,
                flight_boot_id TEXT NOT NULL,
                telemetry_sequence TEXT NOT NULL,
                device_generation INTEGER NOT NULL,
                device_sequence INTEGER NOT NULL,
                timestamp_monotonic_ns TEXT NOT NULL,
                temperature_millideg_c INTEGER NOT NULL,
                voltage_millivolts INTEGER NOT NULL,
                received_at_unix_ns TEXT NOT NULL,
                physically_valid INTEGER NOT NULL CHECK (physically_valid IN (0, 1)),
                validity_reason TEXT,
                PRIMARY KEY (spacecraft_id, flight_boot_id, telemetry_sequence)
            )
            """
        )
        self.connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS event_details (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at_unix_ns TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT NOT NULL CHECK (length(details) <= {MAX_EVENT_DETAILS_LENGTH})
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS command_requests (
                command_id TEXT PRIMARY KEY,
                command_type INTEGER NOT NULL,
                argument TEXT NOT NULL,
                validity_ms INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at_unix_ns TEXT NOT NULL,
                command_session_id TEXT,
                command_sequence TEXT
            )
            """
        )
        self.connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS command_results (
                command_id TEXT PRIMARY KEY,
                command_session_id TEXT NOT NULL,
                command_sequence TEXT NOT NULL,
                received_at_unix_ns TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT NOT NULL CHECK (length(details) <= {MAX_RESULT_DETAILS_LENGTH}),
                command_type INTEGER,
                status_code INTEGER,
                reason INTEGER,
                effect_counter TEXT,
                device_generation TEXT,
                current_period_ms INTEGER
            )
            """
        )
        existing_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(command_results)")
        }
        for name, column_type in (
            ("command_type", "INTEGER"),
            ("status_code", "INTEGER"),
            ("reason", "INTEGER"),
            ("effect_counter", "TEXT"),
            ("device_generation", "TEXT"),
            ("current_period_ms", "INTEGER"),
        ):
            if name not in existing_columns:
                self.connection.execute(
                    f"ALTER TABLE command_results ADD COLUMN {name} {column_type}"
                )
        self.connection.commit()

    def create_command(
        self,
        *,
        command_id: int,
        command_type: int,
        argument: int,
        validity_ms: int,
        created_at_unix_ns: int | None = None,
    ) -> None:
        for name, value, maximum in (
            ("command_id", command_id, _UINT64_MAX),
            ("command_type", command_type, _UINT32_MAX),
            ("argument", argument, _UINT64_MAX),
            ("validity_ms", validity_ms, _UINT32_MAX),
        ):
            _validate_integer(name, value, 0, maximum)
        if validity_ms == 0:
            raise ValueError("validity_ms must be positive")
        created_at_unix_ns = time.time_ns() if created_at_unix_ns is None else created_at_unix_ns
        _validate_integer("created_at_unix_ns", created_at_unix_ns, 0, _UINT64_MAX)
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO command_requests (
                    command_id, command_type, argument, validity_ms, state,
                    created_at_unix_ns, command_session_id, command_sequence
                ) VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL)
                """,
                (
                    _encode_uint64(command_id), command_type, _encode_uint64(argument),
                    validity_ms, _encode_uint64(created_at_unix_ns),
                ),
            )
            self.connection.commit()

    def mark_command_sent(self, command_id: int, session_id: int, sequence: int) -> None:
        for name, value in (("command_id", command_id), ("session_id", session_id), ("sequence", sequence)):
            _validate_integer(name, value, 0, _UINT64_MAX)
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE command_requests SET state = 'sent', command_session_id = ?,
                    command_sequence = ? WHERE command_id = ? AND state = 'queued'
                """,
                (_encode_uint64(session_id), _encode_uint64(sequence), _encode_uint64(command_id)),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise ValueError("command is not queued")
            self.connection.commit()

    def mark_session_uncertain(self, session_id: int) -> int:
        _validate_integer("session_id", session_id, 0, _UINT64_MAX)
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE command_requests SET state = 'outcome_unknown'
                WHERE state = 'sent' AND command_session_id = ?
                """,
                (_encode_uint64(session_id),),
            )
            self.connection.commit()
            return cursor.rowcount

    def mark_all_sent_commands_uncertain(self) -> int:
        """Resolve commands stranded by a prior ground-process exit."""
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE command_requests SET state = 'outcome_unknown' WHERE state = 'sent'"
            )
            self.connection.commit()
            return cursor.rowcount

    def load_commands(self, *, limit: int | None = None) -> list[StoredCommand]:
        if limit is not None:
            _validate_integer("limit", limit, 1, _UINT32_MAX)
        sql = """
            SELECT command_id, command_type, argument, validity_ms, state,
                   created_at_unix_ns, command_session_id, command_sequence
            FROM command_requests ORDER BY created_at_unix_ns, command_id
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)
        with self._lock:
            rows = self.connection.execute(sql, parameters).fetchall()
        return [
            StoredCommand(
                command_id=_decode_uint64(row[0]), command_type=row[1],
                argument=_decode_uint64(row[2]), validity_ms=row[3], state=row[4],
                created_at_unix_ns=_decode_uint64(row[5]),
                command_session_id=None if row[6] is None else _decode_uint64(row[6]),
                command_sequence=None if row[7] is None else _decode_uint64(row[7]),
            )
            for row in rows
        ]

    def load_command(self, command_id: int) -> StoredCommand | None:
        _validate_integer("command_id", command_id, 0, _UINT64_MAX)
        return next((item for item in self.load_commands() if item.command_id == command_id), None)

    def store_command_result(
        self, result: CommandResult, *, received_at_unix_ns: int | None = None
    ) -> bool:
        if not isinstance(result, CommandResult):
            raise TypeError("result must be a CommandResult")
        received_at_unix_ns = time.time_ns() if received_at_unix_ns is None else received_at_unix_ns
        _validate_integer("received_at_unix_ns", received_at_unix_ns, 0, _UINT64_MAX)
        with self._lock:
            try:
                cursor = self.connection.execute(
                    """
                    INSERT INTO command_results (
                        command_id, command_session_id, command_sequence,
                        received_at_unix_ns, status, details, command_type,
                        status_code, reason, effect_counter, device_generation,
                        current_period_ms
                    ) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (command_id) DO NOTHING
                    """,
                    (
                        _encode_uint64(result.command_id),
                        _encode_uint64(result.command_session_id),
                        _encode_uint64(result.command_sequence),
                        _encode_uint64(received_at_unix_ns), str(result.status),
                        result.command_type, result.status, result.reason,
                        _encode_uint64(result.effect_counter),
                        _encode_uint64(result.device_generation), result.current_period_ms,
                    ),
                )
                if cursor.rowcount == 1:
                    self.connection.execute(
                        """
                        UPDATE command_requests SET state = 'result'
                        WHERE command_id = ? AND command_session_id = ? AND command_sequence = ?
                        """,
                        (
                            _encode_uint64(result.command_id),
                            _encode_uint64(result.command_session_id),
                            _encode_uint64(result.command_sequence),
                        ),
                    )
                self.connection.commit()
                return cursor.rowcount == 1
            except sqlite3.Error:
                self.connection.rollback()
                raise

    def load_command_results(self, *, limit: int | None = None) -> list[StoredCommandResult]:
        if limit is not None:
            _validate_integer("limit", limit, 1, _UINT32_MAX)
        sql = """
            SELECT command_session_id, command_sequence, command_id, command_type,
                   status_code, reason, effect_counter, device_generation,
                   current_period_ms, received_at_unix_ns
            FROM command_results WHERE status_code IS NOT NULL
            ORDER BY received_at_unix_ns, command_id
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)
        with self._lock:
            rows = self.connection.execute(sql, parameters).fetchall()
        return [
            StoredCommandResult(
                result=CommandResult(
                    command_session_id=_decode_uint64(row[0]),
                    command_sequence=_decode_uint64(row[1]), command_id=_decode_uint64(row[2]),
                    command_type=row[3], status=row[4], reason=row[5],
                    effect_counter=_decode_uint64(row[6]),
                    device_generation=_decode_uint64(row[7]), current_period_ms=row[8],
                ),
                received_at_unix_ns=_decode_uint64(row[9]),
            )
            for row in rows
        ]

    def store_sample(
        self,
        sample: TelemetrySample,
        *,
        received_at_unix_ns: int | None = None,
        physically_valid: bool = True,
        validity_reason: str | None = None,
    ) -> bool:
        """Commit one sample; return False when that sample already exists.

        A successful return means the transaction has committed, so a caller may
        safely send a telemetry ACK. Database failures propagate to the caller.
        """
        _validate_sample(sample)
        if received_at_unix_ns is None:
            received_at_unix_ns = time.time_ns()
        _validate_integer(
            "received_at_unix_ns", received_at_unix_ns, 0, _UINT64_MAX
        )
        if not isinstance(physically_valid, bool):
            raise TypeError("physically_valid must be a bool")
        if validity_reason is not None and not isinstance(validity_reason, str):
            raise TypeError("validity_reason must be text or None")
        if physically_valid and validity_reason is not None:
            raise ValueError("a physically valid sample cannot have a validity reason")
        if not physically_valid and not validity_reason:
            raise ValueError("a physically invalid sample requires a validity reason")

        try:
            cursor = self.connection.execute(
                """
                INSERT INTO telemetry_samples (
                    spacecraft_id,
                    flight_boot_id,
                    telemetry_sequence,
                    device_generation,
                    device_sequence,
                    timestamp_monotonic_ns,
                    temperature_millideg_c,
                    voltage_millivolts,
                    received_at_unix_ns,
                    physically_valid,
                    validity_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (spacecraft_id, flight_boot_id, telemetry_sequence)
                DO NOTHING
                """,
                (
                    sample.spacecraft_id,
                    _encode_uint64(sample.flight_boot_id),
                    _encode_uint64(sample.telemetry_sequence),
                    sample.device_generation,
                    sample.device_sequence,
                    _encode_uint64(sample.timestamp_monotonic_ns),
                    sample.temperature_millideg_c,
                    sample.voltage_millivolts,
                    _encode_uint64(received_at_unix_ns),
                    int(physically_valid),
                    validity_reason,
                ),
            )
            inserted = cursor.rowcount == 1
            self.connection.commit()
            return inserted
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def load_samples(self) -> list[TelemetrySample]:
        """Return samples in their unsigned telemetry-sequence order."""
        cursor = self.connection.execute(
            """
            SELECT
                spacecraft_id,
                flight_boot_id,
                telemetry_sequence,
                device_generation,
                device_sequence,
                timestamp_monotonic_ns,
                temperature_millideg_c,
                voltage_millivolts
            FROM telemetry_samples
            ORDER BY telemetry_sequence
            """
        )

        return [
            TelemetrySample(
                spacecraft_id=row[0],
                flight_boot_id=_decode_uint64(row[1]),
                telemetry_sequence=_decode_uint64(row[2]),
                device_generation=row[3],
                device_sequence=row[4],
                timestamp_monotonic_ns=_decode_uint64(row[5]),
                temperature_millideg_c=row[6],
                voltage_millivolts=row[7],
            )
            for row in cursor.fetchall()
        ]

    def load_records(self, limit: int | None = None) -> list[StoredTelemetry]:
        """Load samples together with ground receive time and validity metadata."""
        if limit is not None:
            _validate_integer("limit", limit, 1, _UINT32_MAX)
        sql = """
            SELECT
                spacecraft_id, flight_boot_id, telemetry_sequence,
                device_generation, device_sequence, timestamp_monotonic_ns,
                temperature_millideg_c, voltage_millivolts,
                received_at_unix_ns, physically_valid, validity_reason
            FROM telemetry_samples
            ORDER BY telemetry_sequence
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)
        rows = self.connection.execute(sql, parameters).fetchall()
        return [
            StoredTelemetry(
                sample=TelemetrySample(
                    spacecraft_id=row[0],
                    flight_boot_id=_decode_uint64(row[1]),
                    telemetry_sequence=_decode_uint64(row[2]),
                    device_generation=row[3],
                    device_sequence=row[4],
                    timestamp_monotonic_ns=_decode_uint64(row[5]),
                    temperature_millideg_c=row[6],
                    voltage_millivolts=row[7],
                ),
                received_at_unix_ns=_decode_uint64(row[8]),
                physically_valid=bool(row[9]),
                validity_reason=row[10],
            )
            for row in rows
        ]

    def count_samples(self) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM telemetry_samples"
        ).fetchone()[0]

    def record_event(
        self, event_type: str, details: str, *, received_at_unix_ns: int | None = None
    ) -> int:
        if not event_type:
            raise ValueError("event_type must not be empty")
        if not isinstance(details, str):
            raise TypeError("details must be text")
        if len(details) > MAX_EVENT_DETAILS_LENGTH:
            raise ValueError("event details are too long")
        if received_at_unix_ns is None:
            received_at_unix_ns = time.time_ns()
        _validate_integer("received_at_unix_ns", received_at_unix_ns, 0, _UINT64_MAX)
        try:
            cursor = self.connection.execute(
                "INSERT INTO event_details (received_at_unix_ns, event_type, details) VALUES (?, ?, ?)",
                (_encode_uint64(received_at_unix_ns), event_type, details),
            )
            self.connection.commit()
            return cursor.lastrowid
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def record_command_result(
        self,
        *,
        command_id: int,
        command_session_id: int,
        command_sequence: int,
        status: str,
        details: str = "",
        received_at_unix_ns: int | None = None,
    ) -> bool:
        for name, value in (
            ("command_id", command_id),
            ("command_session_id", command_session_id),
            ("command_sequence", command_sequence),
        ):
            _validate_integer(name, value, 0, _UINT64_MAX)
        if not status:
            raise ValueError("status must not be empty")
        if not isinstance(details, str):
            raise TypeError("details must be text")
        if len(details) > MAX_RESULT_DETAILS_LENGTH:
            raise ValueError("command result details are too long")
        if received_at_unix_ns is None:
            received_at_unix_ns = time.time_ns()
        _validate_integer("received_at_unix_ns", received_at_unix_ns, 0, _UINT64_MAX)
        try:
            cursor = self.connection.execute(
                """
                INSERT INTO command_results (
                    command_id, command_session_id, command_sequence,
                    received_at_unix_ns, status, details
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (command_id) DO NOTHING
                """,
                (
                    _encode_uint64(command_id),
                    _encode_uint64(command_session_id),
                    _encode_uint64(command_sequence),
                    _encode_uint64(received_at_unix_ns),
                    status,
                    details,
                ),
            )
            inserted = cursor.rowcount == 1
            self.connection.commit()
            return inserted
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()
