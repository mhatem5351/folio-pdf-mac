#!/usr/bin/env python3
"""QA for the form-field engine commands (Engine/ext/forms.py).

Builds its own fixtures under tmp/qa/forms/, drives the engine CLI, and
verifies results by reopening outputs with PyMuPDF (and pypdf as a second,
independent AcroForm reader). Run: "$FOLIO_PY" -W ignore Tests/qa_forms.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
OUT = ROOT / "tmp" / "qa" / "forms"


def engine(*args: str, expect_ok: bool = True) -> dict:
    result = subprocess.run([sys.executable, str(ENGINE), *args], capture_output=True, text=True)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"engine {args[0]} printed non-JSON:\n{result.stdout}\n{result.stderr}")
    if expect_ok:
        assert payload.get("ok") and result.returncode == 0, f"{args[0]} failed: {payload}"
    else:
        assert not payload.get("ok") and result.returncode != 0, f"{args[0]} should fail: {payload}"
    return payload


def fields_by_name(path: Path) -> dict[str, dict]:
    payload = engine("list-fields", "--input", str(path))
    return {f["name"]: f for f in payload["fields"]}


def close(a: list[float], b: list[float], tol: float = 0.05) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def ink(page, rect, threshold: int = 140) -> int:
    """Dark pixels inside rect (engine coords == unrotated page coords here)."""
    import pymupdf

    pix = page.get_pixmap(clip=pymupdf.Rect(rect), dpi=144, annots=True)
    samples = pix.samples
    return sum(1 for i in range(0, len(samples), pix.n) if samples[i] < threshold)


def make_blank_form(path: Path) -> None:
    """A paper-style form: underscore blanks, drawn boxes, checkboxes, a
    signature rule, plus decoys (dark header bar, filled note box, table
    rules under text) that must NOT become fields."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    # Decoy: dark title bar with white text.
    page.draw_rect(pymupdf.Rect(0, 0, 612, 48), color=None, fill=(0.12, 0.2, 0.45))
    page.insert_text((40, 32), "Membership Application", fontsize=18, color=(1, 1, 1))

    page.insert_text((60, 110), "Name: ______________________________", fontsize=11)
    page.insert_text((60, 140), "Date: ________________", fontsize=11)

    page.insert_text((60, 178), "Address", fontsize=11)
    page.draw_rect(pymupdf.Rect(130, 166, 420, 184), color=(0, 0, 0), width=0.8)

    for i, label in enumerate(["Email updates", "SMS alerts", "Phone calls"]):
        y = 210 + i * 22
        page.draw_rect(pymupdf.Rect(60, y, 71, y + 11), color=(0, 0, 0), width=0.8)
        page.insert_text((78, y + 9), label, fontsize=10)

    page.insert_text((60, 296), "Comments", fontsize=11)
    page.draw_rect(pymupdf.Rect(60, 302, 420, 380), color=(0.3, 0.3, 0.3), width=0.8)

    # Decoy: a bordered note box that already contains text.
    page.draw_rect(pymupdf.Rect(440, 166, 570, 230), color=(0.4, 0.4, 0.4), width=0.8)
    page.insert_textbox(pymupdf.Rect(446, 170, 566, 226), "Office use only. Do not write here.", fontsize=9)

    # Decoy: table rows with rules under text.
    y = 420
    for row in ["Plan", "Basic", "Premium"]:
        page.insert_text((60, y), row, fontsize=10)
        page.draw_line((60, y + 4), (300, y + 4), color=(0.6, 0.6, 0.6), width=0.5)
        y += 18

    # Signature rule with the label underneath.
    page.draw_line((330, 520), (540, 520), color=(0, 0, 0), width=0.8)
    page.insert_text((330, 534), "Signature", fontsize=9)
    doc.save(str(path))
    doc.close()


SYMBOL_FONT = Path("/System/Library/Fonts/Apple Symbols.ttf")


