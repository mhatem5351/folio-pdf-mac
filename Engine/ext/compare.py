"""Compare two PDFs and produce a side-by-side report.

``compare --old A.pdf --new B.pdf --output report.pdf [--json changes.json]
[--mode text|visual|both]``

Text mode diffs the whole-document word streams (so text that reflows onto
another page is not reported as changed), then maps every changed word back
to its page and box. Pages are paired by a monotonic alignment that maximises
shared words, so inserted/removed pages show up as unpaired rows instead of
shifting every later comparison. Visual mode renders paired pages and boxes
the regions whose pixels differ (scans, drawings, images).

Coordinates in the JSON are PDF points in PyMuPDF's page space: origin at the
top-left of the unrotated page, y growing downwards (the "engine coords"
used throughout Engine/pdf_engine.py).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import html as htmllib
import importlib.util
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

RED = (0.89, 0.22, 0.24)
GREEN = (0.08, 0.62, 0.38)
AMBER = (0.96, 0.58, 0.04)
CANVAS = (0.925, 0.933, 0.95)
INK = (0.11, 0.11, 0.13)
MUTED = (0.42, 0.43, 0.48)

MARGIN = 24.0
HEADER = 46.0
GUTTER = 26.0
SUMMARY_ROWS = 60

_helpers: Any = None


def convert_helpers() -> Any:
    """Shared HTML/font helpers from the sibling convert extension."""
    global _helpers
    if _helpers is None:
        path = Path(__file__).with_name("convert.py")
        spec = importlib.util.spec_from_file_location("pdfstudio_ext_convert_helpers", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _helpers = module
    return _helpers


# --- Text extraction and diff -------------------------------------------------

class Word:
    __slots__ = ("key", "text", "page", "rect")

    def __init__(self, key: str, text: str, page: int, rect: tuple[float, float, float, float]):
        self.key = key
        self.text = text
        self.page = page
        self.rect = rect


_MARKS = "\u0300-\u036f\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u200c\u200d"
#: A token is a run of letters/digits (with combining marks, inner apostrophes
#: and hyphens) or a single punctuation character, so "reduced." vs
#: "reduced sharply." diffs as one inserted word, not a replaced phrase.
TOKEN_RE = re.compile(rf"(?:[^\W_]|[{_MARKS}])+(?:['’\-](?:[^\W_]|[{_MARKS}])+)*|\S")


def _is_rtl(text: str) -> bool:
    return any("\u0590" <= ch <= "\u08ff" or "\ufb1d" <= ch <= "\ufeff" for ch in text)


def split_word(text: str, rect: tuple[float, float, float, float]) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Tokens of one extracted word with boxes estimated by character share."""
    tokens = [(m.group(0), m.start(), m.end()) for m in TOKEN_RE.finditer(text)]
    if len(tokens) <= 1:
        return [(text, rect)]
    x0, y0, x1, y1 = rect
    width = x1 - x0
    total = float(len(text))
    rtl = _is_rtl(text)
    out = []
    for token, start, end in tokens:
        a, b = start / total, end / total
        if rtl:  # logical start sits at the right edge
            a, b = 1.0 - b, 1.0 - a
        out.append((token, (x0 + a * width, y0, x0 + b * width, y1)))
    return out


def extract_words(doc: Any, ignore_case: bool) -> list[Word]:
    words: list[Word] = []
    for pno, page in enumerate(doc):
        for x0, y0, x1, y1, text, *_ in page.get_text("words"):
            text = text.strip()
            if not text:
                continue
            if any("\ufb00" <= ch <= "\ufeff" for ch in text):
                text = unicodedata.normalize("NFKC", text)  # ligatures / presentation forms
            for token, rect in split_word(text, (x0, y0, x1, y1)):
                key = unicodedata.normalize("NFKC", token)
                if ignore_case:
                    key = key.casefold()
                words.append(Word(key, token, pno, rect))
    return words


def join_tokens(words: list[Word]) -> str:
    """Readable text from tokens: no space before closing punctuation."""
    out = ""
    for word in words:
        if out and not (len(word.text) == 1 and word.text in ".,;:!?)]}%’”،؛؟") and not out.endswith(("(", "[", "{", "“", "‘")):
            out += " "
        out += word.text
    return out


