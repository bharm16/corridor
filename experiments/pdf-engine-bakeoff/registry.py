"""Registry of isolated adapters authorized for the PDF-engine experiment."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapter_protocol import PdfEngineAdapter

REAL_ENGINES = ("pymupdf", "pdf_oxide", "pdfium")


def load_adapter(name: str) -> "PdfEngineAdapter":
    # Import only the selected implementation.  This is both startup isolation
    # and an evidence boundary: no adapter can use a second engine as fallback.
    if name == "pymupdf":
        from pymupdf_adapter import PyMuPDFAdapter
        return PyMuPDFAdapter()
    if name == "pdf_oxide":
        from pdf_oxide_adapter import PDFOxideAdapter
        return PDFOxideAdapter()
    if name == "pdfium":
        from pdfium_adapter import PDFiumAdapter
        return PDFiumAdapter()
    raise KeyError(f"adapter is not registered: {name}")
