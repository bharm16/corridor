"""PyMuPDF implementation of the frozen bake-off adapter protocol."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from typing import Any, Iterator

import pymupdf

from adapter_protocol import AdapterRequest, AdapterResponse

SUPPORTED = lambda value: {"status": "supported", "value": value}


def _box(value: Any) -> dict[str, float]:
    return {name: round(float(getattr(value, name)), 6) for name in ("x0", "y0", "x1", "y1")}


class PyMuPDFAdapter:
    engine = "pymupdf"
    adapter_version = "1"

    def execute(self, request: AdapterRequest) -> AdapterResponse:
        timings: dict[str, float] = {}

        @contextmanager
        def measured(name: str) -> Iterator[None]:
            started = time.perf_counter()
            yield
            timings[name] = round((time.perf_counter() - started) * 1000, 6)

        try:
            with measured("document_open"):
                document = pymupdf.open(request.source)
                if document.needs_pass and not request.password:
                    raise PermissionError("password required")
                if document.needs_pass and not document.authenticate(request.password):
                    raise PermissionError("password rejected")
            pages = []
            with document:
                metadata = {str(k): (None if v is None else str(v)) for k, v in sorted(document.metadata.items())}
                for index, page in enumerate(document):
                    pages.append(self._page(page, index + 1, request, timings, measured))
            deterministic = self._base(request.source_sha256)
            deterministic.update(status="success", metadata=SUPPORTED(metadata), pages=pages)
            encoded = json.dumps(deterministic, sort_keys=True, separators=(",", ":")).encode()
            return AdapterResponse(deterministic, timings, len(encoded))
        except Exception as exc:
            classification = self._classify(exc)
            deterministic = self._base(request.source_sha256)
            deterministic.update(
                status="failed",
                metadata={"status": "failed", "error_class": classification, "message": classification},
                pages=[],
                errors=[{"classification": classification, "message": classification}],
            )
            encoded = json.dumps(deterministic, sort_keys=True, separators=(",", ":")).encode()
            return AdapterResponse(deterministic, timings, len(encoded))

    def _base(self, digest: str) -> dict[str, Any]:
        version = pymupdf.version
        return {
            "source_sha256": digest,
            "engine": self.engine,
            "engine_version": version[0],
            "bundled_engine_version": version[1],
            "adapter": "corridor-pymupdf",
            "adapter_version": self.adapter_version,
            "warnings": [],
            "errors": [],
            "unsupported_capabilities": [],
        }

    @staticmethod
    def _classify(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "encrypted"
        if isinstance(exc, (pymupdf.FileDataError, pymupdf.EmptyFileError)):
            return "malformed"
        return "adapter_failure"

    def _page(self, page, number, request, timings, measured):
        prefix = f"page_{number}"
        with measured(prefix + ".plain_text"):
            page.get_text("text", sort=True)
        with measured(prefix + ".characters_lines"):
            raw = page.get_text("rawdict", sort=True)
            characters, lines = [], []
            for bi, block in enumerate(raw.get("blocks", [])):
                for li, line in enumerate(block.get("lines", [])):
                    line_chars = []
                    for span in line.get("spans", []):
                        for char in span.get("chars", []):
                            text = char.get("c", "")
                            line_chars.append(text)
                            characters.append({"text": text, "box": _box(pymupdf.Rect(char["bbox"])), "block_index": bi, "line_index": li, "character_index": len(characters)})
                    lines.append({"text": "".join(line_chars), "box": _box(pymupdf.Rect(line["bbox"])), "block_index": bi, "line_index": li})
        with measured(prefix + ".words"):
            words = [{"text": w[4], "box": _box(pymupdf.Rect(w[:4])), "block_index": int(w[5]), "line_index": int(w[6]), "word_index": int(w[7])} for w in page.get_text("words", sort=True)]
        with measured(prefix + ".tables"):
            tables = self._tables(page)
        with measured(prefix + ".vector_paths"):
            paths = self._paths(page)
        with measured(prefix + ".renders"):
            renders = [self._render(page, dpi, None) for dpi in request.render_dpis]
            if request.clip is not None:
                renders.append(self._render(page, 200, pymupdf.Rect(request.clip)))
        media = page.mediabox
        crop = page.cropbox
        return {
            "page_number": number,
            "width_points": round(float(page.rect.width), 6),
            "height_points": round(float(page.rect.height), 6),
            "media_box": SUPPORTED(_box(media)), "crop_box": SUPPORTED(_box(crop)),
            "rotation_degrees": SUPPORTED(int(page.rotation)),
            "user_unit": SUPPORTED(self._user_unit(page)),
            "characters": SUPPORTED(characters), "words": SUPPORTED(words),
            "lines": SUPPORTED(lines), "tables": SUPPORTED(tables),
            "vector_paths": SUPPORTED(paths), "renders": SUPPORTED(renders),
        }

    @staticmethod
    def _tables(page):
        found = page.find_tables()
        result = []
        for ti, table in enumerate(found.tables):
            cells = []
            extracted = table.extract()
            for ri, row in enumerate(table.rows):
                for ci, cell in enumerate(row.cells):
                    if cell is None:
                        continue
                    text = extracted[ri][ci] if ri < len(extracted) and ci < len(extracted[ri]) else ""
                    cells.append({"cell_id": f"t{ti}r{ri}c{ci}", "row_id": f"r{ri}", "column_id": f"c{ci}", "box": _box(pymupdf.Rect(cell)), "text": text or "", "row_span": 1, "column_span": 1})
            result.append({"table_id": f"t{ti}", "box": _box(pymupdf.Rect(table.bbox)), "row_ids": [f"r{i}" for i in range(table.row_count)], "column_ids": [f"c{i}" for i in range(table.col_count)], "cells": cells})
        return result

    @staticmethod
    def _user_unit(page) -> float:
        kind, value = page.parent.xref_get_key(page.xref, "UserUnit")
        return float(value) if kind in {"int", "real"} else 1.0

    @staticmethod
    def _paths(page):
        result = []
        for di, drawing in enumerate(page.get_drawings()):
            for ii, item in enumerate(drawing.get("items", [])):
                kind = {"l": "line", "c": "curve", "re": "rectangle"}.get(item[0], "other")
                points = []
                for value in item[1:]:
                    if hasattr(value, "x"):
                        points.append({"x": round(float(value.x), 6), "y": round(float(value.y), 6)})
                    elif hasattr(value, "x0"):
                        points.extend(({"x": round(float(value.x0), 6), "y": round(float(value.y0), 6)}, {"x": round(float(value.x1), 6), "y": round(float(value.y1), 6)}))
                result.append({"path_id": f"d{di}i{ii}", "operation": kind, "points": points, "closed": bool(drawing.get("closePath", False))})
        return result

    @staticmethod
    def _render(page, dpi, clip):
        scale = dpi / 72
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
        payload = pixmap.tobytes("png")
        return {"sha256": hashlib.sha256(payload).hexdigest(), "width_pixels": pixmap.width, "height_pixels": pixmap.height, "colorspace": pixmap.colorspace.name, "alpha": False, "pdf_to_pixel_transform": [scale, 0, 0, scale, -(clip.x0 * scale if clip else 0), -(clip.y0 * scale if clip else 0)], "clip_box": _box(clip) if clip else None}
