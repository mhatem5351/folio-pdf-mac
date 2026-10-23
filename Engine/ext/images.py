"""Edit EXISTING images, one placement at a time.

A *placement* is one ``/Name Do`` invocation of an image XObject, reached
from a page's content stream either directly or through Form XObjects.
It is identified by (page, xref, placement_index), where placement_index is
the 0-based order of that xref's invocations on that page in paint order
(the same order PyMuPDF's ``page.get_image_info()`` reports).

Why not PyMuPDF's built-ins? ``Page.replace_image`` overwrites the image
object itself (``xref_copy`` into the shared xref), so EVERY placement in the
document changes; ``Page.delete_image`` is replace_image with a 1x1
transparent pixmap (same doc-wide effect, plus it rewrites the page's last
content stream). Redaction would also cut through overlapping text/vectors.

Instead this module tokenizes the content stream with byte offsets and
splices only the target ``Do`` operator:

* delete    -> the ``/Name Do`` bytes are removed;
* transform -> ``/Name Do`` becomes ``q <delta> cm /Name Do Q`` where
               delta = CTM_new x CTM_old^-1, so nothing else in the stream
               (shared ``cm``/``q`` blocks, text, vectors) is touched;
* replace   -> ``q <frame> cm [clip] <fit> cm /NewName Do Q`` with a fresh
               image XObject added to the container's resources.

Page content is rewritten into a NEW stream object (the original streams
may be shared with other pages). Images inside Form XObjects are edited in
the form's stream; if the form (or any form on the path) is invoked more
than once in the document it is first cloned (``xref_copy``) and only this
invocation is re-pointed at the clone, so other uses stay untouched.

Engine coordinates: top-left origin, relative to the page's effective
(unrotated) CropBox, y down - identical to ``get_image_info()['bbox']`` and
to the app's PDFKit<->engine conversion (cropBox bounds, y flipped).
``matrix`` maps the image unit square to engine coordinates (like
``get_image_info()['transform']``); ``pdf_matrix`` is the raw PDF CTM.
"""
from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Content-stream tokenizer with byte offsets
# ---------------------------------------------------------------------------

_WS = frozenset(b" \t\r\n\f\x00")
_DELIMS = frozenset(b"()<>[]{}/%")
_NUM_RE = re.compile(rb"^[+-]?(?:\d+\.?\d*|\.\d+)$")
_REF_RE = re.compile(rb"/([^\s/<>\[\]()%{}]+)\s*(\d+)\s+(\d+)\s+R")
_FALLBACK_TAIL = re.compile(rb"^[\x09\x0a\x0c\x0d\x20-\x7e]*$")


class Op:
    __slots__ = ("name", "operands", "start", "end")

    def __init__(self, name: bytes, operands: list, start: int, end: int):
        self.name = name
        self.operands = operands
        self.start = start  # offset of the first operand (or the operator)
        self.end = end      # offset just past the operator


def _next_token(data: bytes, i: int) -> tuple[str, Any, int, int] | None:
    """Return (kind, value, start, end) of the next token at/after i."""
    n = len(data)
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
            continue
        if c == 0x25:  # % comment
            while i < n and data[i] not in (10, 13):
                i += 1
            continue
        break
    if i >= n:
        return None
    start = i
    c = data[i]
    if c == 0x28:  # ( literal string, balanced parens, backslash escapes
        depth = 1
        i += 1
        while i < n and depth:
            ch = data[i]
            if ch == 0x5C:
                i += 2
                continue
            if ch == 0x28:
                depth += 1
            elif ch == 0x29:
                depth -= 1
            i += 1
        return "str", None, start, min(i, n)
    if c == 0x3C:  # < hex string or << dict
        if i + 1 < n and data[i + 1] == 0x3C:
            return "dict_open", None, start, i + 2
        j = data.find(b">", i + 1)
        return "hex", None, start, (n if j < 0 else j + 1)
    if c == 0x3E:
        if i + 1 < n and data[i + 1] == 0x3E:
            return "dict_close", None, start, i + 2
        return "junk", None, start, i + 1
    if c == 0x5B:
        return "arr_open", None, start, i + 1
    if c == 0x5D:
        return "arr_close", None, start, i + 1
    if c in (0x7B, 0x7D, 0x29):
        return "junk", None, start, i + 1
    if c == 0x2F:  # /Name
        j = i + 1
        while j < n and data[j] not in _WS and data[j] not in _DELIMS:
            j += 1
        return "name", data[i + 1:j], start, j
    j = i
    while j < n and data[j] not in _WS and data[j] not in _DELIMS:
        j += 1
    word = data[i:j]
    if _NUM_RE.match(word):
        return "num", float(word), start, j
    if word in (b"true", b"false", b"null"):
        return "kw", word, start, j
    return "op", word, start, j


