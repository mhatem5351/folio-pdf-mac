"""AcroForm field tools: list, add, update, delete, fill, and auto-detect.

Coordinates ("engine coords") follow the rest of the engine: PDF points,
origin at the top-left of the page's CropBox, y growing downward, measured on
the UNROTATED page (the same space PDFKit's ``bounds(for: .cropBox)`` uses
after flipping y). PyMuPDF widget rects already live in exactly this space,
so widget rects pass through unchanged. Text and vector geometry used by
detect-fields is reported by this PyMuPDF on the unrotated page as well (the
QA test pins this on a rotated page).

Field structure: every field is a field dictionary (/T, /FT, /V, /Ff, /DA,
...) with one or more widget annotations in /Kids (PyMuPDF 2.x layout).
Field-level properties are therefore written to the field object directly,
and appearance streams are regenerated through ``Widget.update()`` so the
result renders identically in Apple Preview/PDFKit, Acrobat, and MuPDF
(/NeedAppearances stays false because every widget carries an /AP).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

FIELD_TYPES = ("text", "multiline", "checkbox", "radio", "combo", "list", "signature", "button")

# PDF field flag bits (PDF 32000-1:2008, 12.7.3 / 12.7.4).
FF_READONLY = 1
FF_REQUIRED = 2
FF_MULTILINE = 4096

DEFAULT_PREFIX = {
    "text": "Text",
    "multiline": "Notes",
    "checkbox": "Checkbox",
    "radio": "Choice",
    "combo": "Dropdown",
    "list": "List",
    "signature": "Signature",
    "button": "Button",
}

BORDER_COLOR = (0.55, 0.58, 0.66)
BOX_GLYPHS = set("☐☑☒□❏❑▢◻⬜")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _fitz() -> Any:
    import pymupdf

    return pymupdf


def _round_rect(rect: Any) -> list[float]:
    return [round(float(rect.x0), 2), round(float(rect.y0), 2), round(float(rect.x1), 2), round(float(rect.y1), 2)]


def _parse_rect(text: str | None) -> Any:
    fitz = _fitz()
    if not text:
        return None
    parts = [float(part.strip()) for part in text.split(",")]
    if len(parts) != 4:
        raise ValueError("--rect must be x0,y0,x1,y1")
    return fitz.Rect(min(parts[0], parts[2]), min(parts[1], parts[3]), max(parts[0], parts[2]), max(parts[1], parts[3]))


def _parse_options(text: str | None) -> list[str] | None:
    if text is None:
        return None
    return [part.strip() for part in text.split("|") if part.strip()]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "checked", "x"}


def _decode_name(name: str) -> str:
    """Decode #xx escapes in a PDF name (without the leading slash)."""
    if "#" not in name:
        return name
    raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), name.encode("latin-1", "replace"))
    return raw.decode("utf-8", "replace")


def _export_name(label: str, index: int, taken: set[str]) -> str:
    """ASCII-safe on-state name for a radio button (PDF names with spaces or
    non-ASCII bytes round-trip inconsistently across viewers)."""
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]+", "_", label.strip()).strip("_")
    base = cleaned or f"Choice{index}"
    if base == "Off":
        base = "Off_"
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}_{n}"
        n += 1
    taken.add(candidate)
    return candidate


def _xref_of(ref: tuple[str, str]) -> int | None:
    kind, value = ref
    if kind != "xref":
        return None
    return int(value.split()[0])


def _field_xref(doc: Any, widget_xref: int) -> int:
    """The field dictionary owning a widget (holds /T, /V, /Ff, /DA...)."""
    if doc.xref_get_key(widget_xref, "T")[0] != "null":
        return widget_xref
    parent = _xref_of(doc.xref_get_key(widget_xref, "Parent"))
    return parent if parent is not None else widget_xref


def _kind_of(widget: Any) -> str:
    fitz = _fitz()
    t = widget.field_type
    if t == fitz.PDF_WIDGET_TYPE_TEXT:
        return "multiline" if (widget.field_flags or 0) & FF_MULTILINE else "text"
    return {
        fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
        fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
        fitz.PDF_WIDGET_TYPE_COMBOBOX: "combo",
        fitz.PDF_WIDGET_TYPE_LISTBOX: "list",
        fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature",
        fitz.PDF_WIDGET_TYPE_BUTTON: "button",
    }.get(t, "unknown")


def _widget_type_constant(kind: str) -> int:
    fitz = _fitz()
    return {
        "text": fitz.PDF_WIDGET_TYPE_TEXT,
        "multiline": fitz.PDF_WIDGET_TYPE_TEXT,
        "checkbox": fitz.PDF_WIDGET_TYPE_CHECKBOX,
        "radio": fitz.PDF_WIDGET_TYPE_RADIOBUTTON,
        "combo": fitz.PDF_WIDGET_TYPE_COMBOBOX,
        "list": fitz.PDF_WIDGET_TYPE_LISTBOX,
        "signature": fitz.PDF_WIDGET_TYPE_SIGNATURE,
        "button": fitz.PDF_WIDGET_TYPE_BUTTON,
    }[kind]


def _collect(doc: Any) -> dict[str, list[tuple[int, int]]]:
    """Field name -> [(page index, widget xref)] in document order."""
    groups: dict[str, list[tuple[int, int]]] = {}
    if not doc.is_form_pdf:
        return groups  # no AcroForm: skip walking every page
    for page in doc:
        for widget in page.widgets():
            groups.setdefault(widget.field_name or "", []).append((page.number, widget.xref))
    return groups


def _load(doc: Any, page_index: int, xref: int) -> Any:
    page = doc[page_index]
    widget = page.load_widget(xref)
    # Widgets only hold a weak reference to their page; keep it alive.
    widget._owner_page = page
    return widget


