#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 JDK_ROOT OUTPUT_DIR [NATIVE_RUNTIME_DIR]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
JDK_ROOT="$(cd -- "$1" && pwd)"
OUTPUT="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$2")"
RUNTIME="${3:-}"
[[ ! -e "$OUTPUT" ]] || { echo "Output already exists: $OUTPUT" >&2; exit 1; }
[[ -x "$JDK_ROOT/bin/jlink" ]] || { echo "JDK jlink was not found: $JDK_ROOT/bin/jlink" >&2; exit 1; }
mkdir -p "$OUTPUT"
MODULES="java.base,java.desktop,java.logging,java.management,java.naming,java.prefs,java.sql,jdk.crypto.ec,jdk.zipfs"
"$JDK_ROOT/bin/jlink" --add-modules "$MODULES" --strip-debug --no-header-files --no-man-pages \
  --compress=2 --output "$OUTPUT/jre"
"$SCRIPT_DIR/build.sh" "$JDK_ROOT" "$OUTPUT/STPyramidJava-0.2.4.jar" >/dev/null
cp "$SCRIPT_DIR/Launch-STPyramidJava.sh" "$SCRIPT_DIR/README.md" "$SOURCE_ROOT/THIRD_PARTY.md" "$OUTPUT/"
if [[ -n "$RUNTIME" ]]; then
  RUNTIME="$(cd -- "$RUNTIME" && pwd)"
  cp -a "$RUNTIME" "$OUTPUT/runtime"
fi
cat > "$OUTPUT/PLATFORM.txt" <<EOF
Platform package built on $(uname -s)-$(uname -m)
Java runtime is bundled under jre/
Source and HEST byte-exact archive modes are cross-platform.
Native Count+TIFF compression requires a qualified runtime for this operating system.
EOF
chmod +x "$OUTPUT/Launch-STPyramidJava.sh"
echo "$OUTPUT"
