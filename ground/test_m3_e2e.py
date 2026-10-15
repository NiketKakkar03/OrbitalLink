from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from ground.orbit_protocol import (
    TelemetryAck,
    TelemetrySample,
    encode_telemetry_ack,
    FrameParser,
)
from ground.orbit_storage import TelemetryStorage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def flight_service(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = PROJECT_ROOT / "flight" / "flight_service.cpp"
    if not source.exists():
        pytest.fail("flight/flight_service.cpp is required for the M3 recovery tests")
    output = tmp_path_factory.mktemp("m3-bin") / "flight_service"
    subprocess.run(
        [
            "c++",
            "-std=c++20",
            "-pthread",
            "-DORBIT_TEST_PLAINTEXT",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(source),
            str(PROJECT_ROOT / "flight" / "durable_outbox.cpp"),
            "-lsqlite3",
            "-o",
            str(output),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    return output


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def run_service(
    executable: Path,
    port: int,
    *,
    count: int,
    interval_ms: int = 0,
    capacity: int = 64,
    drain_ms: int = 1_000,
    timeout: float = 8,
) -> dict[str, int | str]:
    result = subprocess.run(
        [
            str(executable),
            "127.0.0.1",
            str(port),
            "--count",
            str(count),
            "--interval-ms",
            str(interval_ms),
            "--capacity",
            str(capacity),
            "--connect-timeout-ms",
            "50",
            "--io-timeout-ms",
            "75",
            "--drain-ms",
            str(drain_ms),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class FaultGround:
    """Small real TCP peer for deterministic reconnect and ACK fault tests."""

    def __init__(
        self,
        database: Path,
        *,
        expected_unique: int,
        first_ack: str = "normal",
        start_reading: threading.Event | None = None,
    ):
        self.database = database
        self.expected_unique = expected_unique
        self.first_ack = first_ack
        self.start_reading = start_reading
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.error: BaseException | None = None
        self.connections = 0
        self.frames = 0
        self._fault_used = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "FaultGround":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive(), "fault ground did not stop"
        if self.error:
            raise self.error

    def _run(self) -> None:
        storage = TelemetryStorage(self.database)
        try:
            while not self.stop.is_set():
                try:
                    connection, _ = self.listener.accept()
                except (TimeoutError, OSError):
                    continue
                self.connections += 1
                with connection:
                    connection.settimeout(0.1)
                    if self.start_reading is not None:
                        while not self.stop.is_set() and not self.start_reading.wait(0.02):
                            pass
                    parser = FrameParser()
                    close_for_fault = False
                    while not self.stop.is_set() and not close_for_fault:
                        try:
                            chunk = connection.recv(4096)
                        except TimeoutError:
                            continue
                        except OSError:
                            break
                        if not chunk:
                            break
                        for header, message in parser.feed(chunk):
                            assert isinstance(message, TelemetrySample)
                            storage.store_sample(message)
                            self.frames += 1
                            ack = encode_telemetry_ack(
                                TelemetryAck(
                                    message.spacecraft_id,
                                    message.flight_boot_id,
                                    message.telemetry_sequence,
                                ),
                                header.message_sequence,
                            )
                            if not self._fault_used and self.first_ack != "normal":
                                self._fault_used = True
                                if self.first_ack == "partial":
                                    connection.sendall(ack[:17])
                                close_for_fault = True
                                break
                            connection.sendall(ack)
                if storage.count_samples() >= self.expected_unique:
                    # Keep accepting briefly: the service may still be consuming
                    # the final ACK when the durable row becomes visible.
                    if self.stop.wait(0.1):
                        break
        except BaseException as error:
            if not self.stop.is_set():
                self.error = error
        finally:
            storage.close()


def stored_sequences(database: Path) -> list[int]:
    storage = TelemetryStorage(database)
    try:
        return [sample.telemetry_sequence for sample in storage.load_samples()]
    finally:
        storage.close()


def assert_final_accounting(counters: dict[str, int | str]) -> None:
    assert counters["generated"] == (
        counters["acknowledged"]
        + counters["dropped_oldest"]
        + counters["discarded_shutdown"]
    )
    assert counters["retained"] == 0


def test_restarted_service_uses_distinct_boot_identity(
    tmp_path: Path, flight_service: Path
):
    database = tmp_path / "restart.db"
    with FaultGround(database, expected_unique=8) as ground:
        for _ in range(2):
            counters = run_service(flight_service, ground.port, count=4)
            assert counters["acknowledged"] == 4
    storage = TelemetryStorage(database)
    try:
        samples = storage.load_samples()
        assert len(samples) == 8
        assert len({sample.flight_boot_id for sample in samples}) == 2
    finally:
        storage.close()


@pytest.mark.parametrize("fault", ["drop", "partial"])
def test_reconnects_after_lost_or_partial_ack_without_database_duplicates(
    tmp_path: Path, flight_service: Path, fault: str
):
    database = tmp_path / f"{fault}.db"
    with FaultGround(database, expected_unique=8, first_ack=fault) as ground:
        counters = run_service(flight_service, ground.port, count=8, interval_ms=1)

    assert stored_sequences(database) == list(range(1, 9))
    assert counters["generated"] == 8
    assert counters["acknowledged"] == 8
    assert counters["retransmissions"] >= 1
    assert ground.frames >= 9
    assert_final_accounting(counters)


def test_accelerated_30_second_logical_outage_recovers_without_loss(
    tmp_path: Path, flight_service: Path
):
    # At the default 10 Hz, 30 seconds means 300 samples.  Generate the same
    # workload at 1 ms/sample, then expose ground after acquisition completes.
    port = unused_port()
    database = tmp_path / "short-outage.db"
    process = subprocess.Popen(
        [
            str(flight_service), "127.0.0.1", str(port),
            "--count", "300", "--interval-ms", "1", "--capacity", "1024",
            "--connect-timeout-ms", "25", "--io-timeout-ms", "75", "--drain-ms", "2500",
        ],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(0.4)
    # FaultGround normally selects a port; replace its pre-bound listener so it
    # starts on the port the already-running flight process is retrying.
    ground = FaultGround(database, expected_unique=300)
    ground.listener.close()
    ground.listener = socket.socket()
    ground.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ground.listener.bind(("127.0.0.1", port))
    ground.listener.listen()
    ground.listener.settimeout(0.1)
    ground.port = port
    with ground:
        stdout, stderr = process.communicate(timeout=6)
        assert process.returncode == 0, stderr
        counters = json.loads(stdout)

    assert stored_sequences(database) == list(range(1, 301))
    assert counters["dropped_oldest"] == 0
    assert counters["acknowledged"] == 300
    assert counters["connect_failures"] >= 1
    assert_final_accounting(counters)


def test_longer_outage_drops_exact_oldest_samples_and_recovers_survivors(
    tmp_path: Path, flight_service: Path
):
    port = unused_port()
    database = tmp_path / "long-outage.db"
    process = subprocess.Popen(
        [
            str(flight_service), "127.0.0.1", str(port),
            "--count", "6", "--interval-ms", "5", "--capacity", "4",
            "--connect-timeout-ms", "25", "--io-timeout-ms", "75", "--drain-ms", "1500",
        ],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(0.12)
    ground = FaultGround(database, expected_unique=4)
    ground.listener.close()
    ground.listener = socket.socket()
    ground.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ground.listener.bind(("127.0.0.1", port))
    ground.listener.listen()
    ground.listener.settimeout(0.1)
    ground.port = port
    with ground:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        counters = json.loads(stdout)

    assert stored_sequences(database) == [3, 4, 5, 6]
    assert counters["generated"] == 6
    assert counters["dropped_oldest"] == 2
    assert counters["acknowledged"] == 4
    assert counters["discarded_shutdown"] == 0
    assert counters["gap_total_dropped"] == 2
    assert counters["gap_ranges"] == [[1, 2]]
    assert counters["gap_detail_complete"] is True
    assert_final_accounting(counters)


def test_continuous_connect_failures_and_full_queue_shutdown_are_bounded(
    flight_service: Path,
):
    started = time.monotonic()
    counters = run_service(
        flight_service,
        unused_port(),
        count=100,
        capacity=4,
        drain_ms=250,
        timeout=4,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert counters["generated"] == 100
    assert counters["dropped_oldest"] == 96
    assert counters["discarded_shutdown"] == 4
    assert counters["gap_total_dropped"] == 96
    assert counters["gap_ranges"] == [[1, 96]]
    assert counters["connect_failures"] >= 1
    assert counters["retained"] == 0
    assert_final_accounting(counters)


def test_stalled_peer_cannot_prevent_bounded_shutdown(
    tmp_path: Path, flight_service: Path
):
    capacity = 4
    allow_reads = threading.Event()
    database = tmp_path / "stalled.db"
    with FaultGround(
        database, expected_unique=1, start_reading=allow_reads
    ) as ground:
        started = time.monotonic()
        counters = run_service(
            flight_service,
            ground.port,
            count=200,
            capacity=capacity,
            drain_ms=250,
            timeout=4,
        )
        elapsed = time.monotonic() - started

    assert elapsed < 3
    assert counters["generated"] == 200
    # The plaintext fixture bounds queued samples and the single in-flight
    # frame separately. A stalled ACK can hold that frame while acquisition
    # refills all queue slots; scheduler timing determines whether this occurs.
    assert counters["discarded_shutdown"] <= capacity + 1
    assert counters["queue_high_watermark"] <= capacity
    assert counters["acknowledged"] == 0
    assert_final_accounting(counters)


def test_sigterm_during_reconnect_exits_within_three_seconds(flight_service: Path):
    process = subprocess.Popen(
        [
            str(flight_service), "127.0.0.1", str(unused_port()),
            "--count", "1000000", "--interval-ms", "10", "--capacity", "4",
            "--connect-timeout-ms", "50", "--io-timeout-ms", "75", "--drain-ms", "250",
        ],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(0.15)
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=3)
    assert time.monotonic() - started < 3
    assert process.returncode == 0, stderr
    counters = json.loads(stdout)
    assert counters["state"] == "stopping"
    assert counters["generated"] > 0
    assert counters["retained"] == 0
    assert_final_accounting(counters)


def test_sigterm_while_connected_to_stalled_peer_exits_within_three_seconds(
    tmp_path: Path, flight_service: Path
):
    allow_reads = threading.Event()
    with FaultGround(
        tmp_path / "signal-active.db", expected_unique=1, start_reading=allow_reads
    ) as ground:
        process = subprocess.Popen(
            [
                str(flight_service), "127.0.0.1", str(ground.port),
                "--count", "1000000", "--interval-ms", "10", "--capacity", "4",
                "--connect-timeout-ms", "50", "--io-timeout-ms", "75", "--drain-ms", "250",
            ],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 1
        while ground.connections == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ground.connections == 1
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)

    assert time.monotonic() - started < 3
    assert process.returncode == 0, stderr
    counters = json.loads(stdout)
    assert counters["state"] == "stopping"
    assert counters["acknowledged"] == 0
    assert counters["retained"] == 0
    assert_final_accounting(counters)


def terminate_and_read(process: subprocess.Popen[str]) -> tuple[dict[str, int | str], float]:
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=1)
        raise AssertionError("flight service exceeded the three-second shutdown deadline")
    elapsed = time.monotonic() - started
    assert process.returncode == 0, stderr
    return json.loads(stdout), elapsed


def test_sigterm_interrupts_long_acquisition_interval(flight_service: Path):
    process = subprocess.Popen(
        [
            str(flight_service), "127.0.0.1", str(unused_port()),
            "--count", "100", "--interval-ms", "10000", "--capacity", "4",
            "--connect-timeout-ms", "50", "--io-timeout-ms", "75", "--drain-ms", "100",
        ],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(0.1)
    counters, elapsed = terminate_and_read(process)
    assert elapsed < 3
    assert 0 < counters["generated"] < 100
    assert_final_accounting(counters)


def test_sigterm_interrupts_mature_multi_second_backoff(flight_service: Path):
    process = subprocess.Popen(
        [
            str(flight_service), "127.0.0.1", str(unused_port()),
            "--count", "1000000", "--interval-ms", "1", "--capacity", "4",
            "--connect-timeout-ms", "25", "--io-timeout-ms", "75", "--drain-ms", "3000",
        ],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # 250 + 500 + 1000 + 2000 ms places the retry policy in a delay whose
    # nominal duration exceeds the entire shutdown budget.
    time.sleep(4.2)
    counters, elapsed = terminate_and_read(process)
    assert elapsed < 3
    assert counters["connect_failures"] >= 4
    assert_final_accounting(counters)


def test_sigterm_interrupts_long_io_deadline(
    tmp_path: Path, flight_service: Path
):
    allow_reads = threading.Event()
    with FaultGround(
        tmp_path / "long-io.db", expected_unique=1, start_reading=allow_reads
    ) as ground:
        process = subprocess.Popen(
            [
                str(flight_service), "127.0.0.1", str(ground.port),
                "--count", "1000000", "--interval-ms", "1", "--capacity", "4",
                "--connect-timeout-ms", "50", "--io-timeout-ms", "10000", "--drain-ms", "100",
            ],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 1
        while ground.connections == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ground.connections == 1
        counters, elapsed = terminate_and_read(process)

    assert elapsed < 3
    assert counters["acknowledged"] == 0
    assert_final_accounting(counters)


def test_repeated_early_sigterm_does_not_race_queue_close(flight_service: Path):
    for _attempt in range(20):
        ready_read, ready_write = os.pipe()
        process = subprocess.Popen(
            [
                str(flight_service), "127.0.0.1", str(unused_port()),
                "--count", "1000000", "--interval-ms", "0", "--capacity", "4",
                "--connect-timeout-ms", "25", "--io-timeout-ms", "25", "--drain-ms", "0",
                "--ready-fd", str(ready_write),
            ],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            pass_fds=(ready_write,),
        )
        os.close(ready_write)
        assert os.read(ready_read, 1) == b"R"
        os.close(ready_read)
        counters, elapsed = terminate_and_read(process)
        assert elapsed < 3
        assert_final_accounting(counters)
