"""PDFium-only implementation of the frozen bake-off adapter protocol."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from typing import Any, Iterator

import pypdfium2

from adapter_protocol import AdapterRequest, AdapterResponse


def _supported(value: Any) -> dict[str, Any]:
    return {"status": "supported", "value": value}


def _unsupported(reason: str) -> dict[str, str]:
    return {"status": "unsupported", "reason": reason}


def _box(value: Any) -> dict[str, float]:
    x0, y0, x1, y1 = value
    return {"x0": round(float(x0), 6), "y0": round(float(y0), 6),
            "x1": round(float(x1), 6), "y1": round(float(y1), 6)}


class PDFiumAdapter:
    engine = "pdfium"
    adapter_version = "1"

    def execute(self, request: AdapterRequest) -> AdapterResponse:
        timings: dict[str, float] = {}

        @contextmanager
        def measured(name: str) -> Iterator[None]:
            started = time.perf_counter(); yield
            timings[name] = round((time.perf_counter() - started) * 1000, 6)

        try:
            with measured("document_open"):
                document = pypdfium2.PdfDocument(request.source, password=request.password)
            with document:
                metadata = document.get_metadata_dict(skip_empty=True)
                pages = [self._page(document[index], index + 1, request, measured)
                         for index in range(len(document))]
            deterministic = self._base(request.source_sha256)
            deterministic.update(status="success", metadata=_supported(metadata), pages=pages)
        except Exception as exc:
            classification = self._classify(exc)
            deterministic = self._base(request.source_sha256)
            deterministic.update(
                status="failed", pages=[],
                metadata={"status": "failed", "error_class": classification,
                          "message": classification},
                errors=[{"classification": classification, "message": classification}],
            )
        encoded = json.dumps(deterministic, sort_keys=True, separators=(",", ":")).encode()
        return AdapterResponse(deterministic, timings, len(encoded))

    def _base(self, digest: str) -> dict[str, Any]:
        version = pypdfium2.version
        return {"source_sha256": digest, "engine": self.engine,
                "engine_version": str(version.PYPDFIUM_INFO),
                "bundled_engine_version": str(version.PDFIUM_INFO),
                "adapter": "corridor-pdfium", "adapter_version": self.adapter_version,
                "warnings": [], "errors": [],
                "unsupported_capabilities": ["words", "lines", "tables", "vector_paths", "user_unit"]}

    @staticmethod
    def _classify(exc: Exception) -> str:
        message = str(exc).lower()
        if "password" in message or "security handler" in message:
            return "encrypted"
        if "format" in message or "data format" in message:
            return "malformed"
        return "adapter_failure"

    def _page(self, page: Any, number: int, request: AdapterRequest, measured: Any) -> dict[str, Any]:
        prefix = f"page_{number}"
        width, height = page.get_size()
        with measured(prefix + ".characters"):
            textpage = page.get_textpage()
            try:
                characters = []
                for index in range(textpage.count_chars()):
                    text = textpage.get_text_range(index, 1)
                    left, bottom, right, top = textpage.get_charbox(index)
                    # PDFium is bottom-left; normalized receipt space is top-left.
                    characters.append({"text": text,
                                       "box": _box((left, height - top, right, height - bottom)),
                                       "block_index": 0, "line_index": 0,
                                       "character_index": index})
            finally:
                textpage.close()
        with measured(prefix + ".renders"):
            renders = [self._render(page, dpi, None) for dpi in request.render_dpis]
            if request.clip is not None:
                renders.append(self._render(page, 200, request.clip))
        reason = "PDFium exposes character geometry but no native normalized topology for this capability"
        return {"page_number": number, "width_points": round(float(width), 6),
                "height_points": round(float(height), 6),
                "media_box": _supported(_box(page.get_mediabox())),
                "crop_box": _supported(_box(page.get_cropbox())),
                "rotation_degrees": _supported(int(page.get_rotation())),
                "user_unit": _unsupported("pypdfium2 has no public UserUnit accessor"),
                "characters": _supported(characters), "words": _unsupported(reason),
                "lines": _unsupported(reason), "tables": _unsupported(reason),
                "vector_paths": _unsupported("adapter does not map raw PDFium page objects to path topology"),
                "renders": _supported(renders)}

    @staticmethod
    def _render(page: Any, dpi: int, clip: tuple[float, float, float, float] | None) -> dict[str, Any]:
        scale = dpi / 72
        crop = (0, 0, 0, 0)
        if clip is not None:
            width, height = page.get_size(); x0, y0, x1, y1 = clip
            crop = (x0, y0, width - x1, height - y1)
        bitmap = page.render(scale=scale, crop=crop)
        try:
            payload = bytes(bitmap.buffer)
            result = {"sha256": hashlib.sha256(payload).hexdigest(),
                      "width_pixels": bitmap.width, "height_pixels": bitmap.height,
                      "colorspace": "BGR", "alpha": False,
                      "pdf_to_pixel_transform": [scale, 0, 0, scale,
                                                 -(clip[0] * scale if clip else 0),
                                                 -(clip[1] * scale if clip else 0)],
                      "clip_box": _box(clip) if clip else None}
        finally:
            bitmap.close()
        return result
