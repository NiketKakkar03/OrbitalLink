from __future__ import annotations

import json
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ground.orbit_ground import GroundReceiver
from ground.orbit_protocol import (
    TELEMETRY_ACK,
    TelemetryAck,
    TelemetrySample,
    decode_frame,
    encode_telemetry_sample,
)
from ground.orbit_storage import TelemetryStorage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def certs(tmp_path: Path) -> Path:
    out = tmp_path / "certs"
    subprocess.run(["bash", "tools/make_dev_certs.sh", str(out)], cwd=PROJECT_ROOT, check=True)
    return out


def expected_sample(index: int) -> TelemetrySample:
    """Python copy of DeterministicSampleSource's intentionally simple formula."""
    return TelemetrySample(
        spacecraft_id=1,
        flight_boot_id=0x0102030405060708,
        telemetry_sequence=index + 1,
        device_generation=7,
        device_sequence=index,
        timestamp_monotonic_ns=1_000_000_000 + index * 100_000_000,
        temperature_millideg_c=-20_000 + (index % 80_001),
        voltage_millivolts=3_000 + (index % 401),
    )


@pytest.fixture(scope="session")
def native_sender(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("native-bin") / "send_sample"
    subprocess.run(
            [
                "c++",
                "-std=c++20",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I/opt/homebrew/include",
                str(PROJECT_ROOT / "flight" / "send_sample.cpp"),
                "-o",
                str(output),
                "-L/opt/homebrew/lib",
                "-lssl",
                "-lcrypto",
            ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    return output


def start_ground(tmp_path: Path, certs: Path, *, max_receipts: int | None = None):
    database = tmp_path / "telemetry.db"
    ready_file = tmp_path / "ground.port"
    command = [
        sys.executable,
        "-m",
        "ground.orbit_ground",
        "--database",
        str(database),
        "--ready-file",
        str(ready_file),
        "--ca-file",
        str(certs / "ca.cert.pem"),
        "--server-cert",
        str(certs / "server.cert.pem"),
        "--server-key",
        str(certs / "server.key.pem"),
    ]
    if max_receipts is not None:
        command += ["--max-receipts", str(max_receipts)]
    process = subprocess.Popen(
        command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready_file.exists():
            return process, database, int(ready_file.read_text())
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"ground exited before ready:\n{stdout.decode()}\n{stderr.decode()}")
        time.sleep(0.01)
    process.terminate()
    process.wait(timeout=5)
    raise AssertionError("ground did not become ready")


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def receive_exact(connection: socket.socket, byte_count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < byte_count:
        chunk = connection.recv(byte_count - len(chunks))
        if not chunk:
            raise AssertionError("connection closed before the complete frame arrived")
        chunks.extend(chunk)
    return bytes(chunks)


def tls_client(certs: Path) -> ssl.SSLSocket:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(certs / "ca.cert.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(certs / "client.cert.pem"), str(certs / "client.key.pem"))
    raw = socket.create_connection(("127.0.0.1", tls_client.port), timeout=5)
    return context.wrap_socket(raw, server_hostname="orbital-ground")


def test_native_sender_persists_10000_exact_samples_and_deduplicates(
    tmp_path: Path, native_sender: Path, certs: Path
):
    sample_count = 10_000
    duplicate_count = 100
    ground, database, port = start_ground(
        tmp_path, certs, max_receipts=sample_count + duplicate_count
    )
    try:
        sender = subprocess.run(
            [
                str(native_sender),
                "127.0.0.1",
                str(port),
                str(sample_count),
                str(certs / "ca.cert.pem"),
                str(certs / "client.cert.pem"),
                str(certs / "client.key.pem"),
                "orbital-ground",
                str(duplicate_count),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert sender.returncode == 0, sender.stderr
        assert sender.stdout == "acknowledged 10100 samples\n"
        ground.wait(timeout=10)
        assert ground.returncode == 0, ground.stderr.read().decode()

        storage = TelemetryStorage(database)
        try:
            assert storage.load_samples() == [expected_sample(index) for index in range(sample_count)]
        finally:
            storage.close()
    finally:
        stop_process(ground)


def test_malformed_frame_is_not_inserted(tmp_path: Path, certs: Path):
    ground, database, port = start_ground(tmp_path, certs)
    try:
        malformed = bytearray(encode_telemetry_sample(expected_sample(0)))
        malformed[:4] = b"BAD!"
        tls_client.port = port
        with tls_client(certs) as connection:
            connection.sendall(malformed)
            try:
                connection.unwrap()
            except (OSError, ssl.SSLError):
                pass

        storage = TelemetryStorage(database)
        try:
            assert storage.load_samples() == []
        finally:
            storage.close()
    finally:
        stop_process(ground)


def test_database_failure_sends_no_ack():
    class FailingStorage:
        def store_sample(self, _sample: TelemetrySample, **_metadata: object) -> bool:
            raise RuntimeError("injected commit failure")

    server_socket, client_socket = socket.socketpair()
    errors: list[BaseException] = []

    def run_receiver() -> None:
        try:
            with server_socket:
                GroundReceiver(FailingStorage()).handle_connection(server_socket)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_receiver)
    thread.start()
    try:
        client_socket.settimeout(2)
        client_socket.sendall(encode_telemetry_sample(expected_sample(0)))
        assert client_socket.recv(1) == b""
    finally:
        client_socket.close()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert str(errors[0]) == "injected commit failure"


def test_physically_invalid_sample_is_stored_and_acknowledged(tmp_path: Path):
    database = tmp_path / "telemetry.db"
    server_socket, client_socket = socket.socketpair()
    errors: list[BaseException] = []

    def run_receiver() -> None:
        storage = TelemetryStorage(database)
        try:
            with server_socket:
                GroundReceiver(storage).handle_connection(server_socket)
        except BaseException as error:
            errors.append(error)
        finally:
            storage.close()

    thread = threading.Thread(target=run_receiver)
    thread.start()
    try:
        sample = expected_sample(0)
        sample = TelemetrySample(
            spacecraft_id=sample.spacecraft_id,
            flight_boot_id=sample.flight_boot_id,
            telemetry_sequence=sample.telemetry_sequence,
            device_generation=sample.device_generation,
            device_sequence=sample.device_sequence,
            timestamp_monotonic_ns=sample.timestamp_monotonic_ns,
            temperature_millideg_c=150_001,
            voltage_millivolts=sample.voltage_millivolts,
        )
        client_socket.sendall(encode_telemetry_sample(sample))
        ack_frame = receive_exact(client_socket, 48)
        header, ack = decode_frame(ack_frame)
        assert header.message_type == TELEMETRY_ACK
        assert isinstance(ack, TelemetryAck)
        assert ack.telemetry_sequence == sample.telemetry_sequence
        client_socket.shutdown(socket.SHUT_RDWR)
    finally:
        client_socket.close()
        thread.join(timeout=5)

    assert not errors
    storage = TelemetryStorage(database)
    record = storage.load_records()[0]
    assert record.sample.temperature_millideg_c == 150_001
    assert record.physically_valid is False
    assert record.validity_reason == "temperature_out_of_range"
    storage.close()


def test_status_tail_and_export_cli(tmp_path: Path):
    database = tmp_path / "telemetry.db"
    export_path = tmp_path / "telemetry.jsonl"
    storage = TelemetryStorage(database)
    storage.store_sample(expected_sample(0), received_at_unix_ns=100)
    storage.store_sample(expected_sample(1), received_at_unix_ns=200)
    storage.close()

    base_command = [
        sys.executable,
        "-m",
        "ground.orbit_ground",
        "--database",
        str(database),
    ]
    status = subprocess.run(
        base_command + ["--status"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == "telemetry_samples=2\n"

    tail = subprocess.run(
        base_command + ["--tail", "1"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(tail.stdout)["sample"]["telemetry_sequence"] == 2

    exported = subprocess.run(
        base_command + ["--export", str(export_path)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert exported.stdout.startswith("exported=2 path=")
    rows = [json.loads(line) for line in export_path.read_text().splitlines()]
    assert [row["sample"]["telemetry_sequence"] for row in rows] == [1, 2]
