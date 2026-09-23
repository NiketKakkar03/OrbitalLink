# Development Environment

Recorded 2026-09-22 for the native M0-M4 development host.

| Component | Observed value |
| --- | --- |
| Operating system | macOS, Darwin 25.6.0 |
| CPU architecture | Apple ARM64 (`arm64e`), 8 logical CPUs |
| Memory | 16 GiB |
| Workspace filesystem | 460 GiB total, 104 GiB available when recorded |
| C++ compiler | Apple clang 21.0.0, target `arm64-apple-darwin25.6.0` |
| Python | 3.14.6 system interpreter; tests use the project venv or `uv` |
| uv | 0.12.0 |
| OpenSSL | 3.6.3 |
| Git | 2.54.0 |
| CMake | Not installed; native M0-M4 builds currently use the Makefile directly |

The native executables run on this ARM64 macOS host. A future ARM64 Linux guest
binary uses the same instruction family but a different operating-system ABI and
runtime, so it is built with the guest toolchain and run inside the guest. An ARM64
binary also cannot run as an ordinary x86 executable because the CPU instruction
sets differ unless an emulator or translation layer executes it.

## Fresh-shell verification

Prerequisites are a C++20 compiler, OpenSSL development headers/libraries, Python,
`uv`, Bash, and Make. On Apple Silicon Homebrew, OpenSSL defaults to
`/opt/homebrew`; override `OPENSSL_PREFIX`, `OPENSSL_CFLAGS`, and `OPENSSL_LIBS`
when installed elsewhere.

```bash
make check
make test-sanitizers
make test-tsan
git diff --check
```

Generated credentials belong under `.local/` or another temporary directory.
No generated key, certificate, disk image, database, object, or log is tracked.
