"""Generate the Intelligence Corner PWA icons into frontend/public/.

Run: cd app/backend && uv run --frozen python ../frontend/scripts/make_icons.py

Motif: dark tile, a corner bracket in signal blue, a small light square at the
bracket's inner corner. No gold: gold is reserved for work a human owes.
"""
from pathlib import Path

from PIL import Image, ImageDraw

PUBLIC = Path(__file__).resolve().parent.parent / "public"
BG = (8, 12, 20)  # #080C14
BLUE = (43, 127, 212)  # #2B7FD4
LIGHT = (230, 237, 247)  # #E6EDF7


def draw(size: int, safe: float = 1.0, rounded: bool = True) -> Image.Image:
    """safe < 1 shrinks the motif into the central fraction (maskable)."""
    scale = 4  # supersample for smooth edges
    s = size * scale
    img = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(img)
    # Motif lives in a 32-unit grid like the SVG favicon: path M23 8 H9 v14.
    inner = s * safe
    off = (s - inner) / 2

    def p(v: float) -> float:
        return off + v / 32 * inner

    w = max(2, round(2.2 / 32 * inner))
    d.line([(p(23), p(8)), (p(9), p(8)), (p(9), p(22))], fill=BLUE, width=w, joint="curve")
    d.rectangle([p(12), p(11), p(16), p(15)], fill=LIGHT)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    PUBLIC.mkdir(exist_ok=True)
    draw(192).save(PUBLIC / "icon-ic-192.png")
    draw(512).save(PUBLIC / "icon-ic-512.png")
    draw(512, safe=0.8).save(PUBLIC / "icon-ic-maskable-512.png")
    draw(180).save(PUBLIC / "apple-touch-icon-ic.png")


if __name__ == "__main__":
    main()