def _skip_inline_image(data: bytes, i: int) -> int:
    """i is just past ``BI``; return the offset just past the matching ``EI``."""
    n = len(data)
    length = None
    last_name = None
    while True:
        tok = _next_token(data, i)
        if tok is None:
            return n
        kind, value, _start, end = tok
        i = end
        if kind == "op" and value == b"ID":
            break
        if kind == "name":
            last_name = value
        elif kind == "num" and last_name in (b"L", b"Length"):
            length = int(value)
            last_name = None
        else:
            last_name = None
    data_start = i + 1  # exactly one whitespace byte follows ID
    if length is not None:
        j = data_start + length
        while j < n and data[j] in _WS:
            j += 1
        if data[j:j + 2] == b"EI":
            return j + 2
    j = data_start
    while True:
        k = data.find(b"EI", j)
        if k < 0:
            return n
        before_ok = k > 0 and data[k - 1] in _WS
        after = data[k + 2:k + 3]
        after_ok = not after or after[0] in _WS or after[0] in _DELIMS
        if before_ok and after_ok and _FALLBACK_TAIL.match(data[k + 2:k + 40]):
            return k + 2
        j = k + 2


def iter_ops(data: bytes) -> Iterator[Op]:
    """Yield the operators of a content stream with their byte ranges."""
    i = 0
    operands: list = []
    start: int | None = None
    depth = 0
    while True:
        tok = _next_token(data, i)
        if tok is None:
            return
        kind, value, tstart, tend = tok
        i = tend
        if start is None:
            start = tstart
        if kind in ("arr_open", "dict_open"):
            depth += 1
            continue
        if kind in ("arr_close", "dict_close"):
            depth = max(0, depth - 1)
            if depth == 0:
                operands.append(("complex", None))
            continue
        if depth:
            continue
        if kind == "op":
            if value == b"BI":
                i = _skip_inline_image(data, i)
                yield Op(b"BI", [], start, i)
            else:
                yield Op(value, operands, start, i)
            operands = []
            start = None
        else:
            operands.append((kind, value))


# ---------------------------------------------------------------------------
# PDF object helpers
# ---------------------------------------------------------------------------


def _ref(text: str) -> int:
    return int(text.split()[0])


def _norm_name(raw: bytes) -> bytes:
    """Decode #xx escapes so content-stream names match dictionary keys."""
    if b"#" not in raw:
        return raw
    return re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)


def _numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"[+-]?(?:\d+\.?\d*|\.\d+)", text)]


def _inherited(doc: Any, xref: int, key: str) -> tuple[str, str]:
    seen = set()
    while xref and xref not in seen:
        seen.add(xref)
        t, v = doc.xref_get_key(xref, key)
        if t != "null":
            return t, v
        pt, pv = doc.xref_get_key(xref, "Parent")
        if pt != "xref":
            break
        xref = _ref(pv)
    return "null", "null"


def _resources_string(doc: Any, holder: int, inherit: bool) -> tuple[str, str]:
    if inherit:
        return _inherited(doc, holder, "Resources")
    return doc.xref_get_key(holder, "Resources")


def _xobject_names_from_resources(doc: Any, res: tuple[str, str]) -> dict[bytes, int]:
    t, v = res
    if t == "xref":
        rx = _ref(v)
        xt, xv = doc.xref_get_key(rx, "XObject")
    elif t == "dict":
        # Re-read through the dict text: find the XObject entry.
        m = re.search(r"/XObject\s*(\d+\s+\d+\s+R|<<)", v)
        if not m:
            return {}
        if m.group(1).endswith("R"):
            xt, xv = "xref", m.group(1)
        else:
            body = v[m.start(1):]
            depth = 0
            end = len(body)
            k = 0
            while k < len(body) - 1:
                if body[k:k + 2] == "<<":
                    depth += 1
                    k += 2
                    continue
                if body[k:k + 2] == ">>":
                    depth -= 1
                    k += 2
                    if depth == 0:
                        end = k
                        break
                    continue
                k += 1
            xt, xv = "dict", body[:end]
    else:
        return {}
    if xt == "xref":
        xv = doc.xref_object(_ref(xv), compressed=True)
    elif xt != "dict":
        return {}
    names: dict[bytes, int] = {}
    for m in _REF_RE.finditer(xv.encode("latin-1", "replace")):
        names[_norm_name(m.group(1))] = int(m.group(2))
    return names


def _effective_cropbox(doc: Any, page_xref: int) -> tuple[float, float, float, float]:
    mt, mv = _inherited(doc, page_xref, "MediaBox")
    media = _numbers(mv) if mt == "array" else [0, 0, 612, 792]
    ct, cv = _inherited(doc, page_xref, "CropBox")
    crop = _numbers(cv) if ct == "array" else media
    if len(media) != 4:
        media = [0, 0, 612, 792]
    if len(crop) != 4:
        crop = media
    mx0, my0, mx1, my1 = min(media[0], media[2]), min(media[1], media[3]), max(media[0], media[2]), max(media[1], media[3])
    cx0, cy0, cx1, cy1 = min(crop[0], crop[2]), min(crop[1], crop[3]), max(crop[0], crop[2]), max(crop[1], crop[3])
    x0, y0, x1, y1 = max(mx0, cx0), max(my0, cy0), min(mx1, cx1), min(my1, cy1)
    if x1 <= x0 or y1 <= y0:
        x0, y0, x1, y1 = mx0, my0, mx1, my1
    return x0, y0, x1, y1


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _mat_str(m: Any) -> str:
    return " ".join(_fmt(x) for x in (m.a, m.b, m.c, m.d, m.e, m.f))


