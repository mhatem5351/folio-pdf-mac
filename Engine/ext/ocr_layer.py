"""Invisible OCR text layer from externally recognized lines (Apple Vision).

Command:
  add-text-layer --input IN.pdf --output OUT.pdf --json LINES.json

LINES.json (engine coordinates: unrotated crop box, top-left origin, points):
  {"pages": [{"page": 1,                       # 1-based
              "lines": [{"text": "...",
                         "quad": [[x, y], [x, y], [x, y], [x, y]],  # UL, UR, LL, LR
                         "rect": [x0, y0, x1, y1],                  # used when quad is missing
                         "confidence": 0.98}]}]}

Every line becomes invisible text (render mode 3) laid along the quad's
baseline and stretched horizontally to the quad's width, so selection
highlights cover the visible words. Glyphs are written in visual order with a
ToUnicode map back to the logical characters, and mixed Arabic / digit /
Latin lines also carry their logical text as /ActualText (see
ext/_rtl_text.py GlyphWriter), so lines search, copy and extract in logical
order in PDFKit, PyMuPDF and other readers.

Reply: {"ok": true, "output": ..., "pages": N, "lines": M, "skipped_lines": K}
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def _rtl():
    engine_dir = str(Path(__file__).resolve().parent.parent)
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    from ext import _rtl_text  # noqa: E402

    return _rtl_text


def _line_geometry(line: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float], float, float] | None:
    """(bottom-left point, unit baseline direction, width, height) in engine
    coordinates, from the quad (preferred) or the axis-aligned rect."""
    quad = line.get("quad")
    if isinstance(quad, list) and len(quad) == 4 and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in quad):
        (ulx, uly), (urx, ury), (llx, lly), (lrx, lry) = [(float(p[0]), float(p[1])) for p in quad]
        # Average the top and bottom edges: Vision quads are not always
        # perfect parallelograms.
        dx = ((lrx - llx) + (urx - ulx)) / 2
        dy = ((lry - lly) + (ury - uly)) / 2
        width = math.hypot(dx, dy)
        height = (math.hypot(ulx - llx, uly - lly) + math.hypot(urx - lrx, ury - lry)) / 2
        if width <= 0.5 or height <= 0.5:
            return None
        return (llx, lly), (dx / width, dy / width), width, height
    rect = line.get("rect")
    if isinstance(rect, list) and len(rect) == 4:
        x0, y0, x1, y1 = (float(v) for v in rect)
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if x1 - x0 <= 0.5 or y1 - y0 <= 0.5:
            return None
        return (x0, y1), (1.0, 0.0), x1 - x0, y1 - y0
    return None


def add_text_layer(doc: Any, pages: list[dict[str, Any]]) -> dict[str, int]:
    rt = _rtl()
    stats = {"pages": 0, "lines": 0, "skipped_lines": 0}
    for entry in pages:
        try:
            number = int(entry.get("page"))
        except (TypeError, ValueError):
            raise ValueError("every page entry needs a 1-based \"page\" number")
        if number < 1 or number > doc.page_count:
            raise ValueError(f"page {number} is out of range; document has {doc.page_count} pages")
        page = doc[number - 1]
        writer = rt.GlyphWriter(page)
        written = 0
        for line in entry.get("lines") or []:
            text = " ".join(str(line.get("text") or "").split())
            geometry = _line_geometry(line)
            if not text or geometry is None:
                stats["skipped_lines"] += 1
                continue
            font = rt.coverage_font_for(text)
            if font is None:
                # No single installed font has every character: keep the line
                # with the best-covering font and drop only what it lacks.
                font = rt.best_coverage_font(text)
                text = "".join(ch for ch in text if font is not None and font.covers(ch))
                if font is None or not text.strip():
                    stats["skipped_lines"] += 1
                    continue
            (bx, by), direction, width, height = geometry
            span = max(0.5, font.ascender - font.descender)
            size = max(1.0, height / span)
            rtl = rt.paragraph_is_rtl(text)
            # One nominal glyph per character: invisible text needs no
            # shaping, and 1:1 glyphs give the cleanest ToUnicode mapping.
            laid = rt._shape_line(font, text, size, rtl, 0, shaped=False)
            if laid.width <= 0:
                stats["skipped_lines"] += 1
                continue
            hscale = width / laid.width
            # Baseline sits |descender| above the quad's bottom edge.
            ux, uy = direction
            vx, vy = uy, -ux  # "up" in engine coordinates
            lift = -font.descender * size
            origin = (bx + vx * lift, by + vy * lift)
            writer.add_line(font, laid, size, origin, direction=direction, hscale=hscale, render_mode=3)
            written += 1
        if written:
            writer.commit()
            stats["pages"] += 1
            stats["lines"] += written
    return stats


def register(subparsers: Any, api: Any) -> None:
    def cmd_add_text_layer(args: argparse.Namespace) -> int:
        import fitz

        try:
            with open(args.json, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as error:
            return api.respond(False, message=f"cannot read --json: {error}")
        pages = payload.get("pages") if isinstance(payload, dict) else payload
        if not isinstance(pages, list):
            return api.respond(False, message="--json must contain a \"pages\" list")
        output = Path(args.output)
        api.ensure_parent(output)
        doc = fitz.open(args.input)
        try:
            stats = add_text_layer(doc, pages)
        except ValueError as error:
            doc.close()
            return api.respond(False, message=str(error))
        doc.save(str(output), garbage=3, deflate=True)
        doc.close()
        return api.respond(True, output=str(output), **stats)

    parser = subparsers.add_parser("add-text-layer", help="add an invisible OCR text layer from recognized lines")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--json", required=True, help="recognized lines (see module docstring)")
    parser.set_defaults(func=cmd_add_text_layer)
