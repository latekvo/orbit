#!/usr/bin/env bash
# Runs desktop/tests/connectivity in Linux (Docker) with NET_ADMIN, e.g.:
#   desktop/tools/conn-jvm-docker/run.sh -k netfilter
#   ORBIT_FLOOD_SOURCES=127.0.0.2,127.0.0.3,127.0.0.4,127.0.0.5 desktop/tools/conn-jvm-docker/run.sh -k flood
# Builds the JVM harness on the host first and copies its classpath into the container.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
export JAVA_HOME="${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}"
# ORBIT_HARNESS_CLASSPATH: a harness.classpath built elsewhere (e.g. another worktree); else build it here.
classpath_file="${ORBIT_HARNESS_CLASSPATH:-$repo/android/companion/protocol/build/harness.classpath}"
[ -n "${ORBIT_HARNESS_CLASSPATH:-}" ] || (cd "$repo/android/companion" && ./gradlew -q :protocol:harnessClasspath)
dist="$(mktemp -d /tmp/conn-jvm-dist.XXXXXX)"
trap 'rm -rf "$dist"' EXIT
cp_out=()
i=0
IFS=: read -ra entries < "$classpath_file" || true
for e in "${entries[@]}"; do
    [ -e "$e" ] || continue
    i=$((i + 1))
    if [ -d "$e" ]; then cp -R "$e" "$dist/d$i"; cp_out+=("/jvm/d$i"); else cp "$e" "$dist/j$i.jar"; cp_out+=("/jvm/j$i.jar"); fi
done
(IFS=:; echo "${cp_out[*]}") > "$dist/classpath"
image=orbit-conn-jvm
docker build -q -t "$image" "$here" >/dev/null
docker run --rm --cap-add NET_ADMIN \
    -e ORBIT_CONNECTIVITY=1 -e ORBIT_HARNESS_CLASSPATH=/jvm/classpath -e JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64 \
    -e ORBIT_FLOOD_SOURCES="${ORBIT_FLOOD_SOURCES:-127.0.0.1}" -e ORBIT_FLAP_MINUTES="${ORBIT_FLAP_MINUTES:-4}" \
    -v "$repo:/repo:ro" -v "$dist:/jvm:ro" -v orbit-conn-jvm-uv-cache:/root/.cache/uv \
    "$image" bash -c 'mkdir -p /work/desktop && tar -C /repo/desktop --exclude=.venv --exclude=__pycache__ -cf - . | tar -C /work/desktop -xf - && cd /work/desktop && uv sync -q --no-group qt && uv run --no-group qt pytest tests/connectivity -s -q "$@"' _ "$@"
