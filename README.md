# OrbitalLink

OrbitalLink is a small embedded telemetry platform that models how a spacecraft
could collect sensor readings, keep them safe during a network outage, and send
them to a ground station.

The project runs in two ways:

- **Native mode** runs the flight software and ground station directly on macOS
  or Linux. This is the quickest way to try the project.
- **Embedded mode** boots an ARM64 Linux guest in QEMU. A custom virtual sensor
  device raises interrupts, a Linux driver reads the measurements, and the
  flight software sends them to the ground station.

OrbitalLink uses simulated measurements and emulated hardware. It is an
educational embedded-systems project, not flight-qualified spacecraft software.

## Project overview

The system has two main sides:

1. The **flight side** collects power, temperature, orientation, health,
   synthetic radiation, and event reports. It stores unsent reports in a
   bounded SQLite outbox and sends them over an authenticated connection.
2. The **ground side** checks each message, saves it in SQLite, and returns an
   acknowledgement (ACK) only after the database commit succeeds.

If the connection fails, the flight service reconnects and sends the retained
reports again. Ground uses each report's identity to avoid storing duplicates.
This provides retryable delivery while keeping memory and local storage bounded.

The system also supports authenticated commands from ground, including enabling
or disabling the sensor source, changing its reporting period, resetting it,
clearing faults, and reading its current state.

### What has been validated

- A native integration test delivered 10,000 reports and replayed 100 duplicate
  transmissions while ground stored exactly 10,000 unique rows.
- The full QEMU device-to-ground path completed a 10-minute, 10 Hz run with
  6,060 reports and no driver or outbox drops.
- Abrupt VM power-cut tests verified that committed reports survived reboot and
  were replayed with byte-identical contents.
- Native tests run on macOS and Ubuntu and include protocol, storage, outage,
  TLS, command, concurrency, and sanitizer checks.

See [sensor validation](docs/sensor-validation.md) for the measured results and
their limits.

## Technology stack

| Area | Tools and technologies | Purpose |
|---|---|---|
| Flight application | C++20, threads, SQLite | Collect, retain, and transmit telemetry |
| Ground station | Python, SQLite | Receive, validate, store, and inspect telemetry |
| Security | TLS 1.3, OpenSSL, mutual TLS | Authenticate both flight and ground |
| Embedded platform | ARM64 Linux, Buildroot | Build and run the small guest system |
| Virtual hardware | QEMU, MMIO, interrupts | Emulate a multisensor hardware device |
| Linux interface | C kernel driver, character device, `read`/`poll`/`ioctl` | Move reports and commands between hardware and flight software |
| Testing | Pytest, C/C++ tests, ASan, UBSan, TSan | Check behavior under normal and failure conditions |
| Development | Make, Git, Docker | Build and reproduce the project |

## Architecture

The complete embedded path is:

```text
Custom QEMU sensor device
        |
        | MMIO registers and interrupts
        v
Linux character driver
        |
        | read, poll, and ioctl
        v
C++ flight service
  - acquisition thread
  - bounded SQLite outbox
  - network thread
        |
        | mutual TLS 1.3 over TCP
        v
Python ground station
        |
        | validate -> SQLite commit -> ACK
        v
Ground telemetry database
```

The flight service separates sensor collection from networking. A slow or
unavailable network therefore does not immediately stop collection. Before a
report is sent, it is committed to the local outbox. It is removed only after
ground confirms that the same report was committed successfully.

Storage is intentionally bounded. If an outage lasts long enough to fill the
outbox, the system follows a defined priority and eviction policy and records
the loss instead of allowing memory or disk use to grow without a limit. See
the [retention policy](docs/retention.md) for details.

## Run the project locally

The native demo is the recommended starting point. It exercises the real C++
flight service, Python ground station, TLS connection, durable outbox, ACKs, and
SQLite storage. It does not boot QEMU or use the Linux kernel driver.

### 1. Install the prerequisites

You need:

- Git and Make
- Python 3.11 or newer
- A C++20 compiler (Apple Clang or GCC)
- OpenSSL 3 development files
- SQLite development files

On macOS with Homebrew:

```bash
brew install openssl@3 sqlite
```

On Ubuntu or Debian:

```bash
sudo apt update
sudo apt install build-essential libssl-dev libsqlite3-dev python3-venv
```

### 2. Clone the repository and create the Python environment

