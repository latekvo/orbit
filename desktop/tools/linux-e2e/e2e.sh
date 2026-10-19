#!/usr/bin/env bash
# In-container half of run.sh: installs the package from the read-only /repo mount, runs the
# test suite and then the MODE (stub|real) scenario inside a private D-Bus session.
set -uo pipefail

if [ "${1:-}" != --in-session ]; then
    set -e
    mkdir -p /work
    tar -C /repo --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache \
        -cf - desktop spec | tar -C /work -xf -
    cd /work/desktop
    UV_PROJECT_ENVIRONMENT=/venv uv sync --frozen --quiet --no-group qt
    exec dbus-run-session -- bash "$0" --in-session
fi

export PATH=/venv/bin:$PATH
W=/tmp/e2e
mkdir -p "$W"
SOCK=$W/orbitd.sock
export ORBIT_SOCKET=$SOCK XDG_CACHE_HOME=$W/cache
HERE=/work/desktop/tools/linux-e2e
MEDIA=media:com.spotify.music
FAILS=0
PIDS=()
trap 'kill "${PIDS[@]}" 2>/dev/null; wait 2>/dev/null' EXIT

say() { printf '\n== %s\n' "$*"; }
run() { printf '$ %s\n' "$*"; "$@"; }
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; FAILS=$((FAILS + 1)); }
check() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; fi; }
# eventually DESC CMD...: retry CMD for up to 10 s.
eventually() {
    local d=$1; shift
    for _ in $(seq 50); do "$@" >/dev/null 2>&1 && { pass "$d"; return 0; }; sleep 0.2; done
    fail "$d"; return 1
}
orbit_names() { busctl --user list --no-pager --no-legend | awk '{print $1}' | grep '^org\.mpris\.MediaPlayer2\.orbit_' || true; }
has_player() { [ -n "$(orbit_names)" ]; }
no_player() { [ -z "$(orbit_names)" ]; }
meta() { playerctl -p "$P" metadata "$1" 2>/dev/null; }
meta_is() { [ "$(meta "$1")" = "$2" ]; }
status_is() { [ "$(playerctl -p "$P" status 2>/dev/null)" = "$1" ]; }
art_file() { local u; u=$(meta mpris:artUrl) || return 1; [ -n "$u" ] && printf '%s' "${u#file://}"; }
art_is() { local f; f=$(art_file) && [ -f "$f" ] && [ "$(sha256sum <"$f" | cut -c1-16)" = "$1" ]; }
pos_between() { python3 -c "import sys; p=float(sys.argv[1]); sys.exit(not $1 <= p <= $2)" "$(playerctl -p "$P" position)"; }
# monitor_has MEMBER TEXT: some MEMBER signal seen by dbus-monitor contains TEXT.
monitor_has() { awk -v m="member=$1" '/^signal /{on = index($0, m) > 0} on' "$W/monitor.log" | grep -qF "$2"; }

start_mpris() {
    orbit-mpris -v >>"$W/mpris.log" 2>&1 &
    MPRIS_PID=$!
    PIDS+=("$MPRIS_PID")
}

start_monitor() {
    dbus-monitor --session "type='signal',path='/org/mpris/MediaPlayer2'" >"$W/monitor.log" 2>&1 &
    PIDS+=("$!")
    sleep 0.3
}

say "no session bus -> clear error, non-zero exit"
out=$(DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent orbit-mpris 2>&1); rc=$?
printf '%s (exit %s)\n' "$out" "$rc"
check "exits 1 with a message" sh -c "[ $rc -eq 1 ] && printf '%s' \"\$1\" | grep -q 'no D-Bus session bus'" _ "$out"

say "full test suite (Linux, real session bus)"
(cd /work/desktop && run pytest -q -rs tests) && pass "pytest" || fail "pytest"

