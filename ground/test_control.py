import json
import os
import socket
import stat
import sys
import threading
from pathlib import Path

import pytest

from ground.orbit_control import (
    CommandControl,
    NoActiveSessionError,
    QueueFullError,
    UnixControlServer,
    control_request,
)
from ground.orbit_ground import (
    GroundReceiver,
    certificate_common_names,
    is_authorized_certificate,
    main,
)
from ground.orbit_protocol import (
    COMMAND_RESET,
    COMMAND_SET_PERIOD,
    CommandRequest,
    CommandResult,
    CommandSession,
    FrameParser,
    encode_command_result,
    encode_command_session,
)
from ground.orbit_storage import TelemetryStorage


def test_pending_queue_is_bounded_and_materialized_for_active_session(tmp_path):
    storage = TelemetryStorage(tmp_path / "control.db")
    control = CommandControl(storage, capacity=2)
    first_id = control.submit("set-period", argument=250, validity_ms=700)
    control.submit("reset")

    with pytest.raises(QueueFullError, match="full"):
        control.submit("enable")
    with pytest.raises(NoActiveSessionError):
        control.next_request(flight_monotonic_ms=1_000)

    control.activate_session(91)
    request = control.next_request(flight_monotonic_ms=1_000)
    assert request.command_id == first_id
    assert request.command_session_id == 91
    assert request.command_sequence == 1
    assert request.command_type == COMMAND_SET_PERIOD
    assert request.deadline_monotonic_ms == 1_700
    assert request.argument == 250
    assert storage.load_command(first_id).state == "sent"
    storage.close()


def test_result_correlation_persistence_and_disconnect_uncertainty(tmp_path):
    storage = TelemetryStorage(tmp_path / "control.db")
    control = CommandControl(storage)
    reset_id = control.submit("reset")
    queued_id = control.submit("disable")
    control.activate_session(55)
    request = control.next_request(flight_monotonic_ms=100)
    assert request.command_type == COMMAND_RESET

    wrong = CommandResult(55, request.command_sequence + 1, reset_id, COMMAND_RESET, 0, 0, 1, 2, 100)
    with pytest.raises(ValueError, match="does not match"):
        control.accept_result(wrong)

    result = CommandResult(55, request.command_sequence, reset_id, COMMAND_RESET, 0, 0, 1, 2, 100)
    assert control.accept_result(result) is True
    assert control.accept_result(result) is False
    assert storage.load_command(reset_id).state == "result"

    second = control.next_request(flight_monotonic_ms=200)
    assert second.command_id == queued_id
    assert control.deactivate_session() == 1
    assert storage.load_command(queued_id).state == "outcome_unknown"
    assert control.state()["active_session_id"] is None
    storage.close()


def test_restart_restores_queued_commands_and_never_replays_sent_commands(tmp_path):
    database_path = tmp_path / "control.db"
    storage = TelemetryStorage(database_path)
    control = CommandControl(storage)
    sent_id = control.submit("reset")
    queued_id = control.submit("enable")
    control.activate_session(3)
    assert control.next_request(flight_monotonic_ms=1).command_id == sent_id
    storage.close()

    reopened = TelemetryStorage(database_path)
    recovered = CommandControl(reopened)
    assert reopened.load_command(sent_id).state == "outcome_unknown"
    recovered.activate_session(4)
    assert recovered.next_request(flight_monotonic_ms=2).command_id == queued_id
    assert recovered.next_request(flight_monotonic_ms=2) is None
    reopened.close()


def test_unix_control_socket_permissions_submission_and_visibility(tmp_path):
    storage = TelemetryStorage(tmp_path / "control.db")
    control = CommandControl(storage, capacity=1)
    socket_path = Path(f"/private/tmp/orbit-control-{os.getpid()}.sock")
    stop_event = threading.Event()
    ready_event = threading.Event()
    server = UnixControlServer(socket_path, control)
    thread = threading.Thread(target=server.serve, args=(stop_event,), kwargs={"ready_event": ready_event})
    thread.start()
    assert ready_event.wait(2)
    try:
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        submitted = control_request(socket_path, {"operation": "submit", "command": "reset"})
        assert submitted["ok"] is True
        command_id = submitted["command_id"]

        state = control_request(socket_path, {"operation": "state"})
        assert state["pending_commands"] == 1
        status_response = control_request(
            socket_path, {"operation": "status", "command_id": command_id}
        )
        assert status_response["command"]["state"] == "queued"
        full = control_request(socket_path, {"operation": "submit", "command": "enable"})
        assert full == {"ok": False, "error": "pending command queue is full"}
        assert control_request(socket_path, {"operation": "results"}) == {
            "ok": True,
            "results": [],
        }
    finally:
        stop_event.set()
        thread.join(2)
        storage.close()
    assert not socket_path.exists()


