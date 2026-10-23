"""Create PDFs from text, Markdown, HTML, images and existing PDFs.

Command ``convert-to-pdf`` lays out .txt/.md/.html with PyMuPDF's Story (HTML +
CSS, HarfBuzz shaping, bidi), places images fitted to the page, passes PDFs
through, and concatenates everything in input order into one output PDF.
Rich office formats (.docx/.doc/.rtf/.rtfd/.odt) are converted on the Swift
side with AppKit (Services/OfficeToPDF.swift) and arrive here as PDFs when the
caller wants them combined.

Arabic/Hebrew: paragraphs whose first strong character is RTL get dir="rtl";
text is set in Arial/Courier New (system fonts with Arabic + Hebrew glyphs),
whose embedded ToUnicode maps are then rewritten from presentation forms to
base letters so extracted text is in logical order and searchable.
"""
from __future__ import annotations

import argparse
import html as htmllib
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# Points (1/72 in). A4 is 210 x 297 mm.
PAGE_SIZES = {
    "a4": (595.28, 841.89),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
}
# (top, right, bottom, left) like Word's Narrow / Normal / Wide presets.
MARGINS = {
    "narrow": (36.0, 36.0, 36.0, 36.0),
    "normal": (72.0, 72.0, 72.0, 72.0),
    "wide": (72.0, 144.0, 72.0, 144.0),
}

TEXT_EXTS = {".txt", ".text", ".log"}
MARKDOWN_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
HTML_EXTS = {".html", ".htm", ".xhtml"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
PDF_EXTS = {".pdf"}

SUPPLEMENTAL_FONTS = Path("/System/Library/Fonts/Supplemental")
FONT_FACES = {
    # family: [(file, weight, style)]
    "ps-sans": [
        ("Arial.ttf", "normal", "normal"),
        ("Arial Bold.ttf", "bold", "normal"),
        ("Arial Italic.ttf", "normal", "italic"),
        ("Arial Bold Italic.ttf", "bold", "italic"),
    ],
    "ps-mono": [
        ("Courier New.ttf", "normal", "normal"),
        ("Courier New Bold.ttf", "bold", "normal"),
        ("Courier New Italic.ttf", "normal", "italic"),
        ("Courier New Bold Italic.ttf", "bold", "italic"),
    ],
}

BASE_CSS = """
body { margin: 0; padding: 0; font-family: %(sans)s; font-size: 11pt; line-height: 1.45; color: #1d1d1f; }
p { margin: 0 0 7pt 0; }
h1, h2, h3, h4, h5, h6 { font-weight: bold; line-height: 1.2; color: #111114; }
h1 { font-size: 22pt; margin: 4pt 0 10pt 0; }
h2 { font-size: 17pt; margin: 14pt 0 7pt 0; }
h3 { font-size: 14pt; margin: 12pt 0 6pt 0; }
h4 { font-size: 12pt; margin: 10pt 0 5pt 0; }
h5, h6 { font-size: 11pt; margin: 8pt 0 4pt 0; }
a { color: #2f5bd8; text-decoration: none; }
ul, ol { margin: 0 0 7pt 0; padding-left: 20pt; }
li { margin: 0 0 2pt 0; }
blockquote { margin: 6pt 0 8pt 0; padding: 2pt 0 2pt 12pt; border-left: 3pt solid #c9cbd3; color: #4a4c55; }
code { font-family: %(mono)s; font-size: 9.5pt; background-color: #f2f3f6; }
pre { font-family: %(mono)s; font-size: 9pt; line-height: 1.35; background-color: #f4f5f8;
      padding: 7pt 9pt; margin: 4pt 0 9pt 0; white-space: pre-wrap; }
pre code { background-color: transparent; }
table { border-collapse: collapse; margin: 4pt 0 10pt 0; }
th, td { border: 0.6pt solid #c4c6ce; padding: 3pt 6pt; vertical-align: top; }
th { background-color: #f0f1f5; font-weight: bold; }
hr { border: none; border-top: 0.8pt solid #d0d2d9; margin: 10pt 0; }
img { max-width: 100%%; }
p.ps-line { margin: 0; white-space: pre-wrap; }
p.ps-blank { margin: 0; }
"""

RTL_RANGES = (
    (0x0590, 0x08FF),  # Hebrew, Arabic, Syriac, Thaana, NKo, Samaritan, Arabic Ext.
    (0xFB1D, 0xFDFF),  # Hebrew + Arabic presentation forms A
    (0xFE70, 0xFEFF),  # Arabic presentation forms B
    (0x10800, 0x10FFF),
    (0x1E800, 0x1EFFF),
)


def _is_rtl_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in RTL_RANGES)


