#!/usr/bin/env python3
"""QEMU power cut with live typed producer; SQLite recovery and authenticated replay."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from ground.dev_certificates import generate
from ground.orbit_protocol import decode_frame
from ground.orbit_storage import TelemetryStorage
from integration.run_checks import stop_process_group
from run_gate import run_smoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--probe',type=Path,required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    (output/'orbit-manifest.json').write_bytes((args.work/'output/images/orbit-manifest.json').read_bytes())
    import hashlib
    (output/'ground-source-sha256.json').write_text(json.dumps({str(p.relative_to(ROOT)):
        hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'ground').glob('*.py'))},indent=2)+'\n')
    credentials = output / 'credentials'
    generate(credentials)
    state = output / 'state.ext4'
    common = [sys.executable,str(ROOT/'guest/smoke.py'),'--work',str(args.work.resolve()),
        '--ca',str(credentials/'ca.pem'),'--cert',str(credentials/'flight.pem'),
        '--key',str(credentials/'flight.key'),'--server-name','ground.orbital.test',
        '--state',str(state),'--outbox-probe',str(args.probe.resolve()),'--interval-ms','100']
    ground = None
    try:
        run_smoke(common+['--port','1','--sensors','--offline-seed','--power-cut',
            '--count','1000','--artifacts',str(output/'power-cut')],timeout=180)
        prefix = json.loads((output/'power-cut/result.json').read_text())['snapshot']
        run_smoke(common+['--port','1','--inspect-outbox','--count','0',
            '--artifacts',str(output/'recovered')],timeout=180)
        recovered = json.loads((output/'recovered/result.json').read_text())['snapshot']
        if recovered['generated'] != recovered['retained'] or recovered['acked'] or recovered['dropped']:
            raise RuntimeError('post-crash conservation failure')
        if recovered['retained'] < prefix['retained'] or len(recovered['frames']) != recovered['retained']:
            raise RuntimeError('committed prefix was lost or bounded recovery fixture exceeded256records')
        if not set(prefix['frames']).issubset(recovered['frames']):
            raise RuntimeError('committed typed frame bytes changed across VM crash')
        expected = [decode_frame(bytes.fromhex(frame))[1] for frame in recovered['frames']]
        if any(sample.category == 0 for sample in expected):
            raise RuntimeError('power-cut fixture did not retain typed sensor samples')
        ready = output/'ready'
        database = output/'telemetry.db'
        with (output/'ground.log').open('wb') as log:
            ground = subprocess.Popen([sys.executable,'-m','ground.orbit_ground','--database',str(database),
                '--ready-file',str(ready),'--max-receipts',str(len(expected)),
                '--ca',str(credentials/'ca.pem'),'--cert',str(credentials/'ground.pem'),
                '--key',str(credentials/'ground.key'),'--allowed-client','flight.orbital.test'],
                cwd=ROOT,stdout=log,stderr=log,start_new_session=True)
            deadline = time.monotonic()+15
            while not ready.exists():
                if ground.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('recovery ground readiness failed')
                time.sleep(0.05)
            run_smoke(common+['--port',ready.read_text().strip(),'--sensors','--count','0',
                '--storage-batch','16',
                '--replay-count',str(len(expected)),'--artifacts',str(output/'replayed')],timeout=180)
            ground.wait(timeout=15)
            if ground.returncode:
                raise RuntimeError('recovery ground failed')
        storage = TelemetryStorage(database)
        try:
            samples = storage.load_samples()
        finally:
            storage.close()
        if len(samples) != len(expected) or set(samples) != set(expected):
            raise RuntimeError('authenticated ground rows differ from recovered immutable frame identities/values')
        replay = json.loads((output/'replayed/result.json').read_text())['summary']
        if replay['generated'] or replay['durable_generated'] != len(expected) or replay['durable_acknowledged'] != len(expected) or replay['durable_retained'] or replay['durable_dropped']:
            raise RuntimeError('replayed durable counters do not reconcile')
        (output/'result.json').write_text(json.dumps({'status':'passed',
            'gate':'live-producer QEMU SIGKILL; ext4/SQLite recovery; immutable typed prefix; mTLS durable replay',
            'precrash_committed_prefix':prefix['retained'],'recovered_committed_records':len(expected),
            'authenticated_ground_rows':len(samples),'phase_boots':3,
            'qemu_exit_signal':9,'scope':'guest VM power loss; host storage and Docker daemon remained running',
            'storage_batch':1,'replay_storage_batch':16,'replay_drain_ms':3000,
            'replay_summary':replay},indent=2)+'\n')
    except BaseException as error:
        (output/'result.json').write_text(json.dumps({'status':'failed','error':str(error)},indent=2)+'\n')
        raise
    finally:
        if ground:
            stop_process_group(ground)
            ground.wait(timeout=5)


if __name__=='__main__':
    main()
