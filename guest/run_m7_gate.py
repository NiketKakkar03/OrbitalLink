#!/usr/bin/env python3
"""Actual ABI2 guest syscall gate and declared-profile multisensor TLS run."""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ground.dev_certificates import generate
from ground.orbit_storage import TelemetryStorage
from integration.run_checks import stop_process_group
from run_gate import run_smoke


def validate_memory(resource_text, duration, summary):
    rss = [int(line.split()[1])*1024 for line in resource_text.splitlines() if line.startswith('VmRSS:')]
    required = max(1,int(duration / 10 * 0.8))
    if len(rss) < required:
        raise RuntimeError(f'guest RSS coverage insufficient: {len(rss)}<{required}')
    width = max(1,len(rss)//4)
    growth = sum(rss[-width:])/width-sum(rss[:width])/width
    if growth > 32*1024*1024:
        raise RuntimeError(f'guest RSS growth exceeded declared32MiB bound: {growth}')
    return {'rss_samples':len(rss),'required_samples':required,'sample_interval_seconds':10,
        'peak_sampled_rss_bytes':max(rss),'last_vs_first_quarter_mean_bytes':growth,
        'growth_limit_bytes':32*1024*1024,'process_peak_rss_bytes':summary['process_peak_rss_bytes']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ticks', type=int, default=6000)
    parser.add_argument('--period-ms', type=int, default=100)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--skip-contract', action='store_true')
    parser.add_argument('--storage-batch', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.ticks <= 9000 or not 1 <= args.period_ms <= 60000 or not 0 <= args.seed <= 0xffffffff or not 1 <= args.storage_batch <= 16:
        parser.error('ticks/period/seed outside supported bounds')
    profile = [3,1,3,2,3,1,3,4,3,5]
    expected = Counter(profile[tick % 10] for tick in range(args.ticks))
    expected[6] = sum(tick % 600 in {100,200,260,300,350,400} for tick in range(args.ticks))
    expected = +expected
    total = sum(expected.values())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    credentials = output / 'credentials'
    generate(credentials)
    ready = output / 'ready'
    database = output / 'telemetry.db'
    duration = args.ticks * args.period_ms / 1000
    with (output / 'ground.log').open('wb') as log:
        ground = subprocess.Popen([sys.executable, '-m', 'ground.orbit_ground',
            '--database', str(database), '--ready-file', str(ready), '--max-receipts', str(total),
            '--ca', str(credentials / 'ca.pem'), '--cert', str(credentials / 'ground.pem'),
            '--key', str(credentials / 'ground.key'), '--allowed-client', 'flight.orbital.test'],
            cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                if ground.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('ground failed readiness')
                time.sleep(0.05)
            common = [sys.executable, str(ROOT / 'guest/smoke.py'), '--work', str(args.work.resolve()),
                '--ca', str(credentials / 'ca.pem'), '--cert', str(credentials / 'flight.pem'),
                '--key', str(credentials / 'flight.key'), '--port', ready.read_text().strip(),
                '--server-name', 'ground.orbital.test', '--seed', str(args.seed), '--storage-batch', str(args.storage_batch)]
            if not args.skip_contract:
                run_smoke(common + ['--sensor-contract', '--count','0',
                    '--state',str(output / 'contract-state.ext4'),
                    '--artifacts',str(output / 'contract')], timeout=180)
            started = time.monotonic()
            run_smoke(common + ['--sensors', '--count',str(total), '--interval-ms',str(args.period_ms),
                '--timeout',str(max(180,int(duration + 60))), '--state',str(output / 'baseline-state.ext4'),
                '--artifacts',str(output / 'baseline')], timeout=max(240,int(duration + 120)))
            wall_seconds = time.monotonic() - started
            ground.wait(timeout=15)
            if ground.returncode:
                raise RuntimeError('ground failed')
            storage = TelemetryStorage(database)
            try:
                samples = storage.load_samples()
            finally:
                storage.close()
            summary = json.loads((output / 'baseline/result.json').read_text())['summary']
            actual = Counter(sample.category for sample in samples)
            if len(samples) != total or actual != expected:
                raise RuntimeError(f'ground category/count mismatch: {actual}, expected {expected}')
            ordered = sorted(samples, key=lambda sample: sample.device_sequence)
            if [sample.device_sequence for sample in ordered] != list(range(total)):
                raise RuntimeError('source sequence gap in healthy profile')
            if any(sample.device_generation != 1 or sample.schema != 1 or sample.quality for sample in ordered):
                raise RuntimeError('invalid sensor generation/schema/quality')
            cursor = 0
            event_codes = {100:1,400:2,300:3,350:4,200:5,260:6}
            for tick in range(args.ticks):
                phase = tick % 600
                if phase in event_codes:
                    event = ordered[cursor]; cursor += 1
                    code = event_codes[phase]
                    severity = {1:0,2:0,3:2,4:2,5:1,6:0}[code]
                    related = {1:1,2:1,3:1,4:2,5:3,6:3}[code]
                    if event.category != 6 or tuple(event.values[:4]) != (code,severity,related,1) or any(event.values[4:]):
                        raise RuntimeError('transition event/ordering differs from declared scenario')
                sample = ordered[cursor]; cursor += 1
                if sample.category != profile[tick % 10]:
                    raise RuntimeError('routine category order differs from declared profile')
                eclipse = 100 <= phase < 400
                disturbed = 200 <= phase < 260
                charge = max(100,900 - (phase - 100) * 3) if eclipse else 900
                values = list(sample.values)
                if sample.category == 1:
                    wanted = [3200+charge,450 if eclipse else -100,charge,0 if eclipse else 2400,1800 if eclipse else 1200,int(eclipse),0,0]
                elif sample.category == 2:
                    wanted = [15000 if eclipse else 25000,85000 if 350 <= phase < 400 else 35000,int(eclipse),0,0,0,0,0]
                elif sample.category == 3:
                    wanted = [2000 if disturbed else 0,-1000 if disturbed else 0,500 if disturbed else 0,15000 if disturbed else 10,int(disturbed),0,0,0]
                elif sample.category == 5:
                    wanted = [0 if disturbed else 20+(tick*17+args.seed)%13,1000,int(disturbed),0,0,0,0,0]
                else:
                    wanted = values
                    if values[0] < 0 or values[1] != 0 or values[2]+values[4]+values[5] != values[3] or any(values[6:]):
                        raise RuntimeError('health clock/counter snapshot inconsistent')
                if values != wanted:
                    raise RuntimeError(f'interacting synthetic values mismatch at tick{tick}: {values}!={wanted}')
            if len({sample.flight_boot_id for sample in samples}) != 1:
                raise RuntimeError('unexpected flight identity change')
            for field in ['driver_dropped','durable_dropped','dropped_oldest','storage_failed']:
                if summary.get(field) != 0:
                    raise RuntimeError(f'healthy profile reported loss/failure: {field}={summary.get(field)}')
            if summary['source_to_ack_samples'] != total:
                raise RuntimeError('source latency did not cover every current-boot sample')
            p95 = summary['source_to_ack_p95_upper_ms']
            if p95 is None or p95 > 1000:
                raise RuntimeError(f'source-to-durable-ACK p95 exceeded1second: {p95}')
            if summary['driver_generated'] != summary['driver_enqueued'] + summary['driver_dropped'] or summary['driver_enqueued'] != summary['driver_delivered'] + summary['driver_queued']:
                raise RuntimeError('source counters do not reconcile')
            clock_drift = abs(summary['sensor_clock_current_offset_ns'] - summary['sensor_clock_offset_ns'])
            clock_budget = summary['sensor_clock_uncertainty_ns'] + summary['sensor_clock_current_uncertainty_ns'] + 1_000_000
            if clock_drift > clock_budget:
                raise RuntimeError(f'guest/device clock drift exceeds brackets plus declared1ms tolerance: {clock_drift}>{clock_budget}')
            guest_resource_text = (output / 'baseline/guest-flight-resources.txt').read_text()
            guest_memory = validate_memory(guest_resource_text,duration,summary)
            result = {'status':'passed','gate':'actual QEMU multisensor MMIO/IRQ to mTLS durable ground storage',
                'routine_ticks':args.ticks,'period_ms':args.period_ms,'scenario_seed':args.seed,
                'storage_batch':args.storage_batch,
                'declared_virtual_seconds':duration,'host_wall_seconds':wall_seconds,
                'authenticated_samples':total,'category_counts':dict(actual),
                'source_to_ack_p95_upper_ms':p95,'source_latency_samples':summary['source_to_ack_samples'],
                'clock_drift_ns':clock_drift,'clock_drift_budget_ns':clock_budget,
                'guest_memory':guest_memory,
                'flight_summary':summary,'database':str(database),
                'limitations':['synthetic interacting scenario, not physical accuracy','TCG guest; no physical firmware flashed']}
            (output / 'result.json').write_text(json.dumps(result,indent=2) + '\n')
        except BaseException as error:
            (output / 'result.json').write_text(json.dumps({'status':'failed',
                'error':str(error),'routine_ticks':args.ticks,'period_ms':args.period_ms,
                'scenario_seed':args.seed},indent=2) + '\n')
            raise
        finally:
            stop_process_group(ground)
            ground.wait(timeout=5)


if __name__ == '__main__':
    main()
