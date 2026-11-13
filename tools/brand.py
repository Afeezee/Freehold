"""Generate every brand artefact from one definition of the mark.

Run after changing anything here:

    python tools/brand.py

The mark: eight blocks around a solid centre, with the right-hand block
absent. The blocks are the chunks a file is broken into; the centre is the
key that reads them; the gap is the point of the whole project - the ring is
not closed, and you can take your holding elsewhere.

Terracotta rather than the usual blue, because this is about owning ground,
and because every backup tool on earth is already blue.
"""
from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "brand"

INK = "#17191D"
INK_DARK = "#E8E6E3"
PAPER = "#FBFBFA"
PAPER_DARK = "#15171A"
ACCENT = "#C25E3A"
ACCENT_DARK = "#E08A5F"
MUTED = "#6B7177"

# A 24-unit grid: three 6-unit blocks with 3-unit gaps.
CELLS: List[Tuple[int, int]] = [
    (0, 0), (9, 0), (18, 0),
    (0, 9), (18, 9),            # (9, 9) is the centre, drawn in the accent
    (0, 18), (9, 18), (18, 18),
]
OPENING = (18, 9)               # the block deliberately left out
CENTRE = (9, 9)
BLOCK = 6
RADIUS = 1.4


def svg_mark(ink: str = INK, accent: str = ACCENT,
             size: int = 24) -> str:
    rects = []
    for x, y in CELLS:
        if (x, y) == OPENING:
            continue
        rects.append(
            '  <rect x="%d" y="%d" width="%d" height="%d" rx="%s" '
            'fill="%s"/>' % (x, y, BLOCK, BLOCK, RADIUS, ink))
    rects.append(
        '  <rect x="%d" y="%d" width="%d" height="%d" rx="%s" fill="%s"/>'
        % (CENTRE[0], CENTRE[1], BLOCK, BLOCK, RADIUS, accent))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            'width="%d" height="%d" role="img" aria-label="Freehold">\n%s\n'
            '</svg>\n' % (size, size, "\n".join(rects)))


def svg_favicon() -> str:
    """Adapts to the browser's theme, so it stays visible on a dark tab bar."""
    light = svg_mark().split("\n")[1:-2]
    dark = svg_mark(INK_DARK, ACCENT_DARK).split("\n")[1:-2]
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">\n'
            '  <style>\n'
            '    .dark { display: none }\n'
            '    @media (prefers-color-scheme: dark) {\n'
            '      .light { display: none }\n'
            '      .dark { display: inline }\n'
            '    }\n'
            '  </style>\n'
            '  <g class="light">\n%s\n  </g>\n'
            '  <g class="dark">\n%s\n  </g>\n'
            '</svg>\n' % ("\n".join(light), "\n".join(dark)))


def svg_wordmark(ink: str = INK, accent: str = ACCENT) -> str:
    """Mark plus name. The name is set in the system UI font so it matches
    the application chrome wherever it is shown."""
    blocks = []
    for x, y in CELLS:
        if (x, y) == OPENING:
            continue
        blocks.append('    <rect x="%d" y="%d" width="6" height="6" rx="1.4" '
                      'fill="%s"/>' % (x, y, ink))
    blocks.append('    <rect x="9" y="9" width="6" height="6" rx="1.4" '
                  'fill="%s"/>' % accent)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 132 24" '
            'width="132" height="24" role="img" aria-label="Freehold">\n'
            '  <g>\n%s\n  </g>\n'
            '  <text x="36" y="17.5" font-family="-apple-system,'
            'BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif" '
            'font-size="16" font-weight="600" letter-spacing="-0.2" '
            'fill="%s">Freehold</text>\n'
            '</svg>\n' % ("\n".join(blocks), ink))


