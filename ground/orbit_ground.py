"""Plaintext loopback ground receiver used by the M2 native test stage."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from ground.orbit_protocol import (
    FrameParser,
    ProtocolError,
    TELEMETRY_SAMPLE,
    TelemetryAck,
    TelemetrySample,
    encode_telemetry_ack,
)
from ground.orbit_storage import TelemetryStorage


class SampleStorage(Protocol):
    def store_sample(
        self,
        sample: TelemetrySample,
        *,
        physically_valid: bool = True,
        validity_reason: str | None = None,
    ) -> bool: ...


def classify_physical_validity(sample: TelemetrySample) -> tuple[bool, str | None]:
    """Classify plausible sensor values without confusing them with corrupt frames."""
    reasons: list[str] = []
    if not -100_000 <= sample.temperature_millideg_c <= 150_000:
        reasons.append("temperature_out_of_range")
    if not 0 <= sample.voltage_millivolts <= 6_000:
        reasons.append("voltage_out_of_range")
    return (not reasons, ",".join(reasons) or None)


class GroundReceiver:
    """Receive telemetry, commit it, then acknowledge its durable identity."""

    def __init__(self, storage: SampleStorage):
        self._storage = storage
        self.receipts = 0

    def handle_connection(self, connection: socket.socket) -> None:
        parser = FrameParser()
        while True:
            chunk = connection.recv(64 * 1024)
            if not chunk:
                parser.finish()
                return

            for header, message in parser.feed(chunk):
                if header.message_type != TELEMETRY_SAMPLE or not isinstance(message, TelemetrySample):
                    raise ProtocolError("ground receiver expected telemetry sample")

                # store_sample returns only after its transaction commits.  A
                # duplicate is already durable, so it receives the same ACK.
                physically_valid, validity_reason = classify_physical_validity(message)
                self._storage.store_sample(
                    message,
                    physically_valid=physically_valid,
                    validity_reason=validity_reason,
                )
                ack = TelemetryAck(
                    spacecraft_id=message.spacecraft_id,
                    flight_boot_id=message.flight_boot_id,
                    telemetry_sequence=message.telemetry_sequence,
                )
                connection.sendall(encode_telemetry_ack(ack, header.message_sequence))
                self.receipts += 1


def serve(
    host: str,
    port: int,
    database_path: str | Path,
    *,
    ready_file: Path | None = None,
    max_receipts: int | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Serve connections until stopped; return the number of acknowledged receipts."""
    own_stop_event = stop_event or threading.Event()
    storage = TelemetryStorage(database_path)
    receiver = GroundReceiver(storage)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen()
            listener.settimeout(0.2)
            actual_port = listener.getsockname()[1]
            if ready_file is not None:
                ready_file.write_text(f"{actual_port}\n")

            while not own_stop_event.is_set():
                if max_receipts is not None and receiver.receipts >= max_receipts:
                    break
                try:
                    connection, _address = listener.accept()
                except TimeoutError:
                    continue

                with connection:
                    connection.settimeout(10)
                    try:
                        receiver.handle_connection(connection)
                    except (ProtocolError, OSError):
                        # A malformed or interrupted peer is isolated to its
                        # connection. It never reaches storage or receives ACK.
                        continue
    finally:
        storage.close()

    return receiver.receipts


def main() -> int:
    parser = argparse.ArgumentParser(description="OrbitalLink M2 plaintext ground receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--max-receipts", type=int)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="print the stored sample count")
    actions.add_argument("--tail", type=int, metavar="COUNT", help="print the newest records as JSON lines")
    actions.add_argument("--export", type=Path, metavar="PATH", help="export every record as JSON lines")
    arguments = parser.parse_args()

    if arguments.status or arguments.tail is not None or arguments.export is not None:
        storage = TelemetryStorage(arguments.database)
        try:
            if arguments.status:
                print(f"telemetry_samples={storage.count_samples()}")
                return 0
            records = storage.load_records()
            if arguments.tail is not None:
                if arguments.tail < 1:
                    parser.error("--tail COUNT must be positive")
                for record in records[-arguments.tail :]:
                    print(json.dumps(asdict(record), sort_keys=True))
                return 0
            assert arguments.export is not None
            with arguments.export.open("w") as output:
                for record in records:
                    output.write(json.dumps(asdict(record), sort_keys=True) + "\n")
            print(f"exported={len(records)} path={arguments.export}")
            return 0
        finally:
            storage.close()

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    serve(
        arguments.host,
        arguments.port,
        arguments.database,
        ready_file=arguments.ready_file,
        max_receipts=arguments.max_receipts,
        stop_event=stop_event,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
