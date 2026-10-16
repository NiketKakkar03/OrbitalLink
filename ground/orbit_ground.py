"""Authenticated ground receiver with an explicit loopback-only test transport."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import ssl
import threading
import struct
import time
from ground.orbit_commands import CommandBroker, ControlSocket
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from ground.orbit_protocol import (
    FrameParser,
    ProtocolError,
    TELEMETRY_SAMPLE, SENSOR_SAMPLE, CATEGORY_NAMES,
    COMMAND_SESSION, COMMAND_RESULT, STATE_SNAPSHOT, LOSS_REPORT, LossReport, encode_loss_ack, ControlMessage,
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
    if sample.category:
        reasons = []
        if sample.quality & 1:
            reasons.append("sensor_fault")
        if sample.category == 1 and (not 0 <= sample.values[0] <= 6000 or not 0 <= sample.values[2] <= 1000):
            reasons.append("power_out_of_range")
        if sample.category == 2 and any(not -100000 <= value <= 150000 for value in sample.values[:2]):
            reasons.append("temperature_out_of_range")
        return not reasons, ",".join(reasons) or None
    reasons: list[str] = []
    if not -100_000 <= sample.temperature_millideg_c <= 150_000:
        reasons.append("temperature_out_of_range")
    if not 0 <= sample.voltage_millivolts <= 6_000:
        reasons.append("voltage_out_of_range")
    return (not reasons, ",".join(reasons) or None)


class GroundReceiver:
    """Receive telemetry, commit it, then acknowledge its durable identity."""

    def __init__(self, storage: SampleStorage, broker=None, stop_event=None):
        self._storage = storage
        self.receipts = 0
        self.broker = broker
        self.stop_event = stop_event

    def handle_connection(self, connection: socket.socket) -> None:
        parser = FrameParser()
        while not self.stop_event or not self.stop_event.is_set():
            if self.broker:
                # One in-flight operator request keeps dispatch bounded.
                frame=self.broker.next_frame()
                if frame: connection.sendall(frame)
            try:
                chunk = connection.recv(64 * 1024)
            except TimeoutError:
                continue
            if not chunk:
                parser.finish()
                return

            frames=parser.feed(chunk)
            telemetry_types=(TELEMETRY_SAMPLE,SENSOR_SAMPLE)
            eof=False
            # TLS commonly returns one application record per recv. Drain only
            # bytes already available, with no batch-fill delay and a hard bound
            # on additional socket calls. Partial parser state survives a WANT_READ.
            if hasattr(connection,"gettimeout") and hasattr(connection,"settimeout"):
                previous_timeout=connection.gettimeout()
                try:
                    connection.settimeout(0.0)
                    queued=sum(header.message_type in telemetry_types for header,_ in frames)
                    control=any(header.message_type not in telemetry_types for header,_ in frames)
                    for _ in range(16):
                        if queued>=16 or control:break
                        try:
                            available=connection.recv(64*1024)
                        except (ssl.SSLWantReadError,ssl.SSLWantWriteError,BlockingIOError,TimeoutError):
                            break
                        if not available:
                            eof=True;break
                        parsed=parser.feed(available)
                        frames.extend(parsed)
                        queued+=sum(header.message_type in telemetry_types for header,_ in parsed)
                        control=any(header.message_type not in telemetry_types for header,_ in parsed)
                finally:
                    connection.settimeout(previous_timeout)

            pending=[]
            def flush_samples():
                if not pending:return
                records=[(message,*classify_physical_validity(message)) for _,message in pending]
                if hasattr(self._storage,"store_samples"):
                    self._storage.store_samples(records)
                else:
                    for message,valid,reason in records:
                        self._storage.store_sample(message,physically_valid=valid,validity_reason=reason)
                # Every record is committed before the first batch ACK. A
                # partial ACK write leaves durable rows safe for replay/dedup.
                for header,message in pending:
                    ack=TelemetryAck(message.spacecraft_id,message.flight_boot_id,message.telemetry_sequence)
                    connection.sendall(encode_telemetry_ack(ack,header.message_sequence))
                    self.receipts+=1
                pending.clear()

            for header, message in frames:
                if header.message_type in (TELEMETRY_SAMPLE,SENSOR_SAMPLE) and isinstance(message,TelemetrySample):
                    pending.append((header,message))
                    if len(pending)==16:flush_samples()
                    continue
                # Control and loss frames keep their original ordering relative
                # to telemetry, including their own commit-before-ACK boundary.
                flush_samples()
                if header.message_type == COMMAND_SESSION and isinstance(message, ControlMessage):
                    if self.broker: self.broker.open(*struct.unpack('!QQQ',message.payload))
                    continue
                if header.message_type == LOSS_REPORT and isinstance(message, LossReport):
                    self._storage.store_loss(message)
                    connection.sendall(encode_loss_ack(message))
                    continue
                if header.message_type == STATE_SNAPSHOT and isinstance(message, ControlMessage):
                    if self.broker: self.broker.snapshot(message.payload)
                    session,sequence,identifier,*_=struct.unpack('!QQQIIIIIIQQ',message.payload)
                    if hasattr(self._storage,'record_command_result'):
                        self._storage.record_command_result(command_id=identifier,command_session_id=session,command_sequence=sequence,status='applied',details=message.payload.hex())
                    continue
                if header.message_type == COMMAND_RESULT and isinstance(message, ControlMessage):
                    if self.broker: self.broker.result(message.payload)
                    if hasattr(self._storage,'record_command_result'):
                        session,sequence,identifier,status,_=struct.unpack('!QQQII',message.payload)
                        from ground.orbit_commands import STATUS_NAMES
                        self._storage.record_command_result(command_id=identifier,command_session_id=session,command_sequence=sequence,status=STATUS_NAMES[status])
                    continue
                if header.message_type not in (TELEMETRY_SAMPLE, SENSOR_SAMPLE) or not isinstance(message, TelemetrySample):
                    raise ProtocolError("ground receiver expected telemetry sample")

            flush_samples()
            if eof:
                parser.finish()
                return


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
    control_socket: Path | None = None,
    command_broker: CommandBroker | None = None,
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
    broker = command_broker or CommandBroker()
    receiver = GroundReceiver(storage, broker, own_stop_event)
    control = ControlSocket(control_socket,broker,own_stop_event) if control_socket else None

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen()
            listener.settimeout(0.2)
            actual_port = listener.getsockname()[1]
            if ready_file is not None:
                temporary_ready = ready_file.with_name(f".{ready_file.name}.tmp")
                temporary_ready.write_text(f"{actual_port}\n")
                temporary_ready.replace(ready_file)

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
                            connection = tls_context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
                            connection.settimeout(.2)
                            handshake_deadline=time.monotonic()+2
                            while not own_stop_event.is_set():
                                try:
                                    connection.do_handshake()
                                    break
                                except TimeoutError:
                                    if time.monotonic() >= handshake_deadline: raise
                            if own_stop_event.is_set(): continue
                            names = [value for kind, value in connection.getpeercert().get("subjectAltName", ()) if kind == "DNS"]
                            if allowed_client not in names:
                                connection.close()
                                continue
                        connection.settimeout(.2)
                        receiver.handle_connection(connection)
                    except (ProtocolError, OSError, ValueError):
                        # A malformed or interrupted peer is isolated to its
                        # connection. It never reaches storage or receives ACK.
                        continue
                    finally:
                        broker.close()
                        connection.close()
    finally:
        own_stop_event.set()
        broker.close()
        if control: control.close()
        storage.close()

    return receiver.receipts


def main() -> int:
    parser = argparse.ArgumentParser(description="OrbitalLink authenticated ground receiver")
    parser.add_argument("--control-socket",type=Path)
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
                counts = storage.category_counts()
                if any(name != "legacy" for name in counts):
                    print("categories=" + json.dumps(counts, sort_keys=True))
                return 0
            records = storage.load_records()
            if arguments.tail is not None:
                if arguments.tail < 1:
                    parser.error("--tail COUNT must be positive")
                for record in records[-arguments.tail :]:
                    print(json.dumps({**asdict(record), "category_name": CATEGORY_NAMES[record.sample.category]}, sort_keys=True))
                return 0
            assert arguments.export is not None
            with arguments.export.open("w") as output:
                for record in records:
                    output.write(json.dumps({**asdict(record), "category_name": CATEGORY_NAMES[record.sample.category]}, sort_keys=True) + "\n")
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
        control_socket=arguments.control_socket,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
