"""Right-to-left (Arabic/Hebrew) text layout and PDF emission for the engine.

PyMuPDF's insert_text/insert_textbox map characters to glyphs one by one: no
Arabic shaping, no bidi. insert_htmlbox shapes correctly but writes a
ToUnicode CMap that maps shaped glyphs to presentation forms (or garbage for
fonts such as SF Arabic / Geeza Pro), and lays mixed lines out in an order
PDFKit reads backwards. This module does the layout itself:

  * a compact Unicode bidi resolver (UAX #9 W1-W7, N1-N2, I1-I2, L1-L2,
    no explicit embeddings) orders mixed Arabic + Latin + digits;
  * HarfBuzz (uharfbuzz) shapes each directional run with the real font;
  * glyphs are written straight into a content stream, in visual order, with
    an Identity-H font whose ToUnicode maps every glyph to its LOGICAL
    characters (initial/medial/final forms -> base letter, lam-alef -> two
    letters); PDFKit's own bidi pass turns that into the logical string;
  * lines MuPDF, poppler or Acrobat would scramble (digits, Latin words,
    punctuation, ligatures next to Arabic) also carry their logical text as
    /ActualText, so copy and extraction give the logical string there too
    (see GlyphWriter; redaction drops it first, see strip_actual_text).

Files starting with "_" are skipped by the extension loader; pdf_engine.py
and ext/ocr_layer.py import this module directly.

Coordinates: every public function takes ENGINE coordinates -- the unrotated
page (crop box) with a top-left origin, i.e. exactly what page.get_text()
reports and what the app sends for rects/points.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

ZWJ = "\u200d"

# fontTools chatters about harmless quirks of Apple's fonts on stderr.
import logging as _logging

_logging.getLogger("fontTools").setLevel(_logging.ERROR)

_RTL_RE = re.compile("[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")
_PRESENTATION_RE = re.compile("[\ufb50-\ufdff\ufe70-\ufefc]+")

FONT_CACHE_DIR = os.path.expanduser("~/Library/Caches/PDFStudio/fonts")

# Arabic-capable faces in preference order: (path, collection index, bold).
# SF Arabic is deliberately last: it decomposes letters into a dotless body
# plus separate dot glyphs, which no per-glyph ToUnicode can describe.
_ARABIC_FACES = [
    ("/System/Library/Fonts/GeezaPro.ttc", 0, False),
    ("/System/Library/Fonts/GeezaPro.ttc", 1, True),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", 0, False),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0, True),
    ("/System/Library/Fonts/Supplemental/Tahoma.ttf", 0, False),
    ("/System/Library/Fonts/Supplemental/Tahoma Bold.ttf", 0, True),
    ("/System/Library/Fonts/Supplemental/Times New Roman.ttf", 0, False),
    ("/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf", 0, True),
    ("/System/Library/Fonts/SFArabic.ttf", 0, False),
]

# Broad-coverage faces for the invisible OCR layer (Latin, Cyrillic, Greek,
# CJK, Thai, ...). Tried after the Arabic faces when a line is not covered.
_FALLBACK_FACES = [
    ("/System/Library/Fonts/Helvetica.ttc", 0, False),
    ("/System/Library/Fonts/SFNS.ttf", 0, False),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0, False),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, False),
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 0, False),
    ("/System/Library/Fonts/Supplemental/Ayuthaya.ttf", 0, False),
]


# --------------------------------------------------------------------------
# Text classification
# --------------------------------------------------------------------------

def has_rtl(text: str | None) -> bool:
    """True when the text contains any Hebrew/Arabic character."""
    return bool(text) and bool(_RTL_RE.search(text))


def paragraph_is_rtl(text: str) -> bool:
    """Paragraph direction: RTL when right-to-left strong characters are the
    majority (ties go to the first strong character, as in UAX #9 P2)."""
    rtl = ltr = 0
    first = None
    for ch in text:
        cls = unicodedata.bidirectional(ch)
        if cls in ("R", "AL"):
            rtl += 1
            first = first or "R"
        elif cls == "L":
            ltr += 1
            first = first or "L"
    if rtl == ltr:
        if first is None:
            # No strong letters at all ("١٨٪", "2026"): Arabic-Indic digits
            # and Arabic punctuation still belong to an RTL paragraph.
            return has_rtl(text)
        return first == "R"
    return rtl > ltr


def strong_counts(text: str) -> tuple[int, int]:
    """(right-to-left, left-to-right) strong character counts."""
    rtl = ltr = 0
    for ch in text:
        cls = unicodedata.bidirectional(ch)
        if cls in ("R", "AL"):
            rtl += 1
        elif cls == "L":
            ltr += 1
    return rtl, ltr


def paragraph_direction(text: str, fallback: bool) -> bool:
    """RTL/LTR from a clear (2:1) majority of strong letters, else `fallback`."""
    rtl, ltr = strong_counts(text)
    if rtl >= 2 * ltr and rtl:
        return True
    if ltr >= 2 * rtl and ltr:
        return False
    if not rtl and not ltr:
        return has_rtl(text) or fallback
    return fallback


def normalize_extracted(text: str) -> str:
    """Fold Arabic presentation forms (what many PDFs' ToUnicode maps shaped
    glyphs to) back to base letters so edited text is the logical string."""
    if not text or not _PRESENTATION_RE.search(text):
        return text
    return _PRESENTATION_RE.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), text)


def visual_to_logical(visual: str, base_rtl: bool | None = None) -> str:
    """Logical order for a string given in VISUAL (left-to-right) order.
    Running the bidi reordering over the visual sequence undoes it for
    ordinary text (RTL paragraphs with embedded Latin words and numbers)."""
    if not has_rtl(visual):
        return visual
    rtl = paragraph_is_rtl(visual) if base_rtl is None else base_rtl
    candidates = []
    if rtl:
        # Reverse the row, then flip back the left-to-right islands (numbers,
        # Latin words) that the reversal turned around. Classes survive the
        # reversal, so "%10" next to Arabic resolves exactly like "10%".
        mirrored = visual[::-1]
        levels = _resolve_levels(mirrored, 1)
        out, i = [], 0
        while i < len(mirrored):
            j = i
            while j < len(mirrored) and (levels[j] >= 2) == (levels[i] >= 2):
                j += 1
            piece = mirrored[i:j]
            out.append(piece[::-1] if levels[i] >= 2 else piece)
            i = j
        candidates.append("".join(out))
    out = []
    for run in visual_runs(visual, rtl):
        piece = visual[run.start:run.end]
        out.append(piece[::-1] if run.rtl else piece)
    candidates.append("".join(out))
    for candidate in candidates:
        if logical_to_visual(candidate, rtl) == visual:
            return candidate
    return candidates[0]


def logical_to_visual(text: str, base_rtl: bool | None = None) -> str:
    rtl = paragraph_is_rtl(text) if base_rtl is None else base_rtl
    return "".join(
        text[run.start:run.end][::-1] if run.rtl else text[run.start:run.end]
        for run in visual_runs(text, rtl)
    )


def _needs_reorder(text: str) -> bool:
    """Every row holding RTL text is rebuilt from glyph positions: MuPDF's
    order is only right for pure RTL letters (digits, Latin words and even a
    trailing full stop come out misplaced)."""
    return has_rtl(text)


def logical_text_from_chars(chars: list[dict[str, Any]], base_rtl: bool | None = None, force_visual: bool = False) -> str:
    """Logical text for one visual row from PyMuPDF rawdict chars (each with
    "c", "origin" and "bbox"). Rows with RTL text are rebuilt from glyph
    positions (MuPDF's own order breaks on digits, Latin words and edge
    punctuation) and run back through the bidi algorithm. Presentation forms
    fold to base letters."""
    text = "".join(ch.get("c", "") for ch in chars)
    if force_visual or _needs_reorder(text):
        text = visual_to_logical(_visual_string(chars), base_rtl)
    # Word processors and MuPDF's HTML layout often map the space glyph of
    # Arabic fonts to NO-BREAK SPACE; in RTL rows that is just a space.
    return normalize_extracted(text).replace("\u00a0", " ")


