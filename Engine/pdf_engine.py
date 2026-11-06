#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import zlib
from pathlib import Path
from typing import Any

ENGINE_VERSION = "0.2.0"


def respond(ok: bool, **payload: Any) -> int:
    print(json.dumps({"ok": ok, **payload}, indent=2, sort_keys=True))
    return 0 if ok else 1


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def safe_stem(path: str | Path) -> str:
    stem = Path(path).stem
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in stem)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "output"


def parse_page_spec(spec: str | None, total_pages: int) -> list[int]:
    if spec is None or spec.strip().lower() in {"", "all"}:
        return list(range(total_pages))

    pages: set[int] = set()
    for chunk in spec.split(","):
        part = chunk.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                start, end = end, start
            for page in range(start, end + 1):
                pages.add(page - 1)
        else:
            pages.add(int(part) - 1)

    invalid = [page + 1 for page in pages if page < 0 or page >= total_pages]
    if invalid:
        raise ValueError(f"page selection out of range: {invalid}; document has {total_pages} pages")
    return sorted(pages)


def search_terms(text: str) -> list[str]:
    terms: list[str] = []
    raw = text.strip()
    collapsed = " ".join(raw.split())
    for term in [raw, collapsed, *[line.strip() for line in raw.splitlines()]]:
        if term and term not in terms:
            terms.append(term)
    return terms


def search_rects(page: Any, text: str) -> list[Any]:
    for term in search_terms(text):
        rects = page.search_for(term, flags=rtl_module().glyph_flags("search"))
        if rects:
            return merge_touching_rects(rects) if text_has_rtl(term) else rects
    return []


def merge_touching_rects(rects: list[Any]) -> list[Any]:
    """MuPDF reports one Arabic hit as several touching pieces (it searches
    visual runs); join pieces on the same row that touch into one rect."""
    import fitz

    merged: list[Any] = []
    for rect in sorted((fitz.Rect(r) for r in rects), key=lambda r: (round(r.y0, 1), r.x0)):
        if merged:
            last = merged[-1]
            overlap = min(last.y1, rect.y1) - max(last.y0, rect.y0)
            if overlap > 0.5 * min(last.height, rect.height) and rect.x0 - last.x1 <= 1.5:
                merged[-1] = last | rect
                continue
        merged.append(rect)
    return merged


def parse_rect(rect_text: str | None) -> tuple[float, float, float, float] | None:
    if not rect_text:
        return None
    parts = [float(part.strip()) for part in rect_text.split(",")]
    if len(parts) != 4:
        raise ValueError("--rect must be x0,y0,x1,y1")
    return (min(parts[0], parts[2]), min(parts[1], parts[3]), max(parts[0], parts[2]), max(parts[1], parts[3]))


def mirrored_vertical_rect(page: Any, rect: Any) -> Any:
    import fitz

    return fitz.Rect(
        rect.x0,
        page.rect.y1 - (rect.y1 - page.rect.y0),
        rect.x1,
        page.rect.y1 - (rect.y0 - page.rect.y0),
    )


def rects_intersect(a: Any, b: Any) -> bool:
    return not (a.x1 < b.x0 or a.x0 > b.x1 or a.y1 < b.y0 or a.y0 > b.y1)


def rect_overlap_area(a: Any, b: Any) -> float:
    x_overlap = max(0, min(a.x1, b.x1) - max(a.x0, b.x0))
    y_overlap = max(0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return x_overlap * y_overlap


def rect_proximity(anchor: Any, candidate: Any) -> float:
    overlap = rect_overlap_area(anchor, candidate)
    if overlap > 0:
        return -overlap
    dx = max(anchor.x0 - candidate.x1, candidate.x0 - anchor.x1, 0)
    dy = max(anchor.y0 - candidate.y1, candidate.y0 - anchor.y1, 0)
    return dx * dx + dy * dy


def nearest_search_rect(page: Any, click_rect: Any, text: str) -> Any | None:
    matches = search_rects(page, text)
    if not matches:
        return None
    anchors = [click_rect, mirrored_vertical_rect(page, click_rect)]
    return min(matches, key=lambda match: min(rect_proximity(anchor, match) for anchor in anchors))


def pdf_color_to_rgb(value: int | None) -> tuple[float, float, float]:
    if value is None:
        return (0, 0, 0)
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)


def builtin_font_alias(font_name: str | None) -> str:
    name = (font_name or "").lower()
    if "courier" in name or "mono" in name:
        if "bold" in name and ("italic" in name or "oblique" in name):
            return "cobo"
        if "bold" in name:
            return "cob"
        if "italic" in name or "oblique" in name:
            return "coi"
        return "cour"
    if "times" in name or "serif" in name:
        if "bold" in name and ("italic" in name or "oblique" in name):
            return "tibi"
        if "bold" in name:
            return "tibo"
        if "italic" in name or "oblique" in name:
            return "tiit"
        return "tiro"
    if "bold" in name and ("italic" in name or "oblique" in name):
        return "hebo"
    if "bold" in name:
        return "hebo"
    if "italic" in name or "oblique" in name:
        return "heit"
    return "helv"


def align_constant(name: str | None) -> int:
    """Map a friendly alignment name to a fitz TEXT_ALIGN_* constant."""
    import fitz

    return {
        "center": fitz.TEXT_ALIGN_CENTER,
        "right": fitz.TEXT_ALIGN_RIGHT,
    }.get((name or "left").strip().lower(), fitz.TEXT_ALIGN_LEFT)


