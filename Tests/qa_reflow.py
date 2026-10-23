#!/usr/bin/env python3
"""Paragraph reflow: `replace-block --reflow` pushes / pulls the content below.

Fixtures are generated here with PyMuPDF. Checks:
  * growing a paragraph pushes the two following paragraphs, an image, a line
    drawing, a link, a highlight and a form field down by exactly the growth,
    with the very same fonts/glyph positions (visual fidelity), while the
    footer (page number + rule) stays put;
  * shrinking pulls the content up;
  * on a two-column page only the edited column moves; a full-width image
    that ties both columns together blocks reflow ("spans_columns");
  * pushing past the page body is skipped ("overflow") with content intact;
  * an Arabic paragraph that grows pushes the Arabic/English text below and
    the moved Arabic still extracts in logical order;
  * moved text extracts (sorted reading order, page-blocks), is found by
    search, and can be edited / moved again without leaving ghost glyphs;
  * the per-line path is a no-op and plain replace-block is unchanged.
Ends with "reflow ok".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
OUT = ROOT / "tmp" / "qa" / "reflow"

P1 = "The first paragraph is short and sits at the top of the page body."
P1_LONG = (P1 + " Now it grows with a lot more words so that it wraps onto a second and even a third"
           " line of text inside the very same box.")
P2 = ("Second paragraph follows below it and must be pushed down when the first one grows longer"
      " than before.")
P3 = "Third paragraph closes the column."
FOOTER = "Page 1 of 1"

AR1 = "نمت الإيرادات بشكل كبير خلال هذا العام"
AR1_LONG = (AR1 + " بفضل الاشتراكات الجديدة في منطقة الخليج وبقيت التكاليف التشغيلية مستقرة"
            " بينما انخفض الإنفاق على التسويق بشكل ملحوظ في الربع الأخير من السنة")
AR2 = "هذه الفقرة الثانية يجب أن تنزل إلى الأسفل"
EN_BELOW = "English text below the Arabic paragraphs."

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


def blocks(path: Path) -> list[dict]:
    return engine("page-blocks", "--input", str(path), "--page", "1")["blocks"]


def block_with(path: Path, needle: str) -> dict:
    for block in blocks(path):
        if needle in block["text"]:
            return block
    raise SystemExit(f"no block containing {needle!r} in {path.name}")


def rect_str(rect) -> str:
    return ",".join(f"{v:.3f}" for v in rect)


def replace(src: Path, out: Path, block: dict, text: str, *extra: str) -> dict:
    return engine("replace-block", "--input", str(src), "--output", str(out), "--page", "1",
                  "--rect", rect_str(block["rect"]), "--text", text, "--original-text", block["text"], *extra)


def spans(path: Path) -> list[dict]:
    with pymupdf.open(str(path)) as doc:
        out = []
        for b in doc[0].get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                for s in line["spans"]:
                    if s["text"].strip():
                        out.append({"text": s["text"], "font": s["font"], "size": round(s["size"], 2),
                                    "origin": s["origin"], "bbox": s["bbox"]})
        return out


def spans_of(path: Path, needle: str) -> list[dict]:
    return [s for s in spans(path) if needle in s["text"]]


def first_y(path: Path, needle: str) -> float:
    with pymupdf.open(str(path)) as doc:
        hits = doc[0].search_for(needle)
        return hits[0].y0 if hits else float("nan")


def page_text(path: Path, sort: bool = False) -> str:
    with pymupdf.open(str(path)) as doc:
        return doc[0].get_text(sort=sort)


def ink(path: Path, rect: pymupdf.Rect) -> float:
    with pymupdf.open(str(path)) as doc:
        pix = doc[0].get_pixmap(dpi=96, clip=rect, colorspace=pymupdf.csGRAY)
        dark = sum(1 for v in pix.samples if v < 160)
        return dark / max(1, len(pix.samples))


def flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def gray_pixmap() -> pymupdf.Pixmap:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), False)
    pix.clear_with(90)
    return pix


def make_single_column(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(pymupdf.Rect(72, 72, 540, 120), P1, fontname="helv", fontsize=12)
    page.insert_textbox(pymupdf.Rect(72, 130, 540, 180), P2, fontname="tiro", fontsize=12)
    page.draw_line((72, 190), (540, 190), color=(0, 0, 1), width=1.5)
    page.insert_image(pymupdf.Rect(72, 200, 152, 240), pixmap=gray_pixmap())
    page.insert_textbox(pymupdf.Rect(72, 250, 540, 280), P3, fontname="cour", fontsize=11)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 250, 200, 264), "uri": "https://example.com"})
    page.add_highlight_annot(pymupdf.Rect(72, 250, 200, 264))
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "reflow_field"
    widget.rect = pymupdf.Rect(72, 290, 250, 310)
    widget.field_value = "field value"
    page.add_widget(widget)
    # Footer: a rule and the page number in the bottom margin.
    page.draw_line((72, 750), (540, 750), color=(0.5, 0.5, 0.5), width=0.5)
    page.insert_text((280, 770), FOOTER, fontname="helv", fontsize=9)
    doc.save(str(path))


L_PARAS = [
    "Left column first paragraph with several words that wrap inside the narrow column box.",
    "Left column second paragraph also wraps over a couple of lines of text.",
    "Left column third paragraph ends the column.",
]
R_PARAS = [
    "Right column first paragraph sits beside the left one and must not move at all.",
    "Right column second paragraph stays exactly where it was placed.",
    "Right column third paragraph is the last one.",
]


def make_two_column(path: Path, full_width_image: bool = False) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 60), "Two Column Title", fontname="hebo", fontsize=18)
    y = 90
    for left, right in zip(L_PARAS, R_PARAS):
        page.insert_textbox(pymupdf.Rect(72, y, 290, y + 60), left, fontname="helv", fontsize=11)
        page.insert_textbox(pymupdf.Rect(322, y, 540, y + 60), right, fontname="helv", fontsize=11)
        y += 70
    if full_width_image:
        page.insert_image(pymupdf.Rect(72, y + 10, 540, y + 90), pixmap=gray_pixmap())
    doc.save(str(path))


def make_full_page(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(pymupdf.Rect(72, 72, 540, 100), P1, fontname="helv", fontsize=12)
    y = 110
    n = 0
    while y < 700:
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 40), f"Filler paragraph number {n} keeps the page full.",
                            fontname="helv", fontsize=12)
        y += 30
        n += 1
    doc.save(str(path))


def make_arabic(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    arch = pymupdf.Archive("/System/Library/Fonts/Supplemental")
    css = ("@font-face {font-family: ar; src: url('Arial.ttf');} "
           "body {font-family: ar; font-size: 14pt; color: #222222;} div {text-align: right;}")
    page.insert_htmlbox(pymupdf.Rect(60, 80, 535, 110), f'<div dir="rtl">{AR1}</div>', css=css, archive=arch)
    page.insert_htmlbox(pymupdf.Rect(60, 130, 535, 160), f'<div dir="rtl">{AR2}</div>', css=css, archive=arch)
    page.insert_textbox(pymupdf.Rect(60, 180, 535, 210), EN_BELOW, fontname="tiro", fontsize=12)
    doc.save(str(path), garbage=3, deflate=True)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def moved_by(before: Path, after: Path, needle: str, delta: float, label: str) -> None:
    a, b = spans_of(before, needle), spans_of(after, needle)
    ok = bool(a) and len(a) == len(b) and all(
        x["font"] == y["font"] and x["size"] == y["size"]
        and abs(y["origin"][0] - x["origin"][0]) < 0.05 and abs(y["origin"][1] - x["origin"][1] - delta) < 0.05
        for x, y in zip(a, b)
    )
    check(ok, f"{label}: same font/size, origin shifted by {delta:+.2f}")


def scenario_grow_and_shrink() -> None:
    src = OUT / "single.pdf"
    make_single_column(src)

    # Default behaviour is unchanged: no reflow key, nothing below moves.
    plain = OUT / "single_plain.pdf"
    reply = replace(src, plain, block_with(src, "first paragraph"), P1_LONG)
    check("reflow" not in reply, "plain replace-block: no reflow report")
    check(abs(first_y(plain, "Second paragraph") - first_y(src, "Second paragraph")) < 0.01,
          "plain replace-block: following paragraph not moved")

    grown = OUT / "single_grown.pdf"
    reply = replace(src, grown, block_with(src, "first paragraph"), P1_LONG, "--reflow")
    info = reply.get("reflow", {})
    delta = float(info.get("delta", 0))
    check(info.get("applied") is True and delta > 20, f"grow: reflow applied, delta {delta:.2f}")
    check(info.get("lines") == 3 and info.get("images") == 1 and info.get("drawings") == 1,
          f"grow: moved 3 text lines, 1 image, 1 drawing ({info})")

    edited = block_with(grown, "first paragraph")
    p2 = block_with(grown, "Second paragraph")
    p2_src = block_with(src, "Second paragraph")
    gap_before = p2_src["rect"][1] - block_with(src, "first paragraph")["rect"][3]
    gap_after = p2["rect"][1] - edited["rect"][3]
    check(abs(gap_before - gap_after) < 0.6, f"grow: paragraph gap kept ({gap_before:.2f} -> {gap_after:.2f})")
    check(edited["text"].replace("\n", " ") == P1_LONG, "grow: edited paragraph text")

    moved_by(src, grown, "Second paragraph", delta, "grow: paragraph 2")
    moved_by(src, grown, "than before", delta, "grow: paragraph 2 line 2")
    moved_by(src, grown, "Third paragraph", delta, "grow: paragraph 3")
    moved_by(src, grown, FOOTER, 0.0, "grow: footer page number")

    with pymupdf.open(str(src)) as d0, pymupdf.open(str(grown)) as d1:
        p0, p1 = d0[0], d1[0]
        img0, img1 = p0.get_image_info()[0]["bbox"], p1.get_image_info()[0]["bbox"]
        check(abs(img1[1] - img0[1] - delta) < 0.05 and abs(img1[0] - img0[0]) < 0.05
              and abs((img1[3] - img1[1]) - (img0[3] - img0[1])) < 0.05, "grow: image moved, same size")
        lines0 = sorted((d["rect"].y0, d["rect"].x0, d.get("color")) for d in p0.get_drawings() if d.get("color"))
        lines1 = sorted((d["rect"].y0, d["rect"].x0, d.get("color")) for d in p1.get_drawings() if d.get("color"))
        blue0 = [l for l in lines0 if l[2] == (0.0, 0.0, 1.0)]
        blue1 = [l for l in lines1 if l[2] == (0.0, 0.0, 1.0)]
        check(len(blue1) == 1 and abs(blue1[0][0] - blue0[0][0] - delta) < 0.05, "grow: blue rule moved")
        grey1 = [l for l in lines1 if l[2] and abs(l[2][0] - 0.5) < 0.01]
        check(len(grey1) == 1 and abs(grey1[0][0] - 750) < 0.05, "grow: footer rule stays put")
        link0, link1 = p0.get_links()[0]["from"], p1.get_links()[0]["from"]
        check(abs(link1.y0 - link0.y0 - delta) < 0.05, "grow: link moved with its text")
        hl0 = [a.rect for a in p0.annots(types=[pymupdf.PDF_ANNOT_HIGHLIGHT])][0]
        hl1 = [a.rect for a in p1.annots(types=[pymupdf.PDF_ANNOT_HIGHLIGHT])]
        check(len(hl1) == 1 and abs(hl1[0].y0 - hl0.y0 - delta) < 0.1, "grow: highlight moved")
        w1 = [w for w in p1.widgets()]
        check(len(w1) == 1 and abs(w1[0].rect.y0 - 290 - delta) < 0.1 and w1[0].field_value == "field value",
              "grow: form field moved, value kept")
        hits = p1.search_for("closes the column")
        check(len(hits) == 1 and abs(hits[0].y0 - p0.search_for("closes the column")[0].y0 - delta) < 0.05,
              "grow: search finds moved text at its new place")
        # No overlap: the new last line ends above the moved paragraph.
        check(edited["rect"][3] < p2["rect"][1], "grow: edited text does not overlap the moved paragraph")

    ordered = flat(page_text(grown, sort=True))
    pos = [ordered.find(s) for s in ("first paragraph", "Second paragraph", "closes the column", FOOTER)]
    check(all(p >= 0 for p in pos) and pos == sorted(pos), "grow: extracted reading order P1, P2, P3, footer")
    natural = flat(page_text(grown))
    check(natural.count("Second paragraph") == 1 and natural.count("closes the column") == 1,
          "grow: moved text extracted exactly once (no hidden duplicates)")
    check(natural.find("Second paragraph") < natural.find("closes the column"),
          "grow: P2 before P3 in content order")

    # Visual: the moved paragraph renders the same amount of ink at its new place.
    r0 = pymupdf.Rect(p2_src["rect"]) + (-1, -1, 1, 1)
    check(abs(ink(src, r0) - ink(grown, r0 + (0, delta, 0, delta))) < 0.01, "grow: moved paragraph renders the same")

    # --- re-edit the moved paragraph: no ghost glyphs -----------------------
    reedit = OUT / "single_reedit.pdf"
    p2_moved = block_with(grown, "Second paragraph")
    reply = replace(grown, reedit, p2_moved, "Second paragraph, now short.", "--reflow")
    info2 = reply.get("reflow", {})
    check(info2.get("applied") is True and float(info2.get("delta", 0)) < -5,
          f"re-edit moved paragraph: shrink reflow applied ({info2})")
    text = page_text(reedit)
    check("pushed down" not in text and "than before" not in text, "re-edit: old glyphs gone from extraction")
    check(text.count("Second paragraph") == 1, "re-edit: paragraph extracted once")
    old_line1_tail = pymupdf.Rect(300, p2_moved["rect"][1] + 1, 520, p2_moved["rect"][1] + 12)
    check(ink(reedit, old_line1_tail) < 0.002, "re-edit: no ghost ink where the old line was")
    moved_by(grown, reedit, "Third paragraph", float(info2["delta"]), "shrink: paragraph 3 pulled up")
    moved_by(grown, reedit, FOOTER, 0.0, "shrink: footer stays")
    with pymupdf.open(str(reedit)) as d:
        check(len(d[0].search_for("closes the column")) == 1, "shrink: search still finds P3 once")

    # block-at / move-block on text that was moved by reflow
    p3 = block_with(reedit, "Third paragraph")
    cx, cy = (p3["rect"][0] + p3["rect"][2]) / 2, (p3["rect"][1] + p3["rect"][3]) / 2
    hit = engine("block-at", "--input", str(reedit), "--page", "1", "--x", f"{cx:.2f}", "--y", f"{cy:.2f}")
    check(hit.get("found") and hit.get("text") == P3, "block-at finds the moved paragraph")
    moved_file = OUT / "single_move_block.pdf"
    engine("move-block", "--input", str(reedit), "--output", str(moved_file), "--page", "1",
           "--rect", rect_str(p3["rect"]), "--text", p3["text"], "--dest-x", "300", "--dest-y", "500")
    text = page_text(moved_file)
    check(text.count("Third paragraph") == 1, "move-block on moved text: no duplicate left behind")
    check(ink(moved_file, pymupdf.Rect(p3["rect"]) + (-1, -1, 1, 1)) < 0.002, "move-block: old place blank")


def scenario_per_line_noop() -> None:
    src = OUT / "single.pdf"
    out = OUT / "single_perline.pdf"
    block = block_with(src, "Second paragraph")
    rows = block["text"].split("\n")
    edited = "\n".join([rows[0].replace("Second", "2nd"), *rows[1:]])
    reply = replace(src, out, block, edited, "--reflow")
    info = reply.get("reflow", {})
    check(info.get("applied") is False and info.get("reason") == "noop", f"per-line edit: reflow no-op ({info})")
    check("2nd paragraph" in page_text(out), "per-line edit applied")
    moved_by(src, out, "Third paragraph", 0.0, "per-line edit: nothing moved")


def scenario_two_columns() -> None:
    src = OUT / "two_col.pdf"
    make_two_column(src)
    out = OUT / "two_col_grown.pdf"
    block = block_with(src, "Left column first")
    longer = (L_PARAS[0] + " It now carries two extra sentences that need several more lines in the"
              " column. The paragraphs below in this column have to make room for them.")
    reply = replace(src, out, block, longer, "--reflow")
    info = reply.get("reflow", {})
    delta = float(info.get("delta", 0))
    check(info.get("applied") is True and delta > 10, f"two columns: reflow applied ({info})")
    moved_by(src, out, "Left column second", delta, "two columns: left paragraph 2 moved")
    moved_by(src, out, "Left column third", delta, "two columns: left paragraph 3 moved")
    for needle in ("Right column first", "Right column second", "Right column third", "Two Column Title"):
        moved_by(src, out, needle, 0.0, f"two columns: {needle!r} untouched")

    tied = OUT / "two_col_image.pdf"
    make_two_column(tied, full_width_image=True)
    out = OUT / "two_col_image_grown.pdf"
    reply = replace(tied, out, block_with(tied, "Left column first"), longer, "--reflow")
    info = reply.get("reflow", {})
    check(info.get("applied") is False and info.get("reason") == "spans_columns",
          f"full-width image under both columns blocks reflow ({info})")
    check(flat(block_with(out, "Left column first")["text"]) == flat(longer), "blocked reflow: the edit itself applied")
    for needle in ("Left column second", "Right column second", "Right column third"):
        moved_by(tied, out, needle, 0.0, f"blocked reflow: {needle!r} untouched")
    with pymupdf.open(str(tied)) as d0, pymupdf.open(str(out)) as d1:
        check(d0[0].get_image_info()[0]["bbox"] == d1[0].get_image_info()[0]["bbox"], "blocked reflow: image untouched")


def scenario_overflow() -> None:
    src = OUT / "full.pdf"
    make_full_page(src)
    out = OUT / "full_grown.pdf"
    reply = replace(src, out, block_with(src, "first paragraph"), P1_LONG, "--reflow")
    info = reply.get("reflow", {})
    check(info.get("applied") is False and info.get("reason") == "overflow", f"overflow skipped ({info})")
    text = page_text(out)
    n = sum(1 for i in range(40) if f"Filler paragraph number {i} " in text)
    check(n == len([1 for i in range(40) if f"Filler paragraph number {i} " in page_text(src)]),
          "overflow: every paragraph still present")
    moved_by(src, out, "number 5 ", 0.0, "overflow: content below not moved")
    check(flat(block_with(out, "first paragraph")["text"]) == flat(P1_LONG), "overflow: the edit itself applied")


def scenario_arabic() -> None:
    raw = OUT / "arabic_raw.pdf"
    make_arabic(raw)
    # insert_htmlbox stores presentation forms (not searchable by the logical
    # word); re-render the second paragraph through the engine so it carries
    # logical text like every paragraph the app has edited.
    src = OUT / "arabic.pdf"
    replace(raw, src, block_with(raw, "الفقرة"), AR2)
    with pymupdf.open(str(src)) as d:
        # Shaped glyphs come back as one rect per glyph cluster.
        word_hits = d[0].search_for("الثانية")
        check(bool(word_hits), "arabic fixture: second paragraph searchable")
    block = block_with(src, AR1.split()[0])
    check(block["text"] == AR1, "arabic fixture: first paragraph extracts logically")
    out = OUT / "arabic_grown.pdf"
    reply = replace(src, out, block, AR1_LONG, "--reflow")
    info = reply.get("reflow", {})
    delta = float(info.get("delta", 0))
    check(info.get("applied") is True and delta > 5, f"arabic grow: reflow applied ({info})")
    edited = block_with(out, "نمت")
    check(flat(edited["text"]) == flat(AR1_LONG), "arabic grow: edited paragraph extracts logically")
    second = block_with(out, "الفقرة")
    check(second["text"] == AR2, "arabic grow: moved Arabic paragraph still extracts logically")
    check(abs(second["rect"][1] - block_with(src, "الفقرة")["rect"][1] - delta) < 0.1,
          "arabic grow: Arabic paragraph below moved by delta")
    check(second["rect"][1] > edited["rect"][3], "arabic grow: no overlap with the grown paragraph")
    moved_by(src, out, "English text below", delta, "arabic grow: English paragraph moved")
    with pymupdf.open(str(out)) as d:
        moved_hits = d[0].search_for("الثانية")
        check(len(moved_hits) == len(word_hits) and all(abs(b.y0 - a.y0 - delta) < 0.1 and abs(b.x0 - a.x0) < 0.1
                                                        for a, b in zip(word_hits, moved_hits)),
              "arabic grow: search finds the moved Arabic word at its new place")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    scenario_grow_and_shrink()
    scenario_per_line_noop()
    scenario_two_columns()
    scenario_overflow()
    scenario_arabic()
    if failures:
        raise SystemExit(f"{len(failures)} check(s) failed: {failures}")
    print("reflow ok")


if __name__ == "__main__":
    main()