def _det(m: Any) -> float:
    return m.a * m.d - m.b * m.c


# ---------------------------------------------------------------------------
# Placement scanning
# ---------------------------------------------------------------------------


# Plain classes rather than dataclasses: the extension loader executes this
# module without registering it in sys.modules, which @dataclass requires.
class Invocation:
    __slots__ = ("container", "is_page", "start", "end", "name", "target")

    def __init__(self, container: int, is_page: bool, start: int, end: int, name: bytes, target: int):
        self.container = container  # xref of the page or form whose stream holds the Do
        self.is_page = is_page
        self.start = start          # byte range of "/Name Do" in the container data
        self.end = end
        self.name = name            # raw name token (escapes preserved)
        self.target = target        # invoked XObject xref


class Placement:
    __slots__ = ("page", "xref", "name", "ctm", "path", "index", "engine")

    def __init__(self, page: int, xref: int, name: bytes, ctm: Any, path: list):
        self.page = page            # 0-based
        self.xref = xref
        self.name = name
        self.ctm = ctm              # PDF-space CTM at the Do (fitz.Matrix)
        self.path = path            # list[Invocation], outermost first
        self.index = 0
        self.engine: Any = None     # unit square -> engine coords

    @property
    def nested(self) -> bool:
        return len(self.path) > 1


class Scan:
    def __init__(self, doc: Any):
        self.doc = doc
        self.pages: dict[int, list[Placement]] = {}
        self.form_invocations: Counter = Counter()
        self.image_occurrences: Counter = Counter()
        self.image_pages: dict[int, set] = {}
        self.inline_images: Counter = Counter()
        self._subtype: dict[int, str] = {}
        self._complete = False

    def subtype(self, xref: int) -> str:
        if xref not in self._subtype:
            t, v = self.doc.xref_get_key(xref, "Subtype")
            self._subtype[xref] = v.lstrip("/") if t == "name" else ""
        return self._subtype[xref]

    def page_data(self, page: Any) -> bytes:
        return b"\n".join(self.doc.xref_stream(x) or b"" for x in page.get_contents())

    def page_transform(self, pno: int) -> Any:
        import fitz

        page = self.doc[pno]
        x0, _y0, _x1, y1 = _effective_cropbox(self.doc, page.xref)
        return fitz.Matrix(1, 0, 0, -1, -x0, y1)

    def scan_page(self, pno: int) -> list[Placement]:
        import fitz

        if pno in self.pages:
            return self.pages[pno]
        doc = self.doc
        page = doc[pno]
        out: list[Placement] = []
        data = self.page_data(page)
        # Cheap pre-filter: no "Do"/"BI" bytes means no XObject or inline
        # image on this page, so skip tokenizing (text-heavy documents).
        if b"Do" in data or b"BI" in data:
            names = _xobject_names_from_resources(doc, _resources_string(doc, page.xref, inherit=True))
            self._walk(pno, page.xref, True, data, names, names, fitz.Matrix(1, 0, 0, 1, 0, 0), [], out, set(), 0)
        tm = self.page_transform(pno)
        counts: Counter = Counter()
        for p in out:
            p.index = counts[p.xref]
            counts[p.xref] += 1
            p.engine = p.ctm * tm
        self.pages[pno] = out
        return out

    def scan_all(self) -> None:
        if self._complete:
            return
        self.form_invocations.clear()
        self.image_occurrences.clear()
        self.image_pages.clear()
        self.inline_images.clear()
        self.pages.clear()
        for pno in range(self.doc.page_count):
            for p in self.scan_page(pno):
                self.image_occurrences[p.xref] += 1
                self.image_pages.setdefault(p.xref, set()).add(pno + 1)
        self._complete = True

    def _walk(self, pno, container, is_page, data, names, page_names, ctm0, path, out, chain, depth):
        import fitz

        ctm = fitz.Matrix(ctm0)
        stack: list = []
        for op in iter_ops(data):
            name = op.name
            if name == b"q":
                stack.append(fitz.Matrix(ctm))
            elif name == b"Q":
                if stack:
                    ctm = stack.pop()
            elif name == b"cm":
                nums = [v for k, v in op.operands if k == "num"]
                if len(nums) == 6 and len(op.operands) == 6:
                    ctm = fitz.Matrix(*nums) * ctm
            elif name == b"BI":
                self.inline_images[pno] += 1
            elif name == b"Do":
                if not op.operands or op.operands[-1][0] != "name":
                    continue
                raw = op.operands[-1][1]
                target = names.get(_norm_name(raw))
                if not target:
                    continue
                inv = Invocation(container, is_page, op.start, op.end, raw, target)
                kind = self.subtype(target)
                if kind == "Image":
                    out.append(Placement(pno, target, raw, fitz.Matrix(ctm), path + [inv]))
                elif kind == "Form":
                    self.form_invocations[target] += 1
                    if target in chain or depth >= 12:
                        continue
                    mt, mv = self.doc.xref_get_key(target, "Matrix")
                    nums = _numbers(mv) if mt == "array" else []
                    fm = fitz.Matrix(*nums) if len(nums) == 6 else fitz.Matrix(1, 0, 0, 1, 0, 0)
                    res = _resources_string(self.doc, target, inherit=False)
                    fnames = _xobject_names_from_resources(self.doc, res) if res[0] != "null" else page_names
                    fdata = self.doc.xref_stream(target) or b""
                    self._walk(pno, target, False, fdata, fnames, page_names, fm * ctm,
                               path + [inv], out, chain | {target}, depth + 1)