def draw_text_underlines(page: Any, clip: Any, color: Any, width: float = 0.6) -> None:
    """Underline every text line whose center sits inside `clip`, drawing a
    thin rule just below each line's baseline. Additive vector drawing only —
    it never touches or removes the glyphs, so text is preserved."""
    import fitz

    clip_rect = fitz.Rect(clip)
    stroke = color or (0, 0, 0)
    for block in page.get_text("dict", clip=clip_rect, flags=rtl_module().glyph_flags("dict")).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if (s.get("text") or "").strip()]
            if not spans:
                continue
            bbox = line.get("bbox")
            if not bbox:
                continue
            center = fitz.Point((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
            if not clip_rect.contains(center):
                continue
            y = bbox[3] - 1
            page.draw_line(fitz.Point(bbox[0], y), fitz.Point(bbox[2], y), color=stroke, width=width)


def style_for_rect(page: Any, rect: Any) -> dict[str, Any]:
    data = page.get_text("dict", flags=rtl_module().glyph_flags("dict"))
    best_span: dict[str, Any] | None = None
    best_area = 0.0
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox")
                if not bbox:
                    continue
                try:
                    import fitz

                    span_rect = fitz.Rect(bbox)
                except Exception:
                    continue
                if not rects_intersect(span_rect, rect):
                    continue
                x_overlap = max(0, min(span_rect.x1, rect.x1) - max(span_rect.x0, rect.x0))
                y_overlap = max(0, min(span_rect.y1, rect.y1) - max(span_rect.y0, rect.y0))
                area = x_overlap * y_overlap
                if area > best_area:
                    best_span = span
                    best_area = area

    if not best_span:
        return {
            "fontname": "helv",
            "fontsize": max(8, rect.height * 0.72),
            "color": (0, 0, 0),
            "rawfont": None,
            "origin": None,
        }

    return {
        "fontname": builtin_font_alias(best_span.get("font")),
        "fontsize": float(best_span.get("size") or max(8, rect.height * 0.72)),
        "color": pdf_color_to_rgb(best_span.get("color")),
        "rawfont": best_span.get("font"),
        "origin": tuple(best_span.get("origin") or ()) or None,
    }


def strip_subset_prefix(font_name: str) -> str:
    # Embedded subset fonts are named like "ABCDEF+Arial-BoldMT".
    if len(font_name) > 7 and font_name[6] == "+" and font_name[:6].isalpha() and font_name[:6].isupper():
        return font_name[7:]
    return font_name


def font_covers_text(fontbuffer: bytes, text: str) -> bool:
    import fitz

    try:
        font = fitz.Font(fontbuffer=fontbuffer)
    except Exception:
        return False
    # Spaces count too: subset fonts often omit the space glyph, which would
    # render as tofu boxes between words.
    return all(font.has_glyph(ord(ch)) for ch in text if ch not in "\n\r\t")


def font_file_covers_text(path: str, text: str) -> bool:
    import fitz

    try:
        font = fitz.Font(fontfile=path)
    except Exception:
        return False
    return all(font.has_glyph(ord(ch)) for ch in text if ch not in "\n\r\t")


_SYSTEM_FONT_DIRS = (
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
)


def system_font_file(raw_name: str, text: str) -> str | None:
    """Best-effort lookup of an installed .ttf/.otf matching the PDF font name."""
    cleaned = strip_subset_prefix(raw_name or "")
    if not cleaned:
        return None
    lowered = cleaned.lower()
    is_bold = "bold" in lowered
    is_italic = "italic" in lowered or "oblique" in lowered
    family = re.split(r"[-,]", cleaned)[0]
    # Vendor suffixes stack ("TimesNewRomanPSMT"): strip until none is left.
    while True:
        stripped = re.sub(r"(MT|PS|Std|Pro)$", "", family)
        if stripped == family or not stripped:
            break
        family = stripped
    # Split CamelCase into words: "TimesNewRoman" -> "Times New Roman"
    family_words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family).strip()
    if not family_words:
        return None

    candidates = []
    base = family_words
    if is_bold and is_italic:
        candidates += [f"{base} Bold Italic", f"{base}BdIt", f"{base}-BoldItalic"]
    elif is_bold:
        candidates += [f"{base} Bold", f"{base}-Bold"]
    elif is_italic:
        candidates += [f"{base} Italic", f"{base}-Italic"]
    candidates.append(base)

    for directory in _SYSTEM_FONT_DIRS:
        if not os.path.isdir(directory):
            continue
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        by_lower = {entry.lower(): entry for entry in entries}
        for candidate in candidates:
            for ext in (".ttf", ".otf"):
                key = f"{candidate.lower()}{ext}"
                if key in by_lower:
                    path = os.path.join(directory, by_lower[key])
                    if font_file_covers_text(path, text):
                        return path
    return None


def resolve_replacement_font(doc: Any, page: Any, style: dict[str, Any], text: str, scratch: list[str]) -> dict[str, Any]:
    """Pick the closest font for replacement text, matching the original.

    Preference order: the document's own embedded font (when it covers the
    replacement's glyphs) -> an installed system font with the same family ->
    the base-14 alias the old code used.
    Returns kwargs for insert_text/insert_textbox.
    """
    raw_name = style.get("rawfont") or ""
    if raw_name:
        wanted = strip_subset_prefix(raw_name).lower()
        try:
            fonts = page.get_fonts(full=False)
        except Exception:
            fonts = []

        def style_words(name: str) -> set[str]:
            name = name.lower()
            return {w for w in ("bold", "italic", "oblique", "black", "semibold", "demi", "light",
                                "medium", "heavy", "condensed", "narrow") if w in name}

        # The span's OWN font first: an exact name match. Only then a font
        # whose name merely contains it ("Amasis MT Pro" inside "Amasis MT
        # Pro,Bold"), and never one that adds a weight/style the span lacks —
        # a substring match listed first turned regular text bold.
        exact = [e for e in fonts if e[3] and strip_subset_prefix(e[3]).lower() == wanted]
        similar = [e for e in fonts if e[3] and e not in exact
                   and wanted in strip_subset_prefix(e[3]).lower()
                   and style_words(strip_subset_prefix(e[3])) == style_words(wanted)]
        for entry in exact + similar:
            xref, _ext, _ftype, basefont = entry[0], entry[1], entry[2], entry[3]
            try:
                _name, ext, _t, buffer = doc.extract_font(xref)
            except Exception:
                continue
            if not buffer or ext not in ("ttf", "otf", "cff"):
                continue
            if not font_covers_text(buffer, text):
                continue
            handle, path = tempfile.mkstemp(suffix=f".{ext}")
            with os.fdopen(handle, "wb") as fh:
                fh.write(buffer)
            scratch.append(path)
            # One resource name PER FONT: MuPDF reuses an existing page font
            # resource with the same name, so a shared "FolioEmbedded" made a
            # second font's text draw with the first font's glyphs/widths.
            return {"fontname": f"FolioE{xref}", "fontfile": path}

        system_path = system_font_file(raw_name, text)
        if system_path:
            return {"fontname": f"FolioS{zlib.crc32(system_path.encode()) & 0xFFFFFF:06x}", "fontfile": system_path}

    return {"fontname": style["fontname"], "fontfile": None}


def replacement_text_length(text: str, font_kwargs: dict[str, Any], fontsize: float) -> float:
    import fitz

    try:
        if font_kwargs.get("fontfile"):
            font = fitz.Font(fontfile=font_kwargs["fontfile"])
        else:
            font = fitz.Font(font_kwargs["fontname"])
        return font.text_length(text, fontsize=fontsize)
    except Exception:
        return len(text) * fontsize * 0.55


# --- Right-to-left (Arabic/Hebrew) text -------------------------------------
# Shaping, bidi and PDF emission live in Engine/ext/_rtl_text.py (skipped by
# the extension loader); these wrappers keep the call sites below small.

_RTL_CHARS = re.compile("[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")


def rtl_module() -> Any:
    engine_dir = str(Path(__file__).resolve().parent)
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    from ext import _rtl_text

    return _rtl_text


def reflow_module() -> Any:
    engine_dir = str(Path(__file__).resolve().parent)
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    from ext import _reflow

    return _reflow


def reflow_helpers() -> Any:
    """Engine internals the reflow module (Engine/ext/_reflow.py) builds on."""
    from types import SimpleNamespace

    return SimpleNamespace(
        apply_block_edit=apply_block_edit,
        text_dict=text_dict,
        line_span_segments=line_span_segments,
        images_module=images_module,
    )


def images_module() -> Any:
    """Engine/ext/images.py (per-placement image edits), loaded on demand."""
    import importlib.util

    name = "folio_ext_images"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / "ext" / "images.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def text_has_rtl(text: str | None) -> bool:
    return bool(text) and bool(_RTL_CHARS.search(text))


def rtl_render_available() -> bool:
    """Shaping needs uharfbuzz + fontTools (both ship in the engine venv as
    dependencies); without them RTL text falls back to the plain path."""
    try:
        import fontTools  # noqa: F401
        import uharfbuzz  # noqa: F401
    except ImportError:
        return False
    return True


def natural_align(args: argparse.Namespace) -> bool:
    """"auto" (the default) means the paragraph's start edge: left for LTR,
    right for RTL. Explicit "left" stays left."""
    return getattr(args, "align", "auto") in ("auto", "left")


def text_dict(page: Any, clip: Any = None) -> dict[str, Any]:
    """page.get_text("dict") with per-character data kept (rawdict) and every
    span's "text" filled in, so RTL rows can be rebuilt in logical order."""
    # Real glyph boxes (not /ActualText, see _rtl_text.glyph_flags).
    flags = rtl_module().glyph_flags("rawdict")
    data = page.get_text("rawdict", clip=clip, flags=flags) if clip is not None else page.get_text("rawdict", flags=flags)
    rtl_module().prepare_glyph_chars(page, data)
    repairs = rtl_module().broken_text_repairs(page) if any(
        entry[2] == "Type0" for entry in page.get_fonts(full=True)
    ) else {}
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                maps = repairs.get(span.get("font") or "")
                if maps:
                    # Junk ToUnicode (see broken_text_repairs): real letters
                    # from the embedded font; MuPDF's order is unreliable for
                    # such rows, so rebuild them from glyph positions.
                    if rtl_module().repair_span_chars(span.get("chars", []), maps):
                        line["rtl_force_visual"] = True
                span["text"] = "".join(ch.get("c", "") for ch in span.get("chars", []))
    rt = rtl_module()
    rt.merge_line_fragments(data)
    rt.mark_line_directions(data)
    return data


def logical_spans_text(spans: list[dict[str, Any]], rtl_base: bool | None = None, force_visual: bool = False) -> str:
    """Text of spans as one row: plain concatenation for LTR, the logical
    (reading-order) string for rows containing RTL characters."""
    text = "".join(span.get("text", "") for span in spans)
    if not text_has_rtl(text) or not all("chars" in span for span in spans):
        return text
    chars = [ch for span in spans for ch in span.get("chars", [])]
    return rtl_module().logical_text_from_chars(chars, rtl_base, force_visual)


def rtl_font_for(doc: Any, page: Any, text: str, raw_font: str | None, bold: bool, family: str | None = None) -> Any:
    """Font for RTL text: an explicit family (when it shapes Arabic), else the
    document's own embedded font when it covers and shapes the text, else an
    Arabic-capable system font matching the original family, Geeza Pro,
    Arial, Tahoma, Times New Roman."""
    rt = rtl_module()
    if family:
        font = rt.arabic_font_for(text, bold=bold, family_hint=family)
        if font is not None:
            return font
    font = rt.embedded_font_for(doc, page, raw_font, text)
    if font is not None:
        return font
    return rt.arabic_font_for(text, bold=bold, family_hint=raw_font)


def rtl_colors(text: str, color_runs: list | None) -> list | None:
    """Per-character RGB list from the editor's color runs (later runs win)."""
    runs = color_run_rgb(color_runs or [], (0, 0, 0))
    if not runs:
        return None
    colors: list = [None] * len(text)
    for run in runs:
        for index in range(max(0, run["start"]), min(len(text), run["start"] + run["length"])):
            colors[index] = run["rgb"]
    return colors


def draw_rtl_line(page: Any, font: Any, text: str, size: float, color: Any, baseline_y: float,
                  left: float | None = None, right: float | None = None, rtl: bool | None = None) -> Any:
    """One unwrapped line anchored at its left OR right edge (`rtl` forces
    the paragraph direction, e.g. from the surrounding block). Returns its rect."""
    import fitz

    rt = rtl_module()
    lines = rt.layout_text(text, font, size, None, base_rtl=rtl)
    writer = rt.GlyphWriter(page)
    drawn = fitz.Rect()
    step = size * 1.2
    for line in lines:
        x = (right - line.width) if right is not None else (left or 0.0)
        writer.add_line(font, line, size, (x, baseline_y), color=color)
        drawn |= fitz.Rect(x, baseline_y - font.ascender * size, x + line.width, baseline_y - font.descender * size)
        baseline_y += step
    writer.commit()
    return drawn


@contextlib.contextmanager
def silence_process_output():
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stdout = os.dup(1)
    old_stderr = os.dup(2)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(old_stdout, 1)
        os.dup2(old_stderr, 2)
        os.close(old_stdout)
        os.close(old_stderr)
        os.close(devnull)


def import_status(name: str, import_name: str | None = None) -> dict[str, Any]:
    import_name = import_name or name
    try:
        capture = io.StringIO()
        with silence_process_output(), contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            module = __import__(import_name)
        version = getattr(module, "__version__", None)
        if import_name == "fitz":
            version = getattr(module, "version", ["unknown"])[0]
        return {"ok": True, "version": str(version or "unknown"), "path": getattr(module, "__file__", None), "error": None}
    except Exception as exc:
        return {"ok": False, "version": None, "path": None, "error": str(exc)}


def find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    local = Path(sys.executable).resolve().parent / name
    if local.exists():
        return str(local)
    return None


def binary_status(name: str, version_args: list[str] | None = None) -> dict[str, Any]:
    path = find_binary(name)
    if not path:
        return {"ok": False, "version": None, "path": None, "error": "not found"}
    version = "unknown"
    if version_args is not None:
        try:
            result = subprocess.run([path] + version_args, text=True, capture_output=True, timeout=10)
            version_text = (result.stdout or result.stderr).strip().splitlines()
            if version_text:
                version = version_text[0]
        except Exception as exc:
            return {"ok": True, "version": version, "path": path, "error": str(exc)}
    return {"ok": True, "version": version, "path": path, "error": None}


def cmd_health(_args: argparse.Namespace) -> int:
    packages = {
        "pymupdf": import_status("pymupdf", "fitz"),
        "pypdf": import_status("pypdf"),
        "pdfplumber": import_status("pdfplumber"),
        "reportlab": import_status("reportlab"),
        "pillow": import_status("pillow", "PIL"),
        "pdf2docx": import_status("pdf2docx"),
        "ocrmypdf": import_status("ocrmypdf"),
        "openpyxl": import_status("openpyxl"),
        "python-pptx": import_status("python-pptx", "pptx"),
    }
    binaries = {
        "qpdf": binary_status("qpdf", ["--version"]),
        "tesseract": binary_status("tesseract", ["--version"]),
        "gs": binary_status("gs", ["--version"]),
        "pdftoppm": binary_status("pdftoppm", ["-v"]),
    }
    required = ["pymupdf", "pypdf", "pdf2docx", "ocrmypdf", "openpyxl", "python-pptx"]
    required_bins = ["qpdf", "tesseract", "gs"]
    ok = all(packages[item]["ok"] for item in required) and all(binaries[item]["ok"] for item in required_bins)
    return respond(ok, engine=ENGINE_VERSION, python=sys.executable, packages=packages, binaries=binaries)


def cmd_metadata(args: argparse.Namespace) -> int:
    import fitz

    doc = fitz.open(args.input)
    return respond(True, input=args.input, pages=doc.page_count, metadata=doc.metadata)


def cmd_merge(args: argparse.Namespace) -> int:
    from pypdf import PdfReader, PdfWriter

    output = Path(args.output)
    ensure_parent(output)
    writer = PdfWriter()
    total_pages = 0

    for input_path in args.input:
        reader = PdfReader(input_path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        for page in reader.pages:
            writer.add_page(page)
            total_pages += 1

    with output.open("wb") as handle:
        writer.write(handle)

    return respond(True, output=str(output), inputs=args.input, pages=total_pages)


def cmd_merge_pages(args: argparse.Namespace) -> int:
    from pypdf import PdfReader, PdfWriter

    if not args.page_item:
        return respond(False, message="choose at least one page")

    output = Path(args.output)
    ensure_parent(output)
    writer = PdfWriter()
    readers: dict[str, Any] = {}
    ordered_pages: list[dict[str, Any]] = []

    for item in args.page_item:
        if "::" not in item:
            return respond(False, message=f"invalid page item: {item}")
        path_text, page_text = item.rsplit("::", 1)
        page_number = int(page_text)
        reader = readers.get(path_text)
        if reader is None:
            reader = PdfReader(path_text)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            readers[path_text] = reader
        if page_number < 1 or page_number > len(reader.pages):
            return respond(False, message=f"page {page_number} out of range for {path_text}")
        writer.add_page(reader.pages[page_number - 1])
        ordered_pages.append({"input": path_text, "page": page_number})

    with output.open("wb") as handle:
        writer.write(handle)

    return respond(True, output=str(output), pages=len(ordered_pages), ordered_pages=ordered_pages)


def cmd_split(args: argparse.Namespace) -> int:
    import fitz

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(args.input)
    outputs: list[str] = []
    stem = safe_stem(args.input)

    for index in parse_page_spec(args.pages, doc.page_count):
        out_doc = fitz.open()
        out_doc.insert_pdf(doc, from_page=index, to_page=index)
        out_path = output_dir / f"{stem}-page-{index + 1:03d}.pdf"
        out_doc.save(str(out_path), garbage=4, deflate=True)
        out_doc.close()
        outputs.append(str(out_path))

    doc.close()
    return respond(True, output_dir=str(output_dir), outputs=outputs, pages=len(outputs))


def cmd_extract_pages(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    indexes = parse_page_spec(args.pages, doc.page_count)
    out_doc = fitz.open()
    for index in indexes:
        out_doc.insert_pdf(doc, from_page=index, to_page=index)
    out_doc.save(str(output), garbage=4, deflate=True)
    out_doc.close()
    doc.close()
    return respond(True, output=str(output), pages=[i + 1 for i in indexes])


def cmd_delete_pages(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    delete_indexes = set(parse_page_spec(args.pages, doc.page_count))
    keep_indexes = [index for index in range(doc.page_count) if index not in delete_indexes]
    if not keep_indexes:
        return respond(False, message="delete-pages would remove every page")

    out_doc = fitz.open()
    for index in keep_indexes:
        out_doc.insert_pdf(doc, from_page=index, to_page=index)
    out_doc.save(str(output), garbage=4, deflate=True)
    out_doc.close()
    doc.close()
    return respond(True, output=str(output), deleted=[i + 1 for i in sorted(delete_indexes)], pages=len(keep_indexes))


def cmd_rotate_pages(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    indexes = parse_page_spec(args.pages, doc.page_count)
    for index in indexes:
        page = doc[index]
        page.set_rotation((page.rotation + args.degrees) % 360)
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), pages=[i + 1 for i in indexes], degrees=args.degrees)


def cmd_crop_pages(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    indexes = parse_page_spec(args.pages, doc.page_count)
    for index in indexes:
        page = doc[index]
        rect = page.rect
        crop = fitz.Rect(
            rect.x0 + args.left,
            rect.y0 + args.top,
            rect.x1 - args.right,
            rect.y1 - args.bottom,
        )
        if crop.width <= 10 or crop.height <= 10:
            return respond(False, message=f"crop margins leave page {index + 1} too small")
        page.set_cropbox(crop)
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), pages=[i + 1 for i in indexes])


# A column break is a horizontal gap wider than the text's own spacing: at
# least 0.9x the font size, never less than this many points.
SEGMENT_MIN_GAP = 6.0
# Two segments on consecutive lines belong to the same paragraph when their
# x-ranges overlap by at least this fraction of the narrower one.
SEGMENT_OVERLAP_RATIO = 0.4


def spans_bbox(spans: list[dict[str, Any]]) -> list[float]:
    """Union of the spans' bounding boxes."""
    boxes = [s["bbox"] for s in spans if s.get("bbox")]
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def line_span_segments(line: dict[str, Any]) -> list[dict[str, Any]]:
    """Split ONE raw dict line into column SEGMENTS.

    PyMuPDF keeps everything on a shared baseline in a single line/block, so
    an invoice row's description and its right-hand amount arrive merged.
    Consecutive spans whose horizontal gap exceeds max(0.9 x fontsize, 6pt)
    start a new segment, so each column stays independently clickable and
    independently editable. Whitespace-only spans are padding: they join the
    segment they sit inside and are dropped when they straddle a break."""
    spans = sorted(
        [s for s in line.get("spans", []) if s.get("bbox") and (s.get("text") or "")],
        key=lambda s: s["bbox"][0],
    )
    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []  # whitespace awaiting a break decision
    last_content: dict[str, Any] | None = None
    for span in spans:
        if not (span.get("text") or "").strip():
            pending.append(span)
            continue
        if last_content is not None:
            gap = span["bbox"][0] - last_content["bbox"][2]
            size = float(span.get("size") or last_content.get("size") or 10.0)
            if gap > max(0.9 * size, SEGMENT_MIN_GAP):
                segments.append(current)
                current = []
                pending = []  # padding that lived in the gap belongs to neither
        current.extend(pending)
        pending = []
        current.append(span)
        last_content = span
    if current:
        segments.append(current)

    out: list[dict[str, Any]] = []
    stream_order = {id(span): index for index, span in enumerate(line.get("spans", []))}
    for group in segments:
        text = "".join(s.get("text", "") for s in group)
        if text_has_rtl(text):
            # RTL rows: rebuild the logical string from the spans in content-
            # stream order (x order would put the words right-to-left).
            text = logical_spans_text(sorted(group, key=lambda s: stream_order.get(id(s), 0)), line.get("rtl_base"),
                                      bool(line.get("rtl_force_visual")))
        if not text.strip():
            continue
        out.append({"spans": group, "text": text, "bbox": spans_bbox(group)})
    return out


def segment_rows(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group segments into visual ROWS by vertical overlap.

    A block's raw "lines" are not rows: PyMuPDF happily emits the left column,
    the right column, and the left column's wrapped continuation as three
    lines in drawing order. Rebuilding rows from geometry keeps a wrapped
    paragraph together even when a column value was drawn between its lines."""
    rows: list[list[dict[str, Any]]] = []
    for segment in sorted(segments, key=lambda s: (round(s["bbox"][1], 1), s["bbox"][0])):
        y0, y1 = segment["bbox"][1], segment["bbox"][3]
        for row in rows:
            row_y0 = min(s["bbox"][1] for s in row)
            row_y1 = max(s["bbox"][3] for s in row)
            overlap = min(y1, row_y1) - max(y0, row_y0)
            shorter = min(y1 - y0, row_y1 - row_y0)
            if shorter > 0 and overlap / shorter >= 0.6:
                row.append(segment)
                break
        else:
            rows.append([segment])
    for row in rows:
        row.sort(key=lambda s: s["bbox"][0])
    rows.sort(key=lambda row: min(s["bbox"][1] for s in row))
    return rows


def segment_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn one raw dict block into column-aware SEGMENT blocks.

    Each result is shaped exactly like a raw text block ({"type", "bbox",
    "lines"}) so every downstream helper — block_payload above all — works on
    it unchanged. Segments on consecutive lines merge into one paragraph when
    their x-ranges overlap enough, so a wrapped left column stays a single
    editable paragraph while the right column's amount stays on its own."""
    segments: list[dict[str, Any]] = []
    for line in block.get("lines", []):
        segments.extend(line_span_segments(line))
    rows = segment_rows(segments)
    if not rows:
        return []

    groups: list[list[dict[str, Any]]] = []
    previous: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        current: list[tuple[int, dict[str, Any]]] = []
        taken: set[int] = set()
        for segment in row:
            x0, x1 = segment["bbox"][0], segment["bbox"][2]
            best_index, best_ratio = -1, 0.0
            for index, previous_segment in previous:
                if index in taken:
                    continue
                px0, px1 = previous_segment["bbox"][0], previous_segment["bbox"][2]
                overlap = min(x1, px1) - max(x0, px0)
                narrower = min(x1 - x0, px1 - px0)
                ratio = (overlap / narrower) if narrower > 0 else 0.0
                if ratio >= SEGMENT_OVERLAP_RATIO and ratio > best_ratio:
                    best_index, best_ratio = index, ratio
            if best_index < 0:
                groups.append([segment])
                best_index = len(groups) - 1
            else:
                groups[best_index].append(segment)
            taken.add(best_index)
            current.append((best_index, segment))
        previous = current

    result = []
    for group in groups:
        boxes = [segment["bbox"] for segment in group]
        result.append({
            "type": 0,
            "bbox": [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ],
            "lines": [
                {"bbox": segment["bbox"], "spans": segment["spans"], "text": segment["text"]}
                for segment in group
            ],
        })
    return result


def page_segment_blocks(page: Any) -> list[dict[str, Any]]:
    """Every real text SEGMENT on the page, in raw-block shape."""
    blocks = []
    for block in text_dict(page).get("blocks", []):
        if block.get("type") != 0:
            continue
        for segment in segment_blocks(block):
            text = "".join(
                span.get("text", "")
                for line in segment.get("lines", [])
                for span in line.get("spans", [])
            )
            # Generated PDFs often contain overlapping whitespace-only blocks;
            # only real text is clickable.
            if not text.strip():
                continue
            blocks.append(segment)
    return merge_spaced_lines(rtl_module().merge_paragraph_rows(blocks))


_BULLETS = ("•", "◦", "▪", "‣", "-", "–", "—", "*", "·")


def _baselines(block: dict[str, Any]) -> list[float]:
    ys = []
    for line in block.get("lines", []):
        origins = [span.get("origin") for span in line.get("spans", []) if span.get("origin")]
        if origins:
            ys.append(float(origins[0][1]))
    return ys


def _dominant_style(block: dict[str, Any]) -> tuple[str, float]:
    best, area = ("", 0.0), -1.0
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            if not (span.get("text") or "").strip():
                continue
            b = span.get("bbox") or (0, 0, 0, 0)
            a = (b[2] - b[0]) * (b[3] - b[1])
            if a > area:
                best, area = (span.get("font") or "", round(float(span.get("size") or 0), 1)), a
    return best


def merge_spaced_lines(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MuPDF starts a new block when line spacing is generous (1.5x, double
    spacing), so such a paragraph would edit one line at a time. Rejoin LTR
    blocks stacked at a steady baseline pitch (<= 2.4x the font size) with the
    same font, size and left edge. Lines stay hard breaks in the block text, so
    joining never rewraps anything the user didn't touch."""
    rtl = rtl_module()
    result: list[dict[str, Any]] = []
    for block in blocks:
        text = "".join(line.get("text") or "" for line in block.get("lines", []))
        ys = _baselines(block)
        font, size = _dominant_style(block)
        merged = False
        if ys and size > 0 and not rtl.has_rtl(text) and not text.lstrip().startswith(_BULLETS):
            for previous in reversed(result[-6:]):
                p_ys = _baselines(previous)
                if not p_ys or _dominant_style(previous) != (font, size):
                    continue
                if any(rtl.has_rtl(line.get("text") or "") for line in previous.get("lines", [])):
                    continue
                if abs(previous["bbox"][0] - block["bbox"][0]) > max(2.0, size * 0.25):
                    continue
                step = ys[0] - p_ys[-1]
                if not (size * 0.9 <= step <= size * 2.4):
                    continue
                pitches = [b - a for a, b in zip(p_ys, p_ys[1:])] + [b - a for a, b in zip(ys, ys[1:])]
                if any(abs(p - step) > max(1.0, size * 0.08) for p in pitches):
                    continue
                previous["lines"] = previous["lines"] + block["lines"]
                previous["bbox"] = [
                    min(previous["bbox"][0], block["bbox"][0]),
                    min(previous["bbox"][1], block["bbox"][1]),
                    max(previous["bbox"][2], block["bbox"][2]),
                    max(previous["bbox"][3], block["bbox"][3]),
                ]
                merged = True
                break
        if not merged:
            result.append(block)
    return result


def find_text_block(page: Any, x: float, y: float) -> dict[str, Any] | None:
    """Locate the text SEGMENT containing (or nearest to) a page point."""
    import fitz

    point = fitz.Rect(x - 1, y - 1, x + 1, y + 1)
    best = None
    best_score = None
    for block in page_segment_blocks(page):
        rect = fitz.Rect(block["bbox"])
        if rect.contains(fitz.Point(x, y)):
            # Containment always beats proximity; among containing segments the
            # SMALLEST wins, so a click on the amount never picks up a bigger
            # overlapping box.
            score = -1_000_000.0 + rect.get_area()
        else:
            score = rect_proximity(point, rect)
            if score > 900:  # more than ~30pt away — not what was clicked
                continue
        if best_score is None or score < best_score:
            best = block
            best_score = score
    return best


def block_payload(block: dict[str, Any]) -> dict[str, Any]:
    lines = []
    span_best = None
    span_area = 0.0
    sizes = []
    for line in block.get("lines", []):
        text = line["text"] if "text" in line else "".join(span.get("text", "") for span in line.get("spans", []))
        lines.append(text)
        for span in line.get("spans", []):
            bbox = span.get("bbox")
            if not bbox:
                continue
            area = max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
            sizes.append(float(span.get("size") or 0))
            if area > span_area:
                span_area = area
                span_best = span

    raw_font = (span_best or {}).get("font") or ""
    line_count = len(lines)
    bbox = block.get("bbox")
    line_height = 0.0
    if line_count > 1 and bbox:
        # Baseline-to-baseline pitch. Dividing the block height by the line
        # count undercounts it (that height holds n-1 pitches plus one line's
        # own height), so edited paragraphs came back tighter than before.
        baselines = []
        for line in block.get("lines", []):
            origins = [span.get("origin") for span in line.get("spans", []) if span.get("origin")]
            if origins:
                baselines.append(float(origins[0][1]))
        steps = sorted(b - a for a, b in zip(baselines, baselines[1:]) if b - a > 0.5)
        if steps:
            line_height = steps[len(steps) // 2]
        else:
            line_height = (bbox[3] - bbox[1]) / line_count

    return {
        "rect": list(block.get("bbox") or ()),
        "text": "\n".join(lines),
        "fontname": raw_font,
        "fontsize": float((span_best or {}).get("size") or 12),
        "color": "#%06x" % ((span_best or {}).get("color") or 0),
        "bold": "bold" in raw_font.lower(),
        "italic": "italic" in raw_font.lower() or "oblique" in raw_font.lower(),
        "line_count": line_count,
        "line_height": line_height,
    }


def cmd_page_blocks(args: argparse.Namespace) -> int:
    """All real text blocks on a page, so the app can hit-test clicks locally."""
    import fitz

    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        doc.close()
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]
    blocks = [block_payload(block) for block in page_segment_blocks(page)]
    doc.close()
    return respond(True, blocks=blocks)


def cmd_block_at(args: argparse.Namespace) -> int:
    import fitz

    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        doc.close()
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]
    block = find_text_block(page, args.x, args.y)
    if not block:
        doc.close()
        return respond(True, found=False)
    payload = block_payload(block)
    doc.close()
    return respond(True, found=True, **payload)


def parse_hex_color(value: str | None) -> tuple[float, float, float] | None:
    if not value:
        return None
    raw = value.lstrip("#")
    if len(raw) != 6:
        return None
    try:
        return (int(raw[0:2], 16) / 255.0, int(raw[2:4], 16) / 255.0, int(raw[4:6], 16) / 255.0)
    except ValueError:
        return None


def block_erase_rect(rect: Any) -> Any:
    """Erase rect for a block, shrunk INWARD so redaction never removes
    neighboring glyphs that merely touch the block's bounding box (redaction
    deletes any glyph whose bbox intersects the rect)."""
    import fitz

    shrink = min(1.25, rect.height * 0.06, rect.width * 0.06)
    return fitz.Rect(rect.x0 + shrink, rect.y0 + shrink, rect.x1 - shrink, rect.y1 - shrink)


def _family_hint(raw_font: str | None) -> str | None:
    """Guess a friendly family name from a sampled PDF font, so an italic/bold
    request with no explicit --font can still select a matching face."""
    name = (raw_font or "").lower()
    if not name:
        return None
    if "times" in name or "serif" in name or name.startswith("ti"):
        return "Times New Roman"
    if "courier" in name or "mono" in name or name.startswith("co"):
        return "Courier New"
    return "Helvetica"


def resolve_block_style(doc: Any, page: Any, rect: Any, args: argparse.Namespace, text: str, scratch_files: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Sample the block's style (with optional overrides) and pick a font."""
    style = style_for_rect(page, rect)
    style["underline"] = bool(getattr(args, "underline", False))
    if args.font_size:
        style["fontsize"] = args.font_size
    override_color = parse_hex_color(args.color)
    if override_color:
        style["color"] = override_color
    if args.bold and "bold" not in (style.get("rawfont") or "").lower():
        base = strip_subset_prefix(style.get("rawfont") or "Helvetica")
        style["rawfont"] = f"{base.split('-')[0]}-Bold"
        style["fontname"] = builtin_font_alias(style["rawfont"])

    wants_bold = args.bold or "bold" in (style.get("rawfont") or "").lower()
    wants_italic = bool(getattr(args, "italic", False)) \
        or "italic" in (style.get("rawfont") or "").lower() \
        or "oblique" in (style.get("rawfont") or "").lower()
    # Turning bold/italic OFF: the sampled font IS bold/italic, so pick the
    # family's regular/upright face explicitly.
    if getattr(args, "no_bold", False) or getattr(args, "no_italic", False):
        if getattr(args, "no_bold", False):
            wants_bold = False
        if getattr(args, "no_italic", False):
            wants_italic = False
        family = args.font or _family_hint(style.get("rawfont")) or "Helvetica"
        font_choice = resolve_add_text_font(family, wants_bold, wants_italic)
        style["fontname"] = font_choice["fontname"]
        font_kwargs = {"fontname": font_choice["fontname"]}
        if font_choice.get("fontfile"):
            font_kwargs["fontfile"] = font_choice["fontfile"]
        return style, font_kwargs

    # An explicit font family wins over the sampled style: the user chose it.
    if args.font:
        font_choice = resolve_add_text_font(args.font, wants_bold, wants_italic)
        style["fontname"] = font_choice["fontname"]
        style["rawfont"] = args.font
        font_kwargs = {"fontname": font_choice["fontname"]}
        if font_choice.get("fontfile"):
            font_kwargs["fontfile"] = font_choice["fontfile"]
        return style, font_kwargs

    # Italic with no explicit family: derive a matching italic face from the
    # sampled font (the embedded font almost never has an italic variant).
    if getattr(args, "italic", False):
        family = _family_hint(style.get("rawfont")) or "Helvetica"
        font_choice = resolve_add_text_font(family, wants_bold, True)
        style["fontname"] = font_choice["fontname"]
        font_kwargs = {"fontname": font_choice["fontname"]}
        if font_choice.get("fontfile"):
            font_kwargs["fontfile"] = font_choice["fontfile"]
        return style, font_kwargs

    font_choice = resolve_replacement_font(doc, page, style, text, scratch_files)
    font_kwargs = {"fontname": font_choice["fontname"]}
    if font_choice.get("fontfile"):
        font_kwargs["fontfile"] = font_choice["fontfile"]
    return style, font_kwargs


_PITCH_CACHE: dict[tuple, float] = {}


def textbox_line_pitch(font_kwargs: dict[str, Any], size: float) -> float:
    """Baseline pitch insert_textbox produces with lineheight=1 for this font."""
    import fitz

    key = (font_kwargs.get("fontname"), font_kwargs.get("fontfile"), round(size, 2))
    if key not in _PITCH_CACHE:
        pitch = size
        try:
            probe = fitz.open()
            page = probe.new_page()
            page.insert_textbox(fitz.Rect(10, 10, 400, 400), "Ag\nAg", fontsize=size, lineheight=1, **font_kwargs)
            ys = [line["spans"][0]["origin"][1] for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])]
            if len(ys) >= 2 and ys[1] > ys[0]:
                pitch = ys[1] - ys[0]
            probe.close()
        except Exception:
            pass
        _PITCH_CACHE[key] = pitch
    return _PITCH_CACHE[key]


_BASELINE_CACHE: dict[tuple, float] = {}


def textbox_first_baseline(font_kwargs: dict[str, Any], size: float, lineheight: float | None) -> float:
    """Distance from an insert_textbox rect's top to its first baseline, for
    this font/size/line height (measured once on a scratch page)."""
    import fitz

    key = (font_kwargs.get("fontname"), font_kwargs.get("fontfile"), round(size, 2), round(lineheight or 0, 3))
    if key not in _BASELINE_CACHE:
        offset = size * 0.8
        try:
            scratch = fitz.open()
            spage = scratch.new_page(width=600, height=400)
            extra = {"lineheight": lineheight} if lineheight else {}
            spage.insert_textbox(fitz.Rect(10, 100, 590, 390), "Hg", fontsize=size, **font_kwargs, **extra)
            for block in spage.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        offset = float(span["origin"][1]) - 100
                        raise StopIteration
        except StopIteration:
            pass
        except Exception:
            pass
        _BASELINE_CACHE[key] = offset
    return _BASELINE_CACHE[key]


def render_block_text(page: Any, origin_x: float, origin_y: float, width: float, text: str, style: dict[str, Any], font_kwargs: dict[str, Any], line_height: float, expand: float, align: int = 0, color_runs: list | None = None, first_baseline: float | None = None) -> bool:
    """Draw block text with its top-left at (origin_x, origin_y), wrapping at
    the block's width, growing toward the bottom of the page, shrinking only
    if it cannot fit. `align` maps to fitz.TEXT_ALIGN_*; center/right align
    within the block's width box. Draws an underline rule when the resolved
    style asks for it. When `color_runs` is set, each character is drawn in the
    color of the last run covering its global offset (else the style color)."""
    import fitz

    font_size = float(style["fontsize"])
    target = fitz.Rect(
        origin_x,
        origin_y - max(1.5, font_size * 0.1),
        min(origin_x + width + expand, page.rect.x1 - 12),
        page.rect.y1 - 12,
    )
    if first_baseline is not None and not color_runs:
        # Put the first line's baseline exactly where the original one was
        # (the bbox-top heuristic moved headings up 0.1 x size, which reflow
        # then read as a height change and moved everything below).
        lh = None
        if line_height and font_size > 0:
            lh = max(0.75, min(3.0, line_height / textbox_line_pitch(font_kwargs, font_size)))
        target.y0 = first_baseline - textbox_first_baseline(font_kwargs, font_size, lh)
    if color_runs:
        return render_block_text_colored(
            page, target, origin_x, origin_y, text, style, font_kwargs, line_height, align, color_runs
        )
    extra = {}
    if line_height and font_size > 0:
        # insert_textbox spaces lines by fontsize * lineheight * k with a
        # font-dependent k (the ascender for most faces), so measure k.
        natural = textbox_line_pitch(font_kwargs, font_size)
        extra["lineheight"] = max(0.75, min(3.0, line_height / natural))
    size = font_size
    while size >= 6:
        result = page.insert_textbox(
            target,
            text,
            fontsize=size,
            color=style["color"],
            align=align,
            **font_kwargs,
            **extra,
        )
        if result >= 0:
            if style.get("underline"):
                bottom = min(page.rect.y1 - 2, origin_y + max(target.height, size * 1.6 * (text.count("\n") + 1)) + 4)
                draw_text_underlines(page, fitz.Rect(target.x0, target.y0, target.x1, bottom), style.get("color"))
            return True
        size -= 0.5
    return False


def color_run_rgb(color_runs: list, default_color: tuple[float, float, float]) -> list[dict[str, Any]]:
    """Normalize the incoming color-run dicts to {start, length, rgb}, dropping
    any with an unparseable hex. Order is preserved so a later run wins."""
    prepared: list[dict[str, Any]] = []
    for run in color_runs or []:
        try:
            start = int(run["start"])
            length = int(run["length"])
        except (KeyError, TypeError, ValueError):
            continue
        rgb = parse_hex_color(run.get("hex"))
        if rgb is None or length <= 0:
            continue
        prepared.append({"start": start, "length": length, "rgb": rgb})
    return prepared


def _wrap_cells(text: str, font: Any, fontsize: float, max_width: float) -> list[list[tuple[str, int]]]:
    """Greedy word-wrap `text` into visual lines, carrying each character's
    GLOBAL offset in the original string (newlines counted, not drawn) so a
    per-character color lookup stays exact across wrapped lines."""
    lines: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    current_width = 0.0
    last_space = -1  # index in `current` of the last space, for word breaks
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            lines.append(current)
            current, current_width, last_space = [], 0.0, -1
            i += 1
            continue
        w = font.text_length(ch, fontsize=fontsize)
        if current and current_width + w > max_width:
            if last_space >= 0:
                head = current[:last_space]        # drop the breaking space
                tail = current[last_space + 1:]
                lines.append(head)
                current = tail
                current_width = sum(font.text_length(c, fontsize=fontsize) for c, _ in current)
                last_space = -1
            else:
                lines.append(current)
                current, current_width, last_space = [], 0.0, -1
            continue  # re-test the same char against the fresh line
        if ch == " ":
            last_space = len(current)
        current.append((ch, i))
        current_width += w
        i += 1
    lines.append(current)
    return lines


def render_block_text_colored(page: Any, target: Any, origin_x: float, origin_y: float, text: str, style: dict[str, Any], font_kwargs: dict[str, Any], line_height: float, align: int, color_runs: list) -> bool:
    """Manual per-character-color layout: wrap to the box, then draw each line
    as contiguous same-color segments. Used only when color runs are present,
    so the fast single-color insert_textbox path is untouched otherwise."""
    import fitz

    runs = color_run_rgb(color_runs, style["color"])

    def color_at(offset: int) -> tuple[float, float, float]:
        chosen = style["color"]
        for run in runs:
            if run["start"] <= offset < run["start"] + run["length"]:
                chosen = run["rgb"]  # last covering run wins
        return chosen

    try:
        font = fitz.Font(fontfile=font_kwargs["fontfile"]) if font_kwargs.get("fontfile") else fitz.Font(font_kwargs["fontname"])
    except Exception:
        font = fitz.Font("helv")

    box_width = max(1.0, target.x1 - target.x0)
    orig_size = float(style["fontsize"])
    lh_factor = max(0.9, min(2.5, line_height / orig_size)) if (line_height and orig_size > 0) else 1.16
    ascender = font.ascender or 0.8

    size = orig_size
    while size >= 6:
        lines = _wrap_cells(text, font, size, box_width)
        line_step = size * lh_factor
        # First baseline sits one ascender below the box top; ensure the last
        # line still fits above the page's bottom margin before committing.
        first_baseline = origin_y + ascender * size
        last_baseline = first_baseline + line_step * (len(lines) - 1)
        if last_baseline <= page.rect.y1 - 6 or size <= 6:
            baseline = first_baseline
            for cells in lines:
                if cells:
                    line_w = sum(font.text_length(c, fontsize=size) for c, _ in cells)
                    if align == fitz.TEXT_ALIGN_CENTER:
                        x = origin_x + max(0.0, (box_width - line_w) / 2)
                    elif align == fitz.TEXT_ALIGN_RIGHT:
                        x = origin_x + max(0.0, box_width - line_w)
                    else:
                        x = origin_x
                    seg = ""
                    seg_color: tuple[float, float, float] | None = None
                    for ch, idx in cells:
                        col = color_at(idx)
                        if seg and col != seg_color:
                            page.insert_text((x, baseline), seg, fontsize=size, color=seg_color, **font_kwargs)
                            x += font.text_length(seg, fontsize=size)
                            seg = ""
                        seg += ch
                        seg_color = col
                    if seg and seg_color is not None:
                        page.insert_text((x, baseline), seg, fontsize=size, color=seg_color, **font_kwargs)
                baseline += line_step
            if style.get("underline"):
                draw_text_underlines(page, fitz.Rect(target.x0, target.y0, target.x1, min(page.rect.y1 - 2, baseline + 4)), style.get("color"))
            return True
        size -= 0.5
    return False


def render_rtl_block_edit(doc: Any, page: Any, args: argparse.Namespace, style: dict[str, Any],
                          per_line_lines: list[dict[str, Any]], new_lines: list[str], new_text: str,
                          render_rect: Any, resized: bool, color_runs: list | None,
                          strict: bool = False) -> bool | None:
    """Draw replacement text containing Arabic/Hebrew: shaped, bidi-ordered,
    right-aligned by default, logical text in the PDF.

    `per_line_lines` (same count as new_lines) replays each line at its own
    baseline and size, anchored at its RIGHT edge for RTL lines (left edge for
    LTR lines) so an edited word doesn't move the rest of the paragraph.
    Otherwise the text reflows inside render_rect with the block style."""
    import fitz

    rt = rtl_module()
    overall_rtl = rt.paragraph_is_rtl(new_text)
    if per_line_lines:
        plans = []
        for line, text_line in zip(per_line_lines, new_lines):
            if not text_line.strip():
                continue
            raw = line.get("rawfont") or ""
            font = rtl_font_for(doc, page, text_line, raw, "bold" in raw.lower())
            if font is None:
                plans = None
                break
            size = float(line["fontsize"])
            width = sum(l.width for l in rt.layout_text(text_line, font, size, None))
            line_rtl = rt.paragraph_direction(text_line, overall_rtl)
            if line_rtl:
                right = float(line["bbox"][2])
                if right - width < page.rect.x0 + 8:
                    plans = None
                    break
                plans.append((font, text_line, size, line["color"], line["origin"][1], None, right, True))
            else:
                left = float(line["origin"][0])
                if left + width > page.rect.x1 - 8:
                    plans = None
                    break
                plans.append((font, text_line, size, line["color"], line["origin"][1], left, None, False))
        if plans is not None:
            for font, text_line, size, color, baseline, left, right, line_rtl in plans:
                draw_rtl_line(page, font, text_line, size, color, baseline, left=left, right=right, rtl=line_rtl)
            return True
        if strict:
            # Only the changed lines were erased: the caller must erase the
            # rest before a whole-paragraph re-layout.
            return None

    bold = bool(args.bold) or "bold" in (style.get("rawfont") or "").lower()
    font = rtl_font_for(doc, page, new_text, style.get("rawfont"), bold, family=args.font)
    if font is None:
        return False
    align = getattr(args, "align", "auto") or "auto"
    box = fitz.Rect(render_rect)
    if not resized and args.expand:
        # Same headroom the LTR path adds, on the side the text grows toward.
        grows_left = align == "right" or (align == "auto" and rt.paragraph_is_rtl(new_text))
        if grows_left:
            box.x0 = max(page.rect.x0 + 12, box.x0 - args.expand)
        else:
            box.x1 = min(page.rect.x1 - 12, box.x1 + args.expand)
    result = rt.draw_paragraph(
        page, box, new_text, font, float(style["fontsize"]), style["color"],
        align=align, line_height=args.line_height, colors=rtl_colors(new_text, color_runs),
    )
    if result is None:
        return False
    if style.get("underline"):
        draw_text_underlines(page, fitz.Rect(result["rect"]) + (-1, -1, 1, 4), style.get("color"))
    return True


# --- Minimal ("surgical") block edits ----------------------------------------
# An edit must change ONLY what the user changed. Untouched lines are never
# erased or redrawn (they keep the producer's exact kerning, justification,
# fonts and colors); on a changed line the common prefix stays as-is, the
# changed middle is drawn in the style of the characters it replaces, and the
# common suffix is replayed glyph-by-glyph (each glyph's own font, size,
# color and spacing) shifted only as far as the middle grew or shrank.

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
              "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}


class EditTargetMissing(Exception):
    """The paragraph named by --original-text is no longer at --rect."""


def decompose_ligatures(text: str) -> str:
    """ﬁ/ﬂ/… as plain letters: base-14 and most substitute fonts have no
    ligature glyphs (they drew "·"), and the letters re-ligate visually."""
    return "".join(_LIGATURES.get(ch, ch) for ch in text)


class Frame:
    """Coordinates of a text block whose lines run along an axis direction d
    (unrotated page space), turned so the text runs along +x — the minimal
    edit logic then works unchanged. `angle` is insert_text's rotate value
    that draws text in direction d (verified: (0,-1)->90, (-1,0)->180,
    (0,1)->270), which is what makes text look upright on /Rotate pages."""

    _MAPS = {
        (1, 0): (0, lambda x, y: (x, y), lambda u, v: (u, v)),
        (0, -1): (90, lambda x, y: (-y, x), lambda u, v: (v, -u)),
        (-1, 0): (180, lambda x, y: (-x, -y), lambda u, v: (-u, -v)),
        (0, 1): (270, lambda x, y: (y, -x), lambda u, v: (-v, u)),
    }

    def __init__(self, direction: tuple[int, int]):
        self.angle, self._to, self._back = self._MAPS[direction]
        self.identity = direction == (1, 0)

    @classmethod
    def for_lines(cls, lines: list[dict[str, Any]]) -> "Frame | None":
        dirs = set()
        for line in lines:
            d = line.get("dir") or (1.0, 0.0)
            snapped = (round(d[0]), round(d[1]))
            if snapped not in cls._MAPS or abs(d[0] - snapped[0]) > 0.01 or abs(d[1] - snapped[1]) > 0.01:
                return None
            dirs.add(snapped)
        return cls(dirs.pop()) if len(dirs) == 1 else None

    def pt(self, p: Any) -> tuple[float, float]:
        return self._to(float(p[0]), float(p[1]))

    def back(self, x: float, y: float) -> tuple[float, float]:
        return self._back(x, y)

    def rect(self, r: Any) -> Any:
        import fitz

        a = self._to(float(r[0]), float(r[1]))
        b = self._to(float(r[2]), float(r[3]))
        return fitz.Rect(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))

    def rect_back(self, r: Any) -> Any:
        import fitz

        a = self._back(float(r[0]), float(r[1]))
        b = self._back(float(r[2]), float(r[3]))
        return fitz.Rect(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))

    def lines(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copies of the lines in this frame. Each char keeps its page-space
        box/origin (for erasing) under _page_bbox/_page_origin."""
        if self.identity:
            return lines
        out = []
        for line in lines:
            spans = []
            for span in line.get("spans") or []:
                chars = []
                for ch in span.get("chars", []):
                    copy = dict(ch)
                    if ch.get("bbox") is not None:
                        copy["bbox"] = tuple(self.rect(ch["bbox"]))
                        copy["_page_bbox"] = ch["bbox"]
                    if ch.get("origin") is not None:
                        copy["origin"] = self.pt(ch["origin"])
                        copy["_page_origin"] = ch["origin"]
                    chars.append(copy)
                span_copy = dict(span)
                span_copy["chars"] = chars
                if span.get("bbox") is not None:
                    span_copy["bbox"] = tuple(self.rect(span["bbox"]))
                spans.append(span_copy)
            copy = dict(line)
            copy["spans"] = spans
            copy["bbox"] = list(self.rect(line["bbox"]))
            copy["origin"] = self.pt(line["origin"]) if line.get("origin") else None
            copy["dir"] = (1.0, 0.0)
            out.append(copy)
        return out


def line_char_list(line: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The line's glyphs in text order, each with its span (style). None when
    they don't spell the line's text exactly (RTL rows, repaired fonts)."""
    chars: list[dict[str, Any]] = []
    for span in line.get("spans") or []:
        for ch in span.get("chars", []):
            if ch.get("bbox") is None or ch.get("origin") is None:
                return None
            chars.append({"c": ch.get("c", ""), "bbox": ch["bbox"], "origin": ch["origin"], "span": span,
                          "page_bbox": ch.get("_page_bbox", ch["bbox"])})
    if not chars or "".join(c["c"] for c in chars) != line.get("text"):
        return None
    return chars


def _glyph_key(bbox: Any) -> tuple[float, ...]:
    return tuple(round(float(v), 2) for v in bbox)


def page_glyph_boxes(page: Any) -> list[Any]:
    import fitz

    boxes = []
    for block in text_dict(page).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    if ch.get("bbox"):
                        boxes.append(fitz.Rect(ch["bbox"]))
    return boxes


def glyph_erase_rect(bbox: Any, baseline: float, size: float, foreign: list[Any]) -> Any:
    """A redaction rect that removes this glyph and NO other: MuPDF deletes
    every glyph whose box the rect touches, and glyph boxes of tightly leaded
    lines overlap. Take the glyph's horizontal core and the part of its
    ascent band that no foreign glyph box covers. None if nothing is free."""
    import fitz

    rect = fitz.Rect(bbox)
    width = rect.width
    x0, x1 = rect.x0 + 0.2 * width, rect.x1 - 0.2 * width
    if x1 - x0 < 0.2:
        x0, x1 = rect.x0 + 0.05 * width, rect.x1 - 0.05 * width
    if x1 <= x0:
        return None
    free = [(baseline - 0.7 * size, baseline - 0.05 * size)]
    for other in foreign:
        if other.x1 <= x0 or other.x0 >= x1:
            continue
        nxt = []
        for lo, hi in free:
            if other.y1 <= lo or other.y0 >= hi:
                nxt.append((lo, hi))
                continue
            if other.y0 > lo:
                nxt.append((lo, other.y0 - 0.05))
            if other.y1 < hi:
                nxt.append((other.y1 + 0.05, hi))
        free = nxt
    free = [iv for iv in free if iv[1] - iv[0] > 0.3]
    if not free:
        return None
    lo, hi = max(free, key=lambda iv: iv[1] - iv[0])
    return fitz.Rect(x0, lo, x1, hi)


def erase_glyphs(page: Any, targets: list[dict[str, Any]], frame: "Frame | None" = None) -> int:
    """Remove exactly these glyphs. No white fill (shading, highlights and
    white-on-color text survive), no vector art removed (rules, underlines,
    table borders), links and font resources preserved."""
    import fitz

    if not targets:
        return 0
    keys = {_glyph_key(t.get("page_bbox", t["bbox"])) for t in targets}
    foreign = [box for box in page_glyph_boxes(page) if _glyph_key(box) not in keys]
    if frame is not None and not frame.identity:
        # Work in the text's own frame (ascent band above its baseline), then
        # map the rect back to the page.
        foreign = [frame.rect(box) for box in foreign]
    rects = []
    for target in targets:
        size = float(target["span"].get("size") or 10.0)
        rect = glyph_erase_rect(target["bbox"], float(target["origin"][1]), size, foreign)
        if rect is not None:
            rects.append(frame.rect_back(rect) if frame is not None and not frame.identity else rect)
    reflow_module()._redact(page, rects, images=fitz.PDF_REDACT_IMAGE_NONE,
                            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                            text=fitz.PDF_REDACT_TEXT_REMOVE, preserve=True)
    return len(rects)


class FontPicker:
    """Per-run font choice: the span's own embedded font when it has every
    glyph the run needs, else the closest system/base-14 face — decided per
    RUN, so one new letter never re-fonts a whole paragraph."""

    def __init__(self, doc: Any, page: Any, scratch: list[str]):
        self.doc, self.page, self.scratch = doc, page, scratch
        self.cache: dict[tuple[str, frozenset], dict[str, Any]] = {}

    _BASE14 = {"helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
               "times-roman", "times-bold", "times-italic", "times-bolditalic",
               "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
               "symbol", "zapfdingbats"}

    def exact(self, rawfont: str, text: str) -> bool:
        """True when redrawing `text` reproduces the original glyphs exactly:
        the span's own embedded font covers it, or it IS a base-14 font."""
        kwargs = self.kwargs(rawfont, text)
        if str(kwargs.get("fontname", "")).startswith("FolioE"):
            return True
        return not kwargs.get("fontfile") and strip_subset_prefix(rawfont or "").lower() in self._BASE14

    def font(self, kwargs: dict[str, Any]) -> Any:
        import fitz

        key = (kwargs.get("fontname"), kwargs.get("fontfile"))
        cache = self.__dict__.setdefault("_fonts", {})
        if key not in cache:
            try:
                cache[key] = fitz.Font(fontfile=kwargs["fontfile"]) if kwargs.get("fontfile") else fitz.Font(kwargs["fontname"])
            except Exception:
                cache[key] = None
        return cache[key]

    def unscaled(self, chars: list[dict[str, Any]]) -> bool:
        """The glyphs were drawn without horizontal scaling/condensing: each
        glyph box is as wide as the font's own advance at that size (a
        replay with insert_text draws upright, unscaled glyphs)."""
        checked = 0
        for ch in chars:
            c = ch["c"]
            if not c.strip() or len(c) != 1:
                continue
            span = ch["span"]
            font = self.font(self.kwargs(span.get("font") or "", _span_text(span)))
            if font is None:
                return False
            advance = font.glyph_advance(ord(c)) * _span_size(span)
            width = float(ch["bbox"][2]) - float(ch["bbox"][0])
            if advance > 0.5 and abs(width - advance) > max(0.35, 0.06 * advance):
                return False
            checked += 1
            if checked >= 40:
                break
        return True

    def kwargs(self, rawfont: str, text: str) -> dict[str, Any]:
        key = (rawfont or "", frozenset(text))
        if key not in self.cache:
            style = {"fontname": builtin_font_alias(rawfont), "fontsize": 11, "color": (0, 0, 0), "rawfont": rawfont}
            self.cache[key] = resolve_replacement_font(self.doc, self.page, style, text, self.scratch)
        return self.cache[key]


_FONT_OBJECTS: dict[tuple, Any] = {}


def _font_object(kwargs: dict[str, Any]) -> Any:
    import fitz

    key = (kwargs.get("fontname"), kwargs.get("fontfile"))
    if key not in _FONT_OBJECTS:
        try:
            _FONT_OBJECTS[key] = (fitz.Font(fontfile=kwargs["fontfile"]) if kwargs.get("fontfile")
                                  else fitz.Font(kwargs["fontname"]))
        except Exception:
            _FONT_OBJECTS[key] = None
    return _FONT_OBJECTS[key]


_TOUNICODE_FIXES = {"00AD": "002D", "00A0": "0020"}


def fix_inserted_tounicode(page: Any) -> int:
    """Correct the ToUnicode maps of the fonts THIS engine inserted (resource
    names Folio*). MuPDF builds them from the font's cmap, and a glyph shared
    by "-" and U+00AD (or " " and U+00A0) comes back as the soft hyphen /
    no-break space — "10-" copied as "10" + soft hyphen. Map those glyphs to
    the plain characters. Returns the number of entries fixed."""
    doc = page.parent
    fixed = 0
    seen: set[int] = set()
    for entry in page.get_fonts(full=True):
        xref, refname = entry[0], entry[4]
        if not str(refname).startswith("Folio") or xref in seen:
            continue
        seen.add(xref)
        kind, value = doc.xref_get_key(xref, "ToUnicode")
        if kind != "xref":
            continue
        try:
            tu = int(value.split()[0])
            text = doc.xref_stream(tu).decode("latin-1")
        except Exception:
            continue

        def swap(match: Any) -> str:
            nonlocal fixed
            dest = match.group(2).upper()
            if dest in _TOUNICODE_FIXES:
                fixed += 1
                return f"{match.group(1)}<{_TOUNICODE_FIXES[dest]}>"
            return match.group(0)

        # bfchar "<gid> <00AD>" and single-glyph bfrange "<a> <a> <00AD>" only:
        # a multi-glyph range's destination is its FIRST code, and rewriting
        # it would shift the whole range.
        def swap_single(match: Any) -> str:
            codes = re.findall(r"<([0-9A-Fa-f]+)>", match.group(1))
            if len(codes) == 2 and codes[0].lower() != codes[1].lower():
                return match.group(0)
            return swap(match)

        new_text = re.sub(r"((?:<[0-9A-Fa-f]+>[ \t]*){1,2})<([0-9A-Fa-f]{4})>", swap_single, text)
        if new_text != text:
            doc.update_stream(tu, new_text.encode("latin-1"))
    return fixed


def _space_safe(kwargs: dict[str, Any]) -> bool:
    """Spaces can be drawn as glyphs: the font has its own space glyph, not
    shared with U+00A0 (a shared glyph comes back as a non-breaking space in
    the text layer)."""
    font = _font_object(kwargs)
    if font is None:
        return False
    space = font.has_glyph(0x20)
    return bool(space) and space != font.has_glyph(0xA0)


def _glyph_text(kwargs: dict[str, Any], c: str) -> str:
    """A ligature character stays itself only if the font maps it; else its
    letters (what the original text layer usually says anyway)."""
    if len(c) == 1 and ord(c) in range(0xFB00, 0xFB07):
        font = _font_object(kwargs)
        if font is None or not font.has_glyph(ord(c)):
            return decompose_ligatures(c)
    return c


def _span_size(span: dict[str, Any]) -> float:
    return float(span.get("size") or 11.0)


def _draw(page: Any, x: float, y: float, text: str, kwargs: dict[str, Any], size: float, color: Any,
          frame: "Frame | None" = None) -> float:
    """Draw `text` at (x, baseline y). Spaces are not drawn as glyphs: words
    go at their exact positions and text extraction infers the spaces from
    the gaps. (Fonts mapping U+0020 and U+00A0 to one glyph otherwise came
    back as non-breaking spaces, breaking search and copy.)"""
    insert = {"fontname": kwargs["fontname"]}
    if kwargs.get("fontfile"):
        insert["fontfile"] = kwargs["fontfile"]
    if frame is not None and not frame.identity:
        insert["rotate"] = frame.angle

    def put(px: float, piece: str) -> None:
        point = frame.back(px, y) if frame is not None and not frame.identity else (px, y)
        page.insert_text(point, piece, fontsize=size, color=color, **insert)

    if " " not in text or _space_safe(kwargs):
        put(x, text)
        return replacement_text_length(text, kwargs, size)
    cursor = x
    for index, word in enumerate(text.split(" ")):
        if index:
            cursor += replacement_text_length(" ", kwargs, size)
        if word:
            put(cursor, word)
            cursor += replacement_text_length(word, kwargs, size)
    return cursor - x


def _text_for_font(picker: FontPicker, rawfont: str, text: str) -> tuple[str, dict[str, Any]]:
    """Text as drawn plus its font: ligature characters stay as-is when the
    span's own embedded font has them (identical glyphs, and the text layer
    keeps "ﬁnal"); only substitute fonts get the decomposed letters."""
    kwargs = picker.kwargs(rawfont, text)
    if text == decompose_ligatures(text):
        return text, kwargs
    font = _font_object(kwargs)
    if font is not None and all(font.has_glyph(ord(c)) for c in set(text) if 0xFB00 <= ord(c) <= 0xFB06):
        return text, kwargs
    plain = decompose_ligatures(text)
    return plain, picker.kwargs(rawfont, plain)


def _span_text(span: dict[str, Any]) -> str:
    return "".join(c.get("c", "") for c in span.get("chars", [])) or " "


def _replay_ops(chars: list[dict[str, Any]], picker: FontPicker, dx: float = 0.0, dy: float = 0.0) -> list[tuple]:
    """Glyph-by-glyph redraw at the original positions (+dx/dy), each glyph
    in its own span's font, size and color."""
    ops = []
    for ch in chars:
        span = ch["span"]
        kwargs = picker.kwargs(span.get("font") or "", _span_text(span))
        if not ch["c"].strip() and not (ch["c"] == " " and _space_safe(kwargs)):
            continue
        glyph = _glyph_text(kwargs, ch["c"])
        ops.append((float(ch["origin"][0]) + dx, float(ch["origin"][1]) + dy, glyph,
                    kwargs, _span_size(span), pdf_color_to_rgb(span.get("color"))))
    return ops


def _chars_exact(chars: list[dict[str, Any]], picker: FontPicker) -> bool:
    # Whitespace never needs an exact glyph: a space is only drawn when its
    # font has a distinct space glyph, otherwise extraction infers it from the
    # gap. (A bullet's trailing space in a non-embedded font blocked replay.)
    spans = {id(c["span"]): c["span"] for c in chars if c["c"].strip()}
    return (all(picker.exact(span.get("font") or "", _span_text(span)) for span in spans.values())
            and picker.unscaled(chars))


def plan_line_edit(line: dict[str, Any], new_text: str, picker: FontPicker, right_limit: float,
                   justified: bool, dy: float = 0.0, replay: bool = False,
                   snap_ok: bool = False) -> dict[str, Any] | None:
    """Erase/draw operations turning `line` into `new_text` while touching only
    what changed. `dy` moves the whole line vertically (lines below a line
    count change). Returns None when the result would overflow `right_limit`
    (the caller re-wraps instead)."""
    chars = line_char_list(line)
    if chars is None:
        return None
    old = line["text"]
    ops: dict[str, Any] = {"erase": [], "draw": [], "snap": [], "right": max(c["bbox"][2] for c in chars)}
    if new_text == old and abs(dy) < 0.01:
        if replay and _chars_exact(chars, picker):
            # Unchanged line after an edited one: replayed (identical glyphs,
            # identical positions) so the paragraph keeps its reading order in
            # the content stream — text extraction, search and copy see the
            # lines in order rather than the edited line last.
            ops["erase"].extend(chars)
            ops["draw"].extend(_replay_ops(chars, picker))
        return ops
    moving = abs(dy) >= 0.01
    n_old, n_new = len(old), len(new_text)
    p = 0
    if not moving:
        while p < min(n_old, n_new) and old[p] == new_text[p]:
            p += 1
    q = 0
    while q < min(n_old, n_new) - p and old[n_old - 1 - q] == new_text[n_new - 1 - q]:
        q += 1
    # p/q are TEXT positions; a glyph can carry several characters (a "fi"
    # ligature reads as "fi") or none. Snap both to whole glyphs: g_pre glyphs
    # are untouched before the change, g_suf after it; a ligature straddling
    # the change becomes part of the changed middle. (Indexing glyphs by text
    # position shifted everything after a ligature: "high demand" -> "hemand".)
    ends: list[int] = []
    position = 0
    for ch in chars:
        position += len(ch["c"])
        ends.append(position)
    starts = [end - len(ch["c"]) for end, ch in zip(ends, chars)]
    g_pre = sum(1 for end in ends if end <= p)
    g_suf = sum(1 for begin in starts if begin >= n_old - q)
    g_suf = min(g_suf, len(chars) - g_pre)
    p = ends[g_pre - 1] if g_pre else 0
    q = n_old - starts[len(chars) - g_suf] if g_suf else 0
    g_mid_end = len(chars) - g_suf
    # An edited line is redrawn as ONE contiguous line in the content stream
    # (prefix, new text, suffix — the latter two after the former), else text
    # extraction / search / copy read "% discount -7". Only with the glyphs'
    # own fonts, so nothing changes visually.
    # The line can be replayed exactly from `start` on: everything after the
    # last glyph that can't be (a symbol-font bullet, a non-embedded font).
    # Glyphs before it stay where they are.
    start = 0
    for index in range(len(chars) - 1, -1, -1):
        ch = chars[index]
        if ch["c"].strip() and not _chars_exact([ch], picker):
            start = index + 1
            break
    if start:
        # Glyphs left in place keep the whitespace right after them (a bullet
        # and its space): erasing that space glued "" to the next word.
        while start < g_pre and not chars[start]["c"].strip():
            start += 1
    whole_line = start <= g_pre and _chars_exact(chars[start:], picker)
    if start < g_pre and whole_line:
        # Keep the edited line ONE run in the text layer: the unchanged
        # prefix (from `start`) is replayed exactly, right before the new text.
        ops["erase"].extend(chars[start:g_pre])
        ops["draw"].extend(_replay_ops(chars[start:g_pre], picker))
    middle_raw = new_text[p:n_new - q]
    if g_pre < g_mid_end:
        style_char = chars[g_pre]
    elif g_pre > 0:
        style_char = chars[g_pre - 1]
    else:
        style_char = chars[0]
    x = chars[g_pre]["origin"][0] if g_pre < len(chars) else chars[-1]["bbox"][2]
    ops["erase"].extend(chars[g_pre:g_mid_end])
    right = chars[g_pre - 1]["bbox"][2] if g_pre > 0 else x
    if middle_raw.strip() or (middle_raw and q):
        span = style_char["span"]
        size = _span_size(span)
        middle, _kwargs = _text_for_font(picker, span.get("font") or "", middle_raw)
        y = float(style_char["origin"][1]) + dy
        run_ops, width = _run_ops(picker, span.get("font") or "", middle, x, y, size,
                                  pdf_color_to_rgb(span.get("color")))
        ops["draw"].extend(run_ops)
        x += width
        right = max(right, x)
    suffix = chars[g_mid_end:]
    if suffix:
        shift = x - float(suffix[0]["origin"][0])
        if abs(shift) < 0.02:
            shift = 0.0
        shifts = [shift] * len(suffix)
        if shift and justified:
            # Justified line: absorb the change in the word gaps so the right
            # edge stays put (the last word doesn't move at all).
            spaces = [i for i, c in enumerate(suffix) if c["c"] == " "]
            if spaces:
                m = len(spaces)
                gaps_ok = True
                word = 0
                for i, ch in enumerate(suffix):
                    if word < m and i > spaces[word]:
                        word += 1
                    shifts[i] = shift * (1 - word / m)
                if shift > 0:
                    min_gap = min(
                        float(suffix[s + 1]["origin"][0]) - float(suffix[s - 1]["bbox"][2]) if 0 < s < len(suffix) - 1 else 99.0
                        for s in spaces
                    )
                    gaps_ok = min_gap - shift / m > 0.12 * _span_size(suffix[0]["span"])
                if not gaps_ok:
                    shifts = [shift] * len(suffix)
        for ch, s_x in zip(suffix, shifts):
            if abs(s_x) < 0.02 and abs(dy) < 0.01 and not whole_line:
                right = max(right, ch["bbox"][2])
                continue
            ops["erase"].append(ch)
            span = ch["span"]
            kwargs = picker.kwargs(span.get("font") or "", _span_text(span))
            if snap_ok and ch["c"].strip() and not _chars_exact([ch], picker):
                # Can't be redrawn exactly (a symbol-font bullet): move the
                # original glyph itself as a snapshot.
                ops["snap"].append((ch, s_x, dy))
            elif ch["c"].strip() or (ch["c"] == " " and _space_safe(kwargs)):
                glyph = _glyph_text(kwargs, ch["c"])
                ops["draw"].append((float(ch["origin"][0]) + s_x, float(ch["origin"][1]) + dy,
                                    glyph, kwargs, _span_size(span),
                                    pdf_color_to_rgb(span.get("color"))))
            right = max(right, ch["bbox"][2] + s_x)
    ops["right"] = right
    if right > right_limit + 1.0:
        return None
    return ops


def _run_ops(picker: FontPicker, rawfont: str, text: str, x: float, y: float, size: float,
             color: Any) -> tuple[list[tuple], float]:
    """Draw ops for `text` in the span's own font, falling back PER RUN only
    for characters that font lacks (a curly apostrophe or a bullet from
    another font no longer drags a whole line into a substitute font, which
    drew them as "·"). Returns (ops, width)."""
    # Per-character fallback is disabled for now: it flipped bold/regular on
    # some documents. One font choice for the whole run, as before.
    kwargs = picker.kwargs(rawfont, text)
    return [(x, y, text, kwargs, size, color)], replacement_text_length(text, kwargs, size)
    primary = picker.kwargs(rawfont, "")
    font = _font_object(primary)
    runs: list[tuple[dict[str, Any], str]] = []
    for ch in text:
        covered = ch == " " or (font is not None and font.has_glyph(ord(ch)))
        kwargs = primary if covered else picker.kwargs(rawfont, ch)
        if runs and runs[-1][0] is kwargs:
            runs[-1] = (kwargs, runs[-1][1] + ch)
        else:
            runs.append((kwargs, ch))
    ops, cursor = [], x
    for kwargs, piece in runs:
        piece_text = _glyph_text(kwargs, piece) if len(piece) == 1 else piece
        ops.append((cursor, y, piece_text, kwargs, size, color))
        cursor += replacement_text_length(piece_text, kwargs, size)
    return ops, cursor - x


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _wrap_words(text: str, kwargs: dict[str, Any], size: float, first_width: float, width: float) -> list[str]:
    """Greedy word wrap; the first line may be narrower (indent)."""
    lines: list[str] = []
    current = ""
    limit = first_width
    for word in text.split(" "):
        candidate = word if not current else current + " " + word
        if current and replacement_text_length(candidate, kwargs, size) > limit:
            lines.append(current)
            current = word
            limit = width
        else:
            current = candidate
    lines.append(current)
    return lines


def minimal_block_edit(doc: Any, page: Any, block_lines: list[dict[str, Any]], new_text: str,
                       rect: Any, args: argparse.Namespace, scratch_files: list[str],
                       before_render: Any = None, render_rect: Any = None) -> dict[str, Any] | None:
    """Apply the edit touching only what changed. Returns the apply_block_edit
    result (plus old/new bottom for reflow), or None when this path can't
    express the edit (caller falls back to re-laying the block)."""
    import fitz

    # Text running along any axis (upright text on a /Rotate page runs
    # vertically in page space) is handled in its own frame; slanted text
    # falls back to the general path.
    frame = Frame.for_lines(block_lines)
    if frame is None:
        return None
    page_rect = frame.rect(page.rect)
    lines = sorted(frame.lines(block_lines), key=lambda line: (float(line["origin"][1]), float(line["origin"][0])))
    # Reading order = the order of the paragraph's own text (page order is
    # reversed for upside-down text); duplicates are claimed in frame order.
    wanted = (getattr(args, "original_text", None) or "").split("\n")
    if len(wanted) == len(lines):
        claimed: list[int] = []
        for text in wanted:
            index = next((i for i, line in enumerate(lines) if i not in claimed and line["text"] == text), None)
            if index is None:
                claimed = []
                break
            claimed.append(index)
        if claimed:
            lines = [lines[i] for i in claimed]
    # Text order running AGAINST the frame (upside-down text is listed in
    # page order, bottom line first): work in frame order and read the new
    # text's lines in the same reversed order, so pitch and wrapping go the
    # right way.
    reverse = (len(lines) >= 2 and all(float(a["origin"][1]) > float(b["origin"][1]) + 0.5
                                       for a, b in zip(lines, lines[1:])))
    if reverse:
        lines = lines[::-1]
    if any(line_char_list(line) is None for line in lines):
        return None
    picker = FontPicker(doc, page, scratch_files)

    # Unchanged lines are replayed (for reading order) only when EVERY segment
    # on their row can be replayed exactly: replaying half a row ("Know when
    # to retire" but not a non-embedded "10-") splits it in the text layer.
    row_keys: list[float] = []

    def row_key(line: dict[str, Any]) -> float:
        # Segments of one row can sit on slightly different baselines (mixed
        # fonts): cluster within 1pt.
        y = float(line["origin"][1])
        for key in row_keys:
            if abs(key - y) <= 1.0:
                return key
        row_keys.append(y)
        return y

    row_exact: dict[float, bool] = {}
    for line in lines:
        key = row_key(line)
        row_exact[key] = row_exact.get(key, True) and _chars_exact(line_char_list(line) or [], picker)

    def may_replay(line: dict[str, Any]) -> bool:
        return row_exact.get(row_key(line), False)
    old_texts = [line["text"] for line in lines]
    new_lines = new_text.split("\n")
    if reverse:
        new_lines = new_lines[::-1]
    baselines = [float(line["origin"][1]) for line in lines]
    sizes = [_span_size(line_char_list(line)[0]["span"]) for line in lines]
    if len(lines) >= 2:
        pitch = _median([b - a for a, b in zip(baselines, baselines[1:]) if b - a > 0.5]) if any(
            b - a > 0.5 for a, b in zip(baselines, baselines[1:])) else sizes[0] * 1.2
    else:
        pitch = float(args.line_height) if getattr(args, "line_height", 0) else sizes[0] * 1.2
    rights = [max(c["bbox"][2] for c in line_char_list(line)) for line in lines]
    block_right = max(rights)
    lefts = [float(line_char_list(line)[0]["origin"][0]) for line in lines]
    column_left = min(lefts[1:]) if len(lefts) > 1 else lefts[0]
    single = len(lines) == 1
    # Single-line text (a heading, a label) may grow to the right; a
    # paragraph wraps at its own column edge.
    right_limit = (min(page_rect.x1 - 18, block_right + float(getattr(args, "expand", 0) or 0))
                   if single else block_right)
    if single:
        foreign = [box for box in (frame.rect(b) for b in page_glyph_boxes(page))
                   if box.x0 > block_right + 0.5
                   and box.y1 > lines[0]["bbox"][1] + 0.5 and box.y0 < lines[0]["bbox"][3] - 0.5]
        if foreign:
            right_limit = min(right_limit, min(box.x0 for box in foreign) - 2.0)

    def is_justified(index: int) -> bool:
        return (len(lines) >= 2 and index < len(lines) - 1
                and rights[index] >= block_right - 1.5 and " " in old_texts[index])

    def is_soft(index: int) -> bool:
        # The line ran to the column edge: the paragraph continues on the next
        # line (a soft wrap), as opposed to a hard break.
        width = max(1.0, block_right - column_left)
        return index < len(lines) - 1 and rights[index] >= block_right - max(3 * sizes[index], 0.12 * width)

    # A right-aligned column value (an invoice amount) keeps its RIGHT edge:
    # growing from the left edge would push it out of its column.
    if (frame.identity and single and len(new_lines) == 1 and new_text != old_texts[0] and new_text.strip()
            and float(rect.x0) > 0.55 * page.rect.width and len(new_text.strip()) <= 32
            and right_aligned_evidence(page, rect, old_texts[0])):
        chars = line_char_list(lines[0])
        span = next((c["span"] for c in chars if c["c"].strip()), chars[0]["span"])
        size = _span_size(span)
        text = decompose_ligatures(new_text)
        kwargs = picker.kwargs(span.get("font") or "", text)
        width = replacement_text_length(text, kwargs, size)
        x = max(page.rect.x0 + 2, rights[0] - width)
        erase_glyphs(page, chars)
        render = render_rect or rect
        if before_render is not None:
            before_render(page, {"block_lines": block_lines, "render_rect": render})
        _draw(page, x, baselines[0], text, kwargs, size, pdf_color_to_rgb(span.get("color")))
        bottom = float(lines[0]["bbox"][3])
        return {"inserted": True, "block_lines": block_lines, "render_rect": render,
                "old_bottom": bottom, "new_bottom": bottom, "minimal": True}

    k = 0
    while k < min(len(old_texts), len(new_lines)) and old_texts[k] == new_lines[k]:
        k += 1
    if k == len(old_texts) == len(new_lines):
        return {"inserted": True, "block_lines": block_lines, "render_rect": render_rect or rect,
                "old_bottom": float(lines[-1]["bbox"][3]), "new_bottom": float(lines[-1]["bbox"][3])}
    t = 0
    while (t < min(len(old_texts), len(new_lines)) - k
           and old_texts[len(old_texts) - 1 - t] == new_lines[len(new_lines) - 1 - t]):
        t += 1
    old_mid = list(range(k, len(lines) - t))
    new_mid = new_lines[k:len(new_lines) - t]

    erase: list[dict[str, Any]] = []
    draw: list[tuple] = []
    snaps: list[tuple[dict[str, Any], float, float]] = []   # glyph, dx, dy
    line_delta = 0
    plans = []
    pairwise_ok = True
    for offset, index in enumerate(old_mid[:len(new_mid)]):
        plan = plan_line_edit(lines[index], new_mid[offset], picker, right_limit, is_justified(index),
                              replay=may_replay(lines[index]), snap_ok=False)
        if plan is None:
            pairwise_ok = False
            break
        plans.append(plan)

    if pairwise_ok:
        for plan in plans:
            erase.extend(plan["erase"])
            draw.extend(plan["draw"])
            snaps.extend(plan["snap"])
        # Lines the user deleted.
        for index in old_mid[len(new_mid):]:
            erase.extend(line_char_list(lines[index]))
        # Lines the user added: below the last kept middle line, in its style.
        extra = new_mid[len(old_mid):]
        if extra:
            anchor = old_mid[-1] if old_mid else max(k - 1, 0)
            anchor_chars = line_char_list(lines[anchor])
            span = anchor_chars[0]["span"]
            size = _span_size(span)
            color = pdf_color_to_rgb(span.get("color"))
            base_y = baselines[anchor] if old_mid or k > 0 else baselines[0] - pitch
            x = column_left if (old_mid or k > 0) else lefts[0]
            for n, text in enumerate(extra, start=1):
                text = decompose_ligatures(text)
                if text.strip():
                    kwargs = picker.kwargs(span.get("font") or "", text)
                    if x + replacement_text_length(text, kwargs, size) > right_limit + 1.0:
                        pairwise_ok = False
                        break
                    draw.append((x, base_y + n * pitch, text, kwargs, size, color))
        line_delta = len(new_mid) - len(old_mid)

    if not pairwise_ok:
        # Re-wrap the changed region at the column width. Extend it over the
        # rest of the paragraph while lines are soft-wrapped, so no orphan
        # fragments are left behind.
        erase, draw = [], []
        end = len(lines) - t
        new_end = len(new_lines) - t
        while end < len(lines) and end > 0 and is_soft(end - 1):
            end += 1
            new_end += 1
        region_old = list(range(k, end))
        region_new = new_lines[k:new_end]
        if not region_old:
            return None
        paragraphs: list[tuple[str, int]] = []   # (text, old line it starts on)
        current, current_start = "", k
        for offset, text in enumerate(region_new):
            if not current:
                current_start = min(k + offset, end - 1)
            current = text if not current else current.rstrip() + " " + text.lstrip()
            old_index = k + offset
            soft = old_index < end - 1 and is_soft(old_index)
            if not soft:
                paragraphs.append((current, current_start))
                current = ""
        if current:
            paragraphs.append((current, current_start))
        first_chars = line_char_list(lines[k])
        span = first_chars[0]["span"]
        size = _span_size(span)
        color = pdf_color_to_rgb(span.get("color"))
        laid: list[tuple[float, str]] = []
        kept: set[int] = set()   # ids of glyphs left as they are (or snapshot-moved)
        first = True
        for paragraph, start_line in paragraphs:
            paragraph = decompose_ligatures(paragraph)
            # A paragraph opening with glyphs that can't be redrawn exactly (a
            # symbol-font bullet and its space) keeps those original glyphs,
            # moved with the paragraph; only the words after them are re-laid.
            lead_chars = line_char_list(lines[start_line]) or []
            lead: list[dict[str, Any]] = []
            for ch in lead_chars:
                if ch["c"].strip() and _chars_exact([ch], picker):
                    break
                lead.append(ch)
            lead_text = "".join(c["c"] for c in lead)
            x_first = lefts[k] if first else column_left
            target = baselines[k] + len(laid) * pitch
            if frame.identity and lead and len(lead) < len(lead_chars) and lead_text.strip() \
                    and paragraph.startswith(lead_text) and abs(target - baselines[start_line]) <= 0.01 \
                    and False:  # disabled: produced bold/regular flips in testing
                paragraph = paragraph[len(lead_text):]
                x_first = float(lead_chars[len(lead)]["origin"][0])
                target = baselines[k] + len(laid) * pitch
                move = target - baselines[start_line]
                for ch in lead:
                    kept.add(id(ch))
                    if abs(move) > 0.01 and ch["c"].strip():
                        snaps.append((ch, 0.0, move))
            kwargs = picker.kwargs(span.get("font") or "", paragraph or " ")
            wrapped = _wrap_words(paragraph, kwargs, size,
                                  max(10.0, right_limit - x_first), max(10.0, right_limit - column_left))
            for n, text in enumerate(wrapped):
                laid.append((x_first if n == 0 else column_left, text))
            first = False
        for index in region_old:
            erase.extend(c for c in line_char_list(lines[index]) if id(c) not in kept
                         or any(c is snap[0] for snap in snaps))
        for n, (x, text) in enumerate(laid):
            if text.strip():
                ops, _w = _run_ops(picker, span.get("font") or "", text, x, baselines[k] + n * pitch, size, color)
                draw.extend(ops)
        line_delta = len(laid) - len(region_old)
        t = len(lines) - end

    # Identical lines below a line-count change move by whole line pitches,
    # replayed glyph-by-glyph so they keep every font/size/color/position.
    dy = line_delta * pitch
    for index in range(len(lines) - t, len(lines)):
        # Below a line-count change the identical lines move by whole pitches;
        # otherwise they are replayed in place (reading order, see replay).
        plan = plan_line_edit(lines[index], old_texts[index], picker, page_rect.x1, False,
                              dy=dy if abs(dy) > 0.01 else 0.0, replay=may_replay(lines[index]),
                              snap_ok=False)
        if plan is None:
            return None
        erase.extend(plan["erase"])
        draw.extend(plan["draw"])
        snaps.extend(plan["snap"])

    # Lines ABOVE the first change are replayed too (identical glyphs and
    # positions), so the whole paragraph sits contiguously, in order, in the
    # content stream: copy/paste and search read it as one paragraph instead
    # of its head, the rest of the page, then its tail.
    head_erase: list[dict[str, Any]] = []
    head_draw: list[tuple] = []
    for index in range(0, k):
        plan = plan_line_edit(lines[index], old_texts[index], picker, page_rect.x1, False,
                              replay=may_replay(lines[index]))
        if plan is not None:
            head_erase.extend(plan["erase"])
            head_draw.extend(plan["draw"])
    erase = head_erase + erase
    draw = head_draw + draw

    # Snapshots of glyphs that move but can't be redrawn exactly, taken from
    # the page BEFORE anything is erased.
    snapshots = []
    if snaps:
        rf = reflow_module()
        for ch, dx, move_y in snaps[:40]:
            region = (fitz.Rect(ch["page_bbox"]) + (-0.3, -0.3, 0.3, 0.3)) & page.rect
            bare = rf.bare_copy(doc, page.number)
            snapshots.append((rf.build_snapshot(bare, region), region, dx, move_y))

    # Keep only the user's edit: erase, let reflow clear its region, draw.
    erase_glyphs(page, erase, frame)
    for snapshot, region, dx, move_y in snapshots:
        page.show_pdf_page(region + (dx, move_y, dx, move_y), snapshot, 0, clip=region,
                           keep_proportion=False, overlay=True)
        snapshot.close()
    render = render_rect or rect
    if before_render is not None:
        before_render(page, {"block_lines": block_lines, "render_rect": render})
    for x, y, text, kwargs, size, color in draw:
        _draw(page, x, y, text, kwargs, size, color, frame)
    old_bottom = float(lines[-1]["bbox"][3])
    if not frame.identity:
        # Geometry is in the text's own frame: report no page-space growth
        # (reflow does not run on turned text).
        old_bottom = float(block_lines[-1]["bbox"][3]) if block_lines else 0.0
        dy = 0.0
    return {"inserted": True, "block_lines": block_lines, "render_rect": render,
            "old_bottom": old_bottom, "new_bottom": old_bottom + dy, "minimal": True}


def shift_lines_snapshot(doc: Any, page: Any, lines: list[dict[str, Any]], dy: float) -> None:
    """Move whole lines vertically by `dy` WITHOUT re-typesetting them: their
    exact content (any script, shaped glyphs, /ActualText) is copied from a
    snapshot of the page and placed shifted — the same mechanism reflow uses
    for the content below an edit."""
    import fitz

    if not lines or abs(dy) < 0.01:
        return
    rf = reflow_module()
    region = fitz.Rect(lines[0]["bbox"])
    for line in lines[1:]:
        region |= fitz.Rect(line["bbox"])
    region = (region + (-1.0, -0.5, 1.0, 0.5)) & page.rect
    bare = rf.bare_copy(doc, page.number)
    try:
        snapshot = rf.build_snapshot(bare, region)
        erase_lines(page, lines, bound=region)
        page.show_pdf_page(region + (0, dy, 0, dy), snapshot, 0, clip=region,
                           keep_proportion=False, overlay=True)
    finally:
        bare.close()


def rtl_delete_lines(doc: Any, page: Any, block_lines: list[dict[str, Any]], new_lines: list[str],
                     render_rect: Any, before_render: Any = None) -> dict[str, Any] | None:
    """Right-to-left paragraph where the user only DELETED whole lines: erase
    those lines and move the ones below up by whole line pitches, exactly as
    they are (re-shaping the paragraph drifted it). None if it isn't a pure
    deletion."""
    lines = sorted(block_lines, key=lambda l: float(l["origin"][1]) if l.get("origin") else float(l["bbox"][1]))
    old = [l["text"] for l in lines]
    if len(new_lines) >= len(old):
        return None
    kept, j = [], 0
    for i, text in enumerate(old):
        if j < len(new_lines) and text == new_lines[j]:
            kept.append(i)
            j += 1
    if j != len(new_lines):
        return None
    deleted = [i for i in range(len(old)) if i not in kept]
    baselines = [float(l["origin"][1]) if l.get("origin") else float(l["bbox"][3]) for l in lines]
    gaps = [b - a for a, b in zip(baselines, baselines[1:]) if b - a > 0.5]
    pitch = _median(gaps) if gaps else float(lines[0].get("fontsize") or 11) * 1.2
    erase_lines(page, [lines[i] for i in deleted], bound=None)
    if before_render is not None:
        before_render(page, {"block_lines": block_lines, "render_rect": render_rect})
    groups: dict[int, list[dict[str, Any]]] = {}
    for i in kept:
        above = sum(1 for d in deleted if d < i)
        if above:
            groups.setdefault(above, []).append(lines[i])
    for above, group in groups.items():
        shift_lines_snapshot(doc, page, group, -above * pitch)
    bottom = max(float(l["bbox"][3]) for l in lines)
    return {"inserted": True, "block_lines": block_lines, "render_rect": render_rect,
            "old_bottom": bottom, "new_bottom": bottom - len(deleted) * pitch, "minimal": True}


_AMOUNT = re.compile(r"^[\s$€£¥₹%()+\-–—.,:/0-9A-Za-z]*\d[\s$€£¥₹%()+\-–—.,:/0-9A-Za-z]*$")


def right_aligned_evidence(page: Any, rect: Any, text: str) -> bool:
    """A short right-half value is pinned at its right edge only when it IS a
    right-aligned column value: number-like (an amount, a date, a total) or
    sharing its right edge with other text on the page. A left-aligned name
    that merely sits in the right half must not slide leftwards."""
    if _AMOUNT.match(text or "") and sum(ch.isdigit() for ch in text) >= max(1, len(text.strip()) // 4):
        return True
    shared = 0
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            x1 = line["bbox"][2]
            if abs(x1 - rect.x1) <= 1.0 and abs(line["bbox"][1] - rect.y0) > 1.0:
                shared += 1
    return shared >= 1


def apply_block_edit(doc: Any, page: Any, args: argparse.Namespace, rect: Any, scratch_files: list[str],
                     before_render: Any = None) -> dict[str, Any]:
    """The replace-block edit itself, on an open page: erase the block's own
    lines, then re-render the new text. `before_render(page, info)` (reflow)
    runs after the erase and before any new ink, with the erased block lines.
    Returns {"inserted", "block_lines", "render_rect"}."""
    import fitz

    # The box the NEW text is laid into. Defaults to the original block rect;
    # a resized editor frame sends its own --render-rect so the replacement
    # reflows at the width the user dragged out. Erasing always follows
    # --rect (the original segment's lines), never this.
    render_values = parse_rect(getattr(args, "render_rect", None))
    render_rect = fitz.Rect(render_values) if render_values else fitz.Rect(rect)
    # An explicit resize: the user picked this geometry, so it wins over every
    # "keep it where it was" heuristic below (per-line replay, right-anchoring,
    # the width headroom `--expand` adds for unresized edits).
    resized = render_values is not None and (
        abs(render_rect.x0 - rect.x0) > 0.5
        or abs(render_rect.x1 - rect.x1) > 0.5
        or abs(render_rect.y0 - rect.y0) > 0.5
    )

    new_text = args.text
    # Per-word color spans (JSON: [{"start":int,"length":int,"hex":"#rrggbb"}]).
    # Their presence forces full styled rendering (no per-line fast path).
    color_runs = None
    if getattr(args, "color_runs", None):
        try:
            parsed = json.loads(args.color_runs)
            color_runs = parsed if isinstance(parsed, list) and parsed else None
        except (ValueError, TypeError):
            color_runs = None
    style, font_kwargs = resolve_block_style(doc, page, rect, args, new_text, scratch_files)

    # Identify which lines inside the rect actually belong to this block —
    # content merely sharing the area must survive the edit untouched.
    original = args.original_text or new_text
    lines_info, block_lines, complete = resolve_block_lines(page, rect, original)
    if not complete and args.original_text is None and len(lines_info) == len(new_text.split("\n")):
        # No original text supplied: fall back to count-based mapping.
        block_lines, complete = lines_info, True

    # The paragraph the user edited is gone from this rect (a stale or
    # rebased commit): refuse. Erasing the rect blindly would wipe whatever
    # now sits there.
    if args.original_text and not block_lines:
        raise EditTargetMissing("The paragraph changed since you selected it — click it again.")

    # Text-only edits (no style override, no resize): touch ONLY what changed.
    plain_edit = (
        complete and block_lines and not resized
        and not args.font_size and not args.color and not args.bold and not args.font
        and not getattr(args, "italic", False) and not getattr(args, "underline", False)
        and not getattr(args, "no_bold", False) and not getattr(args, "no_italic", False)
        and natural_align(args) and not color_runs and not getattr(args, "background", None)
        and not text_has_rtl(new_text) and not text_has_rtl(original)
    )
    rtl_plain = (
        complete and block_lines and not resized
        and not args.font_size and not args.color and not args.bold and not args.font
        and not getattr(args, "italic", False) and not getattr(args, "underline", False)
        and not getattr(args, "no_bold", False) and not getattr(args, "no_italic", False)
        and natural_align(args) and not color_runs and not getattr(args, "background", None)
        and (text_has_rtl(new_text) or text_has_rtl(original))
    )
    if rtl_plain and len(new_text.split("\n")) < len(block_lines):
        deletion = rtl_delete_lines(doc, page, block_lines, new_text.split("\n"), render_rect, before_render)
        if deletion is not None:
            return deletion

    if plain_edit:
        minimal = minimal_block_edit(doc, page, block_lines, new_text, rect, args, scratch_files,
                                     before_render=before_render, render_rect=render_rect)
        if minimal is not None:
            if args.track_original:
                grown = fitz.Rect(render_rect) + (-2, -2, 2, 2)
                annot = page.add_rect_annot(grown)
                annot.set_colors(stroke=(0.15, 0.4, 0.85))
                annot.set_border(width=1, dashes=[3, 2])
                snippet = args.track_original if len(args.track_original) <= 600 else args.track_original[:600] + "…"
                annot.set_info(title="Tracked change", content=f"Original text:\n{snippet}")
                annot.update()
            return minimal

    # Turned text (upright text on a /Rotate page runs vertically in page
    # space) is only handled by the frame-aware minimal path above; the
    # general renderer below would draw it in the wrong direction.
    if block_lines and not text_has_rtl(new_text) and not text_has_rtl(original):
        turned = Frame.for_lines(block_lines)
        if turned is None or not turned.identity:
            raise EditTargetMissing(
                "Style changes on turned text (e.g. on a rotated page) aren't supported yet — "
                "edit the words only, or rotate the page upright first."
            )

    # When the edited text has the same number of lines as the block, render
    # line-by-line at each line's own origin and style — indents and
    # right-aligned amounts stay exactly where they were.
    new_lines = new_text.split("\n")
    # Arabic/Hebrew replacement text is shaped and laid out right-to-left by
    # render_rtl_block_edit below instead of insert_text/insert_textbox.
    rtl_text = text_has_rtl(new_text) and rtl_render_available()
    per_line = (
        complete
        and not resized
        and not args.font_size and not args.color and not args.bold and not args.font
        and not getattr(args, "italic", False) and not getattr(args, "underline", False)
        and not getattr(args, "no_bold", False) and not getattr(args, "no_italic", False)
        and natural_align(args)
        and not color_runs
        and len(new_lines) == len(block_lines)
        and all(line.get("origin") for line in block_lines)
    )
    per_line_fonts: list[dict[str, Any]] = []
    if per_line and not rtl_text:
        font_cache: dict[tuple[str, str], dict[str, Any]] = {}
        for line, text_line in zip(block_lines, new_lines):
            raw = line["rawfont"] or ""
            key = (raw, text_line)
            if key not in font_cache:
                line_style = {
                    "fontname": builtin_font_alias(raw),
                    "fontsize": line["fontsize"],
                    "color": line["color"],
                    "rawfont": raw,
                }
                font_cache[key] = resolve_replacement_font(doc, page, line_style, text_line or " ", scratch_files)
            per_line_fonts.append(font_cache[key])
        # An edited line that would spill past the page edge needs wrapping —
        # fall back to flowed rendering in that case.
        for line, text_line, font_choice in zip(block_lines, new_lines, per_line_fonts):
            length = replacement_text_length(text_line, font_choice, line["fontsize"])
            if line["origin"][0] + length > page.rect.x1 - 8:
                per_line = False
                break

    # Right-edge anchoring: a SHORT single-line segment living in the right
    # half of the page is a column value (an invoice amount). Re-rendering it
    # from its left edge drifts it out of its column as the text gets longer
    # or shorter, so pin its RIGHT edge where it already was.
    right_anchor = None
    if (
        not resized
        and len(new_lines) == 1
        and len(block_lines) <= 1
        and rect.x0 > 0.55 * page.rect.width
        and 0 < len(new_text.strip()) <= 32
        and natural_align(args)
        and not color_runs
        and not rtl_text
        and right_aligned_evidence(page, rect, original)
    ):
        if per_line and per_line_fonts:
            measure_kwargs, measure_size = per_line_fonts[0], block_lines[0]["fontsize"]
        else:
            measure_kwargs, measure_size = font_kwargs, float(style["fontsize"])
        rendered = replacement_text_length(new_text, measure_kwargs, measure_size)
        if rendered > 0:
            # A longer amount has to grow LEFTWARD to keep its right edge, so
            # the only floor here is the page itself.
            right_anchor = max(page.rect.x0 + 2, rect.x1 - rendered)

    # RTL per-line edits redraw ONLY the lines whose text changed; unchanged
    # lines keep their original glyphs (re-shaping them drifted 0.6-0.8pt).
    rtl_lines = block_lines if per_line else []
    rtl_new_lines = new_lines
    erase_set = block_lines
    rtl_unchanged = False
    if rtl_text and per_line and block_lines:
        # Lines BEFORE the first changed one stay untouched; from there on the
        # paragraph is redrawn in order (its text extracts in reading order).
        first = next((i for i, (line, text) in enumerate(zip(block_lines, new_lines))
                      if line.get("text") != text), None)
        if first is None:
            # Same text: an explicit re-render (the app never sends these; the
            # engine CLI uses it to normalise presentation-form glyphs into
            # logical, searchable text). Redraw the whole paragraph.
            pass
        else:
            rtl_lines = block_lines[first:]
            rtl_new_lines = new_lines[first:]
            erase_set = rtl_lines
    if block_lines:
        if erase_set:
            erase_lines(page, erase_set, bound=rect)
    else:
        reflow_module()._redact(page, [block_erase_rect(rect)], images=fitz.PDF_REDACT_IMAGE_NONE,
                                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                text=fitz.PDF_REDACT_TEXT_REMOVE, preserve=True)
    if before_render is not None:
        before_render(page, {"block_lines": block_lines, "render_rect": render_rect})

    # Shade behind the block (drawn AFTER erasing, BEFORE text) so text
    # color and background persist together in one operation.
    bg = parse_hex_color(args.background) if getattr(args, "background", None) else None
    if bg is not None:
        page.draw_rect(fitz.Rect(render_rect) + (-2, -2, 2, 2), fill=bg, color=None, overlay=True)

    inserted = False
    if rtl_text and new_text.strip():
        if rtl_unchanged:
            inserted = True
        else:
            partial = erase_set is not block_lines
            inserted = render_rtl_block_edit(
                doc, page, args, style, rtl_lines, rtl_new_lines, new_text,
                render_rect, resized, color_runs, strict=partial,
            )
            if inserted is None:
                kept = [line for line in block_lines if line not in erase_set]
                if kept:
                    erase_lines(page, kept, bound=rect)
                inserted = render_rtl_block_edit(
                    doc, page, args, style, [], new_lines, new_text, render_rect, resized, color_runs,
                )
    elif per_line and new_text.strip():
        inserted = True
        for line, text_line, font_choice in zip(block_lines, new_lines, per_line_fonts):
            if not text_line.strip():
                continue
            kwargs = {"fontname": font_choice["fontname"]}
            if font_choice.get("fontfile"):
                kwargs["fontfile"] = font_choice["fontfile"]
            page.insert_text(
                (right_anchor if right_anchor is not None else line["origin"][0], line["origin"][1]),
                text_line,
                fontsize=line["fontsize"],
                color=line["color"],
                **kwargs,
            )
    elif new_text.strip():
        origin_x = right_anchor if right_anchor is not None else render_rect.x0
        # A dragged-out width is exact: no `--expand` headroom past its right
        # edge, so the reflowed text stays inside the box the user drew.
        expand = 0.0 if resized else args.expand
        first_baseline = None
        if block_lines and not resized and block_lines[0].get("origin"):
            first_baseline = float(min(block_lines, key=lambda l: l["origin"][1])["origin"][1])
        inserted = render_block_text(
            page, origin_x, render_rect.y0, max(1.0, render_rect.x1 - origin_x), new_text,
            style, font_kwargs, args.line_height, expand,
            align=align_constant(getattr(args, "align", "auto")),
            color_runs=color_runs, first_baseline=first_baseline,
        )

    # Track Changes: leave a dashed blue review box whose note holds the
    # original wording, so every edit stays reviewable.
    if inserted and args.track_original:
        grown = fitz.Rect(render_rect) + (-2, -2, 2, 2)
        annot = page.add_rect_annot(grown)
        annot.set_colors(stroke=(0.15, 0.4, 0.85))
        annot.set_border(width=1, dashes=[3, 2])
        original = args.track_original
        snippet = original if len(original) <= 600 else original[:600] + "…"
        annot.set_info(title="Tracked change", content=f"Original text:\n{snippet}")
        annot.update()

    return {"inserted": inserted, "block_lines": block_lines, "render_rect": render_rect}


def edit_overlaps(input_path: str, output_path: str, pno: int) -> int:
    """How many lines of the edited text now sit ON other text (a longer
    paragraph whose content below could not be moved). New lines are those
    absent before; replayed lines match exactly and don't count. A real
    overlap covers over half a row's height (tight leading overlaps less)."""
    import fitz
    from collections import Counter

    keys = reflow_module()._line_keys
    with fitz.open(input_path) as before_doc, fitz.open(output_path) as after_doc:
        before = Counter(keys(before_doc[pno]))
        after = Counter(keys(after_doc[pno]))
    new = [fitz.Rect(bbox) for (_t, bbox), _n in (after - before).items()]
    kept = [fitz.Rect(bbox) for (_t, bbox), _n in (after & before).items()]
    hits = 0
    for box in new:
        for other in kept:
            inter = box & other
            if inter.is_empty or inter.width < 2:
                continue
            if inter.height > 0.5 * min(box.height, other.height):
                hits += 1
                break
    return hits


def cmd_replace_block(args: argparse.Namespace) -> int:
    """Replace an entire text block: redact its area, re-render the new text
    with the block's own style (or explicit overrides), wrapping inside the
    original box, like an in-place block editor. With --reflow, the content
    below it in the same column is pushed down / pulled up to fit."""
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        doc.close()
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]

    rect_values = parse_rect(args.rect)
    if rect_values is None:
        doc.close()
        return respond(False, message="--rect is required")
    rect = fitz.Rect(rect_values)

    scratch_files: list[str] = []
    new_text = args.text
    extra: dict[str, Any] = {}
    try:
        if getattr(args, "reflow", False):
            result, extra["reflow"] = reflow_module().replace_block_with_reflow(
                reflow_helpers(), doc, args.page - 1, args, rect, scratch_files)
        else:
            result = apply_block_edit(doc, page, args, rect, scratch_files)
        inserted = result["inserted"]
        fix_inserted_tounicode(page)
        doc.save(str(output), garbage=4, deflate=True)
    except EditTargetMissing as missing:
        # (the finally below closes the document and removes scratch files)
        return respond(False, message=str(missing))
    finally:
        doc.close()
        for path in scratch_files:
            try:
                os.unlink(path)
            except OSError:
                pass
    if new_text.strip() and not inserted:
        return respond(False, message="replacement text did not fit")
    try:
        overlapping = edit_overlaps(args.input, str(output), args.page - 1)
    except Exception:
        overlapping = 0
    if overlapping:
        extra["overlaps"] = overlapping
    return respond(True, output=str(output), inserted=inserted, **extra)


def block_lines_info(page: Any, rect: Any, segmented: bool = True) -> list[dict[str, Any]] | None:
    """Per-line text, baseline origin, and dominant style for every text line
    inside rect — the same content the block erase removes — so edits and
    moves can reproduce each line's exact position and styling.

    With `segmented` (the default) each line is first split into column
    segments, so editing an invoice amount erases and re-renders ONLY the
    amount, never the description sharing its baseline. Callers fall back to
    whole lines when a segment match fails (older rects covered whole rows)."""
    import fitz

    target = fitz.Rect(rect) + (-1, -1, 1, 1)
    lines = []
    for block in text_dict(page).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            if segmented:
                pieces = line_span_segments(line)
            else:
                spans = [s for s in line.get("spans", []) if s.get("text")]
                bbox = line.get("bbox")
                pieces = [{
                    "spans": spans,
                    "bbox": bbox,
                    "text": logical_spans_text(spans, line.get("rtl_base"), bool(line.get("rtl_force_visual"))),
                }] if (spans and bbox) else []
            for piece in pieces:
                spans, bbox = piece["spans"], piece["bbox"]
                if not spans or not bbox:
                    continue
                center = fitz.Point((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
                if not target.contains(center):
                    continue
                dominant = max(
                    spans,
                    key=lambda s: max(0.0, (s["bbox"][2] - s["bbox"][0]) * (s["bbox"][3] - s["bbox"][1])),
                )
                lines.append({
                    "text": piece["text"],
                    # The segment's spans (x order, per-char data kept): the
                    # minimal edit path redraws only what changed from these.
                    "spans": spans,
                    "dir": line.get("dir"),
                    "origin": spans[0].get("origin"),
                    "bbox": list(bbox),
                    "rawfont": dominant.get("font"),
                    "fontsize": float(dominant.get("size") or 11),
                    "color": pdf_color_to_rgb(dominant.get("color")),
                    "rtl_base": line.get("rtl_base"),
                    "sort_key": (round(bbox[1], 1), round(bbox[0], 1)),
                })
    lines.sort(key=lambda line: line["sort_key"])
    return lines or None


def resolve_block_lines(page: Any, rect: Any, original_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Lines of the edited block inside `rect`, preferring column SEGMENTS and
    falling back to whole lines when the caller's rect/original text describes
    a whole row (rects recorded before column segmentation existed)."""
    lines_info = block_lines_info(page, rect) or []
    block_lines, foreign, complete = partition_block_lines(lines_info, original_text, rect)
    if complete:
        return lines_info, block_lines, complete
    whole = block_lines_info(page, rect, segmented=False) or []
    whole_lines, _whole_foreign, whole_complete = partition_block_lines(whole, original_text, rect)
    if whole_complete:
        return whole, whole_lines, True
    return lines_info, block_lines, complete


def partition_block_lines(lines_info: list[dict[str, Any]], original_text: str, rect: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Split the lines inside a rect into the block's own lines (matched
    against its original text) and FOREIGN lines that merely share the area.

    Documents can contain IDENTICAL duplicate lines (especially ones damaged
    by earlier edits) — matching is by spatial affinity: each original line
    claims the unclaimed candidate with equal text that sits closest to the
    block rect, and candidates further than 40pt are never claimed. Foreign
    content must never be erased by a move or edit."""
    import fitz

    target = fitz.Rect(rect) if rect is not None else None

    def distance(line: dict[str, Any]) -> float:
        if target is None:
            return 0.0
        bbox = fitz.Rect(line["bbox"])
        if target.intersects(bbox):
            return 0.0
        dx = max(target.x0 - bbox.x1, bbox.x0 - target.x1, 0)
        dy = max(target.y0 - bbox.y1, bbox.y0 - target.y1, 0)
        return (dx * dx + dy * dy) ** 0.5

    claimed = [False] * len(lines_info)
    matched_count = 0
    for wanted in original_text.split("\n"):
        best_index = -1
        best_distance = 40.0  # never claim text further than this
        for index, line in enumerate(lines_info):
            if claimed[index] or line["text"] != wanted:
                continue
            d = distance(line)
            if d < best_distance:
                best_distance = d
                best_index = index
        if best_index >= 0:
            claimed[best_index] = True
            matched_count += 1

    block_lines = [line for index, line in enumerate(lines_info) if claimed[index]]
    foreign = [line for index, line in enumerate(lines_info) if not claimed[index]]
    complete = matched_count == len(original_text.split("\n")) and bool(block_lines)
    return block_lines, foreign, complete


def erase_lines(page: Any, lines: list[dict[str, Any]], bound: Any = None) -> None:
    """Redact ONLY the given lines' own glyph areas (slightly shrunk), so
    neighbors and content sharing the block's bounding box survive.

    `bound` (the edited block's own rect) clamps every erase to at most 1pt
    outside it: redaction deletes any glyph its rect touches, so an erase that
    ran past the block's edge clipped the neighbouring column's first glyph."""
    import fitz

    # Glyph-precise when the lines carry their glyphs: no neighbour's glyph
    # (overlapping boxes in tight leading) is ever removed, nothing painted.
    glyphs: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for line in lines:
        chars = line_char_list(line) if line.get("spans") else None
        if chars:
            glyphs.extend(chars)
        else:
            rest.append(line)
    if glyphs:
        erase_glyphs(page, glyphs)
    if not rest:
        return
    lines = rest

    limit = fitz.Rect(bound) if bound is not None else None
    for line in lines:
        rect = fitz.Rect(line["bbox"])
        shrink = min(0.75, rect.height * 0.15)
        erase = fitz.Rect(rect.x0 + shrink, rect.y0 + shrink, rect.x1 - shrink, rect.y1 - shrink)
        if limit is not None:
            erase = fitz.Rect(
                max(erase.x0, limit.x0 - 1.0),
                max(erase.y0, limit.y0 - 1.0),
                min(erase.x1, limit.x1 + 1.0),
                min(erase.y1, limit.y1 + 1.0),
            )
        if erase.width <= 0 or erase.height <= 0:
            continue
        page.add_redact_annot(erase, fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)


def cmd_move_block(args: argparse.Namespace) -> int:
    """Move a text block: erase ONLY the block's own glyphs (inward-shrunk
    redact), then re-render its text with matched style exactly at the drop
    point. Never copies or erases neighboring content the way a rectangle
    move would. Pure moves (text unchanged) keep each line's own font, size,
    color, and relative position."""
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        doc.close()
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]

    rect_values = parse_rect(args.rect)
    if rect_values is None:
        doc.close()
        return respond(False, message="--rect is required")
    rect = fitz.Rect(rect_values)

    text = args.text
    if not text.strip():
        doc.close()
        return respond(False, message="--text is required for move-block")

    scratch_files: list[str] = []
    original = args.original_text or text
    _lines_info, block_lines, complete = resolve_block_lines(page, rect, original)

    pure_move = (
        complete
        and text == original
        and not args.font_size and not args.color and not args.bold and not args.font
        and not getattr(args, "italic", False) and not getattr(args, "underline", False)
        and not getattr(args, "no_bold", False) and not getattr(args, "no_italic", False)
        and natural_align(args)
        and all(line.get("origin") for line in block_lines)
    )
    rtl_text = text_has_rtl(text) and rtl_render_available()

    style, font_kwargs = resolve_block_style(doc, page, rect, args, text, scratch_files)
    per_line_fonts: list[dict[str, Any]] = []
    if pure_move and not rtl_text:
        font_cache: dict[str, dict[str, Any]] = {}
        for line in block_lines:
            raw = line["rawfont"] or ""
            if raw not in font_cache:
                line_style = {
                    "fontname": builtin_font_alias(raw),
                    "fontsize": line["fontsize"],
                    "color": line["color"],
                    "rawfont": raw,
                }
                font_cache[raw] = resolve_replacement_font(doc, page, line_style, line["text"], scratch_files)
            per_line_fonts.append(font_cache[raw])

    # Erase ONLY the block's own lines: content that merely shares the
    # block's bounding box (or sits in the drag path) is never touched.
    if block_lines:
        erase_lines(page, block_lines, bound=rect)
    else:
        reflow_module()._redact(page, [block_erase_rect(rect)], images=fitz.PDF_REDACT_IMAGE_NONE,
                                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                text=fitz.PDF_REDACT_TEXT_REMOVE, preserve=True)

    dest_x = max(page.rect.x0 + 6, min(args.dest_x, page.rect.x1 - rect.width - 6))
    dest_y = max(page.rect.y0 + 6, min(args.dest_y, page.rect.y1 - 18))

    if rtl_text:
        if pure_move:
            # Shift each line's box: RTL lines keep their right edges aligned.
            inserted = True
            for line in block_lines:
                raw = line.get("rawfont") or ""
                font = rtl_font_for(doc, page, line["text"], raw, "bold" in raw.lower())
                if font is None:
                    inserted = False
                    break
                baseline = dest_y + (line["origin"][1] - rect.y0)
                if rtl_module().paragraph_direction(line["text"], rtl_module().paragraph_is_rtl(text)):
                    draw_rtl_line(page, font, line["text"], line["fontsize"], line["color"], baseline,
                                  right=dest_x + (line["bbox"][2] - rect.x0), rtl=True)
                else:
                    draw_rtl_line(page, font, line["text"], line["fontsize"], line["color"], baseline,
                                  left=dest_x + (line["origin"][0] - rect.x0), rtl=False)
        else:
            target = fitz.Rect(dest_x, dest_y, dest_x + rect.width, page.rect.y1 - 12)
            inserted = render_rtl_block_edit(doc, page, args, style, [], text.split("\n"), text, target, False, None)
    elif pure_move:
        # Translate every line to the destination, preserving per-line style
        # and exact intra-block layout (indents, baselines).
        inserted = True
        for line, font_choice in zip(block_lines, per_line_fonts):
            kwargs = {"fontname": font_choice["fontname"]}
            if font_choice.get("fontfile"):
                kwargs["fontfile"] = font_choice["fontfile"]
            origin = line["origin"]
            page.insert_text(
                (dest_x + (origin[0] - rect.x0), dest_y + (origin[1] - rect.y0)),
                line["text"],
                fontsize=line["fontsize"],
                color=line["color"],
                **kwargs,
            )
    else:
        inserted = render_block_text(
            page, dest_x, dest_y, rect.width, text,
            style, font_kwargs, args.line_height, args.expand,
            align=align_constant(getattr(args, "align", "auto")),
        )

    for _touched in doc:
        fix_inserted_tounicode(_touched)
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    for path in scratch_files:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not inserted:
        return respond(False, message="moved text did not fit at the destination")
    return respond(True, output=str(output), dest=[dest_x, dest_y])


def cmd_add_symbol(args: argparse.Namespace) -> int:
    """Fill & Sign stamps: checkmark, cross, or dot at a click point, drawn
    with the base-14 ZapfDingbats font so they render everywhere."""
    import fitz

    glyphs = {"check": "3", "cross": "7", "dot": "l"}
    glyph = glyphs.get(args.kind)
    if glyph is None:
        return respond(False, message=f"kind must be one of {sorted(glyphs)}")

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        doc.close()
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]
    color = parse_hex_color(args.color) or (0.05, 0.05, 0.05)
    page.insert_text(
        (args.x, args.y + args.size * 0.72),
        glyph,
        fontname="zadb",
        fontsize=args.size,
        color=color,
    )
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), kind=args.kind)


