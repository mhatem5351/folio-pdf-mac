#!/usr/bin/env python3
"""QA for the Compare & Convert-to-PDF feature group.

Engine: `convert-to-pdf`, `fix-text-map` (Engine/ext/convert.py) and `compare`
(Engine/ext/compare.py). Swift: Services/OfficeToPDF.swift is compiled with a
tiny harness (swiftc, Command Line Tools SDK) and exercised on Arabic RTF and
multi-page DOCX. Fixtures are generated under tmp/qa/convert/.

Run:  "$PS_PY" -W ignore Tests/qa_convert.py   -> prints "convert ok"
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
OUT = ROOT / "tmp" / "qa" / "convert"
SDK = "/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk"

ARABIC = "تقرير ربع سنوي يوضح نمو الإيراد مقارنة بالربع السابق في منطقة الخليج"
ARABIC_2 = "هذا النص لاختبار اتجاه الكتابة من اليمين إلى اليسار داخل الملف"


def engine(*args: str, expect_ok: bool = True) -> dict:
    proc = subprocess.run([sys.executable, str(ENGINE), *args], capture_output=True, text=True)
    try:
        reply = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{args[0]}: no JSON reply\n{proc.stdout}\n{proc.stderr}") from exc
    if expect_ok:
        assert proc.returncode == 0 and reply.get("ok"), f"{args[0]} failed: {reply}\n{proc.stderr}"
    else:
        assert proc.returncode != 0 and not reply.get("ok"), f"{args[0]} unexpectedly succeeded: {reply}"
    return reply


def flat(text: str) -> str:
    """Whitespace-collapsed text for order-sensitive containment checks."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def ink_ratio(page, clip=None) -> float:
    """Share of non-white pixels (rendered at 72 dpi) inside clip."""
    import pymupdf

    pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY, clip=clip, alpha=False)
    samples = pix.samples
    dark = sum(1 for value in samples if value < 200)
    return dark / max(1, len(samples))


def arabic_spans(page):
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if any("\u0600" <= ch <= "\u06ff" or "\ufb50" <= ch <= "\ufeff" for ch in span["text"]):
                    yield span


# --- fixtures ------------------------------------------------------------------

