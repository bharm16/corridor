"""The adapter's rasters and glyphs come through the PDFium execution contract (#732, #729).

The rung's rasterizer and the reader both enter PDFium, which is not safe
across threads. The in-process forms are proved to refuse a second thread the
way `execution.pdfium_entry` does, and the isolated forms to produce the same
bytes and glyphs from another process, with no child left behind.
"""

from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

import pytest

from corridor_pdf_reader.execution import PdfiumConcurrencyError, PdfiumExecutor, pdfium_entered, pdfium_entry
from corridor_pdf_reader.textract.tests.helpers import minimal_pdf
from corridor_pdf_reader.textract_adapter.identity import NativeGlyphs, RequestConfiguration
from corridor_pdf_reader.textract_adapter.rendering import (
    native_glyphs,
    native_glyphs_isolated,
    rasterize_page,
    rasterize_page_isolated,
)

CONFIGURATION = RequestConfiguration(dpi=36)


class _Holder:
    """A thread that enters PDFium and stays there until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with pdfium_entry():
            self.entered.set()
            self.release.wait(10)

    def __enter__(self) -> "_Holder":
        self.thread.start()
        assert self.entered.wait(10)
        return self

    def __exit__(self, *_: object) -> None:
        self.release.set()
        self.thread.join(10)


def test_rasterizing_and_reading_glyphs_are_refused_while_another_thread_holds_pdfium(tmp_path: Path):
    pdf = minimal_pdf(tmp_path / "page.pdf", text="Total")

    with _Holder():
        assert pdfium_entered()
        with pytest.raises(PdfiumConcurrencyError):
            rasterize_page(pdf, 1, CONFIGURATION)
        with pytest.raises(PdfiumConcurrencyError):
            native_glyphs(pdf, 1)
    assert not pdfium_entered()
    assert rasterize_page(pdf, 1, CONFIGURATION).dpi == 36


def test_rasters_are_deterministic_and_carry_the_readers_frame(tmp_path: Path):
    pdf = minimal_pdf(tmp_path / "landscape.pdf", rotate=90, text="Total")

    raster = rasterize_page(pdf, 1, CONFIGURATION)

    assert (raster.geometry.width, raster.geometry.height, raster.geometry.rotation) == (612.0, 792.0, 90)
    assert raster.geometry.displayed == (792.0, 612.0)
    assert (raster.width_px, raster.height_px) == (396, 306)
    assert raster.mode == "L" and raster.dpi == 36
    assert rasterize_page(pdf, 1, CONFIGURATION).png == raster.png
    assert rasterize_page(pdf, 1, RequestConfiguration(dpi=72)).sha256 != raster.sha256


def test_native_glyphs_come_from_the_guarded_reader_at_the_measured_engine(tmp_path: Path):
    pdf = minimal_pdf(tmp_path / "page.pdf", text="Total")

    glyphs = native_glyphs(pdf, 1)

    assert isinstance(glyphs, NativeGlyphs)
    assert "".join(char["text"] for char in glyphs.characters) == "Total"
    assert all("display_box" in char for char in glyphs.characters)
    assert glyphs.clipped == []


def test_the_isolated_forms_produce_the_same_raster_and_glyphs_from_another_process(tmp_path: Path):
    pdf = minimal_pdf(tmp_path / "page.pdf", text="Total")
    executor = PdfiumExecutor()

    raster = rasterize_page_isolated(executor, pdf, 1, CONFIGURATION)
    glyphs = native_glyphs_isolated(executor, pdf, 1)

    assert raster == rasterize_page(pdf, 1, CONFIGURATION)
    assert glyphs == native_glyphs(pdf, 1)
    assert not pdfium_entered()
    assert multiprocessing.active_children() == []
