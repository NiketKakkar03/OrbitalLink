"""Type9 wire, migration and durable-before-ACK regressions."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import struct

import pytest

from ground.orbit_ground import GroundReceiver, classify_physical_validity
from ground.orbit_protocol import TelemetrySample, FrameParser, ProtocolError, encode_telemetry_sample, decode_frame
from ground.orbit_storage import TelemetryStorage

FIXTURE = Path(__file__).resolve().parents[1] / 'protocol/fixtures/sensor_sample_v1.json'


def sample(category=1, values=(3300,-200,800,4000,4500,1,0,0), **changes):
    base = TelemetrySample(1,(1<<64)-1,42,7,99,(1<<64)-1,0,3300,category,1,0,values)
    return replace(base, **changes)


def test_sensor_golden_and_fragmented_parser():
    expected = json.loads(FIXTURE.read_text())
    frame = bytes.fromhex(expected['frame_hex'])
    assert encode_telemetry_sample(sample()) == frame
    header, decoded = decode_frame(frame)
    assert header.message_type == 9 and header.payload_length == 76
    assert decoded == sample()
    parser = FrameParser()
    frames = []
    for byte in frame + encode_telemetry_sample(sample(telemetry_sequence=43)):
        frames.extend(parser.feed(bytes([byte])))
    parser.finish()
    assert [message.telemetry_sequence for _, message in frames] == [42,43]


@pytest.mark.parametrize('category,values', [
    (1,(3300,1,800,200,100,1,0,0)), (2,(-20000,40000,0,0,0,0,0,0)),
    (3,(-100,200,300,400,1,0,0,0)), (4,(600,1,20,200,180,0,0,0)),
    (5,(250,1000,0,0,0,0,0,0)), (6,(8,2,2,1,0,0,0,0)),
])
def test_every_category_preserves_signed_slots(category, values):
    original = sample(category, values, quality=1)
    _, decoded = decode_frame(encode_telemetry_sample(original))
    assert (decoded.category, decoded.schema, decoded.quality, decoded.values) == (category,1,1,values)
    assert decoded.temperature_millideg_c == (values[0] if category==2 else 0)
    assert decoded.voltage_millivolts == (values[0] if category==1 else 0)


@pytest.mark.parametrize('changes', [
    {'category':7}, {'category':True}, {'schema':2}, {'quality':2},
    {'quality':True}, {'values':(0,)*7}, {'values':(0,)*7+(1,)},
    {'values':(0,0,0,0,0,2,0,0)}, {'values':(True,0,0,0,0,0,0,0)},
])
def test_invalid_sensor_metadata_rejected_before_encoding(changes):
    with pytest.raises(ProtocolError):
        encode_telemetry_sample(replace(sample(), **changes))


@pytest.mark.parametrize('offset,data', [(64,b'\0\0'), (64,b'\0\7'), (66,b'\0\2'),(68,struct.pack('!I',2)),(100,struct.pack('!i',1)),(92,struct.pack('!i',2))])
def test_invalid_sensor_wire_metadata_rejected(offset,data):
    frame=bytearray(encode_telemetry_sample(sample()))
    frame[offset:offset+len(data)] = data
    with pytest.raises(ProtocolError): decode_frame(bytes(frame))


def test_legacy_database_migration_preserves_unsigned_identity(tmp_path):
    database = tmp_path/'legacy.db'
    # Construct the schema predating multisensor metadata, with unsigned fields
    # above signed SQLite INTEGER range stored as fixed-width hex TEXT.
    connection=sqlite3.connect(database)
    connection.execute('CREATE TABLE telemetry_samples (spacecraft_id INTEGER,flight_boot_id TEXT,telemetry_sequence TEXT,device_generation INTEGER,device_sequence INTEGER,timestamp_monotonic_ns TEXT,temperature_millideg_c INTEGER,voltage_millivolts INTEGER,received_at_unix_ns TEXT,physically_valid INTEGER,validity_reason TEXT,PRIMARY KEY(spacecraft_id,flight_boot_id,telemetry_sequence))')
    connection.execute('INSERT INTO telemetry_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)',(1,'ffffffffffffffff','fffffffffffffffe',7,1,'ffffffffffffffff',-5250,3300,'ffffffffffffffff',1,None))
    connection.commit(); connection.close()
    storage=TelemetryStorage(database)
    legacy=storage.load_samples()[0]
    assert legacy.category==0 and legacy.values==(0,)*8
    assert legacy.flight_boot_id==(1<<64)-1 and legacy.telemetry_sequence==(1<<64)-2
    assert storage.load_records()[0].received_at_unix_ns==(1<<64)-1
    assert storage.store_sample(sample())
    assert not storage.store_sample(sample())
    assert storage.category_counts()=={'legacy':1,'power':1}
    storage.close()
    reopened=TelemetryStorage(database)
    assert sample() in reopened.load_samples()
    assert len(reopened.load_samples())==2
    reopened.close()


def test_receiver_ack_follows_committed_typed_row(tmp_path):
    storage=TelemetryStorage(tmp_path/'samples.db')
    class Connection:
        def __init__(self): self.chunks=[encode_telemetry_sample(sample()),b'']; self.acks=[]
        def recv(self,size): return self.chunks.pop(0)
        def sendall(self,ack):
            other=TelemetryStorage(tmp_path/'samples.db')
            try: assert other.load_samples()==[sample()]
            finally: other.close()
            self.acks.append(ack)
    connection=Connection()
    GroundReceiver(storage).handle_connection(connection)
    assert len(connection.acks)==1
    assert decode_frame(connection.acks[0])[1].telemetry_sequence==42
    storage.close()


def test_failed_sensor_commit_never_acks():
    class Storage:
        def store_sample(self,*args,**kwargs): raise sqlite3.OperationalError('disk full')
    class Connection:
        def recv(self,size): return encode_telemetry_sample(sample())
        def sendall(self,ack): pytest.fail('ACK before successful commit')
    with pytest.raises(sqlite3.OperationalError): GroundReceiver(Storage()).handle_connection(Connection())


def test_quality_fault_kept_as_physically_invalid_not_discarded():
    assert classify_physical_validity(sample(quality=1))==(False,'sensor_fault')


@pytest.mark.parametrize('values', [(0,0,1,1,0,0,0,0),(1,3,1,1,0,0,0,0),(1,0,7,1,0,0,0,0),(1,0,1,0,0,0,0,0)])
def test_invalid_event_contract(values):
    with pytest.raises(ProtocolError): encode_telemetry_sample(sample(6,values))


def test_cli_exposes_health_and_events(tmp_path):
    import subprocess
    import sys
    database=tmp_path/'categories.db'
    storage=TelemetryStorage(database)
    health=sample(4,(600,2,4,1000,996,0,0,0),voltage_millivolts=0)
    event=sample(6,(8,2,2,3,0,0,0,0),telemetry_sequence=43,voltage_millivolts=0)
    storage.store_sample(health);storage.store_sample(event);storage.close()
    base=[sys.executable,'-m','ground.orbit_ground','--database',str(database)]
    status=subprocess.run(base+['--status'],capture_output=True,text=True,check=True)
    assert '"health": 1' in status.stdout and '"event": 1' in status.stdout
    tail=subprocess.run(base+['--tail','1'],capture_output=True,text=True,check=True)
    assert json.loads(tail.stdout)['category_name']=='event'
    export=tmp_path/'export.jsonl'
    subprocess.run(base+['--export',str(export)],capture_output=True,text=True,check=True)
    rows=[json.loads(line) for line in export.read_text().splitlines()]
    assert [row['category_name'] for row in rows]==['health','event']
    assert rows[0]['sample']['values']==list(health.values)
