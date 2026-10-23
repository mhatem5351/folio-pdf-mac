"""Word-like reflow for ``replace-block --reflow``.

When an edited paragraph needs more (or fewer) lines, the content that
follows it on the same page, in the same column, is pushed down (pulled up)
by the change in height, so nothing overlaps and no gap is left.

Pipeline (``replace_block_with_reflow``):

1. MEASURE: the edit is first run on a throw-away copy of the document. All
   content below the block is removed there, so the text lines the edit adds
   can be told apart from everything else (diff of get_text lines before and
   after rendering). ``delta = new_bottom - old_bottom`` (glyph-box bottoms).
2. PLAN (on the untouched page): collect every text segment, image placement
   and vector path; find the FOLLOWING content: items below the block whose
   x-range overlaps the block's column, grown transitively (an item that
   overlaps the column widens it). Skip with a reason when it is not clearly
   safe (see ``plan_reflow``).
3. SNAPSHOT: the original page is copied into a temp document and everything
   that does NOT move is stripped from the copy (text per span, images and
   paths outside the moved region).
4. LIVE: moved image placements are deleted one by one (images.py splices the
   ``Do`` operator, so a background image behind the column survives), moved
   annotations are shifted, and, inside the edit (after the block is erased,
   before the new text is drawn), the moved text spans and paths are redacted.
   Text geometry is always read with _rtl_text.glyph_flags (real glyph
   boxes): GlyphWriter's RTL lines otherwise report a single carrier point.
   Redactions that remove text drop the touched lines' /ActualText (the
   redaction guard, which also looks inside Form XObjects); line-art-only
   redactions (text=NONE) leave it alone.
5. The snapshot region is drawn back shifted by delta with
   ``show_pdf_page(clip=region)``: exact original fonts, glyphs, vectors and
   images. Its text lives in a Form XObject, which MuPDF extracts, searches
   and (importantly for a second edit) redacts like page-level text.

Heuristics (documented for the planner):
* Column: seed x-range = old block rect + render rect + the new text's extent;
  closure over items below it (x-overlap > 0.5pt).
* Footer: the topmost vertical gap >= 18pt whose lower side starts inside the
  bottom 12% of the page separates a footer (page numbers, footer rules) from
  the body. Footer items never move and the body may not grow into them.
* Body bottom: page bottom minus the page's apparent bottom margin
  (clamped to 18..72pt), and at least 6pt above a detected footer.
* Other columns: an item beside the block (same vertical band, outside the
  block's x-range) is another column. If the closure pulled in content that
  sits under such an item but not under the block (e.g. a full-width image
  below a two-column layout links both columns), reflow is skipped with
  reason "spans_columns".
* Anything else crossing the block's bottom edge inside the column (a box
  around several paragraphs, a table rule, a side image) blocks reflow
  ("straddles"), unless it is a background that contains the whole edited
  area and the moved region — then it stays put and acts as a bottom limit.
* Pure white fill-only paths (erase boxes of earlier edits) are ignored.
* Rotated pages and moved inline images are skipped ("rotated_page",
  "inline_image").
"""
from __future__ import annotations

from collections import Counter
from typing import Any

MIN_DELTA = 1.0          # |delta| below this: nothing to do
BELOW_TOLERANCE = 3.0    # tight leading: a following line's box may start this far above old_bottom
FOOTER_GAP = 18.0        # min vertical gap that separates a footer from the body
FOOTER_ZONE = 0.12       # footer must start in the bottom 12% of the page
REGION_PAD_X = 2.0
REGION_PAD_Y = 1.5


class Item:
    __slots__ = ("kind", "rect", "ref")

    def __init__(self, kind: str, rect: Any, ref: Any = None):
        self.kind = kind   # "text" | "image" | "inline" | "drawing"
        self.rect = rect
        self.ref = ref

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "rect": [round(v, 2) for v in self.rect]}


def _x_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)


def _overlaps_x(rect: Any, x0: float, x1: float, tol: float = 0.5) -> bool:
    return _x_overlap(rect.x0, rect.x1, x0, x1) > tol