scenario_stub() {
    STUB="python3 $HERE/stub_orbitd.py"
    ctl() { $STUB ctl --socket "$SOCK" "$@"; }
    start_stub() { $STUB serve --socket "$SOCK" >>"$W/stub.log" 2>&1 & STUB_PID=$!; PIDS+=("$STUB_PID"); }
    # last_call_is JSON: the newest activities.action the stub received equals JSON.
    last_call_is() { ctl stub.calls | python3 -c 'import json,sys; c=json.load(sys.stdin); sys.exit(not c or c[-1]!=json.loads(sys.argv[1]))' "$1"; }
    show_last_call() { ctl stub.calls | python3 -c 'import json,sys; print("   stub got:", json.dumps(json.load(sys.stdin)[-1]))'; }
    ART0=$(python3 -c "import sys; sys.path.insert(0,'$HERE'); import stub_orbitd as s; print(s.art_key(s.ART[0]))")
    ART1=$(python3 -c "import sys; sys.path.insert(0,'$HERE'); import stub_orbitd as s; print(s.art_key(s.ART[1]))")

    say "start stub orbitd + orbit-mpris"
    start_stub
    start_monitor
    start_mpris
    eventually "player appears on the session bus" has_player
    NAME=$(orbit_names)
    P=${NAME#org.mpris.MediaPlayer2.}
    run sh -c "busctl --user list --no-pager | grep -i mpris"
    check "exactly one player (timer activity ignored)" test "$(orbit_names | wc -l)" -eq 1
    check "bus name is orbit_<dev8>_<sha8>" python3 -c "import re,sys; sys.exit(not re.fullmatch(r'org\.mpris\.MediaPlayer2\.orbit_a1b2c3d4_[0-9a-f]{8}', sys.argv[1]))" "$NAME"
    run busctl --user introspect --no-pager "$NAME" /org/mpris/MediaPlayer2 | cut -c1-200
    run busctl --user get-property "$NAME" /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2 Identity
    check "Identity" test "$(busctl --user get-property "$NAME" /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2 Identity)" = 's "Spotify (Pixel 8)"'
    run busctl --user get-property "$NAME" /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player PlaybackStatus CanGoNext CanSeek

    say "metadata + art"
    run playerctl -l
    check "playerctl lists the player" sh -c "playerctl -l | grep -qx '$P'"
    eventually "artUrl is the fetched PNG (file://)" art_is "$ART0"
    run playerctl -p "$P" metadata
    check "title" meta_is xesam:title "Song One"
    check "artist" meta_is xesam:artist "Artist A"
    check "album" meta_is xesam:album "Album X"
    check "length" meta_is mpris:length 215000000
    run python3 -c "import sys; print(open(sys.argv[1], 'rb').read(8))" "$(art_file)"
    run playerctl -p "$P" status
    check "status Playing" status_is Playing
    run playerctl -p "$P" position
    check "position extrapolated live (61-66 s)" pos_between 61 66

    say "play-pause"
    run playerctl -p "$P" play-pause
    show_last_call
    check "PlayPause -> media.pause" last_call_is "{\"device\":\"a1b2c3d4e5f60718293a4b5c6d7e8f90\",\"activity\":\"$MEDIA\",\"action\":\"media.pause\"}"
    eventually "status Paused" status_is Paused
    eventually "PropertiesChanged PlaybackStatus=Paused" monitor_has PropertiesChanged '"Paused"'

    say "next"
    run playerctl -p "$P" next
    show_last_call
    check "Next -> media.next" last_call_is "{\"device\":\"a1b2c3d4e5f60718293a4b5c6d7e8f90\",\"activity\":\"$MEDIA\",\"action\":\"media.next\"}"
    eventually "title follows (Song Two)" meta_is xesam:title "Song Two"
    eventually "art follows the track" art_is "$ART1"
    run playerctl -p "$P" metadata --format '{{title}} / {{artist}} / {{album}} / {{mpris:artUrl}}'

    say "position (SetPosition) and relative seek (Seek)"
    run playerctl -p "$P" position 30
    show_last_call
    check "SetPosition 30 s -> media.seek 30000" last_call_is "{\"device\":\"a1b2c3d4e5f60718293a4b5c6d7e8f90\",\"activity\":\"$MEDIA\",\"action\":\"media.seek\",\"value\":30000}"
    eventually "position is 30 s" pos_between 30 30.01
    eventually "Seeked signal emitted" monitor_has Seeked "int64 30000000"
    run playerctl -p "$P" position 10+
    show_last_call
    check "Seek +10 s -> media.seek 40000" last_call_is "{\"device\":\"a1b2c3d4e5f60718293a4b5c6d7e8f90\",\"activity\":\"$MEDIA\",\"action\":\"media.seek\",\"value\":40000}"
    eventually "position is 40 s" pos_between 40 40.01

    say "play, raise"
    run playerctl -p "$P" play
    check "Play -> media.play" last_call_is "{\"device\":\"a1b2c3d4e5f60718293a4b5c6d7e8f90\",\"activity\":\"$MEDIA\",\"action\":\"media.play\"}"
    eventually "status Playing" status_is Playing
    run busctl --user call "$NAME" /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2 Raise
    check "Raise -> open" last_call_is "{\"device\":\"a1b2c3d4e5f60718293a4b5c6d7e8f90\",\"activity\":\"$MEDIA\",\"action\":\"open\"}"

    say "phone-side change propagates"
    run ctl stub.patch "{\"id\":\"$MEDIA\",\"set\":{\"title\":\"Live Edit\"}}"
    eventually "title Live Edit" meta_is xesam:title "Live Edit"
    eventually "PropertiesChanged carries the new Metadata" monitor_has PropertiesChanged '"Live Edit"'

    say "orbitd restart"
    kill "$STUB_PID"; wait "$STUB_PID" 2>/dev/null
    eventually "player removed while orbitd is away" no_player
    start_stub
    eventually "player back after orbitd returns" has_player
    run sh -c "busctl --user list --no-pager | grep -i mpris"

    say "activity removed"
    run ctl stub.remove "{\"id\":\"$MEDIA\"}"
    eventually "player vanishes" no_player
    run sh -c "busctl --user list --no-pager | grep -i mpris || echo '(no mpris names)'"
    run playerctl -l
}

scenario_real() {
    # Commands come from the main branch (orbitd, orbit-fakephone, orbit).
    export ORBIT_CONFIG_DIR=$W/laptop
    say "start orbitd + orbit-fakephone, pair"
    orbitd --no-ble >>"$W/orbitd.log" 2>&1 & PIDS+=("$!")
    eventually "orbitd socket" test -S "$SOCK"
    ORBIT_CONFIG_DIR=$W/phone orbit-fakephone --auto-accept --no-mdns --port 47821 >>"$W/fakephone.log" 2>&1 &
    PHONE_PID=$!
    PIDS+=("$PHONE_PID")
    sleep 1
    run orbit connect 127.0.0.1:47821 || fail "orbit connect"
    run orbit pair --accept || fail "orbit pair --accept"
    start_monitor
    start_mpris
    eventually "player appears on the session bus" has_player
    NAME=$(orbit_names | head -1)
    P=${NAME#org.mpris.MediaPlayer2.}
    run sh -c "busctl --user list --no-pager | grep -i mpris"
    run busctl --user get-property "$NAME" /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2 Identity
    run playerctl -l
    run playerctl -p "$P" metadata
    check "has a title" test -n "$(meta xesam:title)"
    eventually "artUrl is a readable file" sh -c "f=\$(playerctl -p '$P' metadata mpris:artUrl) && test -s \"\${f#file://}\""
    before=$(playerctl -p "$P" status)
    run playerctl -p "$P" play-pause
    eventually "play-pause toggles status (was $before)" sh -c "test \"\$(playerctl -p '$P' status)\" != '$before'"
    eventually "fakephone executed media.pause" grep -q "action media.pause -> media paused" "$W/fakephone.log"
    eventually "PropertiesChanged PlaybackStatus" monitor_has PropertiesChanged PlaybackStatus
    title=$(meta xesam:title)
    if [ "$(busctl --user get-property "$NAME" /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player CanGoNext)" = "b true" ]; then
        run playerctl -p "$P" next
        eventually "next changes the title" sh -c "test \"\$(playerctl -p '$P' metadata xesam:title)\" != '$title'"
        eventually "fakephone executed media.next" grep -q "action media.next -> media next" "$W/fakephone.log"
    fi
    if [ "$(busctl --user get-property "$NAME" /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player CanSeek)" = "b true" ]; then
        run playerctl -p "$P" position 5
        eventually "position follows a seek" pos_between 5 8
        eventually "fakephone executed media.seek" grep -q "action media.seek -> media seek to 0:05" "$W/fakephone.log"
    fi
    run grep "action" "$W/fakephone.log"
    run orbit activities
    say "phone goes away"
    kill "$PHONE_PID"
    eventually "player vanishes" no_player
}

case "${MODE:-stub}" in
    stub) scenario_stub ;;
    real) scenario_real ;;
    *) echo "unknown MODE=$MODE"; exit 2 ;;
esac

say "orbit-mpris shuts down on SIGTERM"
kill -TERM "$MPRIS_PID"
wait "$MPRIS_PID"; rc=$?
check "exit status 0 (got $rc)" test "$rc" -eq 0

say "orbit-mpris log (tail)"
tail -n 25 "$W/mpris.log"
say "result: $FAILS failed check(s)"
exit "$FAILS"
