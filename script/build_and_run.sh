#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-run}"
APP_NAME="FolioPDF"
BUNDLE_ID="io.github.mhatem5351.FolioPDF"
MIN_SYSTEM_VERSION="14.0"
VERSION="${FOLIO_VERSION:-2.0.0}"
# FOLIO_RELEASE=1 (set by script/package_release.sh): optimized build, no
# paths from this machine in Info.plist — the engine Python is embedded later.
RELEASE="${FOLIO_RELEASE:-0}"
CONFIG=debug
[[ "$RELEASE" == "1" ]] && CONFIG=release

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Build output lives OUTSIDE iCloud-synced folders — syncing an .app bundle
# mid-write corrupts it and litters " 2" duplicates. Override with FOLIO_DIST_DIR.
DIST_DIR="${FOLIO_DIST_DIR:-$HOME/Builds/FolioPDF}"
APP_BUNDLE="$DIST_DIR/Folio PDF.app"
APP_CONTENTS="$APP_BUNDLE/Contents"
APP_MACOS="$APP_CONTENTS/MacOS"
APP_BINARY="$APP_MACOS/$APP_NAME"
INFO_PLIST="$APP_CONTENTS/Info.plist"
VENV_DIR="${FOLIO_VENV_DIR:-$HOME/Library/Application Support/FolioPDF/engine-venv}"
ENGINE_PYTHON="$VENV_DIR/bin/python3"
ENGINE_SCRIPT="$ROOT_DIR/Engine/pdf_engine.py"

if [[ "$RELEASE" != "1" && ! -x "$ENGINE_PYTHON" ]]; then
  "$ROOT_DIR/script/bootstrap_engine.sh"
fi

pkill -x "$APP_NAME" >/dev/null 2>&1 || true

SCRATCH_DIR="${FOLIO_SCRATCH_DIR:-$HOME/Library/Caches/FolioPDF/build}"
swift build -c "$CONFIG" --scratch-path "$SCRATCH_DIR"
BUILD_BINARY="$(swift build -c "$CONFIG" --scratch-path "$SCRATCH_DIR" --show-bin-path)/$APP_NAME"

rm -rf "$APP_BUNDLE"
mkdir -p "$APP_MACOS" "$APP_CONTENTS/Resources"
cp "$BUILD_BINARY" "$APP_BINARY"
chmod +x "$APP_BINARY"

# Bundle the engine script inside the app. At runtime nothing may live in
# TCC-protected folders (Documents/Desktop): a fresh ad-hoc-signed build
# would hang on a folder-permission prompt when spawning the engine.
cp "$ROOT_DIR/Engine/pdf_engine.py" "$APP_CONTENTS/Resources/pdf_engine.py"
ENGINE_SCRIPT="$APP_CONTENTS/Resources/pdf_engine.py"
# Engine extension modules (Engine/ext/*.py) are loaded relative to the script.
rm -rf "$APP_CONTENTS/Resources/ext"
mkdir -p "$APP_CONTENTS/Resources/ext"
cp "$ROOT_DIR"/Engine/ext/*.py "$APP_CONTENTS/Resources/ext/" 2>/dev/null || true

# Bundle the first-run engine installer + its requirements so a distributed
# .app can self-install the Python engine on a Mac that never ran this script.
# The app runs setup-engine.sh (with FOLIO_ENGINE_SCRIPT/FOLIO_REQUIREMENTS
# pointed at these Resources) the first time the venv is missing.
cp "$ROOT_DIR/script/setup-engine.sh" "$APP_CONTENTS/Resources/setup-engine.sh"
chmod +x "$APP_CONTENTS/Resources/setup-engine.sh"
cp "$ROOT_DIR/Engine/requirements.txt" "$APP_CONTENTS/Resources/requirements.txt"

# App icon (Dock, Finder, About window). CFBundleIconFile points at this.
cp "$ROOT_DIR/Resources/AppIcon.icns" "$APP_CONTENTS/Resources/AppIcon.icns"

DEV_KEYS=""
if [[ "$RELEASE" != "1" ]]; then
  DEV_KEYS="  <key>FolioProjectRoot</key>
  <string>$ROOT_DIR</string>
  <key>FolioEnginePythonPath</key>
  <string>$ENGINE_PYTHON</string>
"
fi

cat >"$INFO_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_ID</string>
  <key>CFBundleName</key>
  <string>Folio PDF</string>
  <key>CFBundleShortVersionString</key>
  <string>$VERSION</string>
  <key>CFBundleVersion</key>
  <string>$VERSION</string>
  <key>NSHumanReadableCopyright</key>
  <string>Based on Sam PDF Studio by Sam Wasserman (Apache-2.0)</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSMinimumSystemVersion</key>
  <string>$MIN_SYSTEM_VERSION</string>
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeName</key>
      <string>PDF Document</string>
      <key>CFBundleTypeRole</key>
      <string>Editor</string>
      <key>LSHandlerRank</key>
      <string>Alternate</string>
      <key>LSItemContentTypes</key>
      <array>
        <string>com.adobe.pdf</string>
      </array>
    </dict>
  </array>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
$DEV_KEYS  <key>FolioBuildTime</key>
  <string>$(date '+%b %-d %-I:%M %p')</string>
</dict>
</plist>
PLIST

open_app() {
  /usr/bin/open -n "$APP_BUNDLE"
}

case "$MODE" in
  build)
    echo "✓ built $APP_BUNDLE"
    ;;
  install)
    # Replace the copy in /Applications and forget the dev bundle so only one
    # "Folio PDF" shows up in Spotlight, Launchpad and Open With.
    pkill -x "$APP_NAME" 2>/dev/null || true
    rm -rf "/Applications/Folio PDF.app"
    /usr/bin/ditto "$APP_BUNDLE" "/Applications/Folio PDF.app"
    LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
    "$LSREGISTER" -u "$APP_BUNDLE" 2>/dev/null || true
    "$LSREGISTER" -f "/Applications/Folio PDF.app"
    echo "✓ installed /Applications/Folio PDF.app"
    ;;
  run)
    open_app
    ;;
  --debug|debug)
    lldb -- "$APP_BINARY"
    ;;
  --logs|logs)
    open_app
    /usr/bin/log stream --info --style compact --predicate "process == \"$APP_NAME\""
    ;;
  --telemetry|telemetry)
    open_app
    /usr/bin/log stream --info --style compact --predicate "subsystem == \"$BUNDLE_ID\""
    ;;
  --verify|verify)
    open_app
    sleep 1
    pgrep -x "$APP_NAME" >/dev/null
    ;;
  *)
    echo "usage: $0 [run|build|install|--debug|--logs|--telemetry|--verify]" >&2
    exit 2
    ;;
esac
