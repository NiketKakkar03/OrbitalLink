"""Real authenticated native peer tests for flight command effects and rejection."""
from __future__ import annotations

import tempfile
import json
import sys
import sqlite3
import time
from pathlib import Path
import socket
import ssl
import struct
import subprocess

import pytest

from ground.orbit_protocol import (COMMAND_RESULT, COMMAND_SESSION, TELEMETRY_SAMPLE,
    FrameParser, TelemetryAck, encode_control, encode_telemetry_ack)
from ground.test_security_e2e import certificates, tls_flight, ROOT


class FlightPeer:
    def __init__(self, certificates, executable, authorized=True, fault=False, identity="ground"):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(3)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(certificates / "ca.pem")
        context.load_cert_chain(certificates / f"{identity}.pem", certificates / f"{identity}.key")
        self.outbox_dir=tempfile.TemporaryDirectory()
        arguments = [str(executable), "127.0.0.1", str(self.listener.getsockname()[1]),
            "--interval-ms", "10", "--outbox", str(Path(self.outbox_dir.name)/"outbox.db"), "--ca", str(certificates / "ca.pem"),
            "--cert", str(certificates / "flight.pem"), "--key", str(certificates / "flight.key"),
            "--server-name", f"{identity}.orbital.test"]
        if fault:
            arguments += ["--fake-fault", "1"]
        if authorized:
            arguments += ["--authorized-command-server", "ground.orbital.test"]
        self.context = context
        self.process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.connection = context.wrap_socket(self.listener.accept()[0], server_side=True)
        self.connection.settimeout(3)
        self.parser = FrameParser()
        self.pending = []
        self.samples = []
        _, message = self.receive(COMMAND_SESSION)
        self.session, self.boot, self.now = struct.unpack("!QQQ", message.payload)

    def receive(self, kind):
        while True:
            if not self.pending:
                chunk = self.connection.recv(4096)
                assert chunk, "flight closed before expected frame"
                self.pending.extend(self.parser.feed(chunk))
            header, message = self.pending.pop(0)
            if header.message_type == TELEMETRY_SAMPLE:
                self.samples.append(message)
                self.connection.sendall(encode_telemetry_ack(TelemetryAck(
                    message.spacecraft_id, message.flight_boot_id, message.telemetry_sequence), header.message_sequence))
            if header.message_type == kind:
                return header, message

    def command(self, sequence, opcode, argument=0, session=None, deadline=None):
        self.connection.sendall(encode_control(4, struct.pack("!QQQQII",
            self.session if session is None else session, sequence, sequence,
            self.now + 4_000_000_000 if deadline is None else deadline, opcode, argument)))
        _, message = self.receive(COMMAND_RESULT)
        result = struct.unpack("!QQQII", message.payload)
        assert result[:3] == (self.session if session is None else session, sequence, sequence)
        assert result[4] == 0
        return result[3]

    def reconnect(self):
        self.connection.close()
        self.connection = self.context.wrap_socket(self.listener.accept()[0], server_side=True)
        self.connection.settimeout(3)
        self.parser = FrameParser()
        self.pending = []
        _, message = self.receive(COMMAND_SESSION)
        self.session, self.boot, self.now = struct.unpack("!QQQ", message.payload)

    def close(self):
        self.process.terminate()
        self.connection.close()
        self.listener.close()
        stdout, stderr = self.process.communicate(timeout=3)
        assert self.process.returncode == 0, stderr
        return json.loads(stdout)


@pytest.mark.parametrize("authorized", [True, False])
def test_authenticated_reset_effect_and_role_rejection(certificates, tls_flight, authorized):
    peer = FlightPeer(certificates, tls_flight, authorized)
    try:
        _, baseline = peer.receive(TELEMETRY_SAMPLE)
        assert peer.command(1, 4) == (0 if authorized else 1)
        for _ in range(4):
            _, sample = peer.receive(TELEMETRY_SAMPLE)
        assert sample.device_generation == baseline.device_generation + int(authorized)
    finally:
        peer.close()


def test_command_rejections_have_no_reset_effect(certificates, tls_flight):
    peer = FlightPeer(certificates, tls_flight)
    try:
        _, baseline = peer.receive(TELEMETRY_SAMPLE)
        assert peer.command(1, 4, session=peer.session ^ 1) == 2
        assert peer.command(1, 4, deadline=0) == 4
        assert peer.command(1, 4, argument=1) == 5
        assert peer.command(1, 4) == 0
        assert peer.command(1, 4) == 3
        for _ in range(4):
            _, sample = peer.receive(TELEMETRY_SAMPLE)
        assert sample.device_generation == baseline.device_generation + 1
    finally:
        summary = peer.close()
    assert summary["command_effects"] == 1


def test_enable_disable_period_and_clear_fault_effects(certificates, tls_flight):
    peer = FlightPeer(certificates, tls_flight, fault=True)
    try:
        _, baseline = peer.receive(TELEMETRY_SAMPLE)
        assert baseline.temperature_millideg_c == 200000
        assert peer.command(1, 5) == 0
        assert peer.command(2, 3, argument=50) == 0
        assert peer.command(3, 2) == 0
        assert peer.command(4, 1) == 0
        for _ in range(4):
            _, sample = peer.receive(TELEMETRY_SAMPLE)
        assert sample.temperature_millideg_c != 200000
    finally:
        summary = peer.close()
    assert summary["command_effects"] == 4
    assert summary["device_enabled"]
    assert summary["device_period_ms"] == 50
    assert not summary["device_fault"]


