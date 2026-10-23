#!/usr/bin/env python3
"""QA for Engine/ext/images.py: per-placement image editing.

Builds fixtures under tmp/qa/images/, drives the engine CLI and checks the
results by reopening outputs with PyMuPDF (placements, text, pixels).
Run:  "$PS_PY" -W ignore Tests/qa_images.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "Engine" / "pdf_engine.py"
OUT = ROOT / "tmp" / "qa" / "images"
OUT.mkdir(parents=True, exist_ok=True)
PY = sys.executable


def run(*args: str, expect_ok: bool = True) -> dict:
    proc = subprocess.run([PY, "-W", "ignore", str(ENGINE), *args], capture_output=True, text=True)
    try:
        reply = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"{args[0]}: no JSON reply\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if expect_ok:
        assert reply.get("ok"), f"{args[0]} failed: {reply}"
    else:
        assert not reply.get("ok"), f"{args[0]} should fail: {reply}"
    return reply


def close(a, b, tol=0.6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(img: Image.Image, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95, **kw)
    return buf.getvalue()


def make_image_a() -> Image.Image:
    """Landscape 120x60, red with a green top-left marker quadrant."""
    img = Image.new("RGB", (120, 60), (220, 30, 30))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 29, 14], fill=(20, 200, 40))
    return img


def add_xobject(doc, page, name: str, xref: int) -> None:
    """Add /name -> xref to the page's XObject resources (direct or indirect)."""
    t, v = doc.xref_get_key(page.xref, "Resources")
    if t == "xref":
        holder, prefix = int(v.split()[0]), ""
    else:
        if t == "null":
            doc.xref_set_key(page.xref, "Resources", "<<>>")
        holder, prefix = page.xref, "Resources/"
    xt, xv = doc.xref_get_key(holder, prefix + "XObject")
    if xt == "xref":
        doc.xref_set_key(int(xv.split()[0]), name, f"{xref} 0 R")
        return
    if xt == "null":
        doc.xref_set_key(holder, prefix + "XObject", "<<>>")
    doc.xref_set_key(holder, prefix + "XObject/" + name, f"{xref} 0 R")


