"""Operator queue, session and local socket regressions independent of TLS."""
from __future__ import annotations

import json
import socket
import stat
import struct
import threading
import time
import tempfile
from pathlib import Path

import pytest

from ground import orbit_commands as commands
from ground.orbit_protocol import decode_frame


def wait_for(predicate, seconds=1):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('condition did not become true before deadline')
        time.sleep(.005)


def pending_submit(broker, **kwargs):
    results = []
    worker = threading.Thread(target=lambda: results.append(broker.submit(3, 250, **kwargs)))
    worker.start()
    wait_for(lambda: bool(broker.waiters))
    return worker, results


def request_payload(frame):
    _, message = decode_frame(frame)
    return struct.unpack('!QQQQII', message.payload)


def test_timeout_discards_unsent_requests_and_queue_recovers():
    broker = commands.CommandBroker()
    broker.open(42, 7, 1000)
    statuses = [broker.submit(1, timeout=0)['status'] for _ in range(40)]
    assert set(statuses) <= {'outcome_unknown', 'queue_full'}
    assert broker.next_frame() is None
    worker, results = pending_submit(broker)
    session, sequence, identifier, _, _, _ = request_payload(broker.next_frame())
    broker.result(struct.pack('!QQQII', session, sequence, identifier, 0, 0))
    worker.join(1)
    assert not worker.is_alive()
    assert results[0]['status'] == 'applied'
    assert broker.next_frame() is None


def test_live_waiter_limit_and_disconnect_release_every_waiter():
    broker = commands.CommandBroker()
    broker.open(42, 7, 1000)
    results = []
    workers = []
    try:
        for count in range(16):
            worker = threading.Thread(target=lambda: results.append(broker.submit(1, timeout=5)))
            worker.start()
            workers.append(worker)
            wait_for(lambda: len(broker.waiters) == count + 1)
        assert broker.submit(1, timeout=0)['status'] == 'queue_full'
    finally:
        broker.close()
        for worker in workers:
            worker.join(1)
    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 16
    assert {result['status'] for result in results} == {'outcome_unknown'}
    assert broker.next_frame() is None


def test_deadline_mapping_and_reset_invalidates_old_requests(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(commands.time, 'monotonic_ns', lambda: clock[0])
    broker = commands.CommandBroker()
    broker.open(42, 7, 9000)
    clock[0] = 4000
    worker, results = pending_submit(broker, ttl_ms=250)
    payload = request_payload(broker.next_frame())
    assert payload[0] == 42
    assert payload[1] == 1
    assert payload[3] == 9000 + 3000 + 250_000_000
    broker.open(43, 8, 15000)
    worker.join(1)
    assert results[0]['status'] == 'outcome_unknown'
    assert broker.next_frame() is None
    worker2, results2 = pending_submit(broker)
    second = request_payload(broker.next_frame())
    assert second[:2] == (43, 1)
    broker.result(struct.pack('!QQQII', *payload[:3], 0, 0))
    assert not results2
    broker.result(struct.pack('!QQQII', *second[:3], 0, 0))
    worker2.join(1)
    assert results2[0]['status'] == 'applied'


@pytest.mark.parametrize('session,boot,clock', [(0,7,1), (42,-1,1), (42,7,-1), (42,7,1<<64)])
def test_invalid_session_announcement_is_rejected(session, boot, clock):
    broker = commands.CommandBroker()
    with pytest.raises(ValueError):
        broker.open(session, boot, clock)
    assert broker.session is None


def test_deadline_overflow_does_not_break_submit(monkeypatch):
    monkeypatch.setattr(commands.time, 'monotonic_ns', lambda: 0)
    broker = commands.CommandBroker()
    broker.open(42, 7, (1<<64)-1)
    assert broker.submit(1, timeout=0)['status'] in {'invalid_argument', 'unavailable'}
    assert not broker.waiters
    assert broker.next_frame() is None


@pytest.mark.parametrize('status,reserved', [(8,0), (0,1)])
def test_malformed_result_does_not_resolve_pending_request(status, reserved):
    broker = commands.CommandBroker()
    broker.open(42, 7, 1000)
    worker, results = pending_submit(broker)
    payload = request_payload(broker.next_frame())
    with pytest.raises(ValueError):
        broker.result(struct.pack('!QQQII', *payload[:3], status, reserved))
    assert not results
    broker.close()
    worker.join(1)
    assert results[0]['status'] == 'outcome_unknown'


def test_result_requires_exact_payload_length():
    with pytest.raises(ValueError):
        commands.CommandBroker().result(b'bad')


@pytest.fixture
def socket_dir():
    # macOS AF_UNIX paths are limited to ~104 bytes; pytest's full tmp path
    # exceeds that limit even before appending the socket name.
    with tempfile.TemporaryDirectory(prefix='orb-', dir='/tmp') as directory:
        yield Path(directory)


def test_control_socket_permissions_and_existing_path_protection(socket_dir: Path):
    stop = threading.Event()
    path = socket_dir / 'control.sock'
    control = commands.ControlSocket(path, commands.CommandBroker(), stop)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(OSError):
            commands.ControlSocket(path, commands.CommandBroker(), stop)
        assert stat.S_ISSOCK(path.stat().st_mode)
    finally:
        stop.set()
        control.close()


def test_control_cleanup_preserves_replacement_file(socket_dir: Path):
    stop = threading.Event()
    path = socket_dir / 'control.sock'
    control = commands.ControlSocket(path, commands.CommandBroker(), stop)
    path.unlink()
    path.write_text('operator replacement')
    stop.set()
    control.close()
    assert path.read_text() == 'operator replacement'


def test_partial_local_client_shutdown_is_bounded(socket_dir: Path):
    stop = threading.Event()
    path = socket_dir / 'control.sock'
    control = commands.ControlSocket(path, commands.CommandBroker(), stop)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))
        client.sendall(b'{"command":')
        time.sleep(.05)
        started = time.monotonic()
        stop.set()
        control.close()
        assert time.monotonic() - started < 1
        assert not control.thread.is_alive()


def test_malformed_local_request_does_not_kill_control_thread(socket_dir: Path):
    stop = threading.Event()
    path = socket_dir / 'control.sock'
    control = commands.ControlSocket(path, commands.CommandBroker(), stop)
    try:
        for request in [[], {'command': []}, {'command': 'enable', 'argument': 'bad'}]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(path))
                client.sendall((json.dumps(request)+'\n').encode())
                assert json.loads(client.recv(4096))['status'] == 'invalid_argument'
        assert control.thread.is_alive()
    finally:
        stop.set()
        control.close()


def test_control_socket_refuses_shared_parent(socket_dir: Path):
    socket_dir.chmod(0o755)
    with pytest.raises(ValueError):
        commands.ControlSocket(socket_dir / 'control.sock', commands.CommandBroker(), threading.Event())
    assert not (socket_dir / 'control.sock').exists()
