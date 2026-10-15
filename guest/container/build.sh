#!/bin/sh
set -eu
# Run inside the ARM64 Linux builder; sources arrive as a read-only Mac bind.
mkdir -p /work/source
rsync -a --delete --exclude=.git --exclude=.venv --exclude=build --exclude=artifacts \
    --exclude=__pycache__ /input/ /work/source/
cd /work/source
python3 guest/guest.py doctor
python3 guest/guest.py --work /work/guest build --jobs "${ORBIT_BUILD_JOBS:-2}"
