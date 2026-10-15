"""Deliberate TLS receiver overload must remain bounded and fully accounted."""
import json
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import pytest
from ground.test_security_e2e import certificates, tls_flight
from ground.orbit_ground import GroundReceiver
from ground.orbit_storage import TelemetryStorage


@pytest.mark.parametrize('storage_batch',[1,16])
def test_overload_with_slow_durable_receiver_is_explicit(tmp_path,certificates,tls_flight,storage_batch):
    database=tmp_path/'ground.db';outbox=tmp_path/'outbox.db'
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_3
    context.verify_mode=ssl.CERT_REQUIRED
    context.load_verify_locations(str(certificates/'ca.pem'))
    context.load_cert_chain(str(certificates/'ground.pem'),str(certificates/'ground.key'))
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(.1)
    stop=threading.Event();ready=threading.Event();errors=[]
    class SlowStorage(TelemetryStorage):
        held=False
        def store_sample(self,*args,**kwargs):
            inserted=super().store_sample(*args,**kwargs)
            if not self.held:
                self.held=True
                # Force genuine overload rather than assuming a particular SSD
                # is faster than this receiver: capacity32 must see64 commits
                # before the first ACK, while the window protects8 rows.
                deadline=time.monotonic()+4
                while True:
                    probe=sqlite3.connect(outbox,timeout=.1)
                    try:
                        generated=probe.execute("SELECT value FROM counters WHERE name='generated'").fetchone()[0]
                    except sqlite3.OperationalError as error:
                        if 'locked' not in str(error):raise
                        generated=0
                    finally:probe.close()
                    if generated>=64:break
                    if time.monotonic()>deadline:raise RuntimeError('producer did not reach overload barrier')
                    time.sleep(.01)
            time.sleep(.02)  # 50 receipts/s ceiling, independent of sender window.
            return inserted
    def receive():
        storage=None
        try:
            storage=SlowStorage(database);ready.set()
            while not stop.is_set():
                try:peer,_=listener.accept()
                except TimeoutError:continue
                except OSError:
                    if stop.is_set():break
                    raise
                with peer:
                    peer.settimeout(2)
                    try:
                        with context.wrap_socket(peer,server_side=True) as connection:
                            assert ('DNS','flight.orbital.test') in connection.getpeercert().get('subjectAltName',())
                            GroundReceiver(storage).handle_connection(connection)
                    except (ConnectionError,ssl.SSLError,TimeoutError):
                        # Match the production listener: an interrupted peer
                        # does not kill the server or undo committed samples.
                        pass
        except BaseException as error:errors.append(error);ready.set()
        finally:
            if storage:storage.close()
    worker=threading.Thread(target=receive);worker.start()
    try:
        assert ready.wait(5) and not errors,errors
        command=[str(tls_flight),'127.0.0.1',str(listener.getsockname()[1]),'--source','scenario','--count','1000','--interval-ms','1','--capacity','32','--send-window','8','--storage-batch',str(storage_batch),'--priority-scheduling','1','--outbox',str(outbox),'--io-timeout-ms','5000','--drain-ms','3000','--ca',str(certificates/'ca.pem'),'--cert',str(certificates/'flight.pem'),'--key',str(certificates/'flight.key'),'--server-name','ground.orbital.test']
        result=subprocess.run(command,capture_output=True,text=True,timeout=60)
        assert result.returncode==0,result.stderr
        summary=json.loads(result.stdout)
        expected=1000+sum(i%600 in (100,200,260,300,350,400) for i in range(1000))
        assert summary['generated']==summary['durable_generated']==expected,(summary,errors)
        assert summary['durable_dropped']>0 and summary['queue_high_watermark']<=32,summary
        assert summary['durable_generated']==summary['durable_acknowledged']+summary['durable_dropped']+summary['durable_retained'],summary
        assert not summary['storage_failed'] and not errors,(summary,errors)
        initial=summary
        # Telemetry and loss reports have independent durable ACK boundaries.
        # A finite drain may finish all samples while leaving loss reports to
        # replay. Recover both within an explicit overall budget, retaining the
        # service's supported three-second drain on each restarted connection.
        recovery=command.copy();recovery[recovery.index('--count')+1]='0'
        recovery[recovery.index('--io-timeout-ms')+1]='1500'
        replays=[];recovery_started=time.monotonic();recovery_deadline=recovery_started+20
        def needs_recovery(state):
            return state['durable_retained']>0 or state['durable_dropped']!=state['durable_loss_acknowledged']
        while needs_recovery(summary):
            remaining=recovery_deadline-time.monotonic()
            assert remaining>0 and len(replays)<8,('durable recovery budget exhausted',summary,replays,errors)
            assert not errors,errors
            replay=subprocess.run(recovery,capture_output=True,text=True,timeout=min(15,remaining))
            assert replay.returncode==0,replay.stderr
            summary=json.loads(replay.stdout)
            replays.append(summary)
        assert not errors and not summary['storage_failed'] and summary['durable_retained']==0,(summary,errors)
        assert summary['durable_generated']==summary['durable_acknowledged']+summary['durable_dropped'],summary
        storage=TelemetryStorage(database)
        try:
            assert len(storage.load_samples())==summary['durable_acknowledged']
            lost=sum(int(row[0],16) for row in storage.connection.execute('SELECT lost_count FROM loss_reports'))
            assert lost==summary['durable_dropped']==summary['durable_loss_acknowledged'],summary
        finally:storage.close()
        with sqlite3.connect(outbox) as pending:
            assert pending.execute('SELECT count(*) FROM loss').fetchone()[0]==0,summary
        (tmp_path/'result.json').write_text(json.dumps({'initial':initial,'replays':replays,'final':summary,'recovery_elapsed_seconds':time.monotonic()-recovery_started},indent=2)+'\n')
    finally:
        stop.set();listener.close();worker.join(8)
    assert not worker.is_alive() and not errors,errors