def make_png(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle([size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4], fill=(250, 250, 250))
    image.save(path)


def write_fixtures() -> dict[str, Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "txt": OUT / "notes.txt",
        "md": OUT / "readme.md",
        "html": OUT / "page.html",
        "png_wide": OUT / "wide.png",
        "png_tall": OUT / "tall.png",
        "rtf": OUT / "arabic.rtf",
    }
    files["txt"].write_text(
        "Meeting notes for the quarterly review\n"
        "    - indented bullet stays indented\n\n"
        f"{ARABIC}\n"
        f"{ARABIC_2}\n"
        "Final English line.\n",
        encoding="utf-8",
    )
    files["md"].write_text(
        "# Project Title\n\nBody text with **bold words** and `inline code`.\n\n"
        "| Column A | Column B |\n|---|---|\n| alpha | beta |\n\n"
        f"## قسم عربي\n\n{ARABIC}\n\n```\nprint('hello')\n```\n",
        encoding="utf-8",
    )
    make_png(OUT / "logo.png", (240, 120), (40, 110, 200))
    files["html"].write_text(
        "<!doctype html><html><head><meta charset='utf-8'><style>h1{color:#b00020}</style></head>"
        "<body><h1>HTML Heading</h1><p>An English paragraph.</p>"
        f"<p>{ARABIC_2}</p><img src='logo.png' width='240' height='120'></body></html>",
        encoding="utf-8",
    )
    make_png(files["png_wide"], (1200, 600), (200, 60, 60))
    make_png(files["png_tall"], (500, 900), (60, 160, 90))

    def rtf_escape(text: str) -> str:
        return "".join(ch if ord(ch) < 128 else f"\\u{ord(ch) if ord(ch) < 32768 else ord(ch) - 65536}?" for ch in text)

    files["rtf"].write_text(
        "{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0 Helvetica;}{\\f1 Arial;}}\n"
        "\\f0\\fs28 English heading in RTF\\par\n"
        f"\\pard\\rtlpar\\qr\\f1\\fs28 {rtf_escape(ARABIC)}\\par\n"
        f"\\pard\\rtlpar\\qr\\f1\\fs28 {rtf_escape(ARABIC_2)}\\par\n"
        "\\pard\\f0 Closing English line.\\par\n}\n",
        encoding="ascii",
    )
    return files


# --- convert: engine path -----------------------------------------------------------

def test_text_and_arabic(files: dict[str, Path]) -> None:
    import pymupdf

    out = OUT / "notes.pdf"
    reply = engine("convert-to-pdf", "--input", str(files["txt"]), "--output", str(out))
    assert reply["pages"] == 1 and reply["inputs"][0]["kind"] == "text", reply
    with pymupdf.open(out) as doc:
        page = doc[0]
        assert abs(page.rect.width - 595.28) < 1 and abs(page.rect.height - 841.89) < 1, page.rect
        text = flat(page.get_text())
        assert "Meeting notes for the quarterly review" in text, text
        assert "Final English line." in text
        # Logical order, base letters (ToUnicode fixed), whole sentences.
        assert flat(ARABIC) in text, f"Arabic not extracted in logical order:\n{text}"
        assert flat(ARABIC_2) in text, text
        assert not any("\ufb50" <= ch <= "\ufeff" for ch in page.get_text()), "presentation forms leaked"
        # Indentation kept.
        words = page.get_text("words")
        assert abs(words[0][0] - 72) < 2, f"text should start at the 1in margin: {words[0]}"
        indented = [w for w in words if w[4] == "-"]
        assert indented and indented[0][0] > words[0][0] + 8, indented
        # Not tofu: Arabic spans use a real Arabic-capable font and draw ink.
        spans = list(arabic_spans(page))
        assert spans, "no Arabic spans"
        assert all("Arial" in span["font"] for span in spans), {span["font"] for span in spans}
        for span in spans:
            assert ink_ratio(page, pymupdf.Rect(span["bbox"])) > 0.04, f"blank Arabic glyphs: {span['text']}"
        # Right-to-left: Arabic lines hug the right margin.
        arabic_lines = [pymupdf.Rect(span["bbox"]) for span in spans]
        assert max(rect.x1 for rect in arabic_lines) > page.rect.width - 72 - 3, arabic_lines
        assert min(rect.x0 for rect in arabic_lines) > 72 + 40, "Arabic line should not start at the left margin"


def test_markdown(files: dict[str, Path]) -> None:
    import pymupdf

    out = OUT / "readme.pdf"
    engine("convert-to-pdf", "--input", str(files["md"]), "--output", str(out), "--page-size", "letter")
    with pymupdf.open(out) as doc:
        page = doc[0]
        assert (round(page.rect.width), round(page.rect.height)) == (612, 792), page.rect
        spans = [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
        title = next(s for s in spans if "Project Title" in s["text"])
        body = next(s for s in spans if "Body text" in s["text"])
        bold = next(s for s in spans if "bold words" in s["text"])
        code = next(s for s in spans if "print" in s["text"])
        assert title["size"] > body["size"] * 1.6, (title["size"], body["size"])
        assert "Bold" in bold["font"], bold["font"]
        assert "Courier" in code["font"], code["font"]
        text = flat(page.get_text())
        for needle in ("Column A", "alpha", "beta", "قسم عربي", flat(ARABIC)):
            assert needle in text, (needle, text)
        assert page.get_drawings(), "table borders / code background expected"


def test_html_with_image(files: dict[str, Path]) -> None:
    import pymupdf

    out = OUT / "page.pdf"
    engine("convert-to-pdf", "--input", str(files["html"]), "--output", str(out), "--margins", "wide")
    with pymupdf.open(out) as doc:
        page = doc[0]
        assert page.get_images(), "relative <img> should be embedded"
        heading = next(s for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])
                       for s in l["spans"] if "HTML Heading" in s["text"])
        assert heading["color"] == 0xB00020, hex(heading["color"])
        assert heading["bbox"][0] >= 144 - 1, "wide margins = 2in sides"
        assert flat(ARABIC_2) in flat(page.get_text())


