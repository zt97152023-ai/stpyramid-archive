#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 JDK_ROOT OUTPUT_JAR" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
JDK_ROOT="$(cd -- "$1" && pwd)"
OUTPUT="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$2")"
COMPILER="$JDK_ROOT/bin/javac"
JAR="$JDK_ROOT/bin/jar"
[[ -x "$COMPILER" ]] || { echo "JDK javac was not found: $COMPILER" >&2; exit 1; }
[[ -x "$JAR" ]] || { echo "JDK jar was not found: $JAR" >&2; exit 1; }
mkdir -p "$(dirname -- "$OUTPUT")"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/stpyramid-java.XXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT
mapfile -t SOURCES < <(find "$SCRIPT_DIR/src/stpyramid" -type f -name '*.java' -print | sort)
"$COMPILER" -encoding UTF-8 --release 17 -Xlint:all -d "$STAGE/classes" "${SOURCES[@]}"
mkdir -p "$STAGE/classes/META-INF"
cat > "$STAGE/classes/META-INF/MANIFEST.MF" <<'EOF'
Manifest-Version: 1.0
Main-Class: stpyramid.Main
EOF
"$JAR" --create --file "$OUTPUT" --manifest "$STAGE/classes/META-INF/MANIFEST.MF" -C "$STAGE/classes" .
echo "$OUTPUT"
