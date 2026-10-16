"""Explicit functional/stress sizes and evidence for durable storage tests."""
import json
import os


def sample_count():
    raw = os.environ.get("ORBIT_TEST_DURABLE_COUNT", "1000")
    try:
        count = int(raw)
    except ValueError as error:
        raise ValueError("ORBIT_TEST_DURABLE_COUNT must be an integer in1000..10000") from error
    if not 1000 <= count <= 10000:
        raise ValueError("ORBIT_TEST_DURABLE_COUNT must be in1000..10000")
    return count


def sender_timeout(count):
    return max(180, min(1200, count * .12))


def write_evidence(path, storage, requested, acknowledged, elapsed, *, transport=False):
    stored = storage.count_samples()
    result = {
        "requested_unique_samples": requested,
        "stored_unique_samples": stored,
        "validation_mode": "native_e2e" if transport else "storage_unit",
        "acknowledged_receipts": acknowledged if transport else None,
        "duplicate_receipts": acknowledged - stored,
        "journal_mode": storage.connection.execute("PRAGMA journal_mode").fetchone()[0],
        "synchronous": storage.connection.execute("PRAGMA synchronous").fetchone()[0],
        "fullfsync": storage.connection.execute("PRAGMA fullfsync").fetchone()[0],
        "elapsed_seconds": elapsed,
        "observed_receipts_per_second": acknowledged / elapsed if elapsed else 0,
    }
    path.write_text(json.dumps(result, indent=2) + "\n")