def test_images_and_combine(files: dict[str, Path]) -> None:
    import pymupdf

    out = OUT / "combined.pdf"
    src_pdf = OUT / "notes.pdf"
    reply = engine(
        "convert-to-pdf",
        "--input", str(files["txt"]),
        "--input", str(files["png_wide"]),
        "--input", str(files["png_tall"]),
        "--input", str(src_pdf),
        "--output", str(out),
        "--title", "Combined QA",
    )
    kinds = [item["kind"] for item in reply["inputs"]]
    assert kinds == ["text", "image", "image", "pdf"], kinds
    assert reply["pages"] == sum(item["pages"] for item in reply["inputs"]) == 4, reply
    with pymupdf.open(out) as doc:
        assert doc.metadata["title"] == "Combined QA"
        assert "Meeting notes" in doc[0].get_text()
        wide, tall = doc[1], doc[2]
        assert wide.rect.width > wide.rect.height, "landscape image -> landscape page"
        assert tall.rect.width < tall.rect.height
        for page in (wide, tall):
            info = page.get_image_info()
            assert len(info) == 1
            bbox = pymupdf.Rect(info[0]["bbox"])
            assert bbox in page.rect + (71, 71, -71, -71), (bbox, page.rect)
            assert ink_ratio(page) > 0.2
        assert flat(ARABIC) in flat(doc[3].get_text()), "PDF input passed through"


def test_errors() -> None:
    bogus = OUT / "slides.key"
    bogus.write_bytes(b"not really keynote")
    reply = engine("convert-to-pdf", "--input", str(bogus), "--output", str(OUT / "x.pdf"), expect_ok=False)
    assert "unsupported" in reply["message"], reply
    reply = engine("convert-to-pdf", "--input", str(OUT / "missing.txt"), "--output", str(OUT / "x.pdf"), expect_ok=False)
    assert "not found" in reply["message"], reply


def test_fix_text_map() -> None:
    """fix-text-map turns MuPDF/Arial presentation-form mappings into letters."""
    import pymupdf

    raw = OUT / "raw-forms.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    arch = pymupdf.Archive("/System/Library/Fonts/Supplemental")
    page.insert_htmlbox(pymupdf.Rect(50, 50, 545, 200), f'<p dir="rtl">{ARABIC}</p>',
                        css="@font-face {font-family: a; src: url(Arial.ttf);} p {font-family: a;}", archive=arch)
    doc.save(raw)
    with pymupdf.open(raw) as check:
        assert any("\ufb50" <= ch <= "\ufeff" for ch in check[0].get_text()), "fixture should use presentation forms"
    fixed = OUT / "fixed-forms.pdf"
    reply = engine("fix-text-map", "--input", str(raw), "--output", str(fixed))
    assert reply["tounicode_fixed"] >= 1
    with pymupdf.open(fixed) as check:
        text = check[0].get_text()
        assert flat(ARABIC) in flat(text), text
        assert not any("\ufb50" <= ch <= "\ufeff" for ch in text)


# --- convert: Swift (AppKit) path ---------------------------------------------------

HARNESS = r"""
import AppKit

let args = CommandLine.arguments
switch args[1] {
case "make-docx":
    let body = NSMutableAttributedString()
    let heading: [NSAttributedString.Key: Any] = [.font: NSFont.boldSystemFont(ofSize: 20)]
    let normal: [NSAttributedString.Key: Any] = [.font: NSFont(name: "Times New Roman", size: 12)!]
    body.append(NSAttributedString(string: "Word Document Heading\n", attributes: heading))
    for index in 1...140 {
        body.append(NSAttributedString(string: "Paragraph \(index): the quick brown fox jumps over the lazy dog while the report keeps going.\n", attributes: normal))
    }
    let data = try body.data(from: NSRange(location: 0, length: body.length),
                             documentAttributes: [.documentType: NSAttributedString.DocumentType.officeOpenXML])
    try data.write(to: URL(fileURLWithPath: args[2]))
case "convert":
    let layout = OfficeToPDF.Layout(
        pageSize: CGSize(width: Double(args[4])!, height: Double(args[5])!),
        margins: NSEdgeInsets(top: 72, left: 72, bottom: 72, right: 72))
    let pages = try OfficeToPDF.convert(URL(fileURLWithPath: args[2]), to: URL(fileURLWithPath: args[3]), layout: layout)
    print(pages)
default:
    fatalError("unknown command")
}
"""