# quality → (Ghostscript preset, PyMuPDF image target dpi, JPEG quality)
_COMPRESS_SETTINGS = {"small": ("/screen", 72, 50), "medium": ("/ebook", 150, 70), "high": ("/printer", 300, 85)}


def _compress_with_pymupdf(source: str, output: Path, target_dpi: int, quality: int) -> None:
    """Built-in fallback when Ghostscript isn't installed (release builds):
    downsample/recompress images, subset fonts, drop unused objects."""
    import fitz

    doc = fitz.open(source)
    try:
        doc.rewrite_images(dpi_threshold=int(target_dpi * 1.3), dpi_target=target_dpi, quality=quality)
    except Exception:
        pass  # images PyMuPDF can't recode stay as they are
    try:
        doc.subset_fonts()
    except Exception:
        pass
    doc.save(str(output), garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True, use_objstms=1)
    doc.close()


def cmd_compress(args: argparse.Namespace) -> int:
    """Reduce file size. Quality: small (72dpi images), medium (150dpi),
    high (300dpi). Uses Ghostscript when installed, otherwise PyMuPDF."""
    setting = _COMPRESS_SETTINGS.get(args.quality)
    if setting is None:
        return respond(False, message=f"quality must be one of {sorted(_COMPRESS_SETTINGS)}")
    preset, target_dpi, jpeg_quality = setting

    output = Path(args.output)
    ensure_parent(output)
    before = os.path.getsize(args.input)
    gs = shutil.which("gs") or "/opt/homebrew/bin/gs"
    if not os.path.exists(gs) or os.environ.get("FOLIO_COMPRESS_ENGINE") == "pymupdf":
        try:
            _compress_with_pymupdf(args.input, output, target_dpi, jpeg_quality)
        except Exception as exc:
            return respond(False, message=f"Compression failed: {exc}")
        after = os.path.getsize(output)
        if after >= before:
            shutil.copyfile(args.input, output)
            after = before
        return respond(True, output=str(output), before_bytes=before, after_bytes=after, method="pymupdf")
    result = subprocess.run(
        [
            gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
            f"-dPDFSETTINGS={preset}",
            f"-sOutputFile={output}",
            str(args.input),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not output.exists():
        return respond(False, message=f"Ghostscript failed: {result.stderr[:400]}")
    after = os.path.getsize(output)
    if after >= before:
        # Compression did not help (already optimized) — keep the original.
        shutil.copyfile(args.input, output)
        after = before
    return respond(True, output=str(output), before_bytes=before, after_bytes=after, method="ghostscript")


def cmd_add_page_numbers(args: argparse.Namespace) -> int:
    import fitz

    positions = {"bottom-center", "bottom-left", "bottom-right", "top-center", "top-left", "top-right"}
    if args.position not in positions:
        return respond(False, message=f"position must be one of {sorted(positions)}")
    formats = {"n", "page-n", "n-of-total"}
    if args.number_format not in formats:
        return respond(False, message=f"format must be one of {sorted(formats)}")

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    total = doc.page_count
    size = args.font_size
    margin = args.margin
    for index in range(total):
        page = doc[index]
        number = args.start + index
        if args.number_format == "page-n":
            label = f"Page {number}"
        elif args.number_format == "n-of-total":
            label = f"{number} of {args.start + total - 1}"
        else:
            label = str(number)
        width = fitz.get_text_length(label, fontname="helv", fontsize=size)
        rect = page.rect
        if "left" in args.position:
            x = rect.x0 + margin
        elif "right" in args.position:
            x = rect.x1 - margin - width
        else:
            x = rect.x0 + (rect.width - width) / 2
        y = rect.y0 + margin if args.position.startswith("top") else rect.y1 - margin + size * 0.72 - size
        page.insert_text((x, y + size * 0.72), label, fontname="helv", fontsize=size, color=(0.2, 0.2, 0.2))
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), pages=total)


