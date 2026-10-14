"""Operator CLI must never turn ambiguous transport into a silent replay."""
import json
import sys

import pytest

from ground import orbit_commands


class FragmentedSocket:
    def __init__(self, chunks, *, connect_error=False, send_error=False):
        self.chunks = iter(chunks)
        self.connect_error = connect_error
        self.send_error = send_error
        self.sent = []
        self.connects = 0

    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def settimeout(self, _timeout): pass

    def connect(self, _path):
        self.connects += 1
        if self.connect_error:
            raise ConnectionRefusedError()

    def sendall(self, data):
        self.sent.append(data)
        if self.send_error:
            raise BrokenPipeError()

    def recv(self, _size): return next(self.chunks, b'')


def use_socket(monkeypatch, chunks=(), **kwargs):
    fake = FragmentedSocket(chunks, **kwargs)
    monkeypatch.setattr(orbit_commands.socket, 'socket', lambda *_args: fake)
    return fake


def test_fragmented_newline_response_is_assembled_once(monkeypatch):
    fake = use_socket(monkeypatch, [b'{"sta', b'tus":"applied",', b'"id":42}', b'\n'])
    assert orbit_commands.submit_local_command('operator.sock', 'reset') == {'status': 'applied', 'id': 42}
    assert fake.connects == 1
    assert len(fake.sent) == 1
    assert json.loads(fake.sent[0])['command'] == 'reset'


@pytest.mark.parametrize('chunks', [
    [b'{"status":"applied"}'],  # Complete JSON without framing is incomplete.
    [b'invalid\n'],
    [b'{"status":"invented"}\n'],
    [b'[]\n'],
    [b'\xff\n'],
    [b'x' * 4097],
    [],
])
def test_incomplete_or_malformed_response_is_unknown_without_replay(monkeypatch, chunks):
    fake = use_socket(monkeypatch, chunks)
    assert orbit_commands.submit_local_command('operator.sock', 'reset')['status'] == 'outcome_unknown'
    assert len(fake.sent) == 1
    assert fake.connects == 1


def test_connection_failure_before_send_is_unavailable(monkeypatch):
    fake = use_socket(monkeypatch, connect_error=True)
    assert orbit_commands.submit_local_command('operator.sock', 'reset')['status'] == 'unavailable'
    assert not fake.sent
    assert fake.connects == 1


def test_partial_send_failure_is_unknown_without_retry(monkeypatch):
    fake = use_socket(monkeypatch, send_error=True)
    assert orbit_commands.submit_local_command('operator.sock', 'reset')['status'] == 'outcome_unknown'
    assert len(fake.sent) == 1


def test_response_deadline_reports_unknown(monkeypatch):
    fake = use_socket(monkeypatch)
    times = iter([0, 4])
    monkeypatch.setattr(orbit_commands.time, 'monotonic', lambda: next(times))
    assert orbit_commands.submit_local_command('operator.sock', 'reset')['status'] == 'outcome_unknown'
    assert len(fake.sent) == 1


@pytest.mark.parametrize('status,expected_exit', [
    ('applied', 0), ('unauthorized', 1), ('expired', 1), ('outcome_unknown', 1), ('unavailable', 1),
])
def test_cli_exit_code_and_json_output(monkeypatch, capsys, status, expected_exit):
    monkeypatch.setattr(sys, 'argv', ['orbit_commands', '--socket', 'operator.sock', 'reset'])
    monkeypatch.setattr(orbit_commands, 'submit_local_command', lambda *_args: {'status': status})
    assert orbit_commands.main() == expected_exit
    assert json.loads(capsys.readouterr().out) == {'status': status}