def _visual_string(chars: list[dict[str, Any]]) -> str:
    """Characters in left-to-right visual order. Zero-width glyphs (Arabic
    marks, the ligature tail markers earlier builds wrote; ligatures now come
    as multi-character glyphs, see prepare_glyph_chars) attach to the glyph
    they sit on: before it
    when that glyph is RTL (so the run reversal puts them after it), after it
    otherwise. Multi-character glyphs (lam-alef) are flipped to visual order."""
    def x0(ch: dict[str, Any]) -> float:
        return float((ch.get("origin") or ch.get("bbox") or (0,))[0])

    def width(ch: dict[str, Any]) -> float:
        bbox = ch.get("bbox") or (0, 0, 0, 0)
        return float(bbox[2]) - float(bbox[0])

    wide = sorted((i for i, ch in enumerate(chars) if width(ch) >= 0.01), key=lambda i: (round(x0(chars[i]), 2), i))
    attached: dict[int, list[int]] = {i: [] for i in wide}
    loose: list[int] = []
    for i, ch in enumerate(chars):
        if width(ch) >= 0.01:
            continue
        x = x0(ch)
        host = None
        best = None
        for j in wide:
            box = chars[j].get("bbox") or (0, 0, 0, 0)
            rtl_host = has_rtl(chars[j].get("c", ""))
            # Ligature tail markers sit exactly on the glyph's left edge (RTL)
            # or right edge (LTR); marks sit inside the glyph's box.
            if (rtl_host and abs(x - box[0]) < 0.05) or (not rtl_host and abs(x - box[2]) < 0.05):
                score = -1.0
            elif box[0] + 0.05 < x < box[2] - 0.05:
                score = abs(x - (box[0] + box[2]) / 2)
            else:
                continue
            if best is None or score < best:
                host, best = j, score
        if host is None:
            loose.append(i)
        else:
            attached[host].append(i)

    order: list[int] = []
    for j in wide:
        marks = attached[j]
        if has_rtl(chars[j].get("c", "")):
            order.extend(marks)
            order.append(j)
        else:
            order.append(j)
            order.extend(marks)
    for i in loose:  # stray zero-width glyphs: by position
        x = x0(chars[i])
        position = next((k for k, j in enumerate(order) if x0(chars[j]) > x), len(order))
        order.insert(position, i)

    pieces = []
    for i in order:
        c = chars[i].get("c", "")
        pieces.append(c[::-1] if len(c) > 1 and has_rtl(c) else c)
    return "".join(pieces)


# --------------------------------------------------------------------------
# Bidi (UAX #9 without explicit embeddings/isolates)
# --------------------------------------------------------------------------

def _resolve_levels(text: str, base_level: int) -> list[int]:
    n = len(text)
    if n == 0:
        return []
    types = [unicodedata.bidirectional(ch) or "ON" for ch in text]
    # Explicit formatting characters are treated as boundary neutrals.
    types = ["BN" if t in ("LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI") else t for t in types]
    sor = "R" if base_level % 2 else "L"

    # W1: NSM takes the type of the previous character.
    prev = sor
    for i, t in enumerate(types):
        if t == "NSM":
            types[i] = prev
        elif t != "BN":
            prev = t
    # W2: EN after AL becomes AN.  W3: AL -> R.
    last_strong = sor
    for i, t in enumerate(types):
        if t in ("L", "R", "AL"):
            last_strong = t
        elif t == "EN" and last_strong == "AL":
            types[i] = "AN"
    types = ["R" if t == "AL" else t for t in types]
    # W4: single separators between numbers.
    for i in range(1, n - 1):
        if types[i] == "ES" and types[i - 1] == "EN" and types[i + 1] == "EN":
            types[i] = "EN"
        elif types[i] == "CS" and types[i - 1] == types[i + 1] and types[i - 1] in ("EN", "AN"):
            types[i] = types[i - 1]
    # W5: ET sequences adjacent to EN become EN.
    i = 0
    while i < n:
        if types[i] == "ET":
            j = i
            while j < n and types[j] == "ET":
                j += 1
            if (i > 0 and types[i - 1] == "EN") or (j < n and types[j] == "EN"):
                for k in range(i, j):
                    types[k] = "EN"
            i = j
        else:
            i += 1
    # W6: remaining separators/terminators are neutral.
    types = ["ON" if t in ("ES", "ET", "CS") else t for t in types]
    # W7: EN after L (or L sor) is L.
    last_strong = sor
    for i, t in enumerate(types):
        if t in ("L", "R"):
            last_strong = t
        elif t == "EN" and last_strong == "L":
            types[i] = "L"
    # N1/N2: neutral runs take the surrounding direction when both sides
    # agree (numbers count as R), else the embedding direction.
    neutral = ("B", "S", "WS", "ON", "BN")
    i = 0
    while i < n:
        if types[i] in neutral:
            j = i
            while j < n and types[j] in neutral:
                j += 1
            before = sor if i == 0 else ("R" if types[i - 1] in ("R", "EN", "AN") else "L")
            after = sor if j == n else ("R" if types[j] in ("R", "EN", "AN") else "L")
            resolved = before if before == after else sor
            for k in range(i, j):
                types[k] = resolved
            i = j
        else:
            i += 1
    # I1/I2: implicit levels.
    levels = []
    for t in types:
        if base_level % 2 == 0:
            levels.append(base_level + (1 if t == "R" else 2 if t in ("AN", "EN") else 0))
        else:
            levels.append(base_level + (1 if t in ("L", "EN", "AN") else 0))
    # L1: trailing whitespace returns to the paragraph level.
    k = n - 1
    while k >= 0 and unicodedata.bidirectional(text[k]) in ("WS", "S", "B", "BN"):
        levels[k] = base_level
        k -= 1
    return levels


@dataclass
class _Run:
    start: int
    end: int
    level: int

    @property
    def rtl(self) -> bool:
        return self.level % 2 == 1


def visual_runs(text: str, base_rtl: bool) -> list[_Run]:
    """Directional runs of `text` in VISUAL (left-to-right) order."""
    levels = _resolve_levels(text, 1 if base_rtl else 0)
    runs: list[_Run] = []
    for i, level in enumerate(levels):
        if runs and runs[-1].level == level:
            runs[-1].end = i + 1
        else:
            runs.append(_Run(i, i + 1, level))
    if not runs:
        return runs
    # L2: from the highest level down to the lowest odd level, reverse every
    # maximal sequence of runs at that level or higher.
    highest = max(r.level for r in runs)
    lowest_odd = min((r.level for r in runs if r.level % 2), default=highest + 1)
    for level in range(highest, lowest_odd - 1, -1):
        i = 0
        while i < len(runs):
            if runs[i].level >= level:
                j = i
                while j < len(runs) and runs[j].level >= level:
                    j += 1
                runs[i:j] = runs[i:j][::-1]
                i = j
            else:
                i += 1
    return runs


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

def _font_cache_path(path: str, index: int) -> str:
    stat = os.stat(path)
    key = hashlib.sha1(f"{path}|{index}|{stat.st_size}|{int(stat.st_mtime)}".encode()).hexdigest()[:16]
    return os.path.join(FONT_CACHE_DIR, f"{key}.ttf")


