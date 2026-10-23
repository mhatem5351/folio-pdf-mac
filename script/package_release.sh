#!/usr/bin/env bash
# Build a self-contained, downloadable Folio PDF for Apple Silicon:
#   dist/FolioPDF-<version>-arm64.dmg (+ .sha256)
#
# The app embeds a relocatable CPython (python-build-standalone) with every
# engine package preinstalled, so it runs on a Mac with no Python, Homebrew or
# internet. Usage:
#   source script/dev-env.sh && ./script/package_release.sh 2.0.0
set -euo pipefail

VERSION="${1:-2.0.0}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_OUT="$ROOT_DIR/dist"

# Pinned interpreter: CPython 3.13.16, python-build-standalone 20261003.
PY_RELEASE="20261003"
PY_ASSET="cpython-3.13.16+${PY_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_SHA256="9e01f63bbb08576cd9c8bc2d0564d098cb30c8453a0cd4bcf6aef458f6d2a147"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/${PY_ASSET//+/%2B}"
PY_CACHE="$HOME/Library/Caches/FolioPDF/python-dist/$PY_ASSET"

log() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }

# 1. App bundle (optimized, no machine-specific Info.plist keys).
export FOLIO_RELEASE=1 FOLIO_VERSION="$VERSION"
export FOLIO_SCRATCH_DIR="${FOLIO_SCRATCH_DIR:-$HOME/Library/Caches/FolioPDF/build}-release"
export FOLIO_DIST_DIR="$HOME/Builds/FolioPDF-release.noindex"
log "Building Folio PDF $VERSION (release)"
"$ROOT_DIR/script/build_and_run.sh" build
APP="$FOLIO_DIST_DIR/Folio PDF.app"
RES="$APP/Contents/Resources"

# 2. Embedded Python.
if [[ ! -f "$PY_CACHE" ]]; then
  log "Downloading $PY_ASSET"
  mkdir -p "$(dirname "$PY_CACHE")"
  curl -fL --retry 3 -o "$PY_CACHE.part" "$PY_URL"
  mv "$PY_CACHE.part" "$PY_CACHE"
fi
echo "$PY_SHA256  $PY_CACHE" | shasum -a 256 -c - >/dev/null || { echo "checksum mismatch: $PY_CACHE" >&2; exit 1; }

log "Embedding Python + engine packages"
rm -rf "$RES/python"
tar -xzf "$PY_CACHE" -C "$RES"
PY="$RES/python/bin/python3"
"$PY" -m pip install --quiet --disable-pip-version-check --no-cache-dir \
  --only-binary=:all: --no-deps -r "$ROOT_DIR/Engine/requirements-bundle.txt"
"$PY" -c "import fitz, pypdf, PIL, reportlab, openpyxl, pptx, docx, pdf2docx, uharfbuzz, fontTools, markdown_it"

# Third-party license summary, generated from the installed metadata.
"$PY" - "$RES/THIRD_PARTY_NOTICES.md" <<'PYCODE'
import sys
from importlib import metadata
rows = []
for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
    name = dist.metadata["Name"]
    if name.lower() in {"pip", "setuptools", "wheel"}:
        continue
    lic = dist.metadata.get("License-Expression") or ""
    if not lic:
        classifiers = [c.split("::")[-1].strip() for c in dist.metadata.get_all("Classifier") or [] if c.startswith("License ::")]
        lic = ", ".join(classifiers) or (dist.metadata.get("License") or "see package").splitlines()[0][:80]
    home = dist.metadata.get("Home-page") or next((u.split(",", 1)[1].strip() for u in dist.metadata.get_all("Project-URL") or []), "")
    rows.append(f"| {name} | {dist.version} | {lic} | {home} |")
with open(sys.argv[1], "w") as fh:
    fh.write("# Third-party software in Folio PDF release builds\n\n")
    fh.write("Release builds embed CPython 3.13 (python-build-standalone, PSF License 2.0) and these packages.\n")
    fh.write("PyMuPDF/MuPDF is AGPL-3.0: Folio PDF's complete source is public at https://github.com/mhatem5351/folio-pdf-mac.\n\n")
    fh.write("| Package | Version | License | Homepage |\n|---|---|---|---|\n")
    fh.write("\n".join(rows) + "\n")
PYCODE

# Slim it down: no pip, tests, Tk/IDLE, or headers; precompile bytecode
# (the signed bundle is read-only at runtime).
"$PY" -m pip uninstall --quiet -y pip setuptools wheel >/dev/null 2>&1 || true
STD="$RES/python/lib/python3.13"
rm -rf "$RES/python/include" "$RES/python/share" "$RES/python/lib/pkgconfig" \
  "$RES/python/lib/"tcl* "$RES/python/lib/"tk* "$RES/python/lib/"itcl* "$RES/python/lib/"thread* \
  "$RES/python/lib/"libtcl* "$RES/python/lib/"libtk* \
  "$STD/"{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data} "$STD/"config-3.13-darwin \
  "$STD/lib-dynload/_tkinter"*.so
find "$RES/python" -type d \( -name tests -o -name test -o -name __pycache__ \) -prune -exec rm -rf {} +
find "$RES/python/bin" ! -type d ! -name 'python3*' -delete
find "$RES/python" -type l ! -exec test -e {} \; -delete   # links into removed parts
"$PY" -m compileall -q -j 0 "$RES/python/lib" "$RES"/pdf_engine.py "$RES"/ext >/dev/null || true

# 3. Smoke test the embedded engine before signing.
log "Engine health check"
"$PY" -W ignore "$RES/pdf_engine.py" health >/dev/null || true
"$PY" -W ignore -c "
import fitz, subprocess, sys, json, tempfile, os
d = fitz.open(); d.new_page().insert_text((72, 72), 'Folio PDF release check'); p = os.path.join(tempfile.mkdtemp(), 'a.pdf'); d.save(p)
out = subprocess.run([sys.executable, '$RES/pdf_engine.py', 'page-blocks', '--input', p, '--page', '1'], capture_output=True, text=True).stdout
assert 'release check' in out, out
print('engine ok')"

# 4. Ad-hoc signature (no Apple Developer ID): every Mach-O, then the app.
log "Signing (ad-hoc)"
find "$RES/python" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0 |
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q Mach-O; then codesign --force --sign - --timestamp=none "$f"; fi
  done
codesign --force --sign - --timestamp=none "$APP"
codesign --verify --deep --strict "$APP"

# 5. DMG with a drag-to-Applications layout.
log "Creating DMG"
mkdir -p "$DIST_OUT"
STAGE="$(mktemp -d)"
ditto "$APP" "$STAGE/Folio PDF.app"
ln -s /Applications "$STAGE/Applications"
cp "$RES/THIRD_PARTY_NOTICES.md" "$STAGE/Third-party notices.md"
DMG="$DIST_OUT/FolioPDF-$VERSION-arm64.dmg"
rm -f "$DMG"
hdiutil create -quiet -volname "Folio PDF $VERSION" -srcfolder "$STAGE" -fs HFS+ -format ULFO -ov "$DMG"
rm -rf "$STAGE"
(cd "$DIST_OUT" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
log "Done: $DMG ($(du -h "$DMG" | cut -f1))"
cat "$DMG.sha256"
