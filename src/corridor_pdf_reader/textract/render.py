"""Page rasters for Textract at a stated resolution, with the page's frame.

Textract returns geometry as ratios of the image it was sent. The image made
here is the displayed page (the PDF's rotation applied) rendered by pypdfium2,
which stretches the page exactly onto the pixel box; so a ratio times the
displayed page size in points is a coordinate in the reader's own frame
(`display_box`: rotated displayed-crop points, origin top left), whatever
rounding the pixel box carries. The unrotated crop size and the rotation are
returned beside the pixels so a page can be written the way the reader
writes it.
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

POINTS_PER_INCH = 72.0
# AnalyzeDocument's synchronous limits: 10 MB per image, 10,000 px a side.
MAX_PNG_BYTES = 10 * 1024 * 1024
MAX_SIDE_PX = 10_000
DEFAULT_DPI = 300


@dataclass(frozen=True)
class Geometry:
    """The reader's page geometry: crop size before rotation, and the rotation."""

    width: float
    height: float
    rotation: int

    @property
    def displayed(self) -> tuple[float, float]:
        if self.rotation % 180:
            return (self.height, self.width)
        return (self.width, self.height)


@dataclass(frozen=True)
class Raster:
    png: bytes
    dpi: int
    width_px: int
    height_px: int
    geometry: Geometry
    mode: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()

    @property
    def scale(self) -> float:
        """Pixels per PDF point, before the stretch onto whole pixels."""
        return self.dpi / POINTS_PER_INCH


def page_count(pdf: Path) -> int:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    try:
        return len(doc)
    finally:
        doc.close()


def page_geometry(pdf: Path, index: int) -> Geometry:
    """Crop box size and rotation, as `replacement.reader` records them."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    try:
        page = doc[index]
        crop = page.get_bbox()
        geometry = Geometry(crop[2] - crop[0], crop[3] - crop[1], page.get_rotation())
        page.close()
    finally:
        doc.close()
    return geometry


def encode_png(image: Image.Image) -> bytes:
    """Deterministic PNG bytes: the same pixels always give the same cache key."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


def render_page(pdf: Path, index: int, dpi: int = DEFAULT_DPI, *, mode: str = "L") -> Raster:
    """The displayed page as a PNG at `dpi`, grayscale unless `mode` is RGB.

    A raster over Textract's limits is not sent: an RGB page too large as a
    PNG is retried in grayscale, and a page still too large, or wider than
    10,000 px, raises.
    """
    import pypdfium2 as pdfium

    if mode not in ("L", "RGB"):
        raise ValueError("mode must be L or RGB")
    doc = pdfium.PdfDocument(pdf)
    try:
        page = doc[index]
        crop = page.get_bbox()
        geometry = Geometry(crop[2] - crop[0], crop[3] - crop[1], page.get_rotation())
        scale = dpi / POINTS_PER_INCH
        expected = (math.ceil(page.get_width() * scale), math.ceil(page.get_height() * scale))
        if max(expected) > MAX_SIDE_PX:
            raise ValueError(f"page {index + 1} renders to {expected[0]}x{expected[1]} px at {dpi} dpi, over Textract's {MAX_SIDE_PX} px side")
        bitmap = page.render(scale=scale, draw_annots=False, rev_byteorder=True, grayscale=(mode == "L"))
        image = bitmap.to_pil().convert(mode)
        page.close()
    finally:
        doc.close()
    if image.size != expected:
        raise ValueError(f"pypdfium2 rendered {image.size}, expected {expected}")
    png = encode_png(image)
    if len(png) > MAX_PNG_BYTES and mode == "RGB":
        mode = "L"
        png = encode_png(image.convert("L"))
    if len(png) > MAX_PNG_BYTES:
        raise ValueError(f"page {index + 1} is {len(png)} bytes as a PNG at {dpi} dpi, over Textract's {MAX_PNG_BYTES}")
    return Raster(png, dpi, image.size[0], image.size[1], geometry, mode)


def downscale_png(png: bytes, factor: float) -> bytes:
    """A smaller copy of a raster, for a model that reads the page image."""
    image = Image.open(io.BytesIO(png))
    size = (max(1, round(image.size[0] * factor)), max(1, round(image.size[1] * factor)))
    return encode_png(image.resize(size, Image.Resampling.LANCZOS))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