def build_fixture() -> Path:
    path = OUT / "fixture.pdf"
    doc = pymupdf.open()
    a_png = png_bytes(make_image_a())
    b_jpg = jpeg_bytes(Image.new("RGB", (80, 80), (30, 60, 230)))
    cmyk_jpg = jpeg_bytes(Image.new("CMYK", (40, 40), (0, 255, 255, 0)))  # pure red in CMYK

    # Page 1: image A twice, JPEG B overlapping the second A, text over both.
    p1 = doc.new_page(width=595, height=842)
    xa = p1.insert_image(pymupdf.Rect(50, 100, 290, 220), stream=a_png)
    p1.insert_image(pymupdf.Rect(300, 100, 540, 220), xref=xa)
    xb = p1.insert_image(pymupdf.Rect(480, 180, 560, 260), stream=b_jpg)
    p1.insert_text((60, 160), "Overlay text on first image", fontsize=14)
    p1.insert_text((310, 160), "Overlay text on second image", fontsize=12)
    p1.insert_text((50, 400), "Body paragraph unaffected by image edits", fontsize=11)

    # Page 2: image A once more, plus text.
    p2 = doc.new_page(width=595, height=842)
    p2.insert_image(pymupdf.Rect(100, 300, 340, 420), xref=xa)
    p2.insert_text((100, 360), "Second page caption over image", fontsize=12)

    # Page 3: a Form XObject drawing A, invoked twice (nested placements).
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text((50, 60), "Nested form page", fontsize=12)
    form = doc.get_new_xref()
    doc.update_object(form, f"<</Type/XObject/Subtype/Form/BBox[0 0 200 100]/Resources<</XObject<</ImA {xa} 0 R>>>>>>")
    doc.update_stream(form, b"q 200 0 0 100 0 0 cm /ImA Do Q")
    add_xobject(doc, p3, "Fm1", form)
    extra = doc.get_new_xref()
    doc.update_object(extra, "<<>>")
    doc.update_stream(extra, b"q 1 0 0 1 50 500 cm /Fm1 Do Q\nq 1 0 0 1 300 500 cm /Fm1 Do Q\n")
    old = p3.get_contents()
    doc.xref_set_key(p3.xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in old + [extra]) + "]")

    # Page 4: rotated page with an offset CropBox; tricky stream syntax
    # (a literal string mentioning "/ImA Do", an inline image whose data
    # contains " EI", a comment) and a 90-degree rotated placement of A.
    p4 = doc.new_page(width=600, height=800)
    p4.insert_text((80, 120), "Rotated page text", fontsize=12)
    add_xobject(doc, p4, "ImA", xa)
    tricky = (
        b"% comment with /ImA Do inside\n"
        b"BT /F0 1 Tf ET\n"
        b"q 1 0 0 1 0 0 cm BT ET Q\n"
        b"q 20 0 0 20 400 600 cm BI /W 4 /H 1 /CS /G /BPC 8 ID \x20EI\x01 EI Q\n"
        b"q 0 -120 60 0 200 500 cm /ImA Do Q\n"   # rotated 90 deg
        b"(fake /ImA Do) pop\n"
    )
    t4 = doc.get_new_xref()
    doc.update_object(t4, "<<>>")
    doc.update_stream(t4, tricky.replace(b"(fake /ImA Do) pop\n", b""))
    c4 = p4.get_contents()
    doc.xref_set_key(p4.xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in c4 + [t4]) + "]")
    p4.set_cropbox(pymupdf.Rect(40, 30, 560, 760))
    p4.set_rotation(90)

    # Pages 5+6 share ONE content stream that draws A.
    p5x = doc.new_page(width=400, height=400).xref
    p6x = doc.new_page(width=400, height=400).xref
    shared = doc.get_new_xref()
    doc.update_object(shared, "<<>>")
    doc.update_stream(shared, b"q 100 0 0 50 50 300 cm /ImA Do Q\n")
    for pgx in (p5x, p6x):
        doc.xref_set_key(pgx, "Resources", f"<</XObject<</ImA {xa} 0 R>>>>")
        doc.xref_set_key(pgx, "Contents", f"{shared} 0 R")

    # Page 7: CMYK JPEG and an Indexed image.
    p7 = doc.new_page(width=400, height=400)
    xc = p7.insert_image(pymupdf.Rect(20, 20, 120, 120), stream=cmyk_jpg)
    idx = doc.get_new_xref()
    doc.update_object(idx, "<</Type/XObject/Subtype/Image/Width 4/Height 2/BitsPerComponent 8"
                           "/ColorSpace[/Indexed/DeviceRGB 1<00FF00FF00FF>]>>")
    doc.update_stream(idx, bytes([0, 1, 0, 1, 1, 0, 1, 0]))
    add_xobject(doc, p7, "ImIdx", idx)
    ti = doc.get_new_xref()
    doc.update_object(ti, "<<>>")
    doc.update_stream(ti, b"q 80 0 0 40 200 200 cm /ImIdx Do Q\n")
    c7 = p7.get_contents()
    doc.xref_set_key(p7.xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in c7 + [ti]) + "]")

    doc.save(str(path), garbage=1, deflate=True)
    doc.close()
    meta = {"xa": xa, "xb": xb, "xc": xc, "idx": idx, "form": form}
    (OUT / "fixture.json").write_text(json.dumps(meta))
    return path


def make_alpha_png() -> Path:
    img = Image.new("RGBA", (60, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, 59, 119], fill=(250, 200, 0, 255))
    p = OUT / "alpha.png"
    img.save(p)
    return p


def make_jpeg() -> Path:
    p = OUT / "replacement.jpg"
    Image.new("RGB", (200, 100), (10, 180, 170)).save(p, "JPEG", quality=95)
    return p


# ---------------------------------------------------------------------------
# Helpers over outputs
# ---------------------------------------------------------------------------


