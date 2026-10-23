"""Stamps & Split: watermark, header-footer, bates, split-advanced.

Placement model
---------------
Every stamp (watermark text/image, header/footer label, Bates number) is
drawn once onto a page of a private "stamp" document, cropped to its ink box
(`clip`), and then placed on target pages with ``Page.show_pdf_page``. That
gives one reusable Form XObject per stamp (small files, fast tiling), real
rotation by any angle, and opacity baked into the stamp's own graphics state.

Positions are computed in *visible* page space: PyMuPDF top-left origin,
relative to the page's CropBox, after /Rotate is applied (what the user sees,
and the same convention `page.rect` uses). They are mapped back to unrotated
page space with ``page.derotation_matrix`` and the page rotation is added to
the stamp rotation, so stamps stay upright on rotated pages and land
correctly on pages whose MediaBox does not start at (0, 0).

Rotation angles are degrees counter-clockwise as seen on screen.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

POSITIONS_9 = (
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
)
WATERMARK_POSITIONS = POSITIONS_9 + ("tile",)
SLOT_POSITIONS = ("top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right")

# Base-14 aliases accepted by --font.
_BASE14 = {
    "helv": "helv", "helvetica": "helv", "sans": "helv", "sans-serif": "helv",
    "hebo": "hebo", "helvetica-bold": "hebo",
    "tiro": "tiro", "times": "tiro", "serif": "tiro", "times-roman": "tiro",
    "tibo": "tibo", "times-bold": "tibo",
    "cour": "cour", "courier": "cour", "mono": "cour", "monospace": "cour",
    "cobo": "cobo", "courier-bold": "cobo",
}

# Installed fonts tried, in order, when base-14 cannot render the text.
_FALLBACK_FONTS = (
    "/System/Library/Fonts/SFArabic.ttf",
    "/System/Library/Fonts/GeezaPro.ttc",
    "/System/Library/Fonts/SFHebrew.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/Tahoma.ttf",
)

_STAMP_CANVAS = 14000  # pt; PDF page size limit is 14400


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _hex_color(value: str | None, default: tuple[float, float, float]) -> tuple[float, float, float]:
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        if value:
            raise ValueError(f"color must be #rrggbb, got {value!r}")
        return default
    try:
        number = int(raw, 16)
    except ValueError as exc:
        raise ValueError(f"color must be #rrggbb, got {value!r}") from exc
    return (((number >> 16) & 255) / 255.0, ((number >> 8) & 255) / 255.0, (number & 255) / 255.0)


def _css_color(rgb: tuple[float, float, float]) -> str:
    return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb)


def _base14_can_render(text: str) -> bool:
    # insert_text with a base-14 font uses a Latin-1 style simple encoding.
    return all(ord(ch) < 256 for ch in text if ch not in "\n\r\t")


def _font_covers(path: str, text: str) -> bool:
    import fitz

    try:
        font = fitz.Font(fontfile=path)
    except Exception:
        return False
    return all(font.has_glyph(ord(ch)) for ch in text if ch not in "\n\r\t ")


def _is_rtl(text: str) -> bool:
    for ch in text:
        kind = unicodedata.bidirectional(ch)
        if kind in ("R", "AL"):
            return True
        if kind == "L":
            return False
    return False


def resolve_font(name: str | None, text: str) -> dict[str, Any]:
    """Decide how to draw `text`.

    Returns {"kind": "base14", "fontname": alias} when a base-14 font can
    render every character, else {"kind": "file", "path": ttf} for an
    installed font that covers the text (drawn via insert_htmlbox so complex
    scripts such as Arabic get HarfBuzz shaping).
    """
    raw = (name or "helv").strip()
    if raw and (os.sep in raw or raw.lower().endswith((".ttf", ".otf", ".ttc"))):
        path = os.path.expanduser(raw)
        if not os.path.isfile(path):
            raise ValueError(f"font file not found: {raw}")
        if _font_covers(path, text):
            return {"kind": "file", "path": path}
        alias = "helv"
    else:
        alias = _BASE14.get(raw.lower())
        if alias is None:
            raise ValueError(f"unknown font {raw!r}; use helv, tiro, cour or a .ttf path")
    if _base14_can_render(text):
        return {"kind": "base14", "fontname": alias}
    for candidate in _FALLBACK_FONTS:
        if os.path.isfile(candidate) and _font_covers(candidate, text):
            return {"kind": "file", "path": candidate}
    # Nothing covers everything; the broadest font is still the best bet.
    for candidate in _FALLBACK_FONTS:
        if os.path.isfile(candidate):
            return {"kind": "file", "path": candidate}
    return {"kind": "base14", "fontname": alias}


def _ink_box(page: Any) -> Any:
    """Union of the text span boxes drawn on a stamp page."""
    import fitz

    box = fitz.Rect()
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip():
                    box |= fitz.Rect(span["bbox"])
    return box


class StampBook:
    """Private document holding one page per distinct stamp."""

    def __init__(self) -> None:
        import fitz

        self.doc = fitz.open()
        self._text_cache: dict[tuple, dict[str, Any]] = {}

    def close(self) -> None:
        self.doc.close()

    def text(self, text: str, font: dict[str, Any], size: float, color: tuple[float, float, float],
             opacity: float = 1.0, align: str = "center") -> dict[str, Any]:
        """Draw `text` on a new stamp page; return {pno, clip, width, height}."""
        key = (text, tuple(sorted(font.items())), size, color, opacity, align)
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        lines = text.split("\n")
        longest = max(len(line) for line in lines) or 1
        width = min(_STAMP_CANVAS, max(400.0, longest * size * 1.4 + 2 * size))
        height = min(_STAMP_CANVAS, max(200.0, len(lines) * size * 1.6 + 2 * size))
        page = self.doc.new_page(width=width, height=height)
        if font["kind"] == "base14":
            self._draw_base14(page, lines, font["fontname"], size, color, opacity, align)
        else:
            self._draw_html(page, text, font["path"], size, color, opacity, align)
        box = _ink_box(page)
        if box.is_empty:
            raise ValueError("could not render the stamp text")
        info = {"pno": page.number, "clip": box, "width": box.width, "height": box.height}
        self._text_cache[key] = info
        return info

    @staticmethod
    def _draw_base14(page: Any, lines: list[str], fontname: str, size: float,
                     color: tuple[float, float, float], opacity: float, align: str) -> None:
        import fitz

        widths = [fitz.get_text_length(line, fontname=fontname, fontsize=size) for line in lines]
        widest = max(widths) if widths else 0
        x0, y = size, size
        for line, line_width in zip(lines, widths):
            y += size * 1.2
            if align == "center":
                x = x0 + (widest - line_width) / 2
            elif align == "right":
                x = x0 + widest - line_width
            else:
                x = x0
            if line:
                page.insert_text((x, y), line, fontname=fontname, fontsize=size,
                                 color=color, fill_opacity=opacity)

    @staticmethod
    def _draw_html(page: Any, text: str, path: str, size: float,
                   color: tuple[float, float, float], opacity: float, align: str) -> None:
        import fitz

        folder, filename = os.path.split(path)
        archive = fitz.Archive(folder)
        css = (
            f"@font-face {{font-family: stampfont; src: url('{filename}');}}\n"
            f"* {{margin: 0; padding: 0;}}\n"
            f"div {{font-family: stampfont; font-size: {size:.2f}pt; color: {_css_color(color)};"
            f" white-space: pre; line-height: 1.2; text-align: {align};}}"
        )
        direction = "rtl" if _is_rtl(text) else "ltr"
        body = "<br>".join(html.escape(line) for line in text.split("\n"))
        markup = f'<div dir="{direction}">{body}</div>'
        rect = fitz.Rect(size, size, page.rect.width - size, page.rect.height - size)
        spare, _scale = page.insert_htmlbox(rect, markup, css=css, archive=archive,
                                            scale_low=1, opacity=opacity)
        if spare < 0:
            raise ValueError("stamp text is too large to lay out")

    def image(self, image_path: str, opacity: float) -> dict[str, Any]:
        import fitz

        if not os.path.isfile(image_path):
            raise ValueError(f"image not found: {image_path}")
        try:
            pix = fitz.Pixmap(image_path)
        except Exception as exc:
            raise ValueError(f"unsupported image: {image_path}") from exc
        if pix.colorspace is not None and pix.colorspace.n not in (1, 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)  # CMYK etc.
        aspect = pix.height / max(pix.width, 1)
        width = 1000.0
        height = max(1.0, width * aspect)
        page = self.doc.new_page(width=width, height=height)
        if opacity >= 0.999:
            page.insert_image(page.rect, filename=image_path)
        else:
            if not pix.alpha:
                pix = fitz.Pixmap(pix, 1)
            samples = bytes(pix.samples)
            alpha = samples[pix.n - 1::pix.n]
            table = bytes(int(round(value * opacity)) for value in range(256))
            pix.set_alpha(alpha.translate(table), premultiply=1)
            page.insert_image(page.rect, pixmap=pix)
        return {"pno": page.number, "clip": page.rect, "width": width, "height": height}


def _rotated_size(width: float, height: float, angle: float) -> tuple[float, float]:
    rad = math.radians(angle)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    return width * c + height * s, width * s + height * c


def _show(page: Any, book: StampBook, stamp: dict[str, Any], visible_rect: Any,
          angle: float, overlay: bool) -> bool:
    """Show a stamp inside `visible_rect` (visible page coordinates).

    `visible_rect` must already have the aspect of the rotated stamp.
    Returns False when the rect misses the page entirely.
    """
    import fitz

    target = (fitz.Rect(visible_rect) * page.derotation_matrix).normalize()
    unrotated_page = (fitz.Rect(page.rect) * page.derotation_matrix).normalize()
    if not target.intersects(unrotated_page):
        return False
    page.show_pdf_page(target, book.doc, stamp["pno"], clip=stamp["clip"],
                       rotate=(angle + page.rotation) % 360, overlay=overlay, keep_proportion=True)
    return True


def anchored_rect(page_rect: Any, width: float, height: float, position: str, margin: float) -> Any:
    """Rect of size width x height anchored at a named position (visible space)."""
    import fitz

    if position.endswith("left"):
        x0 = page_rect.x0 + margin
    elif position.endswith("right"):
        x0 = page_rect.x1 - margin - width
    else:
        x0 = page_rect.x0 + (page_rect.width - width) / 2
    if position.startswith("top"):
        y0 = page_rect.y0 + margin
    elif position.startswith("bottom"):
        y0 = page_rect.y1 - margin - height
    else:
        y0 = page_rect.y0 + (page_rect.height - height) / 2
    return fitz.Rect(x0, y0, x0 + width, y0 + height)


def tile_rects(page_rect: Any, width: float, height: float, max_tiles: int = 400) -> list[Any]:
    """Brick-pattern grid of rects covering the page (visible space)."""
    import fitz

    gap_x = max(24.0, width * 0.3)
    gap_y = max(24.0, height * 0.5)
    step_x, step_y = width + gap_x, height + gap_y
    cols = int(math.ceil(page_rect.width / step_x)) + 2
    rows = int(math.ceil(page_rect.height / step_y)) + 2
    while cols * rows > max_tiles:
        step_x *= 1.25
        step_y *= 1.25
        cols = int(math.ceil(page_rect.width / step_x)) + 2
        rows = int(math.ceil(page_rect.height / step_y)) + 2
    # Centre the grid on the page so the pattern looks balanced.
    origin_x = page_rect.x0 + page_rect.width / 2 - width / 2 - step_x * (cols // 2)
    origin_y = page_rect.y0 + page_rect.height / 2 - height / 2 - step_y * (rows // 2)
    rects = []
    for row in range(rows):
        shift = step_x / 2 if row % 2 else 0.0
        for col in range(cols + 1):
            x0 = origin_x + col * step_x + shift - step_x / 2
            y0 = origin_y + row * step_y
            rect = fitz.Rect(x0, y0, x0 + width, y0 + height)
            if rect.intersects(page_rect):
                rects.append(rect)
    return rects


def _open(path: str) -> Any:
    import fitz

    doc = fitz.open(path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("the PDF is password protected; unlock it first")
    if not doc.is_pdf:
        doc.close()
        raise ValueError("input is not a PDF")
    return doc


def _save(doc: Any, output: Path) -> None:
    doc.save(str(output), garbage=3, deflate=True)


def safe_part_name(text: str, fallback: str = "section") -> str:
    """Filesystem-safe name that keeps non-Latin letters (Arabic titles)."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    return cleaned[:80].rstrip("-_") or fallback


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def register(subparsers: Any, api: Any) -> None:

    # -- watermark ---------------------------------------------------------
    def cmd_watermark(args: argparse.Namespace) -> int:
        text = args.text
        if (text is None or not text.strip()) and not args.image:
            return api.respond(False, message="provide --text or --image")
        if text and args.image:
            return api.respond(False, message="use either --text or --image, not both")
        if args.position not in WATERMARK_POSITIONS:
            return api.respond(False, message=f"position must be one of {list(WATERMARK_POSITIONS)}")
        if args.layer not in ("over", "under"):
            return api.respond(False, message="layer must be over or under")
        if not 0.0 < args.opacity <= 1.0:
            return api.respond(False, message="opacity must be in (0, 1]")
        if args.font_size <= 0:
            return api.respond(False, message="font size must be positive")
        if not 0.0 < args.scale <= 4.0:
            return api.respond(False, message="scale must be between 0 and 4 (fraction of page width)")
        try:
            color = _hex_color(args.color, (0.5, 0.5, 0.5))
            doc = _open(args.input)
        except ValueError as exc:
            return api.respond(False, message=str(exc))

        output = Path(args.output)
        api.ensure_parent(output)
        book = StampBook()
        try:
            pages = api.parse_page_spec(args.pages, doc.page_count)
            font_info: dict[str, Any] = {}
            if args.image:
                stamp = book.image(args.image, args.opacity)
            else:
                font_info = resolve_font(args.font, text)
                stamp = book.text(text, font_info, args.font_size, color, args.opacity)
            angle = float(args.rotation) % 360
            overlay = args.layer == "over"
            placed = 0
            for index in pages:
                page = doc[index]
                visible = page.rect
                if args.image:
                    width = visible.width * args.scale
                    height = width * stamp["height"] / stamp["width"]
                else:
                    width, height = stamp["width"], stamp["height"]
                box_w, box_h = _rotated_size(width, height, angle)
                if args.position == "tile":
                    rects = tile_rects(visible, box_w, box_h)
                else:
                    margin = min(36.0, visible.width * 0.06, visible.height * 0.06)
                    rects = [anchored_rect(visible, box_w, box_h, args.position, margin)]
                for rect in rects:
                    if _show(page, book, stamp, rect, angle, overlay):
                        placed += 1
            _save(doc, output)
        except ValueError as exc:
            return api.respond(False, message=str(exc))
        finally:
            book.close()
            doc.close()
        payload: dict[str, Any] = {"output": str(output), "pages": len(pages), "stamps": placed}
        if font_info:
            payload["font"] = font_info.get("fontname") or font_info.get("path")
        return api.respond(True, **payload)

    wm = subparsers.add_parser("watermark", help="Stamp text or an image over or under pages")
    wm.add_argument("--input", required=True)
    wm.add_argument("--output", required=True)
    wm.add_argument("--text")
    wm.add_argument("--image")
    wm.add_argument("--font-size", type=float, default=60)
    wm.add_argument("--color", default="#808080")
    wm.add_argument("--opacity", type=float, default=0.3)
    wm.add_argument("--rotation", type=float, default=45, help="degrees counter-clockwise")
    wm.add_argument("--position", default="center")
    wm.add_argument("--layer", default="over")
    wm.add_argument("--scale", type=float, default=0.5, help="image width as a fraction of page width")
    wm.add_argument("--pages", default="all")
    wm.add_argument("--font", default="helv")
    wm.set_defaults(func=cmd_watermark)

    # -- shared slot drawing for header-footer and bates -------------------
    def draw_slots(doc: Any, pages: list[int], labels_for: Any, font_name: str | None, size: float,
                   color: tuple[float, float, float], margin: float) -> int:
        """labels_for(position_in_selection, page_index) -> {slot: text}."""
        book = StampBook()
        fonts: dict[str, dict[str, Any]] = {}
        drawn = 0
        try:
            # Phase 1: draw every stamp. The target document caches a graft
            # map of the stamp book on first show_pdf_page, so the book must
            # be complete before anything is placed.
            plan: list[tuple[int, str, dict[str, Any]]] = []
            for order, index in enumerate(pages):
                for slot, text in labels_for(order, index).items():
                    if not text or not text.strip():
                        continue
                    font = fonts.get(text)
                    if font is None:
                        font = resolve_font(font_name, text)
                        fonts[text] = font
                    align = "left" if slot.endswith("left") else ("right" if slot.endswith("right") else "center")
                    plan.append((index, slot, book.text(text, font, size, color, 1.0, align)))
            # Phase 2: place them.
            for index, slot, stamp in plan:
                page = doc[index]
                rect = anchored_rect(page.rect, stamp["width"], stamp["height"], slot, margin)
                if _show(page, book, stamp, rect, 0.0, True):
                    drawn += 1
        finally:
            book.close()
        return drawn

    # -- header-footer -----------------------------------------------------
    slot_args = {
        "header_left": "top-left", "header_center": "top-center", "header_right": "top-right",
        "footer_left": "bottom-left", "footer_center": "bottom-center", "footer_right": "bottom-right",
    }

    def cmd_header_footer(args: argparse.Namespace) -> int:
        templates = {slot: getattr(args, name) for name, slot in slot_args.items() if getattr(args, name)}
        if not templates:
            return api.respond(False, message="provide at least one header or footer slot")
        if args.font_size <= 0:
            return api.respond(False, message="font size must be positive")
        if args.margin < 0:
            return api.respond(False, message="margin must not be negative")
        try:
            color = _hex_color(args.color, (0.2, 0.2, 0.2))
            doc = _open(args.input)
        except ValueError as exc:
            return api.respond(False, message=str(exc))
        output = Path(args.output)
        api.ensure_parent(output)
        try:
            pages = api.parse_page_spec(args.pages, doc.page_count)
            filename = args.filename or Path(args.input).name
            title = args.title or (doc.metadata or {}).get("title") or Path(filename).stem
            try:
                date_text = _dt.date.today().strftime(args.date_format)
            except ValueError:
                return api.respond(False, message=f"invalid date format {args.date_format!r}")
            last_number = args.start_number + len(pages) - 1

            def labels_for(order: int, _index: int) -> dict[str, str]:
                values = {
                    "page": str(args.start_number + order),
                    "pages": str(last_number),
                    "date": date_text,
                    "title": title,
                    "filename": filename,
                }
                return {
                    slot: re.sub(r"\{(page|pages|date|title|filename)\}", lambda m: values[m.group(1)], template)
                    for slot, template in templates.items()
                }

            drawn = draw_slots(doc, pages, labels_for, args.font, args.font_size, color, args.margin)
            _save(doc, output)
        except ValueError as exc:
            return api.respond(False, message=str(exc))
        finally:
            doc.close()
        return api.respond(True, output=str(output), pages=len(pages), labels=drawn)

    hf = subparsers.add_parser("header-footer", help="Running headers and footers with tokens")
    hf.add_argument("--input", required=True)
    hf.add_argument("--output", required=True)
    for name in slot_args:
        hf.add_argument("--" + name.replace("_", "-"), dest=name, default="")
    hf.add_argument("--font", default="helv")
    hf.add_argument("--font-size", type=float, default=10)
    hf.add_argument("--color", default="#333333")
    hf.add_argument("--margin", type=float, default=28)
    hf.add_argument("--start-number", type=int, default=1)
    hf.add_argument("--pages", default="all")
    hf.add_argument("--date-format", default="%Y-%m-%d")
    hf.add_argument("--filename", help="name for {filename} (defaults to the input file name)")
    hf.add_argument("--title", help="value for {title} (defaults to metadata title, then file stem)")
    hf.set_defaults(func=cmd_header_footer)

    # -- bates -------------------------------------------------------------
    def cmd_bates(args: argparse.Namespace) -> int:
        if args.position not in SLOT_POSITIONS:
            return api.respond(False, message=f"position must be one of {list(SLOT_POSITIONS)}")
        if args.start < 0:
            return api.respond(False, message="start must not be negative")
        if not 1 <= args.digits <= 15:
            return api.respond(False, message="digits must be between 1 and 15")
        if args.font_size <= 0:
            return api.respond(False, message="font size must be positive")
        try:
            color = _hex_color(args.color, (0.0, 0.0, 0.0))
            doc = _open(args.input)
        except ValueError as exc:
            return api.respond(False, message=str(exc))
        output = Path(args.output)
        api.ensure_parent(output)

        def ident(number: int) -> str:
            return f"{args.prefix}{str(number).zfill(args.digits)}{args.suffix}"

        try:
            pages = api.parse_page_spec(args.pages, doc.page_count)
            drawn = draw_slots(
                doc, pages,
                lambda order, _index: {args.position: ident(args.start + order)},
                args.font, args.font_size, color, args.margin,
            )
            _save(doc, output)
        except ValueError as exc:
            return api.respond(False, message=str(exc))
        finally:
            doc.close()
        if not pages:
            return api.respond(False, message="no pages selected")
        return api.respond(True, output=str(output), pages=len(pages), labels=drawn,
                           first=ident(args.start), last=ident(args.start + len(pages) - 1),
                           next_number=args.start + len(pages))

    bt = subparsers.add_parser("bates", help="Sequential legal identifiers")
    bt.add_argument("--input", required=True)
    bt.add_argument("--output", required=True)
    bt.add_argument("--prefix", default="")
    bt.add_argument("--suffix", default="")
    bt.add_argument("--start", type=int, default=1)
    bt.add_argument("--digits", type=int, default=6)
    bt.add_argument("--position", default="bottom-right")
    bt.add_argument("--font", default="helv")
    bt.add_argument("--font-size", type=float, default=10)
    bt.add_argument("--color", default="#000000")
    bt.add_argument("--margin", type=float, default=24)
    bt.add_argument("--pages", default="all")
    bt.set_defaults(func=cmd_bates)

    # -- split-advanced ----------------------------------------------------
    def parse_ranges(spec: str, total: int) -> list[tuple[int, int]]:
        parts: list[tuple[int, int]] = []
        for chunk in (spec or "").split(","):
            item = chunk.strip()
            if not item:
                continue
            match = re.fullmatch(r"(\d+)?\s*-\s*(\d+)?|(\d+)", item)
            if not match:
                raise ValueError(f"bad range {item!r}; use e.g. 1-3,4-6,7-")
            if match.group(3):
                start = end = int(match.group(3))
            else:
                start = int(match.group(1)) if match.group(1) else 1
                end = int(match.group(2)) if match.group(2) else total
            if start > end:
                start, end = end, start
            if start < 1 or end > total:
                raise ValueError(f"range {item!r} is outside 1-{total}")
            parts.append((start - 1, end - 1))
        if not parts:
            raise ValueError("--ranges is empty")
        return parts

    def normalized_toc(entries: list[list[Any]]) -> list[list[Any]]:
        result: list[list[Any]] = []
        previous = 0
        for level, title, page in entries:
            level = max(1, min(level, previous + 1))
            result.append([level, title, page])
            previous = level
        return result

    def write_part(doc: Any, toc: list[list[Any]], first: int, last: int, path: Path) -> int:
        import fitz

        part = fitz.open()
        part.insert_pdf(doc, from_page=first, to_page=last)
        entries = [[lvl, title, page - first] for lvl, title, page in toc if first + 1 <= page <= last + 1]
        if entries:
            min_level = min(entry[0] for entry in entries)
            for entry in entries:
                entry[0] -= min_level - 1
            try:
                part.set_toc(normalized_toc(entries))
            except Exception:
                pass
        metadata = {k: v for k, v in (doc.metadata or {}).items() if k not in ("format", "encryption") and v}
        if metadata:
            part.set_metadata(metadata)
        part.save(str(path), garbage=3, deflate=True)
        part.close()
        return path.stat().st_size

    def page_bytes(doc: Any, first: int, last: int) -> int:
        import fitz

        part = fitz.open()
        part.insert_pdf(doc, from_page=first, to_page=last)
        size = len(part.tobytes(garbage=3, deflate=True))
        part.close()
        return size

    def size_groups(doc: Any, max_bytes: int) -> list[tuple[int, int]]:
        estimates = [page_bytes(doc, i, i) for i in range(doc.page_count)]
        groups: list[tuple[int, int]] = []
        start, running = 0, 0
        for index, size in enumerate(estimates):
            if index > start and running + size > max_bytes:
                groups.append((start, index - 1))
                start, running = index, 0
            running += size
        groups.append((start, doc.page_count - 1))
        # Shared resources make estimates pessimistic, but verify anyway and
        # bisect any group that still ends up too large.
        verified: list[tuple[int, int]] = []
        pending = list(reversed(groups))
        while pending:
            first, last = pending.pop()
            if last > first and page_bytes(doc, first, last) > max_bytes:
                middle = (first + last) // 2
                pending.append((middle + 1, last))
                pending.append((first, middle))
            else:
                verified.append((first, last))
        return verified

    def cmd_split_advanced(args: argparse.Namespace) -> int:
        try:
            doc = _open(args.input)
        except ValueError as exc:
            return api.respond(False, message=str(exc))
        output_dir = Path(args.output_dir)
        stem = safe_part_name(args.basename or Path(args.input).stem, "document")
        total = doc.page_count
        try:
            toc = doc.get_toc(simple=True)
            names: list[str | None] = []
            if args.mode == "ranges":
                groups = parse_ranges(args.ranges, total)
            elif args.mode == "every":
                if not args.every or args.every < 1:
                    raise ValueError("--every must be a positive page count")
                groups = [(i, min(i + args.every, total) - 1) for i in range(0, total, args.every)]
            elif args.mode == "bookmarks":
                tops = sorted({(page - 1, title) for level, title, page in toc if level == 1 and page >= 1},
                              key=lambda item: item[0])
                starts: list[tuple[int, str]] = []
                for page, title in tops:
                    if starts and starts[-1][0] == page:
                        continue  # two bookmarks on one page -> one part
                    starts.append((page, title))
                if not starts:
                    raise ValueError("this PDF has no top-level bookmarks to split at")
                groups, names = [], []
                if starts[0][0] > 0:
                    groups.append((0, starts[0][0] - 1))
                    names.append("Front-matter")
                for i, (page, title) in enumerate(starts):
                    end = starts[i + 1][0] - 1 if i + 1 < len(starts) else total - 1
                    groups.append((page, end))
                    names.append(title)
            elif args.mode == "size":
                if not args.max_mb or args.max_mb <= 0:
                    raise ValueError("--max-mb must be positive")
                groups = size_groups(doc, int(args.max_mb * 1024 * 1024))
            else:
                raise ValueError("mode must be ranges, every, bookmarks or size")

            if args.dry_run:
                plan = [{"first": a + 1, "last": b + 1, "pages": b - a + 1,
                         **({"title": names[i]} if names else {})} for i, (a, b) in enumerate(groups)]
                return api.respond(True, parts=len(plan), plan=plan, dry_run=True)

            output_dir.mkdir(parents=True, exist_ok=True)
            width = max(2, len(str(len(groups))))
            outputs = []
            used: set[str] = set()
            limit = int(args.max_mb * 1024 * 1024) if args.mode == "size" else None
            for i, (first, last) in enumerate(groups):
                number = str(i + 1).zfill(width)
                if names:
                    filename = f"{stem}_{number}-{safe_part_name(names[i] or '')}.pdf"
                else:
                    filename = f"{stem}_part-{number}.pdf"
                while filename.lower() in used:
                    filename = filename[:-4] + "-1.pdf"
                used.add(filename.lower())
                path = output_dir / filename
                size = write_part(doc, toc, first, last, path)
                entry: dict[str, Any] = {
                    "path": str(path), "pages": last - first + 1,
                    "first": first + 1, "last": last + 1, "bytes": size,
                }
                if names:
                    entry["title"] = names[i]
                if limit is not None and size > limit:
                    entry["oversize"] = True
                outputs.append(entry)
        except ValueError as exc:
            return api.respond(False, message=str(exc))
        finally:
            doc.close()
        return api.respond(True, output_dir=str(output_dir), outputs=outputs, parts=len(outputs))

    sp = subparsers.add_parser("split-advanced", help="Split by ranges, page count, bookmarks or size")
    sp.add_argument("--input", required=True)
    sp.add_argument("--output-dir", required=True)
    sp.add_argument("--mode", required=True, choices=["ranges", "every", "bookmarks", "size"])
    sp.add_argument("--ranges", default="")
    sp.add_argument("--every", type=int, default=0)
    sp.add_argument("--max-mb", type=float, default=0)
    sp.add_argument("--basename", help="file name stem for the parts (defaults to the input stem)")
    sp.add_argument("--dry-run", action="store_true", help="report the planned parts without writing")
    sp.set_defaults(func=cmd_split_advanced)
