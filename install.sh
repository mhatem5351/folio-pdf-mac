#!/usr/bin/env bash
# Folio PDF installer for macOS (Apple Silicon).
#
# Downloads the latest release DMG and installs "Folio PDF.app" into
# /Applications (or ~/Applications). Files fetched by curl are not
# quarantined, so the unsigned app opens without the Gatekeeper prompt.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mhatem5351/folio-pdf-mac/main/install.sh | bash
set -euo pipefail

REPO="mhatem5351/folio-pdf-mac"
APP="Folio PDF.app"

if [ "$(uname -m)" != "arm64" ]; then
  echo "Folio PDF release builds are for Apple Silicon (M1 or newer)." >&2
  echo "On an Intel Mac, build from source: https://github.com/$REPO#build-from-source" >&2
  exit 1
fi

echo "Finding the latest Folio PDF release..."
URL="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
  | grep -o 'https://[^"]*FolioPDF-[^"]*-arm64\.dmg' | head -1)"
if [ -z "$URL" ]; then
  echo "Could not find a download. See https://github.com/$REPO/releases" >&2
  exit 1
fi

DEST="/Applications"
if [ ! -w "$DEST" ]; then
  DEST="$HOME/Applications"
  mkdir -p "$DEST"
fi

TMP="$(mktemp -d)"
MNT=""
cleanup() {
  [ -n "$MNT" ] && hdiutil detach "$MNT" -quiet 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "Downloading $(basename "$URL")..."
curl -fL --progress-bar "$URL" -o "$TMP/folio.dmg"
if curl -fsSL "$URL.sha256" -o "$TMP/folio.sha256" 2>/dev/null; then
  EXPECTED="$(awk '{print $1}' "$TMP/folio.sha256")"
  ACTUAL="$(shasum -a 256 "$TMP/folio.dmg" | awk '{print $1}')"
  if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "Checksum mismatch, aborting." >&2
    exit 1
  fi
  echo "Checksum verified."
fi

echo "Installing to $DEST..."
MNT="$(hdiutil attach "$TMP/folio.dmg" -nobrowse -readonly 2>/dev/null | awk -F'\t' '/\/Volumes\//{print $3; exit}')"
pkill -x FolioPDF 2>/dev/null || true
rm -rf "$DEST/$APP"
ditto "$MNT/$APP" "$DEST/$APP"
xattr -dr com.apple.quarantine "$DEST/$APP" 2>/dev/null || true

echo "✓ Folio PDF installed in $DEST. Launching..."
open "$DEST/$APP"