# ---------------------------------------------------------------------------
# Image metadata
# ---------------------------------------------------------------------------

_EXT_BY_FILTER = {
    "DCTDecode": "jpg", "DCT": "jpg", "JPXDecode": "jp2", "JBIG2Decode": "jb2",
    "CCITTFaxDecode": "fax", "CCF": "fax",
}


def _image_meta(doc: Any, xref: int) -> dict:
    def key(k: str) -> tuple[str, str]:
        return doc.xref_get_key(xref, k)

    def as_int(k: str) -> int:
        t, v = key(k)
        try:
            return int(float(v)) if t in ("int", "float") else 0
        except ValueError:
            return 0

    ft, fv = key("Filter")
    filters = re.findall(r"/(\w+)", fv) if ft in ("name", "array") else []
    last = filters[-1] if filters else ""
    ext = _EXT_BY_FILTER.get(last, "png")
    ct, cv = key("ColorSpace")
    cs_text = cv
    if ct == "xref":
        cs_text = doc.xref_object(_ref(cv), compressed=True)
    family = (re.findall(r"/(\w+)", cs_text) or [""])[0] if ct != "null" else ""
    components = {"DeviceGray": 1, "CalGray": 1, "DeviceRGB": 3, "CalRGB": 3, "Lab": 3, "DeviceCMYK": 4}.get(family, 0)
    if family == "ICCBased":
        m = re.search(r"(\d+)\s+\d+\s+R", cs_text)
        if m:
            nt, nv = doc.xref_get_key(int(m.group(1)), "N")
            components = int(nv) if nt == "int" else 0
    if family == "Indexed":
        components = 1
    is_mask = key("ImageMask")[1] == "true"
    pretty = {
        "DeviceGray": "Gray", "CalGray": "Gray", "DeviceRGB": "RGB", "CalRGB": "RGB",
        "DeviceCMYK": "CMYK", "Lab": "Lab", "Indexed": "Indexed", "Separation": "Separation",
        "DeviceN": "DeviceN", "Pattern": "Pattern",
    }.get(family, "")
    if family == "ICCBased":
        pretty = {1: "Gray", 3: "RGB", 4: "CMYK"}.get(components, "ICC")
    if is_mask:
        pretty = "Stencil mask"
    st, sv = key("SMask")
    smask = _ref(sv) if st == "xref" else 0
    has_mask = smask > 0 or key("Mask")[0] != "null"
    try:
        size = len(doc.xref_stream_raw(xref) or b"")
    except Exception:
        size = 0
    return {
        "width": as_int("Width"),
        "height": as_int("Height"),
        "bpc": as_int("BitsPerComponent") or (1 if is_mask else 0),
        "colorspace": pretty or family or "Unknown",
        "components": components,
        "filter": last,
        "ext": ext,
        "bytes": size,
        "has_mask": has_mask,
        "smask": smask,
    }


def _payload(scan: Scan, p: Placement, meta_cache: dict) -> dict:
    import fitz

    if p.xref not in meta_cache:
        meta_cache[p.xref] = _image_meta(scan.doc, p.xref)
    meta = meta_cache[p.xref]
    e = p.engine
    bbox = fitz.Rect(0, 0, 1, 1).transform(e)
    disp_w, disp_h = math.hypot(e.a, e.b), math.hypot(e.c, e.d)
    rotation = math.degrees(math.atan2(e.b, e.a))
    # Reported like MuPDF's get_image_info()['transform']: image top-left
    # pixel corner at unit (0,0), y down.
    m = fitz.Matrix(1, 0, 0, -1, 0, 1) * e
    return {
        "page": p.page + 1,
        "xref": p.xref,
        "name": p.name.decode("latin-1"),
        "placement_index": p.index,
        "bbox": [round(v, 3) for v in (bbox.x0, bbox.y0, bbox.x1, bbox.y1)],
        "matrix": [round(v, 5) + 0.0 for v in (m.a, m.b, m.c, m.d, m.e, m.f)],
        "pdf_matrix": [round(v, 5) for v in (p.ctm.a, p.ctm.b, p.ctm.c, p.ctm.d, p.ctm.e, p.ctm.f)],
        "rotation": round(rotation, 3) + 0.0,
        "flipped": _det(e) > 0,
        "display_width": round(disp_w, 3),
        "display_height": round(disp_h, 3),
        "dpi_x": round(meta["width"] * 72 / disp_w, 1) if disp_w > 0 else 0,
        "dpi_y": round(meta["height"] * 72 / disp_h, 1) if disp_h > 0 else 0,
        "occurrences": scan.image_occurrences.get(p.xref, 0),
        "pages_used": sorted(scan.image_pages.get(p.xref, set())),
        "nested": p.nested,
        **{k: v for k, v in meta.items() if k != "smask"},
    }


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def _xobject_slot(doc: Any, container: int, is_page: bool, fallback_res: tuple[str, str]) -> tuple[int, str]:
    """Return (xref, key-prefix) where ``xref_set_key(xref, prefix + Name, ...)``
    adds an entry to the container's XObject resource dict."""
    t, v = doc.xref_get_key(container, "Resources")
    if t == "null":
        # Inherited resources (page tree, or a legacy form using the page's):
        # give the container its own copy first so nothing else changes.
        if fallback_res[0] == "null":
            doc.xref_set_key(container, "Resources", "<<>>")
        else:
            doc.xref_set_key(container, "Resources", fallback_res[1])
        t, v = doc.xref_get_key(container, "Resources")
    if t == "xref":
        holder, prefix = _ref(v), ""
    else:
        holder, prefix = container, "Resources/"
    xt, xv = doc.xref_get_key(holder, prefix + "XObject")
    if xt == "xref":
        return _ref(xv), ""
    if xt == "null":
        doc.xref_set_key(holder, prefix + "XObject", "<<>>")
    return holder, prefix + "XObject/"


