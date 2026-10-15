"""Real native processes prove persistent identity, dedup and loss ACK recovery."""
import json
import sqlite3
import socket
import subprocess
import threading
from ground.test_m3_e2e import flight_service, unused_port
from ground.orbit_ground import GroundReceiver
from ground.orbit_protocol import FrameParser, TELEMETRY_SAMPLE, encode_telemetry_ack, TelemetryAck
from ground.orbit_storage import TelemetryStorage
from ground.orbit_protocol import LOSS_ACK, decode_frame


def run(executable,path,port,count,boot=101,capacity=16):
    process=subprocess.run([str(executable),'127.0.0.1',str(port),'--outbox',str(path),'--count',str(count),'--boot-id',str(boot),'--interval-ms','0','--capacity',str(capacity),'--drain-ms','300','--connect-timeout-ms','100','--io-timeout-ms','100'],capture_output=True,text=True,timeout=6)
    assert process.returncode==0,process.stderr
    return json.loads(process.stdout)


def pending(path):
    with sqlite3.connect(path) as db:
        return [FrameParser().feed(bytes(row[0]))[0][1] for row in db.execute('SELECT frame FROM samples ORDER BY id')]


def receiver(database,listener):
    def work():
        storage=TelemetryStorage(database)
        try:
            with listener.accept()[0] as connection:
                GroundReceiver(storage).handle_connection(connection)
        finally:storage.close()
    thread=threading.Thread(target=work);thread.start()
    return thread


def test_offline_restart_keeps_original_identity_and_payload(tmp_path,flight_service):
    path=tmp_path/'outbox.db';database=tmp_path/'ground.db'
    run(flight_service,path,unused_port(),4)
    original=pending(path)
    assert len(original)==4 and {s.flight_boot_id for s in original}=={101}
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        worker=receiver(database,listener)
        result=run(flight_service,path,listener.getsockname()[1],0,boot=202)
        worker.join(5);assert not worker.is_alive()
    assert result['acknowledged']==4 and pending(path)==[]
    assert result['durable_generated'] == 4
    assert result['durable_acknowledged'] == 4
    assert result['durable_retained'] == 0
    assert not result['storage_failed']
    storage=TelemetryStorage(database)
    assert storage.load_samples()==original
    storage.close()


def test_ground_commit_lost_ack_replayed_without_duplicate(tmp_path,flight_service):
    path=tmp_path/'outbox.db';database=tmp_path/'ground.db'
    run(flight_service,path,unused_port(),1)
    original=pending(path)[0]
    def drop_ack(listener):
        storage=TelemetryStorage(database)
        try:
            with listener.accept()[0] as peer:
                parser=FrameParser()
                while True:
                    frames=parser.feed(peer.recv(4096))
                    if frames:
                        _,sample=frames[0];assert sample==original
                        storage.store_sample(sample)
                        return
        finally:storage.close()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        worker=threading.Thread(target=drop_ack,args=(listener,));worker.start()
        result=run(flight_service,path,listener.getsockname()[1],0)
        worker.join(5)
    assert result['acknowledged']==0 and pending(path)==[original]
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        worker=receiver(database,listener)
        assert run(flight_service,path,listener.getsockname()[1],0)['acknowledged']==1
        worker.join(5)
    storage=TelemetryStorage(database);assert storage.load_samples()==[original];storage.close()
    assert pending(path)==[]


def test_bounded_overflow_report_survives_restart_and_is_acknowledged(tmp_path,flight_service):
    path=tmp_path/'outbox.db';database=tmp_path/'ground.db'
    run(flight_service,path,unused_port(),6,capacity=4)
    assert [s.telemetry_sequence for s in pending(path)]==[3,4,5,6]
    with sqlite3.connect(path) as db:
        loss=db.execute('SELECT boot,first,last,count FROM loss').fetchall()
    assert loss==[('101','1','2',2)]
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        worker=receiver(database,listener)
        assert run(flight_service,path,listener.getsockname()[1],0,boot=202,capacity=4)['acknowledged']==4
        worker.join(5)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT lost_count,reason FROM loss_reports').fetchall()==[('0000000000000002',1)]
        assert db.execute("SELECT COUNT(*) FROM event_details WHERE event_type='telemetry_loss'").fetchone()[0]==1
    with sqlite3.connect(path) as db:assert db.execute('SELECT COUNT(*) FROM loss').fetchone()[0]==0


def test_lost_loss_ack_retries_immutable_report_without_duplicate(tmp_path,flight_service):
    path=tmp_path/'outbox.db';database=tmp_path/'ground.db'
    run(flight_service,path,unused_port(),20,capacity=3)
    stop=threading.Event();errors=[];lost=[]
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(.1)

    class LoseFirstLossAck:
        def __init__(self,connection):self.connection=connection
        def recv(self,length):return self.connection.recv(length)
        def sendall(self,frame):
            header,_=decode_frame(frame)
            if header.message_type==LOSS_ACK and not lost:
                lost.append(True);self.connection.shutdown(socket.SHUT_RDWR)
                raise ConnectionResetError('injected loss ACK disappearance after ground commit')
            self.connection.sendall(frame)

    def work():
        storage=TelemetryStorage(database)
        try:
            while not stop.is_set():
                try:connection,_=listener.accept()
                except TimeoutError:continue
                except OSError:break
                with connection:
                    connection.settimeout(.1)
                    try:GroundReceiver(storage).handle_connection(LoseFirstLossAck(connection))
                    except ConnectionResetError:pass
        except BaseException as error:errors.append(error)
        finally:storage.close()
    worker=threading.Thread(target=work);worker.start()
    try:
        result=run(flight_service,path,listener.getsockname()[1],2,boot=202,capacity=100)
        assert result['retained']==0
    finally:
        stop.set();listener.close();worker.join(3)
    assert lost and not errors and not worker.is_alive()
    storage=TelemetryStorage(database)
    assert {(s.flight_boot_id,s.telemetry_sequence) for s in storage.load_samples()}=={(101,18),(101,19),(101,20),(202,1),(202,2)}
    assert storage.connection.execute('SELECT lost_count FROM loss_reports').fetchall()==[('0000000000000011',)]
    assert storage.connection.execute("SELECT count(*) FROM event_details WHERE event_type='telemetry_loss'").fetchone()[0]==1
    storage.close()
    with sqlite3.connect(path) as db:
        counts=dict(db.execute('SELECT name,value FROM counters'))
        assert counts['generated']==22 and counts['acked']==5 and counts['dropped']==17 and counts['loss_acked']==17
        assert db.execute('SELECT count(*) FROM samples').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM loss').fetchone()[0]==0