def merge_rects(words: list[Word]) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Union word boxes that sit next to each other on the same line."""
    merged: list[tuple[int, list[float]]] = []
    for word in words:
        x0, y0, x1, y1 = word.rect
        if merged:
            page, box = merged[-1]
            height = max(1.0, min(y1 - y0, box[3] - box[1]))
            overlap = min(y1, box[3]) - max(y0, box[1])
            gap = max(x0 - box[2], box[0] - x1)  # either direction (RTL lines)
            if page == word.page and overlap > 0.5 * height and gap < 1.6 * height:
                box[0], box[1] = min(box[0], x0), min(box[1], y0)
                box[2], box[3] = max(box[2], x1), max(box[3], y1)
                continue
        merged.append((word.page, [x0, y0, x1, y1]))
    return [(page, tuple(box)) for page, box in merged]  # type: ignore[misc]


def snippet(words: list[Word], limit: int = 220) -> str:
    text = join_tokens(words)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def diff_words(old: list[Word], new: list[Word]) -> tuple[list[dict[str, Any]], dict[tuple[int, int], int]]:
    """Word-level diff; returns change records and shared-word counts per page pair."""
    a = [word.key for word in old]
    b = [word.key for word in new]
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=len(a) + len(b) > 60000)
    shared: dict[tuple[int, int], int] = {}
    changes: list[dict[str, Any]] = []
    opcodes = _align_to_boundaries(matcher.get_opcodes(), a, b)
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for offset in range(i2 - i1):
                pair = (old[i1 + offset].page, new[j1 + offset].page)
                shared[pair] = shared.get(pair, 0) + 1
            continue
        # Anchor pages for the side without words: the preceding word.
        anchor_old = old[i1 - 1].page if 0 < i1 <= len(old) else (old[0].page if old else 0)
        anchor_new = new[j1 - 1].page if 0 < j1 <= len(new) else (new[0].page if new else 0)
        # One record per page: a change that runs across a page break on
        # either side is split, so every record points at a single report row.
        old_groups = _group_by_page(old[i1:i2])
        new_groups = _group_by_page(new[j1:j2])
        for index in range(max(len(old_groups), len(new_groups))):
            old_words = old_groups[index] if index < len(old_groups) else []
            new_words = new_groups[index] if index < len(new_groups) else []
            kind = "replace" if old_words and new_words else ("delete" if old_words else "insert")
            changes.append({
                "type": kind,
                "old_words": old_words,
                "new_words": new_words,
                "page_old": old_words[0].page if old_words else anchor_old,
                "page_new": new_words[0].page if new_words else anchor_new,
            })
            if old_words:
                anchor_old = old_words[-1].page
            if new_words:
                anchor_new = new_words[-1].page
    return changes, shared


_SENTENCE_END = {".", "!", "?", ":", ";", "؟", "؛", "۔"}


def _boundary_score(seq: list[str], start: int, end: int) -> int:
    """How natural it is for an inserted/deleted run seq[start:end] to sit
    here: starting a sentence and ending with punctuation reads best."""
    score = 0
    prev = seq[start - 1] if start > 0 else None
    if prev is None or prev in _SENTENCE_END:
        score += 2
    elif prev in {",", "،"}:
        score += 1
    if seq[end - 1] in _SENTENCE_END:
        score += 1
    if seq[start][:1].isupper():
        score += 1
    return score


def _align_to_boundaries(opcodes: list[tuple[str, int, int, int, int]], a: list[str], b: list[str]) -> list[tuple[str, int, int, int, int]]:
    """Slide pure insertions/deletions through repeated text to the most
    readable position (a lossless shift: the diff stays minimal). Without
    this, repeated phrases make an added sentence show up as "…end of the
    previous sentence. Start of the added one…"."""
    ops = [list(op) for op in opcodes]
    for index, op in enumerate(ops):
        tag, i1, i2, j1, j2 = op
        if tag not in ("insert", "delete"):
            continue
        before = ops[index - 1] if index > 0 and ops[index - 1][0] == "equal" else None
        after = ops[index + 1] if index + 1 < len(ops) and ops[index + 1][0] == "equal" else None
        if before is None and after is None:
            continue
        seq, start, end = (b, j1, j2) if tag == "insert" else (a, i1, i2)
        max_left = (before[2] - before[1]) if before else 0
        max_right = (after[2] - after[1]) if after else 0
        best_shift, best_score = 0, _boundary_score(seq, start, end)
        shift = 0
        while -shift < max_left and start + shift - 1 >= 0 and seq[start + shift - 1] == seq[end + shift - 1]:
            shift -= 1
            score = _boundary_score(seq, start + shift, end + shift)
            if score > best_score:
                best_shift, best_score = shift, score
        shift = 0
        while shift < max_right and end + shift < len(seq) and seq[start + shift] == seq[end + shift]:
            shift += 1
            score = _boundary_score(seq, start + shift, end + shift)
            if score > best_score:
                best_shift, best_score = shift, score
        if best_shift:
            op[1] += best_shift
            op[2] += best_shift
            op[3] += best_shift
            op[4] += best_shift
            if before:
                before[2] += best_shift
                before[4] += best_shift
            if after:
                after[1] += best_shift
                after[3] += best_shift
    return [tuple(op) for op in ops]  # type: ignore[misc]


