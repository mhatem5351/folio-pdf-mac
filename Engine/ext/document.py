"""Document & security commands: passwords and permissions, flattening,
document properties, hidden-data sanitizing, and the outline (bookmarks).

Commands
  protect           encrypt with owner/user passwords and a permission set
  unlock            write an unencrypted copy (needs a valid password)
  inspect-security  encryption method, password needs, effective permissions
  flatten           burn annotations and/or form fields into page content
  get-metadata      info dictionary + file facts (size, version, XMP, JS, ...)
  set-metadata      update selected info-dictionary fields
  sanitize          remove hidden data (Document.scrub plus annotation removal)
  get-outline       [{level, title, page, x?, y?}]
  set-outline       replace the outline from a JSON list

Pages are 1-based in every JSON payload. Outline destination points (x, y)
are PyMuPDF page coordinates (top-left origin, unrotated page space), the
same "engine coordinates" the editing commands use; they are optional and
only carried through so existing bookmarks keep their scroll target.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# name -> PyMuPDF permission flag attribute
PERMISSIONS: dict[str, str] = {
    "print": "PDF_PERM_PRINT",
    "print-hq": "PDF_PERM_PRINT_HQ",
    "copy": "PDF_PERM_COPY",
    "annotate": "PDF_PERM_ANNOTATE",
    "forms": "PDF_PERM_FORM",
    "assemble": "PDF_PERM_ASSEMBLE",
    "modify": "PDF_PERM_MODIFY",
    "accessibility": "PDF_PERM_ACCESSIBILITY",
}

METADATA_FIELDS = ("title", "author", "subject", "keywords", "creator", "producer")


class DocError(Exception):
    """Bad input that should surface as a clean {ok:false, message} reply."""


# ---------------------------------------------------------------------------
# helpers


def _open(path: str, password: str | None = None) -> Any:
    """Open a PDF, authenticating when it needs a password."""
    import fitz

    if not Path(path).is_file():
        raise DocError(f"file not found: {path}")
    doc = fitz.open(path)
    if not doc.is_pdf:
        doc.close()
        raise DocError("not a PDF document")
    if doc.needs_pass:
        if not password:
            doc.close()
            raise DocError("this PDF is password protected; remove the password first")
        if not doc.authenticate(password):
            doc.close()
            raise DocError("incorrect password")
    return doc


def _encryption_method(doc: Any) -> str | None:
    """Human-readable encryption method read from the trailer's /Encrypt
    dictionary (works even while the document is still locked)."""
    kind, _ = doc.xref_get_key(-1, "Encrypt")
    if kind == "null":
        return None

    def key(name: str) -> str:
        return doc.xref_get_key(-1, f"Encrypt/{name}")[1]

    version = key("V")
    cfm = key("CF/StdCF/CFM")
    length = key("Length")
    if version == "5" or cfm == "/AESV3":
        return "AES-256"
    if cfm == "/AESV2":
        return "AES-128"
    if version in {"2", "3", "4"} or cfm == "/V2":
        bits = length if length.isdigit() else "128"
        return f"RC4-{bits}"
    if version == "1":
        return "RC4-40"
    filt = key("Filter").lstrip("/")
    return filt or "unknown"


def _permission_names(mask: int) -> list[str]:
    import fitz

    return [name for name, attr in PERMISSIONS.items() if mask & int(getattr(fitz, attr))]


def _parse_allow(spec: str | None) -> int:
    import fitz

    if spec is None:
        names = list(PERMISSIONS)
    else:
        names = [part.strip().lower() for part in spec.split(",") if part.strip()]
        if names == ["none"]:
            names = []
    mask = 0
    unknown = [name for name in names if name not in PERMISSIONS]
    if unknown:
        raise DocError(f"unknown permission(s): {', '.join(unknown)}; use {', '.join(PERMISSIONS)}")
    for name in names:
        mask |= int(getattr(fitz, PERMISSIONS[name]))
    return mask


def _pdf_date_to_iso(value: str | None) -> str | None:
    """'D:20240131120000+02'00'' -> '2024-01-31T12:00:00+02:00'."""
    if not value:
        return None
    match = re.match(
        r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*(Z|[+-]\d{2}'?\d{0,2}'?)?",
        value.strip(),
    )
    if not match:
        return None
    year, month, day, hour, minute, second, tz = match.groups()
    try:
        moment = datetime(
            int(year), int(month or 1), int(day or 1),
            int(hour or 0), int(minute or 0), int(second or 0),
        )
    except ValueError:
        return None
    if tz and tz != "Z":
        digits = tz.replace("'", "")
        sign = 1 if digits[0] == "+" else -1
        hours = int(digits[1:3] or 0)
        minutes = int(digits[3:5] or 0)
        moment = moment.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    elif tz == "Z":
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def _pdf_now() -> str:
    now = datetime.now().astimezone()
    offset = now.utcoffset() or timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return now.strftime("D:%Y%m%d%H%M%S") + f"{sign}{total // 60:02d}'{total % 60:02d}'"


def _javascript_count(doc: Any) -> int:
    """Number of JavaScript actions with a non-empty script (scrub() leaves
    neutralised actions behind as /JS ())."""
    count = 0
    for xref in range(1, doc.xref_length()):
        try:
            kind, value = doc.xref_get_key(xref, "JS")
            if kind == "null":
                continue
            if kind == "string" and not value.strip():
                continue
            if kind == "xref" and not doc.xref_stream(int(value.split()[0])).strip():
                continue
            count += 1
        except Exception:
            continue
    # Document-level scripts live in the /Names /JavaScript name tree.
    try:
        if doc.xref_get_key(doc.pdf_catalog(), "Names/JavaScript")[0] != "null" and count == 0:
            count = 1
    except Exception:
        pass
    return count


def _span_count(doc: Any) -> int:
    total = 0
    for page in doc:
        for block in page.get_text("dict").get("blocks", ()):
            for line in block.get("lines", ()):
                total += len(line.get("spans", ()))
    return total


def _save(doc: Any, output: str, api: Any) -> Path:
    out = Path(output)
    api.ensure_parent(out)
    doc.save(str(out), garbage=3, deflate=True)
    return out


# ---------------------------------------------------------------------------
# commands


def register(subparsers: Any, api: Any) -> None:
    def guarded(fn: Any) -> Any:
        def run(args: argparse.Namespace) -> int:
            try:
                return fn(args)
            except DocError as exc:
                return api.respond(False, message=str(exc))
        return run

    # -- protect -----------------------------------------------------------
    def cmd_protect(args: argparse.Namespace) -> int:
        import fitz

        owner = args.owner_password or ""
        user = args.user_password or ""
        if not owner:
            raise DocError("--owner-password cannot be empty")
        mask = _parse_allow(args.allow)
        method = {"aes256": fitz.PDF_ENCRYPT_AES_256, "aes128": fitz.PDF_ENCRYPT_AES_128}[args.encryption]
        warnings: list[str] = []
        if user and user == owner:
            warnings.append("open and permissions passwords are identical, so anyone who can open the file has full rights")
        doc = _open(args.input, args.password)
        try:
            out = Path(args.output)
            api.ensure_parent(out)
            doc.save(
                str(out),
                encryption=method,
                owner_pw=owner,
                user_pw=user or None,
                permissions=mask,
                garbage=3,
                deflate=True,
            )
        finally:
            doc.close()
        return api.respond(
            True,
            output=str(out),
            encryption="AES-256" if args.encryption == "aes256" else "AES-128",
            needs_pass=bool(user),
            permissions=_permission_names(mask),
            warnings=warnings,
        )

    protect = subparsers.add_parser("protect", help="encrypt with passwords and permissions")
    protect.add_argument("--input", required=True)
    protect.add_argument("--output", required=True)
    protect.add_argument("--owner-password", required=True)
    protect.add_argument("--user-password", default=None)
    protect.add_argument("--password", default=None, help="password of an already-protected input")
    protect.add_argument("--allow", default=None,
                         help="comma list of " + ",".join(PERMISSIONS) + "; 'none' allows nothing; omitted allows all")
    protect.add_argument("--encryption", choices=["aes256", "aes128"], default="aes256")
    protect.set_defaults(func=guarded(cmd_protect))

    # -- unlock ------------------------------------------------------------
    def cmd_unlock(args: argparse.Namespace) -> int:
        import fitz

        if not Path(args.input).is_file():
            raise DocError(f"file not found: {args.input}")
        doc = fitz.open(args.input)
        try:
            # Read needs_pass BEFORE authenticating: MuPDF's needs-password
            # check re-tries the empty password, which resets the crypt key —
            # the saved copy would then come out with empty pages.
            needs_pass = bool(doc.needs_pass)
            rc = doc.authenticate(args.password or "")
            method = _encryption_method(doc)
            if method is None:
                raise DocError("this PDF is not password protected")
            if needs_pass and not rc:
                raise DocError("incorrect password")
            if not needs_pass and not rc & 4:
                # Opens without a password: lifting the restrictions takes
                # the permissions (owner) password.
                raise DocError("incorrect permissions password" if args.password else "enter the permissions password")
            role = "owner" if rc & 4 else "user"
            out = Path(args.output)
            api.ensure_parent(out)
            doc.save(str(out), encryption=fitz.PDF_ENCRYPT_NONE, garbage=3, deflate=True)
        finally:
            doc.close()
        return api.respond(True, output=str(out), removed=method, authenticated_as=role)

    unlock = subparsers.add_parser("unlock", help="write an unencrypted copy")
    unlock.add_argument("--input", required=True)
    unlock.add_argument("--output", required=True)
    unlock.add_argument("--password", required=True)
    unlock.set_defaults(func=guarded(cmd_unlock))

    # -- inspect-security --------------------------------------------------
    def cmd_inspect_security(args: argparse.Namespace) -> int:
        import fitz

        if not Path(args.input).is_file():
            raise DocError(f"file not found: {args.input}")
        doc = fitz.open(args.input)
        try:
            method = _encryption_method(doc)
            needs_pass = bool(doc.needs_pass)
            authenticated: str | None = None
            if args.password:
                rc = doc.authenticate(args.password)
                if rc & 4:
                    authenticated = "owner"
                elif rc:
                    authenticated = "user"
                else:
                    authenticated = "none"
            locked = bool(doc.is_encrypted)
            if method is None:
                permissions = list(PERMISSIONS)
            elif locked:
                permissions = []
            else:
                permissions = _permission_names(doc.permissions)
            return api.respond(
                True,
                encrypted=method is not None,
                needs_pass=needs_pass,
                locked=locked,
                method=method,
                permissions=permissions,
                restricted=[name for name in PERMISSIONS if name not in permissions] if method and not locked else [],
                authenticated_as=authenticated,
            )
        finally:
            doc.close()

    inspect = subparsers.add_parser("inspect-security", help="describe encryption and permissions")
    inspect.add_argument("--input", required=True)
    inspect.add_argument("--password", default=None)
    inspect.set_defaults(func=guarded(cmd_inspect_security))

    # -- flatten -----------------------------------------------------------
    def cmd_flatten(args: argparse.Namespace) -> int:
        doc = _open(args.input, args.password)
        try:
            annots = args.what in {"annotations", "all"}
            widgets = args.what in {"forms", "all"}
            annot_count = 0
            widget_count = 0
            for page in doc:
                if annots:
                    # Links are navigation, not markup: bake() leaves them.
                    annot_count += sum(1 for _ in page.annots())
                if widgets:
                    widget_count += sum(1 for _ in page.widgets())
            if annot_count == 0 and widget_count == 0:
                what = {"annotations": "annotations", "forms": "form fields", "all": "annotations or form fields"}[args.what]
                raise DocError(f"nothing to flatten: the document has no {what}")
            doc.bake(annots=annots, widgets=widgets)
            out = _save(doc, args.output, api)
        finally:
            doc.close()
        return api.respond(True, output=str(out), annotations=annot_count, fields=widget_count)

    flatten = subparsers.add_parser("flatten", help="burn annotations/forms into page content")
    flatten.add_argument("--input", required=True)
    flatten.add_argument("--output", required=True)
    flatten.add_argument("--what", choices=["annotations", "forms", "all"], default="all")
    flatten.add_argument("--password", default=None)
    flatten.set_defaults(func=guarded(cmd_flatten))

    # -- get-metadata ------------------------------------------------------
    def cmd_get_metadata(args: argparse.Namespace) -> int:
        doc = _open(args.input, args.password)
        try:
            meta = doc.metadata or {}
            fmt = meta.get("format") or ""
            version = fmt.replace("PDF", "").strip() or None
            info = {field: meta.get(field) or "" for field in METADATA_FIELDS}
            first = doc[0].rect if doc.page_count else None
            annotations = sum(sum(1 for _ in page.annots()) for page in doc)
            payload = dict(
                info,
                creationDate=meta.get("creationDate") or "",
                modDate=meta.get("modDate") or "",
                creationDateISO=_pdf_date_to_iso(meta.get("creationDate")),
                modDateISO=_pdf_date_to_iso(meta.get("modDate")),
                pages=doc.page_count,
                file_size=os.path.getsize(args.input),
                pdf_version=version,
                encrypted=_encryption_method(doc) is not None,
                has_xmp=doc.xref_xml_metadata() > 0,
                has_javascript=_javascript_count(doc) > 0,
                embedded_files=doc.embfile_count(),
                has_forms=bool(doc.is_form_pdf),
                annotations=annotations,
                bookmarks=len(doc.get_toc()),
                page_width=round(first.width, 2) if first else None,
                page_height=round(first.height, 2) if first else None,
            )
        finally:
            doc.close()
        return api.respond(True, **payload)

    get_meta = subparsers.add_parser("get-metadata", help="document properties and file facts")
    get_meta.add_argument("--input", required=True)
    get_meta.add_argument("--password", default=None)
    get_meta.set_defaults(func=guarded(cmd_get_metadata))

    # -- set-metadata ------------------------------------------------------
    def cmd_set_metadata(args: argparse.Namespace) -> int:
        doc = _open(args.input)
        try:
            meta = {key: value for key, value in (doc.metadata or {}).items()
                    if key not in {"format", "encryption"} and value is not None}
            changed: list[str] = []
            for field in METADATA_FIELDS:
                value = getattr(args, field)
                if value is None:
                    continue
                value = value.strip()
                if (meta.get(field) or "") != value:
                    changed.append(field)
                meta[field] = value
            meta["modDate"] = _pdf_now()
            doc.set_metadata(meta)
            out = _save(doc, args.output, api)
        finally:
            doc.close()
        return api.respond(True, output=str(out), changed=changed)

    set_meta = subparsers.add_parser("set-metadata", help="update document properties")
    set_meta.add_argument("--input", required=True)
    set_meta.add_argument("--output", required=True)
    for field in METADATA_FIELDS:
        set_meta.add_argument(f"--{field}", default=None, help=f"new {field}; empty string clears it")
    set_meta.set_defaults(func=guarded(cmd_set_metadata))

    # -- sanitize ----------------------------------------------------------
    def cmd_sanitize(args: argparse.Namespace) -> int:
        selected = [flag for flag in ("metadata", "xmp", "javascript", "embedded_files", "links",
                                       "annotations", "hidden_text", "thumbnails") if getattr(args, flag)]
        if not selected:
            raise DocError("choose at least one kind of hidden data to remove")
        doc = _open(args.input)
        try:
            removed: dict[str, int] = {}
            if args.metadata:
                removed["metadata"] = sum(1 for key, value in (doc.metadata or {}).items()
                                          if key not in {"format", "encryption"} and value)
            if args.xmp:
                removed["xmp"] = 1 if doc.xref_xml_metadata() > 0 else 0
            if args.javascript:
                removed["javascript"] = _javascript_count(doc)
            if args.embedded_files:
                removed["embedded_files"] = doc.embfile_count()
            if args.links:
                removed["links"] = sum(len(page.get_links()) for page in doc)
            if args.thumbnails:
                removed["thumbnails"] = sum(1 for page in doc if doc.xref_get_key(page.xref, "Thumb")[0] != "null")
            spans_before = _span_count(doc) if args.hidden_text else 0

            if args.annotations:
                count = 0
                for page in doc:
                    annot = page.first_annot
                    while annot:
                        nxt = annot.next
                        page.delete_annot(annot)
                        count += 1
                        annot = nxt
                removed["annotations"] = count

            doc.scrub(
                attached_files=args.embedded_files,
                clean_pages=args.hidden_text,
                embedded_files=args.embedded_files,
                hidden_text=args.hidden_text,
                javascript=args.javascript,
                metadata=args.metadata,
                redactions=False,
                remove_links=args.links,
                reset_fields=False,
                reset_responses=False,
                thumbnails=args.thumbnails,
                xml_metadata=args.xmp,
            )
            if args.javascript:
                # Drop the document-level script name tree and a JS
                # OpenAction outright (scrub only blanks the scripts).
                catalog = doc.pdf_catalog()
                if doc.xref_get_key(catalog, "Names/JavaScript")[0] != "null":
                    doc.xref_set_key(catalog, "Names/JavaScript", "null")
                kind, value = doc.xref_get_key(catalog, "OpenAction")
                if kind == "xref" and doc.xref_get_key(int(value.split()[0]), "S")[1] == "/JavaScript":
                    doc.xref_set_key(catalog, "OpenAction", "null")
                elif kind == "dict" and doc.xref_get_key(catalog, "OpenAction/S")[1] == "/JavaScript":
                    doc.xref_set_key(catalog, "OpenAction", "null")
            if args.hidden_text:
                removed["hidden_text"] = max(0, spans_before - _span_count(doc))
            out = _save(doc, args.output, api)
        finally:
            doc.close()
        return api.respond(True, output=str(out), removed=removed, total=sum(removed.values()))

    sanitize = subparsers.add_parser("sanitize", help="remove hidden data")
    sanitize.add_argument("--input", required=True)
    sanitize.add_argument("--output", required=True)
    sanitize.add_argument("--metadata", action="store_true", help="document info (title, author, ...)")
    sanitize.add_argument("--xmp", action="store_true", help="XMP metadata streams")
    sanitize.add_argument("--javascript", action="store_true")
    sanitize.add_argument("--embedded-files", action="store_true", help="attachments and file annotations")
    sanitize.add_argument("--links", action="store_true")
    sanitize.add_argument("--annotations", action="store_true", help="comments, highlights, stamps, ...")
    sanitize.add_argument("--hidden-text", action="store_true", help="invisible text (e.g. OCR layers)")
    sanitize.add_argument("--thumbnails", action="store_true", help="embedded page thumbnails")
    sanitize.set_defaults(func=guarded(cmd_sanitize))

    # -- outline -----------------------------------------------------------
    def cmd_get_outline(args: argparse.Namespace) -> int:
        doc = _open(args.input, args.password)
        try:
            items = []
            for entry in doc.get_toc(simple=False):
                level, title, page = entry[0], entry[1], entry[2]
                item: dict[str, Any] = {"level": int(level), "title": title, "page": int(page)}
                dest = entry[3] if len(entry) > 3 and isinstance(entry[3], dict) else {}
                point = dest.get("to")
                if point is not None and dest.get("kind") == 1:
                    item["x"] = round(float(point.x), 2)
                    item["y"] = round(float(point.y), 2)
                items.append(item)
            pages = doc.page_count
        finally:
            doc.close()
        return api.respond(True, outline=items, pages=pages)

    get_outline = subparsers.add_parser("get-outline", help="read bookmarks")
    get_outline.add_argument("--input", required=True)
    get_outline.add_argument("--password", default=None)
    get_outline.set_defaults(func=guarded(cmd_get_outline))

    def _load_outline(path: str, pages: int) -> list[list[Any]]:
        import fitz

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DocError(f"outline file not found: {path}") from None
        except json.JSONDecodeError as exc:
            raise DocError(f"outline file is not valid JSON: {exc}") from None
        if isinstance(data, dict):
            data = data.get("outline", data.get("items"))
        if not isinstance(data, list):
            raise DocError("outline JSON must be a list of {level, title, page}")
        toc: list[list[Any]] = []
        previous = 0
        for number, raw in enumerate(data, start=1):
            if not isinstance(raw, dict):
                raise DocError(f"entry {number}: expected an object")
            try:
                level = int(raw.get("level", 1))
                page = int(raw.get("page"))
            except (TypeError, ValueError):
                raise DocError(f"entry {number}: level and page must be integers") from None
            title = str(raw.get("title", "")).strip()
            if not title:
                raise DocError(f"entry {number}: title cannot be empty")
            if level < 1:
                raise DocError(f"entry {number} ({title!r}): level must be at least 1")
            if number == 1 and level != 1:
                raise DocError(f"entry 1 ({title!r}): the first bookmark must be at level 1")
            if level > previous + 1:
                raise DocError(f"entry {number} ({title!r}): level {level} skips a level (previous is {previous})")
            if page < 1 or page > pages:
                raise DocError(f"entry {number} ({title!r}): page {page} is outside 1-{pages}")
            entry: list[Any] = [level, title, page]
            if raw.get("y") is not None:
                try:
                    point = fitz.Point(float(raw.get("x") or 0), float(raw["y"]))
                except (TypeError, ValueError):
                    raise DocError(f"entry {number} ({title!r}): x/y must be numbers") from None
                entry.append({"kind": fitz.LINK_GOTO, "page": page - 1, "to": point})
            toc.append(entry)
            previous = level
        return toc

    def cmd_set_outline(args: argparse.Namespace) -> int:
        doc = _open(args.input)
        try:
            toc = _load_outline(args.json, doc.page_count)
            doc.set_toc(toc)
            out = _save(doc, args.output, api)
        finally:
            doc.close()
        return api.respond(True, output=str(out), count=len(toc))

    set_outline = subparsers.add_parser("set-outline", help="replace bookmarks from JSON")
    set_outline.add_argument("--input", required=True)
    set_outline.add_argument("--output", required=True)
    set_outline.add_argument("--json", required=True, help="file with [{level, title, page, x?, y?}]")
    set_outline.set_defaults(func=guarded(cmd_set_outline))
