"""Refine coarse drawn grids using native row geometry and canonical glyphs.

PDFOxide can identify unruled rows that the four-border grid reader collapses.
Its returned text is deliberately unused: PDFium supplies the glyph values.
Well-resolved drawn grids retain their explicit merge/span evidence.
"""

from __future__ import annotations

import statistics
from typing import Any

from corridor_pdf_reader.replacement.layout import ordered_text, rotate_box


def overlap(first: list[float], second: list[float]) -> float:
    area = max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1])
    )
    size = min(
        (first[2] - first[0]) * (first[3] - first[1]),
        (second[2] - second[0]) * (second[3] - second[1]),
    )
    return area / size if size > 0 else 0


def source_indices(tables: list[dict[str, Any]]) -> set[int]:
    """Non-whitespace PDFium character identities owned by structured cells."""
    return {
        index
        for table in tables
        for cell in table["structured_cells"]
        for index in cell.get("source_indices", [])
    }


def refine_rows(
    tables: list[dict[str, Any]],
    native: list[dict[str, Any]],
    chars: list[dict[str, Any]],
    geometry: dict[str, Any],
) -> list[dict[str, Any]]:
    crop = geometry["crop_box"]

    def displayed(box: list[float]) -> list[float]:
        x, y, width, height = box
        return rotate_box(
            [x - crop[0], crop[3] - y - height, x + width - crop[0], crop[3] - y],
            geometry["width"],
            geometry["height"],
            geometry["rotation"],
        )

    ink = [
        (
            rotate_box(
                c["box"], geometry["width"], geometry["height"], geometry["rotation"]
            ),
            c,
        )
        for c in chars
    ]
    result = []
    for table in tables:
        alternatives = [
            n
            for n in native
            if n["col_count"] == table["column_count"]
            and n["row_count"] > table["row_count"]
            and overlap(table["box"], displayed(n["bbox"])) >= 0.8
        ]
        if not alternatives:
            result.append(table)
            continue
        candidate = max(alternatives, key=lambda n: n["row_count"])
        # The drawn vertical rules establish full column extents even when a
        # native row cell reports only the ink-width portion of that column.
        columns = {}
        for col in range(table["column_count"]):
            boxes = [
                c["box"]
                for c in table["structured_cells"]
                if c["column"] == col and c["column_span"] == 1
            ]
            if boxes:
                columns[col] = (
                    statistics.median(b[0] for b in boxes),
                    statistics.median(b[2] for b in boxes),
                )
        cells: list[dict[str, Any]] = []
        assigned_glyphs: set[int] = set()
        rows = [[""] * candidate["col_count"] for _ in range(candidate["row_count"])]
        valid = True
        for ri, row in enumerate(candidate["rows"]):
            if len(row["cells"]) != candidate["col_count"]:
                valid = False
                break
            for ci, source_cell in enumerate(row["cells"]):
                box = displayed(source_cell["bbox"])
                if box[2] <= box[0] or box[3] <= box[1]:
                    valid = False
                    break
                if (
                    ci in columns
                    and columns[ci][0] - 2 <= box[0] <= box[2] <= columns[ci][1] + 2
                ):
                    box[0], box[2] = columns[ci]
                selected = [
                    c
                    for b, c in ink
                    if box[0] <= (b[0] + b[2]) / 2 < box[2]
                    and box[1] <= (b[1] + b[3]) / 2 < box[3]
                ]
                glyph_ids = {id(c) for c in selected}
                if assigned_glyphs.intersection(glyph_ids):
                    valid = False
                    break
                assigned_glyphs.update(glyph_ids)
                text = ordered_text(selected)
                rows[ri][ci] = text
                cells.append(
                    {
                        "row": ri,
                        "column": ci,
                        "row_span": 1,
                        "column_span": 1,
                        "box": box,
                        "text": text,
                        "source_indices": [
                            c["source_index"]
                            for c in selected
                            if "source_index" in c and c["text"].strip()
                        ],
                    }
                )
        supported_rows = sum(
            sum(bool(value.strip()) for value in row) >= 2 for row in rows
        )
        original_supported = sum(
            sum(bool(value.strip()) for value in row) >= 2 for row in table["rows"]
        )
        retained = {index for c in cells for index in c["source_indices"]}
        if (
            valid
            and source_indices([table]) <= retained
            and supported_rows >= 3
            and supported_rows > original_supported
        ):
            result.append(
                {
                    **table,
                    "row_count": candidate["row_count"],
                    "rows": rows,
                    "structured_cells": cells,
                    "cells": [c["box"] for c in cells],
                    "method": "pdfoxide-native-rows+pdfium-glyphs-v1",
                }
            )
        else:
            result.append(table)
    return result