def test_certificate_authorization_uses_only_exact_common_name():
    misleading = {
        "subject": (
            (("commonName", "unauthorized-client"),),
            (("organizationName", "orbital-ground"),),
        )
    }
    authorized = {"subject": ((("commonName", "orbital-ground"),),)}

    assert certificate_common_names(misleading) == {"unauthorized-client"}
    assert is_authorized_certificate(misleading, {"orbital-ground"}) is False
    assert is_authorized_certificate(authorized, {"orbital-ground"}) is True
    assert is_authorized_certificate(authorized, set()) is False


def test_ground_receiver_accepts_and_persists_correlated_command_result(tmp_path):
    storage = TelemetryStorage(tmp_path / "control.db")
    control = CommandControl(storage)
    command_id = control.submit("reset")
    control.activate_session(72)
    request = control.next_request(flight_monotonic_ms=10)
    result = CommandResult(72, request.command_sequence, command_id, COMMAND_RESET, 0, 0, 1, 3, 100)
    receiver = GroundReceiver(storage, control)
    client, server = socket.socketpair()
    try:
        client.sendall(encode_command_result(result))
        client.shutdown(socket.SHUT_WR)
        receiver.handle_connection(server)
    finally:
        client.close()
        server.close()

    assert storage.load_command(command_id).state == "result"
    assert storage.load_command_results()[0].result == result
    storage.close()


def test_operator_cli_submits_without_opening_database(tmp_path, monkeypatch, capsys):
    storage = TelemetryStorage(tmp_path / "control.db")
    control = CommandControl(storage)
    socket_path = Path(f"/private/tmp/orbit-cli-{os.getpid()}.sock")
    stop_event = threading.Event()
    ready_event = threading.Event()
    server = UnixControlServer(socket_path, control)
    thread = threading.Thread(target=server.serve, args=(stop_event,), kwargs={"ready_event": ready_event})
    thread.start()
    assert ready_event.wait(2)
    try:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "orbit_ground", "--control-socket", str(socket_path),
                "--submit-command", "set-period", "--argument", "400",
            ],
        )
        assert main() == 0
        response = json.loads(capsys.readouterr().out)
        assert response["ok"] is True
        assert storage.load_command(response["command_id"]).argument == 400
    finally:
        stop_event.set()
        thread.join(2)
        storage.close()


def test_session_frame_dispatches_requests_up_to_advertised_capacity(tmp_path):
    storage = TelemetryStorage(tmp_path / "control.db")
    control = CommandControl(storage, capacity=3)
    first_id = control.submit("enable")
    second_id = control.submit("reset")
    third_id = control.submit("disable")
    receiver = GroundReceiver(storage, control)
    client, server = socket.socketpair()
    thread = threading.Thread(target=receiver.handle_connection, args=(server,))
    thread.start()
    try:
        session = CommandSession(
            command_session_id=99,
            flight_boot_id=12,
            flight_monotonic_ms=1_000,
            authorized_role=7,
            command_queue_capacity=2,
        )
        client.sendall(encode_command_session(session))
        parser = FrameParser()
        messages = []
        client.settimeout(2)
        while len(messages) < 2:
            messages.extend(message for _, message in parser.feed(client.recv(4096)))

        assert all(isinstance(message, CommandRequest) for message in messages)
        assert [message.command_id for message in messages] == [first_id, second_id]
        assert [message.command_sequence for message in messages] == [1, 2]
        assert all(message.command_session_id == 99 for message in messages)
        assert all(message.role == 7 for message in messages)
        assert control.state()["inflight_commands"] == 2
        assert storage.load_command(third_id).state == "queued"

        first = messages[0]
        client.sendall(
            encode_command_result(
                CommandResult(99, first.command_sequence, first.command_id, first.command_type, 0, 0, 1, 1, 100)
            )
        )
        replacement = None
        while replacement is None:
            for _, message in parser.feed(client.recv(4096)):
                if isinstance(message, CommandRequest):
                    replacement = message
        assert replacement.command_id == third_id
        assert replacement.command_sequence == 3
    finally:
        client.shutdown(socket.SHUT_WR)
        thread.join(2)
        client.close()
        server.close()
        storage.close()
    assert not thread.is_alive()
