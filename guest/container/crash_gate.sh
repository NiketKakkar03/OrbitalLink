#!/bin/sh
set -eu
rsync -a --delete --exclude=__pycache__ /input/guest/ /work/source/guest/
cd /work/source
/work/guest/output/host/bin/aarch64-buildroot-linux-gnu-gcc \
    -O2 -std=c11 -Wall -Wextra -Werror guest/tools/outbox_probe.c -lsqlite3 \
    -o /work/evidence/orbit-outbox-probe
/work/guest/output/host/bin/aarch64-buildroot-linux-gnu-strip /work/evidence/orbit-outbox-probe
exec python3 guest/run_crash_gate.py --work /work/guest --output "$1" \
    --probe /work/evidence/orbit-outbox-probe
