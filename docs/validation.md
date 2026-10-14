# Retained validation evidence

Run milestone checks through the evidence runner from the repository root:

```sh
.venv/bin/python integration/run_checks.py --suite native --suite sanitizers --suite tsan
```

Each run creates a new directory under `build/validation/`, with a JSON manifest
and one combined stdout/stderr log per suite. The manifest records the source
revision, dirty working-tree paths, host/interpreter information, selected
commands, deadlines, exit codes, durations, and explicit pass/fail/timeout status.
Check logs report compiler and pytest details. The runner continues independent
suites after a failure and returns failure if any selected suite fails.

Use `--suite security` for the M4 security target once it is present. A missing
target fails; it is never silently treated as a successful or skipped check.
`--suite security-sanitizers` runs the real TLS and command tests with the
production C++ service instrumented by AddressSanitizer and
UndefinedBehaviorSanitizer. The separate `sanitizers` and `tsan` suites also
exercise concurrent command policy and the bounded recovery fixture.
`--output /path/to/new/run` selects a new evidence directory. Existing directories
are refused to protect earlier results. `--timeout 300` sets a per-suite deadline.
Output is drained continuously, but retained logs are capped at 10 MiB each by
default, including launch-failure diagnostics; the manifest marks truncation and
records both byte counts. A command that cannot start has a null exit code and an
explicit error. Timeout and interruption stop the command's process group,
including child processes, escalating from SIGTERM to SIGKILL after one second.
A parent exiting while its descendants still hold the output pipe is a timeout,
not a passed check. Commands must keep their descendants in the inherited process
group for this cleanup contract to apply.

This is the beginning of M8 evidence capture for native checks. It does not
implement the complete guest/device fault matrix or benchmarks. Guest logs,
database exports, scenario seeds, timing calibration, and QEMU/kernel revision
manifests must be added to the embedded harness before M8 can pass its gate.

Check the runner's own failure and cleanup behavior with:

```sh
.venv/bin/python -m pytest integration/test_run_checks.py
```
