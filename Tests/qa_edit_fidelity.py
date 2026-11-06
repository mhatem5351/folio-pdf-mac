#!/usr/bin/env python3
"""Edit-fidelity regression harness for the in-place text editor (replace-block).

What it proves: editing ONE paragraph must not disturb anything else. For every
text block of a realistic corpus (Chrome-printed HTML with embedded subset
fonts, PyMuPDF pages, a /Rotate 90 page, a CropBox-offset page, the
Tests/fixtures report) it runs replace-block EXACTLY the way the app does
(AppStore.commitBlockEdit -> PDFEngineClient.replaceBlock: --rect from
page-blocks, --original-text, --bold/--italic from the block payload,
--line-height, --reflow on by default) and measures, before vs after:

  * disp      max displacement (pt) of any UNTOUCHED character (every line of
              the page whose text the edit did not change, including the
              edited block's own unchanged lines), matched by text;
  * inblk     the same, restricted to the edited block's unchanged lines;
  * prefix    displacement of the unchanged characters on the edited line
              that precede the edit (kerning / spacing / font-metric drift);
  * fonts     untouched characters whose font family / size / color /
              bold-italic flags changed;
  * lost      untouched lines that disappeared (clipped by a redaction,
              merged, rewrapped);
  * ren       untouched characters whose font only lost its family name
              (re-embedded subset reports "Regular"/"Bold"; informational);
  * spill     how far new ink extends past the block's right edge (pt; a
              failure only for multi-line paragraphs, which must wrap);
  * hits      new words that overlap an untouched word (collisions);
  * px        rendered pixels that changed OUTSIDE the edited lines (150 dpi);
              for long/delete the reflowed region is excluded (its rigid shift
              is checked word by word against the reported delta instead);
  * lnk/pth   link annotations / vector paths (underlines, rules, highlight
              boxes) that disappeared or moved;
  * hol       edited zones whose coloured background was bleached to white
              (white-box redaction fill on shaded areas / white-on-colour text).

Cases per block:
  noop        same text (the app short-circuits this, so it is informational:
              it shows the cost of re-rendering untouched lines)
  word        one word on one line: first letter kept, rest reversed (same
              glyphs, same length -> isolates layout fidelity)
  newglyph    the same word replaced by letters the subset likely lacks
              (exercises font fallback)
  twice       'word', then a second word edit on the re-opened block (the app's
              page-blocks on the new revision), measured against the original
  long        one word -> longer phrase, --reflow (app default)
  long_noreflow  the same without --reflow
  delete      last line removed (multi-line) / last word dropped (one line)
  rtl         one Arabic word reversed (blocks containing Arabic)

Usage (engine venv):
  PY="$HOME/Library/Application Support/FolioPDF/engine-venv/bin/python3"
  "$PY" Tests/qa_edit_fidelity.py                 # corpus + full matrix
  "$PY" Tests/qa_edit_fidelity.py --only invoice --cases word,long
  "$PY" Tests/qa_edit_fidelity.py --no-corpus     # reuse the corpus on disk
  "$PY" Tests/qa_edit_fidelity.py --repro CASE_ID # print the engine command
  "$PY" Tests/qa_edit_fidelity.py --images 40      # more before/after/diff PNGs

Outputs go to --out (default /private/tmp/claude-501/folio-edit-qa): corpus/,
runs/ (edited PDFs), images/ (before/after/diff PNGs of the worst cases),
results.json. Exit status 1 when any non-informational case fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF (engine venv)

fitz.TOOLS.unset_quad_corrections(True)  # same glyph boxes as the engine

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
FIXTURES = ROOT / "Tests" / "fixtures" / "make_samples.py"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_OUT = Path("/private/tmp/claude-501/folio-edit-qa")

# Thresholds
DISP_TOL = 0.5        # pt, untouched text may not move more than this
SPILL_TOL = 2.0       # pt, new ink past the block's right edge (no reflow wrap)
PX_TOL = 25           # changed pixels (150 dpi) outside the edited lines
DPI = 150

sys.path.insert(0, str(ROOT / "Engine"))
try:
    import pdf_engine as _engine  # noqa: E402
except Exception:  # pragma: no cover
    _engine = None
try:
    from ext import _rtl_text  # noqa: E402

    RAW_FLAGS = _rtl_text.glyph_flags("rawdict")
except Exception:  # pragma: no cover
    RAW_FLAGS = fitz.TEXTFLAGS_RAWDICT | getattr(fitz, "TEXT_IGNORE_ACTUALTEXT", 0)


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

LOREM = (
    "The quarterly review covers revenue, operating costs and the hiring plan "
    "for the coming year. Each department submitted its figures before the "
    "deadline, and the finance office reconciled them against the ledger. "
    "Overall performance improved steadily, although travel expenses rose "
    "faster than expected during the second half of the period."
)

PAGE_CSS = """
@page { size: A4; margin: 54pt 60pt; }
body { font-size: 11pt; line-height: 1.35; color: #111; }
h1 { font-size: 20pt; margin: 0 0 8pt 0; }
h2 { font-size: 14pt; margin: 14pt 0 6pt 0; }
p { margin: 0 0 9pt 0; }
"""

HTML_DOCS: dict[str, str] = {
    "justified": f"""
<style>{PAGE_CSS} body {{ font-family: 'Times New Roman', serif; }} p {{ text-align: justify; }}</style>
<h1>Annual Operations Summary</h1>
<p>{LOREM}</p>
<p>Management expects the improvements to continue into next year, provided
that supplier prices remain stable and the new warehouse opens on schedule.
The board approved the revised budget after a lengthy discussion of risks,
contingencies and the timing of capital expenditure across all regions.</p>
<p>{LOREM}</p>
""",
    "mixed_inline": f"""
<style>{PAGE_CSS} body {{ font-family: Arial, Helvetica, sans-serif; }}</style>
<h2>Project Notes</h2>
<p>The team shipped the <b>billing</b> module last week and the <i>reporting</i>
dashboard is now in review. Customers asked for clearer invoices, faster exports
and a simpler way to <b>approve</b> refunds without contacting support first.</p>
<p>Next sprint focuses on <i>performance</i> work: caching the search index,
trimming startup time and removing the legacy sync job that still runs nightly
on the old servers in the <b>primary</b> datacenter.</p>
<p style="color:#1a4fa0">This colored sentence checks that a color survives an edit of another line in the same block of text here.</p>
<p><u>Underlined remark that should stay underlined after edits nearby.</u></p>
<p><i>This entire paragraph is set in italic type, the way captions and quotations
often are, so editing one word must keep the embedded italic face in place.</i></p>
""",
    "sizes_lists": f"""
<style>{PAGE_CSS} body {{ font-family: Georgia, serif; }}</style>
<h1>Release Checklist</h1>
<p>Before tagging a release, verify the items below. <span style="font-size:15pt">Large words</span>
appear in the middle of this sentence so that one line mixes sizes, and the
paragraph continues after them for a few more words to wrap.</p>
<ul>
<li>Run the full regression suite on a clean machine</li>
<li>Update the changelog with every user visible change</li>
<li>Confirm the installer signs and notarizes correctly</li>
<li>Announce the release to the support team in advance</li>
</ul>
<h2>Second heading</h2>
<p>{LOREM}</p>
""",
    "two_column": f"""
<style>{PAGE_CSS} body {{ font-family: Helvetica, Arial, sans-serif; }}
.cols {{ column-count: 2; column-gap: 22pt; }}</style>
<h1>Newsletter</h1>
<div class="cols">
<p>{LOREM}</p>
<p>Management expects the improvements to continue into next year, provided
that supplier prices remain stable and the new warehouse opens on schedule.</p>
<p>{LOREM}</p>
<p>The board approved the revised budget after a lengthy discussion of risks,
contingencies and the timing of capital expenditure across all regions.</p>
</div>
""",
    "invoice": f"""
<style>{PAGE_CSS} body {{ font-family: Arial, Helvetica, sans-serif; }}
table {{ width: 100%; border-collapse: collapse; }}
td, th {{ padding: 4pt 6pt; border-bottom: 0.5pt solid #999; }}
td.amt, th.amt {{ text-align: right; }}</style>
<h1>Invoice INV-2041</h1>
<p>Billed to: Northwind Trading LLC, 42 Harbour Street, Springfield</p>
<table>
<tr><th style="text-align:left">Description</th><th class="amt">Qty</th><th class="amt">Amount</th></tr>
<tr><td>Consulting services, March</td><td class="amt">12</td><td class="amt">1,440.00</td></tr>
<tr><td>Hosting and maintenance</td><td class="amt">1</td><td class="amt">320.50</td></tr>
<tr><td>Training workshop for staff</td><td class="amt">2</td><td class="amt">980.00</td></tr>
<tr><td><b>Total due</b></td><td class="amt"></td><td class="amt"><b>2,740.50</b></td></tr>
</table>
<p>Payment is due within thirty days of the invoice date. Thank you.</p>
""",
    "arabic": """
<style>@page { size: A4; margin: 54pt 60pt; } body { font-family: 'Geeza Pro', 'Arial', sans-serif; font-size: 13pt; line-height: 1.5; }</style>
<div dir="rtl" style="text-align:right">
<h2>تقرير الأداء الفصلي</h2>
<p>نما الإيراد بنسبة كبيرة مقارنة بالربع السابق، وذلك بفضل الاشتراكات الجديدة في منطقة الخليج. بقيت تكاليف التشغيل ثابتة بينما انخفض الإنفاق على التسويق خلال هذه الفترة.</p>
<p>يهدف هذا النص إلى اختبار تحرير اللغة العربية داخل الملف دون تحريك بقية الأسطر من أماكنها الأصلية.</p>
</div>
<p style="font-family: Arial">English line below the Arabic block for neighbour checks.</p>
""",
    "links": f"""
<style>{PAGE_CSS} body {{ font-family: Verdana, sans-serif; font-size: 10pt; }}</style>
<h2>Resources</h2>
<p>Read the <a href="https://example.com/guide">installation guide</a> before you
start, then follow the <a href="https://example.com/tutorial">quick tutorial</a>
to create your first project. The <a href="https://example.com/faq">FAQ page</a>
answers most billing questions in detail.</p>
<p>Second paragraph without links, used as a neighbour that must not move when
the paragraph above is edited by the user in place.</p>
""",
    "ligatures": f"""
<style>{PAGE_CSS} body {{ font-family: 'Hoefler Text', 'Times New Roman', serif; font-size: 12pt; }}</style>
<h2>Office workflow</h2>
<p>The office staff will find the final workflow efficient: files flow from the
first floor to the fifth office, and the official figures fit the file format.
Affluent offices fly flags; the fine print is flawless and difficult to fault.</p>
<p>Plain closing line without ligature pairs for neighbour checks today.</p>
""",
    "shaded": f"""
<style>{PAGE_CSS} body {{ font-family: Arial, Helvetica, sans-serif; }}
.callout {{ background: #e3ecfa; border-left: 3pt solid #2a5db0; padding: 8pt 10pt; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 10pt; }}
th {{ background: #2a5db0; color: #fff; text-align: left; padding: 4pt 6pt; }}
td {{ padding: 4pt 6pt; border-bottom: 0.5pt solid #999; }}
mark {{ background: #fff2a8; }}</style>
<h2>Shaded content</h2>
<div class="callout"><p>Important: this callout sits on a light blue background box, and editing a
word inside it must not punch white holes into the shading or move the border.</p></div>
<p>A sentence with a <mark>highlighted phrase</mark> in the middle and an <u>underlined clause</u> after it.</p>
<table><tr><th>Region</th><th>Owner</th></tr>
<tr><td>North district office</td><td>Samira Haddad</td></tr>
<tr><td>Southern coastal branch</td><td>Daniel Okafor</td></tr></table>
""",
    "tight": f"""
<style>@page {{ size: A4; margin: 54pt 60pt; }} body {{ font-family: 'Times New Roman', serif; font-size: 12pt; }}
p {{ line-height: 1.0; margin: 0; }} h3 {{ font-size: 13pt; margin: 0; line-height: 1.0; }}</style>
<h3>Tight leading section</h3>
<p>Solid-set paragraph with descenders like gjpqy touching the next line, ascenders in
the following line such as bdfhkl, and a footnote marker<sup>12</sup> plus H<sub>2</sub>O inline.</p>
<p>Next paragraph starts immediately below without spacing, glyphs nearly overlap here.</p>
<p>Third tight paragraph keeps going so neighbours above and below exist for every edit.</p>
""",
}

def build_html_pdfs(corpus: Path) -> list[Path]:
    made = []
    html_dir = corpus / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    for name, body in HTML_DOCS.items():
        html = html_dir / f"{name}.html"
        html.write_text(f"<!doctype html><html><head><meta charset='utf-8'></head><body>{body}</body></html>", "utf-8")
        out = corpus / f"chrome_{name}.pdf"
        cmd = [CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
               f"--user-data-dir={corpus / '.chrome-profile'}",
               f"--print-to-pdf={out}", html.as_uri()]
        subprocess.run(cmd, capture_output=True, timeout=120)
        if out.exists() and out.stat().st_size > 0:
            made.append(out)
        else:
            print(f"  ! chrome failed for {name}", file=sys.stderr)
    return made


def build_pymupdf_pdfs(corpus: Path) -> list[Path]:
    made = []

    # Simple base-14 page, several paragraphs + a right-aligned amount column.
    p = corpus / "mupdf_simple.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 70), "Simple Base-14 Document", fontname="hebo", fontsize=18)
    page.insert_textbox(fitz.Rect(60, 90, 535, 200), LOREM, fontname="helv", fontsize=11)
    page.insert_textbox(fitz.Rect(60, 210, 535, 320), LOREM, fontname="tiro", fontsize=12)
    for i, (label, amount) in enumerate([("Subtotal", "1,200.00"), ("Tax", "96.00"), ("Total", "1,296.00")]):
        y = 360 + i * 20
        page.insert_text((60, y), label, fontname="helv", fontsize=11)
        w = fitz.get_text_length(amount, "helv", 11)
        page.insert_text((535 - w, y), amount, fontname="helv", fontsize=11)
    doc.save(str(p)); doc.close(); made.append(p)

    # Embedded TrueType (whole font) with a tight neighbour directly below.
    p = corpus / "mupdf_embedded.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    georgia = "/System/Library/Fonts/Supplemental/Georgia.ttf"
    if os.path.exists(georgia):
        kw = {"fontname": "Geo", "fontfile": georgia}
    else:
        kw = {"fontname": "tiro"}
    page.insert_textbox(fitz.Rect(60, 60, 535, 160), LOREM, fontsize=11, **kw)
    page.insert_textbox(fitz.Rect(60, 163, 535, 260), LOREM, fontsize=11, **kw)  # tight leading neighbour
    doc.save(str(p)); doc.close(); made.append(p)

    # /Rotate 90 page.
    p = corpus / "mupdf_rotated.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(fitz.Rect(60, 60, 535, 160), LOREM, fontname="helv", fontsize=11)
    page.insert_textbox(fitz.Rect(60, 170, 535, 270), LOREM, fontname="tiro", fontsize=11)
    page.set_rotation(90)
    doc.save(str(p)); doc.close(); made.append(p)

    # CropBox with a non-zero origin (and MediaBox offset).
    p = corpus / "mupdf_cropbox.pdf"
    doc = fitz.open()
    page = doc.new_page(width=700, height=900)
    page.insert_textbox(fitz.Rect(110, 120, 560, 230), LOREM, fontname="helv", fontsize=11)
    page.insert_textbox(fitz.Rect(110, 240, 560, 350), LOREM, fontname="tiro", fontsize=11)
    page.insert_text((40, 400), "Margin note that starts left of the crop box edge", fontname="helv", fontsize=10)
    page.set_cropbox(fitz.Rect(50, 60, 645, 842))
    doc.save(str(p)); doc.close(); made.append(p)

    # MediaBox whose lower-left is not (0,0) (common from scanners / imposers).
    p = corpus / "mupdf_mediabox_offset.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(fitz.Rect(60, 60, 535, 160), LOREM, fontname="helv", fontsize=11)
    page.insert_textbox(fitz.Rect(60, 170, 535, 270), LOREM, fontname="tiro", fontsize=11)
    doc.xref_set_key(page.xref, "MediaBox", "[100 200 695 1042]")
    # shift the content so it stays visible inside the moved MediaBox
    conts = page.get_contents()
    doc.update_stream(conts[0], b"q 1 0 0 1 100 200 cm\n" + doc.xref_stream(conts[0]))
    doc.update_stream(conts[-1], doc.xref_stream(conts[-1]) + b"\nQ")
    doc.save(str(p)); doc.close(); made.append(p)

    # Separate blocks whose glyph boxes overlap vertically (solid leading,
    # different indents): erasing one line must not delete the other's glyphs.
    p = corpus / "mupdf_overlap.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 100), "Upper label gjpqy here", fontname="helv", fontsize=12)
    page.insert_text((70, 110.5), "Lower ABCDEFGH neighbour text", fontname="tiro", fontsize=12)
    page.insert_text((60, 160), "Total due", fontname="helv", fontsize=11)
    page.insert_text((400, 160), "Samira Haddad", fontname="helv", fontsize=11)
    doc.save(str(p)); doc.close(); made.append(p)
    return made


def build_fixture_pdfs(corpus: Path) -> list[Path]:
    if not FIXTURES.exists():
        return []
    env = dict(os.environ, SAMPLES_DIR=str(corpus / "fixtures"))
    subprocess.run([sys.executable, str(FIXTURES)], env=env, capture_output=True, timeout=120)
    report = corpus / "fixtures" / "report.pdf"
    if report.exists():
        dest = corpus / "fixture_report.pdf"
        shutil.copy(report, dest)
        return [dest]
    return []


def build_corpus(corpus: Path) -> list[Path]:
    corpus.mkdir(parents=True, exist_ok=True)
    pdfs = build_html_pdfs(corpus) + build_pymupdf_pdfs(corpus) + build_fixture_pdfs(corpus)
    shutil.rmtree(corpus / ".chrome-profile", ignore_errors=True)
    return pdfs


# ---------------------------------------------------------------------------
# Engine calls (the app's exact flags)
# ---------------------------------------------------------------------------

def run_engine(*args: str, timeout: int = 180) -> dict[str, Any]:
    proc = subprocess.run([sys.executable, "-W", "ignore", str(ENGINE), *args],
                          capture_output=True, text=True, timeout=timeout)
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except (ValueError, IndexError):
        payload = {}
    payload.setdefault("ok", proc.returncode == 0)
    if proc.returncode != 0:
        payload["ok"] = False
        payload.setdefault("message", (proc.stderr or proc.stdout)[-400:])
    return payload


# How the app passes bold/italic for an edit whose style panel is untouched:
#   "delta"  (current working tree): --bold/--italic only when the user ADDS the
#            style (ActiveTextBlock.addsBold / addsItalic) -> nothing is sent;
#            text args use the "--text=..." form.
#   "legacy" (git HEAD 45ae031..): bold: block.bold, italic: block.italic, i.e.
#            --bold / --italic whenever the block's dominant font is bold/italic.
APP_STYLE_FLAGS = os.environ.get("FOLIO_QA_STYLE_FLAGS", "delta")


def app_replace_block_args(src: Path, dst: Path, page: int, block: dict[str, Any], text: str,
                           reflow: bool = True) -> list[str]:
    """Mirror of AppStore.commitBlockEdit -> PDFEngineClient.replaceBlock for an
    unstyled text edit (style panel untouched)."""
    r = block["rect"]
    legacy = APP_STYLE_FLAGS == "legacy"
    args = ["replace-block", "--input", str(src), "--output", str(dst), "--page", str(page),
            "--rect", f"{r[0]},{r[1]},{r[2]},{r[3]}"]
    args += ["--text", text] if legacy else [f"--text={text}"]
    if legacy and block.get("bold"):
        args.append("--bold")
    if legacy and block.get("italic"):
        args.append("--italic")
    if (block.get("line_height") or 0) > 0:
        args += ["--line-height", str(block["line_height"])]
    if block.get("text"):
        args += ["--original-text", block["text"]] if legacy else [f"--original-text={block['text']}"]
    if reflow:
        args.append("--reflow")
    return args


# ---------------------------------------------------------------------------
# Geometry capture
# ---------------------------------------------------------------------------

_SUBSET = re.compile(r"^[A-Z]{6}\+")
# Names MuPDF reports for a re-embedded subset whose name table only holds the
# style ("Regular", "Bold", ...): same glyphs, family name lost.
GENERIC_NAMES = {"regular", "bold", "italic", "bold italic", "bolditalic", "black", "medium", "light", "roman", "book"}


def font_key(name: str) -> str:
    return _SUBSET.sub("", name or "").lower()


def page_words(page: Any) -> list[dict[str, Any]]:
    """Every word on the page with per-char origin, bbox and style."""
    words: list[dict[str, Any]] = []
    try:
        data = _engine.text_dict(page) if _engine is not None else page.get_text("rawdict", flags=RAW_FLAGS)
    except Exception:
        data = page.get_text("rawdict", flags=RAW_FLAGS)
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            current: list[dict[str, Any]] = []

            def flush() -> None:
                if current:
                    xs0 = min(c["bbox"][0] for c in current); ys0 = min(c["bbox"][1] for c in current)
                    xs1 = max(c["bbox"][2] for c in current); ys1 = max(c["bbox"][3] for c in current)
                    words.append({
                        "text": "".join(c["c"] for c in current),
                        "chars": list(current),
                        "bbox": (xs0, ys0, xs1, ys1),
                        "center": ((xs0 + xs1) / 2, (ys0 + ys1) / 2),
                    })
                    current.clear()

            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    if not c or c.isspace():
                        flush()
                        continue
                    current.append({
                        "c": c, "o": tuple(ch["origin"]), "bbox": tuple(ch["bbox"]),
                        "font": font_key(span.get("font", "")), "size": float(span.get("size") or 0),
                        "color": int(span.get("color") or 0),
                    })
            flush()
    return words


def match_words(before: list[dict], after: list[dict], expected_shift: Any, limit: float = 40.0) -> dict[int, int]:
    """Greedy nearest assignment of before-words to after-words with the same
    text (after compensating the expected reflow shift)."""
    by_text: dict[str, list[int]] = {}
    for j, w in enumerate(after):
        by_text.setdefault(w["text"], []).append(j)
    pairs = []
    for i, w in enumerate(before):
        ex, ey = expected_shift(w)
        cx, cy = w["center"][0] + ex, w["center"][1] + ey
        for j in by_text.get(w["text"], []):
            a = after[j]["center"]
            d = ((a[0] - cx) ** 2 + (a[1] - cy) ** 2) ** 0.5
            if d <= limit:
                pairs.append((d, i, j))
    pairs.sort()
    used_i, used_j, out = set(), set(), {}
    for _d, i, j in pairs:
        if i in used_i or j in used_j:
            continue
        used_i.add(i); used_j.add(j); out[i] = j
    return out


def rows_in_rect(words: list[dict], rect: Any) -> list[list[int]]:
    """Indices of words inside a block rect, grouped into rows by baseline."""
    r = fitz.Rect(rect) + (-1, -1, 1, 1)
    inside = [i for i, w in enumerate(words) if r.contains(fitz.Point(*w["center"]))]
    rows: list[list[int]] = []
    for i in sorted(inside, key=lambda k: words[k]["chars"][0]["o"][1]):
        y = words[i]["chars"][0]["o"][1]
        for row in rows:
            if abs(words[row[0]]["chars"][0]["o"][1] - y) < 2.0:
                row.append(i)
                break
        else:
            rows.append([i])
    for row in rows:
        row.sort(key=lambda k: words[k]["bbox"][0])
    return rows


def render_gray(page: Any) -> Any:
    import numpy as np

    pix = page.get_pixmap(dpi=DPI, colorspace=fitz.csGRAY, annots=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

LATIN_WORD = re.compile(r"[A-Za-z]{4,}")
NUM_WORD = re.compile(r"\d{2,}")
ARABIC_WORD = re.compile(r"[ء-ي]{3,}")


def reverse_word(word: str) -> str:
    """Same glyphs, same case positions where it matters: keep the first
    character, reverse the rest ("Quarterly" -> "Qylretrau"). Every glyph is
    already in the embedded subset, so this isolates layout fidelity from
    font fallback."""
    return word[:1] + word[1:][::-1]


NEW_GLYPHS = "zqxjkvwyZQXJKVWY"


def newglyph_word(word: str) -> str:
    """Same length, letters the subset most likely lacks (font fallback)."""
    return "".join(NEW_GLYPHS[i % len(NEW_GLYPHS)] if ch.isalpha() else ch for i, ch in enumerate(word))


def pick_word(line: str, pattern: re.Pattern) -> re.Match | None:
    matches = [m for m in pattern.finditer(line) if reverse_word(m.group()) != m.group()]
    if not matches:
        return None
    mid = len(line) / 2
    return min(matches, key=lambda m: abs((m.start() + m.end()) / 2 - mid))


def has_arabic(text: str) -> bool:
    return bool(re.search(r"[؀-ۿ]", text))


def make_cases(block: dict[str, Any], wanted: set[str]) -> list[dict[str, Any]]:
    lines = block["text"].split("\n")
    target = len(lines) // 2 if len(lines) > 1 else 0
    cases = []
    arabic = has_arabic(block["text"])

    def edit_line(index: int, new_line: str) -> str:
        out = list(lines)
        out[index] = new_line
        return "\n".join(out)

    if "noop" in wanted:
        cases.append({"case": "noop", "text": block["text"], "rows": [], "reflow": True, "info": True})
    if not arabic:
        m = pick_word(lines[target], LATIN_WORD) or pick_word(lines[target], NUM_WORD)
        if m is not None:
            line = lines[target]
            if "word" in wanted:
                cases.append({"case": "word", "text": edit_line(target, line[:m.start()] + reverse_word(m.group()) + line[m.end():]),
                              "rows": [target], "reflow": True, "word_span": (m.start(), m.end())})
            if "twice" in wanted:
                cases.append({"case": "twice", "text": edit_line(target, line[:m.start()] + reverse_word(m.group()) + line[m.end():]),
                              "rows": [target], "reflow": True, "word_span": (m.start(), m.end()), "second": True})
            if "newglyph" in wanted and m.group().isalpha():
                cases.append({"case": "newglyph", "text": edit_line(target, line[:m.start()] + newglyph_word(m.group()) + line[m.end():]),
                              "rows": [target], "reflow": True, "word_span": (m.start(), m.end())})
            longer = line[:m.start()] + m.group() + " plus several extra words" + line[m.end():]
            if "long" in wanted:
                cases.append({"case": "long", "text": edit_line(target, longer), "rows": [target], "reflow": True, "free_after": True})
            if "long_noreflow" in wanted:
                cases.append({"case": "long_noreflow", "text": edit_line(target, longer), "rows": [target], "reflow": False, "free_after": True})
    else:
        for index in [target] + [i for i in range(len(lines)) if i != target]:
            m = pick_word(lines[index], ARABIC_WORD)
            if m is not None and "rtl" in wanted:
                line = lines[index]
                cases.append({"case": "rtl", "text": edit_line(index, line[:m.start()] + m.group()[0] + m.group()[1:][::-1] + line[m.end():]),
                              "rows": [index], "reflow": True, "word_span": (m.start(), m.end()), "rtl": True})
                break
    if "delete" in wanted:
        if len(lines) > 1:
            cases.append({"case": "delete", "text": "\n".join(lines[:-1]), "rows": [len(lines) - 1], "reflow": True, "free_after": True})
        else:
            parts = lines[0].rsplit(" ", 1)
            if len(parts) == 2 and parts[0].strip():
                cases.append({"case": "delete", "text": parts[0], "rows": [0], "reflow": True, "free_after": True})
    return cases


# ---------------------------------------------------------------------------
# Measurement of one case
# ---------------------------------------------------------------------------

def measure(src: Path, dst: Path, page_no: int, block: dict[str, Any], blocks: list[dict[str, Any]],
            spec: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
    import numpy as np

    before_doc, after_doc = fitz.open(src), fitz.open(dst)
    bp, ap = before_doc[page_no - 1], after_doc[page_no - 1]
    before, after = page_words(bp), page_words(ap)

    rows = rows_in_rect(before, block["rect"])
    n_lines = len(block["text"].split("\n"))
    edited_rows = set(spec["rows"])
    row_of: dict[int, int] = {}
    if len(rows) == n_lines:
        for r_index, row in enumerate(rows):
            for i in row:
                row_of[i] = r_index
    else:
        # Rows could not be mapped 1:1 (extraction split/merged): treat the whole
        # block as edited so we never blame it for its own changes.
        for row in rows:
            for i in row:
                row_of[i] = -1
        edited_rows = {-1}
    min_edited = min([r for r in edited_rows if r >= 0], default=10 ** 6)

    edited_word_idx: set[int] = set()
    word_span = spec.get("word_span")
    for i, r in row_of.items():
        if r in edited_rows:
            edited_word_idx.add(i)

    # Which words on the edited row are the replaced word itself
    edited_line_others: set[int] = set()
    if word_span and len(rows) == n_lines and spec["rows"]:
        row = rows[spec["rows"][0]]
        old_line = block["text"].split("\n")[spec["rows"][0]]
        target_word = old_line[word_span[0]:word_span[1]]
        hit = [i for i in row if target_word in before[i]["text"]]
        if hit:
            anchor = before[hit[0]]["bbox"]
            for i in row:
                if i == hit[0]:
                    continue
                if spec.get("rtl"):
                    if before[i]["bbox"][0] >= anchor[2] - 0.5:
                        edited_line_others.add(i)
                elif before[i]["bbox"][2] <= anchor[0] + 0.5:
                    edited_line_others.add(i)

    # Expected reflow shift
    reflow = reply.get("reflow") or {}
    delta = float(reflow.get("delta") or 0) if reflow.get("applied") else 0.0
    region = fitz.Rect(reflow["region"]) if reflow.get("applied") and reflow.get("region") else None

    compensate = spec["case"] in ("long", "delete")  # height may change legitimately

    def expected(w: dict) -> tuple[float, float]:
        if compensate and region is not None and region.contains(fitz.Point(*w["center"])):
            return (0.0, delta)
        return (0.0, 0.0)

    mapping = match_words(before, after, expected)

    disp = inblk = prefix = 0.0
    fonts = lost = renamed = 0
    lost_samples: list[str] = []
    font_samples: list[str] = []
    worst: tuple[float, str] = (0.0, "")
    for i, w in enumerate(before):
        r = row_of.get(i)
        if i in edited_word_idx and i not in edited_line_others:
            continue
        if r is not None and spec.get("free_after") and r > min_edited:
            continue  # rewrapped tail of the edited paragraph
        j = mapping.get(i)
        if j is None:
            if i in edited_line_others:
                continue
            lost += 1
            if len(lost_samples) < 4:
                lost_samples.append(w["text"])
            continue
        ex, ey = expected(w)
        a = after[j]
        d = 0.0
        for cb, ca in zip(w["chars"], a["chars"]):
            d = max(d, ((ca["o"][0] - cb["o"][0] - ex) ** 2 + (ca["o"][1] - cb["o"][1] - ey) ** 2) ** 0.5)
            if i in edited_line_others:
                pass
            elif cb["font"] != ca["font"] and ca["font"] in GENERIC_NAMES and abs(cb["size"] - ca["size"]) <= 0.05 \
                    and cb["color"] == ca["color"]:
                renamed += 1
            elif (cb["font"] != ca["font"] or abs(cb["size"] - ca["size"]) > 0.05 or cb["color"] != ca["color"]):
                fonts += 1
                if len(font_samples) < 3:
                    font_samples.append(f"{w['text']}:{cb['font']}/{cb['size']:.1f}/#{cb['color']:06x}->{ca['font']}/{ca['size']:.1f}/#{ca['color']:06x}")
        if i in edited_line_others:
            prefix = max(prefix, d)
            continue
        if r is not None and r >= 0:
            inblk = max(inblk, d)
        disp = max(disp, d)
        if d > worst[0]:
            worst = (d, w["text"])

    # New ink = after-words not matched to any before-word
    matched_after = set(mapping.values())
    new_ink = [after[j] for j in range(len(after)) if j not in matched_after]
    rect = fitz.Rect(block["rect"])
    spill = 0.0
    offpage = 0
    if new_ink and not has_arabic(spec["text"]):
        spill = max(0.0, max(w["bbox"][2] for w in new_ink) - rect.x1)
    if new_ink:
        page_box = bp.rect if not bp.rotation else (bp.rect * bp.derotation_matrix).normalize()
        offpage = sum(1 for w in new_ink if w["bbox"][2] > page_box.x1 + 0.5 or w["bbox"][0] < page_box.x0 - 0.5)
    hits = 0
    untouched_after = [after[j] for i, j in mapping.items() if i not in edited_word_idx]
    for w in new_ink:
        nb = fitz.Rect(w["bbox"])
        for u in untouched_after:
            ub = fitz.Rect(u["bbox"])
            inter = nb & ub
            if not inter.is_empty and inter.get_area() > 0.25 * min(nb.get_area(), ub.get_area()):
                hits += 1
                break

    # Pixel diff outside the edited rows / new ink (and the reflowed region)
    A, B = render_gray(bp), render_gray(ap)
    scale = DPI / 72.0
    mask = np.zeros(A.shape, dtype=bool) if A.shape == B.shape else None
    px = -1
    holes = 0
    diff_info = None
    if mask is not None:
        rot = bp.rotation_matrix

        def cover(r: Any, pad: float = 1.5) -> None:
            r = fitz.Rect(r) + (-pad, -pad, pad, pad)
            if bp.rotation:
                r = (r * rot).normalize()
            x0, y0 = max(0, int(r.x0 * scale)), max(0, int(r.y0 * scale))
            x1, y1 = min(A.shape[1], int(r.x1 * scale) + 1), min(A.shape[0], int(r.y1 * scale) + 1)
            if x1 > x0 and y1 > y0:
                mask[y0:y1, x0:x1] = True
        for i in edited_word_idx:
            if i not in edited_line_others:
                cover(before[i]["bbox"])
        for w in new_ink:
            cover(w["bbox"])
        if spec.get("free_after"):
            for i, r in row_of.items():
                if r > min_edited:
                    cover(before[i]["bbox"])
        if region is not None and compensate:
            cover(region, 2)
            cover(region + (0, delta, 0, delta), 2)
        changed = (np.abs(A.astype(int) - B.astype(int)) > 40) & ~mask
        px = int(changed.sum())
        # Coloured backgrounds bleached to white inside the edited zones
        for i in edited_word_idx:
            if i in edited_line_others:
                continue
            r = fitz.Rect(before[i]["bbox"])
            if bp.rotation:
                r = (r * rot).normalize()
            x0, y0, x1, y1 = int(r.x0 * scale), int(r.y0 * scale), int(r.x1 * scale) + 1, int(r.y1 * scale) + 1
            a_zone, b_zone = A[y0:y1, x0:x1], B[y0:y1, x0:x1]
            if a_zone.size and np.median(a_zone) < 245 and np.median(b_zone) >= 250:
                holes += 1
        diff_info = (A, B, mask, changed)

    # Link annotations and vector paths (underlines, rules, shaded boxes)
    def link_keys(pg: Any) -> list[tuple]:
        return [(l.get("uri") or l.get("page"), fitz.Rect(l["from"])) for l in pg.get_links()]
    links_before, links_after = link_keys(bp), link_keys(ap)
    links_lost = 0
    for uri, r in links_before:
        ex, ey = (0.0, delta) if (compensate and region is not None and region.contains(r.tl)) else (0.0, 0.0)
        if not any(u == uri and abs(a.x0 - r.x0 - ex) < 1 and abs(a.y0 - r.y0 - ey) < 1 for u, a in links_after):
            links_lost += 1

    def path_keys(pg: Any) -> list[tuple]:
        keys = []
        for d in pg.get_drawings():
            if d.get("type") in ("clip", "group"):
                continue
            col = d.get("fill") if d.get("fill") is not None else d.get("color")
            keys.append((fitz.Rect(d["rect"]), tuple(round(c, 2) for c in (col or ())), d.get("fill") == (1.0, 1.0, 1.0)))
        return keys
    paths_before, paths_after = path_keys(bp), path_keys(ap)
    paths_lost = 0
    for r, col, white in paths_before:
        if white:
            continue
        ex, ey = (0.0, delta) if (compensate and region is not None and region.contains(r.tl)) else (0.0, 0.0)
        if not any(c == col and abs(a.x0 - r.x0 - ex) < 1 and abs(a.y0 - r.y0 - ey) < 1
                   and abs(a.width - r.width) < 1 for a, c, _w in paths_after):
            paths_lost += 1
    white_added = sum(1 for _r, _c, w in paths_after if w) - sum(1 for _r, _c, w in paths_before if w)

    before_doc.close(); after_doc.close()
    return {
        "renamed": renamed, "holes": holes, "offpage": offpage, "multiline": n_lines > 1, "links_lost": links_lost, "paths_lost": paths_lost, "white_boxes": max(0, white_added),
        "disp": round(disp, 3), "inblk": round(inblk, 3), "prefix": round(prefix, 3),
        "fonts": fonts, "lost": lost, "spill": round(spill, 2), "hits": hits, "px": px,
        "worst_word": worst[1], "lost_samples": lost_samples, "font_samples": font_samples,
        "reflow": reflow, "rows_mapped": len(rows) == n_lines, "_diff": diff_info,
    }


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------

def judge(rec: dict[str, Any]) -> list[str]:
    why = []
    if rec.get("error"):
        return ["engine:" + rec["error"][:60]]
    if rec["disp"] > DISP_TOL:
        why.append(f"disp {rec['disp']:.2f}")
    if rec["case"] in ("word", "rtl", "newglyph", "twice") and rec["prefix"] > DISP_TOL:
        why.append(f"prefix {rec['prefix']:.2f}")
    if rec["fonts"]:
        why.append(f"fonts {rec['fonts']}")
    if rec["lost"]:
        why.append(f"lost {rec['lost']}")
    if rec["case"].startswith("long") and rec.get("multiline") and rec["spill"] > SPILL_TOL:
        why.append(f"spill {rec['spill']:.0f}")
    if rec.get("offpage"):
        why.append(f"offpage {rec['offpage']}")
    if rec.get("holes"):
        why.append(f"holes {rec['holes']}")
    if rec["hits"]:
        why.append(f"overlap {rec['hits']}")
    if rec.get("links_lost"):
        why.append(f"links {rec['links_lost']}")
    if rec.get("paths_lost"):
        why.append(f"paths {rec['paths_lost']}")
    if rec["px"] > PX_TOL:
        why.append(f"px {rec['px']}")
    return why


def select_blocks(blocks: list[dict[str, Any]], limit: int) -> list[int]:
    order = sorted(range(len(blocks)), key=lambda i: (-min(blocks[i]["line_count"], 4), i))
    chosen = sorted(order[:limit])
    return chosen


def run_case(job: dict[str, Any]) -> dict[str, Any]:
    src, page, block, blocks, spec, out = (job[k] for k in ("src", "page", "block", "blocks", "spec", "out"))
    args = app_replace_block_args(src, out, page, block, spec["text"], reflow=spec["reflow"])
    rec = {"id": job["id"], "doc": src.stem, "block": job["bindex"], "case": spec["case"],
           "info": bool(spec.get("info")), "args": args,
           "block_text": block["text"][:60].replace("\n", " / ")}
    reply = run_engine(*args)
    if spec.get("second") and reply.get("ok") and out.exists():
        # Re-open the edited paragraph the way the app does (page-blocks on the
        # new revision, block under the same spot) and change ANOTHER word.
        listing = run_engine("page-blocks", "--input", str(out), "--page", str(page))
        r0 = fitz.Rect(block["rect"])
        cands = [b for b in listing.get("blocks") or [] if fitz.Rect(b["rect"]).intersects(r0)]
        if cands:
            b2 = max(cands, key=lambda b: (fitz.Rect(b["rect"]) & r0).get_area())
            lines2 = b2["text"].split("\n")
            idx = 0 if len(lines2) > 1 else 0
            m2 = pick_word(lines2[idx], LATIN_WORD)
            if m2 is not None and b2["text"].count("\n") == block["text"].count("\n"):
                lines2[idx] = lines2[idx][:m2.start()] + reverse_word(m2.group()) + lines2[idx][m2.end():]
                mid = out.with_name(out.stem + ".first.pdf")
                out.rename(mid)
                args2 = app_replace_block_args(mid, out, page, b2, "\n".join(lines2), reflow=True)
                reply = run_engine(*args2)
                rec["args2"] = args2
                spec = dict(spec, rows=sorted(set(spec["rows"]) | {idx}), word_span=None)
                job["spec"] = spec
                rec["second_block"] = {k: b2[k] for k in ("fontname", "bold", "italic", "fontsize")}
    rec["reply"] = {k: v for k, v in reply.items() if k in ("ok", "message", "inserted", "reflow")}
    if not reply.get("ok") or not out.exists():
        rec["error"] = str(reply.get("message") or "engine failed")
        return rec
    try:
        rec.update(measure(src, out, page, block, blocks, spec, reply))
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"measure: {type(exc).__name__}: {exc}"
    return rec


def save_images(rec: dict[str, Any], images: Path) -> None:
    import numpy as np
    from PIL import Image

    A, B, mask, changed = rec["_diff"]
    base = images / rec["id"]
    Image.fromarray(A).save(f"{base}_before.png")
    Image.fromarray(B).save(f"{base}_after.png")
    rgb = np.stack([A, A, A], axis=-1).astype(np.uint8)
    rgb = (rgb * 0.35 + 165).astype(np.uint8)
    inside = (np.abs(A.astype(int) - B.astype(int)) > 40) & mask
    rgb[mask] = (rgb[mask] * 0.85 + np.array([0, 0, 40])).clip(0, 255).astype(np.uint8)
    rgb[inside] = (40, 160, 40)     # changed inside the edit zone (expected)
    rgb[changed] = (230, 20, 20)    # changed OUTSIDE the edit zone (defect)
    Image.fromarray(rgb).save(f"{base}_diff.png")


def main() -> int:
    global APP_STYLE_FLAGS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-corpus", action="store_true", help="reuse the corpus already in OUT/corpus")
    ap.add_argument("--only", default="", help="comma list of substrings of corpus file names")
    ap.add_argument("--cases", default="noop,word,newglyph,twice,long,long_noreflow,delete,rtl")
    ap.add_argument("--max-blocks", type=int, default=7, help="blocks per document (multi-line first)")
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    ap.add_argument("--images", type=int, default=16, help="save PNGs for the N worst failing cases")
    ap.add_argument("--style-flags", choices=["delta", "legacy"], default=APP_STYLE_FLAGS,
                    help="app bold/italic flag behaviour to mirror (see APP_STYLE_FLAGS)")
    ap.add_argument("--repro", default="", help="print the engine command for a case id from results.json")
    opts = ap.parse_args()
    APP_STYLE_FLAGS = opts.style_flags

    out = Path(opts.out)
    if opts.repro:
        for rec in json.loads((out / "results.json").read_text()):
            if rec["id"] == opts.repro:
                py = sys.executable
                print(" ".join([repr(py) if " " in py else py, str(ENGINE)] + [f"'{a}'" if (" " in a or "\n" in a) else a for a in rec["args"]]))
                return 0
        print("no such case id", file=sys.stderr)
        return 2

    corpus = out / "corpus"
    runs = out / "runs"
    images = out / "images"
    if not opts.no_corpus or not corpus.exists():
        print("building corpus ...")
        build_corpus(corpus)
    shutil.rmtree(runs, ignore_errors=True); runs.mkdir(parents=True)
    shutil.rmtree(images, ignore_errors=True); images.mkdir(parents=True)

    pdfs = sorted(corpus.glob("*.pdf"))
    if opts.only:
        keys = [k.strip() for k in opts.only.split(",") if k.strip()]
        pdfs = [p for p in pdfs if any(k in p.name for k in keys)]
    wanted = {c.strip() for c in opts.cases.split(",") if c.strip()}

    jobs = []
    for pdf in pdfs:
        listing = run_engine("page-blocks", "--input", str(pdf), "--page", "1")
        blocks = listing.get("blocks") or []
        for bindex in select_blocks(blocks, opts.max_blocks):
            block = blocks[bindex]
            for spec in make_cases(block, wanted):
                cid = f"{pdf.stem}.b{bindex}.{spec['case']}"
                jobs.append({"id": cid, "src": pdf, "page": 1, "block": block, "blocks": blocks,
                             "bindex": bindex, "spec": spec, "out": runs / f"{cid}.pdf"})
    print(f"{len(pdfs)} documents, {len(jobs)} edit cases, {opts.jobs} workers, app style flags: {APP_STYLE_FLAGS}")

    with ThreadPoolExecutor(max_workers=opts.jobs) as pool:
        records = list(pool.map(run_case, jobs))

    for rec in records:
        rec["fail"] = judge(rec)
        rec["status"] = ("INFO" if rec["info"] else "FAIL") if rec["fail"] else "PASS"

    # Images for the worst real failures
    def score(rec: dict[str, Any]) -> float:
        return (rec.get("disp", 0) + rec.get("prefix", 0) + rec.get("lost", 0) + rec.get("hits", 0) * 2
                + max(0, rec.get("px", 0)) / 200 + rec.get("fonts", 0) / 50)
    ranked = sorted([r for r in records if r["fail"] and r.get("_diff") is not None], key=score, reverse=True)
    saved = set()
    per_doc_case: dict[tuple, int] = {}
    for rec in ranked:
        key = (rec["doc"], rec["case"])
        if per_doc_case.get(key, 0) >= 1 or len(saved) >= opts.images:
            continue
        per_doc_case[key] = 1
        save_images(rec, images)
        saved.add(rec["id"])

    # Table
    hdr = f"{'case id':46} {'disp':>6} {'inblk':>6} {'prefix':>6} {'fonts':>5} {'ren':>4} {'lost':>4} {'spill':>5} {'hits':>4} {'px':>6} {'lnk':>3} {'pth':>3} {'hol':>3} {'reflow':>14}  status"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for rec in records:
        if rec.get("error"):
            print(f"{rec['id']:46} ERROR: {' '.join(rec['error'].split())[-110:]}")
            continue
        rf = rec.get("reflow") or {}
        rft = (f"{'Y' if rf.get('applied') else 'n'} {rf.get('delta', 0):+.1f}" + ("" if rf.get("applied") else f" {str(rf.get('reason', ''))[:6]}")) if rf else "-"
        print(f"{rec['id']:46} {rec['disp']:6.2f} {rec['inblk']:6.2f} {rec['prefix']:6.2f} {rec['fonts']:5d} {rec.get('renamed', 0):4d} {rec['lost']:4d} "
              f"{rec['spill']:5.0f} {rec['hits']:4d} {rec['px']:6d} {rec['links_lost']:3d} {rec['paths_lost']:3d} {rec.get('holes', 0):3d} {rft:>14}  {rec['status']}"
              + ("  [" + ", ".join(rec["fail"]) + "]" if rec["fail"] else ""))

    real = [r for r in records if not r["info"]]
    failed = [r for r in real if r["fail"]]
    by_case: dict[str, list[int]] = {}
    for r in real:
        s = by_case.setdefault(r["case"], [0, 0])
        s[0] += 1
        s[1] += bool(r["fail"])
    print("\nper case:", ", ".join(f"{k} {v[1]}/{v[0]} fail" for k, v in sorted(by_case.items())))
    noop = [r for r in records if r["case"] == "noop" and r.get("fail")]
    print(f"noop (informational; the app skips no-op commits): {len(noop)} of "
          f"{sum(r['case'] == 'noop' for r in records)} would change the page")
    print(f"images: {images} ({len(saved)} cases)")
    serial = [{k: v for k, v in r.items() if k != "_diff"} for r in records]
    for r in serial:
        r["args"] = [str(a) for a in r["args"]]
    (out / "results.json").write_text(json.dumps(serial, indent=1, ensure_ascii=False, default=str))
    print(f"results: {out / 'results.json'}")
    print(f"\nTOTAL {len(real)} cases, {len(failed)} FAIL (thresholds: disp/prefix <= {DISP_TOL}pt, "
          f"spill <= {SPILL_TOL}pt, px <= {PX_TOL} @ {DPI}dpi, no font change, nothing lost, no overlap)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
