#!/usr/bin/env python3
"""Mixed Arabic / Latin / digit text must EXTRACT in logical order.

Other suites check extraction through the engine's own reordering helpers
(page-blocks). This one checks what other readers see, on text written by
every RTL path (add-text, replace-block, move-block, replace-text, OCR layer):

  * raw PyMuPDF page.get_text("text") and the rawdict character order (both
    honour the /ActualText GlyphWriter writes) give the logical string;
  * the engine's glyph-level view (page-blocks) still gives it too;
  * the ActualText structure draws nothing: pixels are identical with and
    without it;
  * redaction / replace-text leave no removed word behind in ActualText.

It also writes tmp/qa/rtl/manifest.json for Tests/qa_pdfkit_rtl.swift
(run from script/qa.sh), which checks PDFKit's page text, rectangle
selection and drag selection (what copy uses) on the same fixture.
Ends with "rtl extraction ok".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
OUT = ROOT / "tmp" / "qa" / "rtl"
sys.path.insert(0, str(ROOT / "Engine"))
from ext import _rtl_text as rt  # noqa: E402

# (text, PDFKit extracts it logically). PDFKit runs its own bidi pass over
# the glyphs and, like for Apple's own CoreText PDFs, swaps a Latin word and
# a following European number inside an RTL line and picks its own paragraph
# direction for lines without a clear majority; those lines are still
# checked for every other reader. The last line has no clear direction
# (more Latin than Arabic letters, Arabic first): the engine lays it out
# left-to-right, and page-blocks (which guesses each row's direction from
# its letters and the page) reads it right-to-left, so only raw extraction
# is checked for it.
AMBIGUOUS = "مرحبا Hello World 2026 عالم"
LINES = [
    ("رقم الفاتورة 12345 بتاريخ 2026-10-23", True),
    ("تم الدفع بمبلغ 1,250.75 ريال", True),
    ("العنوان: شارع الملك فهد، الرياض 11564", True),
    ("التاريخ ٢٠٢٦/١٠/٢٣", True),
    ("Please sign the contract مرحبا today", True),
    ("نسبة النمو 18% في عام 2025 (تقريبا)", True),
    ("رقم الفاتورة 4821 من PDF Studio", True),
    ("مرحبا بكم في التقرير السنوي", True),
    ("مرحبا Hello World 2026 عالم", False),
]
PARAGRAPH = "السطر الأول من الفقرة رقم 1\nالسطر الثاني من الفقرة 2026"
REPLACED = "تقرير المبيعات 2026 لشركة PDF Studio بنسبة نمو ١٨٪"
MOVED = "تم الدفع بمبلغ 1,250.75 ريال"

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("ok   " if condition else "FAIL ") + label)
    if not condition:
        failures.append(label)


def engine(*args: str) -> dict:
    result = subprocess.run([sys.executable, "-W", "ignore", str(ENGINE), *args], capture_output=True, text=True)
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        raise SystemExit(f"engine {args[0]} returned non-JSON:\n{result.stdout}\n{result.stderr}")
    if not reply.get("ok"):
        raise SystemExit(f"engine {args[0]} failed: {reply}\n{result.stderr}")
    return reply


def flat(text: str) -> str:
    return " ".join(text.replace(" ", " ").split())


def rect_str(rect: list[float]) -> str:
    return ",".join(f"{v:.3f}" for v in rect)


def blocks(path: Path, page: int = 1) -> list[dict]:
    return engine("page-blocks", "--input", str(path), "--page", str(page))["blocks"]


def block_with(path: Path, needle: str, page: int = 1) -> dict | None:
    return next((b for b in blocks(path, page) if needle in b["text"]), None)


def build(src: Path) -> tuple[Path, list[dict]]:
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=595, height=842)
    doc.save(str(src))
    current = src
    step = 0

    def next_path() -> Path:
        nonlocal step
        step += 1
        return OUT / f"step{step:02d}.pdf"

    y = 50.0
    for text, _pdfkit in LINES + [(PARAGRAPH, True), ("سطر سيستبدل لاحقا بنص مختلط أطول قليلا من هذا السطر", True), (MOVED, True)]:
        out = next_path()
        engine("add-text", "--input", str(current), "--output", str(out), "--page", "1",
               "--x", "60", "--y", f"{y:.1f}", "--text", text, "--font-size", "15")
        current = out
        y += 50 + 22 * text.count("\n")

    # replace-block: the placeholder line becomes mixed text.
    target = block_with(current, "سيستبدل")
    assert target, "placeholder line not found"
    out = next_path()
    engine("replace-block", "--input", str(current), "--output", str(out), "--page", "1",
           "--rect", rect_str(target["rect"]), "--text", REPLACED, "--original-text", target["text"])
    current = out

    # move-block: the last line moves to the bottom of the page.
    moving = [b for b in blocks(current) if b["text"] == MOVED][-1]
    out = next_path()
    engine("move-block", "--input", str(current), "--output", str(out), "--page", "1",
           "--rect", rect_str(moving["rect"]), "--text", moving["text"],
           "--dest-x", "60", "--dest-y", "760", "--original-text", moving["text"])
    current = out

    # OCR layer: every line again on page 2 as invisible text.
    ocr_lines = []
    oy = 50.0
    for text, _pdfkit in LINES + [(row, True) for row in PARAGRAPH.split("\n")]:
        ocr_lines.append({"text": text, "rect": [60, oy, 520, oy + 20]})
        oy += 46
    payload = OUT / "ocr.json"
    payload.write_text(json.dumps({"pages": [{"page": 2, "lines": ocr_lines}]}, ensure_ascii=False), encoding="utf-8")
    final = OUT / "fixture.pdf"
    engine("add-text-layer", "--input", str(current), "--output", str(final), "--json", str(payload))

    items: list[dict] = []
    pdfkit_ok = dict(LINES)
    for text, ok in [(t, k) for t, k in LINES if t != MOVED] + [(PARAGRAPH, True), (REPLACED, True)]:
        block = next((b for b in blocks(final) if flat(b["text"]) == flat(text)), None)
        if text == AMBIGUOUS:
            block = block or next((b for b in blocks(final) if "Hello World" in b["text"]), None)
        else:
            check(block is not None, f"page-blocks (glyph level) reads {text!r} logically")
        if block:
            items.append({"kind": "page", "page": 1, "rect": block["rect"], "lines": block["line_count"],
                          "expected": text, "pdfkit": pdfkit_ok.get(text, ok)})
    moved = [b for b in blocks(final) if b["text"] == MOVED]
    check(len(moved) == 2 and any(b["rect"][1] > 700 for b in moved), "move-block: moved line found at its new place")
    for b in moved:
        items.append({"kind": "page", "page": 1, "rect": b["rect"], "lines": 1, "expected": MOVED, "pdfkit": True})
    for line, (text, ok) in zip(ocr_lines, LINES + [(row, True) for row in PARAGRAPH.split("\n")]):
        items.append({"kind": "ocr", "page": 2, "rect": line["rect"], "lines": 1, "expected": text, "pdfkit": ok})
    return final, items


def mupdf_checks(path: Path, items: list[dict]) -> None:
    with pymupdf.open(str(path)) as doc:
        for item in items:
            page = doc[item["page"] - 1]
            rect = pymupdf.Rect(item["rect"]) + (-2, -2, 2, 2)
            label = f"[{item['kind']}] {item['expected']!r}"
            text = page.get_text("text", clip=rect)
            check(flat(text) == flat(item["expected"]), f"raw get_text {label} -> {flat(text)!r}")
            raw = page.get_text("rawdict", clip=rect)
            order = " ".join("".join(c["c"] for s in l["spans"] for c in s["chars"])
                             for b in raw["blocks"] for l in b.get("lines", []))
            check(flat(order) == flat(item["expected"]), f"rawdict char order {label}")
        # Word search still lands on the right row for pure Arabic text.
        hits = doc[0].search_for("التقرير")
        row = next(i for i in items if i["expected"] == "مرحبا بكم في التقرير السنوي" and i["kind"] == "page")
        check(bool(hits) and any(row["rect"][1] - 3 <= h.y0 <= row["rect"][3] for h in hits),
              "search_for finds an Arabic word on its row")


def rendering_check() -> None:
    """The ActualText spans and carrier glyphs must not change the pixels."""
    renders = []
    for with_actual in (True, False):
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=300)
        writer = rt.GlyphWriter(page)
        y = 40.0
        for text, _ok in LINES[:7]:
            font = rt.arabic_font_for(text)
            for line in rt.layout_text(text, font, 15, None):
                writer.add_line(font, line, 15, (60, y), color=(0.1, 0.1, 0.4))
                y += 30
        if not with_actual:
            writer.lines = [entry[:5] + (None,) + entry[6:] for entry in writer.lines]
        writer.commit()
        renders.append(page.get_pixmap(dpi=144).samples)
        doc.close()
    check(renders[0] == renders[1], "ActualText structure draws nothing (pixels identical)")


def redaction_checks(path: Path) -> None:
    redacted = OUT / "redacted.pdf"
    engine("redact-text", "--input", str(path), "--output", str(redacted), "--find", "12345")
    with pymupdf.open(str(redacted)) as doc:
        text = doc[0].get_text() + doc[1].get_text()
    check("12345" not in text, "redact-text: the redacted number is gone from extracted text (ActualText too)")
    check(flat(MOVED) in flat(text) and flat(LINES[2][0]) in flat(text),
          "redact-text: untouched lines still extract in logical order")

    replaced = OUT / "replaced.pdf"
    engine("replace-text", "--input", str(path), "--output", str(replaced), "--find", "4821", "--replace", "9999", "--page", "1")
    with pymupdf.open(str(replaced)) as doc:
        text = doc[0].get_text()
    check("4821" not in text and "9999" in text, "replace-text: the old number does not survive in ActualText")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    final, items = build(OUT / "blank.pdf")
    mupdf_checks(final, items)
    rendering_check()
    redaction_checks(final)
    (OUT / "manifest.json").write_text(json.dumps({"pdf": str(final), "items": items}, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    if failures:
        raise SystemExit(f"{len(failures)} check(s) failed: {failures}")
    print("rtl extraction ok")


if __name__ == "__main__":
    main()
