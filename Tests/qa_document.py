#!/usr/bin/env python3
"""QA for the Document & Security engine commands (Engine/ext/document.py):
protect / unlock / inspect-security, flatten, get/set-metadata, sanitize,
get/set-outline. Builds its own fixtures under tmp/qa/document/."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
WORK = ROOT / "tmp" / "qa" / "document"


def engine(*args: str, expect_ok: bool = True) -> dict:
    result = subprocess.run(
        [sys.executable, "-W", "ignore", str(ENGINE), *args],
        capture_output=True, text=True,
    )
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"{args[0]}: non-JSON reply\n{result.stdout}\n{result.stderr}")
    if expect_ok:
        assert reply.get("ok"), f"{args[0]} failed: {reply}"
        assert result.returncode == 0
    else:
        assert not reply.get("ok"), f"{args[0]} unexpectedly succeeded: {reply}"
        assert result.returncode != 0
        assert reply.get("message"), reply
        assert "traceback" not in reply, f"{args[0]}: expected a clean error, got {reply}"
    return reply


def p(name: str) -> str:
    return str(WORK / name)


def make_fixtures() -> None:
    import pymupdf

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    # English + Arabic sample report (2 pages, outline) from the shared generator.
    subprocess.run(
        [sys.executable, "-W", "ignore", str(ROOT / "Tests" / "fixtures" / "make_samples.py")],
        check=True, capture_output=True, env={**os.environ, "SAMPLES_DIR": str(WORK)},
    )

    # Rich fixture: annotations, a link, a filled form field, JS, an
    # attachment, XMP, metadata, hidden (render mode 3) text, an outline.
    doc = pymupdf.open()
    for number in range(1, 5):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 80), f"Chapter {number} heading", fontsize=20)
        page.insert_text((72, 120), f"Body text for chapter {number}.", fontsize=12)
    first = doc[0]
    first.insert_text((72, 400), "SECRET OCR LAYER", fontsize=12, render_mode=3)
    first.add_highlight_annot(pymupdf.Rect(72, 62, 260, 86))
    note = first.add_text_annot((400, 300), "Reviewer note")
    note.set_colors(stroke=(1, 0, 0))
    note.update()
    stamp = first.add_freetext_annot(pymupdf.Rect(300, 500, 520, 540), "APPROVED STAMP", fontsize=14,
                                     text_color=(0, 0.4, 0))
    stamp.update()
    first.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 105, 260, 125), "uri": "https://example.com"})
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "customer"
    widget.field_value = "Alice Example"
    widget.rect = pymupdf.Rect(72, 600, 300, 630)
    first.add_widget(widget)
    doc.embfile_add("secret.txt", b"attached secret", filename="secret.txt")
    doc.set_metadata({
        "title": "Rich Fixture", "author": "QA Bot", "subject": "Testing", "keywords": "qa, pdf",
        "creator": "qa_document.py", "producer": "PyMuPDF", "creationDate": "D:20240131120000+02'00'",
    })
    doc.set_xml_metadata('<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
                         'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta><?xpacket end="w"?>')
    doc.set_toc([[1, "Chapter 1", 1], [2, "Section 1.1", 2], [1, "Chapter 3", 3]])
    doc.save(p("rich.pdf"))
    doc.close()

    # Add a JavaScript OpenAction via raw objects.
    doc = pymupdf.open(p("rich.pdf"))
    js = doc.get_new_xref()
    doc.update_object(js, "<</S/JavaScript/JS(app.alert\\('hi'\\);)>>")
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{js} 0 R")
    doc.saveIncr()
    doc.close()


def test_security() -> None:
    import pymupdf

    src = p("report.pdf")
    info = engine("inspect-security", "--input", src)
    assert info["encrypted"] is False and info["needs_pass"] is False and info["method"] is None
    assert "print" in info["permissions"] and len(info["permissions"]) == 8

    # Owner + user password, print/copy only, AES-256.
    engine("protect", "--input", src, "--output", p("locked.pdf"), "--owner-password", "Own3r!pw",
           "--user-password", "openme", "--allow", "print,copy,accessibility", "--encryption", "aes256")
    with pymupdf.open(p("locked.pdf")) as doc:
        assert doc.needs_pass and doc.is_encrypted
        assert doc.authenticate("wrong") == 0
        assert doc.authenticate("openme") and not doc.is_encrypted
        assert "256-bit AES" in (doc.metadata.get("encryption") or "")
        perms = doc.permissions
        assert perms & pymupdf.PDF_PERM_PRINT and perms & pymupdf.PDF_PERM_COPY
        assert not perms & pymupdf.PDF_PERM_MODIFY and not perms & pymupdf.PDF_PERM_ANNOTATE
        assert "Quarterly Report 2026" in doc[0].get_text()

    info = engine("inspect-security", "--input", p("locked.pdf"))
    assert info["encrypted"] and info["needs_pass"] and info["locked"] and info["method"] == "AES-256"
    assert info["permissions"] == []
    info = engine("inspect-security", "--input", p("locked.pdf"), "--password", "openme")
    assert info["authenticated_as"] == "user" and not info["locked"]
    assert set(info["permissions"]) == {"print", "copy", "accessibility"}, info
    assert "modify" in info["restricted"]
    info = engine("inspect-security", "--input", p("locked.pdf"), "--password", "Own3r!pw")
    assert info["authenticated_as"] == "owner"

    # Unlock: wrong password is a clean error, right one yields a plain copy.
    bad = engine("unlock", "--input", p("locked.pdf"), "--output", p("bad.pdf"), "--password", "nope", expect_ok=False)
    assert "incorrect" in bad["message"]
    assert not Path(p("bad.pdf")).exists()
    reply = engine("unlock", "--input", p("locked.pdf"), "--output", p("unlocked.pdf"), "--password", "openme")
    assert reply["removed"] == "AES-256"
    with pymupdf.open(p("unlocked.pdf")) as doc:
        assert not doc.needs_pass and not doc.is_encrypted and doc.metadata.get("encryption") is None
        assert "Quarterly Report 2026" in doc[0].get_text()
        assert doc.get_toc() == [[1, "Quarterly Report", 1], [1, "Appendix A", 2]]

    # Owner-only (opens freely, restrictions apply), AES-128.
    engine("protect", "--input", src, "--output", p("restricted.pdf"), "--owner-password", "boss",
           "--allow", "print", "--encryption", "aes128")
    with pymupdf.open(p("restricted.pdf")) as doc:
        assert not doc.needs_pass
        assert "128-bit AES" in (doc.metadata.get("encryption") or "")
        assert doc.permissions & pymupdf.PDF_PERM_PRINT and not doc.permissions & pymupdf.PDF_PERM_COPY
    info = engine("inspect-security", "--input", p("restricted.pdf"))
    assert info["encrypted"] and not info["needs_pass"] and info["method"] == "AES-128"
    assert info["permissions"] == ["print"], info
    # Lifting restrictions needs the owner password, not an empty one.
    engine("unlock", "--input", p("restricted.pdf"), "--output", p("bad2.pdf"), "--password", "guess", expect_ok=False)
    engine("unlock", "--input", p("restricted.pdf"), "--output", p("free.pdf"), "--password", "boss")
    with pymupdf.open(p("free.pdf")) as doc:
        assert doc.metadata.get("encryption") is None and doc.permissions & pymupdf.PDF_PERM_COPY

    # Validation.
    engine("protect", "--input", src, "--output", p("x.pdf"), "--owner-password", "", expect_ok=False)
    engine("protect", "--input", src, "--output", p("x.pdf"), "--owner-password", "a", "--allow", "print,fly",
           expect_ok=False)
    engine("unlock", "--input", src, "--output", p("x.pdf"), "--password", "a", expect_ok=False)
    engine("flatten", "--input", p("locked.pdf"), "--output", p("x.pdf"), expect_ok=False)
    # Protecting an already-protected file works when its password is given.
    engine("protect", "--input", p("locked.pdf"), "--password", "openme", "--output", p("relocked.pdf"),
           "--owner-password", "new-owner", "--allow", "none")
    info = engine("inspect-security", "--input", p("relocked.pdf"))
    assert info["permissions"] == [] and not info["needs_pass"]
    with pymupdf.open(p("relocked.pdf")) as doc:
        assert "Quarterly Report 2026" in doc[0].get_text(), "re-encrypted copy must keep its content"


def test_flatten() -> None:
    import pymupdf

    src = p("rich.pdf")
    with pymupdf.open(src) as doc:
        before = doc[0].get_pixmap(dpi=50)

    reply = engine("flatten", "--input", src, "--output", p("flat_annots.pdf"), "--what", "annotations")
    assert reply["annotations"] == 3 and reply["fields"] == 0
    with pymupdf.open(p("flat_annots.pdf")) as doc:
        page = doc[0]
        assert list(page.annots()) == []
        assert len(list(page.widgets())) == 1, "form field must survive an annotations-only flatten"
        assert len(page.get_links()) == 1, "links are kept"
        assert "APPROVED STAMP" in page.get_text(), "free-text appearance burned into content"
        after = page.get_pixmap(dpi=50)
        assert after.samples == before.samples or _similar(before, after), "appearance must be kept"

    reply = engine("flatten", "--input", src, "--output", p("flat_forms.pdf"), "--what", "forms")
    assert reply["fields"] == 1
    with pymupdf.open(p("flat_forms.pdf")) as doc:
        assert not doc.is_form_pdf and len(list(doc[0].widgets())) == 0
        assert "Alice Example" in doc[0].get_text()
        assert len(list(doc[0].annots())) == 3

    engine("flatten", "--input", src, "--output", p("flat_all.pdf"), "--what", "all")
    with pymupdf.open(p("flat_all.pdf")) as doc:
        assert not doc.is_form_pdf and list(doc[0].annots()) == []
        text = doc[0].get_text()
        assert "Alice Example" in text and "APPROVED STAMP" in text

    nothing = engine("flatten", "--input", p("report.pdf"), "--output", p("x.pdf"), "--what", "forms", expect_ok=False)
    assert "nothing to flatten" in nothing["message"]


def _similar(a, b) -> bool:
    if (a.width, a.height) != (b.width, b.height):
        return False
    diff = sum(1 for x, y in zip(a.samples, b.samples) if abs(x - y) > 40)
    return diff / len(a.samples) < 0.01


def test_metadata() -> None:
    import pymupdf

    meta = engine("get-metadata", "--input", p("rich.pdf"))
    assert meta["title"] == "Rich Fixture" and meta["author"] == "QA Bot" and meta["keywords"] == "qa, pdf"
    assert meta["pages"] == 4 and meta["file_size"] == os.path.getsize(p("rich.pdf"))
    assert meta["pdf_version"] and meta["has_xmp"] and meta["has_javascript"]
    assert meta["embedded_files"] == 1 and meta["has_forms"] and meta["bookmarks"] == 3
    assert meta["creationDateISO"] == "2024-01-31T12:00:00+02:00", meta["creationDateISO"]
    assert meta["encrypted"] is False and meta["page_width"] == 595

    plain = engine("get-metadata", "--input", p("report.pdf"))
    assert not plain["has_xmp"] and not plain["has_javascript"] and plain["embedded_files"] == 0
    locked = engine("get-metadata", "--input", p("locked.pdf"), expect_ok=False)
    assert "password" in locked["message"]
    assert engine("get-metadata", "--input", p("locked.pdf"), "--password", "openme")["encrypted"]

    engine("set-metadata", "--input", p("rich.pdf"), "--output", p("meta.pdf"),
           "--title", "تقرير ربع سنوي — Q3", "--author", "", "--keywords", "updated")
    with pymupdf.open(p("meta.pdf")) as doc:
        info = doc.metadata
        assert info["title"] == "تقرير ربع سنوي — Q3"
        assert info["author"] == ""
        assert info["keywords"] == "updated"
        assert info["subject"] == "Testing", "untouched fields are kept"
        assert info["creationDate"].startswith("D:20240131"), "creation date is kept"
        assert info["modDate"].startswith("D:")
    again = engine("get-metadata", "--input", p("meta.pdf"))
    assert again["title"] == "تقرير ربع سنوي — Q3"


def test_sanitize() -> None:
    import pymupdf

    engine("sanitize", "--input", p("rich.pdf"), "--output", p("x.pdf"), expect_ok=False)

    reply = engine("sanitize", "--input", p("rich.pdf"), "--output", p("clean.pdf"),
                   "--metadata", "--xmp", "--javascript", "--embedded-files", "--links",
                   "--annotations", "--hidden-text", "--thumbnails")
    removed = reply["removed"]
    assert removed["metadata"] >= 6 and removed["xmp"] == 1 and removed["javascript"] >= 1
    assert removed["embedded_files"] == 1 and removed["links"] == 1 and removed["annotations"] == 3
    assert removed["hidden_text"] >= 1, removed
    assert reply["total"] == sum(removed.values())
    with pymupdf.open(p("clean.pdf")) as doc:
        assert not any(v for k, v in doc.metadata.items() if k not in {"format", "encryption", "creationDate", "modDate"})
        assert doc.embfile_count() == 0
        assert doc.xref_xml_metadata() == 0 or not doc.get_xml_metadata().strip("deleted")
        assert doc[0].get_links() == [] and list(doc[0].annots()) == []
        text = doc[0].get_text()
        assert "SECRET OCR LAYER" not in text
        assert "Chapter 1 heading" in text, "visible text must survive"
        assert len(list(doc[0].widgets())) == 1, "form fields are not touched"
        assert doc.get_toc(), "outline is not touched"
    meta = engine("get-metadata", "--input", p("clean.pdf"))
    assert not meta["has_javascript"] and not meta["has_xmp"] and meta["embedded_files"] == 0

    # Selective: only links; everything else stays.
    reply = engine("sanitize", "--input", p("rich.pdf"), "--output", p("nolinks.pdf"), "--links")
    assert reply["removed"] == {"links": 1}
    with pymupdf.open(p("nolinks.pdf")) as doc:
        assert doc[0].get_links() == [] and len(list(doc[0].annots())) == 3
        assert doc.metadata["title"] == "Rich Fixture" and doc.embfile_count() == 1
        assert "SECRET OCR LAYER" in doc[0].get_text()


def test_outline() -> None:
    import pymupdf

    reply = engine("get-outline", "--input", p("rich.pdf"))
    outline = reply["outline"]
    assert [(e["level"], e["title"], e["page"]) for e in outline] == [
        (1, "Chapter 1", 1), (2, "Section 1.1", 2), (1, "Chapter 3", 3)]
    assert reply["pages"] == 4 and "y" in outline[0]

    new = outline + [{"level": 1, "title": "الفصل الرابع", "page": 4}, {"level": 2, "title": "Deep", "page": 4}]
    new[1]["title"] = "Section 1.1 (renamed)"
    Path(p("outline.json")).write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    assert engine("set-outline", "--input", p("rich.pdf"), "--output", p("outlined.pdf"),
                  "--json", p("outline.json"))["count"] == 5
    with pymupdf.open(p("outlined.pdf")) as doc:
        assert doc.get_toc() == [[1, "Chapter 1", 1], [2, "Section 1.1 (renamed)", 2], [1, "Chapter 3", 3],
                                 [1, "الفصل الرابع", 4], [2, "Deep", 4]]

    # Empty list clears the outline.
    Path(p("empty.json")).write_text("[]")
    engine("set-outline", "--input", p("rich.pdf"), "--output", p("nooutline.pdf"), "--json", p("empty.json"))
    with pymupdf.open(p("nooutline.pdf")) as doc:
        assert doc.get_toc() == []

    bad_cases = [
        [{"level": 2, "title": "starts deep", "page": 1}],
        [{"level": 1, "title": "a", "page": 1}, {"level": 3, "title": "skip", "page": 1}],
        [{"level": 1, "title": "a", "page": 9}],
        [{"level": 1, "title": "  ", "page": 1}],
        [{"level": 1, "title": "a", "page": "x"}],
        {"not": "a list"},
    ]
    for case in bad_cases:
        Path(p("bad.json")).write_text(json.dumps(case))
        engine("set-outline", "--input", p("rich.pdf"), "--output", p("x.pdf"), "--json", p("bad.json"),
               expect_ok=False)
    Path(p("broken.json")).write_text("[{")
    engine("set-outline", "--input", p("rich.pdf"), "--output", p("x.pdf"), "--json", p("broken.json"),
           expect_ok=False)


def main() -> int:
    make_fixtures()
    test_security()
    test_flatten()
    test_metadata()
    test_sanitize()
    test_outline()
    print("document ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
