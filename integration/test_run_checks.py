from __future__ import annotations

import json
from pathlib import Path
import sys

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
