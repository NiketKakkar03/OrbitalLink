"""Local operator control plane for durable M4 command workflows."""

from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import threading
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ground.orbit_protocol import (
    COMMAND_CLEAR_FAULT,
    COMMAND_DISABLE,
    COMMAND_ENABLE,
    COMMAND_RESET,
    COMMAND_SET_PERIOD,
    CommandRequest,
    CommandResult,
)
from ground.orbit_storage import TelemetryStorage


COMMAND_TYPES = {
    "enable": COMMAND_ENABLE,
    "disable": COMMAND_DISABLE,
    "set-period": COMMAND_SET_PERIOD,
    "reset": COMMAND_RESET,
    "clear-fault": COMMAND_CLEAR_FAULT,
}


class QueueFullError(ValueError):
    pass


class NoActiveSessionError(ValueError):
    pass


class CommandControl:
    """Own pending operator intents and bind them to an authenticated session."""

    def __init__(self, storage: TelemetryStorage, *, capacity: int = 32):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._storage = storage
        self._capacity = capacity
        self._storage.mark_all_sent_commands_uncertain()
        queued = [item.command_id for item in self._storage.load_commands() if item.state == "queued"]
        if len(queued) > capacity:
            raise ValueError("durable pending commands exceed configured capacity")
        self._pending: deque[int] = deque(queued)
        self._lock = threading.Lock()
        self._session_id: int | None = None
        self._next_sequence = 1
        self._authorized_role = 0
        self._remote_capacity = 0
        self._inflight = 0

    def submit(self, command_name: str, *, argument: int = 0, validity_ms: int = 5_000) -> int:
        if command_name not in COMMAND_TYPES:
            raise ValueError(f"unknown command: {command_name}")
        if not isinstance(argument, int) or isinstance(argument, bool) or not 0 <= argument < 1 << 64:
            raise ValueError("argument must be an unsigned 64-bit integer")
        if not isinstance(validity_ms, int) or isinstance(validity_ms, bool) or not 1 <= validity_ms <= (1 << 32) - 1:
            raise ValueError("validity_ms must be a positive 32-bit integer")
        if command_name != "set-period" and argument != 0:
            raise ValueError(f"{command_name} does not accept an argument")
        if command_name == "set-period" and argument == 0:
            raise ValueError("set-period requires a positive period")

        with self._lock:
            if len(self._pending) >= self._capacity:
                raise QueueFullError("pending command queue is full")
            command_id = self._new_command_id()
            self._storage.create_command(
                command_id=command_id,
                command_type=COMMAND_TYPES[command_name],
                argument=argument,
                validity_ms=validity_ms,
            )
            self._pending.append(command_id)
            return command_id

    def activate_session(
        self, session_id: int, *, authorized_role: int = 1, remote_capacity: int = 1
    ) -> None:
        if not isinstance(session_id, int) or isinstance(session_id, bool) or not 0 <= session_id < 1 << 64:
            raise ValueError("session_id must be an unsigned 64-bit integer")
        if not isinstance(authorized_role, int) or not 0 <= authorized_role <= (1 << 32) - 1:
            raise ValueError("authorized_role must be an unsigned 32-bit integer")
        if not isinstance(remote_capacity, int) or not 1 <= remote_capacity <= (1 << 32) - 1:
            raise ValueError("remote_capacity must be a positive 32-bit integer")
        with self._lock:
            if self._session_id == session_id:
                return
            if self._session_id is not None and self._session_id != session_id:
                self._storage.mark_session_uncertain(self._session_id)
            self._session_id = session_id
            self._next_sequence = 1
            self._authorized_role = authorized_role
            self._remote_capacity = remote_capacity
            self._inflight = 0

    def deactivate_session(self) -> int:
        with self._lock:
            if self._session_id is None:
                return 0
            changed = self._storage.mark_session_uncertain(self._session_id)
            self._session_id = None
            self._authorized_role = 0
            self._remote_capacity = 0
            self._inflight = 0
            return changed

    def next_request(self, *, flight_monotonic_ms: int) -> CommandRequest | None:
        if not isinstance(flight_monotonic_ms, int) or flight_monotonic_ms < 0:
            raise ValueError("flight_monotonic_ms must be non-negative")
        with self._lock:
            if not self._pending:
                return None
            if self._session_id is None:
                raise NoActiveSessionError("no authenticated command session is active")
            if self._authorized_role == 0:
                return None
            if self._inflight >= self._remote_capacity:
                return None
            command_id = self._pending.popleft()
            stored = self._storage.load_command(command_id)
            if stored is None:
                raise RuntimeError("pending command is missing from durable storage")
            sequence = self._next_sequence
            self._next_sequence += 1
            deadline = flight_monotonic_ms + stored.validity_ms
            if deadline >= 1 << 64:
                raise ValueError("command deadline exceeds protocol range")
            self._storage.mark_command_sent(command_id, self._session_id, sequence)
            self._inflight += 1
            return CommandRequest(
                command_session_id=self._session_id,
                command_sequence=sequence,
                command_id=command_id,
                command_type=stored.command_type,
                deadline_monotonic_ms=deadline,
                argument=stored.argument,
                role=self._authorized_role,
            )

    def accept_result(self, result: CommandResult) -> bool:
        with self._lock:
            if self._session_id is None or result.command_session_id != self._session_id:
                raise ValueError("command result does not belong to the active session")
            command = self._storage.load_command(result.command_id)
            if command is None:
                raise ValueError("command result has no matching local request")
            if (
                command.command_session_id != result.command_session_id
                or command.command_sequence != result.command_sequence
                or command.command_type != result.command_type
            ):
                raise ValueError("command result does not match the sent request")
            inserted = self._storage.store_command_result(result)
            if inserted:
                self._inflight -= 1
            return inserted

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_session_id": self._session_id,
                "pending_commands": len(self._pending),
                "capacity": self._capacity,
                "remote_capacity": self._remote_capacity,
                "inflight_commands": self._inflight,
            }

    def _new_command_id(self) -> int:
        while True:
            command_id = secrets.randbits(64)
            if self._storage.load_command(command_id) is None:
                return command_id


