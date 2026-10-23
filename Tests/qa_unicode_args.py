#!/usr/bin/env python3
"""Regression: the app's Process hands arguments over in NFD. Editing an Arabic
block whose text contains hamza forms (إ) must still erase every original line.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp" / "qa" / "unicode_args"
TMP.mkdir(parents=True, exist_ok=True)
ENGINE = [sys.executable, str(ROOT / "Engine" / "pdf_engine.py")]


def run(*args: str) -> dict:
    result = subprocess.run(ENGINE + list(args), capture_output=True, text=True)
    payload = json.loads(result.stdout)
    assert payload.get("ok"), payload
    return payload


def main() -> int:
    env = dict(os.environ, SAMPLES_DIR=str(TMP))
    subprocess.run([sys.executable, str(ROOT / "Tests" / "fixtures" / "make_samples.py")], check=True, env=env, capture_output=True)
    sample = TMP / "report.pdf"
    block = run("block-at", "--input", str(sample), "--page", "1", "--x", "300", "--y", "590")
    assert "الإيراد" in block["text"], block["text"]
    rect = ",".join(str(v) for v in block["rect"])
    out = TMP / "edited.pdf"
    nfd = lambda s: unicodedata.normalize("NFD", s)
    run("replace-block", "--input", str(sample), "--output", str(out), "--page", "1",
        "--rect", rect, "--text", nfd("hello replaced text"),
        "--original-text", nfd(block["text"]), "--line-height", str(block["line_height"]))
    page = pymupdf.open(out)[0]
    left = [s["text"] for b in page.get_text("dict")["blocks"] if b["type"] == 0
            for l in b["lines"] for s in l["spans"] if s["font"] == "GeezaPro"]
    assert not left, f"original Arabic glyphs survived the edit: {left}"
    assert "hello replaced text" in page.get_text()
    print("unicode args ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