def _group_by_page(words: list[Word]) -> list[list[Word]]:
    groups: list[list[Word]] = []
    for word in words:
        if groups and groups[-1][-1].page == word.page:
            groups[-1].append(word)
        else:
            groups.append([word])
    return groups


def align_pages(n_old: int, n_new: int, shared: dict[tuple[int, int], int]) -> list[tuple[int | None, int | None]]:
    """Monotonic page pairing maximising shared words (pairing always earns a
    small bonus so equal-length documents without text pair 1:1)."""
    if n_old == 0 or n_new == 0:
        return [(i, None) for i in range(n_old)] + [(None, j) for j in range(n_new)]
    bonus = 0.5
    best = [[0.0] * (n_new + 1) for _ in range(n_old + 1)]
    move = [[0] * (n_new + 1) for _ in range(n_old + 1)]  # 1 diag, 2 up, 3 left
    for i in range(1, n_old + 1):
        move[i][0] = 2
    for j in range(1, n_new + 1):
        move[0][j] = 3
    for i in range(1, n_old + 1):
        row, prev = best[i], best[i - 1]
        for j in range(1, n_new + 1):
            diag = prev[j - 1] + shared.get((i - 1, j - 1), 0) + bonus
            up = prev[j]
            left = row[j - 1]
            if diag >= up and diag >= left:
                row[j], move[i][j] = diag, 1
            elif up >= left:
                row[j], move[i][j] = up, 2
            else:
                row[j], move[i][j] = left, 3
    pairs: list[tuple[int | None, int | None]] = []
    i, j = n_old, n_new
    while i > 0 or j > 0:
        step = move[i][j]
        if step == 1:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif step == 2:
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    pairs.reverse()
    return pairs


# --- Visual diff ----------------------------------------------------------------

def render_rgb(page: Any, dpi: int) -> Any:
    import pymupdf
    from PIL import Image

    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def visual_regions(old_page: Any, new_page: Any, dpi: int) -> tuple[list[tuple[float, float, float, float]], float]:
    """Boxes (rotated-page points) where the two renders differ, and the
    changed-area fraction."""
    from PIL import Image, ImageChops

    a = render_rgb(old_page, dpi)
    b = render_rgb(new_page, dpi)
    width, height = max(a.width, b.width), max(a.height, b.height)
    if a.size != (width, height):
        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        canvas.paste(a, (0, 0))
        a = canvas
    if b.size != (width, height):
        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        canvas.paste(b, (0, 0))
        b = canvas
    # Largest per-channel difference, so hue changes count as much as
    # brightness changes; small deltas (anti-aliasing) are ignored.
    red, green, blue = ImageChops.difference(a, b).split()
    delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = delta.point(lambda v: 255 if v > 60 else 0)
    if mask.getbbox() is None:
        return [], 0.0
    cell = 6
    grid = mask.reduce(cell)  # box-average per cell
    gw, gh = grid.size
    data = grid.load()
    hot = {(x, y) for y in range(gh) for x in range(gw) if data[x, y] >= 8}
    changed_fraction = len(hot) / float(max(1, gw * gh))
    boxes: list[list[int]] = []
    seen: set[tuple[int, int]] = set()
    reach = 2  # merge cells up to 2 cells apart into one region
    for start in hot:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        x0 = x1 = start[0]
        y0 = y1 = start[1]
        while stack:
            x, y = stack.pop()
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    nxt = (x + dx, y + dy)
                    if nxt in hot and nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        boxes.append([x0, y0, x1 + 1, y1 + 1])
    scale = cell * 72.0 / dpi
    pad = 2.0
    regions = [
        (box[0] * scale - pad, box[1] * scale - pad, box[2] * scale + pad, box[3] * scale + pad)
        for box in boxes
    ]
    regions.sort(key=lambda r: (r[1], r[0]))
    if len(regions) > 150:  # pathological noise: one box around everything
        regions = [(min(r[0] for r in regions), min(r[1] for r in regions),
                    max(r[2] for r in regions), max(r[3] for r in regions))]
    return regions, changed_fraction


# --- Report rendering -------------------------------------------------------------

def _coverage(region: Any, rects: list[Any]) -> float:
    """Approximate share of region covered by rects (overlaps counted once
    per rect, capped at 1)."""
    area = region.width * region.height
    if area <= 0:
        return 1.0
    covered = sum(abs(region & rect) for rect in rects)
    return min(1.0, covered / area)


def _to_display(page: Any, rect: tuple[float, float, float, float]) -> Any:
    """Unrotated page coords -> rotated (displayed) page coords."""
    import pymupdf

    return pymupdf.Rect(rect) * page.rotation_matrix


