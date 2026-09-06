"""One page of a read document, slimmed to what later tiers consume.

The reader's page carries every character and a render; the semantics tier
and the scorer need only the cells of each table and the text left outside
every table, each with its box. Cells and outside strings are addressed by
ID here so a later tier can point at one without retyping it.
"""

from __future__ import annotations

from typing import Any

from corridor_pdf_reader.replacement.layout import ordered_text


def slim_page(page: dict[str, Any]) -> dict[str, Any]:
    """Tables as cells, outside text as strings, hidden runs as evidence."""
    tables = []
    owned: set[int] = set()
    for table in page["tables"]["value"]:
        cells = []
        for cell in table["structured_cells"]:
            owned.update(cell["source_indices"])
            cells.append(
                {
                    "row": cell["row"],
                    "column": cell["column"],
                    "row_span": cell["row_span"],
                    "column_span": cell["column_span"],
                    "text": cell["text"],
                    "box": [round(v, 2) for v in cell["box"]],
                    **({"cut": cell["cut"]} if cell.get("cut") else {}),
                }
            )
        tables.append({"method": table.get("method"), "box": [round(v, 2) for v in table["box"]], "cells": cells})
    groups: dict[int, list[dict[str, Any]]] = {}
    for char in page["characters"]["value"]:
        if char["source_index"] in owned or not char["text"].strip():
            continue
        groups.setdefault(char["object_id"], []).append(char)
    outside = []
    for group in groups.values():
        boxes = [c.get("ink_display_box", c["display_box"]) for c in group]
        text = ordered_text(group)
        if text.strip():
            outside.append({"text": text, "box": [round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2), round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2)]})
    outside.sort(key=lambda item: (round(item["box"][1]), item["box"][0]))
    hidden: dict[int, list[dict[str, Any]]] = {}
    for char in page.get("clipped", {}).get("value", []):
        hidden.setdefault(char["object_id"], []).append(char)
    clipped = []
    for group in hidden.values():
        boxes = [c.get("ink_display_box", c["display_box"]) for c in group]
        clipped.append({"text": ordered_text(group), "box": [round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2), round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2)]})
    geometry = page["geometry"]
    return {
        "number": page["number"],
        "size": [geometry["width"], geometry["height"]],
        "rotation": geometry["rotation"],
        "tables": tables,
        "outside": outside,
        "clipped": clipped,
    }


def cell_id(table: int, row: int, column: int) -> str:
    return f"t{table}r{row}c{column}"


def outside_id(index: int) -> str:
    return f"o{index}"
