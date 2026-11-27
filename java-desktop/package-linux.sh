#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/package-unix.sh" "$@"
OUTPUT="$(cd -- "$2" && pwd)"
cat > "$OUTPUT/STPyramid.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=STPyramid
Comment=Lossless spatial transcriptomics archive
Exec=$OUTPUT/Launch-STPyramidJava.sh
Terminal=false
Categories=Science;Utility;
EOF
chmod +x "$OUTPUT/STPyramid.desktop"
