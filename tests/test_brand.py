# SPDX-License-Identifier: GPL-3.0-or-later
"""Brand images served by Home Assistant from custom_components/<domain>/brand/."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

BRAND = Path(__file__).resolve().parents[1] / "custom_components" / "petra_tracker" / "brand"


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_icon_files(name, size):
    img = Image.open(BRAND / name)
    assert img.format == "PNG" and img.mode == "RGBA"
    assert img.size == (size, size)
    assert img.getpixel((0, 0))[3] == 0  # transparent outside the rounded badge
    assert img.getpixel((size // 2, size // 2))[3] == 255
    assert (BRAND / name).stat().st_size < 100_000


def test_only_known_brand_files():
    # Home Assistant serves only these names (others fall back to icon.png).
    allowed = {"icon.png", "icon@2x.png", "logo.png", "logo@2x.png",
               "dark_icon.png", "dark_icon@2x.png", "dark_logo.png", "dark_logo@2x.png"}
    assert {p.name for p in BRAND.iterdir()} <= allowed
