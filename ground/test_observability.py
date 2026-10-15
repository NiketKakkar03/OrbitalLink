"""Loss accounting commits before ACK and read-only command snapshots."""
import struct
import threading
import pytest
from ground.orbit_protocol import *
from ground.orbit_storage import TelemetryStorage
from ground.orbit_ground import GroundReceiver
from ground.orbit_commands import CommandBroker

@pytest.mark.parametrize('loss',[LossReport(1,2,3,4,7,4),LossReport(1,0,3,0,0,99,2)])
def test_loss_roundtrip_and_duplicate_commit(tmp_path,loss):
    _,decoded=decode_frame(encode_loss_report(loss))
    assert decoded==loss
    storage=TelemetryStorage(tmp_path/'ground.db')
    assert storage.store_loss(loss)
    assert not storage.store_loss(loss)
    assert storage.connection.execute('SELECT COUNT(*) FROM event_details').fetchone()[0]==1
    storage.close()
    storage=TelemetryStorage(tmp_path/'ground.db')
    assert not storage.store_loss(loss)
    storage.close()

@pytest.mark.parametrize('loss',[LossReport(1,2,0,1,2,2),LossReport(1,2,1,2,1,1),LossReport(1,2,1,1,2,3),LossReport(1,2,1,0,0,1,2)])
def test_invalid_loss(loss):
    with pytest.raises(ProtocolError): encode_loss_report(loss)

def test_loss_identity_cannot_be_mutated(tmp_path):
    storage=TelemetryStorage(tmp_path/'ground.db')
    storage.store_loss(LossReport(1,2,3,4,5,2))
    with pytest.raises(ValueError): storage.store_loss(LossReport(1,2,3,4,6,3))
    storage.close()

def test_storage_failure_never_acknowledged():
    class Store:
        def store_loss(self,_): raise RuntimeError('disk failure')
    class Peer:
        def recv(self,_): return encode_loss_report(LossReport(1,2,3,4,5,2))
        def sendall(self,_): pytest.fail('ACK before commit')
    with pytest.raises(RuntimeError): GroundReceiver(Store()).handle_connection(Peer())

def test_query_state_correlates_and_exposes_snapshot():
    broker=CommandBroker();broker.open(1,2,3)
    result=[]
    worker=threading.Thread(target=lambda:result.append(broker.submit(6)))
    worker.start()
    frame=None
    import time
    while frame is None:
        frame=broker.next_frame();time.sleep(.001)
    _,message=decode_frame(frame)
    session,seq,identifier,*_=struct.unpack('!QQQQII',message.payload)
    payload=struct.pack('!QQQIIIIIIQQ',session,seq,identifier,0,0,1,100,7,0,12,45)
    broker.snapshot(payload)
    worker.join(2)
    assert result[0]['state']=={'enabled':True,'period_ms':100,'generation':7,'fault':False,'effects':12,'monotonic_ns':45}
    with pytest.raises(ValueError): broker.snapshot(struct.pack('!QQQIIIIIIQQ',1,1,1,0,0,2,100,7,0,0,0))

def test_conflicting_duplicate_loss_is_rejected_before_ack(tmp_path):
    storage=TelemetryStorage(tmp_path/'ground.db')
    original=LossReport(1,2,3,4,5,2)
    assert storage.store_loss(original)
    class Peer:
        def recv(self,_): return encode_loss_report(LossReport(1,2,3,4,6,3))
        def sendall(self,_): pytest.fail('conflicting loss body must never receive ACK')
    with pytest.raises(ValueError,match='identity reused'):
        GroundReceiver(storage).handle_connection(Peer())
    assert storage.connection.execute('SELECT lost_count FROM loss_reports').fetchone()[0]=='0000000000000002'
    assert storage.connection.execute('SELECT count(*) FROM event_details').fetchone()[0]==1
    storage.close()


def test_duplicate_loss_ack_follows_committed_report(tmp_path):
    storage=TelemetryStorage(tmp_path/'ground.db')
    loss=LossReport(1,2,3,4,5,2)
    storage.store_loss(loss)
    class Peer:
        def __init__(self):self.sent=[];self.read=False
        def recv(self,_):
            if self.read:return b''
            self.read=True
            return encode_loss_report(loss)
        def sendall(self,frame):
            # A separate connection observes the durable transaction before ACK.
            import sqlite3
            with sqlite3.connect(tmp_path/'ground.db') as verifier:
                assert verifier.execute('SELECT count(*) FROM loss_reports').fetchone()[0]==1
                assert verifier.execute('SELECT count(*) FROM event_details').fetchone()[0]==1
            self.sent.append(frame)
    peer=Peer();GroundReceiver(storage).handle_connection(peer)
    assert peer.sent==[encode_loss_ack(loss)]
    storage.close()
