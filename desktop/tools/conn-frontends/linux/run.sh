#!/usr/bin/env bash
# Linux half of the frontends connectivity harness: orbit-mpris on a real session bus, BLE
# unavailable, and the socket scenarios with Linux socket semantics.
#
#   desktop/tools/conn-frontends/linux/run.sh [pytest args...]
#
# Reuses the linux-e2e image; the repo is mounted read-only and copied inside the container.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../../.." && pwd)"
image=orbit-linux-e2e
docker build -q -t "$image" "$repo/desktop/tools/linux-e2e" >/dev/null
exec docker run --rm -v "$repo:/repo:ro" -v orbit-linux-e2e-uv-cache:/root/.cache/uv "$image" bash -c '
set -e
mkdir -p /work
tar -C /repo --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache -cf - desktop spec | tar -C /work -xf -
cd /work/desktop
UV_PROJECT_ENVIRONMENT=/venv uv sync --frozen --quiet --no-group qt
/venv/bin/python -m pip install -q pytest-xdist 2>/dev/null || uv pip install -q --python /venv/bin/python pytest-xdist
export PATH=/venv/bin:$PATH
exec dbus-run-session -- pytest -p no:cacheprovider "$@"
' bash "$@"
