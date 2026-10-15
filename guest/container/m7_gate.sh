#!/bin/sh
set -eu
rsync -a --delete --exclude=__pycache__ /input/guest/ /work/source/guest/
cd /work/source
exec python3 guest/run_m7_gate.py --work /work/guest --output "$1" \
    --ticks "${2:-6000}" --period-ms "${3:-100}" --seed "${4:-1}"
