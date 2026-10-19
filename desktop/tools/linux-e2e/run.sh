#!/usr/bin/env bash
# Linux E2E for orbit-mpris, run in Docker from any host.
#
#   desktop/tools/linux-e2e/run.sh              # MODE=stub: stub orbitd from this directory
#   MODE=real desktop/tools/linux-e2e/run.sh    # real orbitd + orbit-fakephone
#
# The repo is mounted read-only and copied inside the container, so host virtualenvs are
# never touched. Exit status is the number of failed checks (0 = all passed).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
image=orbit-linux-e2e
docker build -q -t "$image" "$here" >/dev/null
exec docker run --rm -e MODE="${MODE:-stub}" \
    -v "$repo:/repo:ro" -v orbit-linux-e2e-uv-cache:/root/.cache/uv \
    "$image" bash /repo/desktop/tools/linux-e2e/e2e.sh
