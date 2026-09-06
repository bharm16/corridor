"""The rung's rasterizer and the reader's glyphs under the PDFium execution contract (#732, #729).

The imported `textract.render.render_page` and the reader's `read_pdf` both
enter PDFium, which is not safe to call from two threads of one process.
Corridor's rule for that lives in `corridor_pdf_reader.execution`, and every
in-process entry the adapter makes goes through `pdfium_entry` here, so a
threaded caller is refused at once rather than corrupting a render. The
`_isolated` forms run the same functions through a `PdfiumExecutor`, one
spawned process per call, which is what a threaded worker should use.

Page numbers are 1-based, as the reader's are; the imported rasterizer takes
a 0-based index, and this is the one place the two meet.
"""

from __future__ import annotations

from pathlib import Path

from corridor_pdf_reader.execution import PdfiumExecutor, pdfium_entry, read_document
from corridor_pdf_reader.textract.render import Raster, render_page
from corridor_pdf_reader.textract_adapter.identity import NativeGlyphs, RequestConfiguration


def rasterize_page(pdf: Path | str, page_number: int, configuration: RequestConfiguration = RequestConfiguration()) -> Raster:
    """The displayed page as the rung's deterministic PNG, under the in-process guard."""
    with pdfium_entry():
        return render_page(Path(pdf), page_number - 1, configuration.dpi, mode=configuration.mode)


def native_glyphs(pdf: Path | str, page_number: int) -> NativeGlyphs:
    """The reader's visible glyphs and hidden runs for the lane A re-map, at the measured engine and dpi."""
    document = read_document(Path(pdf), [page_number])
    page = document["pages"][0]
    return NativeGlyphs(characters=list(page["characters"]["value"]), clipped=list(page["clipped"]["value"]))


def rasterize_page_isolated(
    executor: PdfiumExecutor,
    pdf: Path | str,
    page_number: int,
    configuration: RequestConfiguration = RequestConfiguration(),
) -> Raster:
    raster: Raster = executor.run(rasterize_page, Path(pdf), page_number, configuration)
    return raster


def native_glyphs_isolated(executor: PdfiumExecutor, pdf: Path | str, page_number: int) -> NativeGlyphs:
    glyphs: NativeGlyphs = executor.run(native_glyphs, Path(pdf), page_number)
    return glyphs
