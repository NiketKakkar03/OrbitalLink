#!/bin/sh
set -eu
rsync -a --delete --exclude=__pycache__ /input/guest/ /work/source/guest/
cd /work/source
exec python3 guest/run_gate.py --work /work/guest --output "$1" ${2:+--driver-tests}