def _union(rects: list[Any]) -> Any:
    fitz = _fitz()
    out = fitz.Rect(rects[0])
    for rect in rects[1:]:
        out |= rect
    return out


def _unique_name(base: str, taken: set[str]) -> str:
    base = base.strip().replace(".", "_") or "Field"
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _default_name(kind: str, taken: set[str]) -> str:
    prefix = DEFAULT_PREFIX.get(kind, "Field")
    n = 1
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


def _radio_states(doc: Any, entries: list[tuple[int, int]]) -> list[str]:
    states = []
    for page_index, xref in entries:
        widget = _load(doc, page_index, xref)
        states.append(widget.on_state() or "")
    return states


def _set_button_state(doc: Any, field_xref: int, entries: list[tuple[int, int]], on: str | None) -> None:
    """Select the kid whose on-state is ``on`` (None = all off) and keep /V
    on the field as a name, never on the kids."""
    chosen = None
    for page_index, xref in entries:
        widget = _load(doc, page_index, xref)
        state = widget.on_state() or "Yes"
        if on is not None and state == on and chosen is None:
            doc.xref_set_key(xref, "AS", f"/{state}")
            chosen = state
        else:
            doc.xref_set_key(xref, "AS", "/Off")
        if xref != field_xref:
            doc.xref_set_key(xref, "V", "null")
    doc.xref_set_key(field_xref, "V", f"/{chosen}" if chosen else "/Off")


def _field_flags(doc: Any, field_xref: int) -> int:
    kind, value = doc.xref_get_key(field_xref, "Ff")
    try:
        return int(float(value)) if kind in ("int", "float") else 0
    except ValueError:
        return 0


def _set_flag(doc: Any, field_xref: int, bit: int, on: bool) -> None:
    flags = _field_flags(doc, field_xref)
    flags = flags | bit if on else flags & ~bit
    doc.xref_set_key(field_xref, "Ff", str(flags))


def _set_font_size(doc: Any, xrefs: list[int], size: float) -> None:
    fitz = _fitz()
    pattern = re.compile(r"(/[^\s/]+\s+)[-+]?[\d.]+(\s+Tf)")
    for xref in xrefs:
        kind, da = doc.xref_get_key(xref, "DA")
        if kind == "null":
            continue
        da = da if kind == "string" else "0 g /Helv 0 Tf"
        if pattern.search(da):
            da = pattern.sub(lambda m: f"{m.group(1)}{size:g}{m.group(2)}", da, count=1)
        else:
            da = f"{da} /Helv {size:g} Tf".strip()
        doc.xref_set_key(xref, "DA", fitz.get_pdf_str(da))


def _choice_value(widget: Any) -> str:
    value = widget.field_value
    if isinstance(value, (list, tuple)):
        return "|".join(str(v) for v in value)
    return "" if value in (None, "Off") else str(value)


def _field_payload(doc: Any, name: str, entries: list[tuple[int, int]]) -> dict[str, Any]:
    first_page, first_xref = entries[0]
    widget = _load(doc, first_page, first_xref)
    kind = _kind_of(widget)
    fx = _field_xref(doc, first_xref)
    flags = widget.field_flags or 0
    widgets = []
    for page_index, xref in entries:
        w = _load(doc, page_index, xref)
        item: dict[str, Any] = {"page": page_index + 1, "rect": _round_rect(w.rect), "xref": xref}
        if kind in ("checkbox", "radio"):
            item["state"] = _decode_name(w.on_state() or "")
        widgets.append(item)

    options: list[str] = []
    if kind == "radio":
        options = [_decode_name(s) for s in _radio_states(doc, entries)]
        v_kind, v = doc.xref_get_key(fx, "V")
        selected = _decode_name(v[1:]) if v_kind == "name" else ""
        value: Any = "" if selected in ("", "Off") else selected
        same_page = [_load(doc, p, x).rect for p, x in entries if p == first_page]
        rect = _union(same_page)
    elif kind == "checkbox":
        value = widget.field_value not in (None, "", "Off", False)
        rect = widget.rect
    elif kind in ("combo", "list"):
        options = [str(o[1] if isinstance(o, (list, tuple)) else o) for o in (widget.choice_values or [])]
        value = _choice_value(widget)
        rect = widget.rect
    elif kind in ("text", "multiline"):
        value = "" if widget.field_value is None else str(widget.field_value)
        rect = widget.rect
    else:
        value = ""
        rect = widget.rect

    return {
        "name": name,
        "type": kind,
        "page": first_page + 1,
        "rect": _round_rect(rect),
        "value": value,
        "options": options,
        "required": bool(flags & FF_REQUIRED),
        "readonly": bool(flags & FF_READONLY),
        "tooltip": widget.field_label or "",
        "font_size": float(widget.text_fontsize or 0),
        "max_len": int(widget.text_maxlen or 0),
        "widgets": widgets,
    }


