"""The page a rasterizer hands back, in one shape both engines fill.

The worker rendered with one engine, so the page geometry it read and the
transform chain it built were written in the same breath as the PyMuPDF calls
that produced them. #735 adds a second engine, and the geometry is exactly
where two rasterizers silently disagree, so the engines are reduced to this:
the boxes, the rotation and the pixels. Everything downstream - the clip, the
affine chain, the preprocessing, the manifest - is engine-independent and
lives in `render_worker`, which is how the two paths can be compared at all.

Boxes are integer thousandths of a PDF point, as every other Corridor page box
is (gold/pdf/v1/README.md). `media_box` is in PDF user space, y upwards, as
the file declares it. `crop_box` is the same box a reader displays, expressed
in the frame the rest of Corridor uses: the media box's top-left corner, y
downwards. `page_origin_y` is the PDF user-space y that page-space y = 0 sits
at - the crop box's top edge - and it is carried separately because MuPDF's
already-flipped crop box cannot express it when the media box's origin is not
zero (see `legacy_pymupdf`).
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

SCALE = 1_000


@dataclass(frozen=True)
class RasterPage:
    """One rendered page: what the file says about it, and its pixels."""

    media_box: dict[str, int]
    crop_box: dict[str, int]
    page_origin_y: int
    rotation: int
    image: Image.Image


def fixed_box(x0: float, y0: float, x1: float, y1: float) -> dict[str, int]:
    """A normalized box in integer thousandths of a point.

    A PDF rectangle may name its corners in any order, and PDFium returns
    whatever the file wrote; normalizing here is what makes the two engines'
    boxes comparable.
    """

    return {
        "x0": round(min(x0, x1) * SCALE),
        "y0": round(min(y0, y1) * SCALE),
        "x1": round(max(x0, x1) * SCALE),
        "y1": round(max(y0, y1) * SCALE),
    }


def flip_box(box: dict[str, int], top: int) -> dict[str, int]:
    """A y-upwards box as a y-downwards one, measured from ``top``."""

    return {
        "x0": box["x0"],
        "y0": top - box["y1"],
        "x1": box["x1"],
        "y1": top - box["y0"],
    }