def _single_face_file(path: str, index: int) -> str:
    """A standalone .ttf for one face of a collection (cached)."""
    if not path.lower().endswith((".ttc", ".otc")):
        return path
    target = _font_cache_path(path, index)
    if os.path.exists(target):
        return target
    from fontTools.ttLib import TTFont

    os.makedirs(FONT_CACHE_DIR, exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp"
    TTFont(path, fontNumber=index).save(tmp)
    os.replace(tmp, target)
    return target


_FONT_MEMO: dict[tuple[str, int], "ShapingFont"] = {}


class ShapingFont:
    """One font face: cmap, metrics, HarfBuzz shaping and glyph->text map."""

    def __init__(self, path: str | None = None, index: int = 0, buffer: bytes | None = None, label: str = ""):
        import uharfbuzz as hb
        from fontTools.ttLib import TTFont

        if buffer is None:
            assert path is not None
            self.file = _single_face_file(path, index)
            with open(self.file, "rb") as fh:
                buffer = fh.read()
        else:
            self.file = None
        self.buffer = buffer
        self.label = label or (os.path.basename(path) if path else "embedded")
        self.tt = TTFont(io.BytesIO(buffer), lazy=True)
        self.cmap: dict[int, str] = self.tt.getBestCmap() or {}
        order = self.tt.getGlyphOrder()
        self._gid_of_name = {name: gid for gid, name in enumerate(order)}
        self.upem = float(self.tt["head"].unitsPerEm)
        hhea = self.tt["hhea"]
        self.ascender = hhea.ascent / self.upem
        self.descender = hhea.descent / self.upem  # negative
        if self.ascender <= 0:
            self.ascender, self.descender = 0.9, -0.25
        face = hb.Face(hb.Blob(buffer), 0)
        self._hb_font = hb.Font(face)
        self._hb = hb
        self._gid_text: dict[int, str] | None = None

    @classmethod
    def system(cls, path: str, index: int = 0) -> "ShapingFont":
        key = (path, index)
        if key not in _FONT_MEMO:
            _FONT_MEMO[key] = cls(path, index)
        return _FONT_MEMO[key]

    @property
    def ps_name(self) -> str:
        name = self.tt["name"].getDebugName(6) or self.tt["name"].getDebugName(4) or self.label
        return re.sub(r"[^A-Za-z0-9\-]", "", name.lstrip(".")) or "PDFStudioFont"

    def advance(self, gid: int) -> float:
        """Advance in em units, rounded the way the PDF /W array stores it."""
        if not hasattr(self, "_advances"):
            hmtx = self.tt["hmtx"].metrics
            order = self.tt.getGlyphOrder()
            self._advances = [round(hmtx[name][0] * 1000.0 / self.upem) / 1000.0 for name in order]
        return self._advances[gid] if 0 <= gid < len(self._advances) else 0.0

    # -- coverage ---------------------------------------------------------
    def nominal_gid(self, ch: str) -> int:
        name = self.cmap.get(ord(ch))
        return self._gid_of_name.get(name, 0) if name else 0

    def covers(self, text: str) -> bool:
        for ch in text:
            if ch in "\n\r\t" or unicodedata.category(ch) in ("Cc", "Cf"):
                continue
            if ord(ch) not in self.cmap:
                return False
        return True

    # -- shaping ------------------------------------------------------------
    def shape(self, text: str, rtl: bool) -> list[tuple[int, float, float, float, int]]:
        """Glyphs in visual order: (gid, advance, x_offset, y_offset, cluster)
        with metrics in em units and clusters as indices into `text`."""
        hb = self._hb
        buf = hb.Buffer()
        buf.add_str(text)
        buf.direction = "rtl" if rtl else "ltr"
        buf.guess_segment_properties()
        # Marks keep their own clusters; only true ligatures merge characters.
        buf.cluster_level = hb.BufferClusterLevel.MONOTONE_CHARACTERS
        hb.shape(self._hb_font, buf, {})
        out = []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            out.append((info.codepoint, pos.x_advance / self.upem, pos.x_offset / self.upem,
                        pos.y_offset / self.upem, info.cluster))
        return out

    def joins_arabic(self) -> bool:
        """True when the font really shapes Arabic: joined forms differ from
        the isolated glyph and no letter splits into several glyphs."""
        sample = "ببب"  # beh beh beh -> initial, medial, final
        if not self.covers(sample):
            return False
        glyphs = self.shape(sample, True)
        if len(glyphs) != 3:
            return False
        isolated = self.nominal_gid("ب")
        return any(g[0] != isolated for g in glyphs)

    def gid_text(self) -> dict[int, str]:
        """Glyph id -> logical text for every glyph shaping can produce:
        nominal cmap glyphs, contextual Arabic forms (shaped in isolated,
        initial, medial and final contexts) and lam-alef ligatures."""
        if self._gid_text is not None:
            return self._gid_text
        mapping: dict[int, str] = {}

        def is_presentation(cp: int) -> bool:
            return 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF

        arabic = sorted(cp for cp in self.cmap if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0x08A0 <= cp <= 0x08FF)
        # Base characters first (standard Arabic before Persian look-alikes,
        # so a glyph shared by yeh U+064A and farsi yeh U+06CC reads U+064A).
        for cp in sorted(self.cmap, key=lambda c: (is_presentation(c), c == 0xA0, c)):
            gid = self._gid_of_name.get(self.cmap[cp])
            if gid is None or gid in mapping:
                continue
            if is_presentation(cp):
                continue  # resolved through shaping below, else fall back
            mapping[gid] = " " if cp == 0xA0 else chr(cp)
        for cp in arabic:
            ch = chr(cp)
            for pre, post in (("", ""), (ZWJ, ""), ("", ZWJ), (ZWJ, ZWJ)):
                mine = [g[0] for g in self.shape(pre + ch + post, True) if g[4] == len(pre)]
                if len(mine) == 1 and mine[0] not in mapping:
                    mapping[mine[0]] = ch
        lam = "ل"
        for alef in "اآأإ":
            for pre in ("", ZWJ):
                glyphs = self.shape(pre + lam + alef, True)
                mine = [g[0] for g in glyphs if g[4] == len(pre)]
                if len(mine) == 1 and not any(g[4] == len(pre) + 1 for g in glyphs):
                    mapping[mine[0]] = lam + alef
        # Other two-letter ligatures (lam-meem, beh-meem, ... in Geeza Pro and
        # other traditional fonts), in every joining context.
        letters = [chr(cp) for cp in range(0x0628, 0x064B) if cp in self.cmap and unicodedata.category(chr(cp)) == "Lo"]
        for first in letters:
            for second in letters:
                for pre, post in (("", ""), (ZWJ, ""), ("", ZWJ), (ZWJ, ZWJ)):
                    glyphs = self.shape(pre + first + second + post, True)
                    mine = [g[0] for g in glyphs if g[4] == len(pre)]
                    if len(mine) == 1 and mine[0] not in mapping and not any(g[4] == len(pre) + 1 for g in glyphs):
                        mapping[mine[0]] = first + second
        for cp in self.cmap:  # presentation forms nothing else claimed
            gid = self._gid_of_name.get(self.cmap[cp])
            if gid is not None and gid not in mapping and is_presentation(cp):
                mapping[gid] = unicodedata.normalize("NFKC", chr(cp))
        self._gid_text = mapping
        return mapping

    def subset_buffer(self, gids: Iterable[int]) -> bytes:
        """A small TrueType subset keeping the given glyph ids unchanged."""
        from fontTools import subset

        options = subset.Options()
        options.retain_gids = True
        options.notdef_outline = True
        options.layout_features = []
        options.name_IDs = ["*"]
        options.drop_tables += ["morx", "mort", "kerx", "feat", "just", "Zapf", "meta", "trak", "GSUB", "GPOS", "GDEF", "kern"]
        from fontTools.ttLib import TTFont

        font = TTFont(io.BytesIO(self.buffer))
        subsetter = subset.Subsetter(options)
        subsetter.populate(gids=sorted(set(gids) | {0}))
        subsetter.subset(font)
        out = io.BytesIO()
        font.save(out)
        return out.getvalue()


def _face_list(bold: bool, faces: list[tuple[str, int, bool]]) -> list[tuple[str, int]]:
    preferred = [(p, i) for p, i, b in faces if b == bold and os.path.exists(p)]
    others = [(p, i) for p, i, b in faces if b != bold and os.path.exists(p)]
    return preferred + others


def arabic_font_for(text: str, bold: bool = False, family_hint: str | None = None) -> ShapingFont | None:
    """Best installed Arabic-capable font covering `text`: the requested
    family first (when it has working Arabic shaping), then Geeza Pro,
    Arial, Tahoma, Times New Roman, SF Arabic."""
    candidates: list[tuple[str, int]] = []
    if family_hint:
        hinted = _family_files(family_hint, bold)
        candidates.extend((p, 0) for p in hinted)
    candidates.extend(_face_list(bold, _ARABIC_FACES))
    seen = set()
    for path, index in candidates:
        if (path, index) in seen:
            continue
        seen.add((path, index))
        try:
            font = ShapingFont.system(path, index)
        except Exception:
            continue
        if font.covers(text) and (not has_rtl(text) or font.joins_arabic()):
            return font
    return None


def coverage_font_for(text: str) -> ShapingFont | None:
    """Any installed font covering every character (OCR layer: shaping is
    irrelevant there, only coverage)."""
    faces = _face_list(False, _ARABIC_FACES) if has_rtl(text) else []
    faces += _face_list(False, _FALLBACK_FACES) + _face_list(False, _ARABIC_FACES)
    seen = set()
    for path, index in faces:
        if (path, index) in seen:
            continue
        seen.add((path, index))
        try:
            font = ShapingFont.system(path, index)
        except Exception:
            continue
        if font.covers(text):
            return font
    return None


def best_coverage_font(text: str) -> ShapingFont | None:
    """The installed candidate covering the most characters of `text`."""
    best, best_count = None, 0
    for path, index in _face_list(False, _ARABIC_FACES) + _face_list(False, _FALLBACK_FACES):
        try:
            font = ShapingFont.system(path, index)
        except Exception:
            continue
        count = sum(1 for ch in text if ord(ch) in font.cmap)
        if count > best_count:
            best, best_count = font, count
    return best


def _family_files(family: str, bold: bool) -> list[str]:
    """Installed font files for a family name ("Arial", "TimesNewRomanPSMT")."""
    cleaned = family
    if len(cleaned) > 7 and cleaned[6] == "+":
        cleaned = cleaned[7:]
    base = re.split(r"[-,]", cleaned)[0]
    base = re.sub(r"(MT|PS|PSMT|Std|Pro)$", "", base)
    compact = re.sub(r"[\s\-_]", "", base).lower()
    if not compact:
        return []
    wants_bold = bold or "bold" in cleaned.lower()
    found: list[tuple[int, str]] = []
    for directory in ("/System/Library/Fonts/Supplemental", "/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")):
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            stem, ext = os.path.splitext(entry)
            if ext.lower() not in (".ttf", ".otf"):
                continue
            compact_stem = re.sub(r"[\s\-_]", "", stem).lower()
            if not compact_stem.startswith(compact):
                continue
            rest = compact_stem[len(compact):]
            is_bold = "bold" in rest
            if "italic" in rest or "oblique" in rest:
                continue
            score = (0 if is_bold == wants_bold else 1) * 100 + len(rest)
            found.append((score, os.path.join(directory, entry)))
    return [path for _score, path in sorted(found)]


