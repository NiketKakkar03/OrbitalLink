from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time

import pytest

from ground.dev_certificates import generate
from ground.orbit_storage import TelemetryStorage

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("certificates")
    generate(directory)
    return directory


@pytest.fixture(scope="session")
def tls_flight(tmp_path_factory):
    output = tmp_path_factory.mktemp("tls-flight") / "flight"
    candidates = [os.environ.get("OPENSSL_PREFIX", ""), "/opt/homebrew/opt/openssl@3", "/usr/local/opt/openssl@3"]
    prefix = next((Path(value) for value in candidates if value and Path(value).exists()), None)
    flags = [f"-I{prefix}/include", f"-L{prefix}/lib"] if prefix else []
    flags += shlex.split(os.environ.get("CPPFLAGS", "")) + shlex.split(os.environ.get("LDFLAGS", ""))
    sanitizers = os.environ.get("ORBIT_TEST_SANITIZERS")
    if sanitizers:
        flags += [f"-fsanitize={sanitizers}", "-fno-omit-frame-pointer", "-g"]
    subprocess.run(["c++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread",
                    *flags, "flight/flight_service.cpp", "flight/durable_outbox.cpp", "-o", str(output), "-lssl", "-lcrypto", "-lsqlite3"],
                   cwd=ROOT, check=True)
    return output


def start_tls_ground(tmp_path, certificates, identity="ground"):
    ready = tmp_path / "ready"
    database = tmp_path / "telemetry.db"
    process = subprocess.Popen([sys.executable, "-m", "ground.orbit_ground",
        "--database", str(database), "--ready-file", str(ready),
        "--ca", str(certificates / "ca.pem"), "--cert", str(certificates / f"{identity}.pem"),
        "--key", str(certificates / f"{identity}.key"), "--allowed-client", "flight.orbital.test"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 5
    while not ready.exists():
        assert process.poll() is None, process.communicate()[1]
        assert time.monotonic() < deadline
        time.sleep(.02)
    return process, database, int(ready.read_text())


@pytest.mark.parametrize("identity,server_name,expected", [
    ("flight", "ground.orbital.test", 4),
    ("unauthorized", "ground.orbital.test", 0),
    ("flight", "wrong.orbital.test", 0),
    ("expired", "ground.orbital.test", 0),
    ("future", "ground.orbital.test", 0),
])
def test_tls_authentication_and_identity(tmp_path, certificates, tls_flight, identity, server_name, expected):
    ground, database, port = start_tls_ground(tmp_path, certificates)
    try:
        result = subprocess.run([str(tls_flight), "127.0.0.1", str(port), "--count", "4",
            "--interval-ms", "0", "--drain-ms", "500", "--ca", str(certificates / "ca.pem"),
            "--cert", str(certificates / f"{identity}.pem"), "--key", str(certificates / f"{identity}.key"),
            "--server-name", server_name, "--outbox", str(tmp_path/"outbox.db")], capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["acknowledged"] == expected
        storage = TelemetryStorage(database)
        try:
            assert storage.count_samples() == expected
        finally:
            storage.close()
    finally:
        ground.terminate()
        ground.communicate(timeout=12)


@pytest.mark.parametrize("mode", ["missing_certificate", "wrong_ca", "plaintext"])
def test_rejected_clients_insert_no_samples(tmp_path, certificates, mode):
    ground, database, port = start_tls_ground(tmp_path, certificates)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            if mode == "plaintext":
                raw.sendall(b"OLNK" + b"\0" * 68)
                try:
                    assert not raw.recv(100)
                except ConnectionResetError:
                    pass
            else:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.minimum_version = ssl.TLSVersion.TLSv1_3
                if mode == "missing_certificate":
                    context.load_verify_locations(certificates / "ca.pem")
                else:
                    context.load_default_certs()
                    context.load_cert_chain(certificates / "flight.pem", certificates / "flight.key")
                with pytest.raises((ssl.SSLError, ConnectionResetError, BrokenPipeError)):
                    with context.wrap_socket(raw, server_hostname="ground.orbital.test") as connection:
                        connection.sendall(b"OLNK" + b"\0" * 68)
                        connection.recv(100)
        storage = TelemetryStorage(database)
        try:
            assert storage.count_samples() == 0
        finally:
            storage.close()
    finally:
        ground.terminate()
        ground.communicate(timeout=12)


def test_foreign_ca_client_rejected_by_server(tmp_path, certificates):
    foreign = tmp_path / "foreign"
    generate(foreign)
    ground, database, port = start_tls_ground(tmp_path, certificates)
    try:
        context = ssl.create_default_context(cafile=str(certificates / "ca.pem"))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(foreign / "flight.pem", foreign / "flight.key")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            with pytest.raises((ssl.SSLError, ConnectionResetError, BrokenPipeError)):
                with context.wrap_socket(raw, server_hostname="ground.orbital.test") as connection:
                    connection.sendall(b"OLNK" + b"\0" * 68)
                    connection.recv(100)
        storage = TelemetryStorage(database)
        try:
            assert storage.count_samples() == 0
        finally:
            storage.close()
    finally:
        ground.terminate()
        ground.communicate(timeout=12)


@pytest.mark.parametrize("identity", ["ground-expired", "ground-future"])
def test_flight_rejects_invalid_server_validity(tmp_path, certificates, tls_flight, identity):
    ground, database, port = start_tls_ground(tmp_path, certificates, identity)
    try:
        result = subprocess.run([str(tls_flight), "127.0.0.1", str(port), "--count", "4",
            "--interval-ms", "0", "--drain-ms", "500", "--ca", str(certificates / "ca.pem"),
            "--cert", str(certificates / "flight.pem"), "--key", str(certificates / "flight.key"),
            "--server-name", "ground.orbital.test", "--outbox", str(tmp_path/"outbox.db")], capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["acknowledged"] == 0
        storage = TelemetryStorage(database)
        try:
            assert storage.count_samples() == 0
        finally:
            storage.close()
    finally:
        ground.terminate()
        ground.communicate(timeout=12)


@pytest.mark.parametrize("arguments", [[], ["--ca", "/nonexistent/orbit-ca"]])
def test_flight_requires_credentials_before_start(tls_flight, arguments):
    result = subprocess.run([str(tls_flight), "127.0.0.1", "9", *arguments],
                            capture_output=True, text=True, timeout=3)
    assert result.returncode != 0
    assert not result.stdout


def test_ground_stalled_handshake_shutdown(tmp_path, certificates):
    ground, _database, port = start_tls_ground(tmp_path, certificates)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            time.sleep(.1)
            ground.terminate()
            ground.communicate(timeout=3)
            assert ground.returncode == 0
    finally:
        if ground.poll() is None:
            ground.kill()
            ground.communicate()


def test_flight_stalled_handshake_shutdown(tmp_path, certificates, tls_flight):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)
        process = subprocess.Popen([str(tls_flight), "127.0.0.1", str(listener.getsockname()[1]),
            "--ca", str(certificates / "ca.pem"), "--cert", str(certificates / "flight.pem"),
            "--key", str(certificates / "flight.key"), "--server-name", "ground.orbital.test", "--outbox", str(tmp_path/"outbox.db")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with listener.accept()[0]:
                process.terminate()
                stdout, stderr = process.communicate(timeout=3)
                assert process.returncode == 0, stderr
                assert json.loads(stdout)["acknowledged"] == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
