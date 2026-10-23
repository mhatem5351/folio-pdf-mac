#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${SAMPDF_VENV_DIR:-$HOME/Library/Application Support/PDFStudio/engine-venv}"

# Build scratch lives outside iCloud-synced Documents; iCloud touching .build
# mid-build causes "input file was modified during the build" failures.
SCRATCH_DIR="${SAMPDF_SCRATCH_DIR:-$HOME/Library/Caches/PDFStudio/build}"
# Command Line Tools builds need the 26.5 SDK (see script/dev-env.sh).
export SDKROOT="${SDKROOT:-/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk}"

"$ROOT_DIR/script/bootstrap_engine.sh"
(cd "$ROOT_DIR" && swift build --scratch-path "$SCRATCH_DIR")

mkdir -p "$SCRATCH_DIR/qa"
swiftc -sdk "$SDKROOT" "$ROOT_DIR/Sources/SamPDFStudio/Models/DocumentSession.swift" \
  "$ROOT_DIR/Tests/qa_session_semantics.swift" \
  -o "$SCRATCH_DIR/qa/qa_session"
"$SCRATCH_DIR/qa/qa_session"

"$VENV_DIR/bin/python3" "$ROOT_DIR/Tests/qa_ui_invariants.py"
"$VENV_DIR/bin/python3" "$ROOT_DIR/Tests/qa_style_match.py"
"$VENV_DIR/bin/python3" "$ROOT_DIR/Tests/qa_block_move.py"
"$VENV_DIR/bin/python3" "$ROOT_DIR/Tests/qa_pdf_tools.py"
"$VENV_DIR/bin/python3" "$ROOT_DIR/Tests/qa_major_functions.py"
for suite in qa_document qa_stamps qa_forms qa_images qa_convert qa_arabic_edit qa_ocr_layer qa_unicode_args qa_reflow; do
  "$VENV_DIR/bin/python3" -W ignore "$ROOT_DIR/Tests/$suite.py"
done
