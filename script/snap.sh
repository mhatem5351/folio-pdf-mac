#!/usr/bin/env bash
# Ask a running QA build (FOLIO_QA=1 / FolioQA default) to save its window as PNG.
out="${1:?usage: snap.sh out.png}"
rm -f "$out"
swift -e "import Foundation; DistributedNotificationCenter.default().postNotificationName(.init(\"io.github.mhatem5351.FolioPDF.snapshot\"), object: \"$out\", userInfo: nil, deliverImmediately: true)" 2>/dev/null
for _ in $(seq 1 30); do [ -s "$out" ] && exit 0; sleep 0.2; done
echo "snapshot timed out" >&2; exit 1
