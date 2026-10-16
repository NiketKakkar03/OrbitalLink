"""Bounded available-record batching retains exact durable ACK boundaries."""
import sqlite3
import ssl
from collections import deque
import pytest
from ground.orbit_ground import GroundReceiver
from ground.orbit_protocol import (COMMAND_SESSION, LOSS_ACK, TELEMETRY_ACK, LossReport,
                                   decode_frame, encode_control, encode_loss_report,
                                   encode_telemetry_sample)
from ground.orbit_storage import TelemetryStorage
from ground.test_storage import make_sample


class RecordingStorage(TelemetryStorage):
    def __init__(self,path):
        super().__init__(path);self.batches=[]
    def store_samples(self,records):
        self.batches.append(len(records));return super().store_samples(records)


class AvailablePeer:
    def __init__(self,chunks,check_ack=lambda frame:None):
        self.chunks=deque(chunks);self.timeout=2;self.acks=[];self.check_ack=check_ack
        self.nonblocking_calls=0;self.maximum_nonblocking_calls=0;self.transitions=[]
    def gettimeout(self):return self.timeout
    def settimeout(self,value):
        self.transitions.append(value)
        if value==0:self.nonblocking_calls=0
        self.timeout=value
    def recv(self,size):
        assert size<=65536
        if self.timeout==0:
            self.nonblocking_calls+=1
            self.maximum_nonblocking_calls=max(self.maximum_nonblocking_calls,self.nonblocking_calls)
        if self.chunks:return self.chunks.popleft()
        if self.timeout==0:raise ssl.SSLWantReadError()
        return b''
    def sendall(self,frame):self.check_ack(frame);self.acks.append(decode_frame(frame))


def frame(sequence):return encode_telemetry_sample(make_sample(telemetry_sequence=sequence),message_sequence=sequence+100)


def test_one_frame_per_read_batches_and_commits_before_first_ack(tmp_path):
    path=tmp_path/'ground.db';storage=RecordingStorage(path)
    def committed(_):
        with sqlite3.connect(path) as reader:
            assert reader.execute('SELECT count(*) FROM telemetry_samples').fetchone()[0]>=min(40,(len(peer.acks)//16+1)*16)
    peer=AvailablePeer([frame(sequence) for sequence in range(40,0,-1)],committed)
    receiver=GroundReceiver(storage);receiver.handle_connection(peer)
    assert storage.batches==[16,16,8]
    assert receiver.receipts==40 and len(peer.acks)==40
    assert [header.message_sequence for header,_ in peer.acks]==list(range(140,100,-1))
    assert [sample.telemetry_sequence for sample in storage.load_samples()]==list(range(1,41))
    assert peer.timeout==2 and peer.maximum_nonblocking_calls<=16
    storage.close()


def test_batch_duplicate_reorder_keeps_original_payload(tmp_path):
    storage=TelemetryStorage(tmp_path/'ground.db')
    original=make_sample(telemetry_sequence=3)
    changed=make_sample(telemetry_sequence=3,temperature_millideg_c=40000)
    records=[(original,True,None),(make_sample(telemetry_sequence=1),True,None),(changed,True,None),(make_sample(telemetry_sequence=2),True,None)]
    assert storage.store_samples(records)==[True,True,False,True]
    assert storage.load_samples()==[make_sample(telemetry_sequence=1),make_sample(telemetry_sequence=2),original]
    assert storage.connection.execute('PRAGMA synchronous').fetchone()[0]==3
    storage.close()


def test_failure_midbatch_rolls_back_all_and_sends_no_ack(tmp_path):
    class FailingStorage(RecordingStorage):
        def store_sample(self,sample,**metadata):
            if sample.telemetry_sequence==2:raise sqlite3.OperationalError('injected second-record failure')
            return super().store_sample(sample,**metadata)
    storage=FailingStorage(tmp_path/'ground.db');peer=AvailablePeer([frame(1),frame(2)])
    receiver=GroundReceiver(storage)
    with pytest.raises(sqlite3.OperationalError):receiver.handle_connection(peer)
    assert storage.count_samples()==0 and not peer.acks and receiver.receipts==0
    assert not storage.connection.in_transaction and peer.timeout==2
    assert storage.store_samples([(make_sample(telemetry_sequence=1),True,None)])==[True]
    storage.close()


def test_control_and_loss_preserve_order_between_telemetry_commits(tmp_path):
    storage=RecordingStorage(tmp_path/'ground.db');events=[]
    class Broker:
        def next_frame(self):return None
        def open(self,*args):events.append(('session',storage.count_samples()))
    import struct
    session=encode_control(COMMAND_SESSION,struct.pack('!QQQ',1,2,3))
    loss=encode_loss_report(LossReport(1,2,3,4,5,2))
    peer=AvailablePeer([frame(1),session,frame(2),loss,frame(3)],lambda data:events.append((decode_frame(data)[0].message_type,storage.count_samples())))
    receiver=GroundReceiver(storage,broker=Broker());receiver.handle_connection(peer)
    assert events==[(TELEMETRY_ACK,1),('session',1),(TELEMETRY_ACK,2),(LOSS_ACK,2),(TELEMETRY_ACK,3)]
    assert storage.batches==[1,1,1] and receiver.receipts==3
    storage.close()


def test_partial_records_bound_nonblocking_calls_and_keep_parser_state(tmp_path):
    storage=RecordingStorage(tmp_path/'ground.db');data=frame(1)
    peer=AvailablePeer([data[index:index+1] for index in range(len(data))])
    receiver=GroundReceiver(storage);receiver.handle_connection(peer)
    assert receiver.receipts==1 and peer.maximum_nonblocking_calls==16 and peer.timeout==2
    storage.close()


def test_nonblocking_protocol_errors_restore_timeout_and_propagate(tmp_path):
    storage=RecordingStorage(tmp_path/'ground.db')
    peer=AvailablePeer([frame(1),b'BAD!' + bytes(28)])
    from ground.orbit_protocol import ProtocolError
    with pytest.raises(ProtocolError):GroundReceiver(storage).handle_connection(peer)
    assert peer.timeout==2 and not peer.acks and storage.count_samples()==0
    storage.close()


@pytest.mark.parametrize('count',[0,17])
def test_storage_rejects_out_of_bound_batch(tmp_path,count):
    storage=TelemetryStorage(tmp_path/'ground.db')
    with pytest.raises(ValueError):storage.store_samples([(make_sample(),True,None)]*count)
    assert storage.count_samples()==0
    storage.close()
