"""Generates matchready/icon.ico. Run: .venv\\Scripts\\python.exe tools\\make_icon.py"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "matchready" / "icon.ico"
S = 1024  # draw large, downsample for smooth edges


def build():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded-square tile with a soft vertical gradient
    tile = Image.new("RGBA", (S, S))
    td = ImageDraw.Draw(tile)
    top, bot = (31, 58, 90), (14, 24, 38)
    for y in range(S):
        t = y / S
        td.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([32, 32, S - 32, S - 32], radius=230, fill=255)
    img.paste(tile, (0, 0), mask)
    # Signal arcs (ping waves) fading outward, centered low so the whole mark sits in the tile
    cx, cy = S // 2, 690
    arcs = [(170, (46, 204, 113)), (330, (120, 190, 235)), (490, (59, 142, 208))]
    for r, col in arcs:
        d.arc([cx - r, cy - r, cx + r, cy + r], start=225, end=315, fill=col + (255,), width=62)
        # round the arc ends (PIL draws the stroke inward from r, so the stroke's center is r - 31)
        for ang in (225, 315):
            x = cx + (r - 31) * math.cos(math.radians(ang))
            y = cy + (r - 31) * math.sin(math.radians(ang))
            d.ellipse([x - 31, y - 31, x + 31, y + 31], fill=col + (255,))
    # Green "ready" dot
    d.ellipse([cx - 88, cy - 88, cx + 88, cy + 88], fill=(46, 204, 113, 255))
    return img


def main():
    img = build()
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    img.resize((256, 256), Image.LANCZOS).save(OUT, format="ICO", sizes=sizes)
    img.resize((512, 512), Image.LANCZOS).save(OUT.with_suffix(".preview.png"))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