def _unique_name(existing: set, stem: str) -> str:
    k = 1
    while f"{stem}{k}".encode() in existing:
        k += 1
    return f"{stem}{k}"


class Editor:
    """Applies one splice per container and writes the streams back."""

    def __init__(self, scan: Scan, placement: Placement):
        self.scan = scan
        self.doc = scan.doc
        self.p = placement
        self.page = self.doc[placement.page]
        self.page_res = _resources_string(self.doc, self.page.xref, inherit=True)
        self.cloned: list[int] = []

    def _container_data(self, inv: Invocation) -> bytes:
        if inv.is_page:
            return self.scan.page_data(self.page)
        return self.doc.xref_stream(inv.container) or b""

    def _write(self, inv_container: int, is_page: bool, data: bytes) -> None:
        doc = self.doc
        if is_page:
            nx = doc.get_new_xref()
            doc.update_object(nx, "<<>>")
            doc.update_stream(nx, data)
            doc.xref_set_key(self.page.xref, "Contents", f"{nx} 0 R")
        else:
            doc.update_stream(inv_container, data)

    def _names(self, container: int, is_page: bool) -> set:
        if is_page:
            return set(_xobject_names_from_resources(self.doc, self.page_res))
        res = _resources_string(self.doc, container, inherit=False)
        if res[0] == "null":
            return set(_xobject_names_from_resources(self.doc, self.page_res))
        return set(_xobject_names_from_resources(self.doc, res))

    def add_xobject(self, container: int, is_page: bool, target_xref: int, stem: str) -> str:
        name = _unique_name(self._names(container, is_page), stem)
        holder, prefix = _xobject_slot(self.doc, container, is_page, self.page_res)
        self.doc.xref_set_key(holder, prefix + name, f"{target_xref} 0 R")
        if is_page:
            self.page_res = _resources_string(self.doc, self.page.xref, inherit=True)
        return name

    def apply(self, replacement_for) -> None:
        """replacement_for(container, is_page) -> bytes replacing the target Do."""
        doc = self.doc
        path = self.p.path
        # Containers along the path, outermost first. A container's data is
        # read before any of its own edits (each container gets ONE splice).
        container, is_page = path[0].container, True
        for level, inv in enumerate(path):
            data = self._container_data(Invocation(container, is_page, 0, 0, b"", 0))
            if level == len(path) - 1:
                new = replacement_for(container, is_page)
                self._write(container, is_page, data[:inv.start] + new + data[inv.end:])
                return
            child = inv.target
            if self.scan.form_invocations.get(child, 0) > 1:
                clone = doc.get_new_xref()
                doc.update_object(clone, "<<>>")  # xref_copy needs a dict target
                doc.xref_copy(child, clone)
                self.cloned.append(clone)
                cname = self.add_xobject(container, is_page, clone, "PSFm")
                self._write(container, is_page, data[:inv.start] + f"/{cname} Do".encode() + data[inv.end:])
                child = clone
            container, is_page = child, False


def _engine_rect(text: str) -> Any:
    import fitz

    vals = _numbers(text or "")
    if len(vals) != 4:
        raise ValueError("--rect must be x0,y0,x1,y1")
    r = fitz.Rect(min(vals[0], vals[2]), min(vals[1], vals[3]), max(vals[0], vals[2]), max(vals[1], vals[3]))
    if r.width < 0.5 or r.height < 0.5:
        raise ValueError("--rect is too small")
    return r


def _rot_about(cx: float, cy: float, deg: float) -> Any:
    import fitz

    return fitz.Matrix(1, 0, 0, 1, -cx, -cy) * fitz.Matrix(deg) * fitz.Matrix(1, 0, 0, 1, cx, cy)


def _bbox_map(src: Any, dst: Any) -> Any:
    import fitz

    if src.width <= 1e-9 or src.height <= 1e-9:
        raise ValueError("the image placement has no area")
    sx, sy = dst.width / src.width, dst.height / src.height
    return fitz.Matrix(1, 0, 0, 1, -src.x0, -src.y0) * fitz.Matrix(sx, 0, 0, sy, 0, 0) * fitz.Matrix(1, 0, 0, 1, dst.x0, dst.y0)


