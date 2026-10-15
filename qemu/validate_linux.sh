#!/bin/sh
set -eu
cd /work/source
make -C /work/guest/output/build/host-qemu-9.2.0 -j2
qtest_run=$(mktemp -d /work/evidence/m7-qtest-run.XXXXXX)
python3 qemu/test_mmio.py --qemu /work/guest/output/build/host-qemu-9.2.0/build/qemu-system-aarch64 \
    --output "$qtest_run/qtest"
printf 'QTEST_EVIDENCE=%s\n' "$qtest_run/qtest"
make -C driver module KDIR=/work/guest/output/build/linux-6.12.27 \
    ARCH=arm64 CROSS_COMPILE=/work/guest/output/host/bin/aarch64-buildroot-linux-gnu-
/work/guest/output/host/bin/aarch64-buildroot-linux-gnu-gcc \
    -std=c11 -Wall -Wextra -Werror -I driver driver/tests/sensors_contract.c \
    -o /work/evidence/orbit-sensors-contract
printf 'PASS: actual QEMU MMIO qtests and Linux ABI2 cross-compilation\n'