def cmd_resize_pages(args: argparse.Namespace) -> int:
    """Rescale every page onto a uniform target size (content aspect-fit,
    centered) — makes merged PDFs of mixed page sizes one consistent size."""
    import fitz

    if args.width <= 10 or args.height <= 10:
        return respond(False, message="width/height must be in points and > 10")

    output = Path(args.output)
    ensure_parent(output)
    src = fitz.open(args.input)
    out = fitz.open()
    for index in range(src.page_count):
        page = src[index]
        target = out.new_page(width=args.width, height=args.height)
        src_rect = page.rect
        if src_rect.width <= 0 or src_rect.height <= 0:
            continue
        scale = min(args.width / src_rect.width, args.height / src_rect.height)
        drawn_w = src_rect.width * scale
        drawn_h = src_rect.height * scale
        x0 = (args.width - drawn_w) / 2
        y0 = (args.height - drawn_h) / 2
        target.show_pdf_page(fitz.Rect(x0, y0, x0 + drawn_w, y0 + drawn_h), src, index)
    out.save(str(output), garbage=4, deflate=True)
    pages = out.page_count
    out.close()
    src.close()
    return respond(True, output=str(output), pages=pages, width=args.width, height=args.height)


def cmd_block_background(args: argparse.Namespace) -> int:
    """Fill a colored rectangle BEHIND existing content — background shading
    for a block, separate from text color."""
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    color = parse_hex_color(args.color)
    if color is None:
        return respond(False, message="--color must be #rrggbb")
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        doc.close()
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    rect_values = parse_rect(args.rect)
    if rect_values is None:
        doc.close()
        return respond(False, message="--rect is required")
    page = doc[args.page - 1]
    rect = fitz.Rect(rect_values) + (-args.padding, -args.padding, args.padding, args.padding)
    page.draw_rect(rect, fill=color, color=None, overlay=False)
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output))


