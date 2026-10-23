"""Editing a loosely spaced paragraph keeps its line spacing.

    "$FOLIO_PY" Tests/qa_line_pitch.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"


def engine(*args: str) -> dict:
    out = subprocess.run([sys.executable, "-W", "ignore", str(ENGINE), *args], capture_output=True, text=True)
    return json.loads(out.stdout)


def baselines(path: Path, y0: float, y1: float) -> list[float]:
    with fitz.open(str(path)) as doc:
        return [line["spans"][0]["origin"][1] for block in doc[0].get_text("dict")["blocks"]
                for line in block.get("lines", []) if y0 < line["spans"][0]["origin"][1] < y1]


def main() -> int:
    failures = []
    tmp = Path(tempfile.mkdtemp(prefix="folio-pitch-"))
    for lineheight in (1.0, 1.45, 1.8):
        src, out = tmp / f"src-{lineheight}.pdf", tmp / f"out-{lineheight}.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(56, 100, 500, 300), "First line of the paragraph\nsecond line of the paragraph\nthird line",
                            fontsize=11, fontname="helv", lineheight=lineheight)
        doc.save(str(src))
        before = baselines(src, 90, 300)
        block = engine("block-at", "--input", str(src), "--page", "1", "--x", "80", "--y", str(before[0]))
        pitch = before[1] - before[0]
        if abs(block["line_height"] - pitch) > 0.3:
            failures.append(f"lineheight {lineheight}: block-at reports {block['line_height']:.2f}, real pitch {pitch:.2f}")
        reply = engine("replace-block", "--input", str(src), "--output", str(out), "--page", "1",
                       "--rect", ",".join(map(str, block["rect"])), "--text", block["text"] + " and a fourth line appears",
                       "--original-text", block["text"], "--line-height", str(block["line_height"]))
        after = baselines(out, 90, 300)
        steps = [b - a for a, b in zip(after, after[1:])]
        if not reply.get("ok") or not steps or max(abs(s - pitch) for s in steps) > 0.5:
            failures.append(f"lineheight {lineheight}: pitch {pitch:.2f} became {[round(s, 2) for s in steps]}")
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("line pitch ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