def draw_side(report_page: Any, target: Any, src_doc: Any, pno: int | None, title: str,
              placeholder: str) -> None:
    import pymupdf

    shadow = target + (1.5, 2.5, 1.5, 2.5)
    report_page.draw_rect(shadow, color=None, fill=(0.82, 0.83, 0.86), overlay=True)
    report_page.draw_rect(target, color=(0.78, 0.79, 0.82), fill=(1, 1, 1), width=0.5, overlay=True)
    if pno is not None:
        report_page.show_pdf_page(target, src_doc, pno, keep_proportion=True)
    else:
        report_page.draw_rect(target, color=None, fill=(0.97, 0.97, 0.98), overlay=True)
        box = pymupdf.Rect(target.x0 + 20, target.y0 + target.height / 2 - 30, target.x1 - 20,
                           target.y0 + target.height / 2 + 30)
        report_page.insert_textbox(box, placeholder, fontname="helv", fontsize=12, color=MUTED,
                                   align=pymupdf.TEXT_ALIGN_CENTER)
    report_page.insert_text((target.x0, MARGIN + 16), title, fontname="hebo", fontsize=10.5, color=INK)


def badge(page: Any, right_x: float, y: float, text: str, color: tuple[float, float, float]) -> float:
    """Pill label ending at right_x; returns its left edge."""
    import pymupdf

    width = pymupdf.get_text_length(text, fontname="hebo", fontsize=8.5) + 14
    rect = pymupdf.Rect(right_x - width, y, right_x, y + 15)
    page.draw_rect(rect, color=None, fill=color, fill_opacity=0.14, radius=0.5, overlay=True)
    page.insert_text((rect.x0 + 7, rect.y0 + 10.6), text, fontname="hebo", fontsize=8.5, color=color)
    return rect.x0 - 6


