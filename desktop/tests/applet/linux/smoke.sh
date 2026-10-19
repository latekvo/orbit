#!/usr/bin/env bash
# Start Xvfb, a tray host and the mock daemon, then run orbit-applet with the panel shown.
set -euo pipefail
OUT=${OUT:-/out}
mkdir -p "$OUT"
export DISPLAY=:99 QT_QPA_PLATFORM=xcb ORBIT_SOCKET=/tmp/orbit/orbitd.sock
Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
sleep 1
stalonetray --geometry 4x1+1100+0 --icon-size 24 --background '#303030' >/dev/null 2>&1 &
uv run --no-sync python tests/applet/mock_orbitd.py --socket "$ORBIT_SOCKET" > "$OUT/mock.log" 2>&1 &
MOCK=$!
for _ in $(seq 50); do [ -S "$ORBIT_SOCKET" ] && break; sleep 0.1; done
( sleep 4; import -window root "$OUT/linux-desktop.png" ) &
uv run --no-sync orbit-applet --show-panel --quit-after 6 -v 2>&1 | tee "$OUT/applet.log"
status=${PIPESTATUS[0]}
wait
kill $MOCK || true
echo "orbit-applet exit status: $status"
exit "$status"