def embedded_font_for(doc: Any, page: Any, raw_name: str | None, text: str) -> ShapingFont | None:
    """The document's own embedded font when it covers `text` AND can shape
    it (subset fonts usually lost their cmap or GSUB, so this is rare)."""
    if not raw_name:
        return None

    def strip(name: str) -> str:
        return name[7:] if len(name) > 7 and name[6] == "+" else name

    target = strip(raw_name).lower()
    try:
        fonts = page.get_fonts(full=False)
    except Exception:
        return None
    for entry in fonts:
        xref, basefont = entry[0], entry[3]
        if not basefont or strip(basefont).lower() != target:
            continue
        try:
            _name, ext, _type, buffer = doc.extract_font(xref)
        except Exception:
            continue
        if not buffer or ext not in ("ttf", "otf"):
            continue
        try:
            font = ShapingFont(buffer=buffer, label=strip(basefont))
        except Exception:
            continue
        if font.covers(text) and font.joins_arabic():
            return font
    return None


def parse_tounicode(data: bytes) -> dict[int, str]:
    """Glyph/CID -> text from a ToUnicode CMap stream (bfchar and bfrange)."""
    text = data.decode("latin-1", "replace")
    out: dict[int, str] = {}

    def decode(hexstr: str) -> str:
        hexstr = re.sub(r"\s+", "", hexstr)
        if len(hexstr) % 4:
            try:
                return chr(int(hexstr, 16))  # some writers emit raw code points
            except (ValueError, OverflowError):
                return ""
        try:
            return bytes.fromhex(hexstr).decode("utf-16-be", "replace")
        except ValueError:
            return ""

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f\s]*)>", block):
            out[int(src, 16)] = decode(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f\s]+>)", block):
            start, end = int(lo, 16), int(hi, 16)
            if end - start > 0xFFFF:
                continue
            if rest.startswith("["):
                for k, dst in enumerate(re.findall(r"<([0-9A-Fa-f\s]*)>", rest)):
                    out[start + k] = decode(dst)
            else:
                base = decode(rest.strip("<>"))
                if len(base) == 1:
                    for k in range(end - start + 1):
                        out[start + k] = chr(ord(base) + k)
    return out


_REPAIR_MEMO: dict[tuple[str, int], Any] = {}


def broken_text_repairs(page: Any) -> dict[str, tuple[dict[str, str], dict[str, str]]]:
    """Fonts on the page whose ToUnicode has no entries for shaped Arabic
    glyphs (MuPDF's HTML layout writes such PDFs for AAT fonts like Geeza
    Pro, as do some word processors), so extraction yields junk. Returns
    {span font name: {junk char: text}}, rebuilt from the embedded font
    itself via HarfBuzz."""
    doc = page.parent
    repairs: dict[str, dict[str, str]] = {}
    try:
        fonts = page.get_fonts(full=True)
    except Exception:
        return repairs
    for entry in fonts:
        xref, ext, kind, basefont = entry[0], entry[1], entry[2], entry[3]
        if kind != "Type0" or ext not in ("ttf", "otf"):
            continue
        key = (doc.name or str(id(doc)), xref)
        if key not in _REPAIR_MEMO:
            _REPAIR_MEMO[key] = _font_repair(doc, xref, basefont)
        found = _REPAIR_MEMO[key]
        if found:
            names, remap = found
            for name in names:
                repairs[name] = remap
    return repairs


def repair_span_chars(chars: list[dict[str, Any]], maps: tuple[dict[str, str], dict[str, str]]) -> bool:
    """Apply a font repair to rawdict chars in place: definite junk first,
    then look-alike Latin characters sitting between Arabic letters."""
    remap, ambiguous = maps
    # Only spans that actually show the junk: several fonts can share a name
    # (an intact copy of the same family, our own edits), and their real
    # letters must never be rewritten.
    letters = [ch.get("c", "") for ch in chars if ch.get("c", "").strip()]
    junk = sum(1 for c in letters if c in remap and not has_rtl(c))
    if not letters or junk < max(1, 0.3 * len(letters)):
        return False
    for ch in chars:
        c = ch.get("c", "")
        if c in remap:
            ch["c"] = remap[c]
        elif c == "\x00":
            ch["c"] = ""
    for i, ch in enumerate(chars):
        c = ch.get("c", "")
        if c in ambiguous:
            before = chars[i - 1].get("c", "") if i > 0 else ""
            after = chars[i + 1].get("c", "") if i + 1 < len(chars) else ""
            if has_rtl(before) or has_rtl(after):
                ch["c"] = ambiguous[c]
    return True


def _font_repair(doc: Any, xref: int, basefont: str) -> Any:
    try:
        kind, value = doc.xref_get_key(xref, "ToUnicode")
        if kind != "xref":
            return None
        tounicode = parse_tounicode(doc.xref_stream(int(value.split()[0])) or b"")
        _name, _ext, _type, buffer = doc.extract_font(xref)
        if not buffer or not tounicode:
            return None
        font = ShapingFont(buffer=buffer, label=basefont)
    except Exception:
        return None
    if not font.joins_arabic():
        return None
    good = font.gid_text()
    # Shaped forms (initial/medial/final, ligatures) have no cmap entry, so
    # a writer that builds ToUnicode from the cmap leaves them out; MuPDF
    # then reports such a glyph as the character whose code point equals the
    # glyph id (U+0159 for glyph 345). Map those back through the font.
    legit = set(tounicode.values())
    remap: dict[str, str] = {}
    ambiguous: dict[str, str] = {}
    for gid, text in good.items():
        if gid in tounicode or gid < 32 or 0xD800 <= gid <= 0xDFFF:
            continue
        char = chr(gid)
        if char not in legit:
            remap[char] = text
        elif not has_rtl(char):
            # Also a real character of the font ("Í"): only trust the glyph
            # reading when the neighbours are Arabic (see repair_span_chars).
            ambiguous[char] = text
    if len(remap) < 5:
        return None
    plain = basefont[7:] if len(basefont) > 7 and basefont[6] == "+" else basefont
    names = {plain, plain.replace(" ", ""), font.ps_name, re.sub(r"[-\s]?Regular$", "", plain).replace(" ", "")}
    return names, (remap, ambiguous)


# --------------------------------------------------------------------------
# PyMuPDF text-dict post-processing (used by the engine's block extraction)
# --------------------------------------------------------------------------

def glyph_flags(kind: str) -> int:
    """PyMuPDF text flags for GEOMETRY-sensitive reads ("rawdict", "dict",
    "search"): the defaults plus TEXT_IGNORE_ACTUALTEXT. Lines written by
    GlyphWriter carry their logical text as /ActualText on one zero-width
    glyph, so with ActualText applied every character of such a line sits on
    that one point; the engine wants the real glyph boxes instead."""
    import fitz

    base = {"rawdict": fitz.TEXTFLAGS_RAWDICT, "dict": fitz.TEXTFLAGS_DICT, "search": fitz.TEXTFLAGS_SEARCH}[kind]
    return base | fitz.TEXT_IGNORE_ACTUALTEXT