def _glyph_flags(kind: str) -> int:
    """Text flags that report REAL glyph boxes. GlyphWriter (RTL edits) puts a
    line's logical text as /ActualText on one zero-width carrier glyph and
    the real glyphs in an empty /ActualText span; read with the default
    flags such a line is a single point, so its glyphs would never be
    redacted (ghost text) or kept/stripped correctly in the snapshot."""
    from ext import _rtl_text

    return _rtl_text.glyph_flags(kind)


def _is_white(color: Any) -> bool:
    return color is not None and len(color) >= 3 and all(c >= 0.995 for c in color[:3])


# ---------------------------------------------------------------------------
# Page inventory
# ---------------------------------------------------------------------------

def collect_items(helpers: Any, page: Any, bare: Any) -> list[Item]:
    """Text segments, image placements and paths of the page. Text, paths and
    inline images come from `bare` (the page without annotations); image
    placements from the live page, so they can be deleted one by one."""
    import fitz

    items: list[Item] = []
    for block in helpers.text_dict(bare).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for segment in helpers.line_span_segments(line):
                items.append(Item("text", fitz.Rect(segment["bbox"]), segment))

    images = helpers.images_module()
    scan = images.Scan(page.parent)
    for placement in scan.scan_page(page.number):
        rect = fitz.Rect(0, 0, 1, 1).transform(placement.engine)
        rect.normalize()
        items.append(Item("image", rect, (placement.xref, placement.index)))
    for info in bare.get_image_info(xrefs=True):
        if not info.get("xref"):
            items.append(Item("inline", fitz.Rect(info["bbox"])))

    for path in bare.get_drawings():
        if path.get("type") == "clip" or path.get("type") == "group":
            continue
        has_stroke = path.get("color") is not None and "s" in (path.get("type") or "")
        fill = path.get("fill")
        stroke_shows = has_stroke and not _is_white(path.get("color"))
        if not stroke_shows and (fill is None or _is_white(fill)):
            continue  # invisible, or a white erase box from an earlier edit
        width = float(path.get("width") or 0) if has_stroke else 0.0
        rect = fitz.Rect(path["rect"]) + (-width / 2, -width / 2, width / 2, width / 2)
        items.append(Item("drawing", rect))
    return items


