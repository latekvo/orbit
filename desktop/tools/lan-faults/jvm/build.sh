#!/usr/bin/env bash
# Compile the Kotlin protocol module (with LanResponderMain from its test sources) and stage its
# runtime classpath in jvm/cp/, which the lab mounts at /jvm.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
companion="$here/../../../../android/companion"
export JAVA_HOME="${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}"
(cd "$companion" && ./gradlew --offline -q -I "$here/classpath.gradle" :protocol:lanClasspath)
rm -rf "$here/cp" && mkdir -p "$here/cp"
i=0
while read -r entry; do
    i=$((i + 1))
    if [ -d "$entry" ]; then cp -R "$entry" "$here/cp/$i"; else cp "$entry" "$here/cp/"; fi
done < "$companion/protocol/build/lan-classpath.txt"