def prepare_glyph_chars(page: Any, data: dict[str, Any]) -> None:
    """Normalise a rawdict read with glyph_flags("rawdict") in place:

      * a glyph whose ToUnicode holds several characters (a ligature) comes
        back from MuPDF as the first character on the glyph plus zero-width
        characters at its right edge; put the whole logical text back on the
        glyph (logical_text_from_chars flips multi-character RTL glyphs);
      * GlyphWriter's ActualText carriers (lone narrow spaces) and other
        zero-width spaces are dropped.
    The trailing characters are found through get_texttrace(), which reports
    them in stream order with glyph id -1 right after their glyph."""
    ligatures: dict[tuple[float, float, str], str] = {}
    try:
        trace = page.get_texttrace()
    except Exception:
        trace = []
    carriers: set[tuple[float, float]] = set()
    for span in trace:
        chars = span.get("chars") or ()
        if len(chars) == 1 and chars[0][0] == 32 and chars[0][1] > 255:
            # GlyphWriter's ActualText carrier: a lone, very narrow space
            # drawn with the font's last (otherwise unused) glyph id.
            box = chars[0][3]
            if box[2] - box[0] < 0.12 * float(span.get("size") or 0):
                carriers.add((round(chars[0][2][0], 1), round(chars[0][2][1], 1)))
        k = 0
        while k < len(chars):
            unicode, gid, origin = chars[k][0], chars[k][1], chars[k][2]
            j = k + 1
            while j < len(chars) and chars[j][1] == -1:
                j += 1
            if gid >= 0 and j > k + 1:
                text = "".join(chr(c[0]) for c in chars[k:j])
                ligatures[(round(origin[0], 1), round(origin[1], 1), chr(unicode))] = text
            k = j
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                chars = span.get("chars")
                if not chars:
                    continue
                drop: set[int] = set()
                for i, ch in enumerate(chars):
                    box = ch.get("bbox") or (0, 0, 0, 0)
                    origin = ch.get("origin") or (box[0], box[3])
                    if ch.get("c") == " " and (round(origin[0], 1), round(origin[1], 1)) in carriers:
                        drop.add(i)
                        continue
                    if box[2] - box[0] < 0.01:
                        if ch.get("c") == " ":
                            drop.add(i)
                        continue
                    text = ligatures.get((round(origin[0], 1), round(origin[1], 1), ch.get("c", "")))
                    if not text:
                        continue
                    # Remove the trailing characters MuPDF placed at the
                    # glyph's right edge (in either order: MuPDF may have
                    # reversed the run they belong to).
                    wanted = list(text[1:])
                    found: list[int] = []
                    for j, other in enumerate(chars):
                        if j in drop or j == i or not wanted:
                            continue
                        obox = other.get("bbox") or (0, 0, 0, 0)
                        if obox[2] - obox[0] < 0.01 and abs(obox[0] - box[2]) < 0.05 and other.get("c") in wanted:
                            wanted.remove(other["c"])
                            found.append(j)
                    if not wanted:
                        ch["c"] = text
                        drop.update(found)
                if drop:
                    span["chars"] = [ch for i, ch in enumerate(chars) if i not in drop]


def mark_line_directions(data: dict[str, Any]) -> None:
    """Paragraph direction for each row containing RTL text, stored as
    line["rtl_base"]. A row alone can be ambiguous ("PDF Studio بنسبة نمو"
    is a wrapped Arabic sentence, yet has more Latin letters), so rows
    without a clear 2:1 majority follow the page's RTL rows overall."""
    rows = []
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if has_rtl(text):
                rows.append((line, text))
    if not rows:
        return
    counts = [strong_counts(text) for _line, text in rows]
    page_rtl = sum(c[0] for c in counts) >= sum(c[1] for c in counts)
    for line, text in rows:
        line["rtl_base"] = paragraph_direction(text, page_rtl)


def merge_line_fragments(data: dict[str, Any]) -> None:
    """Text added to an RTL row later (a replaced word, an edit drawn after
    the original) lands in its own MuPDF line or block. Join RTL fragments
    that share a baseline and touch horizontally back into ONE line, spans in
    reading order, so the row reads (and edits) as a single run."""
    entries = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip() and has_rtl(text) and line.get("bbox"):
                entries.append((block, line))
    if len(entries) < 2:
        return

    def size_of(line: dict[str, Any]) -> float:
        return max((float(s.get("size") or 0) for s in line.get("spans", [])), default=0.0)

    entries.sort(key=lambda item: (round((item[1]["bbox"][1] + item[1]["bbox"][3]) / 2), item[1]["bbox"][0]))
    absorbed: set[int] = set()
    for i, (block, line) in enumerate(entries):
        if id(line) in absorbed:
            continue
        for other_block, other in entries[i + 1:]:
            if id(other) in absorbed:
                continue
            a, b = line["bbox"], other["bbox"]
            overlap = min(a[3], b[3]) - max(a[1], b[1])
            if overlap < 0.6 * min(a[3] - a[1], b[3] - b[1]):
                continue
            size = max(size_of(line), size_of(other))
            if abs(size_of(line) - size_of(other)) > 0.75:
                continue
            left, right = (line, other) if a[0] <= b[0] else (other, line)
            gap = right["bbox"][0] - left["bbox"][2]
            if gap < -1.0 or gap > max(0.9 * size, 6.0):
                continue
            combined = "".join(s.get("text", "") for s in right["spans"] + left["spans"])
            rtl = paragraph_is_rtl(combined)
            line["spans"] = (right["spans"] + left["spans"]) if rtl else (left["spans"] + right["spans"])
            if other.get("rtl_force_visual"):
                line["rtl_force_visual"] = True
            line["bbox"] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
            absorbed.add(id(other))
            other_block["lines"] = [l for l in other_block["lines"] if l is not other]
            a = line["bbox"]
    if absorbed:
        data["blocks"] = [b for b in data.get("blocks", []) if b.get("type") != 0 or b.get("lines")]


def _segment_size(block: dict[str, Any]) -> float:
    sizes = [float(span.get("size") or 0) for line in block.get("lines", []) for span in line.get("spans", [])]
    return max(sizes) if sizes else 0.0


