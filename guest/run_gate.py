#!/usr/bin/env python3
"""Run inside Linux: two guest boots, durable replay, mTLS and ground DB gate."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ground.dev_certificates import generate
from ground.orbit_storage import TelemetryStorage
from integration.run_checks import stop_process_group


def run_smoke(command, timeout=240):
    process = subprocess.Popen(command, start_new_session=True)
    try:
        if process.wait(timeout=timeout):
            raise RuntimeError('guest smoke failed')
    finally:
        # Kill the entire session group, including QEMU if smoke was interrupted.
        stop_process_group(process)
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--driver-tests', action='store_true', help='M6 module/lifecycle and device-origin mTLS gate')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    credentials = output / 'credentials'
    generate(credentials)
    ready = output / 'ready'
    database = output / 'telemetry.db'
    with (output / 'ground.log').open('wb') as log:
        ground = subprocess.Popen([sys.executable, '-m', 'ground.orbit_ground',
            '--database', str(database), '--ready-file', str(ready),
            '--max-receipts', str(100 if args.driver_tests else 107), '--ca', str(credentials / 'ca.pem'),
            '--cert', str(credentials / 'ground.pem'), '--key', str(credentials / 'ground.key'),
            '--allowed-client', 'flight.orbital.test'], cwd=ROOT,
            stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                if ground.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('ground readiness failed; inspect ground.log')
                time.sleep(0.05)
            common = [sys.executable, str(ROOT / 'guest/smoke.py'), '--work', str(args.work.resolve()),
                '--ca', str(credentials / 'ca.pem'), '--cert', str(credentials / 'flight.pem'),
                '--key', str(credentials / 'flight.key'), '--port', ready.read_text().strip(),
                '--server-name', 'ground.orbital.test', '--state', str(output / 'state.ext4')]
            if args.driver_tests:
                run_smoke(common + ['--count', '100', '--driver-tests',
                    '--artifacts', str(output / 'device')], timeout=600)
            else:
                run_smoke(common + ['--count', '7', '--offline-seed',
                    '--artifacts', str(output / 'seed')])
                run_smoke(common + ['--count', '100', '--replay-count', '7',
                    '--artifacts', str(output / 'replay')])
            ground.wait(timeout=15)
            if ground.returncode:
                raise RuntimeError('ground failed')
            storage = TelemetryStorage(database)
            try:
                samples = storage.load_samples()
            finally:
                storage.close()
            groups = Counter(sample.flight_boot_id for sample in samples)
            expected_counts = [100] if args.driver_tests else [7, 100]
            if len(samples) != sum(expected_counts) or sorted(groups.values()) != expected_counts:
                raise RuntimeError(f'durable two-boot identity/count mismatch: {groups}')
            if args.driver_tests:
                ordered = sorted(samples, key=lambda sample: sample.telemetry_sequence)
                sequences = [sample.device_sequence for sample in ordered]
                if sequences != list(range(sequences[0], sequences[0] + 100)):
                    raise RuntimeError('device sequence progression is not consecutive')
                for sample in ordered:
                    if (sample.device_generation != 1 or sample.voltage_millivolts != 3300 or sample.timestamp_monotonic_ns <= 0 or
                            sample.temperature_millideg_c != -10000 + (sample.device_sequence % 1000) * 125):
                        raise RuntimeError('ground rows do not match the deterministic kernel device source')
            (output / 'result.json').write_text(json.dumps({
                'status': 'passed', 'authenticated_samples': sum(expected_counts),
                'boot_sample_counts': sorted(groups.values()),
                'gate': ('driver syscall and 100 lifecycle cycles; device-origin mTLS; durable ground rows'
                    if args.driver_tests else 'two guest boots; persistent outbox replay; mTLS; durable ground rows')
            }, indent=2) + '\n')
        finally:
            stop_process_group(ground)
            ground.wait(timeout=5)


if __name__ == '__main__':
    main()
