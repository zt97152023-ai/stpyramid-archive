#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
JVM="$SCRIPT_DIR/jre/bin/java"
[[ -x "$JVM" ]] || JVM="${JAVA_HOME:-}/bin/java"
[[ -x "$JVM" ]] || JVM="$(command -v java || true)"
[[ -x "$JVM" ]] || { echo "Bundled Java runtime is missing." >&2; exit 1; }
if [[ -f "$SCRIPT_DIR/runtime/app/desktop_worker.py" && -z "${STPYRAMID_PORTABLE_RUNTIME:-}" ]]; then
  export STPYRAMID_PORTABLE_RUNTIME="$SCRIPT_DIR/runtime"
fi
exec "$JVM" -jar "$SCRIPT_DIR/STPyramidJava-0.2.4.jar" "$@"
