# Session 001: Environment and learning loop

## Question

Can a fresh development shell compile the native C++ components, import and test
the Python components, and report failures consistently without tracking outputs?

## Prediction

A warnings-as-errors native build plus pytest provides a small repeatable baseline.
CI should invoke the same public Make targets used locally.

## Change

- Added the C++ and Python temperature conversion implementations and tests.
- Added warnings-as-errors C++ builds and Python test entry points.
- Recorded the native host and tool versions in `docs/environment.md`.
- Added ignore rules for build outputs, virtual environments, credentials, logs,
  databases, and local generated state.
- Added basic CI for regression and sanitizer targets.

## Test command

```bash
make check
make test-sanitizers
make test-tsan
git diff --check
```

## Observed result

The current native suite builds and passes on the recorded ARM64 macOS host. The
CI workflow is configured for Ubuntu and must produce its own run evidence when
the branch is published. Local failure propagation is inherent in Make and pytest;
the workflow also fails on a nonzero command status.

## Explanation

The executable produced here targets the current host. The later ARM64 Linux
executable must use the guest ABI, sysroot, and libraries and run in QEMU even
when the development host also has an ARM64 CPU.

## Next step

Keep the public build/test targets stable as protocol, storage, concurrency, and
security behavior are added.