def _ensure_acroform_defaults(doc: Any) -> None:
    """Default resources Acrobat expects (/DR fonts referenced by /DA),
    explicit NeedAppearances=false (every widget has an /AP), and no stale
    /SigFlags claiming signatures exist when none are signed."""
    cat = doc.pdf_catalog()
    if doc.xref_get_key(cat, "AcroForm")[0] == "null":
        return
    if doc.xref_get_key(cat, "AcroForm/DR/Font/Helv")[0] == "null":
        helv = doc.get_new_xref()
        doc.update_object(helv, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>")
        doc.xref_set_key(cat, "AcroForm/DR/Font/Helv", f"{helv} 0 R")
    if doc.xref_get_key(cat, "AcroForm/DR/Font/ZaDb")[0] == "null":
        zadb = doc.get_new_xref()
        doc.update_object(zadb, "<</Type/Font/Subtype/Type1/BaseFont/ZapfDingbats>>")
        doc.xref_set_key(cat, "AcroForm/DR/Font/ZaDb", f"{zadb} 0 R")
    if doc.xref_get_key(cat, "AcroForm/DA")[0] == "null":
        doc.xref_set_key(cat, "AcroForm/DA", "(/Helv 0 Tf 0 g)")
    doc.xref_set_key(cat, "AcroForm/NeedAppearances", "false")

    signed = False
    for page in doc:
        for widget in page.widgets():
            if _kind_of(widget) == "signature":
                fx = _field_xref(doc, widget.xref)
                if doc.xref_get_key(fx, "V")[0] != "null":
                    signed = True
    if not signed and doc.xref_get_key(cat, "AcroForm/SigFlags")[0] != "null":
        doc.xref_set_key(cat, "AcroForm/SigFlags", "null")


def _save(doc: Any, api: Any, output: str) -> Path:
    _ensure_acroform_defaults(doc)
    path = Path(output)
    api.ensure_parent(path)
    doc.save(str(path), garbage=3, deflate=True)
    return path


def _open(path: str) -> Any:
    doc = _fitz().open(path)
    if not doc.is_pdf:
        doc.close()
        raise ValueError("input is not a PDF")
    if doc.needs_pass:
        doc.close()
        raise ValueError("document is password protected; unlock it first")
    return doc


# --------------------------------------------------------------------------
# Creating widgets
# --------------------------------------------------------------------------

def _new_widget(kind: str, name: str, rect: Any, *, font_size: float = 0, flags: int = 0,
                tooltip: str | None = None, max_len: int = 0) -> Any:
    fitz = _fitz()
    widget = fitz.Widget()
    widget.field_type = _widget_type_constant(kind)
    widget.field_name = name
    widget.rect = fitz.Rect(rect)
    widget.border_color = BORDER_COLOR
    widget.border_width = 1
    widget.text_color = (0, 0, 0)
    widget.text_font = "Helv"
    widget.text_fontsize = max(0.0, float(font_size or 0))
    if kind == "multiline":
        flags |= FF_MULTILINE
    widget.field_flags = flags
    if tooltip:
        widget.field_label = tooltip
    if kind in ("text", "multiline") and max_len and max_len > 0:
        widget.text_maxlen = int(max_len)
    return widget


def _radio_layout(rect: Any, count: int) -> list[Any]:
    """Square buttons spread across ``rect``: a row, or a column when the
    rect is clearly taller than wide."""
    fitz = _fitz()
    vertical = rect.height > rect.width * 1.5
    if vertical:
        side = min(rect.width, rect.height / max(count, 1), 18)
        step = rect.height / count
        return [fitz.Rect(rect.x0, rect.y0 + i * step, rect.x0 + side, rect.y0 + i * step + side) for i in range(count)]
    side = min(rect.height, rect.width / max(count, 1), 18)
    step = rect.width / count
    return [fitz.Rect(rect.x0 + i * step, rect.y0, rect.x0 + i * step + side, rect.y0 + side) for i in range(count)]


def _add_field(doc: Any, page_index: int, rect: Any, kind: str, name: str | None, *,
               options: list[str] | None = None, value: Any = None, font_size: float = 0,
               required: bool = False, readonly: bool = False, tooltip: str | None = None,
               max_len: int = 0, group: str | None = None) -> dict[str, Any]:
    """Create one field. Returns {name, renamed, type, options?}."""
    fitz = _fitz()
    if kind not in FIELD_TYPES:
        raise ValueError(f"unknown field type '{kind}'; use one of {', '.join(FIELD_TYPES)}")
    if rect.width < 4 or rect.height < 4:
        raise ValueError("field rect must be at least 4x4 points")
    page = doc[page_index]
    groups = _collect(doc)
    taken = set(groups)
    flags = (FF_REQUIRED if required else 0) | (FF_READONLY if readonly else 0)

    # Joining an existing radio group: one more button, value = its export.
    if kind == "radio" and group and group in groups:
        entries = groups[group]
        first = _load(doc, *entries[0])
        if _kind_of(first) == "radio":
            states = set(_radio_states(doc, entries))
            export = _export_name(str(value or ""), len(states) + 1, states | {"Off"})
            widget = _new_widget("radio", group, rect, flags=flags, tooltip=tooltip)
            widget.field_value = export
            page.add_widget(widget)
            fx = _field_xref(doc, widget.xref)
            v_kind, v = doc.xref_get_key(fx, "V")
            current = v[1:] if v_kind == "name" and v != "/Off" else None
            _set_button_state(doc, fx, _collect(doc)[group], current)
            return {"name": group, "renamed": False, "type": "radio", "joined_group": True, "export": export}

    requested = (group if kind == "radio" and group else name) or ""
    requested = requested.strip()
    if requested:
        final = _unique_name(requested, taken)
    else:
        final = _default_name(kind, taken)
    result: dict[str, Any] = {"name": final, "renamed": bool(requested) and final != requested, "type": kind}

    if kind == "radio":
        labels = options or ([str(value)] if value not in (None, "") else ["Option 1", "Option 2"])
        if group and not options:
            labels = [str(value)] if value not in (None, "") else ["Choice1"]
        taken_states: set[str] = {"Off"}
        exports = [_export_name(label, i + 1, taken_states) for i, label in enumerate(labels)]
        rects = _radio_layout(rect, len(exports)) if len(exports) > 1 else [rect]
        for export, button_rect in zip(exports, rects):
            widget = _new_widget("radio", final, button_rect, flags=flags, tooltip=tooltip)
            widget.field_value = export
            page.add_widget(widget)
        entries = _collect(doc)[final]
        fx = _field_xref(doc, entries[0][1])
        selected = None
        if options and value not in (None, ""):
            wanted = str(value)
            for label, export in zip(labels, exports):
                if wanted in (label, export):
                    selected = export
                    break
        _set_button_state(doc, fx, entries, selected)
        result["options"] = exports
        return result

    widget = _new_widget(kind, final, rect, font_size=font_size, flags=flags, tooltip=tooltip, max_len=max_len)
    if kind in ("text", "multiline"):
        widget.field_value = "" if value is None else str(value)
    elif kind == "checkbox":
        widget.field_value = _truthy(value)
    elif kind in ("combo", "list"):
        choices = options or ["Option 1", "Option 2", "Option 3"]
        widget.choice_values = choices
        if value not in (None, "") and str(value) in choices:
            widget.field_value = str(value)
        result["options"] = choices
    elif kind == "button":
        widget.button_caption = str(value) if value not in (None, "") else final
    page.add_widget(widget)

    if kind == "checkbox":
        entries = _collect(doc)[final]
        fx = _field_xref(doc, entries[0][1])
        on = _load(doc, *entries[0]).on_state() or "Yes"
        _set_button_state(doc, fx, entries, on if _truthy(value) else None)
    return result


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

def _set_value(doc: Any, entries: list[tuple[int, int]], value: Any) -> str | None:
    """Apply ``value`` to a field. Returns an error string or None."""
    fitz = _fitz()
    widget = _load(doc, *entries[0])
    kind = _kind_of(widget)
    fx = _field_xref(doc, entries[0][1])

    if kind == "checkbox":
        on = widget.on_state() or "Yes"
        _set_button_state(doc, fx, entries, on if _truthy(value) else None)
        return None
    if kind == "radio":
        if value in (None, "", False) or str(value) == "Off":
            _set_button_state(doc, fx, entries, None)
            return None
        wanted = str(value)
        for state in _radio_states(doc, entries):
            if wanted in (state, _decode_name(state)):
                _set_button_state(doc, fx, entries, state)
                return None
        options = [_decode_name(s) for s in _radio_states(doc, entries)]
        return f"'{wanted}' is not one of {options}"
    if kind in ("signature", "button"):
        return f"{kind} fields have no fillable value"

    if kind in ("combo", "list"):
        options = [str(o[1] if isinstance(o, (list, tuple)) else o) for o in (widget.choice_values or [])]
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = "" if value is None else str(value)
        if text and text not in options:
            return f"'{text}' is not one of {options}"
    else:
        text = "" if value is None else str(value)
        if widget.text_maxlen and len(text) > widget.text_maxlen:
            text = text[: widget.text_maxlen]

    doc.xref_set_key(fx, "V", fitz.get_pdf_str(text))
    for page_index, xref in entries:
        w = _load(doc, page_index, xref)
        w.field_value = text
        w.update()
    return None


def _regenerate(doc: Any, entries: list[tuple[int, int]], new_rects: dict[int, Any] | None = None) -> None:
    """Rebuild appearance streams (after DA/flag/option/rect changes)."""
    first = _load(doc, *entries[0])
    kind = _kind_of(first)
    fx = _field_xref(doc, entries[0][1])
    if kind in ("checkbox", "radio"):
        v_kind, v = doc.xref_get_key(fx, "V")
        current = v[1:] if v_kind == "name" and v != "/Off" else None
    for page_index, xref in entries:
        w = _load(doc, page_index, xref)
        if new_rects and xref in new_rects:
            w.rect = new_rects[xref]
        elif kind in ("checkbox", "radio"):
            continue  # on/off appearances do not depend on DA or flags
        w.update()
    if kind in ("checkbox", "radio"):
        _set_button_state(doc, fx, entries, current)


# --------------------------------------------------------------------------
# Auto-detect ("Prepare Form")
# --------------------------------------------------------------------------

LABEL_TRIM = " \t:*_-–—.()[]# "


def _clean_label(text: str) -> str:
    text = re.sub(r"_+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(LABEL_TRIM).strip()
    words = text.split(" ")
    if len(words) > 5:
        text = " ".join(words[-5:])
    return text[:40].strip(LABEL_TRIM)


def _is_light(color: Any) -> bool:
    if color is None:
        return True
    if len(color) == 1:
        return color[0] >= 0.75
    if len(color) == 4:  # CMYK
        c, m, y, k = color
        return max(c, m, y) <= 0.25 and k <= 0.25
    r, g, b = color[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b >= 0.75


def _words(page: Any) -> list[tuple[Any, str, tuple[int, int]]]:
    fitz = _fitz()
    out = []
    for x0, y0, x1, y1, text, block, line, _ in page.get_text("words"):
        if not text.strip() or set(text) <= {"_"}:
            continue
        out.append((fitz.Rect(x0, y0, x1, y1), text, (block, line)))
    return out


def _join_line(words: list[tuple[Any, str, tuple[int, int]]]) -> str:
    return " ".join(t for _, t, _ in sorted(words, key=lambda w: w[0].x0))


def _label_left(words: list, rect: Any, reach: float = 220) -> str:
    cy = (rect.y0 + rect.y1) / 2
    band = [w for w in words if w[0].x1 <= rect.x0 + 1.5 and rect.x0 - w[0].x1 <= reach
            and w[0].y0 - 3 <= cy <= w[0].y1 + 3]
    if not band:
        return ""
    band.sort(key=lambda w: -w[0].x1)
    picked = [band[0]]
    for w in band[1:]:
        if picked[-1][0].x0 - w[0].x1 > 14 or w[2] != band[0][2]:
            break
        picked.append(w)
    return _join_line(picked)


def _label_right(words: list, rect: Any, reach: float = 30) -> str:
    cy = (rect.y0 + rect.y1) / 2
    band = [w for w in words if w[0].x0 >= rect.x1 - 1.5 and w[0].x0 - rect.x1 <= reach
            and w[0].y0 - 3 <= cy <= w[0].y1 + 3]
    if not band:
        return ""
    band.sort(key=lambda w: w[0].x0)
    picked = [band[0]]
    later = sorted([w for w in words if w[0].x0 > band[0][0].x0 and w[0].y0 - 3 <= cy <= w[0].y1 + 3],
                   key=lambda w: w[0].x0)
    for w in later:
        if w[0].x0 - picked[-1][0].x1 > 10:
            break
        picked.append(w)
        if len(picked) >= 6:
            break
    return _join_line(picked)


def _label_above(words: list, rect: Any, reach: float = 18) -> str:
    band = [w for w in words if rect.y0 - reach <= w[0].y1 <= rect.y0 + 2
            and w[0].x1 > rect.x0 - 2 and w[0].x0 < rect.x0 + max(rect.width * 0.6, 40)]
    if not band:
        return ""
    line = max(band, key=lambda w: w[0].y1)[2]
    return _join_line([w for w in band if w[2] == line])


def _label_below(words: list, rect: Any, reach: float = 14) -> str:
    band = [w for w in words if rect.y1 - 2 <= w[0].y0 <= rect.y1 + reach
            and w[0].x1 > rect.x0 and w[0].x0 < rect.x1]
    if not band:
        return ""
    line = min(band, key=lambda w: w[0].y0)[2]
    return _join_line([w for w in band if w[2] == line])


def _text_inside(words: list, rect: Any) -> bool:
    inner = _fitz().Rect(rect.x0 + 0.5, rect.y0 + 0.5, rect.x1 - 0.5, rect.y1 - 0.5)
    for r, _, _ in words:
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        if inner.contains(_fitz().Point(cx, cy)):
            return True
    return False


def _rects_from_path(path: dict) -> tuple[list[Any], list[tuple[float, float, float]]]:
    """Closed axis-aligned rectangles and horizontal segments in a drawing."""
    fitz = _fitz()
    items = path.get("items") or []
    rects: list[Any] = []
    hlines: list[tuple[float, float, float]] = []
    if not items:
        return rects, hlines
    ops = {it[0] for it in items}
    if ops <= {"re"}:
        rects = [fitz.Rect(it[1]) for it in items]
    elif ops <= {"qu"}:
        for it in items:
            quad = it[1]
            if quad.is_rectangular and abs(quad.ul.y - quad.ur.y) < 0.5:
                rects.append(quad.rect)
    elif ops <= {"l"}:
        segs = [(it[1], it[2]) for it in items]
        axis = all(abs(a.x - b.x) < 0.6 or abs(a.y - b.y) < 0.6 for a, b in segs)
        bbox = fitz.Rect(path["rect"])
        if axis and 4 <= len(segs) <= 5 and bbox.width > 3 and bbox.height > 3:
            on_edge = all(
                (abs(a.x - b.x) < 0.6 and (abs(a.x - bbox.x0) < 1 or abs(a.x - bbox.x1) < 1))
                or (abs(a.y - b.y) < 0.6 and (abs(a.y - bbox.y0) < 1 or abs(a.y - bbox.y1) < 1))
                for a, b in segs
            )
            horizontal = sum(1 for a, b in segs if abs(a.y - b.y) < 0.6)
            if on_edge and horizontal >= 2 and len(segs) - horizontal >= 2:
                rects.append(bbox)
                return rects, hlines
        for a, b in segs:
            if abs(a.y - b.y) < 0.6 and abs(a.x - b.x) >= 40:
                hlines.append((min(a.x, b.x), max(a.x, b.x), (a.y + b.y) / 2))
    return rects, hlines


def _overlap_ratio(a: Any, b: Any) -> float:
    inter = a & b
    if inter.is_empty:
        return 0.0
    smaller = min(a.width * a.height, b.width * b.height) or 1.0
    return (inter.width * inter.height) / smaller


def _detect_on_page(page: Any) -> list[dict[str, Any]]:
    fitz = _fitz()
    words = _words(page)
    page_w, page_h = page.cropbox.width, page.cropbox.height
    page_area = page_w * page_h
    found: list[dict[str, Any]] = []

    # 1) Vector boxes and lines.
    boxes: list[Any] = []
    hlines: list[tuple[float, float, float]] = []
    for path in page.get_drawings():
        fill = path.get("fill")
        stroke = path.get("color")
        rects, lines = _rects_from_path(path)
        for r in rects:
            if r.height <= 1.6 and r.width >= 40:
                hlines.append((r.x0, r.x1, (r.y0 + r.y1) / 2))  # rule drawn as a thin filled rect
                continue
            if stroke is None and (fill is None or min(fill[:3] if len(fill) >= 3 else fill) >= 0.98):
                continue  # invisible / white background rect
            if fill is not None and not _is_light(fill):
                continue  # dark decoration, filled bullet, bar
            boxes.append(r)
        hlines.extend(lines)

    candidates: list[tuple[Any, str]] = []
    for r in boxes:
        w, h = r.width, r.height
        if w < 5 or h < 5 or w * h > page_area * 0.3:
            continue
        if abs(w - h) <= max(2.0, 0.15 * max(w, h)) and 6 <= w <= 26:
            kind = "checkbox"
        elif w >= 24 and 9 <= h <= 40:
            kind = "text"
        elif w >= 60 and 40 < h <= 400:
            kind = "multiline"
        else:
            continue
        if _text_inside(words, r):
            continue
        candidates.append((r, kind))
    # Drop containers (a frame around other boxes) and exact duplicates.
    kept: list[tuple[Any, str]] = []
    for r, kind in candidates:
        if any(o is not r and r.contains(o) and (o.width * o.height) < (r.width * r.height) * 0.9 for o, _ in candidates):
            continue
        if any(_overlap_ratio(r, k) > 0.9 for k, _ in kept):
            continue
        kept.append((r, kind))
    # Comb fields: a row of >=4 touching squares is one text field.
    squares = sorted([c for c in kept if c[1] == "checkbox"], key=lambda c: (round(c[0].y0), c[0].x0))
    merged_ids: set[int] = set()
    i = 0
    while i < len(squares):
        chain = [squares[i]]
        j = i + 1
        while j < len(squares):
            prev, cur = chain[-1][0], squares[j][0]
            if abs(cur.y0 - prev.y0) <= 1.0 and abs(cur.height - prev.height) <= 1.0 and 0 <= cur.x0 - prev.x1 <= 2.0:
                chain.append(squares[j])
                j += 1
            else:
                break
        if len(chain) >= 4:
            for c in chain:
                merged_ids.add(id(c[0]))
            kept.append((_union([c[0] for c in chain]), "text"))
        i = j if len(chain) > 1 else i + 1
    kept = [c for c in kept if id(c[0]) not in merged_ids]

    for r, kind in kept:
        if kind == "checkbox":
            hint = _label_right(words, r) or _label_left(words, r)
        elif kind == "multiline":
            hint = _label_above(words, r) or _label_left(words, r)
        else:
            hint = _label_left(words, r) or _label_above(words, r) or _label_below(words, r)
        found.append({"rect": r, "type": kind, "hint": hint, "source": "box"})

    # 2) Runs of underscores in text ("Name: ________").
    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars: list[tuple[str, Any, float, float]] = []
            for span in line.get("spans", []):
                baseline = span["origin"][1]
                for ch in span.get("chars", []):
                    chars.append((ch["c"], fitz.Rect(ch["bbox"]), baseline, span["size"]))
            k = 0
            label_start = 0
            while k < len(chars):
                c = chars[k][0]
                if c in BOX_GLYPHS:
                    _, bbox, baseline, size = chars[k]
                    side = max(7.0, min(size * 0.8, 18.0))
                    cx = (bbox.x0 + bbox.x1) / 2
                    cy = baseline - size * 0.35
                    box = fitz.Rect(cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)
                    rest = "".join(ch[0] for ch in chars[k + 1:])
                    rest = re.split(r"[" + "".join(BOX_GLYPHS) + r"_]", rest)[0]
                    found.append({"rect": box, "type": "checkbox", "hint": rest, "source": "glyph"})
                    k += 1
                    label_start = k
                    continue
                if c in "_＿":
                    end = k
                    while end < len(chars) and chars[end][0] in "_＿":
                        end += 1
                    if end - k >= 3:
                        run = _union([ch[1] for ch in chars[k:end]])
                        baseline, size = chars[k][2], chars[k][3]
                        bottom = baseline + size * 0.18
                        height = max(12.0, min(size * 1.35, 24.0))
                        rect = fitz.Rect(run.x0, bottom - height, run.x1, bottom)
                        hint = "".join(ch[0] for ch in chars[label_start:k])
                        if not _clean_label(hint):
                            hint = _label_below(words, rect, 12) or _label_above(words, rect, 10)
                        found.append({"rect": rect, "type": "text", "hint": hint, "source": "underscore"})
                    k = end
                    label_start = k
                    continue
                k += 1

    # 3) Drawn horizontal rules used as blanks/signature lines.
    for x0, x1, y in hlines:
        line_rect = fitz.Rect(x0, y - 16, x1, y)
        if line_rect.width > page_w * 0.9:
            continue
        sits_on_text = any(r.x1 > x0 + 2 and r.x0 < x1 - 2 and y - 18 <= r.y1 <= y + 2 for r, _, _ in words)
        if sits_on_text:
            continue  # underline / table rule beneath text
        if any(_overlap_ratio(fitz.Rect(x0, y - 1, x1, y + 1), b) > 0.5 for b in boxes):
            continue  # edge of a detected box
        hint = _label_left(words, line_rect, 160) or _label_below(words, line_rect, 14)
        if not hint:
            continue
        found.append({"rect": fitz.Rect(x0, y - 16, x1, y - 0.5), "type": "text", "hint": hint, "source": "line"})

    # Type refinements from the label and de-duplication (boxes win).
    priority = {"box": 0, "glyph": 1, "underscore": 2, "line": 3}
    found.sort(key=lambda f: priority[f["source"]])
    result: list[dict[str, Any]] = []
    for item in found:
        item["hint"] = _clean_label(item["hint"] or "")
        if item["type"] == "text" and re.search(r"\bsignature\b|\bsign here\b|\bsigned\b|توقيع", item["hint"], re.I):
            item["type"] = "signature"
            r = item["rect"]
            if r.height < 24:
                item["rect"] = fitz.Rect(r.x0, r.y1 - 24, r.x1, r.y1)
        if any(_overlap_ratio(item["rect"], other["rect"]) > 0.4 for other in result):
            continue
        result.append(item)
    return result


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def register(subparsers: Any, api: Any) -> None:
    def find_or_fail(doc: Any, name: str) -> list[tuple[int, int]]:
        groups = _collect(doc)
        if name not in groups:
            raise LookupError(f"no field named '{name}'")
        return groups[name]

    def cmd_list_fields(args: argparse.Namespace) -> int:
        with _open(args.input) as doc:
            groups = _collect(doc)
            fields = [_field_payload(doc, name, entries) for name, entries in groups.items()]
        return api.respond(True, fields=fields, count=len(fields))

    def cmd_add_field(args: argparse.Namespace) -> int:
        try:
            rect = _parse_rect(args.rect)
        except ValueError as exc:
            return api.respond(False, message=str(exc))
        with _open(args.input) as doc:
            if args.page < 1 or args.page > doc.page_count:
                return api.respond(False, message=f"page must be between 1 and {doc.page_count}")
            try:
                info = _add_field(
                    doc, args.page - 1, rect, args.type, args.name,
                    options=_parse_options(args.options), value=args.value, font_size=args.font_size,
                    required=args.required, readonly=args.readonly, tooltip=args.tooltip,
                    max_len=args.max_len, group=args.group,
                )
            except ValueError as exc:
                return api.respond(False, message=str(exc))
            out = _save(doc, api, args.output)
        return api.respond(True, output=str(out), page=args.page, **info)

    def cmd_update_field(args: argparse.Namespace) -> int:
        fitz = _fitz()
        with _open(args.input) as doc:
            try:
                entries = find_or_fail(doc, args.name)
            except LookupError as exc:
                return api.respond(False, message=str(exc))
            first = _load(doc, *entries[0])
            kind = _kind_of(first)
            fx = _field_xref(doc, entries[0][1])
            name = args.name
            changed: list[str] = []
            needs_regen = False
            new_rects: dict[int, Any] = {}

            if args.new_name is not None and args.new_name.strip() and args.new_name.strip() != name:
                new_name = args.new_name.strip()
                if "." in new_name:
                    return api.respond(False, message="field names cannot contain '.'")
                if new_name in _collect(doc):
                    return api.respond(False, message=f"a field named '{new_name}' already exists")
                prefix = name.rsplit(".", 1)[0] + "." if "." in name else ""
                doc.xref_set_key(fx, "T", fitz.get_pdf_str(new_name))
                name = prefix + new_name
                changed.append("name")

            if args.options is not None:
                opts = _parse_options(args.options) or []
                if kind in ("combo", "list"):
                    if not opts:
                        return api.respond(False, message="options cannot be empty")
                    doc.xref_set_key(fx, "Opt", "[" + "".join(fitz.get_pdf_str(o) for o in opts) + "]")
                    current = _choice_value(first)
                    if current and current not in opts:
                        doc.xref_set_key(fx, "V", "null")
                    needs_regen = True
                    changed.append("options")
                elif kind == "radio":
                    states = _radio_states(doc, entries)
                    if len(opts) != len(states):
                        return api.respond(False, message=f"radio group has {len(states)} buttons; give exactly {len(states)} options")
                    taken: set[str] = {"Off"}
                    exports = [_export_name(o, i + 1, taken) for i, o in enumerate(opts)]
                    v_kind, v = doc.xref_get_key(fx, "V")
                    for (page_index, xref), old, new in zip(entries, states, exports):
                        if old == new:
                            continue
                        for key in ("AP/N", "AP/D"):
                            k, obj = doc.xref_get_key(xref, key)
                            if k == "dict":
                                doc.xref_set_key(xref, key, re.sub(rf"/{re.escape(old)}(?=[\s/<>\[\]()]|$)", f"/{new}", obj))
                        if doc.xref_get_key(xref, "AS")[1] == f"/{old}":
                            doc.xref_set_key(xref, "AS", f"/{new}")
                        if v_kind == "name" and v == f"/{old}":
                            doc.xref_set_key(fx, "V", f"/{new}")
                    changed.append("options")
                else:
                    return api.respond(False, message=f"{kind} fields have no options")

            if args.font_size is not None:
                xrefs = [fx] + [x for _, x in entries if x != fx and doc.xref_get_key(x, "DA")[0] != "null"]
                if doc.xref_get_key(fx, "DA")[0] == "null":
                    doc.xref_set_key(fx, "DA", fitz.get_pdf_str("0 g /Helv 0 Tf"))
                _set_font_size(doc, xrefs, max(0.0, args.font_size))
                needs_regen = True
                changed.append("font_size")

            if args.required is not None:
                _set_flag(doc, fx, FF_REQUIRED, args.required)
                changed.append("required")
            if args.readonly is not None:
                _set_flag(doc, fx, FF_READONLY, args.readonly)
                needs_regen = True
                changed.append("readonly")
            if args.tooltip is not None:
                doc.xref_set_key(fx, "TU", fitz.get_pdf_str(args.tooltip) if args.tooltip else "null")
                changed.append("tooltip")
            if args.max_len is not None and kind in ("text", "multiline"):
                doc.xref_set_key(fx, "MaxLen", str(args.max_len) if args.max_len > 0 else "null")
                changed.append("max_len")

            if args.rect is not None:
                try:
                    target = _parse_rect(args.rect)
                except ValueError as exc:
                    return api.respond(False, message=str(exc))
                if target.width < 4 or target.height < 4:
                    return api.respond(False, message="field rect must be at least 4x4 points")
                first_page = entries[0][0]
                same_page = [(p, x) for p, x in entries if p == first_page]
                if kind == "radio" and len(same_page) > 1:
                    old_rects = {x: _load(doc, p, x).rect for p, x in same_page}
                    union = _union(list(old_rects.values()))
                    sx = target.width / (union.width or 1)
                    sy = target.height / (union.height or 1)
                    side = min(sx, sy)  # buttons stay square; spacing follows the rect
                    for x, r in old_rects.items():
                        x0 = target.x0 + (r.x0 - union.x0) * sx
                        y0 = target.y0 + (r.y0 - union.y0) * sy
                        new_rects[x] = fitz.Rect(x0, y0, x0 + r.width * side, y0 + r.height * side)
                else:
                    new_rects[entries[0][1]] = target
                changed.append("rect")

            entries = _collect(doc)[name]
            if needs_regen or new_rects:
                _regenerate(doc, entries, new_rects)

            if args.value is not None:
                error = _set_value(doc, entries, args.value)
                if error:
                    return api.respond(False, message=error)
                changed.append("value")

            if not changed:
                return api.respond(False, message="nothing to update")
            out = _save(doc, api, args.output)
            payload = _field_payload(doc, name, _collect(doc)[name])
        return api.respond(True, output=str(out), changed=changed, field=payload)

    def cmd_delete_field(args: argparse.Namespace) -> int:
        with _open(args.input) as doc:
            try:
                entries = find_or_fail(doc, args.name)
            except LookupError as exc:
                return api.respond(False, message=str(exc))
            for page_index, xref in entries:
                page = doc[page_index]
                page.delete_widget(page.load_widget(xref))
            out = _save(doc, api, args.output)
            remaining = len(_collect(doc))
        return api.respond(True, output=str(out), deleted=args.name, widgets=len(entries), remaining=remaining)

    def cmd_fill_fields(args: argparse.Namespace) -> int:
        try:
            values = json.loads(Path(args.json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return api.respond(False, message=f"could not read values JSON: {exc}")
        if not isinstance(values, dict):
            return api.respond(False, message="values JSON must be an object {name: value}")
        with _open(args.input) as doc:
            groups = _collect(doc)
            filled: list[str] = []
            missing: list[str] = []
            invalid: dict[str, str] = {}
            for name, value in values.items():
                if name not in groups:
                    missing.append(name)
                    continue
                error = _set_value(doc, groups[name], value)
                if error:
                    invalid[name] = error
                else:
                    filled.append(name)
            if not filled:
                return api.respond(False, message="no fields were filled", missing=missing, invalid=invalid)
            out = _save(doc, api, args.output)
        return api.respond(True, output=str(out), filled=filled, missing=missing, invalid=invalid)

    def cmd_detect_fields(args: argparse.Namespace) -> int:
        fitz = _fitz()
        if args.apply and not args.output:
            return api.respond(False, message="--apply needs --output")
        with _open(args.input) as doc:
            try:
                pages = api.parse_page_spec(args.pages, doc.page_count)
            except ValueError as exc:
                return api.respond(False, message=str(exc))
            existing: dict[int, list[Any]] = {}
            for page in doc:
                existing[page.number] = [w.rect for w in page.widgets()]
            suggestions: list[dict[str, Any]] = []
            for index in pages:
                page = doc[index]
                # PyMuPDF reports text/drawing geometry on the unrotated page,
                # i.e. already in engine coords (asserted by Tests/qa_forms.py).
                for item in _detect_on_page(page):
                    rect = item["rect"]
                    if any(_overlap_ratio(rect, r) > 0.3 for r in existing[index]):
                        continue
                    hint = item["hint"]
                    suggestions.append({
                        "page": index + 1,
                        "rect": _round_rect(rect),
                        "type": item["type"],
                        "name_hint": hint,
                        "source": item["source"],
                    })
            suggestions.sort(key=lambda s: (s["page"], round(s["rect"][1] / 4), s["rect"][0]))
            if not args.apply:
                return api.respond(True, suggestions=suggestions, count=len(suggestions), pages_scanned=len(pages))
            created: list[str] = []
            for s in suggestions:
                name = s["name_hint"] or None
                info = _add_field(doc, s["page"] - 1, fitz.Rect(s["rect"]), s["type"], name)
                s["name"] = info["name"]
                created.append(info["name"])
            out = _save(doc, api, args.output)
        return api.respond(True, output=str(out), suggestions=suggestions, count=len(suggestions),
                           created=created, pages_scanned=len(pages))

    p = subparsers.add_parser("list-fields", help="List AcroForm fields")
    p.add_argument("--input", required=True)
    p.set_defaults(func=cmd_list_fields)

    p = subparsers.add_parser("add-field", help="Create a form field")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--page", type=int, required=True)
    p.add_argument("--rect", required=True, help="x0,y0,x1,y1 in engine coords")
    p.add_argument("--type", required=True, choices=FIELD_TYPES)
    p.add_argument("--name")
    p.add_argument("--options", help='"a|b|c" for combo/list/radio')
    p.add_argument("--value")
    p.add_argument("--font-size", type=float, default=0.0, help="0 = auto")
    p.add_argument("--required", action="store_true")
    p.add_argument("--readonly", action="store_true")
    p.add_argument("--tooltip")
    p.add_argument("--max-len", type=int, default=0)
    p.add_argument("--group", help="radio group to create or join")
    p.set_defaults(func=cmd_add_field)

    p = subparsers.add_parser("update-field", help="Change a form field")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--new-name")
    p.add_argument("--value")
    p.add_argument("--options")
    p.add_argument("--rect")
    p.add_argument("--font-size", type=float)
    p.add_argument("--required", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--readonly", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--tooltip")
    p.add_argument("--max-len", type=int)
    p.set_defaults(func=cmd_update_field)

    p = subparsers.add_parser("delete-field", help="Remove a form field (all its widgets)")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_delete_field)

    p = subparsers.add_parser("fill-fields", help="Fill field values from a JSON object")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--json", required=True, help="file containing {name: value}")
    p.set_defaults(func=cmd_fill_fields)

    p = subparsers.add_parser("detect-fields", help="Suggest fields from blanks, boxes and lines")
    p.add_argument("--input", required=True)
    p.add_argument("--pages")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--output")
    p.set_defaults(func=cmd_detect_fields)
