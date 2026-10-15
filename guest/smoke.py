#!/usr/bin/env python3
"""Boot a built image and assert known-count mTLS ACKs over serial console."""
import argparse
import base64
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import subprocess
import sys
import time
import tty

from guest import REPO, VERSIONS, command, digest, prepare_state


def assert_summary(summary, count):
    for field, expected in [('generated', count), ('acknowledged', count),
                            ('dropped_oldest', 0), ('discarded_shutdown', 0)]:
        if summary.get(field) != expected:
            raise RuntimeError(f'{field}: expected {expected}, got {summary.get(field)}')


class Console:
    def __init__(self, fd, child, logfile):
        self.fd, self.child, self.logfile = fd, child, logfile
        self.buffer = b''
        self.resources = []
        self.last_resource = 0.0

    def send(self, text):
        data = text.encode() + b'\n'
        while data:
            sent = os.write(self.fd, data)
            data = data[sent:]

    def wait(self, pattern, seconds):
        deadline = time.monotonic() + seconds
        while True:
            match = re.search(pattern, self.buffer)
            if match:
                self.buffer = self.buffer[match.end():]
                return match
            if self.child.poll() is not None:
                raise RuntimeError('QEMU exited before serial assertion')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f'serial assertion timed out: {pattern!r}')
            if time.monotonic() - self.last_resource >= 10 and len(self.resources) < 128:
                self.last_resource = time.monotonic()
                proc = Path(f'/proc/{self.child.pid}/stat')
                if proc.exists():
                    fields = proc.read_text().rsplit(')', 1)[1].split()
                    self.resources.append({'host_monotonic_ns':time.monotonic_ns(),
                        'cpu_seconds':(int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
                        'rss_bytes':int(fields[21]) * os.sysconf('SC_PAGE_SIZE')})
            readable, _, _ = select.select([self.fd], [], [], min(remaining, 1))
            if readable:
                chunk = os.read(self.fd, 65536)
                self.logfile.write(chunk); self.logfile.flush()
                self.buffer += chunk
                # Some guest console layers emit CRCRLF even with a raw host
                # PTY. Normalize line endings only; retain original evidence.
                self.buffer = re.sub(rb'\r+\n', b'\n', self.buffer)
                if len(self.buffer) > 1024 * 1024:
                    raise RuntimeError('serial response exceeds bounded buffer')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work', type=Path, default=REPO / 'build/guest')
    p.add_argument('--ca', required=True, type=Path)
    p.add_argument('--cert', required=True, type=Path)
    p.add_argument('--key', required=True, type=Path)
    p.add_argument('--host', default='10.0.2.2')
    p.add_argument('--port', required=True, type=int)
    p.add_argument('--server-name', required=True)
    p.add_argument('--count', type=int, default=100)
    p.add_argument('--offline-seed', action='store_true', help='retain samples without contacting ground, then power off')
    p.add_argument('--replay-count', type=int, default=0, help='expected retained samples from a previous guest boot')
    p.add_argument('--timeout', type=int, default=180)
    p.add_argument('--artifacts', required=True, type=Path)
    p.add_argument('--state', type=Path, help='dedicated persistent state disk; an existing disk is preserved')
    p.add_argument('--driver-tests', action='store_true', help='run installed module syscall/lifecycle checks and use device samples')
    p.add_argument('--sensors', action='store_true', help='load the real QEMU ABI2 MMIO/IRQ driver')
    p.add_argument('--sensor-contract', action='store_true', help='run ABI2 syscall/profile/reset checks and power off')
    p.add_argument('--interval-ms', type=int, default=10)
    p.add_argument('--seed', type=int, default=1)
    args = p.parse_args()
    if not 1 <= args.port <= 65535 or not 0 <= args.count <= 10000 or args.replay_count < 0 or args.timeout < 1:
        p.error('port/count/timeout outside supported range')
    out = args.work.resolve() / 'output'
    state = args.state.resolve() if args.state else None
    prepare_state(out, state)
    manifest = json.loads((out / 'images/orbit-manifest.json').read_text())
    for name, expected in manifest['artifacts_sha256'].items():
        if digest(out / 'images' / name) != expected:
            raise RuntimeError(f'image identity mismatch: {name}')
    # Read before starting QEMU and keep secrets out of host artifacts.
    credentials = {name: base64.b64encode(path.read_bytes()).decode()
                   for name, path in [('ca', args.ca), ('cert', args.cert), ('key', args.key)]}
    args.artifacts.mkdir(parents=True, exist_ok=False)
    master, slave = pty.openpty()
    # QEMU already supplies terminal CRLF; host line discipline must not add CR.
    tty.setraw(slave)
    child = None
    console = None
    try:
        with (args.artifacts / 'serial.log').open('wb') as logfile:
            if 'qemu_executable_sha256' in manifest and digest(out / 'host/bin/qemu-system-aarch64') != manifest['qemu_executable_sha256']:
                raise RuntimeError('installed QEMU executable identity mismatch')
            child = subprocess.Popen(command(out, state, args.seed), stdin=slave, stdout=slave, stderr=slave)
            os.close(slave); slave = -1
            console = Console(master, child, logfile)
            console.wait(rb'orbit-guest login:', args.timeout)
            console.send('root')
            console.wait(rb'# ', 20)
            # Disable guest echo before provisioning; the next marker proves
            # the command ran, so private keys cannot enter serial.log.
            console.send("stty -echo && printf '\\nORBIT_ECHO_OFF\\n'")
            console.wait(rb'\r?\nORBIT_ECHO_OFF\r?\n', 10)
            console.send(f'date -u -s @{int(time.time())}')
            console.send("uname -a; date -u; /lib/ld-linux-aarch64.so.1 --list /usr/bin/orbit-flight; ip addr show eth0; printf '\\nORBIT_INFO_END\\n'")
            info = console.wait(rb'(?s)(.*?)\r?\nORBIT_INFO_END\r?\n', 20).group(1)
            if ('Linux orbit-guest ' + VERSIONS['linux']['version']).encode() not in info or b'aarch64' not in info:
                raise RuntimeError('unexpected guest kernel/architecture')
            if b'not found' in info or not all(lib in info for lib in [b'libssl', b'libcrypto', b'libstdc++', b'libsqlite3']):
                raise RuntimeError('guest dynamic libraries missing')
            if b'inet ' not in info:
                raise RuntimeError('guest DHCP address missing')
            console.send('umask 077; mkdir -p /tmp/orbit-credentials')
            # Short lines stay below terminal canonical-input limits.
            for name, encoded in credentials.items():
                console.send(f': > /tmp/orbit-credentials/{name}.b64')
                for offset in range(0, len(encoded), 512):
                    console.send(f"printf '%s' '{encoded[offset:offset+512]}' >> /tmp/orbit-credentials/{name}.b64")
                console.send(f'base64 -d /tmp/orbit-credentials/{name}.b64 > /tmp/orbit-credentials/{name}.pem')
            console.send("printf '\\nORBIT_CREDENTIALS_READY\\n'")
            console.wait(rb'\r?\nORBIT_CREDENTIALS_READY\r?\n', 20)
            console.send("mkdir -p /var/lib/orbit; blkid /dev/vdb | grep -q 'LABEL=\"orbit-state\"' && mount /dev/vdb /var/lib/orbit && printf '\\nORBIT_STATE_MOUNTED\\n'")
            console.wait(rb'\r?\nORBIT_STATE_MOUNTED\r?\n', 20)
            if args.sensors or args.sensor_contract:
                console.send("orbit_sensor_module=$(find /lib/modules -name orbit_sensors.ko | head -n 1); test -n \"$orbit_sensor_module\" && insmod \"$orbit_sensor_module\" && test -c /dev/orbit_sensors && printf '\\nORBIT_SENSORS_READY\\n'")
                console.wait(rb'\r?\nORBIT_SENSORS_READY\r?\n', 20)
            if args.sensor_contract:
                console.send("/usr/bin/orbit-sensors-contract; orbit_sensor_rc=$?; dmesg; printf '\\nORBIT_SENSOR_EXIT=%s\\n' \"$orbit_sensor_rc\"")
                contract = console.wait(rb'(?s)(.*?)\r?\nORBIT_SENSOR_EXIT=(\d+)\r?\n', 90)
                if contract.group(2) != b'0' or re.search(rb'BUG:|WARNING:|KASAN:|possible circular locking|use-after-free', contract.group(1)):
                    raise RuntimeError('ABI2 MMIO/IRQ syscall/profile/reset test failed; inspect serial.log')
                console.send('poweroff')
                child.wait(timeout=20)
                if child.returncode:
                    raise RuntimeError('sensor-contract guest shutdown failed')
                (args.artifacts / 'result.json').write_text(json.dumps({'status':'passed',
                    'gate':'ABI2 real MMIO/IRQ syscall/profile/counter/reset test',
                    'image_sha256':manifest['artifacts_sha256']}, indent=2) + '\n')
                return
            if args.driver_tests:
                console.send("orbit_module=$(find /lib/modules -name orbit_test.ko | head -n 1); test -n \"$orbit_module\" && /usr/bin/orbit-driver-tests \"$orbit_module\" /usr/bin/orbit-syscall-contract 100 /var/lib/orbit/driver-artifacts; orbit_driver_rc=$?; for orbit_artifact in /var/lib/orbit/driver-artifacts/*; do printf '\\nORBIT_DRIVER_ARTIFACT=%s\\n' \"$orbit_artifact\"; cat \"$orbit_artifact\"; done; printf '\\nORBIT_DRIVER_EXIT=%s\\n' \"$orbit_driver_rc\"")
                driver_exit = console.wait(rb'\r?\nORBIT_DRIVER_EXIT=(\d+)\r?\n', 300)
                if driver_exit.group(1) != b'0':
                    raise RuntimeError('driver syscall/lifecycle gate failed; inspect serial.log')
                console.send("insmod \"$orbit_module\" initial_period_ms=60000 && printf '\\nORBIT_DRIVER_READY\\n'")
                console.wait(rb'\r?\nORBIT_DRIVER_READY\r?\n', 20)
            cmd = ['/usr/bin/orbit-flight', args.host, str(1 if args.offline_seed else args.port),
                   '--ca', '/tmp/orbit-credentials/ca.pem', '--cert', '/tmp/orbit-credentials/cert.pem',
                   '--key', '/tmp/orbit-credentials/key.pem', '--server-name', args.server_name,
                   '--count', str(args.count), '--interval-ms', str(args.interval_ms),
                   '--outbox', '/var/lib/orbit/outbox.db', '--capacity', str(max(1,args.count + args.replay_count)), '--drain-ms', '3000']
            if args.driver_tests:
                cmd += ['--source', 'device', '--device', '/dev/orbit_telemetry']
            if args.sensors:
                cmd += ['--source', 'sensors', '--device', '/dev/orbit_sensors',
                        '--send-window', '32', '--priority-scheduling', '1', '--io-timeout-ms', '2000']
            invocation = shlex.join(cmd)
            if args.sensors:
                invocation += " > /var/lib/orbit/flight-summary.json 2>/var/lib/orbit/flight-stderr.log & orbit_flight_pid=$!; "
                invocation += "(while kill -0 \"$orbit_flight_pid\" 2>/dev/null; do cat /proc/$orbit_flight_pid/stat; grep '^VmRSS:' /proc/$orbit_flight_pid/status; sleep 10; done) > /var/lib/orbit/flight-resources.txt 2>/dev/null & orbit_sampler_pid=$!; "
                invocation += "wait \"$orbit_flight_pid\"; orbit_rc=$?; kill \"$orbit_sampler_pid\" 2>/dev/null; wait \"$orbit_sampler_pid\" 2>/dev/null; printf '\\nORBIT_GUEST_RESOURCES_BEGIN\\n'; cat /var/lib/orbit/flight-resources.txt; printf '\\nORBIT_GUEST_RESOURCES_END\\n'; cat /var/lib/orbit/flight-stderr.log; cat /var/lib/orbit/flight-summary.json; printf '\\nORBIT_EXIT=%s\\n' \"$orbit_rc\""
            else:
                invocation += "; orbit_rc=$?; printf '\\nORBIT_EXIT=%s\\n' \"$orbit_rc\""
            console.send(invocation)
            if args.sensors:
                resource_text = console.wait(rb'(?s)\r?\nORBIT_GUEST_RESOURCES_BEGIN\r?\n(.*?)\r?\nORBIT_GUEST_RESOURCES_END\r?\n', args.timeout).group(1)
                (args.artifacts / 'guest-flight-resources.txt').write_bytes(resource_text)
            summary_match = console.wait(rb'(\{"state":[^\r\n]+\})', args.timeout)
            summary = json.loads(summary_match.group(1))
            exit_match = console.wait(rb'\r?\nORBIT_EXIT=(\d+)\r?\n', 20)
            if exit_match.group(1) != b'0':
                raise RuntimeError('flight service returned nonzero')
            if args.offline_seed:
                if summary.get('generated') != args.count or summary.get('acknowledged') != 0 or summary.get('durable_retained') != args.count:
                    raise RuntimeError(f'offline seed was not retained: {summary}')
            else:
                expected = args.count + args.replay_count
                for field, value in [('generated',args.count), ('acknowledged',expected), ('durable_retained',0), ('dropped_oldest',0), ('discarded_shutdown',0)]:
                    if summary.get(field) != value:
                        raise RuntimeError(f'{field}: expected {value}, got {summary.get(field)}')
            if args.driver_tests and summary.get('source') != 'device':
                raise RuntimeError('driver gate did not use device-origin telemetry')
            if args.sensors and summary.get('source') != 'sensors':
                raise RuntimeError('sensor gate did not use ABI2 MMIO/IRQ telemetry')
            result = {'status': 'passed', 'summary': summary, 'versions': VERSIONS,
                      'image_sha256': manifest['artifacts_sha256'],
                      'host_alias': args.host, 'port': args.port, 'server_name': args.server_name,
                      'gate': ('guest boot and ABI2 multisensor MMIO/IRQ mTLS durable ACK count; ground database verification separate'
                               if args.sensors else 'guest boot, driver syscall/lifecycle and device-source mTLS durable ACK count; ground database verification separate'
                               if args.driver_tests else 'guest boot and fake-source mTLS durable ACK count; ground database verification separate')}
            console.send('poweroff')
            child.wait(timeout=20)
            if child.returncode:
                raise RuntimeError(f'QEMU shutdown failed: {child.returncode}')
            (args.artifacts / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            (args.artifacts / 'qemu-resources.json').write_text(json.dumps(console.resources, indent=2) + '\n')
            print('Guest smoke passed:', args.artifacts / 'result.json')
    except BaseException as error:
        (args.artifacts / 'result.json').write_text(json.dumps({'status':'failed',
            'error':str(error),'image_sha256':manifest['artifacts_sha256']},indent=2) + '\n')
        if console:
            (args.artifacts / 'qemu-resources.json').write_text(json.dumps(console.resources,indent=2) + '\n')
        raise
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
        os.close(master)
        if slave != -1:
            os.close(slave)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'guest smoke: {error}', file=sys.stderr)
        sys.exit(1)
