#!/usr/bin/env bash
# installer/macos/build.sh
#
# Build the F-Pulse macOS installer (.pkg).
#
# Prereqs (one-time):
#   * Xcode Command Line Tools  (`xcode-select --install`)
#   * Python 3.12+ venv at <repo>/.venv with pyinstaller installed
#   * Node.js + npm for the frontend build
#   * Optional: Developer ID Installer cert in Keychain for signing
#
# Usage:
#   ./build.sh
#   ./build.sh --sign --identity "Developer ID Installer: Hybridyn Data Labs ..."
#   ./build.sh --sign --identity "..." --notarize --apple-id you@example.com --team-id ABC123
#
# Output:
#   installer/macos/output/FPulse-<version>.pkg

set -euo pipefail

# ── Locate paths + parse args ──
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"
VERSION="1.0.0"
SIGN=""
IDENTITY=""
NOTARIZE=""
APPLE_ID=""
TEAM_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign)     SIGN="1"; shift ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --notarize) NOTARIZE="1"; shift ;;
    --apple-id) APPLE_ID="$2"; shift 2 ;;
    --team-id)  TEAM_ID="$2"; shift 2 ;;
    *)          echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo
echo "  F-Pulse macOS installer build"
echo "  Repo: $REPO_ROOT"
echo

BUILD_DIR="$SCRIPT_DIR/build"
OUTPUT_DIR="$SCRIPT_DIR/output"
PAYLOAD_ROOT="$BUILD_DIR/payload"
APP_BUNDLE="$PAYLOAD_ROOT/Applications/FPulse.app"

rm -rf "$BUILD_DIR" "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR" "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"

# ── 1. Frontend ──
echo "  [1/4] Building frontend..."
( cd "$REPO_ROOT/frontend" && npm install --silent && npm run build )

# ── 2. Freeze backend with PyInstaller ──
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
    --distpath "$APP_BUNDLE/Contents/Resources" \
    backend/fpulse/__main__.py )

# ── 3. Assemble .app bundle ──
echo "  [3/4] Assembling .app bundle + Info.plist..."

# Tiny launcher that exec's the bundled fpulse with `serve`
cat > "$APP_BUNDLE/Contents/MacOS/FPulse" <<'EOF'
#!/usr/bin/env bash
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/../Resources/fpulse/fpulse" serve
EOF
chmod +x "$APP_BUNDLE/Contents/MacOS/FPulse"

cat > "$APP_BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>          <string>FPulse</string>
  <key>CFBundleIdentifier</key>          <string>com.hybridyn.fpulse</string>
  <key>CFBundleName</key>                <string>F-Pulse</string>
  <key>CFBundleDisplayName</key>         <string>F-Pulse</string>
  <key>CFBundleIconFile</key>            <string>fpulse</string>
  <key>CFBundleVersion</key>             <string>$VERSION</string>
  <key>CFBundleShortVersionString</key>  <string>$VERSION</string>
  <key>CFBundlePackageType</key>         <string>APPL</string>
  <key>LSMinimumSystemVersion</key>      <string>12.0</string>
  <key>LSUIElement</key>                 <true/>
  <key>NSHighResolutionCapable</key>     <true/>
</dict>
</plist>
EOF

# Brand icon: build fpulse.icns from the logo. sips + iconutil are macOS
# built-ins, so no extra tooling. The Info.plist above points
# CFBundleIconFile at "fpulse" (-> fpulse.icns in Resources).
LOGO="$REPO_ROOT/frontend/public/fpulse-logo-mark.png"
if [[ -f "$LOGO" ]] && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$BUILD_DIR/fpulse.iconset"
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  sips -z 16   16   "$LOGO" --out "$ICONSET/icon_16x16.png"      >/dev/null
  sips -z 32   32   "$LOGO" --out "$ICONSET/icon_16x16@2x.png"   >/dev/null
  sips -z 32   32   "$LOGO" --out "$ICONSET/icon_32x32.png"      >/dev/null
  sips -z 64   64   "$LOGO" --out "$ICONSET/icon_32x32@2x.png"   >/dev/null
  sips -z 128  128  "$LOGO" --out "$ICONSET/icon_128x128.png"    >/dev/null
  sips -z 256  256  "$LOGO" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
  sips -z 256  256  "$LOGO" --out "$ICONSET/icon_256x256.png"    >/dev/null
  sips -z 512  512  "$LOGO" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
  sips -z 512  512  "$LOGO" --out "$ICONSET/icon_512x512.png"    >/dev/null
  sips -z 1024 1024 "$LOGO" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
  iconutil -c icns "$ICONSET" -o "$APP_BUNDLE/Contents/Resources/fpulse.icns"
  echo "      brand icon: fpulse.icns generated"
else
  echo "      (logo or iconutil missing - .app will use the default icon)"
fi

# Copy frontend dist alongside the python bundle
cp -R "$REPO_ROOT/frontend/dist" "$APP_BUNDLE/Contents/Resources/frontend-dist"

# Copy the canonical pre/postinstall scripts (single source of truth
# under installer/macos/scripts/) into the build staging dir, where
# pkgbuild will pick them up via --scripts.
mkdir -p "$BUILD_DIR/scripts"
cp "$SCRIPT_DIR/scripts/preinstall"  "$BUILD_DIR/scripts/preinstall"
cp "$SCRIPT_DIR/scripts/postinstall" "$BUILD_DIR/scripts/postinstall"
chmod +x "$BUILD_DIR/scripts/preinstall" "$BUILD_DIR/scripts/postinstall"

# ── 4. Build the .pkg ──
echo "  [4/4] Building .pkg..."

COMPONENT_PKG="$BUILD_DIR/FPulse-component.pkg"
PRODUCT_PKG="$OUTPUT_DIR/FPulse-$VERSION.pkg"

pkgbuild \
  --root "$PAYLOAD_ROOT" \
  --identifier com.hybridyn.fpulse \
  --version "$VERSION" \
  --scripts "$BUILD_DIR/scripts" \
  --install-location / \
  "$COMPONENT_PKG"

productbuild \
  --package "$COMPONENT_PKG" \
  --version "$VERSION" \
  "$PRODUCT_PKG"

# ── 5. Optional sign + notarize ──
if [[ -n "$SIGN" ]]; then
  [[ -n "$IDENTITY" ]] || { echo "--sign requires --identity"; exit 1; }
  echo "  Signing with $IDENTITY..."
  SIGNED_PKG="$OUTPUT_DIR/FPulse-$VERSION-signed.pkg"
  productsign --sign "$IDENTITY" "$PRODUCT_PKG" "$SIGNED_PKG"
  mv "$SIGNED_PKG" "$PRODUCT_PKG"
fi

if [[ -n "$NOTARIZE" ]]; then
  [[ -n "$APPLE_ID" && -n "$TEAM_ID" ]] || { echo "--notarize requires --apple-id + --team-id"; exit 1; }
  echo "  Notarizing — this can take 5-15 min..."
  xcrun notarytool submit "$PRODUCT_PKG" \
    --apple-id "$APPLE_ID" --team-id "$TEAM_ID" \
    --keychain-profile fpulse-notary --wait
  xcrun stapler staple "$PRODUCT_PKG"
fi

echo
echo "  Done. Output: $PRODUCT_PKG"
echo
