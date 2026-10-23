"""Compose the README images from raw window snapshots.

    "$FOLIO_PY" docs/screenshots/compose.py <shots-dir>

<shots-dir> holds window captures taken with script/snap.sh (2880x1758,
transparent rounded corners). Writes docs/images/*.png: a hero banner and
one framed card per feature.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "images"
SF = "/System/Library/Fonts/SFNS.ttf"
ICON = ROOT / "Resources" / "AppIcon.png"


def font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(SF, size)
    f.set_variation_by_name(weight)
    return f


def gradient(w: int, h: int, top_left: tuple, bottom_right: tuple) -> Image.Image:
    """Diagonal two-stop gradient."""
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(top_left, bottom_right))
    return small.resize((w, h), Image.BICUBIC)


def glow(canvas: Image.Image, center: tuple, radius: int, color: tuple, strength: float) -> None:
    layer = Image.new("RGB", canvas.size, color)
    mask = Image.new("L", canvas.size, 0)
    cx, cy = center
    ImageDraw.Draw(mask).ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=int(255 * strength))
    mask = mask.filter(ImageFilter.GaussianBlur(radius * 0.6))
    canvas.paste(layer, (0, 0), mask)


def window(shot: Image.Image, width: int, crop: tuple | None = None, radius: int = 28) -> Image.Image:
    img = shot.crop(crop) if crop else shot
    img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    if crop:  # re-round the corners a crop cut off
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=radius, fill=255)
        img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    return img


def paste_with_shadow(canvas: Image.Image, img: Image.Image, xy: tuple, blur: int = 40, offset: int = 24, alpha: int = 150) -> None:
    x, y = xy
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    a = img.getchannel("A").point(lambda v: v * alpha // 255)
    black = Image.new("RGBA", img.size, (8, 6, 30, 255))
    black.putalpha(a)
    shadow.paste(black, (x, y + offset), black)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow)
    # Hairline border so dark windows separate from dark backgrounds.
    border = Image.new("RGBA", (img.width + 4, img.height + 4), (0, 0, 0, 0))
    ba = img.getchannel("A").resize(border.size)
    edge = Image.new("RGBA", border.size, (255, 255, 255, 60))
    edge.putalpha(ba.point(lambda v: v * 60 // 255))
    canvas.alpha_composite(edge, (x - 2, y - 2))
    canvas.alpha_composite(img, (x, y))


def chip(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, f: ImageFont.FreeTypeFont) -> int:
    w = int(draw.textlength(text, font=f)) + 52
    h = f.size + 30
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=(255, 255, 255, 30), outline=(255, 255, 255, 70), width=2)
    draw.text((x + 26, y + h // 2), text, font=f, fill=(238, 240, 255), anchor="lm")
    return w


def hero(shot: Image.Image, path: Path) -> None:
    W, H = 2560, 1520
    canvas = gradient(W, H, (24, 20, 66), (58, 22, 104))
    glow(canvas, (380, 140), 620, (99, 102, 241), 0.55)
    glow(canvas, (2260, 360), 560, (168, 85, 247), 0.45)
    glow(canvas, (1280, 1500), 700, (79, 70, 229), 0.35)
    canvas = canvas.convert("RGBA")
    d = ImageDraw.Draw(canvas)

    icon = Image.open(ICON).convert("RGBA").resize((150, 150), Image.LANCZOS)
    title_f, tag_f, chip_f = font(118, "Bold"), font(46, "Medium"), font(32, "Semibold")
    title = "Folio PDF"
    tw = d.textlength(title, font=title_f)
    gx = int((W - (150 + 36 + tw)) / 2)
    paste_with_shadow(canvas, icon, (gx, 92), blur=18, offset=10, alpha=120)
    d.text((gx + 186, 167), title, font=title_f, fill="white", anchor="lm")
    d.text((W // 2, 300), "The free, open-source PDF editor for Mac", font=tag_f, fill=(199, 210, 254), anchor="mm")

    chips = ["Edit any text & image", "Arabic & RTL", "OCR", "Forms & e-sign", "Organize & convert", "100% offline"]
    widths = [int(d.textlength(c, font=chip_f)) + 52 for c in chips]
    x = (W - (sum(widths) + 22 * (len(chips) - 1))) // 2
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))  # translucent fills must blend
    ld = ImageDraw.Draw(layer)
    for c in chips:
        x += chip(ld, x, 370, c, chip_f) + 22
    canvas.alpha_composite(layer)

    win = window(shot, 2140)
    paste_with_shadow(canvas, win, ((W - win.width) // 2, 520), blur=50, offset=30, alpha=190)
    save(canvas, path, 1920)


ACCENTS = {
    "indigo": ((238, 240, 255), (221, 214, 254)),
    "teal": ((230, 250, 246), (204, 240, 255)),
    "amber": ((255, 247, 230), (254, 226, 226)),
    "rose": ((255, 238, 244), (233, 228, 255)),
    "slate": ((241, 245, 249), (224, 231, 255)),
}


def card(shot: Image.Image, path: Path, title: str, subtitle: str, accent: str = "indigo", crop: tuple | None = None) -> None:
    W, H = 2400, 1500
    a, b = ACCENTS[accent]
    canvas = gradient(W, H, a, b).convert("RGBA")
    d = ImageDraw.Draw(canvas)
    d.text((120, 120), title, font=font(76, "Bold"), fill=(24, 24, 40), anchor="ls")
    d.text((120, 190), subtitle, font=font(40, "Regular"), fill=(82, 86, 110), anchor="ls")
    win = window(shot, W - 240, crop)
    paste_with_shadow(canvas, win, (120, 260), blur=40, offset=22, alpha=120)
    save(canvas, path, 1600)


def save(canvas: Image.Image, path: Path, width: int) -> None:
    out = canvas.convert("RGB").resize((width, round(canvas.height * width / canvas.width)), Image.LANCZOS)
    out.save(path, optimize=True)
    print(f"{path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB")


def main() -> None:
    shots = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")
    OUT.mkdir(parents=True, exist_ok=True)

    def load(name: str) -> Image.Image | None:
        p = shots / f"{name}.png"
        return Image.open(p).convert("RGBA") if p.exists() else None

    if (s := load("edit")) is not None:
        hero(s, OUT / "hero.png")
    cards = [
        ("edited", "feature-edit", "Edit text like a word processor", "Same font, size and color; paragraphs below reflow automatically.", "indigo", None),
        ("arabic", "feature-arabic", "Arabic & right-to-left, done right", "Shaped, joined text that copies and searches in logical order.", "teal", None),
        ("pages", "feature-pages", "Organize pages", "Drag to reorder, rotate, insert, extract, split and merge.", "slate", None),
        ("tools", "feature-tools", "Every tool, one click away", "Forms, OCR, protect, stamp, convert, compare and more.", "rose", None),
        ("palette", "feature-palette", "Command palette", "Press ⌘K and type what you want to do.", "indigo", None),
        ("ocr", "feature-ocr", "OCR scanned documents", "Apple Vision on-device: Arabic, English and 30+ languages.", "amber", None),
        ("forms", "feature-forms", "Fill and create forms", "Auto-detect blanks and checkboxes, or draw fields yourself.", "teal", None),
        ("stamp", "feature-stamp", "Watermarks, signatures & annotations", "Stamps, headers and footers, Bates numbers, highlights and notes.", "rose", None),
        ("print", "feature-print", "Print to USB HP LaserJet", "Built-in support for host-based LaserJets that macOS dropped.", "slate", None),
        ("welcome", "feature-welcome", "Private by design", "Everything runs on your Mac. No account, no upload, no subscription.", "indigo", None),
    ]
    for src, name, title, subtitle, accent, crop in cards:
        if (s := load(src)) is not None:
            card(s, OUT / f"{name}.png", title, subtitle, accent, crop)


if __name__ == "__main__":
    main()
