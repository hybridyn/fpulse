#!/usr/bin/env bash
# create-shortcut.sh - add an "F-Pulse" entry to your Linux application
# menu (source / dev checkout), with the F-Pulse logo.
#
#   ./create-shortcut.sh            # create the menu entry
#   ./create-shortcut.sh --remove   # remove it
#
# Requires the `fpulse` CLI on PATH (run `pip install -e .` once in this
# folder). The .deb / .rpm / AppImage packages create their own launcher
# and don't need this - it's the source-user equivalent of the Windows
# Create-Shortcut.ps1.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
ICON="$ROOT/installer/windows/icons/fpulse.png"
DEST="$HOME/.local/share/applications/fpulse.desktop"

if [ "${1:-}" = "--remove" ]; then
  rm -f "$DEST" && echo "Removed $DEST"
  exit 0
fi

mkdir -p "$(dirname "$DEST")"
cat > "$DEST" <<EOF
[Desktop Entry]
Type=Application
Name=F-Pulse
GenericName=Data Pipeline Studio
Comment=Local-first data pipeline studio by Hybridyn
Exec=sh -c 'cd "$ROOT" && fpulse open'
Icon=$ICON
Terminal=false
Categories=Development;Database;
EOF

chmod +x "$DEST" 2>/dev/null || true
echo "Created $DEST"
echo "Look for 'F-Pulse' in your application menu."
echo "(Remove with: ./create-shortcut.sh --remove)"
