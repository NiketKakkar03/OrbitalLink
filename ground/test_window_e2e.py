"""Actual C++ window: delayed/reordered ACKs and replay after connection faults."""
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
import pytest
from ground.test_m3_e2e import flight_service, unused_port
from ground.orbit_protocol import FrameParser, TelemetryAck, encode_telemetry_ack
from ground.orbit_storage import TelemetryStorage


def invoke(binary,path,port,count,window,drain=3000,extra=()):
    result=subprocess.run([str(binary),'127.0.0.1',str(port),'--outbox',str(path),'--count',str(count),'--interval-ms','0','--capacity','64','--send-window',str(window),'--io-timeout-ms','1500','--drain-ms',str(drain),*extra],capture_output=True,text=True,timeout=12)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('batch',[1,16])
@pytest.mark.parametrize('fault',[None,'disconnect','unknown_ack','duplicate_prefix'])
def test_multiple_outstanding_reordered_acks_and_fault_replay(tmp_path,flight_service,fault,batch):
    path=tmp_path/'outbox.db';database=tmp_path/'ground.db'
    assert invoke(flight_service,path,unused_port(),16,1,100)['durable_retained']==16
    errors=[];ready=threading.Event();batches=[]
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(8)
    def receive():
        storage=None
        try:
            storage=TelemetryStorage(database);ready.set();injected=False;acked=set()
            while True:
                connection,_=listener.accept()
                with connection:
                    connection.settimeout(5);parser=FrameParser();pending=[]
                    while True:
                        data=connection.recv(4096)
                        if not data:break
                        for header,sample in parser.feed(data):
                            storage.store_sample(sample);pending.append((header,sample))
                            if len(pending)==4 or len(acked|{(s.flight_boot_id,s.telemetry_sequence) for _,s in pending})==16:
                                batches.append(len(pending))
                                if fault and not injected:
                                    injected=True
                                    if fault=='duplicate_prefix':
                                        h,s=pending[0]
                                        ack=encode_telemetry_ack(TelemetryAck(s.spacecraft_id,s.flight_boot_id,s.telemetry_sequence),message_sequence=h.message_sequence)
                                        connection.sendall(ack+ack);acked.add((s.flight_boot_id,s.telemetry_sequence))
                                    if fault=='unknown_ack':
                                        h,s=pending[0]
                                        connection.sendall(encode_telemetry_ack(TelemetryAck(s.spacecraft_id,s.flight_boot_id,s.telemetry_sequence),message_sequence=h.message_sequence+999))
                                    if fault in ('unknown_ack','duplicate_prefix'):
                                        while connection.recv(4096):pass
                                    else:connection.shutdown(socket.SHUT_RDWR)
                                    break
                                time.sleep(.1)
                                for h,s in reversed(pending):
                                    connection.sendall(encode_telemetry_ack(TelemetryAck(s.spacecraft_id,s.flight_boot_id,s.telemetry_sequence),message_sequence=h.message_sequence))
                                acked.update((s.flight_boot_id,s.telemetry_sequence) for _,s in pending)
                                pending=[]
                        else:continue
                        break
                if not fault or injected and len(storage.load_samples())==16:break
        except BaseException as error:errors.append(error);ready.set()
        finally:
            if storage:storage.close()
    worker=threading.Thread(target=receive);worker.start()
    try:
        assert ready.wait(5) and not errors,errors
        result=invoke(flight_service,path,listener.getsockname()[1],0,4,extra=('--storage-batch',str(batch)))
        worker.join(8)
        assert not worker.is_alive() and not errors,errors
        assert 4 in batches and all(1<=n<=4 for n in batches)
        assert result['acknowledged']==16 and result['durable_retained']==0,result
        if fault:assert result['reconnects']>=1,result
        if fault in ('unknown_ack','duplicate_prefix'):assert result['invalid_acks']>=1,result
        storage=TelemetryStorage(database)
        try:assert len(storage.load_samples())==16
        finally:storage.close()
    finally:
        listener.close();worker.join(8)


def test_priority_mode_replays_urgent_event_before_old_routine_backlog(tmp_path,flight_service):
    path=tmp_path/'priority.db';database=tmp_path/'ground.db'
    seed=invoke(flight_service,path,unused_port(),110,1,100,('--source','scenario','--interval-ms','1','--capacity','256'))
    assert seed['durable_retained']==111,seed
    ready=threading.Event();errors=[];seen=[]
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
    def receive():
        storage=None
        try:
            storage=TelemetryStorage(database);ready.set()
            with listener.accept()[0] as peer:
                peer.settimeout(5);parser=FrameParser()
                while True:
                    data=peer.recv(4096)
                    if not data:break
                    for header,sample in parser.feed(data):
                        seen.append(sample.category);storage.store_sample(sample)
                        peer.sendall(encode_telemetry_ack(TelemetryAck(sample.spacecraft_id,sample.flight_boot_id,sample.telemetry_sequence),message_sequence=header.message_sequence))
        except BaseException as error:errors.append(error);ready.set()
        finally:
            if storage:storage.close()
    worker=threading.Thread(target=receive);worker.start()
    try:
        assert ready.wait(5) and not errors,errors
        result=invoke(flight_service,path,listener.getsockname()[1],0,4,3000,('--priority-scheduling','1','--capacity','256'))
        worker.join(6)
        assert not errors and not worker.is_alive(),errors
        assert seen[0]==6 and set(seen)==set(range(1,7)),seen
        assert len(seen)==111 and result['acknowledged']==111 and result['durable_retained']==0,result
    finally:
        listener.close();worker.join(6)