def merge_paragraph_rows(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MuPDF starts a new block for every right-aligned RTL row (their LEFT
    edges differ), so an Arabic paragraph would edit one line at a time.
    Rejoin RTL segments that sit directly under each other with the same
    size and a shared right edge (or left edge) into one paragraph block."""
    def is_rtl(block: dict[str, Any]) -> bool:
        return any(has_rtl(line.get("text") or "") for line in block.get("lines", []))

    result: list[dict[str, Any]] = []
    for block in sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0])) if any(is_rtl(b) for b in blocks) else blocks:
        if is_rtl(block):
            size = _segment_size(block)
            for previous in reversed(result):
                if not is_rtl(previous) or abs(_segment_size(previous) - size) > 0.75:
                    continue
                gap = block["bbox"][1] - previous["bbox"][3]
                if gap < -2 or gap > max(2.0, size * 0.8):
                    continue
                tolerance = max(3.0, size * 0.3)
                same_right = abs(block["bbox"][2] - previous["bbox"][2]) <= tolerance
                same_left = abs(block["bbox"][0] - previous["bbox"][0]) <= tolerance
                if not (same_right or same_left):
                    continue
                previous["lines"] = previous["lines"] + block["lines"]
                previous["bbox"] = [
                    min(previous["bbox"][0], block["bbox"][0]),
                    min(previous["bbox"][1], block["bbox"][1]),
                    max(previous["bbox"][2], block["bbox"][2]),
                    max(previous["bbox"][3], block["bbox"][3]),
                ]
                break
            else:
                result.append(block)
            continue
        result.append(block)
    return result


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

@dataclass
class PlacedGlyph:
    gid: int
    x: float          # pen position along the line, points from line start
    dy: float         # vertical offset (points, up positive)
    cluster: int      # logical index into the paragraph text
    text: str         # the ONE logical character this glyph stands for
    extra: str = ""   # further characters of a ligature (same glyph's ToUnicode)
    width: float = 0.0  # advance in points
    rtl: bool = False


@dataclass
class LayoutLine:
    glyphs: list[PlacedGlyph] = field(default_factory=list)
    width: float = 0.0
    rtl: bool = False
    text: str = ""
    start: int = 0    # offset of the line in the full (multi-paragraph) text


def _shape_line(font: ShapingFont, text: str, size: float, base_rtl: bool, offset: int, shaped: bool = True) -> LayoutLine:
    line = LayoutLine(rtl=base_rtl, text=text, start=offset)
    pen = 0.0
    for run in visual_runs(text, base_rtl):
        chunk = text[run.start:run.end]
        if shaped:
            glyphs = font.shape(chunk, run.rtl)
        else:
            # Unshaped: one nominal glyph per character, visual order.
            chars = list(enumerate(chunk))
            if run.rtl:
                chars.reverse()
            glyphs = []
            for idx, ch in chars:
                gid = font.nominal_gid(ch)
                adv = font._hb_font.get_glyph_h_advance(gid) / font.upem if gid else 0.5
                glyphs.append((gid, adv, 0.0, 0.0, idx))
        # Logical text per glyph. A cluster's characters are dealt to its
        # glyphs in logical order, one each; when a cluster has fewer glyphs
        # than characters (lam-alef, "fi"), the first glyph keeps the first
        # character and the rest join it in the glyph's ToUnicode.
        clusters = sorted({g[4] for g in glyphs})
        cluster_end = {c: (clusters[k + 1] if k + 1 < len(clusters) else len(chunk)) for k, c in enumerate(clusters)}
        members: dict[int, list[int]] = {}
        for k, g in enumerate(glyphs):
            members.setdefault(g[4], []).append(k)
        labels: dict[int, tuple[str, str]] = {}
        for cluster, ks in members.items():
            chars = chunk[cluster:cluster_end[cluster]]
            logical_ks = ks[::-1] if run.rtl else ks
            for n, k in enumerate(logical_ks):
                if n < len(chars):
                    last = n == len(logical_ks) - 1
                    labels[k] = (chars[n], chars[n + 1:] if last else "")
                else:
                    labels[k] = ("", "")
        for k, (gid, adv, xoff, yoff, cluster) in enumerate(glyphs):
            text_char, extra = labels.get(k, ("", ""))
            line.glyphs.append(PlacedGlyph(gid, pen + xoff * size, yoff * size, offset + run.start + cluster,
                                           text_char, extra, adv * size, run.rtl))
            pen += adv * size
    line.width = pen
    return line


def _measure(font: ShapingFont, text: str, size: float, base_rtl: bool) -> float:
    return _shape_line(font, text, size, base_rtl, 0).width


def layout_text(text: str, font: ShapingFont, size: float, max_width: float | None, base_rtl: bool | None = None) -> list[LayoutLine]:
    """Break `text` into lines no wider than max_width (None: no wrapping),
    honoring explicit newlines. Each paragraph takes its own direction when
    its strong letters show a clear 2:1 majority; ambiguous ones (a wrapped
    Arabic row that happens to hold "PDF Studio") follow the whole text."""
    lines: list[LayoutLine] = []
    offset = 0
    overall = paragraph_is_rtl(text)
    for paragraph in text.split("\n"):
        rtl = base_rtl if base_rtl is not None else paragraph_direction(paragraph, overall)
        if max_width is None or not paragraph.strip():
            lines.append(_shape_line(font, paragraph, size, rtl, offset))
            offset += len(paragraph) + 1
            continue
        words = paragraph.split(" ")
        current: list[str] = []
        consumed = 0  # chars of this paragraph already placed on earlier lines
        for word in words:
            candidate = " ".join(current + [word])
            if current and _measure(font, candidate, size, rtl) > max_width:
                line_text = " ".join(current)
                lines.append(_shape_line(font, line_text, size, rtl, offset + consumed))
                consumed += len(line_text) + 1
                current = [word]
            else:
                current.append(word)
        line_text = " ".join(current)
        lines.append(_shape_line(font, line_text, size, rtl, offset + consumed))
        offset += len(paragraph) + 1
    return lines


# --------------------------------------------------------------------------
# PDF emission
# --------------------------------------------------------------------------

def engine_to_pdf_matrix(page: Any) -> Any:
    """Matrix taking engine coordinates (unrotated crop box, top-left origin)
    to PDF user space."""
    import fitz

    rotation = page.rotation
    if rotation:
        page.set_rotation(0)
    try:
        matrix = ~fitz.Matrix(page.transformation_matrix)
    finally:
        if rotation:
            page.set_rotation(rotation)
    return matrix


def _tounicode_cmap(entries: dict[int, str]) -> bytes:
    lines = [
        "/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
        "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def",
        "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
        "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange",
    ]
    items = sorted((gid, text) for gid, text in entries.items() if text)
    for start in range(0, len(items), 100):
        chunk = items[start:start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for gid, text in chunk:
            lines.append(f"<{gid:04X}> <{text.encode('utf-16-be').hex().upper()}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("ascii")


# Advance of the ActualText carrier glyph, in 1/1000 em. It must not be zero:
# MuPDF drops text lines whose characters all have empty boxes from its
# dict/rawdict/words output. It must stay well under MuPDF's 0.15 em
# "logical order" tolerance, or MuPDF flags the line as visual-order RTL and
# reverses the ActualText characters. Stretched lines (OCR) squeeze the
# carrier back with Tz.
CARRIER_WIDTH = 50


def _actual_text(text: str) -> str:
    """PDF text string (UTF-16BE with BOM, hex) for an /ActualText entry."""
    return "<FEFF" + text.encode("utf-16-be").hex().upper() + ">"


# A GlyphWriter span, also after MuPDF rewrote the content stream (it drops
# whitespace and may turn the hex string into a literal one).
_OUR_SPAN = re.compile(rb"/Span\s*<<\s*/ActualText\s*(?:<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\)])*\))\s*/PSRtl\s+true"
                       rb"\s*/PSBox\s*\[([-0-9.\s]*)\]\s*>>\s*BDC", re.S)


def strip_actual_text(page: Any, areas: list[tuple[float, float, float, float]] | None = None) -> int:
    """Turn GlyphWriter ActualText spans into plain marked-content sequences
    (BMC, same EMC), so those lines' text reverts to their glyphs. `areas`
    (PDF user space rects) limits this to lines whose /PSBox touches one;
    None means every line. Redaction removes glyphs but never marked-content
    properties, so a line's ActualText would keep the removed words.
    Returns the number of spans changed."""
    doc = page.parent

    def hit(match: "re.Match[bytes]") -> bytes:
        if areas is None:
            return b"/PSRtl BMC"
        try:
            x0, y0, x1, y1 = (float(v) for v in match.group(1).split())
        except ValueError:
            return b"/PSRtl BMC"
        for ax0, ay0, ax1, ay1 in areas:
            if min(x1, ax1) >= max(x0, ax0) and min(y1, ay1) >= max(y0, ay0):
                return b"/PSRtl BMC"
        return match.group(0)

    changed = 0
    for xref in page.get_contents():
        data = doc.xref_stream(xref) or b""
        if b"/PSRtl" not in data:
            continue
        new = _OUR_SPAN.sub(hit, data)
        if new != data:
            doc.update_stream(xref, new)
            changed += 1

    # GlyphWriter lines inside Form XObjects (paragraph reflow draws the
    # moved content back through show_pdf_page, and MuPDF's redaction keeps
    # such forms as forms). Their /PSBox is in FORM space: map it to page
    # user space with the form's effective matrix before testing.
    import fitz

    for xref, matrix in form_spaces(page):
        data = doc.xref_stream(xref) or b""
        if b"/PSRtl" not in data:
            continue

        def hit_form(match: "re.Match[bytes]", matrix: Any = matrix) -> bytes:
            if areas is None:
                return b"/PSRtl BMC"
            try:
                box = fitz.Rect(*(float(v) for v in match.group(1).split())) * matrix
            except (TypeError, ValueError):
                return b"/PSRtl BMC"
            for ax0, ay0, ax1, ay1 in areas:
                if min(box.x1, ax1) >= max(box.x0, ax0) and min(box.y1, ay1) >= max(box.y0, ay0):
                    return b"/PSRtl BMC"
            return match.group(0)

        new = _OUR_SPAN.sub(hit_form, data)
        if new != data:
            doc.update_stream(xref, new)
            changed += 1
    return changed


def _images_module() -> Any:
    """Engine/ext/images.py (content-stream tokenizer), loaded once."""
    import importlib.util
    import sys

    name = "pdfstudio_ext_images"
    if name not in sys.modules:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images.py")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def form_spaces(page: Any) -> list[tuple[int, Any]]:
    """Every Form XObject reachable from the page's content, with the matrix
    mapping its form space to page user space (form /Matrix x CTM at the Do).
    A form invoked several times is listed once per invocation."""
    import fitz

    doc = page.parent
    if not page.get_xobjects():
        return []
    images = _images_module()
    out: list[tuple[int, Any]] = []

    def walk(data: bytes, names: dict, ctm: Any, chain: frozenset, depth: int) -> None:
        stack: list = []
        for op in images.iter_ops(data):
            if op.name == b"q":
                stack.append(fitz.Matrix(ctm))
            elif op.name == b"Q":
                if stack:
                    ctm = stack.pop()
            elif op.name == b"cm":
                nums = [v for k, v in op.operands if k == "num"]
                if len(nums) == 6:
                    ctm = fitz.Matrix(*nums) * ctm
            elif op.name == b"Do" and op.operands and op.operands[-1][0] == "name":
                target = names.get(images._norm_name(op.operands[-1][1]))
                if not target or target in chain or depth >= 12:
                    continue
                kind, sub = doc.xref_get_key(target, "Subtype")
                if sub != "/Form":
                    continue
                mt, mv = doc.xref_get_key(target, "Matrix")
                nums = images._numbers(mv) if mt == "array" else []
                eff = (fitz.Matrix(*nums) if len(nums) == 6 else fitz.Matrix(1, 0, 0, 1, 0, 0)) * ctm
                out.append((target, eff))
                res = images._resources_string(doc, target, inherit=False)
                fnames = images._xobject_names_from_resources(doc, res) if res[0] != "null" else names
                walk(doc.xref_stream(target) or b"", fnames, eff, chain | {target}, depth + 1)

    page_names = images._xobject_names_from_resources(doc, images._resources_string(doc, page.xref, inherit=True))
    data = b"\n".join(doc.xref_stream(x) or b"" for x in page.get_contents())
    walk(data, page_names, fitz.Matrix(1, 0, 0, 1, 0, 0), frozenset(), 0)
    return out


def redaction_areas(page: Any) -> list[tuple[float, float, float, float]]:
    """/Rect of every redaction annotation on the page, PDF user space."""
    import fitz

    doc = page.parent
    areas = []
    for annot in page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []:
        kind, value = doc.xref_get_key(annot.xref, "Rect")
        try:
            x0, y0, x1, y1 = (float(v) for v in value.strip("[] ").split())
        except ValueError:
            continue
        areas.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    return areas


def install_redaction_guard() -> None:
    """Make fitz.Page.apply_redactions first drop the ActualText of every
    GlyphWriter line a redaction touches (see strip_actual_text). Idempotent."""
    import fitz

    current = fitz.Page.apply_redactions
    if getattr(current, "_pdfstudio_guard", False):
        return

    def apply_redactions(self: Any, *args: Any, **kwargs: Any) -> Any:
        # apply_redactions(images, graphics, text): a redaction that keeps
        # text (text=PDF_REDACT_TEXT_NONE, e.g. reflow clearing line art)
        # removes no glyph, so every line's ActualText stays valid.
        text = kwargs.get("text", args[2] if len(args) > 2 else fitz.PDF_REDACT_TEXT_REMOVE)
        if text != fitz.PDF_REDACT_TEXT_NONE:
            strip_actual_text(self, redaction_areas(self))
        return current(self, *args, **kwargs)

    apply_redactions._pdfstudio_guard = True  # type: ignore[attr-defined]
    fitz.Page.apply_redactions = apply_redactions


def _mupdf_reads_natively(text: str) -> bool:
    """True when MuPDF's own visual-order handling already extracts this
    line logically: RTL letters, marks and single spaces only (MuPDF reverses
    such runs as a whole). Digits, Latin words, punctuation and ligature
    glyphs need the line's /ActualText instead."""
    if not text or text != text.strip() or "  " in text:
        return False
    for ch in text:
        if ch == " ":
            continue
        if unicodedata.bidirectional(ch) not in ("R", "AL", "NSM"):
            return False
    return True


class GlyphWriter:
    """Collects positioned glyphs for one page and writes them as a single
    content stream with one embedded subset font per ShapingFont.

    Text extraction has to satisfy two families of readers:

      * PDFKit (the app, Preview) ignores /ActualText and runs its own bidi
        pass over the glyphs in CONTENT-STREAM order. Glyphs therefore go out
        in VISUAL order (left to right, one TJ per line), exactly the way
        CoreText writes RTL text: a stream order that differs from the glyph
        positions makes PDFKit reorder wrongly and can crash its selection
        code. Each glyph's ToUnicode is the logical text it was shaped from;
        a ligature maps to all of its characters in logical order (lam-alef
        -> lam + alef), which PDFKit keeps as a unit. (Zero-width "marker"
        glyphs for ligature tails, used before, made PDFKit attach the tails
        to the neighbouring line, so copied text picked up stray letters.)
      * MuPDF/PyMuPDF, poppler and Acrobat honour /ActualText. MuPDF never
        moves digits or Latin words relative to Arabic runs and reverses the
        characters of ligatures, so a visual stream comes out scrambled.
        Every such line carries its LOGICAL text instead:
            /Span <</ActualText ()          /PSRtl true /PSBox [..]>> BDC glyphs EMC
            /Span <</ActualText <line text> /PSRtl true /PSBox [..]>> BDC <carrier> Tj EMC
        The empty span hides the glyphs from those readers; the line's text
        rides on one extra glyph, an empty, nearly zero-advance "carrier" at
        the line's start edge (right end for RTL) whose ToUnicode is a space
        (PDFKit trims it). MuPDF assigns ActualText characters to the glyphs
        of the span one by one and stacks the rest after the last one; with
        a tiny carrier advance its bidi heuristics leave them in logical
        order (see CARRIER_WIDTH). Character boxes of such a line
        all sit on the carrier, so geometry-sensitive engine reads use
        glyph_flags(); redaction first drops the ActualText of the lines it
        touches (strip_actual_text) so no removed word survives in it."""

    def __init__(self, page: Any):
        self.page = page
        self.doc = page.parent
        self.to_pdf = engine_to_pdf_matrix(page)
        # (font, render mode, size, matrix, items, actual text, rtl, PDF box)
        # per line; items are (gid, x, dy, rgb) in visual order.
        self.lines: list[tuple] = []
        self.fonts: dict[int, ShapingFont] = {}
        self.maps: dict[int, dict[int, str]] = {}

    def add_line(self, font: ShapingFont, line: LayoutLine, size: float, origin: tuple[float, float],
                 direction: tuple[float, float] = (1.0, 0.0), hscale: float = 1.0,
                 color: tuple[float, float, float] = (0, 0, 0), colors: list | None = None,
                 render_mode: int = 0) -> None:
        """Queue one laid-out line. `origin` is the left end of the baseline
        in engine coordinates, `direction` the unit baseline vector there
        (engine coords, y down), `hscale` stretches the line horizontally.
        `colors` optionally gives an RGB per logical character offset."""
        import fitz

        ux, uy = direction
        # Up vector (engine coords): the baseline turned 90 degrees counter-
        # clockwise on screen, i.e. (0, -1) for a horizontal line.
        vx, vy = uy, -ux
        matrix = fitz.Matrix(hscale * ux, hscale * uy, vx, vy, origin[0], origin[1]) * self.to_pdf
        key = id(font)
        self.fonts[key] = font
        mapping = self.maps.setdefault(key, {})
        known = None
        items: list[tuple] = []
        ligatures = False
        for glyph in line.glyphs:
            if glyph.gid == 0:
                continue
            rgb = color
            if colors is not None and 0 <= glyph.cluster < len(colors) and colors[glyph.cluster] is not None:
                rgb = colors[glyph.cluster]
            if glyph.extra:
                # Ligature: every character, logical order, on the one glyph.
                mapping[glyph.gid] = glyph.text + glyph.extra
                ligatures = True
            elif glyph.text:
                current = mapping.get(glyph.gid)
                if current is None or (unicodedata.mirrored(glyph.text) and glyph.rtl):
                    mapping[glyph.gid] = glyph.text
            elif glyph.gid not in mapping:
                known = known if known is not None else font.gid_text()
                guess = known.get(glyph.gid)
                if guess and len(guess) == 1:
                    mapping[glyph.gid] = guess
            items.append((glyph.gid, glyph.x, glyph.dy, rgb))
        if not items:
            return
        actual = None
        if has_rtl(line.text) and (ligatures or not _mupdf_reads_natively(line.text)):
            # Edge spaces would match the carrier's own " " in MuPDF's
            # prefix/postfix matching of ActualText against glyph text.
            actual = line.text.strip() or None
        # The line's box in PDF user space (redaction matches against it).
        width = max((x + font.advance(gid) * size for gid, x, _dy, _rgb in items), default=0.0)
        corners = [fitz.Point(px, py) * matrix for px in (0.0, width)
                   for py in (-font.ascender * size, -font.descender * size)]
        box = (min(p.x for p in corners), min(p.y for p in corners), max(p.x for p in corners), max(p.y for p in corners))
        self.lines.append((font, render_mode, size, matrix, items, actual, line.rtl, box, hscale))

    def commit(self) -> int:
        """Embed fonts, write the content stream; returns lines written."""
        if not self.lines:
            return 0
        page, doc = self.page, self.doc
        names: dict[int, str] = {}
        carriers: dict[int, int] = {}
        for key, font in self.fonts.items():
            mapping = self.maps.get(key) or {}
            real = sorted(mapping) or [0]
            used = {item[0] for line in self.lines if id(line[0]) == key for item in line[4]}
            carriers[key] = self._carrier_gid(font, set(mapping) | used)
            digest = hashlib.sha1((font.label + ",".join(map(str, real))).encode()).hexdigest()[:8]
            name = f"PSR{digest}"
            subset = font.subset_buffer(real)
            xref = page.insert_font(fontname=name, fontbuffer=subset)
            # The carrier reads as a space for readers that ignore ActualText
            # (PDFKit trims it; an unmapped glyph breaks PDFKit's selection).
            self._replace_tounicode(xref, {**mapping, carriers[key]: " "} if carriers[key] else mapping)
            self._rename(xref, font, digest)
            if carriers[key]:
                self._set_width(xref, carriers[key], CARRIER_WIDTH)
            names[key] = name
        if not page.is_wrapped:
            page.wrap_contents()
        chunks = ["q", "BT"]
        for font, mode, size, m, items, actual, rtl, box, hscale in self.lines:
            # Horizontal scaling (Tz) for the carrier so that a stretched line
            # (OCR) does not stretch it past MuPDF's tolerance either.
            squeeze = 1.0 / max(1.0, hscale)
            props = "/PSRtl true /PSBox [%.2f %.2f %.2f %.2f]" % box
            key = id(font)
            carrier = carriers[key]
            if not carrier:
                actual = None
            chunks.append(f"{mode} Tr")
            chunks.append(f"/{names[key]} {size:.3f} Tf")
            chunks.append("%.5f %.5f %.5f %.5f %.3f %.3f Tm" % (m.a, m.b, m.c, m.d, m.e, m.f))
            tj: list[str] = []
            pen = 0.0
            rise = 0.0
            current = None

            def flush() -> None:
                if tj:
                    chunks.append("[" + "".join(tj) + "] TJ")
                    tj.clear()

            def carry(back: bool) -> None:
                # The logical text on the empty carrier glyph just inside the
                # line's start edge (its right end for RTL lines: the carrier
                # then ends exactly there, so a clip of the line keeps it).
                flush()
                glyph = f"[{CARRIER_WIDTH} <{carrier:04X}>] TJ" if back else f"<{carrier:04X}> Tj"
                tz = (f"{100 * squeeze:.4f} Tz ", " 100 Tz") if squeeze < 1.0 else ("", "")
                chunks.append(f"/Span <</ActualText {_actual_text(actual)} {props}>> BDC {tz[0]}{glyph}{tz[1]} EMC")

            if actual is not None and not rtl:
                carry(False)
                pen = CARRIER_WIDTH * size * squeeze / 1000.0
            if actual is not None:
                chunks.append(f"/Span <</ActualText () {props}>> BDC")
            for gid, x, dy, rgb in items:
                if rgb != current:
                    flush()
                    chunks.append("%.4f %.4f %.4f rg" % tuple(rgb))
                    current = rgb
                if abs(dy - rise) > 0.01:
                    flush()
                    chunks.append("%.3f Ts" % dy)
                    rise = dy
                shift = (pen - x) * 1000.0 / size
                if abs(shift) > 0.05:
                    tj.append("%.2f" % shift)
                tj.append("<%04X>" % gid)
                pen = x + font.advance(gid) * size
            flush()
            if actual is not None:
                chunks.append("EMC")
                if rise:
                    chunks.append("0 Ts")
                    rise = 0.0
                if rtl:
                    carry(True)
            if rise:
                chunks.append("0 Ts")
        chunks += ["0 Tr", "ET", "Q"]
        stream = "\n".join(chunks).encode("latin-1")
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, stream)
        contents = page.get_contents()
        refs = " ".join(f"{x} 0 R" for x in [*contents, xref])
        doc.xref_set_key(page.xref, "Contents", f"[{refs}]")
        return len(self.lines)

    @staticmethod
    def _carrier_gid(font: ShapingFont, taken: set[int]) -> int:
        """An unused glyph id (empty in the subset) for the ActualText
        carrier."""
        candidate = len(font.tt.getGlyphOrder()) - 1
        while candidate > 0 and candidate in taken:
            candidate -= 1
        return max(candidate, 0)

    def _set_width(self, font_xref: int, gid: int, width: int) -> None:
        """Add one glyph width to the descendant font's /W array."""
        doc = self.doc
        _kind, value = doc.xref_get_key(font_xref, "DescendantFonts")
        match = re.search(r"(\d+) 0 R", value or "")
        if not match:
            return
        descendant = int(match.group(1))
        kind, value = doc.xref_get_key(descendant, "W")
        entry = f" {gid} [{width}]]"
        if kind == "xref":
            wxref = int(value.split()[0])
            body = doc.xref_object(wxref, compressed=True).strip()
            if body.endswith("]"):
                doc.update_object(wxref, body[:-1] + entry)
        elif kind == "array" and value.strip().endswith("]"):
            doc.xref_set_key(descendant, "W", value.strip()[:-1] + entry)
        else:
            doc.xref_set_key(descendant, "W", "[" + entry)

    def _rename(self, xref: int, font: ShapingFont, digest: str) -> None:
        # MuPDF names the font after whatever name record it finds first
        # ("Regular" for Geeza Pro); give viewers the PostScript name with a
        # subset tag, as PDF expects for subset fonts.
        doc = self.doc
        tag = digest[:6].upper().translate(str.maketrans("0123456789", "GHIJKLMNOP"))
        base = f"/{tag}+{font.ps_name}"
        doc.xref_set_key(xref, "BaseFont", base)
        _kind, value = doc.xref_get_key(xref, "DescendantFonts")
        match = re.search(r"(\d+) 0 R", value or "")
        if not match:
            return
        descendant = int(match.group(1))
        doc.xref_set_key(descendant, "BaseFont", base)
        # Glyphs missing from /W must not advance: the PDF default width is
        # 1000 units.
        doc.xref_set_key(descendant, "DW", "0")
        fd_kind, fd_value = doc.xref_get_key(descendant, "FontDescriptor")
        if fd_kind == "xref":
            doc.xref_set_key(int(fd_value.split()[0]), "FontName", base)

    def _replace_tounicode(self, font_xref: int, mapping: dict[int, str]) -> None:
        doc = self.doc
        data = _tounicode_cmap(mapping)
        kind, value = doc.xref_get_key(font_xref, "ToUnicode")
        if kind == "xref":
            doc.update_stream(int(value.split()[0]), data)
            return
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, data)
        doc.xref_set_key(font_xref, "ToUnicode", f"{xref} 0 R")


