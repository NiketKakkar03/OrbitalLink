import sqlite3

import pytest

from ground.orbit_protocol import TelemetrySample
from ground.orbit_storage import TelemetryStorage


def make_sample(**changes: int) -> TelemetrySample:
    values = {
        "spacecraft_id": 1,
        "flight_boot_id": 123,
        "telemetry_sequence": 42,
        "device_generation": 7,
        "device_sequence": 99,
        "timestamp_monotonic_ns": 1234567890,
        "temperature_millideg_c": -5250,
        "voltage_millivolts": 3300,
    }
    values.update(changes)
    return TelemetrySample(**values)


def test_stores_and_loads_sample(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")
    sample = make_sample()

    assert storage.store_sample(sample) is True
    assert storage.load_samples() == [sample]

    storage.close()


def test_duplicate_sample_is_idempotent(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")
    sample = make_sample()

    assert storage.store_sample(sample) is True
    assert storage.store_sample(sample) is False
    assert storage.load_samples() == [sample]

    storage.close()


def test_data_survives_database_reopen(tmp_path):
    database_path = tmp_path / "telemetry.db"
    sample = make_sample()

    storage = TelemetryStorage(database_path)
    storage.store_sample(sample)
    storage.close()

    reopened_storage = TelemetryStorage(database_path)
    assert reopened_storage.load_samples() == [sample]
    reopened_storage.close()


def test_preserves_full_unsigned_64_bit_range(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")
    sample = make_sample(
        flight_boot_id=(1 << 64) - 1,
        telemetry_sequence=(1 << 64) - 1,
        timestamp_monotonic_ns=(1 << 64) - 1,
    )

    assert storage.store_sample(sample) is True
    assert storage.load_samples() == [sample]

    storage.close()


def test_stores_receive_time_and_invalid_sensor_reason(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")
    sample = make_sample(temperature_millideg_c=200_000)

    storage.store_sample(
        sample,
        received_at_unix_ns=999,
        physically_valid=False,
        validity_reason="temperature_out_of_range",
    )

    record = storage.load_records()[0]
    assert record.sample == sample
    assert record.received_at_unix_ns == 999
    assert record.physically_valid is False
    assert record.validity_reason == "temperature_out_of_range"
    storage.close()


def test_orders_unsigned_sequences_numerically(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")
    high = make_sample(telemetry_sequence=1 << 63)
    low = make_sample(telemetry_sequence=9)

    storage.store_sample(high)
    storage.store_sample(low)

    assert storage.load_samples() == [low, high]
    storage.close()


def test_rejects_value_outside_protocol_range(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")

    with pytest.raises(ValueError, match="flight_boot_id"):
        storage.store_sample(make_sample(flight_boot_id=1 << 64))

    assert storage.load_samples() == []
    storage.close()


def test_database_write_error_is_not_reported_as_success(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")
    storage.connection.execute("DROP TABLE telemetry_samples")
    storage.connection.commit()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        storage.store_sample(make_sample())

    storage.close()


def test_event_details_are_bounded(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")

    event_id = storage.record_event("connected", "loopback peer", received_at_unix_ns=5)
    assert event_id == 1
    with pytest.raises(ValueError, match="too long"):
        storage.record_event("error", "x" * 1025)

    storage.close()


def test_command_results_are_idempotent(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")
    arguments = {
        "command_id": (1 << 64) - 1,
        "command_session_id": 8,
        "command_sequence": 2,
        "status": "accepted",
        "details": "period changed",
        "received_at_unix_ns": 10,
    }

    assert storage.record_command_result(**arguments) is True
    assert storage.record_command_result(**arguments) is False
    count = storage.connection.execute("SELECT COUNT(*) FROM command_results").fetchone()[0]
    assert count == 1
    storage.close()


def test_stores_ten_thousand_deterministic_samples_exactly(tmp_path):
    storage = TelemetryStorage(tmp_path / "telemetry.db")

    for sequence in range(10_000):
        sample = make_sample(
            telemetry_sequence=sequence,
            device_sequence=sequence,
            timestamp_monotonic_ns=sequence * 100_000_000,
            temperature_millideg_c=20_000 + (sequence % 1_000),
            voltage_millivolts=3_300 + (sequence % 10),
        )
        assert storage.store_sample(sample, received_at_unix_ns=sequence) is True

    loaded = storage.load_samples()
    assert storage.count_samples() == 10_000
    assert len(loaded) == 10_000
    for sequence, sample in enumerate(loaded):
        assert sample == make_sample(
            telemetry_sequence=sequence,
            device_sequence=sequence,
            timestamp_monotonic_ns=sequence * 100_000_000,
            temperature_millideg_c=20_000 + (sequence % 1_000),
            voltage_millivolts=3_300 + (sequence % 10),
        )

    storage.close()
