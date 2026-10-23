"""Generate the demo documents used for the README screenshots.

    "$FOLIO_PY" docs/screenshots/make_demo_docs.py <out-dir>

Writes lumen-annual-report.pdf (an English report page, an Arabic summary
page written through the engine's own RTL text path, a fill-in form and a
"scanned" image-only page) plus the chart image it embeds. Uses macOS system
fonts, so the PDFs are generated locally rather than committed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "Engine" / "pdf_engine.py"
SUPP = Path("/System/Library/Fonts/Supplemental")
GEORGIA, GEORGIA_BOLD = SUPP / "Georgia.ttf", SUPP / "Georgia Bold.ttf"
SANS = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
SANS_BOLD = Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")

INDIGO = (0.31, 0.27, 0.90)
INK = (0.11, 0.12, 0.16)
MUTED = (0.42, 0.45, 0.52)
SOFT = (0.95, 0.95, 1.0)


def engine(*args: str) -> dict:
    out = subprocess.run([sys.executable, "-W", "ignore", str(ENGINE), *args], capture_output=True, text=True)
    reply = json.loads(out.stdout)
    if not reply.get("ok", False):
        raise SystemExit(f"engine {args[0]} failed: {reply}")
    return reply


def chart(path: Path) -> None:
    w, h, s = 900, 420, 2
    img = Image.new("RGB", (w * s, h * s), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(SANS), 22 * s)
    small = ImageFont.truetype(str(SANS), 18 * s)
    bold = ImageFont.truetype(str(SANS_BOLD), 24 * s)
    d.text((20 * s, 10 * s), "Revenue by quarter (USD, millions)", font=bold, fill=(28, 30, 40))
    values = [(f"Q{i + 1}", v) for i, v in enumerate([0.62, 0.81, 1.08, 1.69])]
    base, top, left = 370 * s, 70 * s, 60 * s
    for gy in range(5):
        y = base - gy * (base - top) // 4
        d.line([(left, y), (w * s - 20 * s, y)], fill=(232, 234, 240), width=2 * s)
        d.text((10 * s, y - 12 * s), f"{gy * 0.5:.1f}", font=small, fill=(130, 134, 150))
    bw = 120 * s
    for i, (label, value) in enumerate(values):
        x = left + 50 * s + i * 200 * s
        y = base - int(value / 2.0 * (base - top))
        colors = [(165, 160, 255), (130, 120, 250), (99, 86, 240), (79, 70, 229)]
        d.rounded_rectangle([x, y, x + bw, base], radius=14 * s, fill=colors[i])
        d.text((x + bw // 2, y - 30 * s), f"{value:.2f}", font=font, fill=(40, 42, 60), anchor="mm")
        d.text((x + bw // 2, base + 24 * s), label, font=small, fill=(110, 114, 130), anchor="mm")
    img.resize((w, h), Image.LANCZOS).save(path)


def report_page(doc: fitz.Document, chart_png: Path) -> None:
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(0, 0, 595, 8), color=None, fill=INDIGO)
    page.insert_text((56, 58), "LUMEN LABS  ·  ANNUAL REPORT 2026", fontsize=8.5, fontfile=str(SANS_BOLD), fontname="ArialBold", color=MUTED)
    page.insert_text((56, 100), "A year of steady growth", fontsize=28, fontfile=str(GEORGIA_BOLD), fontname="GeorgiaBold", color=INK)
    page.insert_text((56, 124), "How a small team tripled revenue while staying profitable", fontsize=12.5, fontfile=str(GEORGIA), fontname="Georgia", color=MUTED)
    body = (
        "Lumen Labs closed 2026 with record results. Revenue grew to 4.2 million dollars, "
        "driven by the launch of our document platform and a 38 percent increase in returning "
        "customers. We hired twelve people, opened an office in Cairo and kept operating costs flat."
    )
    body2 = (
        "Next year we will focus on three priorities: faster onboarding for new teams, "
        "first-class Arabic and right-to-left support, and an open-source desktop editor "
        "that anyone can download for free."
    )
    page.insert_textbox(fitz.Rect(56, 146, 539, 214), body, fontsize=11, fontfile=str(SANS), fontname="Arial", color=INK, lineheight=1.45)
    page.insert_textbox(fitz.Rect(56, 222, 539, 274), body2, fontsize=11, fontfile=str(SANS), fontname="Arial", color=INK, lineheight=1.45)
    cards = [("$4.2M", "Revenue"), ("+38%", "Returning customers"), ("12k", "Active users")]
    for i, (big, label) in enumerate(cards):
        r = fitz.Rect(56 + i * 165, 290, 56 + i * 165 + 152, 360)
        page.draw_rect(r, color=None, fill=SOFT, radius=0.12)
        page.insert_text((r.x0 + 14, r.y0 + 34), big, fontsize=22, fontfile=str(GEORGIA_BOLD), fontname="GeorgiaBold", color=INDIGO)
        page.insert_text((r.x0 + 14, r.y0 + 54), label, fontsize=9.5, fontfile=str(SANS), fontname="Arial", color=MUTED)
    page.insert_image(fitz.Rect(56, 380, 539, 605), filename=str(chart_png))
    page.insert_text((56, 640), "Highlights", fontsize=15, fontfile=str(GEORGIA_BOLD), fontname="GeorgiaBold", color=INK)
    bullets = [
        "Launched PDF tools used by 12,000 people every month",
        "Signed partnerships with three regional banks",
        "Reached profitability in the second quarter",
    ]
    for i, line in enumerate(bullets):
        y = 664 + i * 22
        page.draw_circle((62, y - 3.5), 2.6, color=None, fill=INDIGO)
        page.insert_text((74, y), line, fontsize=11, fontfile=str(SANS), fontname="Arial", color=INK)
    page.draw_line((56, 792), (539, 792), color=(0.88, 0.89, 0.93), width=0.6)
    page.insert_text((56, 808), "lumenlabs.example  ·  Confidential draft", fontsize=8, fontfile=str(SANS), fontname="Arial", color=MUTED)
    page.insert_text((520, 808), "1", fontsize=8, fontfile=str(SANS), fontname="Arial", color=MUTED)


def form_page(doc: fitz.Document) -> None:
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(0, 0, 595, 8), color=None, fill=INDIGO)
    page.insert_text((56, 70), "Client onboarding form", fontsize=24, fontfile=str(GEORGIA_BOLD), fontname="GeorgiaBold", color=INK)
    page.insert_text((56, 94), "Please complete every field and sign at the bottom.", fontsize=11, fontfile=str(SANS), fontname="Arial", color=MUTED)
    rows = ["Full name:", "Company:", "Email:", "Phone:", "Start date:"]
    for i, label in enumerate(rows):
        y = 150 + i * 40
        page.insert_text((56, y), label, fontsize=11.5, fontfile=str(SANS_BOLD), fontname="ArialBold", color=INK)
        page.insert_text((150, y), "_" * 52, fontsize=11.5, fontfile=str(SANS), fontname="Arial", color=MUTED)
    page.insert_text((56, 370), "Services required", fontsize=14, fontfile=str(GEORGIA_BOLD), fontname="GeorgiaBold", color=INK)
    for i, label in enumerate(["Document editing", "OCR and digitization", "Arabic translation", "Printing"]):
        y = 400 + i * 28
        page.draw_rect(fitz.Rect(56, y - 10, 68, y + 2), color=MUTED, width=0.9)
        page.insert_text((78, y), label, fontsize=11.5, fontfile=str(SANS), fontname="Arial", color=INK)
    page.insert_text((56, 550), "Notes:", fontsize=11.5, fontfile=str(SANS_BOLD), fontname="ArialBold", color=INK)
    page.draw_rect(fitz.Rect(56, 560, 539, 650), color=(0.8, 0.82, 0.88), width=0.8)
    page.insert_text((56, 720), "Signature:", fontsize=11.5, fontfile=str(SANS_BOLD), fontname="ArialBold", color=INK)
    page.insert_text((140, 720), "_" * 30, fontsize=11.5, fontfile=str(SANS), fontname="Arial", color=MUTED)
    page.insert_text((360, 720), "Date:", fontsize=11.5, fontfile=str(SANS_BOLD), fontname="ArialBold", color=INK)
    page.insert_text((400, 720), "_" * 18, fontsize=11.5, fontfile=str(SANS), fontname="Arial", color=MUTED)


def scanned_page(doc: fitz.Document, source: fitz.Document) -> None:
    pix = source[0].get_pixmap(dpi=130, colorspace=fitz.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    img = img.rotate(0.6, resample=Image.BICUBIC, fillcolor=246).filter(ImageFilter.GaussianBlur(0.5))
    img = img.point(lambda v: min(255, int(v * 0.93 + 14)))
    path = Path(source.name).with_name("scan.png")
    img.save(path)
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=str(path))


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo").resolve()
    out.mkdir(parents=True, exist_ok=True)
    chart_png = out / "chart.png"
    chart(chart_png)

    doc = fitz.open()
    report_page(doc, chart_png)
    arabic = doc.new_page(width=595, height=842)
    arabic.draw_rect(fitz.Rect(0, 0, 595, 8), color=None, fill=INDIGO)
    form_page(doc)
    base = out / "base.pdf"
    doc.save(str(base))

    # Arabic page through the engine's RTL writer (shaped, logical copy/paste).
    # add-text's "auto" alignment right-aligns inside the text's natural width,
    # so measure that on a scratch page first and anchor it to the right margin.
    scratch = out / "scratch.pdf"
    blank = fitz.open()
    blank.new_page(width=595, height=842)
    blank.save(str(scratch))

    def add_rtl(src: Path, dst: Path, y: float, text: str, size: float, color: str, bold: bool = False) -> Path:
        extra = ["--bold"] if bold else []
        probe = out / "probe.pdf"
        engine("add-text", "--input", str(scratch), "--output", str(probe), "--page", "1", "--x", "56", "--y", str(y),
               "--text", text, "--font-size", str(size), "--color", color, *extra)
        with fitz.open(str(probe)) as d:  # ink extent (text spans carry /ActualText)
            pix = d[0].get_pixmap(dpi=144, colorspace=fitz.csGRAY)
        cols = [x for x in range(pix.width) if any(pix.pixel(x, y)[0] < 200 for y in range(0, pix.height, 2))]
        width = (cols[-1] + 1) / 2 - 56
        probe.unlink()
        engine("add-text", "--input", str(src), "--output", str(dst), "--page", "2", "--x", f"{539 - width:.1f}",
               "--y", str(y), "--text", text, "--font-size", str(size), "--color", color, *extra)
        return dst

    current = add_rtl(base, out / "step.pdf", 70, "ملخص تنفيذي", 26, "#1c1f29", bold=True)
    paragraphs = [
        (118, "حققت لومن لابس في عام 2026 نتائج قياسية، إذ ارتفعت الإيرادات إلى 4.2 مليون دولار\nبفضل إطلاق منصة المستندات وزيادة العملاء العائدين بنسبة 38٪."),
        (176, "نركّز في العام المقبل على تسريع انضمام الفرق الجديدة، ودعم كامل للغة العربية\nوالكتابة من اليمين إلى اليسار، ومحرر مفتوح المصدر متاح للجميع مجاناً."),
    ]
    for i, (y, text) in enumerate(paragraphs):
        current = add_rtl(current, out / f"step{i}.pdf", y, text, 13, "#1c1f29")
    figures = [("الإيرادات", "4.2 مليون دولار"), ("العملاء العائدون", "+38٪"), ("المستخدمون النشطون", "12,000")]
    for i, (label, value) in enumerate(figures):
        current = add_rtl(current, out / f"fig{i}.pdf", 250 + i * 30, f"{label}: {value}", 14, "#4f46e5")
    current = add_rtl(current, out / "h2.pdf", 362, "أبرز الإنجازات", 18, "#1c1f29", bold=True)
    highlights = [
        "• أطلقنا أدوات PDF يستخدمها 12,000 شخص كل شهر",
        "• وقّعنا شراكات مع ثلاثة بنوك إقليمية",
        "• حققنا الربحية في الربع الثاني من العام",
    ]
    for i, line in enumerate(highlights):
        current = add_rtl(current, out / f"hl{i}.pdf", 398 + i * 26, line, 13, "#1c1f29")
    closing = ("نشكر فريقنا وعملاءنا وشركاءنا على ثقتهم، ونتطلع إلى عام جديد\n"
               "من النمو والابتكار في خدمة المستخدمين العرب حول العالم.")
    current = add_rtl(current, out / "close.pdf", 500, closing, 13, "#545a6e")
    scratch.unlink()

    final = fitz.open(str(current))
    scanned_page(final, final)
    final.set_metadata({"title": "Lumen Labs — Annual Report 2026", "author": "Lumen Labs"})
    target = out / "lumen-annual-report.pdf"
    final.save(str(target), garbage=4, deflate=True)
    for f in out.glob("step*.pdf"):
        f.unlink()
    for pattern in ("fig*.pdf", "hl*.pdf", "h2.pdf", "close.pdf"):
        for f in out.glob(pattern):
            f.unlink()
    base.unlink()
    print(target)


if __name__ == "__main__":
    main()