# --------------------------------------------------------------------------
# High-level: a paragraph box
# --------------------------------------------------------------------------

def draw_paragraph(page: Any, rect: Any, text: str, font: ShapingFont, size: float,
                   color: tuple[float, float, float], align: str = "auto",
                   line_height: float = 0.0, wrap: bool = True,
                   colors: list | None = None, min_size: float = 6.0,
                   bottom_limit: float | None = None) -> dict[str, Any] | None:
    """Lay `text` out inside rect (engine coords; rect.y0 is the top of the
    first line) and draw it. Shrinks the size until the last line fits above
    `bottom_limit` (default: page bottom - 12). align: auto (start of each
    paragraph: right for RTL), left, center, right. Returns the drawn box
    {"rect", "size", "lines"} or None when nothing fits."""
    import fitz

    limit = bottom_limit if bottom_limit is not None else page.rect.y1 - 12
    width = max(1.0, rect.x1 - rect.x0)
    factor = (line_height / size) if (line_height and size > 0) else 0.0
    current = size
    while current >= min_size:
        lines = layout_text(text, font, current, width if wrap else None)
        step = current * (max(0.9, min(2.5, factor)) if factor else 1.2)
        baseline = rect.y0 + font.ascender * current
        last = baseline + step * (len(lines) - 1)
        if last - font.descender * current <= limit or current - 0.5 < min_size:
            writer = GlyphWriter(page)
            right_edge = rect.x1
            drawn = fitz.Rect()
            for line in lines:
                mode = align
                if mode in ("auto", "start", None, ""):
                    mode = "right" if line.rtl else "left"
                if mode == "right":
                    x = right_edge - line.width
                elif mode == "center":
                    x = rect.x0 + (width - line.width) / 2
                else:
                    x = rect.x0
                writer.add_line(font, line, current, (x, baseline), color=color, colors=colors)
                if line.glyphs:
                    drawn |= fitz.Rect(x, baseline - font.ascender * current, x + line.width, baseline - font.descender * current)
                baseline += step
            writer.commit()
            return {"rect": drawn, "size": current, "lines": len(lines)}
        current -= 0.5
    return None
