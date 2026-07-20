#!/usr/bin/env bash
# installer/linux/build-appimage.sh
#
# Build the F-Pulse AppImage — a single-file portable binary that
# works on any glibc 2.27+ distro without root install.
#
# Use case: users who can't or won't install a system package (Arch,
# locked-down corp laptops, "just give me a single file" requests).
#
# Prereqs (one-time):
#   wget https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
#   chmod +x appimagetool-x86_64.AppImage
#   mv appimagetool-x86_64.AppImage /usr/local/bin/appimagetool
#
# Usage:
#   ./build-appimage.sh
#
# Output:
#   installer/linux/output/FPulse-<version>-x86_64.AppImage

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"
# Read the version from pyproject.toml rather than hardcoding it — a stale
# literal here silently ships a package whose filename and metadata disagree
# with the code inside it.
VERSION="$(grep -m1 -E '^version = "' "$REPO_ROOT/pyproject.toml" | sed -E 's/^version = "([^"]+)"/\1/')"
[[ -n "$VERSION" ]] || { echo "could not read version from pyproject.toml"; exit 1; }
ARCH="x86_64"
PKG_NAME="fpulse"

echo
echo "  F-Pulse AppImage build"
echo "  Repo:    $REPO_ROOT"
echo "  Version: $VERSION"
echo

APPDIR="$SCRIPT_DIR/build/FPulse.AppDir"
OUTPUT_DIR="$SCRIPT_DIR/output"

rm -rf "$APPDIR"
mkdir -p "$OUTPUT_DIR" \
         "$APPDIR/usr/bin" \
         "$APPDIR/usr/lib/$PKG_NAME" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# ── 1. Frontend ──
# Build it, then STAGE it into the Python package. PyInstaller's
# `--collect-all fpulse` below picks up backend/fpulse/frontend_dist/, so the
# frozen bundle carries the UI where main.py actually looks for it
# (fpulse/frontend_dist). Without staging, the app resolves no UI and serves
# a 404 at / — it will say so loudly at startup.
echo "  [1/4] Building + staging frontend..."
( cd "$REPO_ROOT/frontend" && npm install --silent && npm run build )
"$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/stage_frontend.py"

# ── 2. Freeze backend ──
echo "  [2/4] Freezing backend with PyInstaller..."
PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "Python venv not found at $PYTHON"; exit 1; }

( cd "$REPO_ROOT" && \
  "$PYTHON" -m PyInstaller \
    --onedir --noconfirm --clean \
    --name fpulse \
    --paths backend \
    --hidden-import fpulse.main \
    --collect-all fpulse \
    --collect-all duckdb \
    --collect-all fastapi \
    --collect-all uvicorn \
    --distpath "$APPDIR/usr/lib/$PKG_NAME/runtime" \
    backend/fpulse/__main__.py )

# NOTE: no separate frontend copy here. Older revisions copied frontend/dist
# to .../frontend-dist, which main.py never looks for — the package installed
# cleanly and served a blank page. The UI now travels inside the PyInstaller
# bundle via the staging step above.

# Verify the frozen bundle actually carries the UI, rather than discovering it
# after the package is on someone's machine.
if ! find "$APPDIR/usr/lib/$PKG_NAME/runtime" -path '*/fpulse/frontend_dist/index.html' -print -quit | grep -q .; then
  echo "ERROR: the PyInstaller bundle has no fpulse/frontend_dist/index.html."
  echo "       The package would install and serve a blank page. Refusing to build."
  exit 1
fi
ln -sf "../lib/$PKG_NAME/runtime/fpulse/fpulse" "$APPDIR/usr/bin/fpulse"

# ── 3. AppImage scaffolding ──
echo "  [3/4] Writing AppRun + .desktop + icon..."

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="$HERE/usr/bin:$PATH"
exec "$HERE/usr/bin/fpulse" serve "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/$PKG_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=F-Pulse
GenericName=Data Pipeline Studio
Comment=Local-first ETL/ELT pipeline studio
Exec=fpulse
Icon=fpulse
Terminal=false
Categories=Development;Database;
EOF
cp "$APPDIR/$PKG_NAME.desktop" "$APPDIR/usr/share/applications/"

# A placeholder transparent PNG so appimagetool is happy when no
# icon file ships in the repo. Replace with real branding for release.
if [[ -f "$SCRIPT_DIR/../windows/icons/fpulse.png" ]]; then
  cp "$SCRIPT_DIR/../windows/icons/fpulse.png" "$APPDIR/$PKG_NAME.png"
  cp "$SCRIPT_DIR/../windows/icons/fpulse.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$PKG_NAME.png"
else
  # 1x1 transparent PNG (base64)
  /usr/bin/printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82' \
    > "$APPDIR/$PKG_NAME.png"
  cp "$APPDIR/$PKG_NAME.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$PKG_NAME.png"
fi

# ── 4. Pack into a single AppImage ──
echo "  [4/4] Packing AppImage..."
if ! command -v appimagetool >/dev/null 2>&1; then
  cat <<'MSG'
appimagetool not on PATH. Install with:
  wget https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x appimagetool-x86_64.AppImage
  sudo mv appimagetool-x86_64.AppImage /usr/local/bin/appimagetool
MSG
  exit 1
fi

OUT="$OUTPUT_DIR/FPulse-$VERSION-$ARCH.AppImage"
ARCH="$ARCH" appimagetool "$APPDIR" "$OUT"
chmod +x "$OUT"

echo
echo "  Done. Output: $OUT"
echo "  Run with: ./$(basename "$OUT")"
echo