def cmd_set_password(args: argparse.Namespace) -> int:
    """Protect the PDF with a password (AES-256)."""
    import fitz

    if not args.password:
        return respond(False, message="--password cannot be empty")
    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    permissions = int(
        fitz.PDF_PERM_ACCESSIBILITY
        | fitz.PDF_PERM_PRINT
        | fitz.PDF_PERM_COPY
        | fitz.PDF_PERM_ANNOTATE
    )
    doc.save(
        str(output),
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw=args.password,
        user_pw=args.password,
        permissions=permissions,
        garbage=4,
        deflate=True,
    )
    doc.close()
    return respond(True, output=str(output))


def cmd_redline(args: argparse.Namespace) -> int:
    """Review marks: squiggly underline, insert caret, or replace (strikeout +
    caret + note) over the given line rects."""
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        doc.close()
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]

    rects = []
    for part in (args.rects or "").split(";"):
        values = parse_rect(part) if part.strip() else None
        if values:
            rects.append(fitz.Rect(values))

    red = (0.78, 0.16, 0.16)
    made = 0
    if args.kind == "strikeout":
        for rect in rects:
            annot = page.add_strikeout_annot(rect)
            annot.set_colors(stroke=red)
            annot.update()
            made += 1
    elif args.kind == "squiggly":
        for rect in rects:
            annot = page.add_squiggly_annot(rect)
            annot.set_colors(stroke=red)
            annot.update()
            made += 1
    elif args.kind == "caret":
        point = fitz.Point(args.x, args.y)
        annot = page.add_caret_annot(point)
        annot.set_colors(stroke=(0.1, 0.3, 0.8))
        if args.note:
            annot.set_info(content=args.note)
        annot.update()
        made = 1
    elif args.kind == "replace":
        if not rects:
            doc.close()
            return respond(False, message="--rects required for replace")
        for rect in rects:
            annot = page.add_strikeout_annot(rect)
            annot.set_colors(stroke=red)
            if args.note:
                annot.set_info(content=f"Replace with: {args.note}")
            annot.update()
            made += 1
        last = rects[-1]
        caret = page.add_caret_annot(fitz.Point(last.x1, last.y1))
        caret.set_colors(stroke=(0.1, 0.3, 0.8))
        if args.note:
            caret.set_info(content=f"Replace with: {args.note}")
        caret.update()
    else:
        doc.close()
        return respond(False, message="kind must be strikeout, squiggly, caret, or replace")

    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), kind=args.kind, marks=made)


