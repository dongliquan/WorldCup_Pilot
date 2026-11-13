#!/usr/bin/env bash
# Build the macOS app: dist/World Cup Pilot.app and dist/WorldCupPilot-mac-<arch>.dmg (arch = arm64 or x86_64).
# Run it on a Mac. The build is not code-signed.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> icon.icns from assets/icon_1024.png"
if [ ! -f icon.icns ]; then
  rm -rf build/icon.iconset && mkdir -p build/icon.iconset
  for s in 16 32 128 256 512; do
    sips -z "$s" "$s" assets/icon_1024.png --out "build/icon.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z "$((s * 2))" "$((s * 2))" assets/icon_1024.png --out "build/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns build/icon.iconset -o icon.icns
fi

echo "==> installing dependencies"
python3 -m pip install -r requirements.txt

echo "==> building the .app (PyInstaller)"
python3 -m PyInstaller --noconfirm worldcup_mac.spec

echo "==> packing the .dmg"
DMG="dist/WorldCupPilot-mac-$(uname -m).dmg"
rm -f "$DMG"
hdiutil create -volname "World Cup Pilot" -srcfolder "dist/World Cup Pilot.app" -ov -format UDZO "$DMG"

echo "==> done: dist/World Cup Pilot.app and $DMG"
