"""Authenticated ground receiver with an explicit loopback-only test transport."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import ssl
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
    tls_context: ssl.SSLContext | None = None,
    allowed_client: str | None = None,
    test_plaintext: bool = False,
) -> int:
    """Serve connections until stopped; return the number of acknowledged receipts."""
    own_stop_event = stop_event or threading.Event()
    if tls_context is None and not test_plaintext:
        raise ValueError("TLS context required")
    if test_plaintext and host not in ("127.0.0.1", "localhost"):
        raise ValueError("test plaintext transport requires loopback")
    if tls_context is not None and not allowed_client:
        raise ValueError("explicit client SAN allowlist identity required")
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
                        if tls_context is not None:
                            connection = tls_context.wrap_socket(connection, server_side=True)
                            names = [value for kind, value in connection.getpeercert().get("subjectAltName", ()) if kind == "DNS"]
                            if allowed_client not in names:
                                connection.close()
                                continue
                        receiver.handle_connection(connection)
                    except (ProtocolError, OSError):
                        # A malformed or interrupted peer is isolated to its
                        # connection. It never reaches storage or receives ACK.
                        continue
                    finally:
                        connection.close()
    finally:
        storage.close()

    return receiver.receipts


def main() -> int:
    parser = argparse.ArgumentParser(description="OrbitalLink authenticated ground receiver")
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--allowed-client")
    parser.add_argument("--test-plaintext", action="store_true", help="loopback test fixture transport only")
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
    context = None
    if not arguments.test_plaintext:
        if not all((arguments.ca, arguments.cert, arguments.key, arguments.allowed_client)):
            parser.error("TLS requires --ca --cert --key --allowed-client")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(arguments.ca)
        context.load_cert_chain(arguments.cert, arguments.key)
    elif any((arguments.ca, arguments.cert, arguments.key, arguments.allowed_client)):
        parser.error("test plaintext cannot be combined with TLS options")

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
        tls_context=context,
        allowed_client=arguments.allowed_client,
        test_plaintext=arguments.test_plaintext,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
