"""Deliberate TLS receiver overload must remain bounded and fully accounted."""
import json
import socket
import ssl
import subprocess
import threading
import time
from ground.test_security_e2e import certificates, tls_flight
from ground.orbit_ground import GroundReceiver
from ground.orbit_storage import TelemetryStorage


def test_overload_with_slow_durable_receiver_is_explicit(tmp_path,certificates,tls_flight):
    database=tmp_path/'ground.db';outbox=tmp_path/'outbox.db'
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_3
    context.verify_mode=ssl.CERT_REQUIRED
    context.load_verify_locations(str(certificates/'ca.pem'))
    context.load_cert_chain(str(certificates/'ground.pem'),str(certificates/'ground.key'))
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(.1)
    stop=threading.Event();ready=threading.Event();errors=[]
    class SlowStorage(TelemetryStorage):
        def store_sample(self,*args,**kwargs):
            inserted=super().store_sample(*args,**kwargs)
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
                    with context.wrap_socket(peer,server_side=True) as connection:
                        assert ('DNS','flight.orbital.test') in connection.getpeercert().get('subjectAltName',())
                        GroundReceiver(storage).handle_connection(connection)
        except BaseException as error:errors.append(error);ready.set()
        finally:
            if storage:storage.close()
    worker=threading.Thread(target=receive);worker.start()
    try:
        assert ready.wait(5) and not errors,errors
        command=[str(tls_flight),'127.0.0.1',str(listener.getsockname()[1]),'--source','scenario','--count','1000','--interval-ms','1','--capacity','32','--send-window','8','--priority-scheduling','1','--outbox',str(outbox),'--io-timeout-ms','1000','--drain-ms','3000','--ca',str(certificates/'ca.pem'),'--cert',str(certificates/'flight.pem'),'--key',str(certificates/'flight.key'),'--server-name','ground.orbital.test']
        result=subprocess.run(command,capture_output=True,text=True,timeout=25)
        assert result.returncode==0,result.stderr
        summary=json.loads(result.stdout)
        expected=1000+sum(i%600 in (100,200,260,300,350,400) for i in range(1000))
        assert summary['generated']==summary['durable_generated']==expected,summary
        assert summary['durable_dropped']>0 and summary['queue_high_watermark']<=32,summary
        assert summary['durable_generated']==summary['durable_acknowledged']+summary['durable_dropped']+summary['durable_retained'],summary
        assert not summary['storage_failed'] and summary['durable_retained']==0,summary
        storage=TelemetryStorage(database)
        try:
            assert len(storage.load_samples())==summary['acknowledged']
            lost=sum(int(row[0],16) for row in storage.connection.execute('SELECT lost_count FROM loss_reports'))
            assert lost==summary['durable_dropped']==summary['durable_loss_acknowledged'],summary
        finally:storage.close()
        (tmp_path/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    finally:
        stop.set();listener.close();worker.join(8)
    assert not worker.is_alive() and not errors,errors
