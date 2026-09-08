"""The retained MuPDF raster path, kept whole for the measured rollback.

This is the rasteriser `render_worker` shipped from #441 until #735 put PDFium
beside it, moved here unchanged so that the worker itself imports no MuPDF and
so that #741 can retire the engine by deleting one file. Nothing else moved:
the clip, the affine chain, the OpenCV preprocessing and the manifest are the
worker's, and both engines run through them, which is what makes the corpus
comparison in `artifacts/render-rasterizer-comparison/` a comparison of
rasterizers rather than of two pipelines.

One difference is deliberate and is the reason `RasterPage.page_origin_y`
exists. `page.cropbox` is not the file's /CropBox: MuPDF flips it about
`mediabox.y1`, which loses the media box's own origin. On a page whose media
box starts at y = 0 - every page in the corpus but twelve - the flipped box's
top edge is the crop box's top edge and the two agree exactly. On the twelve
that do not, MuPDF's flipped `y1` is short by `mediabox.y0`, and this path
reports the value it has always reported rather than a corrected one, because
the manifests already written record it and a rollback must reproduce them.
The receipt names those pages; the PDFium path reports the true top edge.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
import pymupdf

from raster import RasterPage, fixed_box


def rasterise(source_path: Path, page_number: int, dpi: int) -> RasterPage:
    """Render one page's displayed area at ``dpi``, with its declared boxes."""

    with pymupdf.open(source_path) as document:
        page = document[page_number - 1]
        media = pymupdf.Rect(page.mediabox)
        crop = pymupdf.Rect(page.cropbox)
        media_box = fixed_box(media.x0, media.y0, media.x1, media.y1)
        crop_box = fixed_box(crop.x0, crop.y0, crop.x1, crop.y1)
        rotation = int(page.rotation)
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    return RasterPage(
        media_box=media_box,
        crop_box=crop_box,
        page_origin_y=crop_box["y1"],
        rotation=rotation,
        image=image,
    )


def library_versions() -> dict[str, str]:
    return {"pymupdf": pymupdf.VersionBind}