def build_summary(info: dict[str, Any], changes: list[dict[str, Any]], width: float, height: float) -> tuple[Any, list[tuple[int, Any, int]]]:
    """Story-rendered summary pages; returns (doc, [(summary page, rect, report row)])."""
    import pymupdf

    helpers = convert_helpers()
    esc = htmllib.escape
    stats = [
        ("Insertions", info["insertions"], "#12945f"),
        ("Deletions", info["deletions"], "#d9383c"),
        ("Changed pages", len(info["changed_rows"]), "#5b4fe9"),
    ]
    if info["mode"] in ("visual", "both"):
        stats.append(("Visual regions", info["visual_regions"], "#d98a06"))
    stat_cells = "".join(
        f'<td class="stat"><div class="num" style="color:{color}">{value}</div>'
        f'<div class="lbl">{label}</div></td>'
        for label, value, color in stats
    )
    rows = []
    for index, change in enumerate(changes[:SUMMARY_ROWS]):
        kind = change["type"]
        label, color = {"insert": ("Inserted", "#12945f"), "delete": ("Deleted", "#d9383c"),
                        "replace": ("Changed", "#b7791f")}[kind]
        if kind == "replace":
            text = (f'<span class="old">{esc(change["old_text"])}</span> '
                    f'<span class="arrow">→</span> <span class="new">{esc(change["text"])}</span>')
        elif kind == "delete":
            text = f'<span class="old">{esc(change["text"])}</span>'
        else:
            text = f'<span class="new">{esc(change["text"])}</span>'
        po = change["page_old"] if change["page_old"] else "–"
        pn = change["page_new"] if change["page_new"] else "–"
        rows.append(
            f'<tr id="chg{index}"><td class="n">{index + 1}</td>'
            f'<td class="kind" style="color:{color}">{label}</td>'
            f'<td class="pg">{po}</td><td class="pg">{pn}</td><td class="txt">{text}</td></tr>'
        )
    more = len(changes) - SUMMARY_ROWS
    if info["identical"]:
        body_changes = '<p class="banner ok">No differences found. The documents have the same text.</p>'
        if info["mode"] in ("visual", "both"):
            body_changes = '<p class="banner ok">No differences found in text or appearance.</p>'
    elif not changes and info["mode"] == "visual":
        body_changes = ('<p class="banner">Visual comparison only. Changed regions are boxed in amber '
                        'on the pages that follow.</p>')
    elif not changes:
        body_changes = '<p class="banner">No text changes. Differences are visual only (boxed in amber).</p>'
    else:
        body_changes = (
            '<table class="changes"><tr><th class="n">#</th><th>Change</th><th class="pg">Original</th>'
            '<th class="pg">Revised</th><th>Text</th></tr>' + "".join(rows) + "</table>"
        )
        if more > 0:
            body_changes += f'<p class="more">…and {more} more change{"s" if more != 1 else ""}. See the pages that follow, or the JSON export.</p>'
    notes = []
    if not info["text_found"] and info["mode"] == "text":
        notes.append('<p class="banner warn">Neither document has extractable text (scanned pages?). '
                     'Run the comparison in Visual mode, or recognize text first.</p>')
    legend = ('<p class="legend"><span class="sw del">&#160;&#160;&#160;</span> Deleted from the original'
              '&#160;&#160;&#160;<span class="sw ins">&#160;&#160;&#160;</span> Inserted in the revision')
    if info["mode"] in ("visual", "both"):
        legend += '&#160;&#160;&#160;<span class="sw vis">&#160;&#160;&#160;</span> Visual difference'
    legend += "</p>"
    html = f"""
<html><body>
<p class="eyebrow">PDF STUDIO · COMPARE</p>
<h1>Comparison Report</h1>
<table class="files"><tr>
<td class="file"><div class="role">ORIGINAL</div><div class="fname">{esc(info["old_name"])}</div>
<div class="meta">{info["pages_old"]} page{"s" if info["pages_old"] != 1 else ""}</div></td>
<td class="file"><div class="role">REVISED</div><div class="fname">{esc(info["new_name"])}</div>
<div class="meta">{info["pages_new"]} page{"s" if info["pages_new"] != 1 else ""}</div></td>
</tr></table>
<table class="stats"><tr>{stat_cells}</tr></table>
{legend}
{"".join(notes)}
{body_changes}
<p class="foot">Generated {esc(info["generated"])} · mode: {esc(info["mode"])}</p>
</body></html>"""
    font_rules, families = helpers._font_css()
    css = font_rules + f"""
body {{ font-family: {families["sans"]}; font-size: 9.5pt; color: #1d1d1f; line-height: 1.35; }}
.eyebrow {{ font-size: 8pt; color: #8e91a0; font-weight: bold; margin: 0 0 2pt 0; }}
h1 {{ font-size: 22pt; margin: 0 0 12pt 0; color: #16171d; }}
table {{ border-collapse: collapse; width: 100%; }}
.files td {{ width: 50%; padding: 8pt 10pt; border: 0.6pt solid #dcdee5; background-color: #f7f8fa; }}
.role {{ font-size: 7.5pt; font-weight: bold; color: #8e91a0; }}
.fname {{ font-size: 12pt; font-weight: bold; margin: 2pt 0; }}
.meta {{ color: #5d6070; font-size: 9pt; }}
.stats {{ margin: 10pt 0 6pt 0; }}
.stat {{ width: 25%; padding: 6pt 10pt; border: 0.6pt solid #e4e5ea; }}
.num {{ font-size: 20pt; font-weight: bold; }}
.lbl {{ font-size: 8.5pt; color: #5d6070; }}
.legend {{ font-size: 8.5pt; color: #5d6070; margin: 4pt 0 10pt 0; }}
.sw {{ font-size: 8pt; }}
.sw.del {{ background-color: #f6c5c6; }}
.sw.ins {{ background-color: #bfe8d2; }}
.sw.vis {{ background-color: #fbe0b0; }}
.changes th {{ text-align: left; font-size: 8pt; color: #5d6070; background-color: #f0f1f5; padding: 4pt 6pt; border-bottom: 0.6pt solid #d0d2d9; }}
.changes td {{ padding: 4pt 6pt; border-bottom: 0.5pt solid #e6e7ec; vertical-align: top; }}
.n {{ width: 22pt; color: #8e91a0; }}
.kind {{ width: 52pt; font-weight: bold; }}
.pg {{ width: 46pt; text-align: center; color: #5d6070; }}
.old {{ background-color: #fbdcdc; text-decoration: line-through; }}
.new {{ background-color: #d4f1e1; }}
.arrow {{ color: #8e91a0; }}
.banner {{ padding: 8pt 10pt; background-color: #eef0ff; border: 0.6pt solid #c9c6f7; }}
.banner.ok {{ background-color: #e6f6ee; border: 0.6pt solid #a9dfc4; }}
.banner.warn {{ background-color: #fff4e0; border: 0.6pt solid #f1d19a; }}
.more {{ color: #5d6070; margin-top: 6pt; }}
.foot {{ color: #8e91a0; font-size: 8pt; margin-top: 14pt; }}
"""
    archive = pymupdf.Archive()
    if helpers.SUPPLEMENTAL_FONTS.is_dir():
        archive.add(str(helpers.SUPPLEMENTAL_FONTS))
    story = pymupdf.Story(html=helpers.auto_dir(html), user_css=css, archive=archive)
    mediabox = pymupdf.Rect(0, 0, width, height)
    where = mediabox + (40, 36, -40, -36)
    positions: list[tuple[int, Any, int]] = []

    def rectfn(rect_num: int, filled: Any) -> tuple[Any, Any, Any]:
        return mediabox, where, None

    def positionfn(position: Any) -> None:
        ident = getattr(position, "id", "") or ""
        if ident.startswith("chg") and getattr(position, "open_close", 0) & 1:
            index = int(ident[3:])
            # page_num / rect_num are 1-based.
            page_num = getattr(position, "page_num", None) or position.rect_num
            positions.append((page_num - 1, pymupdf.Rect(position.rect), index))

    doc = story.write_with_links(rectfn, positionfn=positionfn)
    return doc, positions


