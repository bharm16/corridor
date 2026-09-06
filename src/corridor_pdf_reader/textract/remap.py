"""Lane A: Textract's geometry, the document's own text.

On a page with a text layer Textract supplies only the cell polygons. Every
glyph the reader extracted (each carrying `display_box` and `text`) is
assigned to the cell whose polygon holds its centre, and the cell's text is
`replacement.layout.ordered_text` over those glyphs, so values stay the
document's text; Textract's words are never stored. Glyphs in no cell
become outside strings grouped by their text object, as the reader's slim
page groups them, and the reader's hidden runs stay as clipped evidence.
"""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from corridor_pdf_reader.replacement.layout import ordered_text

TEXT_SOURCE = "pdfium-glyphs"


def contains(polygon: list[list[float]], x: float, y: float) -> bool:
    """Ray casting; a point on the boundary counts as inside."""
    inside = False
    count = len(polygon)
    for k in range(count):
        x0, y0 = polygon[k]
        x1, y1 = polygon[(k + 1) % count]
        if (y0 > y) != (y1 > y):
            cross = (x1 - x0) * (y - y0) / (y1 - y0) + x0
            if x <= cross:
                inside = not inside
    return inside


def polygon_of(cell: dict[str, Any], margin: float = 0.0) -> list[list[float]]:
    """The cell's polygon; with a margin, its bounding box grown by that many points on every side."""
    if cell.get("polygon"):
        polygon = [list(map(float, point)) for point in cell["polygon"]]
    else:
        x0, y0, x1, y1 = cell["box"]
        polygon = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    if margin <= 0:
        return polygon
    x0, y0 = min(p[0] for p in polygon) - margin, min(p[1] for p in polygon) - margin
    x1, y1 = max(p[0] for p in polygon) + margin, max(p[1] for p in polygon) + margin
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def centre(box: list[float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def glyph_box(char: dict[str, Any]) -> list[float]:
    box: list[float] = char.get("ink_display_box") or char["display_box"]
    return box


def union_box(boxes: list[list[float]]) -> list[float]:
    x0: float = min(b[0] for b in boxes)
    y0: float = min(b[1] for b in boxes)
    x1: float = max(b[2] for b in boxes)
    y1: float = max(b[3] for b in boxes)
    return [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]


def grouped_strings(chars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Text objects as strings with the union of their glyphs' ink boxes."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for char in chars:
        groups.setdefault(int(char.get("object_id", -1)), []).append(char)
    strings: list[dict[str, Any]] = []
    for group in groups.values():
        text = ordered_text(group)
        if not text.strip():
            continue
        strings.append({"text": text, "box": union_box([glyph_box(c) for c in group])})
    strings.sort(key=lambda item: (round(item["box"][1]), item["box"][0]))
    return strings


def remap_page(page: dict[str, Any], chars: list[dict[str, Any]], clipped: list[dict[str, Any]] | None = None, margin: float = 0.0, rescue_runs: bool = False) -> dict[str, Any]:
    """The page with every cell's text taken from the glyphs its polygon holds.

    Two measured variants, both off in the specified lane A: `margin` grows
    every polygon by that many points before the test, and `rescue_runs`
    lets a glyph no polygon holds join the cell that holds most of its own
    text object's glyphs (the period Textract's tight cell box leaves at the
    baseline rejoins its number; a run wholly outside stays outside).
    """
    out = copy.deepcopy(page)
    cells = [cell for table in out["tables"] for cell in table["cells"]]
    polygons = [polygon_of(cell, margin) for cell in cells]
    centres = [centre(cell["box"]) for cell in cells]
    assigned: list[list[dict[str, Any]]] = [[] for _ in cells]
    loose: list[dict[str, Any]] = []
    for char in chars:
        if not str(char.get("text", "")).strip():
            continue
        x, y = centre(glyph_box(char))
        holders = [k for k, polygon in enumerate(polygons) if contains(polygon, x, y)]
        if not holders:
            loose.append(char)
            continue
        # Cells can overlap by a hair at a shared border; the nearer centre wins.
        best = min(holders, key=lambda k: (centres[k][0] - x) ** 2 + (centres[k][1] - y) ** 2)
        assigned[best].append(char)
    if rescue_runs:
        run_holders: dict[int, Counter[int]] = {}
        for k, glyphs in enumerate(assigned):
            for char in glyphs:
                run_holders.setdefault(int(char.get("object_id", -1)), Counter())[k] += 1
        still_loose = []
        for char in loose:
            counts = run_holders.get(int(char.get("object_id", -1)))
            if counts:
                assigned[counts.most_common(1)[0][0]].append(char)
            else:
                still_loose.append(char)
        loose = still_loose
    for cell, glyphs in zip(cells, assigned, strict=True):
        cell["text"] = ordered_text(glyphs) if glyphs else ""
        cell["glyphs"] = len(glyphs)
        cell.pop("word_ids", None)
    out["outside"] = grouped_strings(loose)
    out["clipped"] = grouped_strings(clipped or [])
    out["text_source"] = TEXT_SOURCE
    if margin > 0:
        out["remap_margin"] = margin
    if rescue_runs:
        out["remap_rescue_runs"] = True
    return out