def _visual_frame(e: Any) -> Any:
    """Upright frame (unit square -> engine) matching how the placement looks.

    Quarter-turns and flips of the old CTM are treated as "how the old image
    data was stored", so a replacement is drawn upright in the same frame; a
    residual tilt within (-45, 45] degrees is kept.
    """
    import fitz

    phi = math.degrees(math.atan2(e.b, e.a))
    k = round(phi / 90.0)
    theta = phi - 90.0 * k
    w, h = math.hypot(e.a, e.b), math.hypot(e.c, e.d)
    if k % 2:
        w, h = h, w
    c = fitz.Point(0.5, 0.5) * e
    upright = fitz.Matrix(w, 0, 0, -h, c.x - w / 2, c.y + h / 2)
    return upright * _rot_about(c.x, c.y, theta), w, h


def _find_placement(scan: Scan, pno: int, xref: int, index: int | None) -> Placement:
    matches = [p for p in scan.scan_page(pno) if p.xref == xref]
    if not matches:
        raise ValueError(f"image xref {xref} is not drawn on page {pno + 1}")
    if index is None:
        if len(matches) > 1:
            raise ValueError(f"image xref {xref} is drawn {len(matches)} times on page {pno + 1}; pass --placement-index")
        return matches[0]
    if index < 0 or index >= len(matches):
        raise ValueError(f"--placement-index must be between 0 and {len(matches) - 1}")
    return matches[index]


def _new_image_xref(doc: Any, image_path: str) -> int:
    """Embed an image file as a new XObject (PNG alpha -> SMask, JPEG kept)."""
    tmp = doc.new_page()
    try:
        xref = tmp.insert_image(tmp.rect, filename=image_path, keep_proportion=False)
    finally:
        doc.delete_page(tmp.number)
    return xref


# ---------------------------------------------------------------------------
# Pixel access (extract / thumbnails)
# ---------------------------------------------------------------------------


def _image_pixmap(doc: Any, xref: int) -> Any:
    import fitz

    pix = fitz.Pixmap(doc, xref)
    if pix.colorspace is None:
        # Stencil mask: alpha-only samples; show painted area as black.
        gray = fitz.Pixmap(fitz.csGRAY, (0, 0, pix.width, pix.height), True)
        gray.clear_with(0)
        gray.set_alpha(pix.samples)
        return gray
    if pix.colorspace.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix)
    st, sv = doc.xref_get_key(xref, "SMask")
    if st == "xref":
        try:
            mask = fitz.Pixmap(doc, _ref(sv))
            if mask.colorspace is not None and mask.colorspace.n != 1:
                mask = fitz.Pixmap(fitz.csGRAY, mask)
            if mask.alpha:
                mask = fitz.Pixmap(mask, 0)
            if (mask.width, mask.height) == (pix.width, pix.height):
                base = fitz.Pixmap(pix, 0) if pix.alpha else pix
                pix = fitz.Pixmap(base, mask)
        except Exception:
            pass
    return pix