def replace_rtl_in_lines(doc: Any, page: Any, args: argparse.Namespace, rect_values: Any) -> int:
    """Find/replace for Arabic/Hebrew: match against each row's LOGICAL text
    and re-render the whole row (shaped, bidi-ordered) with the row's own
    style. Patching a word into an RTL row would leave two separate runs
    that PDFKit reads in the wrong order. Returns the replacements made."""
    import fitz

    lines = [line for line in (block_lines_info(page, page.rect) or []) if args.find in line["text"]]
    if not lines:
        return 0
    if rect_values:
        click = fitz.Rect(rect_values)
        lines = [min(lines, key=lambda line: rect_proximity(click, fitz.Rect(line["bbox"])))]
    rt = rtl_module()
    plans = []
    for line in lines:
        count = 1 if rect_values else line["text"].count(args.find)
        new_text = line["text"].replace(args.find, args.replace, count)
        raw = (line.get("rawfont") or "") if args.match_style else ""
        font = rtl_font_for(doc, page, new_text, raw or None, "bold" in raw.lower())
        if font is None:
            continue
        size = float(line["fontsize"]) if args.match_style else float(args.font_size)
        color = line["color"] if args.match_style else (0, 0, 0)
        plans.append((line, new_text, font, size, color, count))
    if not plans:
        return 0
    erase_lines(page, [plan[0] for plan in plans])
    total = 0
    for line, new_text, font, size, color, count in plans:
        total += count
        if not new_text.strip():
            continue
        bbox = fitz.Rect(line["bbox"])
        baseline = line["origin"][1] if line.get("origin") else bbox.y1 - size * 0.22
        base = line.get("rtl_base")
        rtl = rt.paragraph_direction(new_text, rt.paragraph_is_rtl(new_text) if base is None else bool(base))
        width = max(l.width for l in rt.layout_text(new_text, font, size, None, base_rtl=rtl))
        room = (bbox.x1 - page.rect.x0 - 8) if rtl else (page.rect.x1 - 8 - bbox.x0)
        if args.auto_size and width > room > 0:
            size = max(6.0, size * room / width)
        draw_rtl_line(page, font, new_text, size, color, baseline,
                      left=None if rtl else bbox.x0, right=bbox.x1 if rtl else None, rtl=rtl)
    return total


