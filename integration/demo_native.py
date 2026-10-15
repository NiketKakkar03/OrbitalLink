"""Run authenticated native telemetry and verify its durable SQLite contents."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ground.dev_certificates import generate
from ground.orbit_storage import TelemetryStorage
from integration.run_checks import stop_process_group


def run_demo(flight: Path, output: Path, samples: int) -> dict:
    credentials = output / "certificates"
    generate(credentials)
    database = output / "telemetry.db"
    ready = output / "ready"
    logs = ExitStack()
    ground_stdout = logs.enter_context((output / "ground.stdout.log").open("wb"))
    ground_stderr = logs.enter_context((output / "ground.stderr.log").open("wb"))
    try:
        ground = subprocess.Popen([
            sys.executable, "-m", "ground.orbit_ground", "--database", str(database),
            "--ready-file", str(ready), "--max-receipts", str(samples),
            "--ca", str(credentials / "ca.pem"), "--cert", str(credentials / "ground.pem"),
            "--key", str(credentials / "ground.key"), "--allowed-client", "flight.orbital.test",
        ], cwd=ROOT, stdout=ground_stdout, stderr=ground_stderr, start_new_session=True)
    except BaseException:
        logs.close()
        raise
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if ground.poll() is not None:
                raise RuntimeError("ground failed to start; see ground.stderr.log")
            if time.monotonic() >= deadline:
                raise RuntimeError("ground readiness deadline exceeded")
            time.sleep(0.02)
        command = [str(flight), "127.0.0.1", ready.read_text().strip(),
                   "--outbox", str(output / "flight-outbox.db"),
                   "--count", str(samples), "--interval-ms", "0", "--capacity", str(samples),
                   "--drain-ms", "3000", "--ca", str(credentials / "ca.pem"),
                   "--cert", str(credentials / "flight.pem"), "--key", str(credentials / "flight.key"),
                   "--server-name", "ground.orbital.test"]
        with (output / "flight.json").open("w") as stdout_log, \
             (output / "flight.stderr.log").open("w") as stderr_log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=stdout_log,
                                       stderr=stderr_log, start_new_session=True)
            try:
                returncode = process.wait(timeout=max(15, samples / 100))
            finally:
                # Always stop the process group, including descendants left
                # behind by an exited parent, before closing its evidence logs.
                stop_process_group(process)
                process.wait(timeout=3)
            if returncode:
                raise RuntimeError(f"flight exited with {returncode}; see flight.stderr.log")
        summary = json.loads((output / "flight.json").read_text())
        ground.wait(timeout=12)
        if ground.returncode:
            raise RuntimeError(f"ground exited with {ground.returncode}; see ground.stderr.log")
        storage = TelemetryStorage(database)
        try:
            stored = storage.load_samples()
        finally:
            storage.close()
        if summary["generated"] != samples or summary["acknowledged"] != samples or len(stored) != samples:
            raise RuntimeError(f"telemetry count mismatch: {summary}; stored={len(stored)}")
        if {sample.telemetry_sequence for sample in stored} != set(range(1, samples + 1)):
            raise RuntimeError("SQLite telemetry sequences differ from expected samples")
        if len({sample.flight_boot_id for sample in stored}) != 1:
            raise RuntimeError("expected one boot identity in the native demo")
        report = {"status": "passed", "authenticated_samples": samples,
                  "database": str(database), "flight_summary": summary}
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        try:
            stop_process_group(ground)
            ground.wait(timeout=3)
        finally:
            logs.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flight", type=Path, default=Path("/tmp/orbit_flight_service"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100000:
        parser.error("samples must be in 1..100000")
    if not args.flight.resolve().is_file():
        parser.error("flight executable missing; run make test-cpp first")
    if args.output is None:
        base = ROOT / "build" / "native-demo"
        base.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    else:
        output = args.output.expanduser().resolve()
        try:
            output.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            parser.error(f"cannot create new demo directory: {error}")
    try:
        print(json.dumps(run_demo(args.flight.resolve(), output, args.samples), indent=2))
        print(f"Demo evidence: {output}")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        (output / "result.json").write_text(json.dumps({"status": "failed", "error": str(error)}, indent=2) + "\n")
        print(f"Demo failed: {error}\nEvidence: {output}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
