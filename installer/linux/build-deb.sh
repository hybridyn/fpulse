#!/usr/bin/env bash
# installer/linux/build-deb.sh
#
# Build the F-Pulse Debian package (.deb).
#
# Output target distros: Ubuntu 22.04+, Debian 12+.
#
# Prereqs (one-time):
#   sudo apt install dpkg-dev fakeroot python3-venv nodejs npm
#   And a Python 3.12+ venv at <repo>/.venv with pyinstaller installed.
#
# Usage:
#   ./build-deb.sh
#
# Output:
#   installer/linux/output/fpulse_<version>_amd64.deb

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"
VERSION="1.0.0"
ARCH="amd64"
PKG_NAME="fpulse"

echo
echo "  F-Pulse .deb build"
echo "  Repo:    $REPO_ROOT"
echo "  Version: $VERSION"
echo

STAGING="$SCRIPT_DIR/build/deb-staging"
OUTPUT_DIR="$SCRIPT_DIR/output"

rm -rf "$STAGING" "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR" \
         "$STAGING/DEBIAN" \
         "$STAGING/usr/lib/$PKG_NAME" \
         "$STAGING/usr/bin" \
         "$STAGING/usr/share/applications" \
         "$STAGING/usr/share/icons/hicolor/256x256/apps" \
         "$STAGING/usr/share/doc/$PKG_NAME"

# ── 1. Frontend ──
echo "  [1/4] Building frontend..."
( cd "$REPO_ROOT/frontend" && npm install --silent && npm run build )

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
    --distpath "$STAGING/usr/lib/$PKG_NAME/runtime" \
    backend/fpulse/__main__.py )

# Bring the frontend in
cp -R "$REPO_ROOT/frontend/dist" "$STAGING/usr/lib/$PKG_NAME/frontend-dist"

# Symlink fpulse onto PATH
ln -sf "/usr/lib/$PKG_NAME/runtime/fpulse/fpulse" "$STAGING/usr/bin/fpulse"

# Desktop launcher (opens the localhost UI in default browser)
cat > "$STAGING/usr/share/applications/fpulse.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=F-Pulse
GenericName=Data Pipeline Studio
Comment=Build, run, and schedule data pipelines locally
Exec=xdg-open http://localhost:8001
Icon=fpulse
Terminal=false
Categories=Development;Database;
StartupNotify=true
EOF

# Optional icon — only copy if it exists, the .deb is valid either way
if [[ -f "$SCRIPT_DIR/../windows/icons/fpulse.png" ]]; then
  cp "$SCRIPT_DIR/../windows/icons/fpulse.png" \
     "$STAGING/usr/share/icons/hicolor/256x256/apps/fpulse.png"
fi

# Docs
cp "$REPO_ROOT/README.md"  "$STAGING/usr/share/doc/$PKG_NAME/"
cp "$REPO_ROOT/LICENSE"    "$STAGING/usr/share/doc/$PKG_NAME/copyright"
[[ -f "$REPO_ROOT/NOTICE" ]] && cp "$REPO_ROOT/NOTICE" "$STAGING/usr/share/doc/$PKG_NAME/"

# ── 3. DEBIAN/ control files ──
echo "  [3/4] Generating DEBIAN/ metadata..."

# Compute the installed size (in KB) for the control file
INSTALLED_SIZE_KB="$(du -sk "$STAGING" | cut -f1)"

cp "$SCRIPT_DIR/debian/control"  "$STAGING/DEBIAN/control"
cp "$SCRIPT_DIR/debian/postinst" "$STAGING/DEBIAN/postinst"
cp "$SCRIPT_DIR/debian/prerm"    "$STAGING/DEBIAN/prerm"

# Fill in templated fields
sed -i "s|@@VERSION@@|$VERSION|g"               "$STAGING/DEBIAN/control"
sed -i "s|@@ARCH@@|$ARCH|g"                     "$STAGING/DEBIAN/control"
sed -i "s|@@INSTALLED_SIZE_KB@@|$INSTALLED_SIZE_KB|g" "$STAGING/DEBIAN/control"

chmod 0755 "$STAGING/DEBIAN/postinst" "$STAGING/DEBIAN/prerm"

# ── 4. Build the .deb ──
echo "  [4/4] dpkg-deb --build..."
DEB_FILE="$OUTPUT_DIR/${PKG_NAME}_${VERSION}_${ARCH}.deb"
fakeroot dpkg-deb --build --root-owner-group "$STAGING" "$DEB_FILE"

# Optional GPG sign (uses FPULSE_GPG_KEY_ID env var)
if [[ -n "${FPULSE_GPG_KEY_ID:-}" ]] && command -v dpkg-sig >/dev/null 2>&1; then
  echo "  Signing with GPG key $FPULSE_GPG_KEY_ID..."
  dpkg-sig --sign builder -k "$FPULSE_GPG_KEY_ID" "$DEB_FILE"
fi

echo
echo "  Done. Output: $DEB_FILE"
echo "  Install with: sudo apt install $DEB_FILE"
echo
