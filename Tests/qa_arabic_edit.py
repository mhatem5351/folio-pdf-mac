#!/usr/bin/env python3
"""Arabic / RTL in-place editing through the engine commands the app uses.

Builds a fixture with an Arabic heading, a wrapped Arabic paragraph (both via
insert_htmlbox with an Arabic font) and an English paragraph, then drives:
  page-blocks -> replace-block (reflow + per-line), add-text, replace-text,
  move-block
and checks after every step that
  * the edited text extracts as the LOGICAL Arabic string (page-blocks, which
    feeds the editor, plus PyMuPDF get_text/search for pure-Arabic runs),
  * mixed Arabic + Latin + digits round-trips in reading order,
  * RTL text is right-aligned by default and keeps the block's right edge,
  * the English paragraph is untouched, and the region renders non-blank.
When swiftc is available, PDFKit (what the app's search/selection uses) is
checked on the final file too. Ends with "arabic edit ok".
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
OUT = ROOT / "tmp" / "qa" / "arabic"
SDK = "/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk"

HEADING = "التقرير السنوي للشركة"
PARAGRAPH = ("نمت الإيرادات بشكل كبير خلال هذا العام بفضل الاشتراكات الجديدة في منطقة الخليج "
             "وبقيت التكاليف التشغيلية مستقرة بينما انخفض الإنفاق على التسويق بشكل ملحوظ")
ENGLISH = ("Revenue grew compared with the previous quarter, driven mainly by new "
           "subscriptions in the Gulf region. This paragraph must never change.")

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


def make_fixture(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    arch = pymupdf.Archive("/System/Library/Fonts/Supplemental")
    css = ("@font-face {font-family: ar; src: url('Arial.ttf');} "
           "body {font-family: ar; font-size: 15pt; color: #1f3a93;} div {text-align: right;}")
    page.insert_htmlbox(pymupdf.Rect(60, 60, 535, 90), f'<div dir="rtl">{HEADING}</div>', css=css, archive=arch)
    page.insert_htmlbox(pymupdf.Rect(60, 150, 535, 240), f'<div dir="rtl" style="font-size: 13pt; color: #222222">{PARAGRAPH}</div>',
                        css=css, archive=arch)
    page.insert_textbox(pymupdf.Rect(60, 260, 535, 330), ENGLISH, fontname="tiro", fontsize=12)
    doc.save(str(path), garbage=3, deflate=True)


def blocks(path: Path) -> list[dict]:
    return engine("page-blocks", "--input", str(path), "--page", "1")["blocks"]


def block_containing(path: Path, needle: str) -> dict | None:
    for block in blocks(path):
        if needle in block["text"]:
            return block
    return None


def flat(text: str) -> str:
    """Rows of a wrapped paragraph joined back into one line."""
    return " ".join(text.split("\n"))


def rect_str(rect: list[float]) -> str:
    return ",".join(f"{v:.3f}" for v in rect)


def ink_ratio(path: Path, rect: pymupdf.Rect) -> float:
    with pymupdf.open(str(path)) as doc:
        pix = doc[0].get_pixmap(dpi=96, clip=rect, colorspace=pymupdf.csGRAY)
        samples = pix.samples
        dark = sum(1 for value in samples if value < 160)
        return dark / max(1, len(samples))


def text_in(path: Path, rect: pymupdf.Rect | None = None) -> str:
    with pymupdf.open(str(path)) as doc:
        return doc[0].get_text(clip=rect) if rect else doc[0].get_text()


def pdfkit_probe() -> Path | None:
    """Compile a tiny PDFKit text/search probe (skipped without swiftc)."""
    if not shutil.which("swiftc") or not Path(SDK).exists():
        return None
    source = OUT / "pdfkit_probe.swift"
    binary = OUT / "pdfkit_probe"
    source.write_text(
        "import Foundation\nimport PDFKit\n"
        "let a = CommandLine.arguments\n"
        "guard let d = PDFDocument(url: URL(fileURLWithPath: a[1])) else { exit(1) }\n"
        "var hits: [String: Int] = [:]\n"
        "for t in a.dropFirst(2) { hits[t] = d.findString(t, withOptions: []).count }\n"
        "let o: [String: Any] = [\"text\": d.page(at: 0)?.string ?? \"\", \"hits\": hits]\n"
        "print(String(data: try! JSONSerialization.data(withJSONObject: o), encoding: .utf8)!)\n",
        encoding="utf-8",
    )
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        result = subprocess.run(["swiftc", "-O", "-sdk", SDK, str(source), "-o", str(binary)], capture_output=True, text=True)
        if result.returncode != 0:
            print("note: PDFKit probe did not compile; skipping PDFKit checks\n" + result.stderr[-800:])
            return None
    return binary


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    src = OUT / "arabic_fixture.pdf"
    make_fixture(src)

    english_rect = pymupdf.Rect(55, 255, 540, 335)
    english_before = text_in(src, english_rect)

    # --- 1. the editor sees logical Arabic -----------------------------------
    heading = block_containing(src, "التقرير")
    para = block_containing(src, "الإيرادات")
    check(heading is not None and heading["text"] == HEADING, "page-blocks: heading extracts as logical Arabic")
    check(para is not None and para["line_count"] >= 2, "page-blocks: Arabic paragraph is one multi-line block")
    para_words = para["text"].split() if para else []
    check(para is not None and para_words[:3] == PARAGRAPH.split()[:3], "page-blocks: paragraph starts with its first logical words")
    assert heading and para

    # --- 2. replace-block with mixed text (reflow path) ----------------------
    mixed = "تقرير المبيعات 2026 لشركة PDF Studio بنسبة نمو ١٨٪"
    step1 = OUT / "step1_replace_heading.pdf"
    engine("replace-block", "--input", str(src), "--output", str(step1), "--page", "1",
           "--rect", rect_str(heading["rect"]), "--text", mixed,
           "--original-text", heading["text"], "--line-height", str(heading["line_height"]))
    edited = block_containing(step1, "المبيعات")
    check(edited is not None and flat(edited["text"]) == mixed, "replace-block: mixed Arabic/Latin/digits extract in logical order")
    check(edited is not None and edited["line_count"] >= 2, "replace-block: longer text wraps inside one paragraph block")
    check("التقرير السنوي" not in text_in(step1), "replace-block: original heading is gone")
    plain = text_in(step1)
    check("المبيعات" in plain and "Studio" in plain and "2026" in plain, "replace-block: PyMuPDF get_text sees the new words")
    with pymupdf.open(str(step1)) as doc:
        hits = doc[0].search_for("المبيعات")
    check(bool(hits) and abs(hits[0].y0 - heading["rect"][1]) < 15, "replace-block: search_for finds the Arabic word in place")
    if edited:
        check(abs(edited["rect"][2] - heading["rect"][2]) < 3.0, "replace-block: RTL text right-aligned to the block's right edge")
        check(abs(edited["fontsize"] - heading["fontsize"]) < 0.6, "replace-block: font size preserved")
        check(edited["color"].lower() == heading["color"].lower(), "replace-block: color preserved")
    check(ink_ratio(step1, pymupdf.Rect(heading["rect"]) + (-40, -2, 2, 2)) > 0.02, "replace-block: edited region renders (not blank)")
    check(text_in(step1, english_rect) == english_before, "replace-block: English paragraph untouched")
    with pymupdf.open(str(step1)) as doc:
        fonts = [f[3] for f in doc[0].get_fonts()]
    check(any("Geeza" in f or "Arial" in f or "Tahoma" in f for f in fonts if "+" in f), "replace-block: an Arabic-capable subset font is embedded")

    # --- 3. per-line edit keeps every line's right edge -----------------------
    lines_before = para["text"].split("\n")
    changed = [lines_before[0].replace("الإيرادات", "الأرباح", 1)] + lines_before[1:]
    step2 = OUT / "step2_word_edit.pdf"
    engine("replace-block", "--input", str(step1), "--output", str(step2), "--page", "1",
           "--rect", rect_str(para["rect"]), "--text", "\n".join(changed),
           "--original-text", para["text"], "--line-height", str(para["line_height"]))
    para2 = block_containing(step2, "الأرباح")
    check(para2 is not None and para2["text"] == "\n".join(changed), "replace-block per-line: edited paragraph extracts logically")
    if para2:
        check(abs(para2["rect"][2] - para["rect"][2]) < 3.0, "replace-block per-line: right edge kept")
        check(abs(para2["rect"][1] - para["rect"][1]) < 3.0, "replace-block per-line: top kept")
    check("الإيرادات" not in text_in(step2), "replace-block per-line: replaced word is gone")
    check(text_in(step2, english_rect) == english_before, "replace-block per-line: English paragraph untouched")

    # --- 4. add-text: new Arabic text, right-aligned in its own box ----------
    new_text = "ملاحظة: تمت المراجعة في ٢٣ أكتوبر"
    step3 = OUT / "step3_add_text.pdf"
    engine("add-text", "--input", str(step2), "--output", str(step3), "--page", "1",
           "--x", "300", "--y", "400", "--text", new_text, "--font-size", "14", "--color", "#c0392b")
    added = block_containing(step3, "ملاحظة")
    check(added is not None and added["text"] == new_text, "add-text: Arabic with Arabic-Indic digits extracts logically")
    if added:
        check(abs(added["rect"][0] - 300) < 3.0, "add-text: block starts at the click x")
        check(added["color"].lower() == "#c0392b", "add-text: color applied")
    check(ink_ratio(step3, pymupdf.Rect(295, 395, 535, 425)) > 0.02, "add-text: renders")

    multi = "سطر أول طويل نسبيا\nسطر ثان"
    step3b = OUT / "step3b_add_multiline.pdf"
    engine("add-text", "--input", str(step3), "--output", str(step3b), "--page", "1",
           "--x", "100", "--y", "460", "--text", multi, "--font-size", "12")
    added2 = block_containing(step3b, "سطر أول")
    check(added2 is not None and added2["text"] == multi, "add-text: multi-line Arabic extracts logically")
    with pymupdf.open(str(step3b)) as doc:
        rows = [l for b in doc[0].get_text("dict", clip=pymupdf.Rect(90, 450, 400, 520))["blocks"] for l in b.get("lines", [])]
    if len(rows) >= 2:
        check(abs(rows[0]["bbox"][2] - rows[1]["bbox"][2]) < 1.5, "add-text: RTL lines share a right edge (auto alignment)")
    else:
        check(False, "add-text: expected two rendered rows")

    # --- 5. replace-text: find an Arabic word, replace it --------------------
    step4 = OUT / "step4_replace_text.pdf"
    reply = engine("replace-text", "--input", str(step3b), "--output", str(step4), "--find", "المراجعة", "--replace", "الموافقة")
    check(reply.get("matches", 0) >= 1, "replace-text: Arabic search term matched")
    note = block_containing(step4, "ملاحظة")
    check(note is not None and "الموافقة" in note["text"] and "المراجعة" not in text_in(step4), "replace-text: Arabic replacement extracts logically")

    # --- 6. move-block keeps the Arabic text intact --------------------------
    moved_src = block_containing(step4, "المبيعات")
    assert moved_src
    step5 = OUT / "step5_move.pdf"
    engine("move-block", "--input", str(step4), "--output", str(step5), "--page", "1",
           "--rect", rect_str(moved_src["rect"]), "--text", moved_src["text"],
           "--dest-x", "60", "--dest-y", "560", "--original-text", moved_src["text"])
    moved = block_containing(step5, "المبيعات")
    check(moved is not None and flat(moved["text"]) == mixed, "move-block: moved Arabic block extracts logically")
    if moved:
        check(abs(moved["rect"][1] - 560) < 6, "move-block: lands at the drop point")

    # --- 7. color runs on Arabic ---------------------------------------------
    target = block_containing(step5, "الأرباح")
    assert target
    first_word = target["text"].split()[0]
    runs = json.dumps([{"start": 0, "length": len(first_word), "hex": "#e5484d"}])
    step6 = OUT / "step6_color_runs.pdf"
    engine("replace-block", "--input", str(step5), "--output", str(step6), "--page", "1",
           "--rect", rect_str(target["rect"]), "--text", target["text"].replace("\n", " "),
           "--original-text", target["text"], "--color-runs", runs)
    with pymupdf.open(str(step6)) as doc:
        colors = {span["color"] for b in doc[0].get_text("dict", clip=pymupdf.Rect(target["rect"]) + (-60, -2, 2, 60))["blocks"]
                  for l in b.get("lines", []) for span in l["spans"]}
    check(0xE5484D in colors, "color runs: the first Arabic word is drawn in the run color")
    final_para = block_containing(step6, "الأرباح")
    check(final_para is not None and final_para["text"].replace("\n", " ") == target["text"].replace("\n", " "),
          "color runs: text still extracts logically")
    check(text_in(step6, english_rect) == english_before, "English paragraph untouched after every edit")

    # --- 8. the shipped sample (Geeza Pro via insert_htmlbox) ----------------
    # MuPDF writes no ToUnicode entries for Geeza's shaped glyphs, so plain
    # extraction is junk; the engine rebuilds the letters from the font.
    samples = OUT / "samples"
    subprocess.run([sys.executable, "-W", "ignore", str(ROOT / "Tests" / "fixtures" / "make_samples.py")],
                   check=True, capture_output=True, env={**os.environ, "SAMPLES_DIR": str(samples)})
    report = samples / "report.pdf"
    sample = block_containing(report, "الإيراد")
    check(sample is not None and "نما الإيراد بنسبة ١٨٪ مقارنة بالربع السابق" in sample["text"]
          and flat(sample["text"]).endswith("داخل الملف."), "sample report: Geeza Pro paragraph recovered as logical Arabic")
    if sample:
        rows = sample["text"].split("\n")
        edited_rows = [rows[0].replace("الإيراد", "الربح", 1)] + rows[1:]
        step7 = OUT / "step7_sample_edit.pdf"
        engine("replace-block", "--input", str(report), "--output", str(step7), "--page", "1",
               "--rect", rect_str(sample["rect"]), "--text", "\n".join(edited_rows),
               "--original-text", sample["text"], "--line-height", str(sample["line_height"]))
        after = block_containing(step7, "الربح")
        check(after is not None and after["text"] == "\n".join(edited_rows), "sample report: edited paragraph extracts logically")
        check(after is not None and abs(after["rect"][2] - sample["rect"][2]) < 3.0, "sample report: right edge kept")

    # --- 9. PDFKit (the app's search/selection) ------------------------------
    probe = pdfkit_probe()
    if probe:
        terms = ["المبيعات", "الأرباح", "الموافقة", "PDF Studio", "ملاحظة", "Revenue"]
        result = subprocess.run([str(probe), str(step6), *terms], capture_output=True, text=True)
        data = json.loads(result.stdout)
        for term in terms:
            check(data["hits"].get(term, 0) >= 1, f"PDFKit finds {term!r}")
        check(mixed in " ".join(data["text"].split()), "PDFKit page text has the mixed heading in logical order")
    else:
        print("note: swiftc unavailable; PDFKit checks skipped")

    if failures:
        raise SystemExit(f"{len(failures)} check(s) failed: {failures}")
    print("arabic edit ok")


if __name__ == "__main__":
    main()
