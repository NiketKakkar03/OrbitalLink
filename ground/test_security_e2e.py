from __future__ import annotations

import json
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
    prefix = Path("/opt/homebrew/opt/openssl@3")
    flags = [f"-I{prefix}/include", f"-L{prefix}/lib"] if prefix.exists() else []
    subprocess.run(["c++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread",
                    *flags, "flight/flight_service.cpp", "-o", str(output), "-lssl", "-lcrypto"],
                   cwd=ROOT, check=True)
    return output


def start_tls_ground(tmp_path, certificates):
    ready = tmp_path / "ready"
    database = tmp_path / "telemetry.db"
    process = subprocess.Popen([sys.executable, "-m", "ground.orbit_ground",
        "--database", str(database), "--ready-file", str(ready),
        "--ca", str(certificates / "ca.pem"), "--cert", str(certificates / "ground.pem"),
        "--key", str(certificates / "ground.key"), "--allowed-client", "flight.orbital.test"],
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
            "--server-name", server_name], capture_output=True, text=True, timeout=5)
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
                with pytest.raises((ssl.SSLError, ConnectionResetError)):
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
