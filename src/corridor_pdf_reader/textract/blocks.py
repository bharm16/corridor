"""Textract blocks to the reader's page shape.

The shape is the one `bootstrap.read` writes through `replacement.pages`:
number, size, rotation, tables (method, box, cells with 0-based row and
column, spans, text and box in points), the strings outside every table,
and the clipped runs (none here: Textract reads pixels, which hide nothing).

A CELL's text is its child WORDs in Textract's order, one line per LINE the
words came from. A MERGED_CELL stands in for the CELLs it covers, with the
merged span and the covered cells' words. Geometry ratios become points
through the displayed page size, the frame the reader's `display_box` uses.
Each cell records its mean word confidence and the block ids behind it.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from typing import Any

METHOD = "textract-analyze-document-tables-v1"


def displayed_size(size: tuple[float, float] | list[float], rotation: int) -> tuple[float, float]:
    width, height = float(size[0]), float(size[1])
    return (height, width) if rotation % 180 else (width, height)


def ratio_box(bounding: dict[str, Any], frame: tuple[float, float]) -> list[float]:
    """Textract's Left/Top/Width/Height ratios as a points box in the frame."""
    width, height = frame
    left, top = float(bounding["Left"]) * width, float(bounding["Top"]) * height
    return [round(left, 2), round(top, 2), round(left + float(bounding["Width"]) * width, 2), round(top + float(bounding["Height"]) * height, 2)]


def ratio_polygon(points: Iterable[dict[str, Any]], frame: tuple[float, float]) -> list[list[float]]:
    width, height = frame
    return [[round(float(p["X"]) * width, 2), round(float(p["Y"]) * height, 2)] for p in points]


def related(block: dict[str, Any], kind: str = "CHILD") -> list[str]:
    ids: list[str] = []
    for relationship in block.get("Relationships") or []:
        if relationship.get("Type") == kind:
            ids.extend(relationship.get("Ids") or [])
    return ids


def union(boxes: list[list[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def mean_confidence(blocks: list[dict[str, Any]]) -> float | None:
    values = [float(b["Confidence"]) for b in blocks if b.get("Confidence") is not None]
    return round(statistics.fmean(values), 2) if values else None


class Blocks:
    """The response's blocks, indexed."""

    def __init__(self, blocks: list[dict[str, Any]]) -> None:
        self.blocks = blocks
        self.by_id = {block["Id"]: block for block in blocks}
        self.line_of: dict[str, str] = {}
        for block in blocks:
            if block.get("BlockType") == "LINE":
                for word in related(block):
                    self.line_of[word] = block["Id"]
        # A merged cell covers cells; either the table names the merged cells
        # or they name their parts, and both are read.
        self.merged_over: dict[str, dict[str, Any]] = {}
        for block in blocks:
            if block.get("BlockType") == "MERGED_CELL":
                for cell in related(block):
                    self.merged_over[cell] = block

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [block for block in self.blocks if block.get("BlockType") == kind]

    def words_of(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        return [self.by_id[i] for i in related(block) if i in self.by_id and self.by_id[i].get("BlockType") == "WORD"]

    def text_of(self, words: list[dict[str, Any]]) -> str:
        """Words in order, a newline where the words' LINE changes."""
        parts: list[str] = []
        previous: str | None = None
        for word in words:
            line = self.line_of.get(word["Id"])
            if parts:
                parts.append("\n" if previous is not None and line is not None and line != previous else " ")
            parts.append(str(word.get("Text") or ""))
            previous = line
        return "".join(parts)


def cell_from(index: Blocks, block: dict[str, Any], parts: list[dict[str, Any]], frame: tuple[float, float]) -> dict[str, Any]:
    words = [word for part in parts for word in index.words_of(part)]
    geometry = block.get("Geometry") or {}
    cell: dict[str, Any] = {
        "row": int(block["RowIndex"]) - 1,
        "column": int(block["ColumnIndex"]) - 1,
        "row_span": int(block.get("RowSpan") or 1),
        "column_span": int(block.get("ColumnSpan") or 1),
        "text": index.text_of(words),
        "box": ratio_box(geometry["BoundingBox"], frame),
        "confidence": mean_confidence(words),
        "block_ids": [block["Id"]] + [part["Id"] for part in parts if part is not block],
        "word_ids": [word["Id"] for word in words],
    }
    if geometry.get("Polygon"):
        cell["polygon"] = ratio_polygon(geometry["Polygon"], frame)
    if block.get("EntityTypes"):
        cell["entity_types"] = list(block["EntityTypes"])
    return cell


def page_from_blocks(blocks: list[dict[str, Any]], *, number: int, size: tuple[float, float] | list[float], rotation: int) -> dict[str, Any]:
    """One AnalyzeDocument response as the reader's slim page."""
    index = Blocks(blocks)
    frame = displayed_size(size, rotation)
    tables = []
    owned: set[str] = set()
    for table in index.of_type("TABLE"):
        cells: list[dict[str, Any]] = []
        emitted: set[str] = set()
        for cell_id in related(table):
            block = index.by_id.get(cell_id)
            if block is None or block.get("BlockType") != "CELL":
                continue
            merged = index.merged_over.get(cell_id)
            if merged is not None:
                if merged["Id"] in emitted:
                    continue
                emitted.add(merged["Id"])
                parts = [index.by_id[i] for i in related(merged) if i in index.by_id]
                cell = cell_from(index, merged, parts, frame)
            else:
                cell = cell_from(index, block, [block], frame)
            owned.update(cell["word_ids"])
            cells.append(cell)
        cells.sort(key=lambda c: (c["row"], c["column"]))
        tables.append(
            {
                "method": METHOD,
                "box": ratio_box(table["Geometry"]["BoundingBox"], frame),
                "cells": cells,
                "confidence": round(float(table["Confidence"]), 2) if table.get("Confidence") is not None else None,
                "block_id": table["Id"],
            }
        )
    outside = []
    for line in index.of_type("LINE"):
        words = [word for word in index.words_of(line) if word["Id"] not in owned]
        if not words:
            continue
        if len(words) == len(index.words_of(line)):
            box = ratio_box(line["Geometry"]["BoundingBox"], frame)
        else:
            box = union([ratio_box(word["Geometry"]["BoundingBox"], frame) for word in words])
        text = " ".join(str(word.get("Text") or "") for word in words)
        if text.strip():
            outside.append({"text": text, "box": box, "confidence": mean_confidence(words), "block_id": line["Id"]})
    outside.sort(key=lambda item: (round(item["box"][1]), item["box"][0]))
    return {
        "number": number,
        "size": [float(size[0]), float(size[1])],
        "rotation": rotation,
        "tables": tables,
        "outside": outside,
        "clipped": [],
    }