def cmd_replace_text(args: argparse.Namespace) -> int:
    import fitz

    if not args.find:
        return respond(False, message="--find cannot be empty")

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    matches = 0
    inserted = 0
    rect_values = parse_rect(args.rect)
    target_indexes = [args.page - 1] if args.page else list(range(doc.page_count))
    scratch_files: list[str] = []
    rtl_mode = (text_has_rtl(args.find) or text_has_rtl(args.replace)) and rtl_render_available()

    for page_index in target_indexes:
        if page_index < 0 or page_index >= doc.page_count:
            return respond(False, message=f"page must be between 1 and {doc.page_count}")
        page = doc[page_index]
        if rtl_mode:
            replaced = replace_rtl_in_lines(doc, page, args, rect_values)
            if replaced:
                matches += replaced
                inserted += replaced
                continue
        if rect_values:
            click_rect = fitz.Rect(rect_values)
            found_rects = [nearest_search_rect(page, click_rect, args.find) or click_rect]
        else:
            found_rects = search_rects(page, args.find)
        if not found_rects:
            continue

        matches += len(found_rects)
        styles = [style_for_rect(page, rect) for rect in found_rects]
        font_choices = [
            resolve_replacement_font(doc, page, style, args.replace, scratch_files)
            if args.match_style
            else {"fontname": "helv", "fontfile": None}
            for style in styles
        ]

        for rect in found_rects:
            # Exact hit rect: outward padding would delete neighboring glyphs
            # that merely touch it (redaction removes any intersecting glyph).
            page.add_redact_annot(rect, fill=(1, 1, 1))

        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        for rect, style, font_choice in zip(found_rects, styles, font_choices):
            base_font_size = float(style["fontsize"])
            color = style["color"] if args.match_style else (0, 0, 0)
            font_kwargs = {"fontname": font_choice["fontname"]}
            if font_choice.get("fontfile"):
                font_kwargs["fontfile"] = font_choice["fontfile"]

            font_size = base_font_size if args.match_style else args.font_size

            if text_has_rtl(args.replace) and rtl_render_available():
                # Arabic/Hebrew replacement: shaped and bidi-ordered; RTL text
                # keeps the found phrase's RIGHT edge (where it starts).
                rt = rtl_module()
                raw = style.get("rawfont") if args.match_style else None
                rtl_font = rtl_font_for(doc, page, args.replace, raw, "bold" in (raw or "").lower())
                if rtl_font is None:
                    continue
                rtl_para = rt.paragraph_is_rtl(args.replace)
                origin = style.get("origin")
                if "\n" not in args.replace:
                    width = rt.layout_text(args.replace, rtl_font, font_size, None)[0].width
                    room = (rect.x1 - page.rect.x0 - 12) if rtl_para else (page.rect.x1 - 12 - rect.x0)
                    if args.auto_size and width > room > 0:
                        font_size = max(6.0, font_size * room / width)
                    baseline_y = origin[1] if origin else rect.y1 - max(1.5, base_font_size * 0.18)
                    draw_rtl_line(page, rtl_font, args.replace, max(font_size, 6), color, baseline_y,
                                  left=None if rtl_para else rect.x0, right=rect.x1 if rtl_para else None)
                else:
                    box_width = max(rect.width + args.expand, 80)
                    x0 = max(page.rect.x0 + 12, rect.x1 - box_width) if rtl_para else rect.x0
                    x1 = rect.x1 if rtl_para else min(page.rect.x1 - 12, rect.x0 + box_width)
                    rt.draw_paragraph(page, fitz.Rect(x0, rect.y0, x1, page.rect.y1 - 12), args.replace,
                                      rtl_font, font_size, color)
                inserted += 1
                continue

            # Single-line replacements sit on the original baseline at the
            # original size; shrink only if the new text
            # would run off the right edge of the page.
            single_line = "\n" not in args.replace
            origin = style.get("origin")
            if single_line:
                if args.auto_size:
                    max_width = page.rect.x1 - 12 - rect.x0
                    while font_size > 6 and replacement_text_length(args.replace, font_choice, font_size) > max_width:
                        font_size -= 0.5
                baseline_y = origin[1] if origin else rect.y1 - max(1.5, base_font_size * 0.18)
                page.insert_text(
                    (rect.x0, baseline_y),
                    args.replace,
                    fontsize=max(font_size, 6),
                    color=color,
                    **font_kwargs,
                )
                inserted += 1
                continue

            target_width = max(
                rect.width + args.expand,
                replacement_text_length(args.replace, font_choice, base_font_size) + 6,
                80,
            )
            target = fitz.Rect(
                rect.x0,
                max(page.rect.y0 + 2, rect.y0 - max(2, base_font_size * 0.12)),
                min(page.rect.x1 - 12, rect.x0 + target_width),
                min(page.rect.y1 - 2, rect.y1 + max(5, base_font_size * 0.35)),
            )

            result = -1
            while args.auto_size and font_size >= 6:
                result = page.insert_textbox(
                    target,
                    args.replace,
                    fontsize=font_size,
                    color=color,
                    align=fitz.TEXT_ALIGN_LEFT,
                    **font_kwargs,
                )
                if result >= 0:
                    break
                font_size -= 0.5

            if not args.auto_size:
                result = page.insert_textbox(
                    target,
                    args.replace,
                    fontsize=font_size,
                    color=color,
                    align=fitz.TEXT_ALIGN_LEFT,
                    **font_kwargs,
                )

            if result < 0:
                page.insert_text(
                    (target.x0, target.y1 - 3),
                    args.replace,
                    fontsize=max(font_size, 6),
                    color=color,
                    **font_kwargs,
                )
            inserted += 1

    if matches == 0:
        doc.close()
        for path in scratch_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        return respond(False, message=f"No matches found for {args.find!r}")

    for _touched in doc:
        fix_inserted_tounicode(_touched)
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    for path in scratch_files:
        try:
            os.unlink(path)
        except OSError:
            pass
    return respond(True, output=str(output), matches=matches, inserted=inserted)


def cmd_redact_text(args: argparse.Namespace) -> int:
    import fitz

    if not args.find:
        return respond(False, message="--find cannot be empty")

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    rect_values = parse_rect(args.rect)
    indexes = [args.page - 1] if args.page else parse_page_spec(args.pages, doc.page_count)
    matches = 0

    for index in indexes:
        if index < 0 or index >= doc.page_count:
            return respond(False, message=f"page must be between 1 and {doc.page_count}")
        page = doc[index]
        if rect_values:
            click_rect = fitz.Rect(rect_values)
            rects = [nearest_search_rect(page, click_rect, args.find) or click_rect]
        else:
            rects = search_rects(page, args.find)
        matches += len(rects)
        for rect in rects:
            padded = fitz.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1)
            page.add_redact_annot(padded, text=args.label or None, fill=(1, 1, 1), text_color=(0, 0, 0))
        if rects:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    if matches == 0:
        doc.close()
        return respond(False, message=f"No matches found for {args.find!r}")

    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), matches=matches, pages=[i + 1 for i in indexes])


# Friendly font names → base-14 aliases by variant
# (regular, bold, italic, bold-italic). Verified against fitz.Font():
# Helvetica-Oblique is "heit" (not "heli"), bold-italic "hebi".
_ADD_TEXT_BUILTIN = {
    "helvetica": ("helv", "hebo", "heit", "hebi"),
    "times new roman": ("tiro", "tibo", "tiit", "tibi"),
    "times": ("tiro", "tibo", "tiit", "tibi"),
    "courier new": ("cour", "cobo", "coit", "cobi"),
    "courier": ("cour", "cobo", "coit", "cobi"),
}

# Folders that hold installed macOS font files, richest first.
_ADD_TEXT_FONT_DIRS = (
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
)


def _variant_score(rest: str, bold: bool, italic: bool) -> int | None:
    """Rank a font file's trailing variant tokens against the wanted style.
    `rest` is the file stem with the family prefix already stripped (spaces
    and hyphens removed, lower-cased). Lower score = better match; None means
    the file carries a style we explicitly did NOT ask for."""
    has_bold = "bold" in rest or rest.endswith("bd") or "black" in rest or "heavy" in rest
    has_italic = "italic" in rest or "oblique" in rest or rest.endswith("it")
    if bold and italic:
        if has_bold and has_italic:
            return 0
    elif bold:
        if has_bold and not has_italic:
            return 0
        if has_bold:
            return 2
    elif italic:
        if has_italic and not has_bold:
            return 0
        if has_italic:
            return 2
    else:
        if not has_bold and not has_italic:
            return 0
    # Regular file is always an acceptable fallback for any request.
    if not has_bold and not has_italic:
        return 5
    return None


def find_family_font_file(family: str | None, bold: bool, italic: bool) -> str | None:
    """Locate an installed .ttf/.otf whose name matches `family`, honoring the
    bold/italic variant. Matches by compact (spaces/hyphens removed, case-
    insensitive) family prefix so "Times New Roman" finds "TimesNewRomanPSMT"
    etc. Returns the best-scoring file path or None."""
    compact = re.sub(r"[\s\-]", "", (family or "")).lower()
    if not compact:
        return None
    best_path: str | None = None
    best_score = 1_000
    for directory in _ADD_TEXT_FONT_DIRS:
        if not os.path.isdir(directory):
            continue
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
            score = _variant_score(rest, bold, italic)
            if score is None:
                continue
            # Shorter remainder (closer family match) breaks score ties, so
            # "Arial" beats "Arial Narrow" and "Arial Bold" beats the narrow
            # variants for the same requested style.
            score = score * 100 + min(len(rest), 99)
            if score < best_score:
                best_score = score
                best_path = os.path.join(directory, entry)
    return best_path


def resolve_add_text_font(name: str | None, bold: bool, italic: bool = False) -> dict[str, Any]:
    """Map ANY font family name to insert_text kwargs.

    Resolution order: (a) base-14 builtins (Helvetica/Times/Courier) with the
    right bold/italic variant; (b) an installed TTF/OTF for the family (any
    system family — Georgia, Verdana, Arial, …) honoring the variant, falling
    back to the family's regular file; (c) the Helvetica base-14 variant."""
    key = (name or "").strip().lower()
    builtin = _ADD_TEXT_BUILTIN.get(key)
    if builtin:
        index = (2 if italic else 0) + (1 if bold else 0)
        return {"fontname": builtin[index], "fontfile": None}

    path = find_family_font_file(name, bold, italic)
    if path is None and (bold or italic):
        path = find_family_font_file(name, False, False)
    if path:
        label = re.sub(r"[^A-Za-z0-9]", "", name or "")[:16] or "F0"
        return {"fontname": label, "fontfile": path}

    helv = _ADD_TEXT_BUILTIN["helvetica"]
    index = (2 if italic else 0) + (1 if bold else 0)
    return {"fontname": helv[index], "fontfile": None}


def cmd_add_text(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        return respond(False, message=f"page must be between 1 and {doc.page_count}")

    color = parse_hex_color(args.color) or (0, 0, 0)
    font_choice = resolve_add_text_font(args.font, args.bold, getattr(args, "italic", False))
    kwargs = {"fontname": font_choice["fontname"]}
    if font_choice.get("fontfile"):
        kwargs["fontfile"] = font_choice["fontfile"]
    # --y is the TOP of the first line; PyMuPDF draws from the baseline.
    baseline_y = args.y + args.font_size * 0.83

    page = doc[args.page - 1]
    align = align_constant(getattr(args, "align", "auto"))
    if text_has_rtl(args.text) and rtl_render_available():
        # Arabic/Hebrew: shaped, bidi-ordered glyphs with a logical ToUnicode.
        # "auto" right-aligns RTL paragraphs inside the text's own width, so
        # the click point stays the block's left edge either way.
        rt = rtl_module()
        family = None if (args.font or "").strip().lower() in ("", "helvetica") else args.font
        font = rtl_font_for(doc, page, args.text, None, args.bold, family=family)
        if font is None:
            doc.close()
            return respond(False, message="no installed font can display this text")
        mode = getattr(args, "align", "auto")
        natural = max((line.width for line in rt.layout_text(args.text, font, args.font_size, None)), default=0.0)
        right_limit = page.rect.x1 - 12
        if mode in ("center", "right") or args.x + natural > right_limit:
            box = fitz.Rect(args.x, args.y, right_limit, page.rect.y1 - 12)
        else:
            box = fitz.Rect(args.x, args.y, args.x + natural + 0.5, page.rect.y1 - 12)
        result = rt.draw_paragraph(page, box, args.text, font, args.font_size, color, align=mode)
        if result is None:
            doc.close()
            return respond(False, message="text did not fit on the page")
        if getattr(args, "underline", False):
            draw_text_underlines(page, fitz.Rect(result["rect"]) + (-1, -1, 1, 4), color)
        for _touched in doc:
            fix_inserted_tounicode(_touched)
        doc.save(str(output), garbage=4, deflate=True)
        doc.close()
        return respond(True, output=str(output), page=args.page)
    if align != fitz.TEXT_ALIGN_LEFT:
        # A box from the click x to the right margin gives center/right text
        # room to lay out; left keeps the exact baseline placement below
        # (insert_text already handles left-aligned multi-line text).
        box = fitz.Rect(args.x, args.y - max(1.5, args.font_size * 0.1), page.rect.x1 - 12, page.rect.y1 - 12)
        page.insert_textbox(
            box, args.text,
            fontsize=args.font_size, color=color, align=align, **kwargs,
        )
    else:
        page.insert_text(
            (args.x, baseline_y),
            args.text,
            fontsize=args.font_size,
            color=color,
            **kwargs,
        )
    if getattr(args, "underline", False):
        clip = fitz.Rect(
            args.x - 1, args.y - 2, page.rect.x1 - 2,
            args.y + args.font_size * 1.6 * (args.text.count("\n") + 1) + 4,
        )
        draw_text_underlines(page, clip, color)
    for _touched in doc:
        fix_inserted_tounicode(_touched)
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), page=args.page)


def cmd_annotate_text(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    indexes = parse_page_spec(args.pages, doc.page_count)
    matches = 0

    for index in indexes:
        page = doc[index]
        rects = search_rects(page, args.find)
        matches += len(rects)
        for rect in rects:
            if args.kind == "highlight":
                annot = page.add_highlight_annot(rect)
            elif args.kind == "underline":
                annot = page.add_underline_annot(rect)
            else:
                annot = page.add_strikeout_annot(rect)
            annot.set_info(content=args.note or f"{args.kind}: {args.find}")
            annot.update()

    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), kind=args.kind, matches=matches)


def cmd_add_note(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]
    annot = page.add_text_annot((args.x, args.y), args.text)
    annot.set_info(title="Folio PDF", content=args.text)
    annot.update()
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), page=args.page)


def cmd_add_signature(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]
    rect = fitz.Rect(args.x, args.y, args.x + args.width, args.y + args.height)
    page.insert_textbox(
        rect,
        args.text,
        fontname="tiro",
        fontsize=args.font_size,
        color=(0, 0, 0),
        align=fitz.TEXT_ALIGN_CENTER,
    )
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), page=args.page)


def cmd_add_image(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]
    rect = fitz.Rect(args.x, args.y, args.x + args.width, args.y + args.height)
    page.insert_image(rect, filename=args.image)
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), page=args.page, image=args.image)


