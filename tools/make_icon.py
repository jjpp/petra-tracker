# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the integration's brand icon (custom_components/petra_tracker/brand/).

Taxi checker band (5 x 2: three black squares on the first row, two on the
second) above a radar screen tracking a blip. Drawn at high resolution and
downscaled for smooth edges. Home Assistant serves brand/icon.png (256 px) and
brand/icon@2x.png (512 px) for custom integrations; other variants fall back
to icon.png.

Usage: python tools/make_icon.py [--preview PATH]
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

S = 2048  # supersampled canvas
YELLOW = (255, 199, 44, 255)
BLACK = (28, 28, 30, 255)
SCREEN = (22, 32, 42, 255)
RING = (255, 199, 44, 120)
SWEEP = (255, 199, 44, 95)
BLIP = (255, 236, 160, 255)

OUT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "petra_tracker" / "brand"


def f(x: float) -> int:
    return round(x * S)


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Badge.
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=f(0.19), fill=YELLOW)

    # Checker band: 5 x 2, first row black at 0, 2, 4; second row at 1, 3.
    cell = 0.132
    left = 0.5 - 2.5 * cell
    top = 0.085
    # Only the black squares are drawn; the badge's yellow forms the others.
    for row in range(2):
        for col in range(5):
            if (col + row) % 2 == 0:
                x0, y0 = left + col * cell, top + row * cell
                d.rectangle((f(x0), f(y0), f(x0 + cell) - 1, f(y0 + cell) - 1), fill=BLACK)

    # Radar screen.
    cx, cy, r = 0.5, 0.665, 0.262
    d.ellipse((f(cx - r), f(cy - r), f(cx + r), f(cy + r)), fill=SCREEN)

    overlay = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    o = ImageDraw.Draw(overlay)
    # Sweep wedge trailing behind the leading edge (angles: 0 = east, clockwise).
    lead = -40
    o.pieslice((f(cx - r * 0.93), f(cy - r * 0.93), f(cx + r * 0.93), f(cy + r * 0.93)),
               start=lead - 70, end=lead, fill=SWEEP)
    # Range rings and cross-hair.
    for rr in (0.62, 0.31):
        o.ellipse((f(cx - r * rr), f(cy - r * rr), f(cx + r * rr), f(cy + r * rr)),
                  outline=RING, width=f(0.011))
    o.line((f(cx - r * 0.93), f(cy), f(cx + r * 0.93), f(cy)), fill=RING, width=f(0.007))
    o.line((f(cx), f(cy - r * 0.93), f(cx), f(cy + r * 0.93)), fill=RING, width=f(0.007))
    # Leading edge of the sweep.
    a = math.radians(lead)
    o.line((f(cx), f(cy), f(cx + math.cos(a) * r * 0.93), f(cy + math.sin(a) * r * 0.93)),
           fill=YELLOW, width=f(0.016))
    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)

    # Blip (the tracked car) just behind the sweep, with a halo.
    b = math.radians(lead - 14)
    bx, by = cx + math.cos(b) * r * 0.6, cy + math.sin(b) * r * 0.6
    halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse((f(bx - 0.036), f(by - 0.036), f(bx + 0.036), f(by + 0.036)),
                                 fill=(255, 199, 44, 80))
    img = Image.alpha_composite(img, halo)
    d = ImageDraw.Draw(img)
    d.ellipse((f(bx - 0.021), f(by - 0.021), f(bx + 0.021), f(by + 0.021)), fill=BLIP)
    # Screen bezel.
    d.ellipse((f(cx - r), f(cy - r), f(cx + r), f(cy + r)), outline=BLACK, width=f(0.016))
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", type=Path, help="also write a large preview here")
    args = ap.parse_args()
    big = draw()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        big.resize((size, size), Image.Resampling.LANCZOS).save(OUT_DIR / name, optimize=True)
        print("wrote", OUT_DIR / name)
    if args.preview:
        big.resize((512, 512), Image.Resampling.LANCZOS).save(args.preview)


if __name__ == "__main__":
    main()
