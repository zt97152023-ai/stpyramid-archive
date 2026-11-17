#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/package-unix.sh" "$@"
OUTPUT="$(cd -- "$2" && pwd)"
APP="$OUTPUT/STPyramid.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp -a "$OUTPUT/jre" "$APP/Contents/"
cp "$OUTPUT/STPyramidJava-0.2.4.jar" "$APP/Contents/Resources/"
if [[ -d "$OUTPUT/runtime" ]]; then cp -a "$OUTPUT/runtime" "$APP/Contents/Resources/"; fi
cat > "$APP/Contents/MacOS/STPyramid" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT/Resources/runtime"
if [[ -f "$RUNTIME/app/desktop_worker.py" ]]; then export STPYRAMID_PORTABLE_RUNTIME="$RUNTIME"; fi
exec "$ROOT/jre/bin/java" -jar "$ROOT/Resources/STPyramidJava-0.2.4.jar" "$@"
EOF
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>STPyramid</string>
<key>CFBundleDisplayName</key><string>STPyramid</string>
<key>CFBundleIdentifier</key><string>ai.zt97152023.stpyramid</string>
<key>CFBundleVersion</key><string>0.2.4</string>
<key>CFBundleExecutable</key><string>STPyramid</string>
<key>LSMinimumSystemVersion</key><string>11.0</string>
</dict></plist>
EOF
chmod +x "$APP/Contents/MacOS/STPyramid"
