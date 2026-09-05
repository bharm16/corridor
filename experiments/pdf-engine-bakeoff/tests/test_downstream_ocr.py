"""Optional same-Tesseract downstream check on one fixed generated raster."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

EXPERIMENT = Path(__file__).parents[1]
sys.path.insert(0, str(EXPERIMENT))

from evaluators import tesseract_tokens  # noqa: E402
from fixtures import generate  # noqa: E402


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="optional Tesseract executable absent")
def test_same_tesseract_configuration_receives_each_engine_raster(tmp_path):
    import pdf_oxide
    import pymupdf
    import pypdfium2

    generate(tmp_path); source = tmp_path / "borderless.pdf"
    images = tmp_path / "ocr"; images.mkdir()
    document = pymupdf.open(source)
    with document:
        pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        _ppm(images / "pymupdf.ppm", pixmap.width, pixmap.height, bytes(pixmap.samples))
    oxide = pdf_oxide.PdfDocument.from_bytes(source.read_bytes())
    pixmap = oxide.render_pixmap(0, dpi=144)
    _ppm(images / "pdf-oxide.ppm", pixmap.width, pixmap.height, bytes(pixmap.data))
    document = pypdfium2.PdfDocument(source); page = document[0]; bitmap = page.render(scale=2)
    try:
        raw = bytes(bitmap.buffer)
        rgb = bytes(channel for offset in range(0, len(raw), 3)
                    for channel in (raw[offset + 2], raw[offset + 1], raw[offset]))
        _ppm(images / "pdfium.ppm", bitmap.width, bitmap.height, rgb)
    finally:
        bitmap.close(); page.close(); document.close()
    tokens = {path.stem: tesseract_tokens(str(path)) for path in sorted(images.glob("*.ppm"))}
    assert set(tokens) == {"pymupdf", "pdf-oxide", "pdfium"}
    assert all(engine_tokens for engine_tokens in tokens.values())


def _ppm(path: Path, width: int, height: int, rgb: bytes) -> None:
    assert len(rgb) == width * height * 3
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + rgb)
