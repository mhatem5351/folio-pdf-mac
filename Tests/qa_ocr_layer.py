#!/usr/bin/env python3
"""Invisible OCR text layer (engine add-text-layer) + Apple Vision harness.

1. Synthetic lines (no Vision): quads with known geometry on an image page,
   including an Arabic line and a 90-degree rotated page. Checks render mode 3
   (nothing visible), PyMuPDF search/extraction, Arabic in logical order and
   that the selectable text covers the quad it came from.
2. Real recognition: renders English + Arabic text to an IMAGE-ONLY PDF,
   runs Services/VisionOCR.swift through Tests/qa_vision_ocr.swift (compiled
   by Tests/run_qa_vision_ocr.sh) with ar-SA + en-US, feeds its JSON to
   add-text-layer, and checks search/extraction in PyMuPDF and PDFKit.
   Skipped (with a note) when swiftc or Vision Arabic support is missing.
Ends with "ocr layer ok".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
OUT = ROOT / "tmp" / "qa" / "ocr"
SDK = "/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk"
ARCHIVE = pymupdf.Archive("/System/Library/Fonts/Supplemental")
CSS = ("@font-face {font-family: ar; src: url('Arial.ttf');} "
       "body {font-family: ar; font-size: 22pt; color: #111;} p {margin: 0 0 14pt 0;}")

ENGLISH = ["Quarterly revenue grew strongly this year.", "Invoice number 4821 is due on Monday."]
ARABIC = ["مرحبا بكم في التقرير السنوي", "نمت الإيرادات بشكل كبير هذا العام", "رقم الفاتورة 4821 بتاريخ 2026"]

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


def text_image(width: float, height: float, dpi: int = 200) -> pymupdf.Pixmap:
    """English paragraph, then right-to-left Arabic, rendered to pixels."""
    scratch = pymupdf.open()
    page = scratch.new_page(width=width, height=height)
    page.insert_htmlbox(pymupdf.Rect(50, 60, width - 50, 220), "".join(f"<p>{t}</p>" for t in ENGLISH),
                        css=CSS, archive=ARCHIVE)
    page.insert_htmlbox(pymupdf.Rect(50, 240, width - 50, 420),
                        '<div dir="rtl">' + "".join(f"<p>{t}</p>" for t in ARABIC) + "</div>",
                        css=CSS, archive=ARCHIVE)
    return page.get_pixmap(dpi=dpi)


def make_scan(path: Path) -> None:
    """p1: image only; p2: real text (skip-text target); p3: image page shown
    rotated 90 degrees (the scan is stored sideways, /Rotate makes it upright)."""
    doc = pymupdf.open()
    pix = text_image(595, 842)
    p1 = doc.new_page(width=595, height=842)
    p1.insert_image(p1.rect, pixmap=pix)
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((72, 100), "This page already has a real text layer.", fontsize=14)
    # Page 3: unrotated it is 842 wide x 595 high with the image turned -90
    # degrees; /Rotate 90 turns it back upright for the viewer.
    p3 = doc.new_page(width=842, height=595)
    p3.insert_image(p3.rect, pixmap=pix, rotate=90)
    p3.set_rotation(270)
    doc.save(str(path), garbage=3, deflate=True)


def synthetic_checks() -> None:
    src = OUT / "synthetic_src.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))  # an "image" page with no text
    doc.save(str(src))

    lines = [
        {"text": "Hello searchable world", "quad": [[100, 100], [400, 100], [100, 124], [400, 124]], "confidence": 0.99},
        {"text": "مرحبا بكم في التقرير السنوي", "quad": [[150, 200], [480, 200], [150, 228], [480, 228]], "confidence": 0.97},
        {"text": "رقم الفاتورة 4821 من PDF Studio", "rect": [120, 300, 470, 326], "confidence": 0.9},
        # A slightly skewed line (rotated ~3 degrees).
        {"text": "Skewed line of text", "quad": [[100, 400], [350, 413], [99, 424], [349, 437]], "confidence": 0.8},
        {"text": "", "rect": [0, 0, 10, 10]},  # empty: skipped
    ]
    payload = OUT / "synthetic.json"
    payload.write_text(json.dumps({"pages": [{"page": 1, "lines": lines}]}, ensure_ascii=False), encoding="utf-8")
    out = OUT / "synthetic_ocr.pdf"
    reply = engine("add-text-layer", "--input", str(src), "--output", str(out), "--json", str(payload))
    check(reply["lines"] == 4 and reply["skipped_lines"] == 1 and reply["pages"] == 1, "add-text-layer: reply counts lines/skips")

    with pymupdf.open(str(out)) as result:
        page = result[0]
        text = page.get_text()
        check("Hello searchable world" in text, "synthetic: English line extracts")
        check("مرحبا بكم في التقرير السنوي" in text, "synthetic: Arabic line extracts in logical order")
        hits = page.search_for("searchable")
        check(bool(hits) and 100 <= hits[0].x0 and hits[0].x1 <= 400.5 and 98 <= hits[0].y0 <= 104 and hits[0].y1 <= 126,
              "synthetic: English word found inside its quad")
        whole = page.search_for("Hello searchable world")
        check(bool(whole) and abs(whole[0].x0 - 100) < 1.5 and abs(whole[0].x1 - 400) < 1.5,
              "synthetic: selection spans the quad's full width (horizontal scaling)")
        arabic = page.search_for("التقرير")
        check(bool(arabic) and all(150 <= r.x0 and r.x1 <= 480.5 and 196 <= r.y0 for r in arabic),
              "synthetic: Arabic word found inside its quad")
        check(bool(page.search_for("Skewed")), "synthetic: skewed line searchable")
        contents = b"".join(result.xref_stream(x) for x in page.get_contents())
        check(b"3 Tr" in contents, "synthetic: text uses render mode 3 (invisible)")
        pix = page.get_pixmap(dpi=40, colorspace=pymupdf.csGRAY)
        check(min(pix.samples) > 250, "synthetic: nothing visible is drawn")
        check("رقم الفاتورة 4821 من PDF Studio" in text, "synthetic: raw get_text has the mixed line in logical order")
        # Glyph level (what the engine's editor reads): rebuilt from positions.
        glyph_flags = pymupdf.TEXTFLAGS_RAWDICT | pymupdf.TEXT_IGNORE_ACTUALTEXT
        sys.path.insert(0, str(ROOT / "Engine"))
        from ext import _rtl_text as rt  # the engine's own logical-order helper

        data = page.get_text("rawdict", flags=glyph_flags)
        rt.prepare_glyph_chars(page, data)
        rows = [l for b in data["blocks"] for l in b.get("lines", [])]
        mixed = [l for l in rows if any("4821" in "".join(c["c"] for c in s["chars"]) for s in l["spans"])]

        logical = rt.logical_text_from_chars([c for s in mixed[0]["spans"] for c in s["chars"]]) if mixed else ""
        check(logical == "رقم الفاتورة 4821 من PDF Studio", "synthetic: mixed Arabic/digits/Latin line rebuilds logically")

    # Bad input is rejected with a message, not a crash.
    bad = OUT / "bad.json"
    bad.write_text(json.dumps({"pages": [{"page": 9, "lines": lines}]}), encoding="utf-8")
    result = subprocess.run([sys.executable, "-W", "ignore", str(ENGINE), "add-text-layer", "--input", str(src),
                             "--output", str(OUT / "bad.pdf"), "--json", str(bad)], capture_output=True, text=True)
    reply = json.loads(result.stdout)
    check(reply.get("ok") is False and "out of range" in reply.get("message", ""), "add-text-layer: out-of-range page rejected")


def pdfkit_probe() -> Path | None:
    if not shutil.which("swiftc") or not Path(SDK).exists():
        return None
    source = OUT / "pdfkit_probe.swift"
    binary = OUT / "pdfkit_probe"
    source.write_text(
        "import Foundation\nimport PDFKit\n"
        "let a = CommandLine.arguments\n"
        "guard let d = PDFDocument(url: URL(fileURLWithPath: a[1])) else { exit(1) }\n"
        "var hits: [String: [Int]] = [:]\n"
        "for t in a.dropFirst(2) { hits[t] = d.findString(t, withOptions: []).compactMap { s in s.pages.first.map { d.index(for: $0) + 1 } } }\n"
        "var texts: [String] = []\n"
        "for i in 0..<d.pageCount { texts.append(d.page(at: i)?.string ?? \"\") }\n"
        "let o: [String: Any] = [\"texts\": texts, \"hits\": hits]\n"
        "print(String(data: try! JSONSerialization.data(withJSONObject: o), encoding: .utf8)!)\n",
        encoding="utf-8",
    )
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        result = subprocess.run(["swiftc", "-O", "-sdk", SDK, str(source), "-o", str(binary)], capture_output=True, text=True)
        if result.returncode != 0:
            return None
    return binary


def vision_checks() -> None:
    if not shutil.which("swiftc") or not Path(SDK).exists():
        print("note: swiftc unavailable; Vision checks skipped")
        return
    build = subprocess.run([str(ROOT / "Tests" / "run_qa_vision_ocr.sh")], capture_output=True, text=True,
                           env={**os.environ, "SDKROOT": os.environ.get("SDKROOT", SDK)})
    if build.returncode != 0:
        check(False, "Vision harness compiles:\n" + build.stderr[-2000:])
        return
    harness = build.stdout.strip().splitlines()[-1]
    languages = subprocess.run([harness, "--languages-only"], capture_output=True, text=True).stdout.strip().split(",")
    if not any(code.startswith("ar") for code in languages):
        print("note: Vision on this Mac has no Arabic model; Vision checks skipped")
        return

    scan = OUT / "scan.pdf"
    make_scan(scan)
    with pymupdf.open(str(scan)) as doc:
        check(not doc[0].get_text().strip(), "fixture: page 1 is image-only")

    lines_json = OUT / "vision.json"
    run = subprocess.run([harness, str(scan), str(lines_json), "--languages", "ar-SA,en-US", "--skip-text"],
                         capture_output=True, text=True, timeout=600)
    check(run.returncode == 0, "Vision harness runs")
    if run.returncode != 0:
        print(run.stdout, run.stderr)
        return
    summary = json.loads(run.stdout.strip().splitlines()[-1])
    check(summary.get("revision", 0) >= 3, f"Vision uses revision 3+ (got {summary.get('revision')})")
    check(summary.get("skipped") == [2], "Vision: page with a text layer skipped")
    check(summary.get("progress_events", 0) >= 3, "Vision: progress reported per page")
    data = json.loads(lines_json.read_text(encoding="utf-8"))
    by_page = {p["page"]: p for p in data["pages"]}
    recognized = " ".join(line["text"] for line in by_page[1]["lines"])
    check("Quarterly" in recognized and "Monday" in recognized, "Vision recognized the English lines")
    check("التقرير" in recognized and "الإيرادات" in recognized, "Vision recognized the Arabic lines (logical order)")

    # Quads are in engine coordinates: the Arabic lines sit in the right
    # place on the 595x842 page (they were laid out from y=240 down).
    arabic_lines = [l for l in by_page[1]["lines"] if "التقرير" in l["text"]]
    if arabic_lines:
        x0, y0, x1, y1 = arabic_lines[0]["rect"]
        check(230 < y0 < 300 and 40 < x0 and x1 < 560, "Vision quad maps to engine coordinates (unrotated page)")

    # Rotated page: displayed upright, quads converted to the unrotated page.
    rotated = [l for l in by_page.get(3, {}).get("lines", []) if "Quarterly" in l["text"]]
    check(bool(rotated), "Vision recognized the rotated page")

    ocr_pdf = OUT / "scan_ocr.pdf"
    reply = engine("add-text-layer", "--input", str(scan), "--output", str(ocr_pdf), "--json", str(lines_json))
    check(reply["pages"] == 2 and reply["lines"] >= 8, "add-text-layer wrote the recognized lines")
    with pymupdf.open(str(ocr_pdf)) as doc:
        page1 = doc[0]
        text1 = page1.get_text()
        check(bool(page1.search_for("Quarterly")) and bool(page1.search_for("Monday")), "OCR'd page: English words searchable")
        check("التقرير" in text1 and "الإيرادات" in text1, "OCR'd page: Arabic words extract")
        flat1 = " ".join(text1.split())
        missing = [l["text"] for l in by_page[1]["lines"] if " ".join(l["text"].split()) not in flat1]
        check(not missing, f"OCR'd page: raw get_text has every recognized line in logical order {missing}")
        check("This page already has a real text layer." in doc[1].get_text(), "skipped page untouched")
        page3 = doc[2]
        hits = page3.search_for("Quarterly")
        if rotated and hits:
            qx0, qy0, qx1, qy1 = rotated[0]["rect"]
            hit = hits[0]
            overlap = max(0, min(hit.x1, qx1) - max(hit.x0, qx0)) * max(0, min(hit.y1, qy1) - max(hit.y0, qy0))
            check(overlap > 0, "rotated page: invisible text lands on the recognized quad")
        else:
            check(False, "rotated page: English word searchable")

    probe = pdfkit_probe()
    if probe:
        terms = ["Quarterly", "Monday", "مرحبا", "التقرير", "الإيرادات"]
        result = subprocess.run([str(probe), str(ocr_pdf), *terms], capture_output=True, text=True)
        found = json.loads(result.stdout)
        for term in terms:
            check(1 in found["hits"].get(term, []), f"PDFKit finds {term!r} on the OCR'd page")
        page_text = " ".join(found["texts"][0].split())
        arabic_rows = [l["text"] for l in by_page[1]["lines"] if any("؀" <= ch <= "ۿ" for ch in l["text"])]
        missing = [t for t in arabic_rows if " ".join(t.split()) not in page_text]
        check(not missing, f"PDFKit page text has every recognized Arabic line in logical order {missing}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    synthetic_checks()
    vision_checks()
    if failures:
        raise SystemExit(f"{len(failures)} check(s) failed: {failures}")
    print("ocr layer ok")


if __name__ == "__main__":
    main()