def build_harness() -> Path | None:
    swiftc = shutil.which("swiftc")
    if not swiftc or not Path(SDK).exists():
        print("  (skipping AppKit conversion checks: swiftc / SDK not available)")
        return None
    folder = OUT / "harness"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.swift").write_text(HARNESS, encoding="utf-8")
    binary = folder / "office2pdf"
    env = dict(os.environ, SDKROOT=SDK)
    proc = subprocess.run(
        [swiftc, "-O", str(ROOT / "Sources/SamPDFStudio/Services/OfficeToPDF.swift"), str(folder / "main.swift"),
         "-o", str(binary)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"harness build failed:\n{proc.stderr}"
    return binary


def test_office_conversion(files: dict[str, Path]) -> None:
    import pymupdf

    binary = build_harness()
    if binary is None:
        return
    # Arabic RTF -> PDF: shaped, right-to-left, extractable.
    out = OUT / "arabic-rtf.pdf"
    proc = subprocess.run([str(binary), "convert", str(files["rtf"]), str(out), "595.28", "841.89"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    with pymupdf.open(out) as doc:
        page = doc[0]
        assert abs(page.rect.width - 595.28) < 1
        text = page.get_text()
        assert "English heading in RTF" in text and "Closing English line." in text, text
        flat_text = flat(text)
        assert flat(ARABIC) in flat_text and flat(ARABIC_2) in flat_text, f"Arabic order:\n{text}"
        spans = list(arabic_spans(page))
        assert spans and all("Geeza" in span["font"] for span in spans), {s["font"] for s in spans}
        for span in spans:
            assert ink_ratio(page, pymupdf.Rect(span["bbox"])) > 0.04, "blank Arabic glyphs"
        assert max(span["bbox"][2] for span in spans) > page.rect.width - 72 - 3, "RTL paragraphs right-aligned"
    # Multi-page DOCX on Letter: pagination and margins.
    docx = OUT / "long.docx"
    subprocess.run([str(binary), "make-docx", str(docx)], check=True, capture_output=True)
    out = OUT / "long-docx.pdf"
    proc = subprocess.run([str(binary), "convert", str(docx), str(out), "612", "792"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    with pymupdf.open(out) as doc:
        assert doc.page_count == int(proc.stdout.strip()) >= 3, (doc.page_count, proc.stdout)
        assert (round(doc[0].rect.width), round(doc[0].rect.height)) == (612, 792)
        full = flat("".join(p.get_text() for p in doc))
        assert "Word Document Heading" in full
        for index in (1, 70, 140):
            assert f"Paragraph {index}:" in full, index
        for page in doc:
            for x0, y0, x1, y1, *_ in page.get_text("words"):
                assert x0 >= 71 and x1 <= 612 - 71 and y0 >= 70 and y1 <= 792 - 70, (page.number, x0, y0, x1, y1)
    # The engine accepts the AppKit PDF as a pass-through input (combine flow).
    combined = OUT / "office-combined.pdf"
    reply = engine("convert-to-pdf", "--input", str(OUT / "arabic-rtf.pdf"), "--input", str(files["md"]),
                   "--output", str(combined))
    assert reply["inputs"][0]["kind"] == "pdf" and reply["pages"] >= 2


# --- compare -------------------------------------------------------------------------

LOREM = ("Quarterly revenue grew in every region while operating costs stayed flat and the team "
         "delivered the new subscription platform on schedule. ")


def make_text_pdf(path: Path, paragraphs: list[str], extra_page: str | None = None) -> None:
    """Paragraphs flow across A4 pages like a word processor would."""
    import pymupdf

    html = "".join(f"<p>{p}</p>" for p in paragraphs)
    story = pymupdf.Story(html=html, user_css="p { font-size: 11pt; margin-bottom: 8pt; }")
    writer = pymupdf.DocumentWriter(str(path))
    more = 1
    while more:
        device = writer.begin_page(pymupdf.paper_rect("a4"))
        more, _ = story.place(pymupdf.paper_rect("a4") + (72, 72, -72, -72))
        story.draw(device)
        writer.end_page()
    writer.close()
    if extra_page:
        doc = pymupdf.open(path)
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), extra_page, fontsize=12)
        doc.saveIncr()
        doc.close()


def test_compare_text_reflow() -> None:
    import pymupdf

    base = [f"Section {n}. " + LOREM * 3 for n in range(1, 13)]
    old_paras = list(base)
    new_paras = list(base)
    # A whole new paragraph near the top pushes every later line down, so
    # text crosses page boundaries differently in the revision.
    new_paras.insert(1, "This brand new paragraph was added in the revision to force reflow. " + LOREM * 2)
    new_paras[9] = new_paras[9].replace("Section 9.", "Section nine.", 1)
    old_paras[11] = old_paras[11] + " Obsolete closing sentence."
    old_pdf, new_pdf = OUT / "cmp-old.pdf", OUT / "cmp-new.pdf"
    make_text_pdf(old_pdf, old_paras)
    make_text_pdf(new_pdf, new_paras, extra_page="Appendix page that only exists in the revision")
    report, changes_json = OUT / "cmp-report.pdf", OUT / "cmp-changes.json"
    reply = engine("compare", "--old", str(old_pdf), "--new", str(new_pdf), "--output", str(report),
                   "--json", str(changes_json), "--old-label", "Contract v1.pdf", "--new-label", "Contract v2.pdf")
    with pymupdf.open(old_pdf) as d:
        assert reply["pages_old"] == d.page_count
    with pymupdf.open(new_pdf) as d:
        assert reply["pages_new"] == d.page_count
    kinds = [(c["type"], c["text"]) for c in reply["changes"]]
    # Reflow must not create spurious changes: only the 4 real edits remain
    # (the added paragraph may split at a page break into two records).
    assert reply["total_changes"] <= 6, kinds
    full_changes = json.loads(changes_json.read_text(encoding="utf-8"))["changes"]
    texts = " ".join(c["text"] + " " + c.get("old_text", "") for c in full_changes)
    assert "brand new paragraph" in texts, texts
    assert any(c["type"] == "replace" and c["text"] == "nine" and c["old_text"] == "9" for c in reply["changes"]), kinds
    assert any(c["type"] == "delete" and "Obsolete closing sentence" in c["text"] for c in reply["changes"]), kinds
    assert any(c["type"] == "insert" and "Appendix page" in c["text"] for c in reply["changes"]), kinds
    assert reply["insertions"] >= 3 and reply["deletions"] >= 2
    appendix = [p for p in reply["changed_pages"] if p["page_old"] is None]
    assert appendix and appendix[0]["page_new"] == reply["pages_new"], reply["changed_pages"]
    assert not reply["identical"] and reply["text_found"]

    with pymupdf.open(report) as doc:
        assert doc.page_count == reply["report_pages"]
        summary = doc[0].get_text()
        assert "Comparison Report" in summary and "Contract v1.pdf" in summary and "Contract v2.pdf" in summary
        assert doc[0].get_links(), "summary rows link to report pages"
        # Every change's report page shows red (old side) / green (new side) highlights.
        for change in reply["changes"]:
            page = doc[change["report_page"] - 1]
            mid = page.rect.width / 2
            fills = [(d["fill"], d["rect"]) for d in page.get_drawings() if d.get("fill") and d.get("fill_opacity", 1) < 0.5]
            reds = [r for f, r in fills if f[0] > 0.8 and f[1] < 0.3 and r.x1 <= mid]
            greens = [r for f, r in fills if f[1] > 0.55 and f[0] < 0.2 and r.x0 >= mid]
            if change["type"] in ("delete", "replace"):
                assert reds, f"no red highlight for {change}"
            if change["type"] in ("insert", "replace"):
                assert greens, f"no green highlight for {change}"
        side = doc[reply["summary_pages"]]
        assert side.rect.width > 2 * 595, "side-by-side page"
    data = json.loads(changes_json.read_text(encoding="utf-8"))
    assert len(data["changes"]) == reply["total_changes"]
    assert all(c["rects_old"] or c["rects_new"] for c in data["changes"])
    assert all(len(r) == 5 for c in data["changes"] for r in c["rects_old"] + c["rects_new"])


def test_compare_identical_and_arabic(files: dict[str, Path]) -> None:
    import pymupdf

    notes = OUT / "notes.pdf"
    reply = engine("compare", "--old", str(notes), "--new", str(notes), "--output", str(OUT / "same.pdf"))
    assert reply["identical"] and reply["insertions"] == reply["deletions"] == 0, reply
    with pymupdf.open(OUT / "same.pdf") as doc:
        assert "No differences found" in doc[0].get_text()

    edited = OUT / "notes-edited.txt"
    edited.write_text(files["txt"].read_text(encoding="utf-8").replace("الإيراد", "الأرباح"), encoding="utf-8")
    engine("convert-to-pdf", "--input", str(edited), "--output", str(OUT / "notes-edited.pdf"))
    reply = engine("compare", "--old", str(notes), "--new", str(OUT / "notes-edited.pdf"),
                   "--output", str(OUT / "arabic-report.pdf"))
    assert reply["total_changes"] == 1, reply["changes"]
    change = reply["changes"][0]
    assert change["type"] == "replace" and change["old_text"] == "الإيراد" and change["text"] == "الأرباح", change
    with pymupdf.open(OUT / "arabic-report.pdf") as doc:
        assert "الأرباح" in flat(doc[0].get_text()), "Arabic change listed on the summary"


def test_compare_visual() -> None:
    import pymupdf

    def scan(path: Path, box_color: tuple[float, float, float]) -> None:
        # Image-only pages (no text layer), like scans.
        src = pymupdf.open()
        page = src.new_page(width=595, height=842)
        page.draw_rect(pymupdf.Rect(60, 60, 535, 200), color=None, fill=(0.2, 0.2, 0.2))
        page.draw_rect(pymupdf.Rect(300, 500, 450, 620), color=None, fill=box_color)
        pix = page.get_pixmap(dpi=100)
        out = pymupdf.open()
        target = out.new_page(width=595, height=842)
        target.insert_image(target.rect, pixmap=pix)
        out.save(path)

    old_scan, new_scan = OUT / "scan-old.pdf", OUT / "scan-new.pdf"
    scan(old_scan, (0.1, 0.3, 0.9))
    scan(new_scan, (0.9, 0.3, 0.1))
    text_reply = engine("compare", "--old", str(old_scan), "--new", str(new_scan), "--output", str(OUT / "scan-text.pdf"))
    # Text mode on scans must not claim "identical"; it points to Visual mode.
    assert not text_reply["text_found"] and not text_reply["identical"], text_reply
    with pymupdf.open(OUT / "scan-text.pdf") as doc:
        assert "Visual mode" in flat(doc[0].get_text())
    reply = engine("compare", "--old", str(old_scan), "--new", str(new_scan), "--output", str(OUT / "scan-report.pdf"),
                   "--json", str(OUT / "scan.json"), "--mode", "visual")
    assert reply["changed_pages"] and reply["changed_pages"][0]["visual_regions"] >= 1, reply
    data = json.loads((OUT / "scan.json").read_text(encoding="utf-8"))
    regions = [pymupdf.Rect(r) for r in data["visual"][0]["regions"]]
    changed = pymupdf.Rect(300, 500, 450, 620)
    assert any(abs(region & changed) > 0.8 * abs(changed) for region in regions), regions
    assert not any(abs(region & pymupdf.Rect(60, 60, 535, 200)) > 100 for region in regions), "unchanged area boxed"
    same = engine("compare", "--old", str(old_scan), "--new", str(old_scan), "--output", str(OUT / "scan-same.pdf"),
                  "--mode", "visual")
    assert same["identical"], same


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    files = write_fixtures()
    steps = [
        ("text + Arabic", lambda: test_text_and_arabic(files)),
        ("markdown", lambda: test_markdown(files)),
        ("html + image", lambda: test_html_with_image(files)),
        ("images + combine", lambda: test_images_and_combine(files)),
        ("errors", test_errors),
        ("fix-text-map", test_fix_text_map),
        ("AppKit office path", lambda: test_office_conversion(files)),
        ("compare text/reflow", test_compare_text_reflow),
        ("compare identical + Arabic", lambda: test_compare_identical_and_arabic(files)),
        ("compare visual", test_compare_visual),
    ]
    for name, step in steps:
        step()
        print(f"  ✓ {name}")
    print("convert ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