def _footer_top(items: list[Item], page_rect: Any) -> float | None:
    """Top of the page footer, or None (see module docstring)."""
    zone_top = page_rect.y1 - FOOTER_ZONE * page_rect.height
    bands: list[list[float]] = []
    for item in sorted(items, key=lambda i: i.rect.y0):
        y0, y1 = item.rect.y0, max(item.rect.y1, item.rect.y0)
        if bands and y0 <= bands[-1][1]:
            bands[-1][1] = max(bands[-1][1], y1)
        else:
            bands.append([y0, y1])
    for upper, lower in zip(bands, bands[1:]):
        if lower[0] >= zone_top and lower[0] - upper[1] >= FOOTER_GAP:
            return lower[0]  # topmost qualifying gap
    return None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def plan_reflow(helpers: Any, page: Any, bare: Any, block_rect: Any, block_lines: list[dict[str, Any]],
                render_rect: Any, new_extent: Any, delta: float) -> dict[str, Any]:
    """Decide what moves. Returns {"ok": True, ...plan} or {"ok": False, "reason"}."""
    import fitz

    if abs(delta) < MIN_DELTA:
        return {"ok": False, "reason": "noop"}
    if page.rotation % 360:
        return {"ok": False, "reason": "rotated_page"}

    old_bottom = max((float(l["bbox"][3]) for l in block_lines), default=float(block_rect.y1))
    own_boxes = [fitz.Rect(l["bbox"]) + (-0.5, -0.5, 0.5, 0.5) for l in block_lines] or [fitz.Rect(block_rect)]

    items = []
    for item in collect_items(helpers, page, bare):
        if item.kind == "text":
            center = (item.rect.tl + item.rect.br) / 2
            if any(box.contains(center) for box in own_boxes):
                continue  # the edited block itself
        items.append(item)

    page_rect = page.rect
    # Page-size backgrounds and frames say nothing about where the footer or
    # the bottom margin is.
    sized = [i for i in items if i.kind == "text" or i.rect.height <= 0.5 * page_rect.height]
    footer_top = _footer_top(sized + [Item("text", fitz.Rect(block_rect))], page_rect)
    body = [i for i in items if footer_top is None or i.rect.y0 < footer_top - 0.01]

    body_ids = {id(i) for i in body}
    lowest = max([i.rect.y1 for i in sized if id(i) in body_ids] + [block_rect.y1])
    margin = min(72.0, max(18.0, page_rect.y1 - lowest))
    body_bottom = page_rect.y1 - margin
    if footer_top is not None:
        body_bottom = min(body_bottom, footer_top - 6.0)

    seed_x0 = min(block_rect.x0, render_rect.x0, new_extent.x0 if new_extent else block_rect.x0)
    seed_x1 = max(block_rect.x1, render_rect.x1, new_extent.x1 if new_extent else block_rect.x1)

    below, straddle, band = [], [], []
    for item in body:
        r = item.rect
        if (r.y0 + r.y1) / 2 > old_bottom and r.y0 >= old_bottom - BELOW_TOLERANCE:
            below.append(item)
        elif r.y0 < old_bottom - 2 and r.y1 > old_bottom + 2:
            straddle.append(item)
        elif r.y1 > block_rect.y0 + 1 and r.y0 < old_bottom:
            band.append(item)

    # Transitive column closure.
    x0, x1 = seed_x0, seed_x1
    moved: list[Item] = []
    pending = list(below)
    changed = True
    while changed:
        changed = False
        for item in list(pending):
            if _overlaps_x(item.rect, x0, x1):
                moved.append(item)
                pending.remove(item)
                x0, x1 = min(x0, item.rect.x0), max(x1, item.rect.x1)
                changed = True
    if not moved:
        return {"ok": False, "reason": "nothing_below"}

    region = fitz.Rect(moved[0].rect)
    for item in moved[1:]:
        region |= item.rect

    limit = body_bottom
    whole = fitz.Rect(block_rect) | region
    for item in straddle:
        if not _overlaps_x(item.rect, x0, x1):
            continue  # another column's image/rule
        if item.kind in ("drawing", "image") and (item.rect + (1, 1, -1, -1)).contains(whole):
            limit = min(limit, item.rect.y1 - 1.0)  # background box: content moves inside it
            continue
        return {"ok": False, "reason": "straddles", "blocker": item.describe()}

    for side in band:
        if _overlaps_x(side.rect, seed_x0, seed_x1) or not _overlaps_x(side.rect, x0, x1):
            continue
        for item in moved:
            if _overlaps_x(item.rect, side.rect.x0, side.rect.x1) and not _overlaps_x(item.rect, seed_x0, seed_x1):
                return {"ok": False, "reason": "spans_columns", "blocker": item.describe()}

    if any(item.kind == "inline" for item in moved):
        return {"ok": False, "reason": "inline_image"}

    # Annotations (links, highlights, notes, form fields) below the block in
    # the moving column travel with it. They never widen the region (that
    # could pull another column's content in) and never block reflow.
    annots = []
    for xref, rect in page_annotations(page):
        center_y = (rect.y0 + rect.y1) / 2
        if footer_top is not None and rect.y0 >= footer_top - 0.01:
            continue
        if center_y > old_bottom and rect.y0 >= old_bottom - BELOW_TOLERANCE and _overlaps_x(rect, x0, x1):
            annots.append((xref, rect))
    bottom = max([region.y1] + [rect.y1 for _xref, rect in annots])
    if delta > 0 and bottom + delta > limit:
        return {"ok": False, "reason": "overflow"}

    clip = region + (-REGION_PAD_X, -REGION_PAD_Y, REGION_PAD_X, REGION_PAD_Y)
    clip &= page_rect
    counts = Counter(item.kind for item in moved)
    return {
        "ok": True,
        "region": clip,
        "moved": moved,
        "counts": counts,
        "images": [item.ref for item in moved if item.kind == "image"],
        "annots": [xref for xref, _rect in annots],
    }


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------

