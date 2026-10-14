# Native development environment

The native M0–M3 path supports macOS and Linux. Install Git, Make, a C++20
compiler with pthread support (Apple Clang or GCC), and Python 3.11 or newer.
M4 also requires the OpenSSL CLI and development headers/libraries, plus a Python
SSL build supporting TLS 1.3. On macOS use `brew install openssl@3`; on Ubuntu
install `build-essential libssl-dev python3-venv`. Make detects Homebrew's
`openssl@3` prefix; set `OPENSSL_PREFIX` for another installation.
The ground station uses Python's standard-library SQLite implementation.

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
make doctor
make check
make test-sanitizers
make test-tsan
make test-security-sanitizers
```

Integration tests bind ephemeral loopback TCP ports and launch local child
processes. A restricted execution sandbox must permit those operations.
`make` selects `.venv/bin/python` when present; override `PYTHON` or `CXX` for
another interpreter or compiler. Sanitizer targets require the corresponding
compiler runtime; their availability depends on the platform/toolchain.

The verified local baseline uses Apple Clang on arm64 macOS and Python 3.14.6.
CI is configured to check the native suite on Ubuntu and macOS; local results
do not establish that the remote workflow has run. These native checks do not
require QEMU, a Linux guest, a kernel build, or a cross-compiler; those belong
to later embedded milestones. Run `make demo-native` for authenticated telemetry
with exact SQLite assertions. See the README for a two-terminal telemetry demo.