class UnixControlServer:
    """Serve one-line JSON requests over a same-user, mode-0600 Unix socket."""

    def __init__(self, path: str | Path, control: CommandControl):
        self.path = Path(path)
        self.control = control
        self._listener: socket.socket | None = None

    def serve(self, stop_event: threading.Event, *, ready_event: threading.Event | None = None) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            if not stat.S_ISSOCK(self.path.stat().st_mode):
                raise FileExistsError(f"refusing to replace non-socket path: {self.path}")
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener = listener
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen()
            listener.settimeout(0.1)
            if ready_event is not None:
                ready_event.set()
            while not stop_event.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(1)
                    try:
                        response = self._handle(self._read_request(connection))
                    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
                        response = {"ok": False, "error": str(error)}
                    connection.sendall(json.dumps(response, sort_keys=True).encode() + b"\n")
        finally:
            listener.close()
            self._listener = None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_request(connection: socket.socket) -> dict[str, Any]:
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 16 * 1024:
                raise ValueError("control request is too large")
        value = json.loads(bytes(data).split(b"\n", 1)[0])
        if not isinstance(value, dict):
            raise ValueError("control request must be a JSON object")
        return value

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            operation = request.get("operation")
            if operation == "submit":
                command_id = self.control.submit(
                    request.get("command", ""),
                    argument=request.get("argument", 0),
                    validity_ms=request.get("validity_ms", 5_000),
                )
                return {"ok": True, "command_id": command_id, "state": "queued"}
            if operation == "state":
                return {"ok": True, **self.control.state()}
            if operation == "status":
                command = self.control._storage.load_command(int(request["command_id"]))
                return {"ok": True, "command": None if command is None else asdict(command)}
            if operation == "results":
                results = self.control._storage.load_command_results(limit=request.get("limit"))
                return {"ok": True, "results": [asdict(item) for item in results]}
            raise ValueError("unknown control operation")
        except (KeyError, TypeError, ValueError) as error:
            return {"ok": False, "error": str(error)}


def control_request(path: str | Path, request: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))
        client.sendall(json.dumps(request).encode() + b"\n")
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
    value = json.loads(bytes(response).split(b"\n", 1)[0])
    if not isinstance(value, dict):
        raise ValueError("control response must be a JSON object")
    return value
