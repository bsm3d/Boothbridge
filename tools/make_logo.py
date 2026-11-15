# Benoit Saint-Moulin
# Boothbridge : draws the logo files from branding/*.svg
#
#   python tools/make_logo.py [OUTDIR]
#
# The icon of the program (djapp/ui/res) is rewritten, and the other sizes go to OUTDIR (default ../Boothbridge-site/src/images)

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "."), "Fonts")
)

from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

app = QGuiApplication([])


def render(svg: Path, width: int, height: int) -> Image.Image:
    renderer = QSvgRenderer(str(svg))
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)
    renderer.render(painter, QRectF(0, 0, width, height))
    painter.end()
    buffer = image.bits().tobytes()
    return Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)


# the name next to the mark, drawn with the fonts of the system (the svg renderer has no font of its own here)
def wordmark(mark: Image.Image) -> Image.Image:
    from PIL import ImageDraw, ImageFont

    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    light = ImageFont.truetype(str(fonts / "segoeuil.ttf"), 250)
    bold = light
    first, second = "Booth", "bridge"
    width = int(light.getlength(first) + bold.getlength(second))
    image = Image.new("RGBA", (mark.width + 56 + width + 20, mark.height), (0, 0, 0, 0))
    image.paste(mark, (0, 0))
    draw = ImageDraw.Draw(image)
    x, y = mark.width + 56, 62
    draw.text((x, y), first, font=light, fill="#ece6da")
    draw.text((x + light.getlength(first), y), second, font=bold, fill="#e3a553")
    return image


def main():
    out = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else ROOT.parent / "Boothbridge-site" / "src" / "images"
    )
    out.mkdir(parents=True, exist_ok=True)
    mark = ROOT / "branding" / "mark.svg"
    big = render(mark, 1024, 1024)
    res = ROOT / "djapp" / "ui" / "res"
    big.save(res / "icon.png")
    big.resize((256, 256), Image.LANCZOS).save(
        res / "icon.ico",
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )
    for size in (512, 256):
        render(mark, size, size).save(out / f"logo-mark-{size}.png")
    render(mark, 64, 64).save(out / "favicon.png")
    wordmark(render(mark, 512, 512)).save(out / "logo-wordmark.png")
    print(
        "written",
        res / "icon.ico",
        "and",
        sorted(p.name for p in out.glob("logo*")),
        out / "favicon.png",
    )


if __name__ == "__main__":
    main()