def make_tricky_form(path: Path) -> None:
    """Rotated page with a comb field (8 touching squares), a Word-style
    underline drawn as a thin filled rect, and ballot-box glyphs."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 100), "Account number", fontsize=10)
    for i in range(8):
        page.draw_rect(pymupdf.Rect(150 + i * 14, 90, 164 + i * 14, 104), color=(0, 0, 0), width=0.6)
    page.insert_text((60, 220), "Phone", fontsize=11)
    page.draw_rect(pymupdf.Rect(100, 221, 300, 221.8), color=None, fill=(0, 0, 0))
    if SYMBOL_FONT.exists():
        page.insert_text((60, 150), "☐ I accept the terms", fontsize=11, fontfile=str(SYMBOL_FONT), fontname="sym")
        page.insert_text((60, 170), "☐ Yes   ☐ No", fontsize=11, fontfile=str(SYMBOL_FONT), fontname="sym")
    page.set_rotation(90)
    doc.save(str(path))
    doc.close()


def main() -> int:
    import pymupdf
    import pypdf

    OUT.mkdir(parents=True, exist_ok=True)
    samples = OUT / "samples"
    env = dict(os.environ, SAMPLES_DIR=str(samples))
    subprocess.run([sys.executable, str(ROOT / "Tests" / "fixtures" / "make_samples.py")], check=True,
                   env=env, capture_output=True, cwd=str(ROOT))
    report = samples / "report.pdf"
    assert report.exists(), "sample generator did not produce report.pdf"

    # ------------------------------------------------------------------ add
    step = OUT / "a0.pdf"
    step.write_bytes(report.read_bytes())
    specs = [
        ("text", "full_name", "60,740,260,760", ["--value", "Jane Doe", "--tooltip", "Your legal name",
                                                 "--required", "--max-len", "40", "--font-size", "11"]),
        ("multiline", "notes", "300,700,545,780", []),
        ("checkbox", "agree", "60,770,72,782", ["--value", "true"]),
        ("combo", "color", "300,660,450,680", ["--options", "Red|Green|Blue", "--value", "Blue"]),
        ("list", "fruit", "460,600,545,660", ["--options", "Apple|Pear|Plum"]),
        ("radio", "plan", "60,700,240,716", ["--options", "Basic|Pro|Team"]),
        ("signature", "sign_here", "60,620,260,650", ["--readonly"]),
        ("button", "submit", "300,620,400,640", ["--value", "Submit"]),
    ]
    for i, (kind, name, rect, extra) in enumerate(specs):
        nxt = OUT / f"a{i + 1}.pdf"
        reply = engine("add-field", "--input", str(step), "--output", str(nxt), "--page", "1",
                       "--rect", rect, "--type", kind, "--name", name, *extra)
        assert reply["name"] == name and reply["renamed"] is False, reply
        step = nxt

    fields = fields_by_name(step)
    assert set(fields) == {s[1] for s in specs}, sorted(fields)
    for kind, name, rect, _ in specs:
        f = fields[name]
        assert f["type"] == kind, (name, f["type"])
        assert f["page"] == 1
        if kind != "radio":
            assert close(f["rect"], [float(v) for v in rect.split(",")]), (name, f["rect"], rect)
    fn = fields["full_name"]
    assert fn["value"] == "Jane Doe" and fn["tooltip"] == "Your legal name"
    assert fn["required"] and not fn["readonly"] and fn["max_len"] == 40 and fn["font_size"] == 11, fn
    assert fields["agree"]["value"] is True
    assert fields["color"]["options"] == ["Red", "Green", "Blue"] and fields["color"]["value"] == "Blue"
    assert fields["fruit"]["options"] == ["Apple", "Pear", "Plum"] and fields["fruit"]["value"] == ""
    assert fields["plan"]["options"] == ["Basic", "Pro", "Team"] and fields["plan"]["value"] == ""
    assert len(fields["plan"]["widgets"]) == 3
    plan_rect = fields["plan"]["rect"]
    assert plan_rect[0] >= 59.9 and plan_rect[2] <= 240.1, plan_rect
    assert fields["sign_here"]["readonly"]
    assert fields["notes"]["type"] == "multiline"

    # Duplicate names are auto-suffixed (and reported).
    dup = OUT / "dup.pdf"
    reply = engine("add-field", "--input", str(step), "--output", str(dup), "--page", "2",
                   "--rect", "60,100,200,120", "--type", "text", "--name", "full_name")
    assert reply["name"] == "full_name_2" and reply["renamed"] is True, reply
    # No name -> sensible default.
    auto = OUT / "auto.pdf"
    reply = engine("add-field", "--input", str(dup), "--output", str(auto), "--page", "2",
                   "--rect", "60,130,72,142", "--type", "checkbox")
    assert reply["name"] == "Checkbox1", reply
    # Joining an existing radio group adds one button with its own export value.
    joined = OUT / "joined.pdf"
    reply = engine("add-field", "--input", str(step), "--output", str(joined), "--page", "1",
                   "--rect", "250,700,266,716", "--type", "radio", "--group", "plan", "--value", "Enterprise")
    assert reply.get("joined_group") and reply["export"] == "Enterprise", reply
    assert fields_by_name(joined)["plan"]["options"] == ["Basic", "Pro", "Team", "Enterprise"]

    # AcroForm hygiene: /DR fonts for /DA, NeedAppearances false, no stale SigFlags.
    with pymupdf.open(str(step)) as doc:
        cat = doc.pdf_catalog()
        assert doc.xref_get_key(cat, "AcroForm/DR/Font/Helv")[0] != "null"
        assert doc.xref_get_key(cat, "AcroForm/DR/Font/ZaDb")[0] != "null"
        assert doc.xref_get_key(cat, "AcroForm/NeedAppearances") == ("bool", "false")
        assert doc.xref_get_key(cat, "AcroForm/SigFlags")[0] == "null"
        for w in doc[0].widgets():
            assert doc.xref_get_key(w.xref, "AP/N")[0] != "null", f"{w.field_name} lacks an appearance stream"
        page = doc[0]
        assert ink(page, fields["full_name"]["rect"]) > 40, "text field value not rendered"
        assert ink(page, fields["agree"]["rect"]) > 10, "checked checkbox not rendered"

    # ---------------------------------------------------------------- update
    up = OUT / "u1.pdf"
    reply = engine("update-field", "--input", str(step), "--output", str(up), "--name", "full_name",
                   "--new-name", "applicant", "--value", "John Smith", "--rect", "70,730,300,752",
                   "--font-size", "14", "--no-required", "--readonly", "--tooltip", "Applicant")
    assert set(reply["changed"]) >= {"name", "value", "rect", "font_size", "required", "readonly", "tooltip"}, reply
    f = fields_by_name(up)
    assert "full_name" not in f and "applicant" in f
    a = f["applicant"]
    assert a["value"] == "John Smith" and close(a["rect"], [70, 730, 300, 752]) and a["font_size"] == 14
    assert not a["required"] and a["readonly"] and a["tooltip"] == "Applicant", a

    up2 = OUT / "u2.pdf"
    engine("update-field", "--input", str(up), "--output", str(up2), "--name", "color",
           "--options", "Cyan|Magenta", "--value", "Magenta")
    engine("update-field", "--input", str(up2), "--output", str(up2.with_name("u3.pdf")), "--name", "plan",
           "--options", "Starter|Growth|Scale", "--rect", "60,690,300,710", "--value", "Growth")
    f = fields_by_name(up2.with_name("u3.pdf"))
    assert f["color"]["options"] == ["Cyan", "Magenta"] and f["color"]["value"] == "Magenta", f["color"]
    assert f["plan"]["options"] == ["Starter", "Growth", "Scale"] and f["plan"]["value"] == "Growth", f["plan"]
    pr = f["plan"]["rect"]
    assert pr[0] == 60 and pr[1] == 690 and 280 <= pr[2] <= 300.1 and pr[3] <= 710.1, pr
    for w in f["plan"]["widgets"]:  # buttons stay square when the group is resized
        x0, y0, x1, y1 = w["rect"]
        assert abs((x1 - x0) - (y1 - y0)) < 0.05, w
    engine("update-field", "--input", str(up2), "--output", str(OUT / "bad.pdf"), "--name", "nope",
           "--value", "x", expect_ok=False)
    engine("update-field", "--input", str(up2), "--output", str(OUT / "bad.pdf"), "--name", "color",
           "--new-name", "plan", expect_ok=False)
    engine("update-field", "--input", str(up2), "--output", str(OUT / "bad.pdf"), "--name", "color",
           "--value", "NotAnOption", expect_ok=False)

    # ------------------------------------------------------------------ fill
    values = OUT / "values.json"
    values.write_text(json.dumps({
        "notes": "مرحبا — Arabic and English notes",
        "agree": False,
        "plan": "Pro",
        "color": "Green",
        "fruit": "Plum",
        "ghost": "nobody",
        "sign_here": "x",
    }, ensure_ascii=False), encoding="utf-8")
    filled = OUT / "filled.pdf"
    reply = engine("fill-fields", "--input", str(step), "--output", str(filled), "--json", str(values))
    assert set(reply["filled"]) == {"notes", "agree", "plan", "color", "fruit"}, reply
    assert reply["missing"] == ["ghost"] and "sign_here" in reply["invalid"], reply
    f = fields_by_name(filled)
    assert f["notes"]["value"].startswith("مرحبا") and f["agree"]["value"] is False
    assert f["plan"]["value"] == "Pro" and f["color"]["value"] == "Green" and f["fruit"]["value"] == "Plum"
    with pymupdf.open(str(filled)) as doc:
        assert ink(doc[0], f["notes"]["rect"]) > 40, "Arabic multiline value not rendered"
        states = {w.on_state(): doc.xref_get_key(w.xref, "AS")[1] for w in doc[0].widgets() if w.field_name == "plan"}
        assert states == {"Basic": "/Off", "Pro": "/Pro", "Team": "/Off"}, states
    # Independent reader (pypdf) agrees on values -> Acrobat/Preview-style readers see them.
    pf = pypdf.PdfReader(str(filled)).get_fields()
    assert pf["plan"].get("/V") == "/Pro" and pf["agree"].get("/V") == "/Off", (pf["plan"], pf["agree"])
    assert pf["color"].get("/V") == "Green" and pf["notes"].get("/V", "").startswith("مرحبا")
    assert pf["full_name"].get("/V") == "Jane Doe"
    engine("fill-fields", "--input", str(step), "--output", str(OUT / "bad.pdf"), "--json", str(OUT / "missing.json"),
           expect_ok=False)

    # ---------------------------------------------------------------- delete
    deleted = OUT / "deleted.pdf"
    reply = engine("delete-field", "--input", str(step), "--output", str(deleted), "--name", "plan")
    assert reply["widgets"] == 3 and reply["remaining"] == len(specs) - 1, reply
    assert "plan" not in fields_by_name(deleted)
    assert "plan" not in (pypdf.PdfReader(str(deleted)).get_fields() or {})
    with pymupdf.open(str(deleted)) as doc:
        assert not [w for w in doc[0].widgets() if w.field_name == "plan"]
    engine("delete-field", "--input", str(step), "--output", str(OUT / "bad.pdf"), "--name", "nope", expect_ok=False)
    engine("add-field", "--input", str(step), "--output", str(OUT / "bad.pdf"), "--page", "9",
           "--rect", "1,1,50,20", "--type", "text", expect_ok=False)

    # ------------------------------------------------ coordinates convention
    rot = OUT / "rotated.pdf"
    with pymupdf.open() as doc:
        p = doc.new_page(width=600, height=800)
        p.set_rotation(90)
        p2 = doc.new_page(width=600, height=800)
        p2.set_cropbox(pymupdf.Rect(50, 100, 550, 700))
        doc.save(str(rot))
    rot_out = OUT / "rotated_out.pdf"
    engine("add-field", "--input", str(rot), "--output", str(rot_out), "--page", "1",
           "--rect", "10,20,110,40", "--type", "text", "--name", "r")
    engine("add-field", "--input", str(rot_out), "--output", str(rot_out.with_name("rot2.pdf")), "--page", "2",
           "--rect", "10,20,110,40", "--type", "text", "--name", "c")
    with pymupdf.open(str(rot_out.with_name("rot2.pdf"))) as doc:
        raw = {w.field_name: doc.xref_get_key(w.xref, "Rect")[1] for page in doc for w in page.widgets()}
    # Engine coords: unrotated, CropBox top-left origin, y down.
    assert raw["r"] == "[10 760 110 780]", raw
    assert raw["c"] == "[60 660 160 680]", raw
    f = fields_by_name(rot_out.with_name("rot2.pdf"))
    assert close(f["r"]["rect"], [10, 20, 110, 40]) and close(f["c"]["rect"], [10, 20, 110, 40]), f

    # ---------------------------------------------------------------- detect
    blank = OUT / "blank_form.pdf"
    make_blank_form(blank)
    reply = engine("detect-fields", "--input", str(blank))
    sugg = reply["suggestions"]
    summary = [(s["type"], s["name_hint"], s["source"]) for s in sugg]
    by_hint = {s["name_hint"]: s for s in sugg}
    for hint, kind in [("Name", "text"), ("Date", "text"), ("Address", "text"), ("Email updates", "checkbox"),
                       ("SMS alerts", "checkbox"), ("Phone calls", "checkbox"), ("Comments", "multiline"),
                       ("Signature", "signature")]:
        assert hint in by_hint and by_hint[hint]["type"] == kind, (hint, kind, summary)
    assert reply["count"] == 8, summary  # no decoys (header bar, note box, table rules)
    name_rect = by_hint["Name"]["rect"]
    assert name_rect[0] > 80 and name_rect[2] > 250 and name_rect[3] - name_rect[1] >= 12, name_rect
    cb = by_hint["SMS alerts"]["rect"]
    assert close(cb, [60, 232, 71, 243], 0.6), cb

    applied = OUT / "blank_applied.pdf"
    reply = engine("detect-fields", "--input", str(blank), "--apply", "--output", str(applied))
    assert len(reply["created"]) == 8, reply
    f = fields_by_name(applied)
    assert {"Name", "Date", "Address", "Email updates", "Comments", "Signature"} <= set(f), sorted(f)
    assert f["Comments"]["type"] == "multiline" and f["Signature"]["type"] == "signature"
    # Re-detecting a prepared form suggests nothing new (existing widgets win).
    again = engine("detect-fields", "--input", str(applied))
    assert again["count"] == 0, again["suggestions"]
    # Page filter and the sample report (prose + table) stay quiet.
    assert engine("detect-fields", "--input", str(blank), "--pages", "1")["count"] == 8
    quiet = engine("detect-fields", "--input", str(report))
    assert quiet["count"] <= 1, quiet["suggestions"]
    engine("detect-fields", "--input", str(blank), "--apply", expect_ok=False)

    # Rotated page: comb boxes merge into one text field, thin-rect rules
    # count as blanks, ballot glyphs become checkboxes; rects in engine coords.
    tricky = OUT / "tricky_form.pdf"
    make_tricky_form(tricky)
    sugg = engine("detect-fields", "--input", str(tricky))["suggestions"]
    by_hint = {s["name_hint"]: s for s in sugg}
    assert by_hint["Account number"]["type"] == "text", sugg
    assert close(by_hint["Account number"]["rect"], [150, 90, 262, 104], 0.6), by_hint["Account number"]
    assert by_hint["Phone"]["type"] == "text" and close(by_hint["Phone"]["rect"][::2], [100, 300], 0.6), by_hint["Phone"]
    assert 219 <= by_hint["Phone"]["rect"][3] <= 222.5, by_hint["Phone"]
    if SYMBOL_FONT.exists():
        assert {"I accept the terms", "Yes", "No"} <= set(by_hint), sugg
        assert all(by_hint[h]["type"] == "checkbox" for h in ("I accept the terms", "Yes", "No"))
        assert len(sugg) == 5, sugg
    else:
        assert len(sugg) == 2, sugg

    print("forms ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
