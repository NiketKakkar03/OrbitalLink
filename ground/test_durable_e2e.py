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


def run(executable,path,port,count,boot=101,capacity=16,*,recover=False):
    # Offline/intentional lost-ACK runs should finish quickly. Recovery tests
    # require several FULL-synchronous transactions on both peers; they exercise
    # correctness within the supported 3s drain, not a 100ms filesystem benchmark.
    drain_ms=3000 if recover else 300
    io_timeout_ms=1000 if recover else 100
    process=subprocess.run([str(executable),'127.0.0.1',str(port),'--outbox',str(path),'--count',str(count),'--boot-id',str(boot),'--interval-ms','0','--capacity',str(capacity),'--drain-ms',str(drain_ms),'--connect-timeout-ms','100','--io-timeout-ms',str(io_timeout_ms)],capture_output=True,text=True,timeout=8)
    assert process.returncode==0,process.stderr
    return json.loads(process.stdout)


def pending(path):
    with sqlite3.connect(path) as db:
        return [FrameParser().feed(bytes(row[0]))[0][1] for row in db.execute('SELECT frame FROM samples ORDER BY id')]


def receiver(database,listener,handler=None):
    ready=threading.Event();errors=[]
    def work():
        storage=None
        try:
            storage=TelemetryStorage(database)
            ready.set()
            with listener.accept()[0] as connection:
                if handler is None:GroundReceiver(storage).handle_connection(connection)
                else:handler(storage,connection)
        except BaseException as error:errors.append(error)
        finally:
            ready.set()
            if storage is not None:storage.close()
    thread=threading.Thread(target=work);thread.errors=errors;thread.start()
    assert ready.wait(5), "ground storage did not become ready"
    assert not errors, errors
    return thread


def join_receiver(worker):
    worker.join(5)
    assert not worker.is_alive(), "ground receiver did not stop"
    assert not worker.errors, worker.errors


def test_offline_restart_keeps_original_identity_and_payload(tmp_path,flight_service):
    path=tmp_path/'outbox.db';database=tmp_path/'ground.db'
    run(flight_service,path,unused_port(),4)
    original=pending(path)
    assert len(original)==4 and {s.flight_boot_id for s in original}=={101}
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        worker=receiver(database,listener)
        result=run(flight_service,path,listener.getsockname()[1],0,boot=202,recover=True)
        join_receiver(worker)
    assert result['acknowledged']==4 and pending(path)==[], result
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
    def drop_ack(storage,peer):
        parser=FrameParser()
        while True:
            chunk=peer.recv(4096)
            assert chunk, "flight disconnected before sending its retained sample"
            frames=parser.feed(chunk)
            if frames:
                _,sample=frames[0];assert sample==original
                storage.store_sample(sample)
                return
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        worker=receiver(database,listener,drop_ack)
        result=run(flight_service,path,listener.getsockname()[1],0)
        join_receiver(worker)
    assert result['acknowledged']==0 and pending(path)==[original]
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        worker=receiver(database,listener)
        result=run(flight_service,path,listener.getsockname()[1],0,recover=True)
        join_receiver(worker)
        assert result['acknowledged']==1, result
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
        result=run(flight_service,path,listener.getsockname()[1],0,boot=202,capacity=4,recover=True)
        join_receiver(worker)
        assert result['acknowledged']==4, result
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT lost_count,reason FROM loss_reports').fetchall()==[('0000000000000002',1)]
        assert db.execute("SELECT COUNT(*) FROM event_details WHERE event_type='telemetry_loss'").fetchone()[0]==1
    with sqlite3.connect(path) as db:assert db.execute('SELECT COUNT(*) FROM loss').fetchone()[0]==0


def test_lost_loss_ack_retries_immutable_report_without_duplicate(tmp_path,flight_service):
    path=tmp_path/'outbox.db';database=tmp_path/'ground.db'
    run(flight_service,path,unused_port(),20,capacity=3)
    stop=threading.Event();ready=threading.Event();errors=[];lost=[]
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
        storage=None
        try:
            storage=TelemetryStorage(database)
            ready.set()
            while not stop.is_set():
                try:connection,_=listener.accept()
                except TimeoutError:continue
                except OSError:break
                with connection:
                    connection.settimeout(.1)
                    try:GroundReceiver(storage).handle_connection(LoseFirstLossAck(connection))
                    except ConnectionResetError:pass
        except BaseException as error:errors.append(error)
        finally:
            ready.set()
            if storage is not None:storage.close()
    worker=threading.Thread(target=work);worker.start()
    try:
        assert ready.wait(5), "ground storage did not become ready"
        assert not errors, errors
        result=run(flight_service,path,listener.getsockname()[1],2,boot=202,capacity=100,recover=True)
        assert result['retained']==0, result
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