def test_local_operator_cli_persists_applied_reset(tmp_path, certificates, tls_flight):
    # Short protected parent also avoids the Unix-domain socket pathname limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="orbit-control-", dir="/tmp") as directory:
        control = Path(directory) / "control.sock"
        ready = Path(directory) / "ready"
        database = Path(directory) / "telemetry.db"
        ground = subprocess.Popen([sys.executable, "-m", "ground.orbit_ground",
            "--database", str(database), "--ready-file", str(ready), "--control-socket", str(control),
            "--ca", str(certificates / "ca.pem"), "--cert", str(certificates / "ground.pem"),
            "--key", str(certificates / "ground.key"), "--allowed-client", "flight.orbital.test"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        flight = None
        try:
            deadline = time.monotonic()+5
            while not ready.exists():
                assert ground.poll() is None, ground.communicate()[1]
                assert time.monotonic()<deadline
                time.sleep(.02)
            flight = subprocess.Popen([str(tls_flight), "127.0.0.1", ready.read_text().strip(),
                "--interval-ms", "10", "--outbox", str(Path(directory)/"outbox.db"), "--ca", str(certificates / "ca.pem"),
                "--cert", str(certificates / "flight.pem"), "--key", str(certificates / "flight.key"),
                "--server-name", "ground.orbital.test", "--authorized-command-server", "ground.orbital.test"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # The first committed telemetry proves TLS session registration completed.
            with sqlite3.connect(database) as connection:
                while not connection.execute("SELECT count(*) FROM telemetry_samples").fetchone()[0]:
                    assert time.monotonic()<deadline
                    time.sleep(.02)
            result = subprocess.run([sys.executable, "-m", "ground.orbit_commands", "--socket", str(control), "reset"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=4)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["status"] == "applied"
            query=subprocess.run([sys.executable,"-m","ground.orbit_commands","--socket",str(control),"query-state"],cwd=ROOT,capture_output=True,text=True,timeout=4)
            assert query.returncode==0,query.stderr
            snapshot=json.loads(query.stdout)
            assert snapshot["status"]=="applied"
            assert snapshot["state"]["generation"]==8 and snapshot["state"]["effects"]==1
            assert snapshot["state"]["enabled"] and snapshot["state"]["period_ms"]==10
            with sqlite3.connect(database) as connection:
                while not connection.execute("SELECT count(*) FROM command_results").fetchone()[0]:
                    assert time.monotonic()<deadline
                    time.sleep(.02)
        finally:
            if flight:
                flight.terminate()
                stdout, stderr = flight.communicate(timeout=3)
                assert flight.returncode == 0, stderr
                assert json.loads(stdout)["device_generation"] == 8
            ground.terminate()
            ground.communicate(timeout=3)


def test_lost_result_is_not_replayed_and_old_session_rejected(certificates, tls_flight):
    peer = FlightPeer(certificates, tls_flight)
    try:
        peer.receive(TELEMETRY_SAMPLE)
        old = peer.session
        peer.connection.sendall(encode_control(4, struct.pack("!QQQQII", old, 1, 1,
            peer.now+4_000_000_000, 4, 0)))
        # Deliberately lose the completion; execution may or may not have happened.
        peer.reconnect()
        assert peer.session != old
        assert peer.command(1, 4, session=old) == 2
        for _ in range(4):
            peer.receive(TELEMETRY_SAMPLE)
    finally:
        summary = peer.close()
    assert summary["command_effects"] in (0, 1)
    assert summary["device_generation"] == 7+summary["command_effects"]


def test_authenticated_observer_san_cannot_command(certificates, tls_flight):
    peer = FlightPeer(certificates, tls_flight, identity="observer")
    try:
        peer.receive(TELEMETRY_SAMPLE)
        assert peer.command(1, 4) == 1
        for _ in range(3):
            _, sample = peer.receive(TELEMETRY_SAMPLE)
        assert sample.device_generation == 7
    finally:
        summary = peer.close()
    assert summary["command_effects"] == 0


def test_service_restart_rejects_old_session(certificates, tls_flight):
    original = FlightPeer(certificates, tls_flight)
    try:
        old_session, old_boot = original.session, original.boot
        original.receive(TELEMETRY_SAMPLE)
    finally:
        original.close()
    restarted = FlightPeer(certificates, tls_flight)
    try:
        assert restarted.session != old_session
        assert restarted.boot != old_boot
        assert restarted.command(1, 4, session=old_session) == 2
        assert restarted.command(1, 4) == 0
        for _ in range(4):
            _, sample = restarted.receive(TELEMETRY_SAMPLE)
        assert sample.device_generation == 8
    finally:
        summary = restarted.close()
    assert summary["command_effects"] == 1

def test_read_only_query_snapshot_after_command(certificates, tls_flight):
    from ground.orbit_protocol import STATE_SNAPSHOT
    peer=FlightPeer(certificates,tls_flight)
    try:
        assert peer.command(1,2)==0
        peer.connection.sendall(encode_control(4,struct.pack('!QQQQII',peer.session,2,2,peer.now+4_000_000_000,6,0)))
        _,message=peer.receive(STATE_SNAPSHOT)
        session,sequence,identifier,status,reserved,enabled,period,generation,fault,effects,now=struct.unpack('!QQQIIIIIIQQ',message.payload)
        assert (session,sequence,identifier,status,reserved)==(peer.session,2,2,0,0)
        assert enabled==0 and effects==1 and period==10 and generation==7 and fault==0 and now>=peer.now
    finally:
        peer.close()
