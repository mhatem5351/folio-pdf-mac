#!/usr/bin/env python3
"""QA for the Stamps & Split engine commands (Engine/ext/stamps.py):
watermark, header-footer, bates, split-advanced.

Builds its own fixtures under tmp/qa/stamps/, runs the engine CLI and checks
the outputs by reopening them with PyMuPDF (text, fonts, rendered pixels).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
WORK = ROOT / "tmp" / "qa" / "stamps"


def engine(*args: str, expect_ok: bool = True) -> dict:
    result = subprocess.run(
        [sys.executable, "-W", "ignore", str(ENGINE), *args],
        capture_output=True, text=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"{args[0]}: no JSON reply\nstdout={result.stdout}\nstderr={result.stderr}")
    assert payload.get("ok") is expect_ok, f"{args[0]} ok={payload.get('ok')} expected {expect_ok}: {payload}\n{result.stderr}"
    return payload


def color_centroid(pix, test) -> tuple[float, float, int]:
    """Centroid (x, y) and count of pixels matching test(r, g, b)."""
    samples, n, w = pix.samples, pix.n, pix.width
    xs = ys = count = 0
    for y in range(0, pix.height, 2):
        row = y * w * n
        for x in range(0, w, 2):
            i = row + x * n
            if test(samples[i], samples[i + 1], samples[i + 2]):
                xs += x
                ys += y
                count += 1
    if not count:
        return (-1.0, -1.0, 0)
    return (xs / count, ys / count, count)


def is_red(r, g, b):
    return r > 180 and g < 90 and b < 90


def is_blue(r, g, b):
    return b > 180 and r < 90 and g < 90


def make_fixtures() -> dict[str, Path]:
    import pymupdf

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    # English + Arabic sample report (shared fixture generator).
    env = dict(os.environ, SAMPLES_DIR=str(WORK))
    subprocess.run([sys.executable, "-W", "ignore", str(ROOT / "Tests" / "fixtures" / "make_samples.py")],
                   check=True, cwd=ROOT, env=env, capture_output=True)
    paths["report"] = WORK / "report.pdf"

    # Blank pages: portrait, landscape-by-rotation, offset mediabox.
    doc = pymupdf.open()
    doc.new_page(width=600, height=800)                      # 1: plain
    rotated = doc.new_page(width=600, height=800)             # 2: /Rotate 90
    rotated.set_rotation(90)
    doc.new_page(width=600, height=800)                       # 3: offset mediabox (set below)
    blocker = doc.new_page(width=600, height=800)             # 4: black square at centre
    blocker.draw_rect(pymupdf.Rect(200, 300, 400, 500), color=None, fill=(0, 0, 0))
    doc.new_page(width=600, height=800)                       # 5
    doc.new_page(width=600, height=800)                       # 6
    doc.xref_set_key(doc[2].xref, "MediaBox", "[100 200 700 1000]")
    doc.save(str(WORK / "blank.pdf"))
    doc.close()
    paths["blank"] = WORK / "blank.pdf"

    # Asymmetric image: left half red, right half blue (orientation check).
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100))
    pix.set_rect(pymupdf.IRect(0, 0, 100, 100), (255, 0, 0))
    pix.set_rect(pymupdf.IRect(100, 0, 200, 100), (0, 0, 255))
    pix.save(str(WORK / "halves.png"))
    paths["image"] = WORK / "halves.png"

    # 10 pages with a 3-chapter outline (+ front matter) for splitting.
    doc = pymupdf.open()
    for i in range(10):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Split page {i + 1}", fontsize=18)
    doc.set_toc([
        [1, "Introduction", 2],
        [2, "Background", 3],
        [1, "Methods / Data", 5],
        [1, "الخاتمة", 8],
    ])
    doc.save(str(WORK / "chapters.pdf"))
    doc.close()
    paths["chapters"] = WORK / "chapters.pdf"

    # Image-heavy doc for size split (noise images do not compress).
    doc = pymupdf.open()
    for i in range(6):
        page = doc.new_page(width=595, height=842)
        noise = pymupdf.Pixmap(pymupdf.csRGB, 300, 300, os.urandom(300 * 300 * 3), False)
        page.insert_image(pymupdf.Rect(50, 50, 545, 545), pixmap=noise)
        page.insert_text((72, 700), f"Heavy page {i + 1}", fontsize=14)
    doc.save(str(WORK / "heavy.pdf"), deflate=True)
    doc.close()
    paths["heavy"] = WORK / "heavy.pdf"
    return paths


def check_watermark(p: dict[str, Path]) -> None:
    import pymupdf

    # Text watermark, Latin, centred, translucent over content.
    out = WORK / "wm_text.pdf"
    reply = engine("watermark", "--input", str(p["blank"]), "--output", str(out), "--text", "DRAFT",
                   "--color", "#ff0000", "--opacity", "0.4", "--rotation", "0", "--font-size", "80",
                   "--pages", "1")
    assert reply["stamps"] == 1 and reply["font"] == "helv", reply
    with pymupdf.open(str(out)) as d:
        assert "DRAFT" in d[0].get_text(), "watermark text not extractable"
        assert "DRAFT" not in d[1].get_text(), "--pages 1 leaked onto page 2"
        pix = d[0].get_pixmap(dpi=72)
        # 40% red over white -> pinkish (255, ~153, ~153), never pure red.
        pinks = color_centroid(pix, lambda r, g, b: r > 240 and 120 < g < 190 and 120 < b < 190)
        reds = color_centroid(pix, is_red)
        assert pinks[2] > 50 and reds[2] == 0, f"opacity not applied: pink={pinks} red={reds}"
        assert abs(pinks[0] - 300) < 30 and abs(pinks[1] - 400) < 30, f"not centred: {pinks}"

    # Rotation direction: 45 deg counter-clockwise -> right end higher.
    out = WORK / "wm_img_rot.pdf"
    engine("watermark", "--input", str(p["blank"]), "--output", str(out), "--image", str(p["image"]),
           "--opacity", "1", "--rotation", "45", "--scale", "0.5", "--pages", "1")
    with pymupdf.open(str(out)) as d:
        pix = d[0].get_pixmap(dpi=72)
        red, blue = color_centroid(pix, is_red), color_centroid(pix, is_blue)
        assert red[2] and blue[2], "image watermark missing"
        assert blue[0] > red[0] and blue[1] < red[1], f"rotation should be CCW: red={red} blue={blue}"

    # Image opacity is real transparency (blended with the white page).
    out = WORK / "wm_img_half.pdf"
    engine("watermark", "--input", str(p["blank"]), "--output", str(out), "--image", str(p["image"]),
           "--opacity", "0.5", "--rotation", "0", "--scale", "0.5", "--pages", "1")
    with pymupdf.open(str(out)) as d:
        r, g, b = d[0].get_pixmap(dpi=72).pixel(250, 400)
        assert r > 240 and 100 < g < 160 and 100 < b < 160, f"image opacity not applied: {(r, g, b)}"

    # Upright & placed correctly on a /Rotate 90 page and on an offset MediaBox.
    for page_no in ("2", "3"):
        out = WORK / f"wm_img_p{page_no}.pdf"
        engine("watermark", "--input", str(p["blank"]), "--output", str(out), "--image", str(p["image"]),
               "--opacity", "1", "--rotation", "0", "--scale", "0.4", "--position", "top-left",
               "--pages", page_no)
        with pymupdf.open(str(out)) as d:
            page = d[int(page_no) - 1]
            pix = page.get_pixmap(dpi=72)
            red, blue = color_centroid(pix, is_red), color_centroid(pix, is_blue)
            assert red[2] and blue[2], f"page {page_no}: watermark missing"
            assert blue[0] > red[0] + 40 and abs(blue[1] - red[1]) < 6, f"page {page_no}: not upright red={red} blue={blue}"
            assert red[0] < pix.width * 0.3 and red[1] < pix.height * 0.3, f"page {page_no}: not top-left red={red} size={pix.width}x{pix.height}"

    # Layer: under sits behind page content, over sits on top.
    for layer, expect_black in (("under", True), ("over", False)):
        out = WORK / f"wm_{layer}.pdf"
        engine("watermark", "--input", str(p["blank"]), "--output", str(out), "--image", str(p["image"]),
               "--opacity", "1", "--rotation", "0", "--scale", "0.3", "--layer", layer, "--pages", "4")
        with pymupdf.open(str(out)) as d:
            pix = d[3].get_pixmap(dpi=72)
            r, g, b = pix.pixel(300, 400)
            black = r < 30 and g < 30 and b < 30
            assert black == expect_black, f"layer {layer}: centre pixel {(r, g, b)}"

    # Tile covers the page with many copies.
    out = WORK / "wm_tile.pdf"
    reply = engine("watermark", "--input", str(p["report"]), "--output", str(out), "--text", "SAMPLE",
                   "--position", "tile", "--font-size", "28", "--rotation", "30")
    assert reply["stamps"] >= 2 * 8, f"tile produced too few stamps: {reply}"
    with pymupdf.open(str(out)) as d:
        assert d[0].get_text().count("SAMPLE") >= 8
        assert out.stat().st_size < p["report"].stat().st_size + 60_000, "tiling should reuse one XObject"

    # Arabic text: rendered with an Arabic-capable font and real shaping.
    out = WORK / "wm_arabic.pdf"
    reply = engine("watermark", "--input", str(p["blank"]), "--output", str(out), "--text", "سري للغاية",
                   "--color", "#ff0000", "--opacity", "1", "--rotation", "0", "--pages", "1")
    assert any(k in reply["font"] for k in ("SFArabic", "GeezaPro", "Arial Unicode")), reply
    with pymupdf.open(str(out)) as d:
        fonts = "".join(f[3] for f in d[0].get_fonts(full=True)).replace(" ", "")
        assert any(k in fonts for k in ("SFArabic", "GeezaPro", "ArialUnicode")), f"Arabic font not embedded: {fonts}"
        text = d[0].get_text()
        # Contextual shaping turns base letters into presentation forms
        # (e.g. final teh marbuta U+FE94); unshaped text would keep U+06xx.
        shaped = [ch for ch in text if 0xFE70 <= ord(ch) <= 0xFEFF]
        assert shaped, f"Arabic not shaped: {text!r}"
        pix = d[0].get_pixmap(dpi=72)
        red = color_centroid(pix, is_red)
        assert red[2] > 100 and abs(red[0] - 300) < 40, f"Arabic watermark not rendered centrally: {red}"

    # Bad input is rejected cleanly.
    engine("watermark", "--input", str(p["blank"]), "--output", str(WORK / "x.pdf"), expect_ok=False)
    engine("watermark", "--input", str(p["blank"]), "--output", str(WORK / "x.pdf"), "--text", "A",
           "--opacity", "2", expect_ok=False)
    engine("watermark", "--input", str(p["blank"]), "--output", str(WORK / "x.pdf"), "--text", "A",
           "--position", "middle", expect_ok=False)


def check_header_footer(p: dict[str, Path]) -> None:
    import datetime
    import pymupdf

    out = WORK / "hf.pdf"
    reply = engine("header-footer", "--input", str(p["blank"]), "--output", str(out),
                   "--header-left", "{title}", "--header-right", "{date}",
                   "--footer-center", "Page {page} of {pages}", "--footer-right", "{filename}",
                   "--start-number", "3", "--pages", "2-6", "--title", "Annual Plan",
                   "--filename", "plan.pdf", "--date-format", "%d/%m/%Y")
    assert reply["pages"] == 5 and reply["labels"] == 20, reply
    today = datetime.date.today().strftime("%d/%m/%Y")
    with pymupdf.open(str(out)) as d:
        assert "Page" not in d[0].get_text(), "page 1 should be excluded"
        t2 = d[1].get_text()
        assert "Page 3 of 7" in t2 and "Annual Plan" in t2 and today in t2 and "plan.pdf" in t2, t2
        assert "Page 7 of 7" in d[5].get_text()
        # Header near the top, footer near the bottom (visible coordinates).
        page = d[4]  # plain page 5 = 4th selected page -> number 6
        top = page.search_for("Annual Plan")[0]
        bottom = page.search_for("Page 6 of 7")[0]
        assert top.y0 < 60 and top.x0 < 60, f"header-left misplaced: {top}"
        assert bottom.y1 > page.rect.height - 60 and abs((bottom.x0 + bottom.x1) / 2 - page.rect.width / 2) < 3, f"footer-center misplaced: {bottom}"
        # Rotated page 2 (displays landscape): footer ink at the visible
        # bottom centre, header ink at the visible top corners.
        pix = d[1].get_pixmap(dpi=72)
        assert pix.width > pix.height, "fixture page 2 should display landscape"
        dark = lambda r, g, b: r < 120 and g < 120 and b < 120  # noqa: E731
        band = pymupdf.Rect(pix.width * 0.3, pix.height - 60, pix.width * 0.7, pix.height)
        ink_bottom = sum(1 for yy in range(int(band.y0), int(band.y1), 2) for xx in range(int(band.x0), int(band.x1), 2)
                         if dark(*pix.pixel(xx, yy)))
        assert ink_bottom > 10, "rotated page: footer not at the visible bottom"

    # Arabic header uses an Arabic font.
    out = WORK / "hf_ar.pdf"
    engine("header-footer", "--input", str(p["report"]), "--output", str(out),
           "--header-center", "تقرير سري — {page}")
    with pymupdf.open(str(out)) as d:
        fonts = "".join(f[3] for f in d[0].get_fonts(full=True)).replace(" ", "")
        assert any(k in fonts for k in ("SFArabic", "GeezaPro", "ArialUnicode")), fonts

    engine("header-footer", "--input", str(p["blank"]), "--output", str(WORK / "x.pdf"), expect_ok=False)


def check_bates(p: dict[str, Path]) -> None:
    import pymupdf

    out = WORK / "bates.pdf"
    reply = engine("bates", "--input", str(p["blank"]), "--output", str(out), "--prefix", "ABC",
                   "--suffix=-X", "--start", "41", "--digits", "6", "--position", "bottom-right")
    assert reply["first"] == "ABC000041-X" and reply["last"] == "ABC000046-X", reply
    assert reply["next_number"] == 47, reply
    with pymupdf.open(str(out)) as d:
        for i, page in enumerate(d):
            ident = f"ABC{41 + i:06d}-X"
            hits = page.search_for(ident)
            assert hits, f"page {i + 1}: {ident} missing"
            if page.rotation == 0:
                assert hits[0].x1 > page.rect.width - 60 and hits[0].y1 > page.rect.height - 60, f"not bottom-right: {hits[0]}"

    out = WORK / "bates_sel.pdf"
    reply = engine("bates", "--input", str(p["report"]), "--output", str(out), "--prefix", "DOC-",
                   "--start", "1", "--digits", "4", "--pages", "2", "--position", "top-left")
    assert reply["first"] == reply["last"] == "DOC-0001", reply
    with pymupdf.open(str(out)) as d:
        assert "DOC-0001" not in d[0].get_text() and "DOC-0001" in d[1].get_text()

    engine("bates", "--input", str(p["blank"]), "--output", str(WORK / "x.pdf"), "--position", "center",
           expect_ok=False)


def check_split(p: dict[str, Path]) -> None:
    import pymupdf

    def pages_of(path: str) -> list[str]:
        with pymupdf.open(path) as d:
            return [page.get_text().strip() for page in d]

    # ranges, including an open-ended range.
    out_dir = WORK / "split_ranges"
    reply = engine("split-advanced", "--input", str(p["chapters"]), "--output-dir", str(out_dir),
                   "--mode", "ranges", "--ranges", "1-3, 4-6,9-", "--basename", "My Report")
    parts = reply["outputs"]
    assert [o["pages"] for o in parts] == [3, 3, 2], reply
    assert Path(parts[0]["path"]).name == "My-Report_part-01.pdf", parts[0]
    assert pages_of(parts[2]["path"]) == ["Split page 9", "Split page 10"]

    # every N
    out_dir = WORK / "split_every"
    reply = engine("split-advanced", "--input", str(p["chapters"]), "--output-dir", str(out_dir),
                   "--mode", "every", "--every", "4")
    assert [o["pages"] for o in reply["outputs"]] == [4, 4, 2], reply
    assert Path(reply["outputs"][1]["path"]).name == "chapters_part-02.pdf"

    # bookmarks: front matter + 3 top-level chapters, named after them.
    out_dir = WORK / "split_bookmarks"
    dry = engine("split-advanced", "--input", str(p["chapters"]), "--output-dir", str(out_dir),
                 "--mode", "bookmarks", "--dry-run")
    assert dry["parts"] == 4 and not out_dir.exists(), dry
    reply = engine("split-advanced", "--input", str(p["chapters"]), "--output-dir", str(out_dir),
                   "--mode", "bookmarks")
    names = [Path(o["path"]).name for o in reply["outputs"]]
    assert names == ["chapters_01-Front-matter.pdf", "chapters_02-Introduction.pdf",
                     "chapters_03-Methods-Data.pdf", "chapters_04-الخاتمة.pdf"], names
    assert [(o["first"], o["last"]) for o in reply["outputs"]] == [(1, 1), (2, 4), (5, 7), (8, 10)]
    with pymupdf.open(reply["outputs"][1]["path"]) as d:
        assert d.get_toc() == [[1, "Introduction", 1], [2, "Background", 2]], d.get_toc()
    engine("split-advanced", "--input", str(p["blank"]), "--output-dir", str(WORK / "nobm"),
           "--mode", "bookmarks", expect_ok=False)

    # size: every part under the limit, all pages accounted for, in order.
    out_dir = WORK / "split_size"
    limit_mb = 0.6
    reply = engine("split-advanced", "--input", str(p["heavy"]), "--output-dir", str(out_dir),
                   "--mode", "size", "--max-mb", str(limit_mb))
    parts = reply["outputs"]
    assert len(parts) >= 2, reply
    assert sum(o["pages"] for o in parts) == 6
    for o in parts:
        assert Path(o["path"]).stat().st_size <= limit_mb * 1024 * 1024 or o["pages"] == 1, o
    order = [t for o in parts for t in pages_of(o["path"])]
    assert order == [f"Heavy page {i}" for i in range(1, 7)], order

    engine("split-advanced", "--input", str(p["chapters"]), "--output-dir", str(WORK / "bad"),
           "--mode", "ranges", "--ranges", "1-30", expect_ok=False)


def main() -> int:
    paths = make_fixtures()
    check_watermark(paths)
    check_header_footer(paths)
    check_bates(paths)
    check_split(paths)
    print("stamps ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