def cmd_paste_region(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    source_doc = fitz.open(args.source)
    dest_doc = fitz.open(args.input)

    if args.source_page < 1 or args.source_page > source_doc.page_count:
        source_doc.close()
        dest_doc.close()
        return respond(False, message=f"source page must be between 1 and {source_doc.page_count}")
    if args.destination_page < 1 or args.destination_page > dest_doc.page_count:
        source_doc.close()
        dest_doc.close()
        return respond(False, message=f"destination page must be between 1 and {dest_doc.page_count}")

    source_rect_values = parse_rect(args.source_rect)
    if source_rect_values is None:
        source_doc.close()
        dest_doc.close()
        return respond(False, message="--source-rect is required")

    source_page = source_doc[args.source_page - 1]
    source_rect = fitz.Rect(source_rect_values)
    clip = source_rect & source_page.rect
    if clip.width <= 1 or clip.height <= 1:
        source_doc.close()
        dest_doc.close()
        return respond(False, message="source region is outside the page")

    # Build a single-page scratch doc containing ONLY the clipped region as
    # live vector content (text stays selectable/searchable/redactable and
    # razor sharp at any zoom). Everything outside the clip
    # is redacted away so the embedded form holds no hidden content.
    region_doc = fitz.open()
    region_doc.insert_pdf(source_doc, from_page=args.source_page - 1, to_page=args.source_page - 1)
    region_page = region_doc[0]
    page_rect = region_page.rect
    outside = [
        fitz.Rect(page_rect.x0, page_rect.y0, page_rect.x1, clip.y0),
        fitz.Rect(page_rect.x0, clip.y1, page_rect.x1, page_rect.y1),
        fitz.Rect(page_rect.x0, clip.y0, clip.x0, clip.y1),
        fitz.Rect(clip.x1, clip.y0, page_rect.x1, clip.y1),
    ]
    for strip in outside:
        if strip.width > 0.5 and strip.height > 0.5:
            region_page.add_redact_annot(strip, fill=False)
    region_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    same_file = Path(args.source).resolve() == Path(args.input).resolve()
    if args.erase_source:
        if not same_file:
            source_doc.close()
            dest_doc.close()
            region_doc.close()
            return respond(False, message="--erase-source can only be used when source and input are the same PDF")
        erase_page = dest_doc[args.source_page - 1]
        padded = fitz.Rect(clip.x0 - 1, clip.y0 - 1, clip.x1 + 1, clip.y1 + 1)
        erase_page.add_redact_annot(padded, fill=(1, 1, 1))
        erase_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    dest_page = dest_doc[args.destination_page - 1]
    dest_x = max(dest_page.rect.x0, min(args.destination_x, dest_page.rect.x1 - clip.width))
    dest_y = max(dest_page.rect.y0, min(args.destination_y, dest_page.rect.y1 - clip.height))
    dest_rect = fitz.Rect(dest_x, dest_y, dest_x + clip.width, dest_y + clip.height)
    try:
        dest_page.show_pdf_page(dest_rect, region_doc, 0, clip=clip)
    except Exception:
        # Fallback: rasterize the region (previous behavior).
        matrix = fitz.Matrix(args.dpi / 72, args.dpi / 72)
        pixmap = source_page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        dest_page.insert_image(dest_rect, stream=pixmap.tobytes("png"))
    region_doc.close()

    dest_doc.save(str(output), garbage=4, deflate=True)
    source_doc.close()
    dest_doc.close()
    return respond(
        True,
        output=str(output),
        source_page=args.source_page,
        destination_page=args.destination_page,
        erased=args.erase_source,
        rect=[dest_rect.x0, dest_rect.y0, dest_rect.x1, dest_rect.y1],
    )


def cmd_add_link(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    if args.page < 1 or args.page > doc.page_count:
        return respond(False, message=f"page must be between 1 and {doc.page_count}")
    page = doc[args.page - 1]
    rect = fitz.Rect(args.x, args.y, args.x + args.width, args.y + args.height)
    page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": args.url})
    doc.save(str(output), garbage=4, deflate=True)
    doc.close()
    return respond(True, output=str(output), page=args.page, url=args.url)


def run_ocrmypdf(input_path: Path, output_path: Path, language: str, force: bool) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "ocrmypdf",
        "--output-type",
        "pdf",
        "--deskew",
        "--rotate-pages",
        "--language",
        language,
    ]
    command.append("--force-ocr" if force else "--skip-text")
    command.extend([str(input_path), str(output_path)])

    env = os.environ.copy()
    env["PATH"] = ":".join([
        str(Path(sys.executable).resolve().parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        env.get("PATH", ""),
    ])
    font_config = Path("/opt/homebrew/etc/fonts/fonts.conf")
    if font_config.exists():
        env.setdefault("FONTCONFIG_FILE", str(font_config))
    return subprocess.run(command, text=True, capture_output=True, env=env)


def cmd_ocr(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)

    if args.page:
        source = fitz.open(args.input)
        if args.page < 1 or args.page > source.page_count:
            return respond(False, message=f"page must be between 1 and {source.page_count}")
        with tempfile.TemporaryDirectory(prefix="folio-ocr-") as tmp_dir:
            single_path = Path(tmp_dir) / "page.pdf"
            ocr_path = Path(tmp_dir) / "page-ocr.pdf"
            single = fitz.open()
            single.insert_pdf(source, from_page=args.page - 1, to_page=args.page - 1)
            single.save(str(single_path), garbage=4, deflate=True)
            single.close()
            result = run_ocrmypdf(single_path, ocr_path, args.language, True)
            if result.returncode != 0:
                source.close()
                return respond(False, output=str(output), message="OCR failed", stdout=result.stdout[-4000:], stderr=result.stderr[-4000:])

            ocr_doc = fitz.open(str(ocr_path))
            final = fitz.open()
            for index in range(source.page_count):
                if index == args.page - 1:
                    final.insert_pdf(ocr_doc)
                else:
                    final.insert_pdf(source, from_page=index, to_page=index)
            final.save(str(output), garbage=4, deflate=True)
            final.close()
            ocr_doc.close()
            source.close()
        return respond(True, output=str(output), page=args.page, stdout=result.stdout[-1000:], stderr=result.stderr[-1000:])

    result = run_ocrmypdf(Path(args.input), output, args.language, args.force)
    if result.returncode != 0:
        return respond(False, output=str(output), message="OCR failed", stdout=result.stdout[-4000:], stderr=result.stderr[-4000:])
    return respond(True, output=str(output), stdout=result.stdout[-1000:], stderr=result.stderr[-1000:])


def cmd_export_images(args: argparse.Namespace) -> int:
    import fitz

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(args.input)
    zoom = args.dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    outputs: list[str] = []
    # The app exports from a session copy ("v3.pdf"); --name carries the
    # document's real file name so the images aren't called v3-page-001.png.
    stem = safe_stem(args.name or args.input)

    for index, page in enumerate(doc, start=1):
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        suffix = "jpg" if args.format == "jpg" else "png"
        out_path = output_dir / f"{stem}-page-{index:03d}.{suffix}"
        if args.format == "jpg":
            pixmap.pil_save(str(out_path), format="JPEG", quality=92)
        else:
            pixmap.save(str(out_path))
        outputs.append(str(out_path))

    doc.close()
    return respond(True, output_dir=str(output_dir), outputs=outputs, pages=len(outputs), dpi=args.dpi, format=args.format)


_MD_BULLETS = ("•", "◦", "▪", "‣", "–", "-", "*")


def _markdown_run(lines: list[dict], body: float) -> str:
    spans = [s for l in lines for s in l["spans"] if s["text"].strip()]
    size = max(s["size"] for s in spans)
    bold = all(s["flags"] & 16 or "bold" in s["font"].lower() for s in spans)
    texts = [" ".join("".join(s["text"] for s in l["spans"]).replace("\u00ad", "-").split()) for l in lines]
    joined = " ".join(texts)
    if size >= body * 1.6:
        return f"# {joined}"
    if size >= body * 1.25:
        return f"## {joined}"
    if bold and len(joined) < 90:
        return f"### {joined}"
    if all(t[:1] in _MD_BULLETS and t[1:2] == " " for t in texts):
        return "\n".join(f"- {t[2:]}" for t in texts)
    return joined


def pdf_to_markdown(doc: Any) -> str:
    """Structure-aware Markdown: headings from font size, bullets, paragraphs.

    The body size is the size carrying the most characters in the document;
    lines clearly larger become headings, bold-only short lines sub-headings.
    """
    from collections import Counter

    pages = [page.get_text("dict", sort=True)["blocks"] for page in doc]
    weight: Counter = Counter()
    for blocks in pages:
        for block in blocks:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    weight[round(span["size"], 1)] += len(span["text"].strip())
    body = weight.most_common(1)[0][0] if weight else 11.0

    out: list[str] = []
    for index, blocks in enumerate(pages):
        if index:
            out.append("---")
        for block in blocks:
            lines = [l for l in block.get("lines", []) if "".join(s["text"] for s in l["spans"]).strip()]
            # Split a block where the type size changes (kicker above a title).
            runs: list[list[dict]] = []
            for line in lines:
                size = round(max(s["size"] for s in line["spans"] if s["text"].strip()))
                if runs and runs[-1][0]["_size"] == size:
                    runs[-1].append(line)
                else:
                    line["_size"] = size
                    runs.append([line])
            for run in runs:
                out.append(_markdown_run(run, body))
    return "\n\n".join(out).strip() + "\n"


def cmd_export_text(args: argparse.Namespace) -> int:
    import fitz

    output = Path(args.output)
    ensure_parent(output)

    if args.format == "md":
        doc = fitz.open(args.input)
        text = pdf_to_markdown(doc)
        doc.close()
    elif args.format == "html":
        doc = fitz.open(args.input)
        chunks = ["<!doctype html><html><head><meta charset=\"utf-8\"><title>PDF Export</title></head><body>"]
        for index, page in enumerate(doc, start=1):
            chunks.append(f"<section data-page=\"{index}\">")
            chunks.append(page.get_text("html"))
            chunks.append("</section>")
        chunks.append("</body></html>")
        text = "\n".join(chunks)
        doc.close()
    else:
        doc = fitz.open(args.input)
        chunks = [f"--- Page {i + 1} ---\n{page.get_text('text')}" for i, page in enumerate(doc)]
        text = "\n\n".join(chunks)
        doc.close()

    output.write_text(text, encoding="utf-8")
    return respond(True, output=str(output), bytes=output.stat().st_size, format=args.format)


def cmd_export_docx(args: argparse.Namespace) -> int:
    from pdf2docx import Converter

    output = Path(args.output)
    ensure_parent(output)
    converter = Converter(args.input)
    try:
        converter.convert(str(output), start=0, end=None)
    finally:
        converter.close()
    return respond(True, output=str(output), bytes=output.stat().st_size)


def cmd_export_xlsx(args: argparse.Namespace) -> int:
    import fitz
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "PDF Text"
    sheet.append(["Page", "Line", "Text"])

    for page_index, page in enumerate(doc, start=1):
        # Control characters (e.g. NUL from glyphs without a Unicode mapping)
        # are illegal in worksheet cells and would abort the whole export.
        text = ILLEGAL_CHARACTERS_RE.sub("", page.get_text("text"))
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line_index, line in enumerate(lines, start=1):
            sheet.append([page_index, line_index, line])

    doc.close()
    workbook.save(output)
    return respond(True, output=str(output), bytes=output.stat().st_size)


def cmd_export_pptx(args: argparse.Namespace) -> int:
    import fitz
    from pptx import Presentation
    from pptx.util import Inches

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    presentation = Presentation()
    blank = presentation.slide_layouts[6]

    if doc.page_count > 0:
        first = doc[0].rect
        presentation.slide_width = Inches(10)
        presentation.slide_height = Inches(max(5.625, 10 * first.height / max(first.width, 1)))

    with tempfile.TemporaryDirectory(prefix="folio-pptx-") as tmp_dir:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(args.dpi / 72.0, args.dpi / 72.0), alpha=False)
            image_path = Path(tmp_dir) / f"page-{index:03d}.png"
            pix.save(str(image_path))
            slide = presentation.slides.add_slide(blank)
            slide.shapes.add_picture(str(image_path), 0, 0, width=presentation.slide_width, height=presentation.slide_height)

    doc.close()
    presentation.save(output)
    return respond(True, output=str(output), bytes=output.stat().st_size)


def cmd_images_to_pdf(args: argparse.Namespace) -> int:
    from PIL import Image, ImageOps

    output = Path(args.output)
    ensure_parent(output)
    images = []
    for image_path in args.input:
        image = Image.open(image_path)
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")
        images.append(image)

    if not images:
        return respond(False, message="no images supplied")

    first, rest = images[0], images[1:]
    first.save(output, "PDF", resolution=args.dpi, save_all=True, append_images=rest)
    for image in images:
        image.close()
    return respond(True, output=str(output), pages=1 + len(rest), bytes=output.stat().st_size)


def cmd_enhance_scan(args: argparse.Namespace) -> int:
    import fitz
    from PIL import Image, ImageEnhance, ImageFilter

    output = Path(args.output)
    ensure_parent(output)
    doc = fitz.open(args.input)
    images = []
    matrix = fitz.Matrix(args.dpi / 72.0, args.dpi / 72.0)

    with tempfile.TemporaryDirectory(prefix="folio-enhance-") as tmp_dir:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            if args.grayscale:
                image = image.convert("L").convert("RGB")
            image = ImageEnhance.Contrast(image).enhance(args.contrast)
            image = ImageEnhance.Sharpness(image).enhance(args.sharpness)
            if args.denoise:
                image = image.filter(ImageFilter.MedianFilter(size=3))
            image_path = Path(tmp_dir) / f"page-{index:03d}.jpg"
            image.save(image_path, "JPEG", quality=92)
            saved = Image.open(image_path).convert("RGB")
            images.append(saved)

        if not images:
            return respond(False, message="input has no pages")
        images[0].save(output, "PDF", resolution=args.dpi, save_all=True, append_images=images[1:])

    for image in images:
        image.close()
    doc.close()
    return respond(True, output=str(output), pages=len(images), bytes=output.stat().st_size)


def engine_api() -> Any:
    """Shared helpers handed to extension modules in Engine/ext/."""
    from types import SimpleNamespace

    return SimpleNamespace(
        respond=respond,
        ensure_parent=ensure_parent,
        parse_page_spec=parse_page_spec,
        safe_stem=safe_stem,
        version=ENGINE_VERSION,
    )


def load_extensions(subparsers: Any) -> None:
    """Register commands from every Engine/ext/*.py module.

    Each module exposes ``register(subparsers, api)``. Modules must import
    heavy dependencies (fitz, PIL, ...) lazily inside their commands so that
    loading every extension stays cheap for each engine invocation.
    """
    import importlib.util

    ext_dir = Path(__file__).resolve().parent / "ext"
    if not ext_dir.is_dir():
        return
    api = engine_api()
    for path in sorted(ext_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"folio_ext_{path.stem}", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        # Registered before exec so dataclasses/typing can resolve the module.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.register(subparsers, api)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Folio PDF engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health")
    health.set_defaults(func=cmd_health)

    metadata = subparsers.add_parser("metadata")
    metadata.add_argument("--input", required=True)
    metadata.set_defaults(func=cmd_metadata)

    merge = subparsers.add_parser("merge")
    merge.add_argument("--input", action="append", required=True)
    merge.add_argument("--output", required=True)
    merge.set_defaults(func=cmd_merge)

    merge_pages = subparsers.add_parser("merge-pages")
    merge_pages.add_argument("--page-item", action="append", required=True)
    merge_pages.add_argument("--output", required=True)
    merge_pages.set_defaults(func=cmd_merge_pages)

    split = subparsers.add_parser("split")
    split.add_argument("--input", required=True)
    split.add_argument("--output-dir", required=True)
    split.add_argument("--pages")
    split.set_defaults(func=cmd_split)

    extract = subparsers.add_parser("extract-pages")
    extract.add_argument("--input", required=True)
    extract.add_argument("--output", required=True)
    extract.add_argument("--pages", required=True)
    extract.set_defaults(func=cmd_extract_pages)

    delete = subparsers.add_parser("delete-pages")
    delete.add_argument("--input", required=True)
    delete.add_argument("--output", required=True)
    delete.add_argument("--pages", required=True)
    delete.set_defaults(func=cmd_delete_pages)

    rotate = subparsers.add_parser("rotate-pages")
    rotate.add_argument("--input", required=True)
    rotate.add_argument("--output", required=True)
    rotate.add_argument("--pages")
    rotate.add_argument("--degrees", type=int, choices=[90, 180, 270, -90, -180, -270], default=90)
    rotate.set_defaults(func=cmd_rotate_pages)

    crop = subparsers.add_parser("crop-pages")
    crop.add_argument("--input", required=True)
    crop.add_argument("--output", required=True)
    crop.add_argument("--pages")
    crop.add_argument("--left", type=float, default=0)
    crop.add_argument("--top", type=float, default=0)
    crop.add_argument("--right", type=float, default=0)
    crop.add_argument("--bottom", type=float, default=0)
    crop.set_defaults(func=cmd_crop_pages)

    replace = subparsers.add_parser("replace-text")
    replace.add_argument("--input", required=True)
    replace.add_argument("--output", required=True)
    replace.add_argument("--find", required=True)
    replace.add_argument("--replace", required=True)
    replace.add_argument("--font-size", type=float, default=11)
    replace.add_argument("--expand", type=float, default=160)
    replace.add_argument("--auto-size", action="store_true")
    replace.add_argument("--match-style", action=argparse.BooleanOptionalAction, default=True)
    replace.add_argument("--page", type=int)
    replace.add_argument("--rect")
    replace.set_defaults(func=cmd_replace_text)

    page_blocks = subparsers.add_parser("page-blocks")
    page_blocks.add_argument("--input", required=True)
    page_blocks.add_argument("--page", type=int, required=True)
    page_blocks.set_defaults(func=cmd_page_blocks)

    block_at = subparsers.add_parser("block-at")
    block_at.add_argument("--input", required=True)
    block_at.add_argument("--page", type=int, required=True)
    block_at.add_argument("--x", type=float, required=True)
    block_at.add_argument("--y", type=float, required=True)
    block_at.set_defaults(func=cmd_block_at)

    replace_block = subparsers.add_parser("replace-block")
    replace_block.add_argument("--input", required=True)
    replace_block.add_argument("--output", required=True)
    replace_block.add_argument("--page", type=int, required=True)
    replace_block.add_argument("--rect", required=True)
    # Where the replacement text is laid out (default: --rect). Set when the
    # user resized the editor frame; erasing still follows --rect.
    replace_block.add_argument("--render-rect", default=None)
    replace_block.add_argument("--text", required=True)
    replace_block.add_argument("--font-size", type=float, default=0)
    replace_block.add_argument("--font", default=None)
    replace_block.add_argument("--color")
    replace_block.add_argument("--bold", action="store_true")
    replace_block.add_argument("--italic", action="store_true")
    replace_block.add_argument("--no-bold", action="store_true", help="make bold text regular")
    replace_block.add_argument("--no-italic", action="store_true", help="make italic text upright")
    replace_block.add_argument("--underline", action="store_true")
    replace_block.add_argument("--align", choices=["auto", "left", "center", "right"], default="auto")
    replace_block.add_argument("--background", default=None)
    replace_block.add_argument("--expand", type=float, default=40)
    replace_block.add_argument("--line-height", type=float, default=0)
    replace_block.add_argument("--track-original")
    replace_block.add_argument("--original-text")
    replace_block.add_argument("--color-runs")
    # Push down / pull up the content below the block (same column) when the
    # new text needs more / fewer lines. Off by default.
    replace_block.add_argument("--reflow", action="store_true")
    replace_block.set_defaults(func=cmd_replace_block)

    block_background = subparsers.add_parser("block-background")
    block_background.add_argument("--input", required=True)
    block_background.add_argument("--output", required=True)
    block_background.add_argument("--page", type=int, required=True)
    block_background.add_argument("--rect", required=True)
    block_background.add_argument("--color", required=True)
    block_background.add_argument("--padding", type=float, default=3)
    block_background.set_defaults(func=cmd_block_background)

    move_block = subparsers.add_parser("move-block")
    move_block.add_argument("--input", required=True)
    move_block.add_argument("--output", required=True)
    move_block.add_argument("--page", type=int, required=True)
    move_block.add_argument("--rect", required=True)
    move_block.add_argument("--text", required=True)
    move_block.add_argument("--dest-x", type=float, required=True)
    move_block.add_argument("--dest-y", type=float, required=True)
    move_block.add_argument("--font-size", type=float, default=0)
    move_block.add_argument("--font", default=None)
    move_block.add_argument("--color")
    move_block.add_argument("--bold", action="store_true")
    move_block.add_argument("--italic", action="store_true")
    move_block.add_argument("--no-bold", action="store_true", help="make bold text regular")
    move_block.add_argument("--no-italic", action="store_true", help="make italic text upright")
    move_block.add_argument("--underline", action="store_true")
    move_block.add_argument("--align", choices=["auto", "left", "center", "right"], default="auto")
    move_block.add_argument("--expand", type=float, default=40)
    move_block.add_argument("--line-height", type=float, default=0)
    move_block.add_argument("--original-text")
    move_block.set_defaults(func=cmd_move_block)

    add_symbol = subparsers.add_parser("add-symbol")
    add_symbol.add_argument("--input", required=True)
    add_symbol.add_argument("--output", required=True)
    add_symbol.add_argument("--page", type=int, required=True)
    add_symbol.add_argument("--kind", required=True)
    add_symbol.add_argument("--x", type=float, required=True)
    add_symbol.add_argument("--y", type=float, required=True)
    add_symbol.add_argument("--size", type=float, default=16)
    add_symbol.add_argument("--color")
    add_symbol.set_defaults(func=cmd_add_symbol)

    compress = subparsers.add_parser("compress")
    compress.add_argument("--input", required=True)
    compress.add_argument("--output", required=True)
    compress.add_argument("--quality", default="medium")
    compress.set_defaults(func=cmd_compress)

    page_numbers = subparsers.add_parser("add-page-numbers")
    page_numbers.add_argument("--input", required=True)
    page_numbers.add_argument("--output", required=True)
    page_numbers.add_argument("--position", default="bottom-center")
    page_numbers.add_argument("--number-format", default="n")
    page_numbers.add_argument("--start", type=int, default=1)
    page_numbers.add_argument("--font-size", type=float, default=11)
    page_numbers.add_argument("--margin", type=float, default=28)
    page_numbers.set_defaults(func=cmd_add_page_numbers)

    resize_pages = subparsers.add_parser("resize-pages")
    resize_pages.add_argument("--input", required=True)
    resize_pages.add_argument("--output", required=True)
    resize_pages.add_argument("--width", type=float, required=True)
    resize_pages.add_argument("--height", type=float, required=True)
    resize_pages.set_defaults(func=cmd_resize_pages)

    set_password = subparsers.add_parser("set-password")
    set_password.add_argument("--input", required=True)
    set_password.add_argument("--output", required=True)
    set_password.add_argument("--password", required=True)
    set_password.set_defaults(func=cmd_set_password)

    redline = subparsers.add_parser("redline")
    redline.add_argument("--input", required=True)
    redline.add_argument("--output", required=True)
    redline.add_argument("--page", type=int, required=True)
    redline.add_argument("--kind", required=True)
    redline.add_argument("--rects")
    redline.add_argument("--x", type=float, default=0)
    redline.add_argument("--y", type=float, default=0)
    redline.add_argument("--note")
    redline.set_defaults(func=cmd_redline)

    redact = subparsers.add_parser("redact-text")
    redact.add_argument("--input", required=True)
    redact.add_argument("--output", required=True)
    redact.add_argument("--find", required=True)
    redact.add_argument("--label", default="")
    redact.add_argument("--pages")
    redact.add_argument("--page", type=int)
    redact.add_argument("--rect")
    redact.set_defaults(func=cmd_redact_text)

    add_text = subparsers.add_parser("add-text")
    add_text.add_argument("--input", required=True)
    add_text.add_argument("--output", required=True)
    add_text.add_argument("--page", type=int, required=True)
    add_text.add_argument("--x", type=float, required=True)
    add_text.add_argument("--y", type=float, required=True)
    add_text.add_argument("--text", required=True)
    add_text.add_argument("--font-size", type=float, default=12)
    add_text.add_argument("--font", default="Helvetica")
    add_text.add_argument("--color", default="#000000")
    add_text.add_argument("--bold", action="store_true")
    add_text.add_argument("--italic", action="store_true")
    add_text.add_argument("--underline", action="store_true")
    add_text.add_argument("--align", choices=["auto", "left", "center", "right"], default="auto")
    add_text.set_defaults(func=cmd_add_text)

    annotate = subparsers.add_parser("annotate-text")
    annotate.add_argument("--input", required=True)
    annotate.add_argument("--output", required=True)
    annotate.add_argument("--kind", choices=["highlight", "underline", "strikeout"], required=True)
    annotate.add_argument("--find", required=True)
    annotate.add_argument("--note")
    annotate.add_argument("--pages")
    annotate.set_defaults(func=cmd_annotate_text)

    note = subparsers.add_parser("add-note")
    note.add_argument("--input", required=True)
    note.add_argument("--output", required=True)
    note.add_argument("--page", type=int, required=True)
    note.add_argument("--x", type=float, required=True)
    note.add_argument("--y", type=float, required=True)
    note.add_argument("--text", required=True)
    note.set_defaults(func=cmd_add_note)

    signature = subparsers.add_parser("add-signature")
    signature.add_argument("--input", required=True)
    signature.add_argument("--output", required=True)
    signature.add_argument("--page", type=int, required=True)
    signature.add_argument("--x", type=float, required=True)
    signature.add_argument("--y", type=float, required=True)
    signature.add_argument("--width", type=float, default=180)
    signature.add_argument("--height", type=float, default=42)
    signature.add_argument("--text", required=True)
    signature.add_argument("--font-size", type=float, default=24)
    signature.set_defaults(func=cmd_add_signature)

    add_image = subparsers.add_parser("add-image")
    add_image.add_argument("--input", required=True)
    add_image.add_argument("--output", required=True)
    add_image.add_argument("--image", required=True)
    add_image.add_argument("--page", type=int, required=True)
    add_image.add_argument("--x", type=float, required=True)
    add_image.add_argument("--y", type=float, required=True)
    add_image.add_argument("--width", type=float, required=True)
    add_image.add_argument("--height", type=float, required=True)
    add_image.set_defaults(func=cmd_add_image)

    paste_region = subparsers.add_parser("paste-region")
    paste_region.add_argument("--input", required=True)
    paste_region.add_argument("--source", required=True)
    paste_region.add_argument("--output", required=True)
    paste_region.add_argument("--source-page", type=int, required=True)
    paste_region.add_argument("--source-rect", required=True)
    paste_region.add_argument("--destination-page", type=int, required=True)
    paste_region.add_argument("--destination-x", type=float, required=True)
    paste_region.add_argument("--destination-y", type=float, required=True)
    paste_region.add_argument("--dpi", type=int, default=220)
    paste_region.add_argument("--erase-source", action="store_true")
    paste_region.set_defaults(func=cmd_paste_region)

    link = subparsers.add_parser("add-link")
    link.add_argument("--input", required=True)
    link.add_argument("--output", required=True)
    link.add_argument("--page", type=int, required=True)
    link.add_argument("--x", type=float, required=True)
    link.add_argument("--y", type=float, required=True)
    link.add_argument("--width", type=float, required=True)
    link.add_argument("--height", type=float, required=True)
    link.add_argument("--url", required=True)
    link.set_defaults(func=cmd_add_link)

    ocr = subparsers.add_parser("ocr")
    ocr.add_argument("--input", required=True)
    ocr.add_argument("--output", required=True)
    ocr.add_argument("--language", default="eng")
    ocr.add_argument("--force", action="store_true")
    ocr.add_argument("--page", type=int)
    ocr.set_defaults(func=cmd_ocr)

    enhance = subparsers.add_parser("enhance-scan")
    enhance.add_argument("--input", required=True)
    enhance.add_argument("--output", required=True)
    enhance.add_argument("--dpi", type=int, default=220)
    enhance.add_argument("--contrast", type=float, default=1.35)
    enhance.add_argument("--sharpness", type=float, default=1.15)
    enhance.add_argument("--grayscale", action="store_true")
    enhance.add_argument("--denoise", action="store_true")
    enhance.set_defaults(func=cmd_enhance_scan)

    images = subparsers.add_parser("export-images")
    images.add_argument("--input", required=True)
    images.add_argument("--output-dir", required=True)
    images.add_argument("--format", choices=["png", "jpg"], default="png")
    images.add_argument("--dpi", type=int, default=200)
    images.add_argument("--name", default=None, help="file name to base the image names on")
    images.set_defaults(func=cmd_export_images)

    text = subparsers.add_parser("export-text")
    text.add_argument("--input", required=True)
    text.add_argument("--output", required=True)
    text.add_argument("--format", choices=["txt", "md", "html"], default="txt")
    text.set_defaults(func=cmd_export_text)

    docx = subparsers.add_parser("export-docx")
    docx.add_argument("--input", required=True)
    docx.add_argument("--output", required=True)
    docx.set_defaults(func=cmd_export_docx)

    xlsx = subparsers.add_parser("export-xlsx")
    xlsx.add_argument("--input", required=True)
    xlsx.add_argument("--output", required=True)
    xlsx.set_defaults(func=cmd_export_xlsx)

    pptx = subparsers.add_parser("export-pptx")
    pptx.add_argument("--input", required=True)
    pptx.add_argument("--output", required=True)
    pptx.add_argument("--dpi", type=int, default=160)
    pptx.set_defaults(func=cmd_export_pptx)

    images_pdf = subparsers.add_parser("images-to-pdf")
    images_pdf.add_argument("--input", action="append", required=True)
    images_pdf.add_argument("--output", required=True)
    images_pdf.add_argument("--dpi", type=int, default=200)
    images_pdf.set_defaults(func=cmd_images_to_pdf)

    load_extensions(subparsers)
    return parser


# Argument names holding file-system paths: left exactly as received.
_PATH_ARG_HINTS = ("input", "output", "path", "dir", "image", "json", "file", "font")


def normalize_text_args(args: argparse.Namespace) -> None:
    """NFC-normalize every text argument in place.

    Arguments from the app arrive decomposed (NFD): Swift's Process passes
    them through the file-system representation. Arabic hamza forms (إ → ا +
    U+0655), accented Latin letters, etc. then no longer equal the composed
    text PyMuPDF extracts, so block matching, find/replace and redaction miss
    those lines. Paths are skipped (APFS lookups are normalization-insensitive).
    """
    import unicodedata

    def fix(value: Any) -> Any:
        if isinstance(value, str):
            return unicodedata.normalize("NFC", value)
        if isinstance(value, list):
            return [fix(v) for v in value]
        return value

    for name, value in vars(args).items():
        if any(hint in name for hint in _PATH_ARG_HINTS):
            continue
        setattr(args, name, fix(value))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    normalize_text_args(args)
    try:
        # Glyph boxes without MuPDF's quad corrections, the geometry every
        # rect heuristic here was tuned on. pymupdf4llm sets this globally on
        # import, so set it explicitly: results must not depend on whether an
        # optional package happens to be installed.
        import fitz
        fitz.TOOLS.unset_quad_corrections(True)
        # Redaction must not leave removed words in RTL lines' /ActualText.
        rtl_module().install_redaction_guard()
        return args.func(args)
    except Exception as exc:
        return respond(False, message=str(exc), traceback=traceback.format_exc(limit=8))


if __name__ == "__main__":
    raise SystemExit(main())