def infos(path: Path, pno: int) -> list[dict]:
    with pymupdf.open(path) as doc:
        return [i for i in doc[pno].get_image_info(xrefs=True) if i["xref"]]


def page_texts(path: Path) -> list[str]:
    with pymupdf.open(path) as doc:
        return [pg.get_text("text") for pg in doc]


def page_words(path: Path) -> list:
    with pymupdf.open(path) as doc:
        return [[tuple(round(v, 1) for v in w[:4]) + (w[4],) for w in pg.get_text("words")] for pg in doc]


def render(path: Path, pno: int) -> Image.Image:
    with pymupdf.open(path) as doc:
        pix = doc[pno].get_pixmap(dpi=72, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def region_equal(a: Image.Image, b: Image.Image, box) -> bool:
    box = tuple(int(round(v)) for v in box)
    return ImageChops.difference(a.crop(box), b.crop(box)).getbbox() is None


def pixel(img: Image.Image, x, y):
    return img.getpixel((int(x), int(y)))


def is_red(px) -> bool:
    return px[0] > 180 and px[1] < 80 and px[2] < 80


def is_white(px) -> bool:
    return all(c > 240 for c in px)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tokenizer_against_pikepdf(src: Path) -> None:
    sys.path.insert(0, str(ROOT / "Engine" / "ext"))
    import images as ext  # noqa: E402

    try:
        import pikepdf
    except ImportError:
        print("  (pikepdf not installed; tokenizer cross-check skipped)")
        return
    with pymupdf.open(src) as doc:
        datas = [b"\n".join(doc.xref_stream(x) for x in pg.get_contents()) for pg in doc]
    for data in datas:
        mine = [op.name.decode() for op in ext.iter_ops(data)]
        with pikepdf.new() as pdf:
            theirs = []
            for item in pikepdf.parse_content_stream(pikepdf.Stream(pdf, data)):
                theirs.append("BI" if isinstance(item, pikepdf.ContentStreamInlineImage) else str(item.operator))
        assert mine == theirs, f"tokenizer mismatch:\n{mine}\n{theirs}"
    print("  tokenizer matches pikepdf on all fixture pages")


def test_list(src: Path, m: dict) -> None:
    rep = run("list-images", "--input", str(src))
    imgs = rep["images"]
    a = [i for i in imgs if i["xref"] == m["xa"]]
    assert len(a) == 8, f"A placements: {len(a)}"  # p1 x2, p2, p3 x2 nested, p4, p5, p6
    assert all(i["occurrences"] == 8 for i in a)
    assert sorted(set(i["page"] for i in a)) == [1, 2, 3, 4, 5, 6]
    assert [i["nested"] for i in a if i["page"] == 3] == [True, True]
    assert not any(i["nested"] for i in a if i["page"] != 3)
    assert rep["inline_images"] == 1, rep["inline_images"]
    first = a[0]
    assert first["width"] == 120 and first["height"] == 60 and first["colorspace"] in ("RGB", "ICC")
    assert first["ext"] == "png" and first["bytes"] > 0
    b = [i for i in imgs if i["xref"] == m["xb"]][0]
    assert b["ext"] == "jpg" and b["filter"] == "DCTDecode"
    idx = [i for i in imgs if i["xref"] == m["idx"]][0]
    assert idx["colorspace"] == "Indexed"
    # bbox/matrix agree with MuPDF's own placement report, every page,
    # including the rotated page with an offset CropBox.
    with pymupdf.open(src) as doc:
        for pno in range(doc.page_count):
            ours = [i for i in imgs if i["page"] == pno + 1]
            mu = [i for i in doc[pno].get_image_info(xrefs=True) if i["xref"]]
            assert len(ours) == len(mu), (pno, len(ours), len(mu))
            for o, u in zip(ours, mu):
                assert o["xref"] == u["xref"], (pno, o["xref"], u["xref"])
                assert close(o["bbox"], u["bbox"]), (pno, o["bbox"], u["bbox"])
                assert close(o["matrix"], u["transform"], 0.01), (pno, o["matrix"], u["transform"])
    rot = [i for i in a if i["page"] == 4][0]
    assert abs(abs(rot["rotation"]) - 90) < 0.01, rot["rotation"]
    one = run("list-images", "--input", str(src), "--page", "1")
    assert one["count"] == 3
    print("  list-images ok (8 placements of A, nested + rotated/cropped bboxes match MuPDF)")


def test_image_at(src: Path, m: dict) -> None:
    hit = run("image-at", "--input", str(src), "--page", "1", "--x", "500", "--y", "200")
    assert hit["found"] and hit["image"]["xref"] == m["xb"], hit  # B painted over A
    hit = run("image-at", "--input", str(src), "--page", "1", "--x", "400", "--y", "120")
    assert hit["found"] and hit["image"]["xref"] == m["xa"] and hit["image"]["placement_index"] == 1
    miss = run("image-at", "--input", str(src), "--page", "1", "--x", "20", "--y", "20")
    assert miss["found"] is False
    near = run("image-at", "--input", str(src), "--page", "1", "--x", "48", "--y", "150", "--tolerance", "4")
    assert near["found"] and near["image"]["placement_index"] == 0
    print("  image-at ok (topmost wins, miss, tolerance)")


def test_transform(src: Path, m: dict) -> None:
    out = OUT / "transform.pdf"
    before = infos(src, 0)
    rep = run("transform-image", "--input", str(src), "--output", str(out), "--page", "1",
              "--xref", str(m["xa"]), "--placement-index", "1", "--rect", "320,500,440,560")
    assert close(rep["bbox"], [320, 500, 440, 560])
    after = infos(out, 0)
    assert close(after[0]["bbox"], before[0]["bbox"]) and after[0]["xref"] == m["xa"]
    assert close(after[1]["bbox"], [320, 500, 440, 560]), after[1]["bbox"]
    assert close(after[2]["bbox"], before[2]["bbox"])  # B untouched
    for pno in (1, 3, 4, 5):
        assert [i["bbox"] for i in infos(out, pno)] == [i["bbox"] for i in infos(src, pno)], pno
    assert page_words(out) == page_words(src)
    r0, r1 = render(src, 0), render(out, 0)
    assert region_equal(r0, r1, (50, 100, 290, 220)), "first placement changed"
    assert region_equal(r0, r1, (40, 380, 400, 420)), "body text changed"
    assert is_red(pixel(r1, 420, 545)) and is_white(pixel(r1, 420, 545 - 60))
    # Old spot: image gone (white) where nothing else is drawn.
    assert is_white(pixel(r1, 400, 200)) and is_red(pixel(r0, 400, 200))
    # The green marker (image top-left) stays top-left -> orientation kept.
    g = pixel(r1, 325, 503)
    assert g[1] > 150 and g[0] < 100, g

    # Rotate 90 deg clockwise around the rect center.
    out2 = OUT / "rotate.pdf"
    run("transform-image", "--input", str(src), "--output", str(out2), "--page", "2",
        "--xref", str(m["xa"]), "--rect", "100,300,340,420", "--rotate", "90")
    info = infos(out2, 1)[0]
    assert close(info["bbox"], [160, 240, 280, 480]), info["bbox"]
    rr = render(out2, 1)
    g = pixel(rr, 276, 245)  # marker: image top-left -> top-right after clockwise turn
    assert g[1] > 150 and g[0] < 100, g
    assert page_words(out2) == page_words(src)

    # Ambiguous placement without index is rejected; bad index too.
    run("transform-image", "--input", str(src), "--output", str(OUT / "x.pdf"), "--page", "1",
        "--xref", str(m["xa"]), "--rect", "0,0,10,10", expect_ok=False)
    run("transform-image", "--input", str(src), "--output", str(OUT / "x.pdf"), "--page", "1",
        "--xref", str(m["xa"]), "--placement-index", "5", "--rect", "0,0,10,10", expect_ok=False)
    print("  transform-image ok (one placement moved/rotated; others, text, pixels intact)")


def test_transform_shared_and_rotated_page(src: Path, m: dict) -> None:
    out = OUT / "shared.pdf"
    run("transform-image", "--input", str(src), "--output", str(out), "--page", "5",
        "--xref", str(m["xa"]), "--rect", "10,10,110,60")
    assert close(infos(out, 4)[0]["bbox"], [10, 10, 110, 60])
    assert infos(out, 5)[0]["bbox"] == infos(src, 5)[0]["bbox"], "page sharing the stream changed"
    # Rotated page + CropBox offset: engine rect round-trips exactly.
    out2 = OUT / "rotpage.pdf"
    before = infos(src, 3)[0]["bbox"]
    target = [before[0] + 15, before[1] + 25, before[2] + 15, before[3] + 25]
    run("transform-image", "--input", str(src), "--output", str(out2), "--page", "4",
        "--xref", str(m["xa"]), "--rect", ",".join(map(str, target)))
    assert close(infos(out2, 3)[0]["bbox"], target), (infos(out2, 3)[0]["bbox"], target)
    assert page_texts(out2) == page_texts(src)
    with pymupdf.open(out2) as doc:
        data = b"".join(doc.xref_stream(x) for x in doc[3].get_contents())
        assert b"% comment with /ImA Do inside" in data and b"\x20EI\x01 EI" in data
    print("  shared content stream + rotated/cropped page ok")


def test_replace(src: Path, m: dict) -> None:
    alpha = make_alpha_png()
    out = OUT / "replace.pdf"
    rep = run("replace-image", "--input", str(src), "--output", str(out), "--page", "1",
              "--xref", str(m["xa"]), "--placement-index", "0", "--image", str(alpha), "--fit", "fit")
    new = rep["xref"]
    # Portrait 60x120 fit into 240x120 -> 60x120 centered at x=170.
    assert close(rep["bbox"], [140, 100, 200, 220]), rep["bbox"]
    after = infos(out, 0)
    assert after[0]["xref"] == new and close(after[0]["bbox"], [140, 100, 200, 220])
    assert after[0]["has-mask"], "alpha channel lost"
    assert after[1]["xref"] == m["xa"] and close(after[1]["bbox"], [300, 100, 540, 220])
    assert infos(out, 1)[0]["xref"] == m["xa"]
    assert [i["xref"] for i in infos(out, 2)] == [m["xa"], m["xa"]]
    assert page_words(out) == page_words(src)
    r0, r1 = render(src, 0), render(out, 0)
    assert region_equal(r0, r1, (300, 100, 470, 175)), "second placement changed"
    px = pixel(r1, 170, 200)
    assert px[0] > 200 and px[1] > 150 and px[2] < 80, px  # yellow ellipse
    assert is_white(pixel(r1, 60, 210)), pixel(r1, 60, 210)  # old image area outside new bbox
    assert is_white(pixel(r1, 142, 104))  # transparent corner shows the white page
    with pymupdf.open(out) as doc:
        assert doc.xref_get_key(new, "SMask")[0] == "xref"

    jpg = make_jpeg()
    out2 = OUT / "replace_fill.pdf"
    rep = run("replace-image", "--input", str(src), "--output", str(out2), "--page", "2",
              "--xref", str(m["xa"]), "--image", str(jpg), "--fit", "fill")
    assert close(rep["bbox"], [100, 300, 340, 420])
    with pymupdf.open(out2) as doc:
        assert doc.xref_get_key(rep["xref"], "Filter")[1] == "/DCTDecode", "JPEG was re-encoded"
    r = render(out2, 1)
    assert is_white(pixel(r, 98, 360)) and is_white(pixel(r, 342, 360)), "fill escaped its clip"
    px = pixel(r, 120, 310)
    assert px[1] > 150 and px[2] > 140 and px[0] < 60, px
    assert page_words(out2) == page_words(src)

    out3 = OUT / "replace_stretch.pdf"
    rep = run("replace-image", "--input", str(src), "--output", str(out3), "--page", "2",
              "--xref", str(m["xa"]), "--image", str(alpha), "--fit", "stretch")
    assert close(rep["bbox"], [100, 300, 340, 420])

    # Rotated placement on the rotated page: replacement drawn upright in
    # the visual frame (quarter-turn of the old data is dropped).
    out4 = OUT / "replace_rot.pdf"
    old = infos(src, 3)[0]
    rep = run("replace-image", "--input", str(src), "--output", str(out4), "--page", "4",
              "--xref", str(m["xa"]), "--image", str(jpg), "--fit", "stretch")
    new_info = infos(out4, 3)[0]
    assert close(new_info["bbox"], old["bbox"]), (new_info["bbox"], old["bbox"])
    t = new_info["transform"]
    assert abs(t[1]) < 1e-3 and abs(t[2]) < 1e-3 and t[0] > 0, t
    run("replace-image", "--input", str(src), "--output", str(OUT / "x.pdf"), "--page", "1",
        "--xref", str(m["xa"]), "--placement-index", "0", "--image", str(OUT / "missing.png"), expect_ok=False)
    print("  replace-image ok (PNG alpha fit, JPEG fill clip, stretch, rotated frame)")


def test_delete(src: Path, m: dict) -> None:
    out = OUT / "delete.pdf"
    rep = run("delete-image", "--input", str(src), "--output", str(out), "--page", "2", "--xref", str(m["xa"]))
    assert rep["remaining_occurrences"] == 7
    assert infos(out, 1) == []
    assert len(infos(out, 0)) == 3 and len(infos(out, 2)) == 2
    assert page_words(out) == page_words(src)
    r = render(out, 1)
    assert is_white(pixel(r, 300, 400))
    # Delete the topmost image (B) on page 1; the A underneath remains.
    out2 = OUT / "delete_b.pdf"
    run("delete-image", "--input", str(src), "--output", str(out2), "--page", "1", "--xref", str(m["xb"]))
    assert [i["xref"] for i in infos(out2, 0)] == [m["xa"], m["xa"]]
    r = render(out2, 0)
    assert is_red(pixel(r, 500, 210))
    assert is_white(pixel(r, 550, 250))
    print("  delete-image ok")


def test_nested(src: Path, m: dict) -> None:
    out = OUT / "nested.pdf"
    before = infos(src, 2)
    rep = run("transform-image", "--input", str(src), "--output", str(out), "--page", "3",
              "--xref", str(m["xa"]), "--placement-index", "0", "--rect", "60,100,160,150")
    assert rep["nested"] and rep["cloned_forms"], rep
    after = infos(out, 2)
    assert close(after[0]["bbox"], [60, 100, 160, 150]), after[0]["bbox"]
    assert close(after[1]["bbox"], before[1]["bbox"]), "other invocation of the form moved"
    assert page_words(out) == page_words(src)
    with pymupdf.open(out) as doc:
        assert doc.xref_stream(m["form"]) == b"q 200 0 0 100 0 0 cm /ImA Do Q", "shared form was modified"
    out2 = OUT / "nested_delete.pdf"
    run("delete-image", "--input", str(src), "--output", str(out2), "--page", "3",
        "--xref", str(m["xa"]), "--placement-index", "1")
    after = infos(out2, 2)
    assert len(after) == 1 and close(after[0]["bbox"], before[0]["bbox"])
    out3 = OUT / "nested_replace.pdf"
    rep = run("replace-image", "--input", str(src), "--output", str(out3), "--page", "3",
              "--xref", str(m["xa"]), "--placement-index", "1", "--image", str(make_jpeg()), "--fit", "fit")
    after = infos(out3, 2)
    assert after[0]["xref"] == m["xa"] and after[1]["xref"] == rep["xref"]
    # Chained edit on the output (form clone now invoked once -> edited in place).
    out4 = OUT / "nested_chain.pdf"
    rep = run("transform-image", "--input", str(out), "--output", str(out4), "--page", "3",
              "--xref", str(m["xa"]), "--placement-index", "0", "--rect", "70,110,170,160")
    assert not rep["cloned_forms"]
    assert close(infos(out4, 2)[0]["bbox"], [70, 110, 170, 160])
    assert close(infos(out4, 2)[1]["bbox"], before[1]["bbox"])
    print("  nested form placements ok (form cloned only when shared)")


def test_extract(src: Path, m: dict) -> None:
    rep = run("extract-image", "--input", str(src), "--xref", str(m["xa"]), "--output", str(OUT / "a.png"))
    assert rep["format"] == "png" and Path(rep["path"]).exists()
    img = Image.open(rep["path"]).convert("RGB")
    assert img.size == (120, 60) and is_red(img.getpixel((100, 40))) and img.getpixel((5, 5))[1] > 150
    rep = run("extract-image", "--input", str(src), "--xref", str(m["xb"]), "--output", str(OUT / "b.png"))
    assert rep["format"] == "jpg" and rep["path"].endswith(".jpg")
    assert Path(rep["path"]).read_bytes()[:2] == b"\xff\xd8"
    rep = run("extract-image", "--input", str(src), "--xref", str(m["xc"]), "--output", str(OUT / "cmyk.jpg"))
    assert rep["format"] == "png" and rep["path"].endswith(".png"), rep
    px = Image.open(rep["path"]).convert("RGB").getpixel((20, 20))
    assert px[0] > 180 and px[1] < 90 and px[2] < 90, f"CMYK not converted: {px}"
    rep = run("extract-image", "--input", str(src), "--xref", str(m["idx"]), "--output", str(OUT / "idx.png"))
    im = Image.open(rep["path"]).convert("RGB")
    assert im.getpixel((0, 0)) == (0, 255, 0) and im.getpixel((1, 0)) == (255, 0, 255), im.getpixel((0, 0))
    # Alpha survives extraction of a replaced PNG.
    rep_r = run("list-images", "--input", str(OUT / "replace.pdf"), "--page", "1")
    new = [i for i in rep_r["images"] if i["has_mask"]][0]["xref"]
    rep = run("extract-image", "--input", str(OUT / "replace.pdf"), "--xref", str(new), "--output", str(OUT / "alpha_out.png"))
    im = Image.open(rep["path"])
    assert im.mode in ("RGBA", "LA") and im.getpixel((0, 0))[-1] == 0 and im.getpixel((30, 60))[-1] == 255
    run("extract-image", "--input", str(src), "--xref", "1", "--output", str(OUT / "bad.png"), expect_ok=False)
    print("  extract-image ok (png, jpg passthrough, CMYK->PNG, indexed->PNG, alpha)")


def test_thumbnails(src: Path, m: dict) -> None:
    tdir = OUT / "thumbs"
    rep = run("image-thumbnails", "--input", str(src), "--page", "1", "--output-dir", str(tdir), "--max", "64")
    assert rep["count"] == 3
    for item in rep["images"]:
        im = Image.open(item["thumbnail"])
        assert max(im.size) == 64 and item["thumb_width"] == im.size[0]
    rep = run("image-thumbnails", "--input", str(src), "--page", "4", "--output-dir", str(tdir), "--max", "64")
    im = Image.open(rep["images"][0]["thumbnail"]).convert("RGB")
    assert im.size[1] > im.size[0], f"rotated placement thumbnail not portrait: {im.size}"
    # Visual orientation: page 4 draws A turned 90 deg clockwise (image top
    # faces right), so the green top-left marker lands at the top-right.
    w, h = im.size
    g = im.getpixel((w - 3, 2))
    assert g[1] > 150 and g[0] < 100, g
    allp = run("image-thumbnails", "--input", str(src), "--output-dir", str(tdir), "--max", "48")
    assert allp["count"] == 11
    print("  image-thumbnails ok")


def main() -> int:
    src = build_fixture()
    m = json.loads((OUT / "fixture.json").read_text())
    test_tokenizer_against_pikepdf(src)
    test_list(src, m)
    test_image_at(src, m)
    test_transform(src, m)
    test_transform_shared_and_rotated_page(src, m)
    test_replace(src, m)
    test_delete(src, m)
    test_nested(src, m)
    test_extract(src, m)
    test_thumbnails(src, m)
    print("images ok")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
