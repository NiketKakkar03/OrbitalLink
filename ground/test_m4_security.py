from __future__ import annotations

import socket
import ssl
import stat
import subprocess
import sys
import time
from multiprocessing import get_context
from pathlib import Path

import pytest

from ground.orbit_ground import serve
from ground.orbit_protocol import (
    TELEMETRY_ACK,
    TelemetryAck,
    TelemetrySample,
    decode_frame,
    encode_telemetry_sample,
)
from ground.orbit_storage import TelemetryStorage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def native_sender(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("m4-security-bin") / "send_sample"
    subprocess.run(
        [
            "c++", "-std=c++20", "-Wall", "-Wextra", "-Werror",
            "-I/opt/homebrew/include", "flight/send_sample.cpp", "-o", str(output),
            "-L/opt/homebrew/lib", "-lssl", "-lcrypto",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    return output


@pytest.fixture()
def certs(tmp_path: Path) -> Path:
    out = tmp_path / "certs"
    subprocess.run(["bash", "tools/make_dev_certs.sh", str(out)], cwd=PROJECT_ROOT, check=True)
    return out


def start_ground(tmp_path: Path, certs: Path, *, authorized_clients: tuple[str, ...] = ("orbital-ground",)):
    database = tmp_path / "telemetry.db"
    ready = tmp_path / "ground.port"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ground.orbit_ground",
            "--database",
            str(database),
            "--ready-file",
            str(ready),
            "--ca-file",
            str(certs / "ca.cert.pem"),
            "--server-cert",
            str(certs / "server.cert.pem"),
            "--server-key",
            str(certs / "server.key.pem"),
            *[
                argument
                for identity in authorized_clients
                for argument in ("--authorized-client", identity)
            ],
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ready.exists():
            return process, database, int(ready.read_text())
        time.sleep(0.01)
    process.terminate()
    raise AssertionError("ground did not start")


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=5)


def client_context(
    certs: Path,
    *,
    certificate: str | None = "client",
    ca: Path | None = None,
) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca or certs / "ca.cert.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    if certificate is not None:
        context.load_cert_chain(
            str(certs / f"{certificate}.cert.pem"),
            str(certs / f"{certificate}.key.pem"),
        )
    return context


def sample(sequence: int = 1) -> TelemetrySample:
    return TelemetrySample(
        spacecraft_id=7,
        flight_boot_id=11,
        telemetry_sequence=sequence,
        device_generation=2,
        device_sequence=sequence,
        timestamp_monotonic_ns=123_456_789,
        temperature_millideg_c=22_500,
        voltage_millivolts=3_300,
    )


def exchange_payload(context: ssl.SSLContext, port: int, *, sequence: int = 1) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname="orbital-ground") as tls:
            tls.settimeout(2)
            tls.sendall(encode_telemetry_sample(sample(sequence), message_sequence=sequence))
            return tls.recv(4096)


def assert_valid_ack(payload: bytes, sequence: int = 1) -> None:
    header, message = decode_frame(payload)
    assert header.message_type == TELEMETRY_ACK
    assert message == TelemetryAck(7, 11, sequence)


def assert_payload_rejected(context: ssl.SSLContext, port: int) -> None:
    try:
        response = exchange_payload(context, port)
    except (ConnectionError, OSError, ssl.SSLError):
        return
    assert response == b""


def stored_count(database: Path) -> int:
    storage = TelemetryStorage(database)
    try:
        return storage.count_samples()
    finally:
        storage.close()


def run_ground_with_allowlist(
    database: str,
    ready_file: str,
    cert_directory: str,
    authorized_clients: set[str],
) -> None:
    certs = Path(cert_directory)
    serve(
        "127.0.0.1",
        0,
        database,
        ready_file=Path(ready_file),
        ca_file=certs / "ca.cert.pem",
        server_cert=certs / "server.cert.pem",
        server_key=certs / "server.key.pem",
        authorized_clients=authorized_clients,
    )


def test_certificate_generator_is_portable_and_restricts_private_keys(certs: Path) -> None:
    assert stat.S_IMODE(certs.stat().st_mode) == 0o700
    private_keys = list(certs.glob("*.key.pem"))
    assert private_keys
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_keys)
    for expected in (
        "wrong-ca-client",
        "expired-client",
        "not-yet-valid-client",
        "unauthorized-client",
        "misleading-subject-client",
    ):
        assert (certs / f"{expected}.cert.pem").is_file()


@pytest.mark.parametrize(
    "certificate",
    [None, "wrong-ca-client", "expired-client", "not-yet-valid-client"],
)
def test_untrusted_or_invalid_client_certificate_rejects_valid_payload_and_server_recovers(
    tmp_path: Path, certs: Path, certificate: str | None
) -> None:
    ground, database, port = start_ground(tmp_path, certs)
    try:
        assert_payload_rejected(client_context(certs, certificate=certificate), port)
        assert stored_count(database) == 0
        assert_valid_ack(exchange_payload(client_context(certs), port))
        assert stored_count(database) == 1
    finally:
        stop(ground)


@pytest.mark.parametrize("certificate", ["unauthorized-client", "misleading-subject-client"])
def test_authorization_matches_exact_common_name_only(
    tmp_path: Path, certs: Path, certificate: str
) -> None:
    ground, database, port = start_ground(tmp_path, certs)
    try:
        assert_payload_rejected(client_context(certs, certificate=certificate), port)
        assert stored_count(database) == 0
        assert_valid_ack(exchange_payload(client_context(certs), port))
        assert stored_count(database) == 1
    finally:
        stop(ground)


def test_explicit_empty_allowlist_denies_every_client(tmp_path: Path, certs: Path) -> None:
    database = tmp_path / "deny-all.db"
    ready = tmp_path / "deny-all.port"
    process = get_context("spawn").Process(
        target=run_ground_with_allowlist,
        args=(str(database), str(ready), str(certs), set()),
    )
    process.start()
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.01)
        assert ready.exists(), "ground did not start"
        assert_payload_rejected(client_context(certs), int(ready.read_text()))
        assert stored_count(database) == 0
    finally:
        process.terminate()
        process.join(timeout=5)


def test_wrong_server_identity_and_plaintext_are_rejected(tmp_path: Path, certs: Path) -> None:
    ground, database, port = start_ground(tmp_path, certs)
    try:
        with pytest.raises(ssl.SSLError):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                client_context(certs).wrap_socket(raw, server_hostname="wrong.example")

        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            raw.sendall(b"OLNK plaintext")
            try:
                assert raw.recv(1) == b""
            except ConnectionResetError:
                pass

        assert stored_count(database) == 0
        assert_valid_ack(exchange_payload(client_context(certs), port))
        assert stored_count(database) == 1
    finally:
        stop(ground)


@pytest.mark.parametrize(
    ("ca_name", "server_name"),
    [("wrong-ca.cert.pem", "orbital-ground"), ("ca.cert.pem", "wrong.example")],
)
def test_cpp_client_rejects_wrong_ca_and_server_identity(
    tmp_path: Path,
    certs: Path,
    native_sender: Path,
    ca_name: str,
    server_name: str,
) -> None:
    ground, database, port = start_ground(tmp_path, certs)
    try:
        result = subprocess.run(
            [
                str(native_sender), "127.0.0.1", str(port), "1",
                str(certs / ca_name), str(certs / "client.cert.pem"),
                str(certs / "client.key.pem"), server_name,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert stored_count(database) == 0
        assert_valid_ack(exchange_payload(client_context(certs), port))
        assert stored_count(database) == 1
    finally:
        stop(ground)
