"""Bounded local operator requests; ambiguous completion is never retried."""
from __future__ import annotations
import argparse
import json
import os
import stat
from pathlib import Path
import queue
import secrets
import socket
import threading
import time

from ground.orbit_protocol import COMMAND_REQUEST, encode_control
import struct

STATUS_NAMES = ('applied', 'unauthorized', 'wrong_session', 'replay', 'expired', 'invalid_argument', 'queue_full', 'outcome_unknown')
OPCODES = {'enable': 1, 'disable': 2, 'set-period': 3, 'reset': 4, 'clear-fault': 5}

class CommandBroker:
    def __init__(self):
        self.lock = threading.Lock()
        self.session = None
        self.sequence = 0
        self.pending = queue.Queue(maxsize=16)
        self.waiters = {}

    def open(self, session, boot, flight_now):
        if not all(type(value) is int and 0 <= value < 2**64 for value in (session,boot,flight_now)) or not session:
            raise ValueError('invalid session announcement')
        self.close()
        with self.lock:
            self.session = (session, boot, flight_now, time.monotonic_ns())
            self.sequence = 0

    def close(self):
        with self.lock:
            self.session = None
            for entry in self.waiters.values():
                entry['result'] = {'status': 'outcome_unknown', 'id': entry['id']}
                entry['event'].set()
            self.waiters.clear()
            while not self.pending.empty():
                self.pending.get_nowait()

    def submit(self, opcode, argument=0, ttl_ms=1000, timeout=2):
        if not all(type(value) is int for value in (opcode,argument,ttl_ms)) or opcode not in range(1,6) or (opcode == 3 and not 1 <= argument <= 60000) or (opcode != 3 and argument != 0) or not 1 <= ttl_ms <= 5000:
            return {'status': 'invalid_argument'}
        with self.lock:
            if not self.session:
                return {'status': 'unavailable'}
            if len(self.waiters) >= 16:
                return {'status': 'queue_full'}
            session, boot, flight_now, local_now = self.session
            if self.sequence >= 2**64-1:
                return {'status':'unavailable'}
            self.sequence += 1
            identifier = secrets.randbits(64)
            deadline = flight_now + time.monotonic_ns() - local_now + ttl_ms * 1000000
            if deadline >= 2**64:
                return {'status':'invalid_argument'}
            entry = {'id': identifier, 'sequence': self.sequence, 'session': session, 'event': threading.Event(), 'result': None}
            self.waiters[identifier] = entry
            try:
                self.pending.put_nowait((identifier, encode_control(COMMAND_REQUEST, struct.pack('!QQQQII', session, self.sequence, identifier, deadline, opcode, argument), self.sequence)))
            except queue.Full:
                self.waiters.pop(identifier,None)
                return {'status':'queue_full'}
        if not entry['event'].wait(timeout):
            # Do not replay; a timeout may occur after the device effect.
            with self.lock:
                self.waiters.pop(identifier, None)
            return {'status': 'outcome_unknown', 'id': identifier}
        return entry['result']

    def next_frame(self):
        with self.lock:
            while True:
                try: identifier,frame=self.pending.get_nowait()
                except queue.Empty: return None
                if identifier in self.waiters: return frame

    def result(self, payload):
        if len(payload)!=32: raise ValueError('invalid command result length')
        session, sequence, identifier, status, reserved = struct.unpack('!QQQII', payload)
        if reserved or status >= len(STATUS_NAMES):
            raise ValueError('invalid command result')
        with self.lock:
            entry = self.waiters.get(identifier)
            if entry and (session, sequence) == (entry['session'], entry['sequence']):
                self.waiters.pop(identifier)
                entry['result'] = {'status': STATUS_NAMES[status], 'id': identifier, 'sequence': sequence}
                entry['event'].set()

class ControlSocket:
    def __init__(self, path, broker, stop):
        self.path=Path(path); self.broker=broker; self.stop=stop
        parent=self.path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError('control socket requires a parent directory owned by the current user with mode 0700')
        self.active=None
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        # Refuse existing paths; never unlink an operator's unrelated socket/file.
        self.listener.bind(str(self.path)); os.chmod(self.path,0o600)
        self.identity=self.path.lstat().st_ino
        self.listener.listen(16); self.listener.settimeout(.2)
        self.thread=threading.Thread(target=self.run,daemon=True); self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try: client,_=self.listener.accept()
            except (TimeoutError,OSError): continue
            self.active=client
            with client:
                client.settimeout(.2)
                try:
                    data=b''
                    deadline=time.monotonic()+2
                    while b'\n' not in data and len(data)<=4096 and not self.stop.is_set():
                        try: chunk=client.recv(4096-len(data)+1)
                        except TimeoutError:
                            if time.monotonic() >= deadline: raise ValueError('request timeout')
                            continue
                        if not chunk: break
                        data+=chunk
                    if len(data)>4096 or b'\n' not in data: raise ValueError('invalid request')
                    request=json.loads(data.split(b'\n')[0])
                    opcode=OPCODES[request['command']]
                    result=self.broker.submit(opcode,int(request.get('argument',0)),int(request.get('ttl_ms',1000)))
                    client.sendall((json.dumps(result)+'\n').encode())
                except (ValueError,KeyError,OSError,TypeError):
                    try: client.sendall(b'{"status":"invalid_argument"}\n')
                    except OSError: pass

    def close(self):
        self.stop.set(); self.broker.close(); self.listener.close()
        if self.active:
            try: self.active.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        self.thread.join(timeout=1)
        try:
            if self.path.lstat().st_ino==self.identity: self.path.unlink()
        except FileNotFoundError: pass


def submit_local_command(path, command, argument=0, ttl_ms=1000, *, timeout=3):
    """Send once and read one bounded JSON line; uncertain transport never retries."""
    send_attempted = False
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            request = json.dumps({'command': command, 'argument': argument, 'ttl_ms': ttl_ms}) + '\n'
            # sendall failure may mean a partial request reached the server.
            send_attempted = True
            client.sendall(request.encode())
            response = bytearray()
            while b'\n' not in response:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('command response timed out')
                client.settimeout(remaining)
                chunk = client.recv(4097 - len(response))
                if not chunk:
                    raise ValueError('command response ended before newline')
                response.extend(chunk)
                if len(response) > 4096:
                    raise ValueError('command response too large')
            result = json.loads(response.split(b'\n', 1)[0])
            if not isinstance(result, dict) or result.get('status') not in (*STATUS_NAMES, 'unavailable'):
                raise ValueError('invalid command response')
            return result
    except (OSError, ValueError, UnicodeError):
        return {'status': 'outcome_unknown' if send_attempted else 'unavailable'}


def main():
    parser = argparse.ArgumentParser(description='Submit one local command, never automatically replay it')
    parser.add_argument('--socket', required=True)
    parser.add_argument('command', choices=OPCODES)
    parser.add_argument('--argument', type=int, default=0)
    parser.add_argument('--ttl-ms', type=int, default=1000)
    args = parser.parse_args()
    result = submit_local_command(args.socket, args.command, args.argument, args.ttl_ms)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'applied' else 1


if __name__ == '__main__':
    raise SystemExit(main())
