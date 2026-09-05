"""PDFOxide-only implementation of the frozen bake-off adapter protocol."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from typing import Any, Iterator

import pdf_oxide

from adapter_protocol import AdapterRequest, AdapterResponse


def _supported(value: Any) -> dict[str, Any]:
    return {"status": "supported", "value": value}


def _box(value: Any, page_height: float | None = None) -> dict[str, float]:
    x, y, width, height = value
    if page_height is not None:
        y = page_height - y - height
    return {"x0": round(float(x), 6), "y0": round(float(y), 6),
            "x1": round(float(x + width), 6), "y1": round(float(y + height), 6)}


class PDFOxideAdapter:
    engine = "pdf_oxide"
    adapter_version = "1"

    def execute(self, request: AdapterRequest) -> AdapterResponse:
        timings: dict[str, float] = {}

        @contextmanager
        def measured(name: str) -> Iterator[None]:
            started = time.perf_counter(); yield
            timings[name] = round((time.perf_counter() - started) * 1000, 6)

        try:
            with measured("document_open"):
                document = pdf_oxide.PdfDocument.from_bytes(
                    request.source.read_bytes(), password=request.password
                )
            pages = [self._page(document, index, request, measured)
                     for index in range(document.page_count)]
            deterministic = self._base(request.source_sha256)
            deterministic["unsupported_capabilities"] = getattr(self, "_unsupported", [])
            # PDFOxide exposes XMP, rather than the document-info dictionary.
            metadata = document.xmp_metadata()
            deterministic.update(status="success", metadata=_supported(
                {"xmp": metadata} if metadata else {}
            ), pages=pages)
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
        return {"source_sha256": digest, "engine": self.engine,
                "engine_version": pdf_oxide.__version__,
                "bundled_engine_version": pdf_oxide.__version__,
                "adapter": "corridor-pdf-oxide", "adapter_version": self.adapter_version,
                "warnings": [], "errors": [], "unsupported_capabilities": []}

    @staticmethod
    def _classify(exc: Exception) -> str:
        message = str(exc).lower()
        if "password" in message or "encrypt" in message or "authentication" in message:
            return "encrypted"
        if any(word in message for word in ("xref", "trailer", "eof", "invalid pdf", "parse")):
            return "malformed"
        return "adapter_failure"

    def _page(self, document: Any, index: int, request: AdapterRequest, measured: Any) -> dict[str, Any]:
        prefix = f"page_{index + 1}"
        media = document.page_media_box(index); crop = document.page_crop_box(index) or media
        rotation = int(document.page_rotation(index)); page_height = float(crop[3] - crop[1])
        with measured(prefix + ".characters"):
            characters = [{"text": item.char, "box": _box(item.bbox, page_height), "block_index": 0,
                           "line_index": 0, "character_index": position}
                          for position, item in enumerate(document.extract_chars(index))]
        with measured(prefix + ".words"):
            words = [{"text": item.text, "box": _box(item.bbox, page_height), "block_index": 0,
                      "line_index": 0, "word_index": position}
                     for position, item in enumerate(document.extract_words(index))]
        with measured(prefix + ".text_lines"):
            text_lines = document.extract_text_lines(index)
            lines = [{"text": item.text, "box": _box(item.bbox, page_height), "block_index": 0,
                      "line_index": position} for position, item in enumerate(text_lines)]
        with measured(prefix + ".tables"):
            tables = self._tables(document.extract_tables(index), page_height)
        with measured(prefix + ".vector_paths"):
            paths = self._paths(document.extract_paths(index), page_height)
        with measured(prefix + ".renders"):
            renders = [self._render(document, index, dpi) for dpi in request.render_dpis]
        unsupported = []
        if request.clip is not None:
            unsupported.append("clip_rendering")
        # Page dimensions are the crop-box dimensions, matching the displayed page model.
        page = {"page_number": index + 1, "width_points": round(float(crop[2] - crop[0]), 6),
                "height_points": round(float(crop[3] - crop[1]), 6),
                "media_box": _supported(_box((media[0], media[1], media[2]-media[0], media[3]-media[1]))),
                "crop_box": _supported(_box((crop[0], crop[1], crop[2]-crop[0], crop[3]-crop[1]))),
                "rotation_degrees": _supported(rotation),
                "user_unit": {"status": "unsupported", "reason": "PDFOxide has no public UserUnit accessor"},
                "characters": _supported(characters), "words": _supported(words),
                "lines": _supported(lines), "tables": _supported(tables),
                "vector_paths": _supported(paths), "renders": _supported(renders)}
        # The document-level list is populated after every page is known.
        base_unsupported = ["user_unit", *unsupported]
        # execute() consumes this private marker before validation.
        page["_unsupported"] = base_unsupported
        return self._finish_page(page)

    def _finish_page(self, page: dict[str, Any]) -> dict[str, Any]:
        # Store capability names on the instance for _base result completion.
        self._unsupported = sorted(set(getattr(self, "_unsupported", ())) | set(page.pop("_unsupported")))
        return page

    @staticmethod
    def _tables(found: list[Any], page_height: float) -> list[dict[str, Any]]:
        result = []
        for ti, table in enumerate(found):
            # PDFOxide table objects are intentionally read only through their
            # documented attributes; absent topology is not reconstructed.
            rows = getattr(table, "rows", [])
            cells = []
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    bbox = getattr(cell, "bbox", None)
                    if bbox is not None:
                        cells.append({"cell_id": f"t{ti}r{ri}c{ci}", "row_id": f"r{ri}",
                                      "column_id": f"c{ci}", "box": _box(bbox, page_height),
                                      "text": str(getattr(cell, "text", "")),
                                      "row_span": 1, "column_span": 1})
            bbox = getattr(table, "bbox", (0, 0, 0, 0))
            result.append({"table_id": f"t{ti}", "box": _box(bbox, page_height),
                           "row_ids": [f"r{i}" for i in range(len(rows))],
                           "column_ids": [f"c{i}" for i in range(max((len(r) for r in rows), default=0))],
                           "cells": cells})
        return result

    @staticmethod
    def _paths(found: list[dict[str, Any]], page_height: float) -> list[dict[str, Any]]:
        result = []
        for pi, path in enumerate(found):
            points = []
            for operation in path.get("operations", []):
                if "x" in operation and "y" in operation:
                    points.append({"x": round(float(operation["x"]), 6),
                                   "y": round(page_height - float(operation["y"]), 6)})
            result.append({"path_id": f"p{pi}", "operation": "path", "points": points,
                           "closed": any(op.get("op") == "close_path" for op in path.get("operations", []))})
        return result

    @staticmethod
    def _render(document: Any, index: int, dpi: int) -> dict[str, Any]:
        pixmap = document.render_pixmap(index, dpi=dpi)
        rgba = bytes(pixmap.data)
        if len(rgba) != pixmap.width * pixmap.height * 4:
            raise ValueError("PDFOxide render did not return one RGBA sample per pixel")
        payload = bytes(channel for offset in range(0, len(rgba), 4)
                        for channel in rgba[offset:offset + 3])
        scale = dpi / 72
        return {"sha256": hashlib.sha256(payload).hexdigest(), "width_pixels": pixmap.width,
                "height_pixels": pixmap.height, "colorspace": "RGB", "alpha": False,
                "pdf_to_pixel_transform": [scale, 0, 0, scale, 0, 0], "clip_box": None}
