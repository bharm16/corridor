"""Rasters carry the reader's frame: crop size before rotation, displayed pixels."""

from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image

from corridor_pdf_reader.textract.render import downscale_png, page_count, page_geometry, render_page
from corridor_pdf_reader.textract.tests.helpers import minimal_pdf


def test_a_rotated_page_renders_displayed_and_keeps_the_unrotated_size(tmp_path: Path) -> None:
    pdf = minimal_pdf(tmp_path / "landscape.pdf", rotate=90)
    raster = render_page(pdf, 0, 300)
    # pypdfium2 rounds the pixel box up from the float product: 792 * 300 / 72
    # is a hair over 3300, so the page is stretched onto 3301 px; the frame is
    # still the page's points, which is why boxes never go through pixels.
    assert (raster.width_px, raster.height_px) == (math.ceil(792.0 * (300 / 72)), math.ceil(612.0 * (300 / 72)))
    assert abs(raster.width_px - 3300) <= 1 and abs(raster.height_px - 2550) <= 1
    assert (raster.geometry.width, raster.geometry.height, raster.geometry.rotation) == (612.0, 792.0, 90)
    assert raster.geometry.displayed == (792.0, 612.0)
    assert raster.scale == 300 / 72 and raster.mode == "L"
    image = Image.open(io.BytesIO(raster.png))
    assert image.size == (raster.width_px, raster.height_px) and image.mode == "L"
    assert page_geometry(pdf, 0) == raster.geometry and page_count(pdf) == 1


def test_renders_are_deterministic_so_cache_keys_hold(tmp_path: Path) -> None:
    pdf = minimal_pdf(tmp_path / "text.pdf", text="Total")
    assert render_page(pdf, 0, 72).png == render_page(pdf, 0, 72).png
    assert render_page(pdf, 0, 72).sha256 != render_page(pdf, 0, 96).sha256


def test_rgb_mode_and_downscaling(tmp_path: Path) -> None:
    pdf = minimal_pdf(tmp_path / "text.pdf", text="Total")
    raster = render_page(pdf, 0, 72, mode="RGB")
    assert raster.mode == "RGB" and Image.open(io.BytesIO(raster.png)).mode == "RGB"
    small = Image.open(io.BytesIO(downscale_png(raster.png, 0.5)))
    assert small.size == (306, 396)
