from __future__ import annotations

import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ground.orbit_control import control_request
from ground.orbit_protocol import (
    COMMAND_RESULT,
    COMMAND_SESSION,
    REJECT_BAD_SESSION,
    REJECT_EXPIRED,
    REJECT_QUEUE_FULL,
    REJECT_UNAUTHORIZED,
    RESULT_ACCEPTED,
    RESULT_REJECTED,
    CommandRequest,
    CommandResult,
    CommandSession,
    TelemetryAck,
    TelemetrySample,
    decode_frame,
    encode_command_request,
    encode_telemetry_ack,
)
from ground.orbit_storage import TelemetryStorage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def flight_service(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("m4-command-bin") / "flight_service"
    subprocess.run(
        [
            "c++", "-std=c++20", "-pthread", "-Wall", "-Wextra", "-Werror",
            "-I/opt/homebrew/include", "flight/flight_service.cpp", "-o", str(output),
            "-L/opt/homebrew/lib", "-lssl", "-lcrypto",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    return output


@pytest.fixture()
def certs(tmp_path: Path) -> Path:
    output = tmp_path / "certs"
    subprocess.run(["bash", "tools/make_dev_certs.sh", str(output)], cwd=PROJECT_ROOT, check=True)
    return output


def wait_until(predicate, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true before deadline")


def receive_exact(connection: ssl.SSLSocket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed during frame")
        data.extend(chunk)
    return bytes(data)


def receive_frame(connection: ssl.SSLSocket):
    header = receive_exact(connection, 28)
    payload_length = int.from_bytes(header[12:16], "big")
    return decode_frame(header + receive_exact(connection, payload_length))


def acknowledge(connection: ssl.SSLSocket, header, sample: TelemetrySample) -> None:
    connection.sendall(
        encode_telemetry_ack(
            TelemetryAck(sample.spacecraft_id, sample.flight_boot_id, sample.telemetry_sequence),
            header.message_sequence,
        )
    )


def test_operator_commands_execute_once_over_live_tls(
    tmp_path: Path, certs: Path, flight_service: Path
) -> None:
    database = tmp_path / "commands.db"
    ready_file = tmp_path / "ground.port"
    control_socket = Path("/tmp") / f"orbit-m4-{os.getpid()}-{time.time_ns()}.sock"
    ground = subprocess.Popen(
        [
            sys.executable, "-m", "ground.orbit_ground",
            "--database", str(database), "--ready-file", str(ready_file),
            "--control-socket", str(control_socket),
            "--ca-file", str(certs / "ca.cert.pem"),
            "--server-cert", str(certs / "server.cert.pem"),
            "--server-key", str(certs / "server.key.pem"),
            "--authorized-client", "orbital-ground",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    flight: subprocess.Popen[str] | None = None
    try:
        port = wait_until(lambda: int(ready_file.read_text()) if ready_file.exists() else None)
        try:
            wait_until(control_socket.exists)
        except AssertionError:
            ground.terminate()
            _stdout, ground_stderr = ground.communicate(timeout=5)
            pytest.fail(f"control socket did not start: {ground_stderr.decode()}")
        flight = subprocess.Popen(
            [
                str(flight_service), "127.0.0.1", str(port),
                "--count", "10000", "--interval-ms", "10", "--capacity", "1024",
                "--connect-timeout-ms", "250", "--io-timeout-ms", "500", "--drain-ms", "500",
                "--boot-id", "0x1122334455667788",
                "--ca-file", str(certs / "ca.cert.pem"),
                "--client-cert", str(certs / "client.cert.pem"),
                "--client-key", str(certs / "client.key.pem"),
                "--server-name", "orbital-ground",
                "--command-authorized-server-name", "orbital-ground",
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_until(
            lambda: control_request(control_socket, {"operation": "state"}).get("active_session_id")
        )

        command_ids: list[int] = []
        for command, argument in (
            ("set-period", 20),
            ("reset", 0),
            ("clear-fault", 0),
            ("disable", 0),
            ("enable", 0),
        ):
            response = control_request(
                control_socket,
                {"operation": "submit", "command": command, "argument": argument, "validity_ms": 5_000},
            )
            assert response["ok"] is True
            command_ids.append(response["command_id"])

        storage = TelemetryStorage(database)
        try:
            results = wait_until(
                lambda: storage.load_command_results()
                if len(storage.load_command_results()) == len(command_ids)
                else None
            )
            assert {item.result.command_id for item in results} == set(command_ids)
            assert all(item.result.status == RESULT_ACCEPTED for item in results)
            assert sorted(item.result.effect_counter for item in results) == [1, 2, 3, 4, 5]
            final = max(results, key=lambda item: item.result.effect_counter).result
            assert final.device_generation == 8
            assert final.current_period_ms == 20
            assert wait_until(
                lambda: any(sample.device_generation == 8 for sample in storage.load_samples())
            )
        finally:
            storage.close()

        flight.send_signal(signal.SIGTERM)
        stdout, stderr = flight.communicate(timeout=5)
        assert flight.returncode == 0, stderr
        summary = json.loads(stdout)
        assert summary["generated"] == (
            summary["acknowledged"]
            + summary["dropped_oldest"]
            + summary["discarded_shutdown"]
        )
    finally:
        if flight is not None and flight.poll() is None:
            flight.terminate()
            flight.wait(timeout=5)
        if ground.poll() is None:
            ground.terminate()
        ground.wait(timeout=5)


def test_lost_reset_result_and_old_session_retry_do_not_repeat_effect(
    tmp_path: Path, certs: Path, flight_service: Path
) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(certs / "ca.cert.pem"))
    context.load_cert_chain(str(certs / "server.cert.pem"), str(certs / "server.key.pem"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    port = listener.getsockname()[1]
    flight = subprocess.Popen(
        [
            str(flight_service), "127.0.0.1", str(port),
            "--count", "10000", "--interval-ms", "10", "--capacity", "1024",
            "--connect-timeout-ms", "250", "--io-timeout-ms", "500", "--drain-ms", "500",
            "--boot-id", "0x8877665544332211",
            "--ca-file", str(certs / "ca.cert.pem"),
            "--client-cert", str(certs / "client.cert.pem"),
            "--client-key", str(certs / "client.key.pem"),
            "--server-name", "orbital-ground",
            "--command-authorized-server-name", "orbital-ground",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        raw, _ = listener.accept()
        with context.wrap_socket(raw, server_side=True) as first:
            first.settimeout(3)
            _, first_session = receive_frame(first)
            assert isinstance(first_session, CommandSession)
            telemetry_header, telemetry = receive_frame(first)
            assert isinstance(telemetry, TelemetrySample)
            reset = CommandRequest(
                first_session.command_session_id, 1, 7001, 4,
                first_session.flight_monotonic_ms + 5_000, 0, first_session.authorized_role,
            )
            first.sendall(encode_command_request(reset, 1))
            acknowledge(first, telemetry_header, telemetry)
            while True:
                header, message = receive_frame(first)
                if isinstance(message, TelemetrySample):
                    acknowledge(first, header, message)
                    continue
                assert isinstance(message, CommandResult)
                assert message.command_id == reset.command_id
                assert message.status == RESULT_ACCEPTED
                assert message.effect_counter == 1
                break
            # Deliberately discard the successful reset result from the
            # operator workflow and reconnect with no result acknowledgement.

        raw, _ = listener.accept()
        with context.wrap_socket(raw, server_side=True) as second:
            second.settimeout(3)
            _, second_session = receive_frame(second)
            assert isinstance(second_session, CommandSession)
            assert second_session.command_session_id != first_session.command_session_id
            telemetry_header, telemetry = receive_frame(second)
            assert isinstance(telemetry, TelemetrySample)
            stale_reset = CommandRequest(
                first_session.command_session_id, 1, 7001, 4,
                second_session.flight_monotonic_ms + 5_000, 0, second_session.authorized_role,
            )
            set_period = CommandRequest(
                second_session.command_session_id, 1, 7002, 3,
                second_session.flight_monotonic_ms + 5_000, 250, second_session.authorized_role,
            )
            second.sendall(
                encode_command_request(stale_reset, 2)
                + encode_command_request(set_period, 3)
            )
            acknowledge(second, telemetry_header, telemetry)

            results: dict[int, CommandResult] = {}
            while len(results) < 2:
                header, message = receive_frame(second)
                if isinstance(message, TelemetrySample):
                    acknowledge(second, header, message)
                elif header.message_type == COMMAND_RESULT and isinstance(message, CommandResult):
                    results[message.command_id] = message
            assert results[7001].status == RESULT_REJECTED
            assert results[7001].reason == REJECT_BAD_SESSION
            assert results[7001].effect_counter == 1
            assert results[7002].status == RESULT_ACCEPTED
            assert results[7002].effect_counter == 2
            assert results[7002].device_generation == 8
            assert results[7002].current_period_ms == 250
    finally:
        flight.send_signal(signal.SIGTERM)
        stdout, stderr = flight.communicate(timeout=5)
        listener.close()
        assert flight.returncode == 0, stderr
        json.loads(stdout)


def test_live_full_queue_and_queued_expiration_have_no_device_effect(
    tmp_path: Path, certs: Path, flight_service: Path
) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(certs / "ca.cert.pem"))
    context.load_cert_chain(str(certs / "server.cert.pem"), str(certs / "server.key.pem"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    port = listener.getsockname()[1]
    flight = subprocess.Popen(
        [
            str(flight_service), "127.0.0.1", str(port),
            "--count", "10000", "--interval-ms", "10", "--capacity", "1024",
            "--connect-timeout-ms", "250", "--io-timeout-ms", "1000", "--drain-ms", "500",
            "--command-execution-delay-ms", "100",
            "--boot-id", "0x123456789abcdef0",
            "--ca-file", str(certs / "ca.cert.pem"),
            "--client-cert", str(certs / "client.cert.pem"),
            "--client-key", str(certs / "client.key.pem"),
            "--server-name", "orbital-ground",
            "--command-authorized-server-name", "orbital-ground",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        raw, _ = listener.accept()
        with context.wrap_socket(raw, server_side=True) as connection:
            connection.settimeout(5)
            _, session = receive_frame(connection)
            assert isinstance(session, CommandSession)
            telemetry_header, telemetry = receive_frame(connection)
            assert isinstance(telemetry, TelemetrySample)
            requests = [
                CommandRequest(
                    session.command_session_id, sequence, 8000 + sequence, 4,
                    session.flight_monotonic_ms + 50, 0, session.authorized_role,
                )
                for sequence in range(1, 18)
            ]
            connection.sendall(
                b"".join(encode_command_request(request, request.command_sequence) for request in requests)
            )
            acknowledge(connection, telemetry_header, telemetry)

            results: dict[int, CommandResult] = {}
            while len(results) < len(requests):
                header, message = receive_frame(connection)
                if isinstance(message, TelemetrySample):
                    acknowledge(connection, header, message)
                elif isinstance(message, CommandResult):
                    results[message.command_id] = message
            assert all(result.status == RESULT_REJECTED for result in results.values())
            assert all(result.effect_counter == 0 for result in results.values())
            reasons = [result.reason for result in results.values()]
            assert reasons.count(REJECT_QUEUE_FULL) == 1
            assert reasons.count(REJECT_EXPIRED) == 16
    finally:
        flight.send_signal(signal.SIGTERM)
        stdout, stderr = flight.communicate(timeout=5)
        listener.close()
        assert flight.returncode == 0, stderr
        json.loads(stdout)


def test_verified_telemetry_peer_without_command_mapping_cannot_forge_role(
    tmp_path: Path, certs: Path, flight_service: Path
) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(certs / "ca.cert.pem"))
    context.load_cert_chain(str(certs / "server.cert.pem"), str(certs / "server.key.pem"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    port = listener.getsockname()[1]
    flight = subprocess.Popen(
        [
            str(flight_service), "127.0.0.1", str(port),
            "--count", "10000", "--interval-ms", "10", "--capacity", "1024",
            "--connect-timeout-ms", "250", "--io-timeout-ms", "500", "--drain-ms", "500",
            "--ca-file", str(certs / "ca.cert.pem"),
            "--client-cert", str(certs / "client.cert.pem"),
            "--client-key", str(certs / "client.key.pem"),
            "--server-name", "orbital-ground",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        raw, _ = listener.accept()
        with context.wrap_socket(raw, server_side=True) as connection:
            connection.settimeout(3)
            _, session = receive_frame(connection)
            assert isinstance(session, CommandSession)
            assert session.authorized_role == 0
            telemetry_header, telemetry = receive_frame(connection)
            assert isinstance(telemetry, TelemetrySample)
            forged = CommandRequest(
                session.command_session_id, 1, 9001, 4,
                session.flight_monotonic_ms + 5_000, 0, 1,
            )
            connection.sendall(encode_command_request(forged, 1))
            acknowledge(connection, telemetry_header, telemetry)
            while True:
                header, message = receive_frame(connection)
                if isinstance(message, TelemetrySample):
                    acknowledge(connection, header, message)
                    continue
                assert isinstance(message, CommandResult)
                assert message.command_id == forged.command_id
                assert message.status == RESULT_REJECTED
                assert message.reason == REJECT_UNAUTHORIZED
                assert message.effect_counter == 0
                assert message.device_generation == 7
                break
    finally:
        flight.send_signal(signal.SIGTERM)
        stdout, stderr = flight.communicate(timeout=5)
        listener.close()
        assert flight.returncode == 0, stderr
        json.loads(stdout)
