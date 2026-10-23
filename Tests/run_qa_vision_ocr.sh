#!/bin/sh
# Build the Vision OCR harness (Tests/qa_vision_ocr.swift + Services/VisionOCR.swift).
#   Tests/run_qa_vision_ocr.sh                 -> builds, prints the binary path
#   Tests/run_qa_vision_ocr.sh in.pdf out.json [--languages ar-SA,en-US] ...  -> builds and runs
set -eu
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SDK="${SDKROOT:-/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk}"
OUT_DIR="$ROOT_DIR/tmp/qa/ocr"
BIN="$OUT_DIR/qa_vision_ocr"
mkdir -p "$OUT_DIR"
SRC1="$ROOT_DIR/Sources/SamPDFStudio/Services/VisionOCR.swift"
SRC2="$ROOT_DIR/Tests/qa_vision_ocr.swift"
if [ ! -x "$BIN" ] || [ "$SRC1" -nt "$BIN" ] || [ "$SRC2" -nt "$BIN" ]; then
  swiftc -O -sdk "$SDK" -target "$(uname -m)-apple-macos14.0" "$SRC1" "$SRC2" -o "$BIN" >&2
fi
if [ "$#" -eq 0 ]; then
  echo "$BIN"
else
  exec "$BIN" "$@"
fi