def _span_rects(page: Any, inside: Any, want_inside: bool) -> list[Any]:
    """Redaction rects for every text span (whitespace included) whose center
    lies inside (want_inside) / outside `inside`. Shrunk so a rect never
    reaches a neighbouring glyph (redaction removes any glyph it touches)."""
    import fitz

    rects = []
    for block in page.get_text("rawdict", flags=_glyph_flags("rawdict")).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                box = fitz.Rect(span["bbox"])
                center = fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
                if inside.contains(center) != want_inside:
                    continue
                sx = min(0.75, box.width * 0.25)
                sy = min(0.75, box.height * 0.15)
                rect = fitz.Rect(box.x0 + sx, box.y0 + sy, box.x1 - sx, box.y1 - sy)
                if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                    rect = fitz.Rect(center.x - 0.1, center.y - 0.1, center.x + 0.1, center.y + 0.1)
                rects.append(rect)
                # Narrow glyphs at the span's edge fall outside the shrunk
                # rect: above all GlyphWriter's ActualText carrier (0.05 em
                # wide, at the line's start edge). Left behind it would read
                # as a stray space once its line's ActualText is dropped.
                for ch in span.get("chars", ()):
                    cb = fitz.Rect(ch["bbox"])
                    mid = (cb.x0 + cb.x1) / 2
                    if cb.width < 2.0 and not rect.x0 <= mid <= rect.x1:
                        rects.append(fitz.Rect(cb.x0 - 0.3, rect.y0, cb.x1 + 0.3, rect.y1))
    return rects


def _redact(page: Any, rects: list[Any], *, images: int, graphics: int, text: int,
            preserve: bool = False) -> None:
    """Apply redactions (no fill). With `preserve`, undo two side effects of
    MuPDF redaction on the LIVE page: it prunes font resources no remaining
    text uses (insert_textbox then lays out a re-added base-14 font with
    different line metrics, so the edit would no longer match the measured
    height) and it deletes link annotations overlapping the area (moved
    links are shifted afterwards instead)."""
    if not rects:
        return
    doc = page.parent
    fonts = []
    annots = []
    if preserve:
        fonts = [(entry[0], entry[4]) for entry in page.get_fonts(full=True)
                 if entry[4] and entry[-1] in (0, page.xref)]
        annots = [entry[0] for entry in page.annot_xrefs()]
    for rect in rects:
        page.add_redact_annot(rect, fill=False, cross_out=False)
    page.apply_redactions(images=images, graphics=graphics, text=text)
    if not preserve:
        return
    present = {entry[4] for entry in page.get_fonts(full=True) if entry[-1] in (0, page.xref)}
    for xref, name in fonts:
        if name not in present:
            _set_page_resource(doc, page, "Font", name, xref)
    remaining = {entry[0] for entry in page.annot_xrefs()}
    lost = [xref for xref in annots if xref not in remaining]
    if lost:
        _append_annots(doc, page, lost)


def _set_page_resource(doc: Any, page: Any, category: str, name: str, xref: int) -> None:
    kind, value = doc.xref_get_key(page.xref, "Resources")
    if kind == "xref":
        holder, prefix = int(value.split()[0]), ""
    else:
        if kind != "dict":
            doc.xref_set_key(page.xref, "Resources", "<<>>")
        holder, prefix = page.xref, "Resources/"
    kind, value = doc.xref_get_key(holder, prefix + category)
    if kind == "xref":
        holder, prefix = int(value.split()[0]), ""
    else:
        if kind != "dict":
            doc.xref_set_key(holder, prefix + category, "<<>>")
        prefix = prefix + category + "/"
    doc.xref_set_key(holder, prefix + name, f"{xref} 0 R")


def _append_annots(doc: Any, page: Any, xrefs: list[int]) -> None:
    refs = " ".join(f"{x} 0 R" for x in xrefs)
    kind, value = doc.xref_get_key(page.xref, "Annots")
    if kind == "array":
        doc.xref_set_key(page.xref, "Annots", value.rstrip()[:-1] + " " + refs + "]")
    elif kind == "xref":
        target = int(value.split()[0])
        body = doc.xref_object(target, compressed=True).strip()
        doc.update_object(target, body[:-1] + " " + refs + "]")
    else:
        doc.xref_set_key(page.xref, "Annots", "[" + refs + "]")


def bare_copy(doc: Any, pno: int) -> Any:
    """The page alone in a new document, without annotations, links or
    widgets (get_drawings would otherwise report their appearance streams
    as page content)."""
    import fitz

    copy = fitz.open()
    copy.insert_pdf(doc, from_page=pno, to_page=pno, links=0, annots=0, widgets=0)
    return copy


