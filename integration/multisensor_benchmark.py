#!/usr/bin/env python3
"""Measured native multisensor TLS profile. A passed result requires exact rows."""
from __future__ import annotations
import argparse
import hashlib
import shutil
from collections import Counter
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ground.dev_certificates import generate
from ground.orbit_storage import TelemetryStorage
from integration.run_checks import stop_process_group


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flight',type=Path,default=Path('/tmp/orbit_flight_service'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=600)
    parser.add_argument('--rate',type=int,choices=[10,100,1000],default=10)
    parser.add_argument('--window',type=int,default=32)
    args=parser.parse_args()
    if not 1<=args.seconds<=3600 or not 1<=args.window<=64:parser.error('invalid duration/window')
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    certs=output/'credentials';generate(certs)
    executable=output/'flight-executable';shutil.copy2(args.flight.resolve(),executable)
    identity={'flight_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
              'revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip())}
    (output/'execution-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
    ground=flight=None
    count=args.seconds*args.rate
    expected_events=sum(i%600 in (100,200,260,300,350,400) for i in range(count))
    expected=count+expected_events
    ready=output/'ready';database=output/'ground.db'
    try:
        with (output/'ground.log').open('wb') as glog,(output/'flight.log').open('wb') as flog,(output/'resources.jsonl').open('w') as resource_log:
            ground=subprocess.Popen([sys.executable,'-m','ground.orbit_ground','--database',str(database),'--ready-file',str(ready),'--max-receipts',str(expected),'--ca',str(certs/'ca.pem'),'--cert',str(certs/'ground.pem'),'--key',str(certs/'ground.key'),'--allowed-client','flight.orbital.test'],cwd=ROOT,stdout=glog,stderr=glog,start_new_session=True)
            deadline=time.monotonic()+15
            while not ready.exists():
                if ground.poll() is not None or time.monotonic()>deadline:raise RuntimeError('ground readiness failed')
                time.sleep(.05)
            command=[str(executable),'127.0.0.1',ready.read_text().strip(),'--source','scenario','--scenario-seed','42','--count',str(count),'--interval-ms',str(1000//args.rate),'--send-window',str(args.window),'--priority-scheduling','1','--capacity',str(max(1024,expected)),'--outbox',str(output/'outbox.db'),'--ca',str(certs/'ca.pem'),'--cert',str(certs/'flight.pem'),'--key',str(certs/'flight.key'),'--server-name','ground.orbital.test','--io-timeout-ms','2000','--drain-ms','3000']
            (output/'command.json').write_text(json.dumps(command,indent=2)+'\n')
            started=time.monotonic();flight=subprocess.Popen(command,cwd=ROOT,stdout=flog,stderr=subprocess.PIPE,start_new_session=True)
            resources=[]
            while flight.poll() is None:
                elapsed=time.monotonic()-started
                if elapsed>args.seconds+30:raise RuntimeError('profile exceeded deadline')
                sample={'elapsed_seconds':elapsed}
                for name,process in [('flight',flight),('ground',ground)]:
                    if process.poll() is None:
                        probe=subprocess.run(['ps','-p',str(process.pid),'-o','rss=,pcpu='],capture_output=True,text=True,timeout=5)
                        values=probe.stdout.split()
                        if len(values)==2:sample[name]={'rss_bytes':int(values[0])*1024,'cpu_percent':float(values[1])}
                resources.append(sample);resource_log.write(json.dumps(sample)+'\n');resource_log.flush()
                time.sleep(min(1,max(.05,args.seconds/20)))
            elapsed=time.monotonic()-started
            stderr=flight.stderr.read().decode(errors='replace');(output/'flight.stderr').write_text(stderr)
            if flight.returncode:raise RuntimeError(f'flight failed: {stderr}')
            ground.wait(timeout=10)
            if ground.returncode:raise RuntimeError('ground failed')
        summary=json.loads((output/'flight.log').read_text().strip().splitlines()[-1])
        storage=TelemetryStorage(database)
        try:samples=storage.load_samples()
        finally:storage.close()
        categories=Counter(s.category for s in samples)
        failures=[]
        for key,value in [('generated',expected),('acknowledged',expected),('durable_retained',0),('durable_dropped',0),('storage_failed',False),('source_to_ack_samples',expected)]:
            if summary.get(key)!=value:failures.append(f'{key}: {summary.get(key)} != {value}')
        if len(samples)!=expected:failures.append('ground row count mismatch')
        if len({(s.flight_boot_id,s.telemetry_sequence) for s in samples})!=expected:failures.append('identity mismatch')
        if categories[6]!=expected_events or any(not categories[c] for c in range(1,6)):failures.append('missing categories/events')
        p95=summary.get('source_to_ack_p95_upper_ms')
        if p95 is None or p95>1000:failures.append('source-to-durableACK p95 exceeds1second or unavailable')
        if args.seconds>=600 and elapsed<args.seconds-1:failures.append('baseline did not sustain full requested duration')
        memory={}
        for name in ['flight','ground']:
            values=[r[name]['rss_bytes'] for r in resources if name in r]
            if values:
                width=max(1,len(values)//4)
                growth=sum(values[-width:])/width-sum(values[:width])/width
                memory[name]={'peak_rss_bytes':max(values),'last_vs_first_quarter_mean_bytes':growth}
                if args.seconds>=600 and growth>32*1024*1024:failures.append(f'{name} RSS grows more than32MiB across profile')
        result={'status':'failed' if failures else 'passed','scope':'native scenario source; embedded device benchmark separate','requested_seconds':args.seconds,'elapsed_seconds':elapsed,'routine_rate':args.rate,'expected_rows':expected,'ground_rows':len(samples),'category_counts':dict(categories),'summary':summary,'memory':memory,'failures':failures,'host':platform.platform(),'execution_identity':identity}
        (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
        if failures:raise SystemExit(1)
    except BaseException as error:
        if not (output/'result.json').exists():
            (output/'result.json').write_text(json.dumps({'status':'failed','error':str(error),'requested_seconds':args.seconds,'routine_rate':args.rate,'expected_rows':expected,'host':platform.platform()},indent=2)+'\n')
        raise
    finally:
        for process in [flight,ground]:
            if process is not None:
                stop_process_group(process)
                process.wait(timeout=5)

if __name__=='__main__':main()