def _pil_image(pix: Any) -> Any:
    from PIL import Image

    mode = {(1, False): "L", (1, True): "LA", (3, False): "RGB", (3, True): "RGBA"}[(pix.colorspace.n, bool(pix.alpha))]
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _thumbnail(scan: Scan, p: Placement, out_path: Path, max_side: int) -> tuple[int, int]:
    """Render the image as it appears on the page (orientation applied)."""
    import fitz
    from PIL import Image

    img = _pil_image(_image_pixmap(scan.doc, p.xref))
    if img.mode in ("L", "LA"):
        img = img.convert("RGBA" if img.mode == "LA" else "RGB")
    pre = max(max_side * 2, 64)
    if max(img.size) > pre:
        img.thumbnail((pre, pre))
    w, h = img.size
    frame, fw, fh = _visual_frame(p.engine)
    scale = max_side / max(fw, fh, 1e-6)
    ow, oh = max(1, round(fw * scale)), max(1, round(fh * scale))
    # output px -> frame unit -> image unit -> image px
    out2frame = fitz.Matrix(1 / ow, 0, 0, -1 / oh, 0, 1)
    frame2img = ~(p.engine * ~frame)
    img2px = fitz.Matrix(w, 0, 0, -h, 0, h)
    t = out2frame * frame2img * img2px
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    thumb = img.transform((ow, oh), Image.Transform.AFFINE, (t.a, t.c, t.e, t.b, t.d, t.f),
                          resample=Image.Resampling.BILINEAR)
    thumb.save(out_path, "PNG")
    return ow, oh


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def register(subparsers: Any, api: Any) -> None:
    def open_doc(path: str) -> Any:
        import fitz

        return fitz.open(path)

    def check_page(doc: Any, page: int) -> int:
        if page < 1 or page > doc.page_count:
            raise ValueError(f"page must be between 1 and {doc.page_count}")
        return page - 1

    def save(doc: Any, output: str) -> None:
        out = Path(output)
        api.ensure_parent(out)
        doc.save(str(out), garbage=1, deflate=True)

    # -- list-images -------------------------------------------------------
    def cmd_list(args: argparse.Namespace) -> int:
        with open_doc(args.input) as doc:
            scan = Scan(doc)
            scan.scan_all()
            if args.page is not None:
                pages = [check_page(doc, args.page)]
            else:
                pages = list(range(doc.page_count))
            meta: dict = {}
            images = [_payload(scan, p, meta) for pno in pages for p in scan.pages[pno]]
            inline = sum(scan.inline_images.get(pno, 0) for pno in pages)
            return api.respond(True, images=images, count=len(images), inline_images=inline,
                               page_count=doc.page_count)

    # -- image-at ----------------------------------------------------------
    def cmd_at(args: argparse.Namespace) -> int:
        import fitz

        with open_doc(args.input) as doc:
            pno = check_page(doc, args.page)
            scan = Scan(doc)
            scan.scan_all()
            pt = fitz.Point(args.x, args.y)
            tol = max(0.0, args.tolerance)
            for p in reversed(scan.pages[pno]):  # topmost = painted last
                if abs(_det(p.engine)) < 1e-12:
                    continue
                u = pt * ~p.engine
                e = p.engine
                tu = tol / max(math.hypot(e.a, e.b), 1e-9)
                tv = tol / max(math.hypot(e.c, e.d), 1e-9)
                if -tu <= u.x <= 1 + tu and -tv <= u.y <= 1 + tv:
                    return api.respond(True, found=True, image=_payload(scan, p, {}))
            return api.respond(True, found=False)

    # -- transform-image ---------------------------------------------------
    def cmd_transform(args: argparse.Namespace) -> int:
        import fitz

        rect = _engine_rect(args.rect)
        with open_doc(args.input) as doc:
            pno = check_page(doc, args.page)
            scan = Scan(doc)
            scan.scan_all()
            p = _find_placement(scan, pno, args.xref, args.placement_index)
            e_old = p.engine
            if abs(_det(e_old)) < 1e-12:
                return api.respond(False, message="the image placement is degenerate (zero area)")
            bbox = fitz.Rect(0, 0, 1, 1).transform(e_old)
            e_new = e_old * _bbox_map(bbox, rect)
            if args.rotate:
                c = (rect.tl + rect.br) / 2
                e_new = e_new * _rot_about(c.x, c.y, args.rotate)
            delta = e_new * ~e_old  # == CTM_new x CTM_old^-1 (page transform cancels)
            editor = Editor(scan, p)
            editor.apply(lambda *_: f"\nq {_mat_str(delta)} cm /{p.name.decode('latin-1')} Do Q\n".encode("latin-1"))
            save(doc, args.output)
            new_bbox = fitz.Rect(0, 0, 1, 1).transform(e_new)
            return api.respond(True, output=args.output, page=args.page, xref=p.xref,
                               placement_index=p.index, nested=p.nested, cloned_forms=editor.cloned,
                               bbox=[round(v, 3) for v in new_bbox])

    # -- replace-image -----------------------------------------------------
    def cmd_replace(args: argparse.Namespace) -> int:
        import fitz

        image_path = Path(args.image)
        if not image_path.is_file():
            return api.respond(False, message=f"image not found: {image_path}")
        with open_doc(args.input) as doc:
            pno = check_page(doc, args.page)
            scan = Scan(doc)
            scan.scan_all()
            p = _find_placement(scan, pno, args.xref, args.placement_index)
            e_old = p.engine
            if abs(_det(e_old)) < 1e-12:
                return api.respond(False, message="the image placement is degenerate (zero area)")
            try:
                new_xref = _new_image_xref(doc, str(image_path))
            except Exception as exc:
                return api.respond(False, message=f"could not read image: {exc}")
            iw = int(float(doc.xref_get_key(new_xref, "Width")[1]))
            ih = int(float(doc.xref_get_key(new_xref, "Height")[1]))
            frame, fw, fh = _visual_frame(e_old)
            r_img, r_box = iw / max(ih, 1), fw / max(fh, 1e-9)
            sx = sy = 1.0
            if args.fit == "fit":
                if r_img > r_box:
                    sy = r_box / r_img
                else:
                    sx = r_img / r_box
            elif args.fit == "fill":
                if r_img > r_box:
                    sx = r_img / r_box
                else:
                    sy = r_box / r_img
            inner = fitz.Matrix(sx, 0, 0, sy, (1 - sx) / 2, (1 - sy) / 2)
            to_frame = frame * ~e_old
            clip = "0 0 1 1 re W n " if args.fit == "fill" else ""
            editor = Editor(scan, p)

            def build(container: int, is_page: bool) -> bytes:
                name = editor.add_xobject(container, is_page, new_xref, "PSImg")
                return (f"\nq {_mat_str(to_frame)} cm {clip}{_mat_str(inner)} cm /{name} Do Q\n").encode("latin-1")

            editor.apply(build)
            save(doc, args.output)
            e_new = inner * frame
            new_bbox = fitz.Rect(0, 0, 1, 1).transform(e_new)
            if args.fit == "fill":
                new_bbox = fitz.Rect(0, 0, 1, 1).transform(frame)
            return api.respond(True, output=args.output, page=args.page, old_xref=p.xref, xref=new_xref,
                               placement_index=p.index, nested=p.nested, cloned_forms=editor.cloned,
                               width=iw, height=ih, fit=args.fit,
                               bbox=[round(v, 3) for v in new_bbox])

    # -- delete-image ------------------------------------------------------
    def cmd_delete(args: argparse.Namespace) -> int:
        with open_doc(args.input) as doc:
            pno = check_page(doc, args.page)
            scan = Scan(doc)
            scan.scan_all()
            p = _find_placement(scan, pno, args.xref, args.placement_index)
            editor = Editor(scan, p)
            editor.apply(lambda *_: b"\n")
            save(doc, args.output)
            remaining = scan.image_occurrences.get(p.xref, 1) - 1
            return api.respond(True, output=args.output, page=args.page, xref=p.xref,
                               placement_index=p.index, nested=p.nested, cloned_forms=editor.cloned,
                               remaining_occurrences=remaining)

    # -- extract-image -----------------------------------------------------
    def cmd_extract(args: argparse.Namespace) -> int:
        with open_doc(args.input) as doc:
            if args.xref <= 0 or args.xref >= doc.xref_length() or not doc.xref_is_image(args.xref):
                return api.respond(False, message=f"xref {args.xref} is not an image")
            meta = _image_meta(doc, args.xref)
            out = Path(args.output)
            api.ensure_parent(out)
            raw_ok = (
                args.format == "auto"
                and meta["filter"] in ("DCTDecode", "DCT", "JPXDecode")
                and meta["components"] in (1, 3)
                and not meta["has_mask"]
                and doc.xref_get_key(args.xref, "Decode")[0] == "null"
            )
            if raw_ok:
                fmt = "jp2" if meta["filter"] == "JPXDecode" else "jpg"
                if out.suffix.lower() not in ({".jpg", ".jpeg"} if fmt == "jpg" else {".jp2", ".jpx"}):
                    out = out.with_suffix("." + fmt)
                info = doc.extract_image(args.xref)
                out.write_bytes(info["image"])
            else:
                fmt = "png"
                if out.suffix.lower() != ".png":
                    out = out.with_suffix(".png")
                _image_pixmap(doc, args.xref).save(str(out))
            return api.respond(True, path=str(out), format=fmt, width=meta["width"], height=meta["height"],
                               colorspace=meta["colorspace"], bytes=out.stat().st_size)

    # -- image-thumbnails --------------------------------------------------
    def cmd_thumbs(args: argparse.Namespace) -> int:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        max_side = max(16, min(int(args.max), 2048))
        with open_doc(args.input) as doc:
            scan = Scan(doc)
            scan.scan_all()
            pnos = list(range(doc.page_count)) if args.page in (None, 0) else [check_page(doc, args.page)]
            meta: dict = {}
            items = []
            for pno in pnos:
                for p in scan.pages[pno]:
                    entry = _payload(scan, p, meta)
                    path = out_dir / f"p{pno + 1}-x{p.xref}-i{p.index}.png"
                    try:
                        tw, th = _thumbnail(scan, p, path, max_side)
                        entry.update(thumbnail=str(path), thumb_width=tw, thumb_height=th)
                    except Exception as exc:  # unreadable/exotic image data
                        entry.update(thumbnail=None, thumbnail_error=str(exc))
                    items.append(entry)
            return api.respond(True, images=items, count=len(items), output_dir=str(out_dir))

    def add_target(sp: Any) -> None:
        sp.add_argument("--input", required=True)
        sp.add_argument("--output", required=True)
        sp.add_argument("--page", type=int, required=True)
        sp.add_argument("--xref", type=int, required=True)
        sp.add_argument("--placement-index", type=int, default=None)

    sp = subparsers.add_parser("list-images")
    sp.add_argument("--input", required=True)
    sp.add_argument("--page", type=int, default=None)
    sp.set_defaults(func=cmd_list)

    sp = subparsers.add_parser("image-at")
    sp.add_argument("--input", required=True)
    sp.add_argument("--page", type=int, required=True)
    sp.add_argument("--x", type=float, required=True)
    sp.add_argument("--y", type=float, required=True)
    sp.add_argument("--tolerance", type=float, default=0.0)
    sp.set_defaults(func=cmd_at)

    sp = subparsers.add_parser("transform-image")
    add_target(sp)
    sp.add_argument("--rect", required=True)
    sp.add_argument("--rotate", type=float, default=0.0)
    sp.set_defaults(func=cmd_transform)

    sp = subparsers.add_parser("replace-image")
    add_target(sp)
    sp.add_argument("--image", required=True)
    sp.add_argument("--fit", choices=["fit", "fill", "stretch"], default="fit")
    sp.set_defaults(func=cmd_replace)

    sp = subparsers.add_parser("delete-image")
    add_target(sp)
    sp.set_defaults(func=cmd_delete)

    sp = subparsers.add_parser("extract-image")
    sp.add_argument("--input", required=True)
    sp.add_argument("--xref", type=int, required=True)
    sp.add_argument("--output", required=True)
    sp.add_argument("--format", choices=["auto", "png"], default="auto")
    sp.set_defaults(func=cmd_extract)

    sp = subparsers.add_parser("image-thumbnails")
    sp.add_argument("--input", required=True)
    sp.add_argument("--page", type=int, default=None)
    sp.add_argument("--output-dir", required=True)
    sp.add_argument("--max", type=int, default=256)
    sp.set_defaults(func=cmd_thumbs)