def first_strong_direction(text: str) -> str | None:
    """'rtl' / 'ltr' from the first strong character (Unicode bidi P2), else None."""
    for ch in text:
        if _is_rtl_char(ch):
            if unicodedata.bidirectional(ch) in ("R", "AL"):
                return "rtl"
            continue
        if unicodedata.bidirectional(ch) == "L":
            return "ltr"
    return None


BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "blockquote",
    "div", "dd", "dt", "caption", "figcaption", "pre", "section", "article",
}


class _AutoDir(HTMLParser):
    """Pass HTML through unchanged, adding dir="rtl" to block elements whose
    first strong character is right-to-left (an approximation of dir=auto,
    which MuPDF's layout engine does not implement)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.pending: list[int] = []  # indices in out of undecided block tags
        self.stack: list[tuple[str, bool]] = []  # (tag, had explicit dir)
        self.skip = 0  # inside <script>/<style>/<head>

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or f"<{tag}>"
        self.out.append(raw)
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
            return
        if tag in BLOCK_TAGS and not any(name == "dir" for name, _ in attrs):
            self.pending.append(len(self.out) - 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.out.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        self.out.append(f"</{tag}>")
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
            return
        if tag in BLOCK_TAGS:
            # An empty block closes undecided; drop the innermost pending
            # entry that belongs to this tag.
            for pos in range(len(self.pending) - 1, -1, -1):
                if self.out[self.pending[pos]].lower().startswith(f"<{tag}"):
                    del self.pending[pos]
                    break

    def _text(self, text: str) -> None:
        if self.skip or not self.pending:
            return
        direction = first_strong_direction(text)
        if direction is None:
            return
        if direction == "rtl":
            for index in self.pending:
                tag_text = self.out[index]
                close = "/>" if tag_text.endswith("/>") else ">"
                self.out[index] = tag_text[: -len(close)].rstrip() + ' dir="rtl"' + close
        self.pending.clear()

    def handle_data(self, data: str) -> None:
        self.out.append(data)
        self._text(data)

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")
        self._text(htmllib.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")
        self._text(htmllib.unescape(f"&#{name};"))

    def handle_comment(self, data: str) -> None:
        self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.out.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.out.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self.out.append(f"<![{data}]>")


def auto_dir(html: str) -> str:
    parser = _AutoDir()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1256" if _looks_like_cp1256(data) else "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _looks_like_cp1256(data: bytes) -> bool:
    """Heuristic for legacy Windows-Arabic text (lots of 0xC1-0xED bytes)."""
    high = sum(1 for b in data[:4000] if 0xC1 <= b <= 0xED)
    return high > max(20, len(data[:4000]) // 6)


def text_to_html(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    parts = []
    for line in lines:
        if not line.strip():
            parts.append('<p class="ps-blank">&#160;</p>')
            continue
        escaped = htmllib.escape(line.expandtabs(4), quote=False)
        # Keep indentation and runs of spaces (pre-wrap is honoured, but
        # leading spaces are the part readers actually notice).
        escaped = re.sub(r"^( +)", lambda m: "&#160;" * len(m.group(1)), escaped)
        escaped = re.sub(r"  +", lambda m: " " + "&#160;" * (len(m.group(0)) - 1), escaped)
        parts.append(f'<p class="ps-line">{escaped}</p>')
    return "<html><body>" + "\n".join(parts) + "</body></html>"


def markdown_to_html(text: str) -> str:
    try:
        from markdown_it import MarkdownIt
    except ImportError:  # pragma: no cover - requirements list markdown-it-py
        return text_to_html(text)
    md = MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": True})
    md.enable(["table", "strikethrough"])
    return "<html><body>" + md.render(text) + "</body></html>"


def read_html_file(path: Path) -> str:
    data = path.read_bytes()
    head = data[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", head, re.I)
    encodings = [match.group(1)] if match else []
    encodings += ["utf-8-sig", "cp1256" if _looks_like_cp1256(data) else "cp1252"]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1")


def _font_css() -> tuple[str, dict[str, str]]:
    """@font-face rules for the bundled-with-macOS fonts, plus family names."""
    rules = []
    families = {"sans": "sans-serif", "mono": "monospace"}
    for family, faces in FONT_FACES.items():
        available = [face for face in faces if (SUPPLEMENTAL_FONTS / face[0]).exists()]
        if not any(weight == "normal" and style == "normal" for _, weight, style in available):
            continue
        for filename, weight, style in available:
            rules.append(
                f"@font-face {{ font-family: {family}; src: url('{filename}'); "
                f"font-weight: {weight}; font-style: {style}; }}"
            )
        families["sans" if family == "ps-sans" else "mono"] = family
    return "\n".join(rules), families


def inject_base_style(html: str, css: str) -> str:
    """Insert <style>css</style> before any author styles."""
    tag = f"<style>{css}</style>"
    for pattern in (r"<head\b[^>]*>", r"<html\b[^>]*>"):
        match = re.search(pattern, html, re.I)
        if match:
            return html[: match.end()] + tag + html[match.end():]
    return tag + html


def render_html(html: str, base_dir: Path | None, page_size: tuple[float, float],
                margins: tuple[float, float, float, float]) -> Any:
    """Lay out HTML across as many pages as needed; returns a pymupdf Document."""
    import pymupdf

    font_rules, families = _font_css()
    archive = pymupdf.Archive()
    if SUPPLEMENTAL_FONTS.is_dir():
        archive.add(str(SUPPLEMENTAL_FONTS))
    if base_dir is not None and base_dir.is_dir():
        archive.add(str(base_dir))

    # MuPDF gives user_css precedence over the document's own styles, so only
    # the @font-face rules go there; the house style is injected as the first
    # <style> element so the author's CSS still wins.
    story = pymupdf.Story(html=inject_base_style(auto_dir(html), BASE_CSS % families),
                          user_css=font_rules, archive=archive)
    width, height = page_size
    top, right, bottom, left = margins
    mediabox = pymupdf.Rect(0, 0, width, height)
    where = pymupdf.Rect(left, top, width - right, height - bottom)

    def rectfn(rect_num: int, filled: Any) -> tuple[Any, Any, Any]:
        return mediabox, where, None

    return story.write_with_links(rectfn)


def image_pages(path: Path) -> list[tuple[bytes, int, int]]:
    """(encoded image, width, height) per frame, EXIF orientation applied."""
    import io

    from PIL import Image, ImageOps, ImageSequence

    frames = []
    with Image.open(path) as image:
        sequence = [image] if path.suffix.lower() in (".gif", ".webp") else list(ImageSequence.Iterator(image))
        for frame in sequence:
            frame = ImageOps.exif_transpose(frame)
            if frame.mode in ("RGBA", "LA", "P"):
                frame = frame.convert("RGBA")
                background = Image.new("RGB", frame.size, (255, 255, 255))
                background.paste(frame, mask=frame.split()[-1])
                frame = background
            elif frame.mode not in ("RGB", "L"):
                frame = frame.convert("RGB")
            buf = io.BytesIO()
            if path.suffix.lower() in (".jpg", ".jpeg"):
                frame.save(buf, "JPEG", quality=92)
            else:
                frame.save(buf, "PNG", optimize=True)
            frames.append((buf.getvalue(), frame.width, frame.height))
    return frames


def add_image_pages(doc: Any, path: Path, page_size: tuple[float, float],
                    margins: tuple[float, float, float, float]) -> int:
    import pymupdf

    count = 0
    for data, w, h in image_pages(path):
        pw, ph = page_size
        if w > h and pw < ph:  # landscape image -> landscape page
            pw, ph = ph, pw
        top, right, bottom, left = margins
        page = doc.new_page(width=pw, height=ph)
        box = pymupdf.Rect(left, top, pw - right, ph - bottom)
        # Fit inside the margins, centered; small images are enlarged at most
        # 2x (1 px = 1 pt) so icons and screenshots don't turn into mush.
        scale = min(box.width / w, box.height / h, 2.0)
        iw, ih = w * scale, h * scale
        x0 = box.x0 + (box.width - iw) / 2
        y0 = box.y0 + (box.height - ih) / 2
        page.insert_image(pymupdf.Rect(x0, y0, x0 + iw, y0 + ih), stream=data, keep_proportion=True)
        count += 1
    return count


# --- ToUnicode clean-up ----------------------------------------------------

#: Code points that share a glyph with a more common Arabic character in most
#: fonts; reverse cmap lookups pick the higher code point, so map them back.
#: (Shared Arabic-Indic/Persian digit glyphs are deliberately left as Persian
#: digits: those are bidi class EN, and MuPDF's extraction keeps the line in
#: logical order with EN digits but flips segments containing AN digits.)
SHARED_GLYPH_FIXES = {
    0x066C: "\u060c",  # Arabic thousands separator -> Arabic comma
    0x00AD: "-",  # soft hyphen shares the hyphen glyph; MuPDF only draws it at breaks
}


def _logical_text(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0xA0:
            out.append(" ")
        elif cp in SHARED_GLYPH_FIXES:
            out.append(SHARED_GLYPH_FIXES[cp])
        elif 0xFB00 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF:
            norm = unicodedata.normalize("NFKC", ch)
            if 0xFE70 <= cp <= 0xFE7F:  # isolated/medial harakat carry space/tatweel
                norm = norm.replace(" ", "").replace("\u0640", "") or norm
            out.append(norm)
        else:
            out.append(ch)
    return "".join(out)


def _utf16_hex_to_str(hexstr: str) -> str:
    return bytes.fromhex(hexstr).decode("utf-16-be", errors="replace")


def _str_to_utf16_hex(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()


def fix_cmap(cmap: str) -> str:
    """Rewrite bfchar/bfrange entries that map to presentation forms (Arabic
    contextual forms, lam-alef, fi/fl ligatures) or NBSP so extraction yields
    base letters. Returns the input unchanged when nothing needs fixing."""
    section_re = re.compile(r"(\d+)\s+begin(bfchar|bfrange)(.*?)end\2", re.S)
    chars: list[tuple[str, str]] = []
    ranges: list[tuple[str, str, str]] = []
    changed = False
    for match in section_re.finditer(cmap):
        kind, body = match.group(2), match.group(3)
        if kind == "bfchar":
            for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", body):
                text = _utf16_hex_to_str(dst)
                fixed = _logical_text(text)
                changed |= fixed != text
                chars.append((src, _str_to_utf16_hex(fixed) if fixed != text else dst))
        else:
            for item in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", body):
                lo, hi, dst = item.groups()
                lo_i, hi_i = int(lo, 16), int(hi, 16)
                width = len(lo)
                if dst.startswith("["):
                    for offset, code in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", dst)):
                        text = _utf16_hex_to_str(code)
                        fixed = _logical_text(text)
                        changed |= fixed != text
                        chars.append((format(lo_i + offset, f"0{width}X"), _str_to_utf16_hex(fixed)))
                    continue
                base_hex = dst[1:-1]
                if len(base_hex) != 4:  # multi-unit start: leave untouched
                    ranges.append((lo, hi, base_hex))
                    continue
                base = int(base_hex, 16)
                needs = any(_logical_text(chr(base + i)) != chr(base + i)
                            for i in range(hi_i - lo_i + 1) if not 0xD800 <= base + i <= 0xDFFF)
                if not needs:
                    ranges.append((lo, hi, base_hex))
                    continue
                changed = True
                for i in range(hi_i - lo_i + 1):
                    ch = chr(base + i)
                    chars.append((format(lo_i + i, f"0{width}X"), _str_to_utf16_hex(_logical_text(ch))))
    if not changed:
        return cmap

    def sections(entries: list[str], kind: str) -> list[str]:
        blocks = []
        for start in range(0, len(entries), 100):
            chunk = entries[start:start + 100]
            blocks.append(f"{len(chunk)} begin{kind}\n" + "\n".join(chunk) + f"\nend{kind}")
        return blocks

    body = sections([f"<{s}> <{d}>" for s, d in chars], "bfchar")
    body += sections([f"<{lo}> <{hi}> <{d}>" for lo, hi, d in ranges], "bfrange")
    first = section_re.search(cmap)
    last = None
    for last in section_re.finditer(cmap):
        pass
    assert first is not None and last is not None
    return cmap[: first.start()] + "\n".join(body) + cmap[last.end():]


def normalize_tounicode(doc: Any) -> int:
    """Apply fix_cmap to every ToUnicode stream; returns streams changed."""
    changed = 0
    seen: set[int] = set()
    for xref in range(1, doc.xref_length()):
        try:
            kind, value = doc.xref_get_key(xref, "ToUnicode")
        except Exception:
            continue
        if kind != "xref":
            continue
        stream_xref = int(value.split()[0])
        if stream_xref in seen:
            continue
        seen.add(stream_xref)
        try:
            original = doc.xref_stream(stream_xref).decode("latin-1")
        except Exception:
            continue
        fixed = fix_cmap(original)
        if fixed != original:
            doc.update_stream(stream_xref, fixed.encode("latin-1"))
            changed += 1
    return changed


# --- Command ---------------------------------------------------------------

def input_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return "text"
    if ext in MARKDOWN_EXTS:
        return "markdown"
    if ext in HTML_EXTS:
        return "html"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    return None


def register(subparsers: Any, api: Any) -> None:
    def cmd_convert_to_pdf(args: argparse.Namespace) -> int:
        import pymupdf

        page_size = PAGE_SIZES[args.page_size]
        margins = MARGINS[args.margins]
        inputs = [Path(item) for item in args.input]
        for path in inputs:
            if not path.is_file():
                return api.respond(False, message=f"input not found: {path}")
            if input_kind(path) is None:
                return api.respond(
                    False,
                    message=f"unsupported file type: {path.name} (supported: txt, md, html, images, pdf; "
                            "Word/RTF/ODT are converted by the app)",
                )

        output = Path(args.output)
        api.ensure_parent(output)
        out = pymupdf.open()
        report = []
        for path in inputs:
            kind = input_kind(path)
            before = out.page_count
            if kind == "pdf":
                with pymupdf.open(path) as src:
                    if src.needs_pass:
                        return api.respond(False, message=f"{path.name} is password-protected")
                    out.insert_pdf(src)
            elif kind == "image":
                add_image_pages(out, path, page_size, margins)
            else:
                if kind == "text":
                    html = text_to_html(read_text_file(path))
                elif kind == "markdown":
                    html = markdown_to_html(read_text_file(path))
                else:
                    html = read_html_file(path)
                rendered = render_html(html, path.parent, page_size, margins)
                out.insert_pdf(rendered)
                rendered.close()
            report.append({"input": str(path), "kind": kind, "pages": out.page_count - before})

        if out.page_count == 0:
            return api.respond(False, message="nothing to convert")
        try:
            out.subset_fonts()
        except Exception:
            pass  # subsetting is an optimisation only
        fixed_maps = normalize_tounicode(out)
        title = args.title or inputs[0].stem
        out.set_metadata({"title": title, "creator": "PDF Studio", "producer": f"PDF Studio engine {api.version}"})
        out.save(str(output), garbage=3, deflate=True)
        pages = out.page_count
        out.close()
        return api.respond(
            True,
            output=str(output),
            pages=pages,
            bytes=output.stat().st_size,
            inputs=report,
            tounicode_fixed=fixed_maps,
        )

    parser = subparsers.add_parser("convert-to-pdf", help="Text, Markdown, HTML, images or PDFs to one PDF")
    parser.add_argument("--input", action="append", required=True, help="repeat; combined in order")
    parser.add_argument("--output", required=True)
    parser.add_argument("--page-size", choices=sorted(PAGE_SIZES), default="a4")
    parser.add_argument("--margins", choices=sorted(MARGINS), default="normal")
    parser.add_argument("--title")
    parser.set_defaults(func=cmd_convert_to_pdf)

    def cmd_fix_text_map(args: argparse.Namespace) -> int:
        import pymupdf

        output = Path(args.output)
        api.ensure_parent(output)
        with pymupdf.open(args.input) as doc:
            fixed = normalize_tounicode(doc)
            doc.save(str(output), garbage=3, deflate=True)
        return api.respond(True, output=str(output), tounicode_fixed=fixed)

    fix = subparsers.add_parser(
        "fix-text-map",
        help="Rewrite ToUnicode maps from presentation forms to base letters (better copy/search)",
    )
    fix.add_argument("--input", required=True)
    fix.add_argument("--output", required=True)
    fix.set_defaults(func=cmd_fix_text_map)
