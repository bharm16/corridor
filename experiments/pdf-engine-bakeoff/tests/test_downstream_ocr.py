"""Required same-Tesseract downstream check on one fixed generated raster."""

from __future__ import annotations

import difflib
import itertools
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

EXPERIMENT = Path(__file__).parents[1]
sys.path.insert(0, str(EXPERIMENT))

from evaluators import tesseract_tokens  # noqa: E402
from fixtures import generate  # noqa: E402


def test_same_tesseract_configuration_receives_each_engine_raster(tmp_path):
    assert shutil.which("tesseract") is not None, (
        "required OCR executable 'tesseract' was not found; run the environment "
        "setup step that installs the 'tesseract-ocr' package"
    )

    generate(tmp_path); source = tmp_path / "borderless.pdf"
    images = tmp_path / "ocr"; images.mkdir()
    _render_rasters(source, images)
    tokens = {path.stem: tesseract_tokens(str(path)) for path in sorted(images.glob("*.ppm"))}
    assert set(tokens) == {"pymupdf", "pdf-oxide", "pdfium"}
    assert all(engine_tokens for engine_tokens in tokens.values())
    differences = {
        f"{left} -> {right}": [
            difference
            for difference in difflib.ndiff(tokens[left], tokens[right])
            if not difference.startswith("  ")
        ]
        for left, right in itertools.combinations(tokens, 2)
    }
    print("downstream OCR token differences: " + json.dumps(differences, sort_keys=True))


def _render_rasters(source: Path, images: Path) -> None:
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path

        import pdf_oxide
        import pymupdf
        import pypdfium2

        source, images = Path(sys.argv[1]), Path(sys.argv[2])

        def ppm(name, width, height, rgb):
            assert len(rgb) == width * height * 3
            (images / name).write_bytes(f"P6\\n{width} {height}\\n255\\n".encode() + rgb)

        document = pymupdf.open(source)
        with document:
            pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            ppm("pymupdf.ppm", pixmap.width, pixmap.height, bytes(pixmap.samples))
        oxide = pdf_oxide.PdfDocument.from_bytes(source.read_bytes())
        pixmap = oxide.render_pixmap(0, dpi=144)
        rgba = bytes(pixmap.data)
        assert len(rgba) == pixmap.width * pixmap.height * 4
        rgb = bytes(channel for offset in range(0, len(rgba), 4)
                    for channel in rgba[offset:offset + 3])
        ppm("pdf-oxide.ppm", pixmap.width, pixmap.height, rgb)
        document = pypdfium2.PdfDocument(source)
        page = document[0]
        bitmap = page.render(scale=2)
        try:
            raw = bytes(bitmap.buffer)
            rgb = bytes(channel for offset in range(0, len(raw), 3)
                        for channel in (raw[offset + 2], raw[offset + 1], raw[offset]))
            ppm("pdfium.ppm", bitmap.width, bitmap.height, rgb)
        finally:
            bitmap.close()
            page.close()
            document.close()
        """
    )
    subprocess.run(
        ["uv", "run", "--project", str(EXPERIMENT), "--frozen", "--no-sync",
         "python", "-c", script, str(source), str(images)],
        check=True,
    )
