import pymupdf, io
from PIL import Image, ImageDraw
import os
out = os.environ.get("SAMPLES_DIR", "tmp/samples"); os.makedirs(out, exist_ok=True)
# --- image asset
img = Image.new("RGB", (480, 300), (30, 110, 200)); d = ImageDraw.Draw(img)
for i in range(0, 480, 40): d.rectangle([i, 280 - i//3, i+30, 300], fill=(250, 180, 40))
d.ellipse([340, 30, 440, 130], fill=(255, 240, 200)); buf = io.BytesIO(); img.save(buf, "PNG")
doc = pymupdf.open()
p = doc.new_page(width=595, height=842)  # A4
p.insert_text((50, 70), "Quarterly Report 2026", fontname="helv", fontsize=26, color=(0.1, 0.2, 0.5))
p.insert_text((50, 100), "Prepared by the Finance Team - Draft v3", fontname="helv", fontsize=12, color=(0.3, 0.3, 0.3))
p.insert_textbox(pymupdf.Rect(50, 120, 545, 230),
  "Revenue grew 18% compared with the previous quarter, driven mainly by new subscriptions "
  "in the Gulf region. Operating costs stayed flat while marketing spend was reduced. "
  "This paragraph exists to test in-place editing of multi-line body text.", fontname="tiro", fontsize=12)
p.insert_image(pymupdf.Rect(50, 240, 350, 427), stream=buf.getvalue())
p.insert_text((370, 260), "Figure 1: Growth chart", fontname="helv", fontsize=10)
y = 460
for row in [("Item", "Q1", "Q2"), ("Subscriptions", "120,000", "141,600"), ("Services", "45,500", "47,000"), ("Total", "165,500", "188,600")]:
    for x, cell in zip((50, 300, 430), row):
        p.insert_text((x, y), cell, fontname="hebo" if row[0] in ("Item", "Total") else "helv", fontsize=11)
    p.draw_line((50, y + 6), (545, y + 6), color=(0.7, 0.7, 0.7), width=0.5); y += 24
geeza = "/System/Library/Fonts/GeezaPro.ttc"
css = "@font-face {font-family: geeza; src: url(GeezaPro.ttc);} body {font-family: geeza; font-size: 14pt;}"
arch = pymupdf.Archive("/System/Library/Fonts")
p.insert_htmlbox(pymupdf.Rect(50, 580, 545, 700),
  '<div dir="rtl" style="text-align:right">تقرير ربع سنوي — نما الإيراد بنسبة ١٨٪ مقارنة بالربع السابق. هذا النص لاختبار تحرير العربية داخل الملف.</div>',
  css=css, archive=arch)
p.insert_text((50, 800), "Page 1 - Confidential", fontname="helv", fontsize=9, color=(0.5, 0.5, 0.5))
p2 = doc.new_page(width=595, height=842)
p2.insert_text((50, 70), "Appendix A", fontname="helv", fontsize=20)
p2.insert_textbox(pymupdf.Rect(50, 90, 545, 300), "Second page body text for search, page operations and print tests. " * 5, fontname="helv", fontsize=11)
doc.set_toc([[1, "Quarterly Report", 1], [1, "Appendix A", 2]])
doc.save(f"{out}/report.pdf", garbage=3, deflate=True)
print("ok", doc.page_count)