def register(subparsers: Any, api: Any) -> None:
    def cmd_compare(args: argparse.Namespace) -> int:
        import pymupdf

        old_path, new_path = Path(args.old), Path(args.new)
        for path in (old_path, new_path):
            if not path.is_file():
                return api.respond(False, message=f"file not found: {path}")
        old_doc = pymupdf.open(old_path)
        new_doc = pymupdf.open(new_path)
        for path, doc in ((old_path, old_doc), (new_path, new_doc)):
            if doc.needs_pass:
                return api.respond(False, message=f"{path.name} is password-protected; unlock it first")
            if doc.page_count == 0:
                return api.respond(False, message=f"{path.name} has no pages")
        old_label = args.old_label or old_path.name
        new_label = args.new_label or new_path.name
        mode = args.mode
        use_text = mode in ("text", "both")
        use_visual = mode in ("visual", "both")

        old_words = extract_words(old_doc, args.ignore_case) if use_text else []
        new_words = extract_words(new_doc, args.ignore_case) if use_text else []
        text_found = bool(old_words or new_words)
        raw_changes, shared = diff_words(old_words, new_words) if use_text else ([], {})
        if use_text and text_found:
            rows = align_pages(old_doc.page_count, new_doc.page_count, shared)
        else:
            count = max(old_doc.page_count, new_doc.page_count)
            rows = [(i if i < old_doc.page_count else None, i if i < new_doc.page_count else None)
                    for i in range(count)]
        # A replacement whose two sides landed on pages that are not shown
        # side by side reads better as a deletion plus an insertion.
        partner = {i: j for i, j in rows if i is not None and j is not None}
        split_changes: list[dict[str, Any]] = []
        for change in raw_changes:
            if change["type"] == "replace" and partner.get(change["page_old"]) != change["page_new"]:
                split_changes.append(dict(change, type="delete", new_words=[]))
                split_changes.append(dict(change, type="insert", old_words=[]))
            else:
                split_changes.append(change)
        raw_changes = split_changes
        row_of_old = {i: r for r, (i, _) in enumerate(rows) if i is not None}
        row_of_new = {j: r for r, (_, j) in enumerate(rows) if j is not None}

        # Highlights per page.
        marks_old: dict[int, list[tuple[float, float, float, float]]] = {}
        marks_new: dict[int, list[tuple[float, float, float, float]]] = {}
        row_ins: dict[int, set[int]] = {}
        row_del: dict[int, set[int]] = {}
        for index, change in enumerate(raw_changes):
            change["rects_old"] = merge_rects(change["old_words"])
            change["rects_new"] = merge_rects(change["new_words"])
            for page, rect in change["rects_old"]:
                marks_old.setdefault(page, []).append(rect)
                row_del.setdefault(row_of_old[page], set()).add(index)
            for page, rect in change["rects_new"]:
                marks_new.setdefault(page, []).append(rect)
                row_ins.setdefault(row_of_new[page], set()).add(index)
            anchor_row = (row_of_old.get(change["page_old"]) if change["old_words"]
                          else row_of_new.get(change["page_new"]))
            change["row"] = anchor_row if anchor_row is not None else 0

        # Visual differences per row.
        visual: dict[int, dict[str, Any]] = {}
        if use_visual:
            for r, (i, j) in enumerate(rows):
                if i is None or j is None:
                    continue
                regions, fraction = visual_regions(old_doc[i], new_doc[j], args.dpi)
                if use_text and regions:
                    # Text changes are already highlighted; keep only the
                    # regions that are mostly something else (images, art).
                    covered = [_to_display(old_doc[i], rect) + (-3, -3, 3, 3) for rect in marks_old.get(i, [])]
                    covered += [_to_display(new_doc[j], rect) + (-3, -3, 3, 3) for rect in marks_new.get(j, [])]
                    regions = [region for region in regions
                               if _coverage(pymupdf.Rect(region), covered) < 0.5]
                if regions:
                    visual[r] = {"regions": regions, "fraction": fraction}

        insertions = sum(1 for c in raw_changes if c["new_words"])
        deletions = sum(1 for c in raw_changes if c["old_words"])
        unpaired = {r for r, (i, j) in enumerate(rows) if i is None or j is None}
        changed_rows = sorted(set(row_ins) | set(row_del) | set(visual) | unpaired)
        identical = not raw_changes and not visual and not unpaired and (text_found or use_visual)

        # Assemble the report: side-by-side rows first, summary inserted at the front.
        report = pymupdf.open()
        for r, (i, j) in enumerate(rows):
            old_rect = old_doc[i].rect if i is not None else None
            new_rect = new_doc[j].rect if j is not None else None
            left_size = old_rect or new_rect
            right_size = new_rect or old_rect
            assert left_size is not None and right_size is not None
            scale = min(1.0, 1100.0 / max(left_size.height, right_size.height))
            lw, lh = left_size.width * scale, left_size.height * scale
            rw, rh = right_size.width * scale, right_size.height * scale
            width = MARGIN + lw + GUTTER + rw + MARGIN
            height = HEADER + max(lh, rh) + MARGIN
            page = report.new_page(width=width, height=height)
            page.draw_rect(page.rect, color=None, fill=CANVAS, overlay=True)
            left = pymupdf.Rect(MARGIN, HEADER, MARGIN + lw, HEADER + lh)
            right = pymupdf.Rect(MARGIN + lw + GUTTER, HEADER, MARGIN + lw + GUTTER + rw, HEADER + rh)
            draw_side(page, left, old_doc, i,
                      f"Original · page {i + 1} of {old_doc.page_count}" if i is not None else "Original",
                      "No matching page\n(added in the revision)")
            draw_side(page, right, new_doc, j,
                      f"Revised · page {j + 1} of {new_doc.page_count}" if j is not None else "Revised",
                      "No matching page\n(removed from the original)")

            def place(src_page: Any, target: Any, rect: tuple[float, float, float, float], rotated: bool = False) -> Any:
                shown = pymupdf.Rect(rect) if rotated else _to_display(src_page, rect)
                factor = target.width / src_page.rect.width
                return pymupdf.Rect(target.x0 + shown.x0 * factor, target.y0 + shown.y0 * factor,
                                    target.x0 + shown.x1 * factor, target.y0 + shown.y1 * factor)

            if i is not None:
                for rect in marks_old.get(i, []):
                    box = place(old_doc[i], left, rect) + (-1, -1, 1, 1)
                    page.draw_rect(box, color=RED, fill=RED, fill_opacity=0.28, stroke_opacity=0.65, width=0.5, overlay=True)
            if j is not None:
                for rect in marks_new.get(j, []):
                    box = place(new_doc[j], right, rect) + (-1, -1, 1, 1)
                    page.draw_rect(box, color=GREEN, fill=GREEN, fill_opacity=0.26, stroke_opacity=0.65, width=0.5, overlay=True)
            if r in visual and i is not None and j is not None:
                for region in visual[r]["regions"]:
                    for src, target in ((old_doc[i], left), (new_doc[j], right)):
                        box = place(src, target, region, rotated=True) & target
                        if not box.is_empty:
                            page.draw_rect(box, color=AMBER, width=1.3, dashes="[4 2] 0", overlay=True)

            # Header badges (right side).
            x = width - MARGIN
            ins_count, del_count = len(row_ins.get(r, ())), len(row_del.get(r, ()))
            if r in visual:
                x = badge(page, x, MARGIN + 5, f"{len(visual[r]['regions'])} visual", AMBER)
            if ins_count:
                x = badge(page, x, MARGIN + 5, f"{ins_count} inserted", GREEN)
            if del_count:
                x = badge(page, x, MARGIN + 5, f"{del_count} deleted", RED)
            if not (ins_count or del_count or r in visual or r in unpaired):
                badge(page, x, MARGIN + 5, "No changes", MUTED)

        # Public change records (1-based pages).
        public: list[dict[str, Any]] = []
        for change in raw_changes:
            record = {
                "type": change["type"],
                "text": snippet(change["new_words"] if change["new_words"] else change["old_words"]),
                "page_old": change["page_old"] + 1 if old_doc.page_count else None,
                "page_new": change["page_new"] + 1 if new_doc.page_count else None,
                "row": change["row"],
            }
            if change["type"] == "replace":
                record["old_text"] = snippet(change["old_words"])
            public.append(record)

        summary_size = (842.0, 595.0)  # A4 landscape
        info = {
            "old_name": old_label, "new_name": new_label,
            "pages_old": old_doc.page_count, "pages_new": new_doc.page_count,
            "insertions": insertions, "deletions": deletions,
            "changed_rows": changed_rows, "mode": mode, "identical": identical,
            "text_found": text_found,
            "visual_regions": sum(len(v["regions"]) for v in visual.values()),
            "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        summary, positions = build_summary(info, public, *summary_size)
        summary_pages = summary.page_count
        report.insert_pdf(summary, start_at=0)
        summary.close()
        for spage, rect, index in positions:
            if 0 <= spage < summary_pages and index < len(public):
                target = summary_pages + public[index]["row"]
                report[spage].insert_link({"kind": pymupdf.LINK_GOTO, "page": target, "from": rect,
                                           "to": pymupdf.Point(0, 0)})
        try:
            report.subset_fonts()
        except Exception:
            pass
        convert_helpers().normalize_tounicode(report)
        report.set_metadata({"title": f"Comparison: {old_label} vs {new_label}",
                             "creator": "PDF Studio", "producer": f"PDF Studio engine {api.version}"})
        report.set_toc([[1, "Summary", 1]] + [
            [1, f"Original p.{i + 1 if i is not None else '–'} · Revised p.{j + 1 if j is not None else '–'}",
             summary_pages + r + 1]
            for r, (i, j) in enumerate(rows) if r in changed_rows
        ])
        output = Path(args.output)
        api.ensure_parent(output)
        report.save(str(output), garbage=3, deflate=True)
        report_pages = report.page_count
        report.close()

        for record in public:
            record["report_page"] = summary_pages + record.pop("row") + 1
        changed_pages = []
        for r in changed_rows:
            i, j = rows[r]
            changed_pages.append({
                "report_page": summary_pages + r + 1,
                "page_old": i + 1 if i is not None else None,
                "page_new": j + 1 if j is not None else None,
                "insertions": len(row_ins.get(r, ())),
                "deletions": len(row_del.get(r, ())),
                "visual_regions": len(visual.get(r, {}).get("regions", [])),
            })
        payload = {
            "output": str(output),
            "mode": mode,
            "pages_old": old_doc.page_count,
            "pages_new": new_doc.page_count,
            "insertions": insertions,
            "deletions": deletions,
            "words_inserted": sum(len(c["new_words"]) for c in raw_changes),
            "words_deleted": sum(len(c["old_words"]) for c in raw_changes),
            "identical": identical,
            "text_found": text_found,
            "changed_pages": changed_pages,
            "changed_pages_old": sorted({i + 1 for r in changed_rows for i in [rows[r][0]] if i is not None}),
            "changed_pages_new": sorted({j + 1 for r in changed_rows for j in [rows[r][1]] if j is not None}),
            "summary_pages": summary_pages,
            "report_pages": report_pages,
            "total_changes": len(public),
            "truncated": len(public) > args.max_changes,
            "changes": public[: args.max_changes],
        }
        if args.json:
            json_path = Path(args.json)
            api.ensure_parent(json_path)
            full = dict(payload)
            full["changes"] = []
            for record, change in zip(public, raw_changes):
                item = dict(record)
                item["text"] = join_tokens(change["new_words"] or change["old_words"])
                if change["type"] == "replace":
                    item["old_text"] = join_tokens(change["old_words"])
                item["rects_old"] = [[page + 1, *[round(v, 2) for v in rect]] for page, rect in change["rects_old"]]
                item["rects_new"] = [[page + 1, *[round(v, 2) for v in rect]] for page, rect in change["rects_new"]]
                full["changes"].append(item)
            full["visual"] = [
                {"report_page": summary_pages + r + 1, "page_old": rows[r][0] + 1, "page_new": rows[r][1] + 1,
                 "changed_fraction": round(v["fraction"], 5),
                 "regions": [[round(c, 2) for c in region] for region in v["regions"]]}
                for r, v in sorted(visual.items())
            ]
            full["pairs"] = [{"page_old": i + 1 if i is not None else None,
                              "page_new": j + 1 if j is not None else None,
                              "report_page": summary_pages + r + 1} for r, (i, j) in enumerate(rows)]
            full["truncated"] = False
            json_path.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
            payload["json"] = str(json_path)
        old_doc.close()
        new_doc.close()
        return api.respond(True, **payload)

    parser = subparsers.add_parser("compare", help="Side-by-side comparison report of two PDFs")
    parser.add_argument("--old", required=True, help="original PDF")
    parser.add_argument("--new", required=True, help="revised PDF")
    parser.add_argument("--output", required=True, help="report PDF")
    parser.add_argument("--json", help="also write every change (with boxes) here")
    parser.add_argument("--mode", choices=("text", "visual", "both"), default="text")
    parser.add_argument("--max-changes", type=int, default=200, help="cap for the changes list in the reply")
    parser.add_argument("--dpi", type=int, default=100, help="render resolution for visual mode")
    parser.add_argument("--ignore-case", action="store_true")
    parser.add_argument("--old-label", help="display name for the original (default: file name)")
    parser.add_argument("--new-label", help="display name for the revision (default: file name)")
    parser.set_defaults(func=cmd_compare)