def build_snapshot(snap: Any, region: Any) -> Any:
    """Strip a bare page copy (see bare_copy) down to ONLY the content that moves."""
    import fitz

    page = snap[0]
    _redact(page, _span_rects(page, region, want_inside=False),
            images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE)
    full = page.rect
    outside = [
        fitz.Rect(full.x0, full.y0, full.x1, region.y0),
        fitz.Rect(full.x0, region.y1, full.x1, full.y1),
        fitz.Rect(full.x0, region.y0, region.x0, region.y1),
        fitz.Rect(region.x1, region.y0, full.x1, region.y1),
    ]
    _redact(page, [r for r in outside if r.width > 0 and r.height > 0],
            images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            text=fitz.PDF_REDACT_TEXT_NONE)
    return snap


def delete_image_placements(helpers: Any, doc: Any, pno: int, refs: list[tuple[int, int]]) -> None:
    """Delete exactly these (xref, placement_index) image placements; later
    placements first so the earlier indices stay valid."""
    images = helpers.images_module()
    order = {}
    scan = images.Scan(doc)
    for position, placement in enumerate(scan.scan_page(pno)):
        order[(placement.xref, placement.index)] = position
    for xref, index in sorted(refs, key=lambda ref: order.get(ref, 0), reverse=True):
        scan = images.Scan(doc)
        scan.scan_all()
        placement = images._find_placement(scan, pno, xref, index)
        images.Editor(scan, placement).apply(lambda *_: b"\n")


def _numbers(text: str) -> list[float]:
    import re

    return [float(v) for v in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)", text or "")]


def _fmt(values: list[float]) -> str:
    return "[" + " ".join(f"{v:.4f}".rstrip("0").rstrip(".") for v in values) + "]"


def page_annotations(page: Any) -> list[tuple[int, Any]]:
    """(xref, rect in engine coordinates) of every annotation on the page
    (links, highlights, notes, form widgets...), popups excluded."""
    import fitz

    doc = page.parent
    found = []
    for entry in page.annot_xrefs():
        xref, kind = entry[0], entry[1]
        if kind == fitz.PDF_ANNOT_POPUP:
            continue
        rtype, rvalue = doc.xref_get_key(xref, "Rect")
        nums = _numbers(rvalue) if rtype == "array" else []
        if len(nums) != 4:
            continue
        rect = fitz.Rect(nums) * page.transformation_matrix
        rect.normalize()
        found.append((xref, rect))
    return found


def shift_annotations(doc: Any, xrefs: list[int], delta: float) -> int:
    """Move these annotations down by `delta`. Rect and the point arrays move
    in PDF space (y up); appearance streams are mapped onto Rect, so they
    follow without being regenerated."""
    moved = 0
    seen: set[int] = set()

    def shift_key(xref: int, key: str, nested: bool = False) -> None:
        kind, value = doc.xref_get_key(xref, key)
        if kind != "array":
            return
        if nested:
            import re

            parts = re.findall(r"\[([^\[\]]*)\]", value[1:-1] if value.startswith("[") else value)
            arrays = []
            for part in parts:
                nums = _numbers(part)
                arrays.append(_fmt([v - delta if i % 2 else v for i, v in enumerate(nums)]))
            doc.xref_set_key(xref, key, "[" + " ".join(arrays) + "]")
            return
        nums = _numbers(value)
        doc.xref_set_key(xref, key, _fmt([v - delta if i % 2 else v for i, v in enumerate(nums)]))

    for xref in xrefs:
        if xref in seen:
            continue
        for key in ("Rect", "QuadPoints", "Vertices", "L", "CL"):
            shift_key(xref, key)
        shift_key(xref, "InkList", nested=True)
        seen.add(xref)
        ptype, pvalue = doc.xref_get_key(xref, "Popup")
        if ptype == "xref":
            popup = int(pvalue.split()[0])
            if popup not in seen:
                shift_key(popup, "Rect")
                seen.add(popup)
        moved += 1
    return moved


