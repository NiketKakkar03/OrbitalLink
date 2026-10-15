#!/bin/sh
# SPDX-License-Identifier: GPL-2.0
# Execute as root in a disposable, freshly booted pinned Linux guest.
set -eu
module=${1:-./orbit_test.ko}
harness=${2:-./orbit-syscall-contract}
cycles=${3:-100}
artifacts=${4:-./driver-test-artifacts}
case "$cycles" in ''|*[!0-9]*) echo 'cycles must be a positive integer' >&2; exit 2;; esac
[ "$cycles" -ge 1 ] || exit 2
mkdir -p "$artifacts"
uname -a > "$artifacts/uname.txt"
dmesg > "$artifacts/dmesg-before.txt"
cleanup() {
	status=$?
	trap - EXIT HUP INT TERM
	# Preserve diagnostics even when the harness or a lifecycle operation fails.
	dmesg > "$artifacts/dmesg-after.txt" 2>&1 || true
	rmmod orbit_test 2>/dev/null || true
	exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
insmod "$module" initial_period_ms=60000
"$harness" > "$artifacts/syscalls.txt" 2>&1
rmmod orbit_test
i=1
while [ "$i" -le "$cycles" ]; do
	insmod "$module" initial_period_ms=60000
	"$harness" --detach >> "$artifacts/lifecycle.txt" 2>&1
	# Rebind demonstrates that old device allocation can be freed on close.
	printf '%s' orbit-test > /sys/bus/platform/drivers/orbit-test/bind
	"$harness" --detach >> "$artifacts/lifecycle.txt" 2>&1
	rmmod orbit_test
	i=$((i + 1))
done
dmesg > "$artifacts/dmesg-after.txt"
# On a clean test guest, any of these diagnostics fails the gate.
if grep -E 'BUG:|WARNING:|KASAN:|possible circular locking|kernel NULL pointer|use-after-free' "$artifacts/dmesg-after.txt" > "$artifacts/kernel-errors.txt"; then
	echo 'Kernel diagnostic found; inspect artifacts' >&2
	exit 1
fi
printf 'PASS: syscall suite and %s load/unbind/rebind/unload cycles\n' "$cycles"