```bash
git clone https://github.com/NiketKakkar03/OrbitalLink.git
cd OrbitalLink
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows users should run the project through WSL2 or a Linux environment.

### 3. Check the local setup

```bash
make doctor
```

The command reports the detected compiler, Python, SQLite, OpenSSL, and any
missing prerequisite.

### 4. Run the automated authenticated demo

```bash
make demo-native
```

The demo:

1. Compiles the C++ flight service.
2. Creates short-lived development certificates.
3. Starts the ground station and flight service.
4. Sends 100 reports over mutual TLS 1.3.
5. Verifies that all 100 reports were acknowledged and stored exactly once.
6. Saves the database, logs, and result file under `build/native-demo/run-*`.

At the end, copy the evidence directory printed after `Demo evidence:`. For
example, if it prints `build/native-demo/run-example`, inspect that run with:

```bash
.venv/bin/python -m ground.orbit_ground \
  --database build/native-demo/run-example/telemetry.db \
  --status
```

Display the five most recent reports with a separate command:

```bash
.venv/bin/python -m ground.orbit_ground \
  --database build/native-demo/run-example/telemetry.db \
  --tail 5
```

Replace `run-example` with the directory created on your machine. Run each code
block separately; do not append an inspection command to `make demo-native`.

### 5. Run the complete native test suite

```bash
make check
```

Additional concurrency and memory-safety checks are available separately:

```bash
make test-sanitizers
make test-tsan
make test-security-sanitizers
```

Sanitizer availability depends on the compiler and operating system. See the
[development environment guide](docs/environment.md) for platform notes.

## Run a simple two-terminal example

This smaller example uses an explicit local plaintext test mode so the data flow
is easy to observe. Production-style flight builds use TLS and do not silently
fall back to plaintext.

In the first terminal, start ground:

```bash
.venv/bin/python -m ground.orbit_ground \
  --test-plaintext \
  --database /tmp/orbit-telemetry.db \
  --port 9000
```

In a second terminal, compile and run the deterministic sample sender:

```bash
c++ -std=c++20 -Wall -Wextra -Werror \
  flight/send_sample.cpp -o /tmp/orbit-send-sample
/tmp/orbit-send-sample 127.0.0.1 9000 10
```

Stop ground with `Ctrl-C`, then inspect the stored reports:

```bash
.venv/bin/python -m ground.orbit_ground \
  --database /tmp/orbit-telemetry.db \
  --status

.venv/bin/python -m ground.orbit_ground \
  --database /tmp/orbit-telemetry.db \
  --tail 5
```

## Run the ARM64/QEMU version

The embedded setup is optional and considerably heavier than the native demo.
It uses Docker Desktop as an ARM64 Linux builder, compiles Buildroot, Linux,
QEMU, the driver, and the flight service, and then boots a separate ARM64 guest.
The first build requires substantial time and disk space.

Start with the [ARM64 guest setup](docs/guest.md). After the image is built, the
embedded validation runner can boot the virtual hardware and verify the complete
device-to-ground path. Use a new output directory for every evidence run:

```bash
python3 guest/run_m7_gate.py \
  --work /work/guest \
  --output /work/evidence/my-m7-run \
  --ticks 6000 \
  --period-ms 100 \
  --seed 1 \
  --storage-batch 16
```

This is a 10-minute measurement run in addition to guest boot and shutdown time.
The native demo is better for a quick introduction; the QEMU gate is the proof
that the custom device, Linux driver, and ARM64 flight image work together.

## Project documentation

- [Complete project walkthrough](docs/project-overview.md)
- [Native development environment](docs/environment.md)
- [Security and authenticated commands](docs/security.md)
- [ARM64 guest setup](docs/guest.md)
- [Linux driver](docs/driver.md)
- [Wire protocol](protocol/specification.md)
- [Sensor contract](protocol/sensor-contract.md)
- [Retention and outage behavior](docs/retention.md)
- [Validation evidence](docs/validation.md)
- [Multisensor measurements](docs/sensor-validation.md)

## Scope and limitations

OrbitalLink demonstrates embedded data flow, driver interfaces, durable retry,
authenticated networking, and failure testing. It does not connect to a
physical spacecraft, radio, or sensor board. Sensor values are deterministic
examples rather than calibrated physics, and the custom network protocol is not
CCSDS. Production deployment would also require operational certificate
management, dependency patch auditing, database archival, and hardware-specific
qualification.
