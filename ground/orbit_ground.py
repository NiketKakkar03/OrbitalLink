"""Mutually authenticated ground telemetry and operator service."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import ssl
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from ground.orbit_protocol import (
    COMMAND_RESULT,
    COMMAND_SESSION,
    FrameParser,
    ProtocolError,
    TELEMETRY_SAMPLE,
    CommandRequest,
    CommandResult,
    CommandSession,
    TelemetryAck,
    TelemetrySample,
    encode_telemetry_ack,
    encode_command_request,
)
from ground.orbit_control import CommandControl, UnixControlServer, control_request
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


def certificate_common_names(certificate: dict[object, object]) -> set[str]:
    """Extract only X.509 commonName values from ssl.getpeercert() output."""
    subject = certificate.get("subject", ())
    return {
        value
        for relative_name in subject
        for attribute, value in relative_name
        if attribute == "commonName" and isinstance(value, str)
    }


def is_authorized_certificate(certificate: dict[object, object], allowed: set[str]) -> bool:
    return not certificate_common_names(certificate).isdisjoint(allowed)


class GroundReceiver:
    """Receive telemetry, commit it, then acknowledge its durable identity."""

    def __init__(self, storage: SampleStorage, command_control: CommandControl | None = None):
        self._storage = storage
        self._command_control = command_control
        self._flight_monotonic_ms = 0
        self._session_received_monotonic_ns = 0
        self._outbound_message_sequence = 1
        self.receipts = 0

    def activate_command_session(self, session_id: int) -> None:
        if self._command_control is None:
            raise RuntimeError("command control is not configured")
        self._command_control.activate_session(session_id)

    def next_command(self, *, flight_monotonic_ms: int) -> CommandRequest | None:
        if self._command_control is None:
            return None
        return self._command_control.next_request(flight_monotonic_ms=flight_monotonic_ms)

    def close_command_session(self) -> int:
        if self._command_control is None:
            return 0
        return self._command_control.deactivate_session()

    def handle_connection(self, connection: socket.socket) -> None:
        parser = FrameParser()
        self._flight_monotonic_ms = 0
        self._session_received_monotonic_ns = 0
        self._outbound_message_sequence = 1
        connection.settimeout(0.1)
        try:
            while True:
                try:
                    chunk = connection.recv(64 * 1024)
                except TimeoutError:
                    self._send_pending_commands(connection)
                    continue
                if not chunk:
                    parser.finish()
                    return

                for header, message in parser.feed(chunk):
                    if header.message_type == COMMAND_SESSION and isinstance(message, CommandSession):
                        if self._command_control is None:
                            raise ProtocolError("command session received without command control")
                        self._command_control.activate_session(
                            message.command_session_id,
                            authorized_role=message.authorized_role,
                            remote_capacity=message.command_queue_capacity,
                        )
                        self._flight_monotonic_ms = message.flight_monotonic_ms
                        self._session_received_monotonic_ns = time.monotonic_ns()
                        self._send_pending_commands(connection)
                        continue
                    if header.message_type == COMMAND_RESULT and isinstance(message, CommandResult):
                        if self._command_control is None:
                            raise ProtocolError("command result received without command control")
                        self._command_control.accept_result(message)
                        self._send_pending_commands(connection)
                        continue
                    if header.message_type != TELEMETRY_SAMPLE or not isinstance(message, TelemetrySample):
                        raise ProtocolError(
                            "ground receiver expected telemetry sample, command session, or command result"
                        )

                    # ACK only after the telemetry transaction commits.
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
                    self._send_pending_commands(connection)
        finally:
            self.close_command_session()
            self._flight_monotonic_ms = 0
            self._session_received_monotonic_ns = 0

    def _send_pending_commands(self, connection: socket.socket) -> None:
        if self._command_control is None or self._session_received_monotonic_ns == 0:
            return
        elapsed_ms = (time.monotonic_ns() - self._session_received_monotonic_ns) // 1_000_000
        flight_now = self._flight_monotonic_ms + elapsed_ms
        while True:
            request = self._command_control.next_request(flight_monotonic_ms=flight_now)
            if request is None:
                return
            connection.sendall(
                encode_command_request(request, self._outbound_message_sequence)
            )
            self._outbound_message_sequence += 1


def serve(
    host: str,
    port: int,
    database_path: str | Path,
    *,
    ready_file: Path | None = None,
    max_receipts: int | None = None,
    stop_event: threading.Event | None = None,
    ca_file: Path | None = None,
    server_cert: Path | None = None,
    server_key: Path | None = None,
    authorized_clients: set[str] | None = None,
    control_socket: Path | None = None,
    command_queue_capacity: int = 32,
) -> int:
    """Serve connections until stopped; return the number of acknowledged receipts."""
    own_stop_event = stop_event or threading.Event()
    storage = TelemetryStorage(database_path)
    command_storage = TelemetryStorage(database_path)
    command_control = CommandControl(command_storage, capacity=command_queue_capacity)
    receiver = GroundReceiver(storage, command_control)
    if ca_file is None or server_cert is None or server_key is None:
        raise ValueError("TLS is mandatory: ca_file, server_cert, and server_key are required")
    allowed = {"orbital-ground"} if authorized_clients is None else authorized_clients
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(ca_file))
    context.load_cert_chain(certfile=str(server_cert), keyfile=str(server_key))

    control_thread: threading.Thread | None = None
    if control_socket is not None:
        control_server = UnixControlServer(control_socket, command_control)
        control_thread = threading.Thread(
            target=control_server.serve, args=(own_stop_event,), name="orbit-control"
        )
        control_thread.start()

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
                        with context.wrap_socket(connection, server_side=True) as tls_connection:
                            certificate = tls_connection.getpeercert()
                            if not is_authorized_certificate(certificate, allowed):
                                continue
                            receiver.handle_connection(tls_connection)
                    except (ProtocolError, OSError, ssl.SSLError):
                        # A malformed, unauthenticated, unauthorized, or
                        # interrupted peer is isolated to its connection.
                        continue
    finally:
        own_stop_event.set()
        if control_thread is not None:
            control_thread.join(timeout=1)
        command_storage.close()
        storage.close()

    return receiver.receipts


def main() -> int:
    parser = argparse.ArgumentParser(description="OrbitalLink secure ground receiver and operator CLI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--max-receipts", type=int)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--server-cert", type=Path)
    parser.add_argument("--server-key", type=Path)
    parser.add_argument("--authorized-client", action="append", default=[])
    parser.add_argument("--control-socket", type=Path)
    parser.add_argument("--command-queue-capacity", type=int, default=32)
    parser.add_argument("--argument", type=int, default=0)
    parser.add_argument("--validity-ms", type=int, default=5_000)
    parser.add_argument("--command-id", type=int)
    parser.add_argument("--result-limit", type=int)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="print the stored sample count")
    actions.add_argument("--tail", type=int, metavar="COUNT", help="print the newest records as JSON lines")
    actions.add_argument("--export", type=Path, metavar="PATH", help="export every record as JSON lines")
    actions.add_argument("--submit-command", choices=("enable", "disable", "set-period", "reset", "clear-fault"))
    actions.add_argument("--command-status", action="store_true")
    actions.add_argument("--command-results", action="store_true")
    actions.add_argument("--control-state", action="store_true")
    arguments = parser.parse_args()

    control_action = (
        arguments.submit_command is not None
        or arguments.command_status
        or arguments.command_results
        or arguments.control_state
    )
    if control_action:
        if arguments.control_socket is None:
            parser.error("operator commands require --control-socket")
        if arguments.command_status and arguments.command_id is None:
            parser.error("--command-status requires --command-id")
        if arguments.submit_command is not None:
            request = {
                "operation": "submit", "command": arguments.submit_command,
                "argument": arguments.argument, "validity_ms": arguments.validity_ms,
            }
        elif arguments.command_status:
            request = {"operation": "status", "command_id": arguments.command_id}
        elif arguments.command_results:
            request = {"operation": "results", "limit": arguments.result_limit}
        else:
            request = {"operation": "state"}
        response = control_request(arguments.control_socket, request)
        print(json.dumps(response, sort_keys=True))
        return 0 if response.get("ok") else 1

    if arguments.status or arguments.tail is not None or arguments.export is not None:
        if arguments.database is None:
            parser.error("telemetry queries require --database")
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

    if arguments.database is None:
        parser.error("the receiver requires --database")

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
        ca_file=arguments.ca_file,
        server_cert=arguments.server_cert,
        server_key=arguments.server_key,
        authorized_clients=set(arguments.authorized_client or ["orbital-ground"]),
        control_socket=arguments.control_socket,
        command_queue_capacity=arguments.command_queue_capacity,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
