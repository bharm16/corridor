"""Registry of real adapters authorized for the incumbent-only ticket."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapter_protocol import PdfEngineAdapter

REAL_ENGINES = ("pymupdf",)


def load_adapter(name: str) -> "PdfEngineAdapter":
    if name != "pymupdf":
        raise KeyError(f"adapter is not registered: {name}")
    # Deliberately import only the selected incumbent.  In particular, this
    # module never probes either challenger distribution installed by #720.
    from pymupdf_adapter import PyMuPDFAdapter

    return PyMuPDFAdapter()

