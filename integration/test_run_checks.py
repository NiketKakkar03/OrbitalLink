from __future__ import annotations

import json
from pathlib import Path
import sys
import signal
import time

from integration import run_checks


def test_failure_is_retained_in_evidence(tmp_path: Path):
    log = tmp_path / "failure.log"
    result = run_checks.run_command(
        [sys.executable, "-c", "print('assertion failed'); raise SystemExit(7)"], log,
    )
    assert result["status"] == "failed"
    assert result["returncode"] == 7
    assert "assertion failed" in log.read_text()


def test_large_output_is_drained_but_retention_is_bounded(tmp_path: Path):
    log = tmp_path / "large.log"
    result = run_checks.run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
        log, max_log_bytes=1024,
    )
    assert result["status"] == "passed"
    assert result["output_bytes"] == 1000000
    assert result["log_truncated"]
    assert log.stat().st_size == 1024


def test_timeout_stops_child_process(tmp_path: Path):
    marker = tmp_path / "should_not_exist"
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    result = run_checks.run_command(
        [sys.executable, "-c", parent], tmp_path / "timeout.log", timeout=0.2,
    )
    assert result["status"] == "timed_out"
    assert not marker.exists()


def test_exited_parent_with_live_descendant_is_not_passed(tmp_path: Path):
    parent = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
    result = run_checks.run_command(
        [sys.executable, "-c", parent], tmp_path / "descendant.log", timeout=0.2,
    )
    assert result["status"] == "timed_out"


def test_timeout_escalates_when_descendant_ignores_sigterm(tmp_path: Path):
    marker = tmp_path / "escaped"
    child = (
        "import signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"print('ready', flush=True); time.sleep(3); Path({str(marker)!r}).touch()"
    )
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    result = run_checks.run_command(
        [sys.executable, "-c", parent], tmp_path / "forced.log", timeout=0.3,
    )
    assert result["status"] == "timed_out"
    assert "ready" in (tmp_path / "forced.log").read_text()
    # The marker is delayed past the cleanup deadline. Checking immediately
    # would also pass if the resistant descendant were still running.
    time.sleep(2)
    assert not marker.exists()


def test_timeout_forces_sigkill_for_resistant_group_leader(tmp_path: Path):
    result = run_checks.run_command(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
         "print('ready', flush=True); time.sleep(30)"],
        tmp_path / "leader.log", timeout=0.3,
    )
    assert result["status"] == "timed_out"
    assert result["returncode"] == -signal.SIGKILL
    assert result["duration_seconds"] < 4


def test_manifest_records_failure_and_continues_other_suites(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(run_checks, "SUITES", {
        "bad": [sys.executable, "-c", "raise SystemExit(2)"],
        "good": [sys.executable, "-c", "print('passed')"],
    })
    output = tmp_path / "run"
    assert run_checks.main(["--suite", "bad", "--suite", "good", "--output", str(output)]) == 1
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert [check["status"] for check in manifest["checks"]] == ["failed", "passed"]
    assert "source_revision" in manifest


def test_missing_executable_is_a_failed_check(tmp_path: Path):
    result = run_checks.run_command([str(tmp_path / "absent")], tmp_path / "missing.log")
    assert result["status"] == "failed"
    assert "Could not start check" in (tmp_path / "missing.log").read_text()


def test_start_failure_log_obeys_retention_limit(tmp_path: Path):
    log = tmp_path / "missing.log"
    result = run_checks.run_command([str(tmp_path / "absent")], log, max_log_bytes=8)
    assert result["returncode"] is None
    assert result["retained_bytes"] == log.stat().st_size == 8
    assert result["log_truncated"]


def test_manifest_records_timeout_and_deduplicates_suites(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(run_checks, "SUITES", {
        "slow": [sys.executable, "-c", "import time; time.sleep(30)"],
    })
    output = tmp_path / "run"
    assert run_checks.main([
        "--suite", "slow", "--suite", "slow", "--timeout", "0.1",
        "--output", str(output),
    ]) == 1
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["parameters"]["suites"] == ["slow"]
    assert len(manifest["checks"]) == 1
    check = manifest["checks"][0]
    assert check["status"] == "timed_out"
    assert check["retained_bytes"] == (output / check["log"]).stat().st_size
    assert manifest["finished_at_utc"] >= manifest["started_at_utc"]


def test_interrupted_manifest_is_finalized(tmp_path: Path, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_checks, "run_command", interrupt)
    output = tmp_path / "interrupted"
    assert run_checks.main(["--output", str(output)]) == 130
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "interrupted"
    assert "finished_at_utc" in manifest


def test_suite_deadlines_and_explicit_override_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(run_checks, "SUITES", {name: ["unused"] for name in ("native", "stress", "security")})
    seen=[]
    def complete(command, log_path, **kwargs):
        seen.append(kwargs["timeout"])
        log_path.write_text("passed\n")
        return {"status": "passed", "log": log_path.name}
    monkeypatch.setattr(run_checks, "run_command", complete)
    output=tmp_path/"defaults"
    assert run_checks.main(["--suite", "native", "--suite", "stress", "--suite", "security", "--output", str(output)])==0
    assert seen==[900,1800,300]
    manifest=json.loads((output/"manifest.json").read_text())
    assert manifest["parameters"]["suite_timeouts_seconds"]=={"native":900,"stress":1800,"security":300}
    seen.clear()
    assert run_checks.main(["--suite", "native", "--suite", "stress", "--timeout", "12", "--output", str(tmp_path/"override")])==0
    assert seen==[12,12]
