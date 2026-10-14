"""Run milestone checks with bounded logs, deadlines, and retained evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "native": ["make", "check"],
    "sanitizers": ["make", "test-sanitizers"],
    "tsan": ["make", "test-tsan"],
    "security": ["make", "test-security"],
    "security-sanitizers": ["make", "test-security-sanitizers"],
}


def stop_process_group(process: subprocess.Popen, grace_seconds: float = 1.0) -> None:
    """Stop descendants too, including those left by an exited parent."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # Reap the direct child before signalling again. On macOS an unreaped dead
    # group leader can otherwise make the second killpg report EPERM.
    deadline = time.monotonic() + grace_seconds
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(max(0.0, deadline - time.monotonic()))
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_command(
    command: list[str],
    log_path: Path,
    *,
    cwd: Path = ROOT,
    timeout: float = 300,
    max_log_bytes: int = 10 * 1024 * 1024,
) -> dict:
    """Drain output even after its retention limit; fail closed on timeout."""
    started = time.monotonic()
    result = {"command": command, "log": log_path.name, "status": "failed"}
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        diagnostic = f"Could not start check: {error}\n".encode()
        retained = diagnostic[:max_log_bytes]
        log_path.write_bytes(retained)
        return {
            **result, "error": str(error), "returncode": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "output_bytes": len(diagnostic), "retained_bytes": len(retained),
            "log_truncated": len(diagnostic) > len(retained), "log_errors": [],
        }

    assert process.stdout is not None
    output_bytes = 0
    retained_bytes = 0
    reader_errors: list[str] = []

    def drain() -> None:
        nonlocal output_bytes, retained_bytes
        try:
            with log_path.open("wb") as log:
                while chunk := process.stdout.read(65536):
                    output_bytes += len(chunk)
                    retain = chunk[:max(0, max_log_bytes - retained_bytes)]
                    log.write(retain)
                    retained_bytes += len(retain)
        except OSError as error:
            reader_errors.append(str(error))

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
        reader.join(timeout=max(0.0, timeout - (time.monotonic() - started)))
        # A descendant holding stdout open is also an unfinished check.
        timed_out = reader.is_alive()
    except subprocess.TimeoutExpired:
        timed_out = True
    except BaseException:
        stop_process_group(process)
        process.wait()
        reader.join(timeout=2)
        raise
    if timed_out:
        stop_process_group(process)
        process.wait()
        reader.join(timeout=2)
    process.stdout.close()
    return {
        **result,
        "status": "timed_out" if timed_out else (
            "passed" if process.returncode == 0 and not reader_errors else "failed"
        ),
        "returncode": process.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_bytes": output_bytes,
        "retained_bytes": retained_bytes,
        "log_truncated": output_bytes > retained_bytes,
        "log_errors": reader_errors,
    }


def git_value(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=ROOT, capture_output=True, text=True,
            timeout=5, check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def write_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", choices=SUITES, help="Repeat to select checks; default native")
    parser.add_argument("--output", type=Path, help="New run directory; existing paths are refused")
    parser.add_argument("--timeout", type=float, default=300, help="Deadline in seconds per suite")
    parser.add_argument("--max-log-bytes", type=int, default=10 * 1024 * 1024)
    args = parser.parse_args(argv)
    if not 0 < args.timeout < float("inf") or args.max_log_bytes < 1:
        parser.error("timeout must be positive and finite; max-log-bytes must be positive")
    suites = list(dict.fromkeys(args.suite or ["native"]))
    now = dt.datetime.now(dt.timezone.utc)
    output = args.output or ROOT / "build" / "validation" / (
        now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        parser.error(f"cannot create a new run directory: {error}")
    manifest = {
        "schema_version": 1, "started_at_utc": now.isoformat(),
        "source_revision": git_value("rev-parse", "HEAD"),
        "working_tree_status": git_value("status", "--short"),
        "host": {"platform": platform.platform(), "machine": platform.machine(),
                 "python": sys.version, "python_executable": sys.executable},
        "parameters": {"suites": suites, "timeout_seconds": args.timeout,
                       "max_log_bytes": args.max_log_bytes},
        "status": "running", "checks": [],
    }
    manifest_path = output / "manifest.json"
    write_manifest(manifest_path, manifest)
    try:
        for suite in suites:
            print(f"Running {suite}; log: {output / (suite + '.log')}", flush=True)
            result = run_command(
                SUITES[suite], output / (suite + ".log"), timeout=args.timeout,
                max_log_bytes=args.max_log_bytes,
            )
            manifest["checks"].append({"suite": suite, **result})
            write_manifest(manifest_path, manifest)
            print(f"{suite}: {result['status']}", flush=True)
        manifest["status"] = "passed" if all(
            check["status"] == "passed" for check in manifest["checks"]
        ) else "failed"
    except (KeyboardInterrupt, SystemExit):
        manifest["status"] = "interrupted"
        return 130
    finally:
        if manifest["status"] == "running":
            manifest["status"] = "failed"
        manifest["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_manifest(manifest_path, manifest)
        print(f"Evidence: {manifest_path}", flush=True)
    return 0 if manifest["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