def _line_keys(page: Any) -> list[tuple[str, tuple[float, ...]]]:
    keys = []
    for block in page.get_text("dict", flags=_glyph_flags("dict")).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                keys.append((text, tuple(round(v, 1) for v in line["bbox"])))
    return keys


def measure_edit(helpers: Any, input_path: str, pno: int, args: Any, rect: Any,
                 scratch_files: list[str]) -> dict[str, Any]:
    """Run the edit on a throw-away copy and report the rendered text's extent."""
    import fitz

    state: dict[str, Any] = {}

    def isolate(page: Any, info: dict[str, Any]) -> None:
        # Lines present once the block is erased. Everything the edit draws
        # next is appended to the content stream and MuPDF builds lines in
        # stream order, so the new text never merges into these lines even
        # where it overlaps the content below. (Redacting that content first
        # would also prune the page's font resources and change the edit's
        # own layout.)
        lines = info["block_lines"]
        state["old_bottom"] = max((float(l["bbox"][3]) for l in lines), default=float(rect.y1))
        state["before"] = Counter(_line_keys(page))

    with fitz.open(input_path) as scratch:
        page = scratch[pno]
        result = helpers.apply_block_edit(scratch, page, args, rect, scratch_files, before_render=isolate)
        after = Counter(_line_keys(page))
    new = after - state.get("before", Counter())
    extent = None
    for (_text, bbox), _count in new.items():
        box = fitz.Rect(bbox)
        extent = box if extent is None else extent | box
    new_bottom = extent.y1 if extent is not None else float(rect.y0)
    return {
        "inserted": result["inserted"],
        "block_lines": result["block_lines"],
        "render_rect": result["render_rect"],
        "old_bottom": state.get("old_bottom", float(rect.y1)),
        "new_bottom": new_bottom,
        "extent": extent,
    }


def replace_block_with_reflow(helpers: Any, doc: Any, pno: int, args: Any, rect: Any,
                              scratch_files: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """replace-block with reflow. Returns (edit result, reflow report). The
    edit itself always happens; the report says whether content moved."""
    import fitz

    page = doc[pno]
    measured = measure_edit(helpers, args.input, pno, args, rect, scratch_files)
    delta = measured["new_bottom"] - measured["old_bottom"]
    report: dict[str, Any] = {"applied": False, "delta": round(delta, 2)}

    bare = bare_copy(doc, pno)
    try:
        plan: dict[str, Any] = {"ok": False, "reason": "not_inserted"}
        if measured["inserted"] or not args.text.strip():
            plan = plan_reflow(helpers, page, bare[0], rect, measured["block_lines"], measured["render_rect"],
                               measured["extent"], delta)
        if not plan["ok"]:
            report["reason"] = plan["reason"]
            if plan.get("blocker"):
                report["blocker"] = plan["blocker"]
            return helpers.apply_block_edit(doc, page, args, rect, scratch_files), report

        region = plan["region"]
        snapshot = build_snapshot(bare, region)
        moved_annots = plan["annots"]
        if plan["images"]:
            # The page object reads /Contents from the page dict on every
            # operation, so it stays valid after these xref-level splices.
            delete_image_placements(helpers, doc, pno, plan["images"])

        def clear_region(live: Any, _info: dict[str, Any]) -> None:
            # After the block is erased, before the new text is drawn (it may
            # grow into the region): remove the moving text and paths.
            _redact(live, _span_rects(live, region, want_inside=True),
                    images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                    text=fitz.PDF_REDACT_TEXT_REMOVE, preserve=True)
            _redact(live, [region], images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED, text=fitz.PDF_REDACT_TEXT_NONE,
                    preserve=True)

        result = helpers.apply_block_edit(doc, page, args, rect, scratch_files, before_render=clear_region)
        page.show_pdf_page(region + (0, delta, 0, delta), snapshot, 0, clip=region,
                           keep_proportion=False, overlay=True)
        annotations = shift_annotations(doc, moved_annots, delta)
    finally:
        bare.close()

    counts = plan["counts"]
    report.update({
        "applied": True,
        "moved": len(plan["moved"]),
        "lines": counts.get("text", 0),
        "images": counts.get("image", 0),
        "drawings": counts.get("drawing", 0),
        "annotations": annotations,
        "region": [round(v, 2) for v in region],
    })
    return result, report