# -- raster ---------------------------------------------------------------
def draw_mark(size: int, ink: str, accent: str, bg=None,
              pad_ratio: float = 0.0) -> Image.Image:
    """Redraw the same geometry with Pillow, supersampled for clean edges."""
    scale = 8
    big = size * scale
    img = Image.new("RGBA", (big, big), bg or (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = big * pad_ratio
    span = big - pad * 2
    unit = span / 24.0
    r = RADIUS * unit

    def cell(x, y, colour):
        x0 = pad + x * unit
        y0 = pad + y * unit
        d.rounded_rectangle([x0, y0, x0 + BLOCK * unit, y0 + BLOCK * unit],
                            radius=r, fill=colour)

    for x, y in CELLS:
        if (x, y) == OPENING:
            continue
        cell(x, y, ink)
    cell(CENTRE[0], CENTRE[1], accent)
    return img.resize((size, size), Image.LANCZOS)


def ui_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        r"C:\Windows\Fonts\seguisb.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def og_image() -> Image.Image:
    """1200x630 social card. No gradient, no stock photography, no glow."""
    w, h = 1200, 630
    img = Image.new("RGB", (w, h), PAPER)
    d = ImageDraw.Draw(img)

    # A faint grid of the same blocks, as a ground rather than decoration.
    step = 42
    for gx in range(0, w, step):
        for gy in range(0, h, step):
            d.rounded_rectangle([gx + 14, gy + 14, gx + 26, gy + 26],
                                radius=2, fill="#F1F0ED")

    mark = draw_mark(132, INK, ACCENT)
    img.paste(mark, (96, 112), mark)

    d.text((96, 286), "Freehold", font=ui_font(86, bold=True), fill=INK)
    d.text((96, 396),
           "Backups only you can read, on storage you can change.",
           font=ui_font(34), fill="#3A3F45")
    d.text((96, 452),
           "Encrypted on your machine. No accounts. No servers.",
           font=ui_font(28), fill=MUTED)

    d.line([(96, 528), (1104, 528)], fill="#E2E0DC", width=1)
    d.text((96, 552), "github.com/Afeezee/Freehold",
           font=ui_font(24), fill=MUTED)
    bar = draw_mark(28, MUTED, ACCENT)
    img.paste(bar, (1076, 550), bar)
    return img


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    elif isinstance(data, bytes):
        path.write_bytes(data)
    else:
        data.save(path)
    print("  " + str(path.relative_to(ROOT)))


def main() -> None:
    print("brand artefacts:")

    # Vectors
    write(OUT / "logo.svg", svg_mark(size=96))
    write(OUT / "logo-dark.svg", svg_mark(INK_DARK, ACCENT_DARK, size=96))
    write(OUT / "wordmark.svg", svg_wordmark())
    write(OUT / "wordmark-dark.svg", svg_wordmark(INK_DARK, ACCENT_DARK))
    write(OUT / "favicon.svg", svg_favicon())

    # Favicon: three sizes in one .ico, drawn at each size rather than
    # scaled down from one, so 16px stays crisp.
    ico = OUT / "favicon.ico"
    ico.parent.mkdir(parents=True, exist_ok=True)
    draw_mark(256, INK, ACCENT).save(
        ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    print("  " + str(ico.relative_to(ROOT)))

    # Windows application icon, used by the packaged executable.
    appico = OUT / "Freehold.ico"
    draw_mark(256, INK, ACCENT, pad_ratio=0.10).save(
        appico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                       (128, 128), (256, 256)])
    print("  " + str(appico.relative_to(ROOT)))

    # Web and mobile rasters
    for size in (32, 180, 192, 256, 512):
        name = {180: "apple-touch-icon.png"}.get(size, "icon-%d.png" % size)
        img = draw_mark(size, INK, ACCENT,
                        bg=PAPER if size in (180,) else None,
                        pad_ratio=0.12 if size == 180 else 0.0)
        write(OUT / name, img)

    # Maskable: Android crops to a circle, so the mark needs room to breathe.
    write(OUT / "icon-maskable-512.png",
          draw_mark(512, INK, ACCENT, bg=PAPER, pad_ratio=0.22))

    # Android launcher densities
    for folder, px in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96),
                       ("xxhdpi", 144), ("xxxhdpi", 192)):
        write(ROOT / "android/app/src/main/res" / ("mipmap-" + folder)
              / "ic_launcher.png",
              draw_mark(px, INK, ACCENT, bg=PAPER, pad_ratio=0.16))
        write(ROOT / "android/app/src/main/res" / ("mipmap-" + folder)
              / "ic_launcher_round.png",
              draw_mark(px, INK, ACCENT, bg=PAPER, pad_ratio=0.22))

    write(OUT / "og.png", og_image())
    print("done")


if __name__ == "__main__":
    main()
