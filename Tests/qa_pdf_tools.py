#!/usr/bin/env python3
"""Checks for the parity tools: symbols (fill & sign stamps),
compression, page numbers, uniform page resizing, and redline review marks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "Engine" / "pdf_engine.py"


def run_engine(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ENGINE), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    import fitz

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "mixed.pdf"

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)  # Letter
        page.insert_text((72, 100), "Alpha page with reviewable text content", fontsize=13)
        # Big image so compression has something to shrink.
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 900, 900))
        pix.set_rect(pix.irect, (200, 60, 60))
        page.insert_image(fitz.Rect(72, 140, 540, 600), pixmap=pix)
        page2 = doc.new_page(width=595, height=842)  # A4
        page2.insert_text((72, 100), "Beta page", fontsize=13)
        doc.save(str(src))
        doc.close()

        # Symbols
        out = tmp_path / "sym.pdf"
        assert run_engine("add-symbol", "--input", str(src), "--output", str(out), "--page", "1", "--kind", "check", "--x", "300", "--y", "300").get("ok")
        with fitz.open(str(out)) as d:
            fonts = {f[3] for f in d[0].get_fonts()}
        assert any("ZapfDingbats" in (f or "") for f in fonts), f"ZapfDingbats stamp missing: {fonts}"

        # Compression
        out = tmp_path / "small.pdf"
        payload = run_engine("compress", "--input", str(src), "--output", str(out), "--quality", "small")
        assert payload.get("ok") and payload["after_bytes"] <= payload["before_bytes"], f"compress failed: {payload}"

        # Page numbers
        out = tmp_path / "nums.pdf"
        assert run_engine("add-page-numbers", "--input", str(src), "--output", str(out), "--position", "bottom-right", "--number-format", "n-of-total", "--start", "5").get("ok")
        with fitz.open(str(out)) as d:
            assert "5 of 6" in d[0].get_text(), "page number label missing"
            assert "6 of 6" in d[1].get_text(), "second page number missing"

        # Resize to uniform Letter
        out = tmp_path / "resized.pdf"
        assert run_engine("resize-pages", "--input", str(src), "--output", str(out), "--width", "612", "--height", "792").get("ok")
        with fitz.open(str(out)) as d:
            sizes = {(round(p.rect.width), round(p.rect.height)) for p in d}
            assert sizes == {(612, 792)}, f"pages not uniform: {sizes}"
            assert "Beta page" in d[1].get_text(), "content lost during resize"

        # Redline marks
        out = tmp_path / "redline.pdf"
        assert run_engine("redline", "--input", str(src), "--output", str(out), "--page", "1", "--kind", "replace", "--rects", "72,88,300,104", "--note", "tighter wording").get("ok")
        with fitz.open(str(out)) as d:
            kinds = sorted(a.type[1] for a in (d[0].annots() or []))
            assert kinds == ["Caret", "StrikeOut"], f"replace mark annots wrong: {kinds}"
            notes = [a.info.get("content", "") for a in d[0].annots()]
            assert any("tighter wording" in n for n in notes), "replacement note missing"

        out = tmp_path / "squig.pdf"
        assert run_engine("redline", "--input", str(src), "--output", str(out), "--page", "1", "--kind", "squiggly", "--rects", "72,88,300,104").get("ok")
        with fitz.open(str(out)) as d:
            kinds = [a.type[1] for a in (d[0].annots() or [])]
            assert kinds == ["Squiggly"], f"squiggly annot wrong: {kinds}"

        # Block background shading (behind content, separate from text color)
        out = tmp_path / "shaded.pdf"
        assert run_engine("block-background", "--input", str(src), "--output", str(out), "--page", "1", "--rect", "70,86,320,106", "--color", "#fff3b0").get("ok")
        with fitz.open(str(out)) as d:
            assert "Alpha page" in d[0].get_text(), "text lost under background"
            # Sample the padded strip above the text where no glyphs sit.
            pix = d[0].get_pixmap(clip=fitz.Rect(100, 83.5, 106, 85.5))
            r, g, b = pix.pixel(2, 1)
            assert r > 230 and g > 220 and b < 220, f"background shade missing: {(r, g, b)}"

        # Text color AND background applied in ONE block edit must BOTH
        # persist (Sam's report: setting background used to discard the
        # pending text-color change).
        out = tmp_path / "color-and-bg.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", "70,86,320,106", "--text", "Alpha page with reviewable text content",
            "--color", "#c0392b", "--background", "#fff3b0",
            "--original-text", "Alpha page with reviewable text content",
        ).get("ok")
        with fitz.open(str(out)) as d:
            span = next(s for b in d[0].get_text("dict")["blocks"]
                        for l in b.get("lines", []) for s in l.get("spans", [])
                        if "Alpha page" in s["text"])
            assert span["color"] == 0xc0392b, f"text color lost when background set: {hex(span['color'])}"
            pix = d[0].get_pixmap(clip=fitz.Rect(100, 83.5, 106, 85.5))
            r, g, b = pix.pixel(2, 1)
            assert r > 230 and g > 220 and b < 220, f"background lost when color set: {(r, g, b)}"

        # Font-family change alone must apply (was a no-op / reverted).
        out = tmp_path / "font-only.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", "70,86,320,106", "--text", "Alpha page with reviewable text content",
            "--font", "Courier New",
            "--original-text", "Alpha page with reviewable text content",
        ).get("ok")
        with fitz.open(str(out)) as d:
            span = next(s for b in d[0].get_text("dict")["blocks"]
                        for l in b.get("lines", []) for s in l.get("spans", [])
                        if "Alpha page" in s["text"])
            assert "cour" in span["font"].lower(), f"font-only change not applied: {span['font']}"

        # Alignment: center and right shift the text away from the left edge.
        def alpha_span(path: str) -> dict:
            with fitz.open(path) as d:
                return next(
                    s for b in d[0].get_text("dict")["blocks"]
                    for l in b.get("lines", []) for s in l.get("spans", [])
                    if "Alpha page" in s["text"]
                )

        wide_rect = "70,86,540,106"
        out = tmp_path / "align-left.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", wide_rect, "--text", "Alpha page with reviewable text content",
            "--original-text", "Alpha page with reviewable text content",
        ).get("ok")
        left_x0 = alpha_span(str(out))["bbox"][0]

        out = tmp_path / "align-center.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", wide_rect, "--text", "Alpha page with reviewable text content",
            "--align", "center",
            "--original-text", "Alpha page with reviewable text content",
        ).get("ok")
        center_x0 = alpha_span(str(out))["bbox"][0]
        assert center_x0 > left_x0 + 10, f"center did not shift text right: {center_x0} vs {left_x0}"

        out = tmp_path / "align-right.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", wide_rect, "--text", "Alpha page with reviewable text content",
            "--align", "right",
            "--original-text", "Alpha page with reviewable text content",
        ).get("ok")
        right_x0 = alpha_span(str(out))["bbox"][0]
        assert right_x0 > center_x0 + 10, f"right did not shift past center: {right_x0} vs {center_x0}"

        # Italic: the span reports an italic/oblique face.
        out = tmp_path / "italic.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", wide_rect, "--text", "Alpha page with reviewable text content",
            "--italic",
            "--original-text", "Alpha page with reviewable text content",
        ).get("ok")
        italic_font = alpha_span(str(out))["font"].lower()
        assert any(tag in italic_font for tag in ("italic", "oblique", "ital", "heit", "tiit", "coit")), \
            f"italic face not applied: {italic_font}"

        # Underline: text survives and an underline rule is drawn.
        out = tmp_path / "underline.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", wide_rect, "--text", "Alpha page with reviewable text content",
            "--underline",
            "--original-text", "Alpha page with reviewable text content",
        ).get("ok")
        with fitz.open(str(out)) as d:
            assert "Alpha page" in d[0].get_text(), "underline lost the text"
            assert d[0].get_drawings(), "underline rule missing"

        # Any system font family (Georgia is a system TTF → file-search path).
        out = tmp_path / "georgia.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", wide_rect, "--text", "Alpha page with reviewable text content",
            "--font", "Georgia",
            "--original-text", "Alpha page with reviewable text content",
        ).get("ok")
        georgia_font = alpha_span(str(out))["font"].lower()
        assert "georgia" in georgia_font, f"Georgia font not applied: {georgia_font}"

        # Track Changes on block edit: dashed review box with original text
        out = tmp_path / "tracked.pdf"
        assert run_engine(
            "replace-block", "--input", str(src), "--output", str(out), "--page", "1",
            "--rect", "70,86,320,106", "--text", "Alpha page with reworded content",
            "--track-original", "Alpha page with reviewable text content",
        ).get("ok")
        with fitz.open(str(out)) as d:
            marks = [a for a in (d[0].annots() or []) if a.type[1] == "Square"]
            assert marks, "tracked-change box missing"
            assert "reviewable text content" in marks[0].info.get("content", ""), "original text not preserved in note"

        # Selection-level (per-word) text color: coloring ONLY the middle word
        # must recolor just that word — the others stay black (Sam's report:
        # "highlight one or two words ... changes the whole section").
        colortext = "alpha beta gamma"
        runs_src = tmp_path / "runs-src.pdf"
        rdoc = fitz.open()
        rpage = rdoc.new_page(width=612, height=792)
        rpage.insert_text((72, 300), colortext, fontsize=14)
        # Use the drawn block's own bbox as the replace rect.
        rblk = next(
            b for b in rpage.get_text("dict")["blocks"]
            if any(colortext in "".join(s["text"] for s in l["spans"]) for l in b.get("lines", []))
        )
        bx = rblk["bbox"]
        rdoc.save(str(runs_src))
        rdoc.close()

        rect_runs = f"{bx[0] - 1},{bx[1] - 1},{bx[2] + 2},{bx[3] + 2}"
        beta_start = colortext.index("beta")  # 6
        runs_json = json.dumps([{"start": beta_start, "length": len("beta"), "hex": "#d00000"}])
        out = tmp_path / "wordcolor.pdf"
        assert run_engine(
            "replace-block", "--input", str(runs_src), "--output", str(out), "--page", "1",
            "--rect", rect_runs, "--text", colortext, "--original-text", colortext,
            "--color-runs", runs_json,
        ).get("ok")
        with fitz.open(str(out)) as d:
            page = d[0]
            spans = [
                s for b in page.get_text("dict")["blocks"]
                for l in b.get("lines", []) for s in l.get("spans", [])
            ]

            def span_for(word: str) -> dict:
                return next(s for s in spans if word in s["text"])

            # PyMuPDF splits differently-colored text into separate spans.
            beta = span_for("beta")
            assert beta["color"] == 0xD00000, f"beta not recolored red: {hex(beta['color'])}"
            for word in ("alpha", "gamma"):
                s = span_for(word)
                assert s["color"] == 0x000000, f"{word} should stay black: {hex(s['color'])}"

            # Pixel proof: red only under "beta", never under alpha/gamma.
            def red_pixels(bbox) -> int:
                pix = page.get_pixmap(clip=fitz.Rect(bbox), colorspace=fitz.csRGB)
                count = 0
                for yy in range(pix.height):
                    for xx in range(pix.width):
                        r, g, bl = pix.pixel(xx, yy)
                        if r > 150 and g < 90 and bl < 90:
                            count += 1
                return count

            assert red_pixels(beta["bbox"]) > 0, "no red pixels where beta is"
            assert red_pixels(span_for("alpha")["bbox"]) == 0, "red bled into alpha"
            assert red_pixels(span_for("gamma")["bbox"]) == 0, "red bled into gamma"

        # ------------------------------------------------------------------
        # Invoice row: same-baseline COLUMNS must be independent segments.
        # PyMuPDF hands back one block per row, so a description and the
        # amount on its right used to be a single editable paragraph: clicking
        # the amount opened an editor over the description, and committing
        # re-stacked both columns at the row's left edge.
        # ------------------------------------------------------------------
        invoice = tmp_path / "invoice.pdf"
        idoc = fitz.open()
        ipage = idoc.new_page(width=612, height=792)
        helv, hebo = fitz.Font("helv"), fitz.Font("hebo")
        column_right = 540.0
        description = "Work on Project - Progress Payment 2 - still in progress, covering"
        continuation = "+ work through 6/6/2026"

        def right_aligned(text: str, y: float, font, fontname: str, size: float) -> float:
            x = column_right - font.text_length(text, fontsize=size)
            ipage.insert_text((x, y), text, fontname=fontname, fontsize=size)
            return x

        # Header row (letter-spaced, as invoice headers usually are).
        ipage.insert_text((72, 200), "D E S C R I P T I O N", fontname="hebo", fontsize=9)
        header_amount_x = right_aligned("A M O U N T", 200, hebo, "hebo", 9)
        # Body row: wrapped description on the left, amount on the FIRST
        # baseline. Drawn in this order on purpose — it is what makes PyMuPDF
        # hand back all three lines as ONE block, the exact shape that used to
        # break editing (verified: the raw dict block spans x 72 -> 540).
        ipage.insert_text((72, 230), description, fontname="helv", fontsize=10)
        right_aligned("$5,000", 230, helv, "helv", 10)
        ipage.insert_text((72, 244), continuation, fontname="helv", fontsize=10)
        # A plain left-hand paragraph: must NOT be split up by segmentation.
        ipage.insert_textbox(
            fitz.Rect(72, 400, 400, 480),
            "This is a plain wrapped paragraph of ordinary body copy that runs "
            "across several lines without any second column at all.",
            fontname="helv", fontsize=10,
        )
        idoc.save(str(invoice))
        idoc.close()

        blocks = run_engine("page-blocks", "--input", str(invoice), "--page", "1")["blocks"]
        texts = [b["text"] for b in blocks]

        # (a) the amount is its own narrow segment...
        amounts = [b for b in blocks if b["text"] == "$5,000"]
        assert len(amounts) == 1, f"amount is not its own segment: {texts}"
        amount = amounts[0]
        amount_width = amount["rect"][2] - amount["rect"][0]
        assert amount_width < 100, f"amount segment too wide (columns still merged): {amount_width}"
        # ...and the header row is TWO segments, not one merged block.
        header_left = [b for b in blocks if b["text"].replace(" ", "") == "DESCRIPTION"]
        header_right = [b for b in blocks if b["text"].replace(" ", "") == "AMOUNT"]
        assert len(header_left) == 1 and len(header_right) == 1, \
            f"header row did not split into two segments: {texts}"
        assert header_left[0]["rect"][2] < header_right[0]["rect"][0], "header segments overlap"
        # The description keeps BOTH its wrapped lines in one segment.
        body = [b for b in blocks if b["text"] == f"{description}\n{continuation}"]
        assert len(body) == 1, f"wrapped description is not one segment: {texts}"
        assert body[0]["rect"][2] < amount["rect"][0], "description segment ran into the amount"

        # (e) a plain multi-line left paragraph still edits as ONE segment.
        plain = [b for b in blocks if b["text"].startswith("This is a plain wrapped")]
        assert len(plain) == 1, f"plain paragraph over-split into {len(plain)} segments"
        assert plain[0]["text"].count("\n") >= 1, "plain paragraph lost its line structure"

        # (b) clicking the amount selects the amount, not the whole row.
        hit = run_engine(
            "block-at", "--input", str(invoice), "--page", "1",
            "--x", str((amount["rect"][0] + amount["rect"][2]) / 2),
            "--y", str((amount["rect"][1] + amount["rect"][3]) / 2),
        )
        assert hit.get("found") and hit["text"] == "$5,000", f"block-at hit the wrong segment: {hit}"

        # (c) editing the amount keeps its RIGHT edge and leaves the
        # description byte-identical (no clipped or duplicated glyphs).
        before_txt = tmp_path / "invoice-before.txt"
        assert run_engine("export-text", "--input", str(invoice), "--output", str(before_txt)).get("ok")
        out = tmp_path / "invoice-amount.pdf"
        assert run_engine(
            "replace-block", "--input", str(invoice), "--output", str(out), "--page", "1",
            "--rect", ",".join(str(v) for v in amount["rect"]),
            "--text", "$12,345.67", "--original-text", "$5,000",
        ).get("ok")
        with fitz.open(str(out)) as d:
            edited = next(
                s for b in d[0].get_text("dict")["blocks"]
                for l in b.get("lines", []) for s in l.get("spans", [])
                if "12,345" in s["text"]
            )
            drift = abs(edited["bbox"][2] - amount["rect"][2])
            assert drift <= 2.5, f"amount drifted out of its column by {drift}pt"
        after_txt = tmp_path / "invoice-after.txt"
        assert run_engine("export-text", "--input", str(out), "--output", str(after_txt)).get("ok")
        before_lines = before_txt.read_text(encoding="utf-8").splitlines()
        after_lines = after_txt.read_text(encoding="utf-8").splitlines()
        for wanted in (description, continuation):
            assert wanted in before_lines, f"fixture line missing before the edit: {wanted!r}"
            assert wanted in after_lines, \
                f"description text damaged by the amount edit: {[l for l in after_lines if 'covering' in l or '6/6/2026' in l]}"
        assert before_lines.count(description) == after_lines.count(description), \
            "description line duplicated or lost by the amount edit"

        # (d) editing the left header must not move the right header.
        out = tmp_path / "invoice-header.pdf"
        assert run_engine(
            "replace-block", "--input", str(invoice), "--output", str(out), "--page", "1",
            "--rect", ",".join(str(v) for v in header_left[0]["rect"]),
            "--text", "ITEMS", "--original-text", header_left[0]["text"],
        ).get("ok")
        after_blocks = run_engine("page-blocks", "--input", str(out), "--page", "1")["blocks"]
        moved = [b for b in after_blocks if b["text"].replace(" ", "") == "AMOUNT"]
        assert len(moved) == 1, f"AMOUNT header lost by the DESCRIPTION edit: {[b['text'] for b in after_blocks]}"
        shift = abs(moved[0]["rect"][0] - header_amount_x)
        assert shift <= 2, f"AMOUNT header moved {shift}pt when DESCRIPTION was edited"
        assert any(b["text"] == "ITEMS" for b in after_blocks), "replacement header text missing"

        # ------------------------------------------------------------------
        # Resize the editor frame: --render-rect reflows the replacement into
        # the NEW box (erasing still follows --rect). A narrower box wraps to
        # more lines and stays inside itself; a wider box wraps to fewer.
        # ------------------------------------------------------------------
        reflow = tmp_path / "reflow.pdf"
        fdoc = fitz.open()
        fpage = fdoc.new_page(width=612, height=792)
        paragraph = (
            "This paragraph is long enough to demonstrate that narrowing the "
            "editor box forces the replacement text to wrap onto more lines."
        )
        # Left column paragraph...
        fpage.insert_textbox(fitz.Rect(72, 300, 400, 360), paragraph, fontname="helv", fontsize=10)
        # ...and an untouched right column sharing the first baseline.
        fpage.insert_text((470, 308), "KEEP ME", fontname="helv", fontsize=10)
        fdoc.save(str(reflow))
        fdoc.close()

        para_block = next(
            b for b in run_engine("page-blocks", "--input", str(reflow), "--page", "1")["blocks"]
            if b["text"].startswith("This paragraph is long")
        )
        px0, py0, px1, py1 = para_block["rect"]
        base_rect = f"{px0},{py0},{px1},{py1}"

        def reflow_run(name: str, render_rect: str | None) -> tuple[int, tuple[float, float, float, float]]:
            """Replace the paragraph (optionally into `render_rect`) and report
            (line count, bbox of the replacement text)."""
            out_path = tmp_path / name
            call = [
                "replace-block", "--input", str(reflow), "--output", str(out_path),
                "--page", "1", "--rect", base_rect, "--text", paragraph,
                "--original-text", para_block["text"],
            ]
            if render_rect:
                call += ["--render-rect", render_rect]
            assert run_engine(*call).get("ok"), f"{name}: replace-block failed"
            with fitz.open(str(out_path)) as d:
                p = d[0]
                assert "KEEP ME" in p.get_text(), f"{name}: right column destroyed"
                spans = [
                    s for b in p.get_text("dict")["blocks"]
                    for l in b.get("lines", []) for s in l.get("spans", [])
                    if "KEEP ME" not in s["text"] and s["text"].strip()
                ]
                assert spans, f"{name}: replacement text missing"
                lines = {round(s["origin"][1], 1) for s in spans}
                box = (
                    min(s["bbox"][0] for s in spans), min(s["bbox"][1] for s in spans),
                    max(s["bbox"][2] for s in spans), max(s["bbox"][3] for s in spans),
                )
                return len(lines), box

        baseline_lines, _ = reflow_run("reflow-same.pdf", None)

        # HALF the original width: strictly more lines, all inside the box.
        half_x1 = px0 + (px1 - px0) / 2
        narrow = f"{px0},{py0},{half_x1},{py1}"
        narrow_lines, narrow_box = reflow_run("reflow-narrow.pdf", narrow)
        assert narrow_lines > baseline_lines, \
            f"half-width render-rect did not wrap to more lines: {narrow_lines} vs {baseline_lines}"
        assert narrow_box[0] >= px0 - 2 and narrow_box[2] <= half_x1 + 2, \
            f"reflowed text escaped the render-rect: {narrow_box} vs x {px0}-{half_x1}"
        assert narrow_box[1] >= py0 - 4, f"reflowed text started above the render-rect: {narrow_box[1]} vs {py0}"

        # A WIDER box wraps to fewer lines than the narrow one (and than the
        # unresized default, whose width is the original block's).
        wide = f"{px0},{py0},{px0 + 480},{py1}"
        wide_lines, wide_box = reflow_run("reflow-wide.pdf", wide)
        assert wide_lines < narrow_lines, \
            f"wider render-rect did not reduce wrapping: {wide_lines} vs {narrow_lines}"
        assert wide_lines <= baseline_lines, \
            f"wider render-rect wrapped more than the original width: {wide_lines} vs {baseline_lines}"
        assert wide_box[2] <= px0 + 480 + 2, f"wide reflow escaped its render-rect: {wide_box}"

        # No --render-rect => byte-identical to the pre-existing behavior.
        plain_a = tmp_path / "reflow-plain-a.pdf"
        assert run_engine(
            "replace-block", "--input", str(reflow), "--output", str(plain_a), "--page", "1",
            "--rect", base_rect, "--text", paragraph, "--original-text", para_block["text"],
        ).get("ok")
        # ...and passing the SAME rect as --render-rect is treated as no resize.
        plain_b = tmp_path / "reflow-plain-b.pdf"
        assert run_engine(
            "replace-block", "--input", str(reflow), "--output", str(plain_b), "--page", "1",
            "--rect", base_rect, "--text", paragraph, "--original-text", para_block["text"],
            "--render-rect", base_rect,
        ).get("ok")
        with fitz.open(str(plain_a)) as da, fitz.open(str(plain_b)) as db:
            assert da[0].get_text() == db[0].get_text(), "identical render-rect changed the result"

        # Right-anchoring is skipped when an explicit resize is given: the
        # amount follows the render-rect instead of pinning its old right edge.
        anchor_src = tmp_path / "anchor.pdf"
        adoc = fitz.open()
        apage = adoc.new_page(width=612, height=792)
        afont = fitz.Font("helv")
        apage.insert_text((540 - afont.text_length("$5,000", fontsize=10), 400), "$5,000", fontname="helv", fontsize=10)
        adoc.save(str(anchor_src))
        adoc.close()
        amount_block = next(
            b for b in run_engine("page-blocks", "--input", str(anchor_src), "--page", "1")["blocks"]
            if "$5,000" in b["text"]
        )
        ax0, ay0, ax1, ay1 = amount_block["rect"]
        out = tmp_path / "anchor-resized.pdf"
        assert run_engine(
            "replace-block", "--input", str(anchor_src), "--output", str(out), "--page", "1",
            "--rect", f"{ax0},{ay0},{ax1},{ay1}", "--text", "$12,345.67",
            "--original-text", "$5,000",
            "--render-rect", f"{ax0 - 120},{ay0},{ax1},{ay1}",
        ).get("ok")
        with fitz.open(str(out)) as d:
            moved = next(
                s for b in d[0].get_text("dict")["blocks"]
                for l in b.get("lines", []) for s in l.get("spans", [])
                if "12,345" in s["text"]
            )
            assert abs(moved["bbox"][0] - (ax0 - 120)) <= 2.5, \
                f"resize did not override right-anchoring: left edge {moved['bbox'][0]} vs {ax0 - 120}"

        # Password protection
        out = tmp_path / "locked.pdf"
        assert run_engine("set-password", "--input", str(src), "--output", str(out), "--password", "hunter2").get("ok")
        with fitz.open(str(out)) as d:
            assert d.needs_pass, "document is not password protected"
            assert d.authenticate("hunter2"), "correct password rejected"
            assert "Alpha page" in d[0].get_text(), "content unreadable after unlock"

        # Insert new text anywhere: styled single line on a fresh one-page PDF.
        fresh = tmp_path / "fresh.pdf"
        blank = fitz.open()
        blank.new_page(width=612, height=792)
        blank.save(str(fresh))
        blank.close()

        out = tmp_path / "newtext.pdf"
        assert run_engine(
            "add-text", "--input", str(fresh), "--output", str(out),
            "--page", "1", "--x", "100", "--y", "300",
            "--text", "Styled insert", "--font", "Times New Roman",
            "--font-size", "16", "--bold", "--color", "#CC0000",
        ).get("ok")
        with fitz.open(str(out)) as d:
            page = d[0]
            assert "Styled insert" in page.get_text(), "inserted text missing"
            span = None
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for s in line.get("spans", []):
                        if "Styled" in s.get("text", ""):
                            span = s
            assert span is not None, "inserted span not found"
            font = span["font"].lower()
            assert "times" in font or "tibo" in font or "bold" in font, f"unexpected font: {span['font']}"
            color = span["color"]
            cr = ((color >> 16) & 255) / 255.0
            cg = ((color >> 8) & 255) / 255.0
            cb = (color & 255) / 255.0
            assert cr > cg and cr > cb, f"inserted text not red-dominant: {(cr, cg, cb)}"
            # --y is the TOP of the first line: baseline sits y + size*0.83 below,
            # so the span box (whose ascender metric ~= size) tops out near 300.
            assert abs(span["origin"][1] - (300 + 16 * 0.83)) <= 0.5, f"baseline off: {span['origin'][1]}"
            assert abs(span["bbox"][1] - 300) <= 4, f"text top not near 300: {span['bbox'][1]}"

        # Multi-line insert: both lines present, second below the first.
        out = tmp_path / "multiline.pdf"
        assert run_engine(
            "add-text", "--input", str(fresh), "--output", str(out),
            "--page", "1", "--x", "100", "--y", "200",
            "--text", "Line one\nLine two",
        ).get("ok")
        with fitz.open(str(out)) as d:
            page = d[0]
            text = page.get_text()
            assert "Line one" in text and "Line two" in text, "multiline text missing"
            y_first = y_second = None
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for s in line.get("spans", []):
                        if "Line one" in s.get("text", ""):
                            y_first = s["bbox"][1]
                        if "Line two" in s.get("text", ""):
                            y_second = s["bbox"][1]
            assert y_first is not None and y_second is not None, "multiline spans missing"
            assert y_second > y_first, f"second line not below first: {y_first} vs {y_second}"

        # Signature stamping: a transparent PNG placed over page text must keep
        # its alpha — the page/text shows through the see-through areas of the
        # stamp (not a black box), while the underlying text survives.
        from PIL import Image, ImageDraw

        stamp = tmp_path / "stamp.png"
        img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))  # fully transparent
        ImageDraw.Draw(img).line([(0, 0), (39, 39)], fill=(0, 0, 0, 255), width=3)
        img.save(str(stamp))

        signbase = tmp_path / "signbase.pdf"
        b = fitz.open()
        p = b.new_page(width=612, height=792)
        p.insert_text((72, 120), "Underlying page text here", fontsize=13)
        b.save(str(signbase))
        b.close()

        out = tmp_path / "signed.pdf"
        assert run_engine(
            "add-image", "--input", str(signbase), "--output", str(out),
            "--image", str(stamp), "--page", "1",
            "--x", "200", "--y", "200", "--width", "40", "--height", "40",
        ).get("ok")
        with fitz.open(str(out)) as d:
            page = d[0]
            assert "Underlying page text here" in page.get_text(), "page text lost under signature stamp"
            # A transparent corner of the stamp still shows the white page.
            corner = page.get_pixmap(clip=fitz.Rect(236, 201, 240, 205))
            r, g, bl = corner.pixel(1, 1)
            assert r > 240 and g > 240 and bl > 240, f"stamp flattened alpha to a box: {(r, g, bl)}"
            # The diagonal ink actually landed (dark near the center).
            center = page.get_pixmap(clip=fitz.Rect(218, 218, 222, 222))
            rr, gg, bb = center.pixel(1, 1)
            assert rr < 120 and gg < 120 and bb < 120, f"signature ink missing: {(rr, gg, bb)}"

    print("pdf tools ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
