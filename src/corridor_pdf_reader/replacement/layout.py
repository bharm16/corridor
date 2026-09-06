"""Engine-neutral geometry, word grouping, and reconstruction of drawn grids.

No semantic guesses: cells require all four drawn borders. Borderless tables are
reported unsupported by this extractor rather than inferred from text spacing.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from typing import Any


def rotate_point(
    x: float, y: float, width: float, height: float, rotation: int
) -> tuple[float, float]:
    return {
        0: (x, y),
        90: (height - y, x),
        180: (width - x, height - y),
        270: (y, width - x),
    }[rotation % 360]


def rotate_box(
    box: list[float], width: float, height: float, rotation: int
) -> list[float]:
    points = [
        rotate_point(x, y, width, height, rotation)
        for x in (box[0], box[2])
        for y in (box[1], box[3])
    ]
    return [
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    ]


def ordered_text(chars: list[dict[str, Any]]) -> str:
    chars = [c for c in chars if c["text"] not in ("\r", "\n", "\t")]
    visible = [c for c in chars if c["text"].strip()]
    if not visible:
        return ""
    angle = statistics.mode(round(c.get("angle", 0) / 90) * 90 % 360 for c in visible)
    radians = math.radians(-angle)
    cos, sin = math.cos(radians), math.sin(radians)
    placed = []
    for c in chars:
        box = c["display_box"]
        points = [
            (x * cos - y * sin, x * sin + y * cos)
            for x in (box[0], box[2])
            for y in (box[1], box[3])
        ]
        b = [
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        ]
        if c["text"].isspace() and b[2] - b[0] <= 0.2:
            continue
        placed.append((b, c["text"], c.get("break_before")))
    if not placed:
        return ""
    height = statistics.median(max(1, b[3] - b[1]) for b, _, _ in placed)
    lines: list[list[tuple[list[float], str, bool | None]]] = []
    for box, text, boundary in sorted(
        placed, key=lambda item: ((item[0][1] + item[0][3]) / 2, item[0][0])
    ):
        cy = (box[1] + box[3]) / 2
        if not lines or abs(cy - sum(lines[-1][0][0][1::2]) / 2) > max(
            1, height * 0.35
        ):
            lines.append([])
        lines[-1].append((box, text, boundary))
    output = []
    for line in lines:
        ink = [b for b, value, _ in line if not value.isspace()]
        degenerate_metrics = (
            bool(ink) and statistics.median(b[3] - b[1] for b in ink) < 0.1
        )
        # Some bindings insert a second "space" overlapping the next glyph.
        # Keep real whitespace advances; discard these overlapping placeholders.
        line = [
            (b, value, boundary)
            for b, value, boundary in line
            if degenerate_metrics
            or not value.isspace()
            or not any(
                max(0, min(b[2], other[2]) - max(b[0], other[0])) > (b[2] - b[0]) * 0.5
                for other in ink
            )
        ]
        text = ""
        last = None
        for box, value, boundary in sorted(line, key=lambda item: item[0][0]):
            if boundary is True or (
                boundary is None
                and not degenerate_metrics
                and last
                and box[0] - last[2] > max(0.5, height * 0.18)
            ):
                text += " "
            text += value
            last = box
        output.append(" ".join(text.split()))
    return "\n".join(output)


def path_lines(
    points: list[tuple[float, float]], *, closed: bool, filled: bool, stroked: bool
) -> list[list[float]]:
    if not points:
        return []
    x0, y0 = min(x for x, _ in points), min(y for _, y in points)
    x1, y1 = max(x for x, _ in points), max(y for _, y in points)
    # Spreadsheet exports often draw rules as narrow filled rectangles.
    if filled and closed and min(x1 - x0, y1 - y0) <= 2:
        if x1 - x0 > 3:
            return [[x0, (y0 + y1) / 2, x1, (y0 + y1) / 2]]
        if y1 - y0 > 3:
            return [[(x0 + x1) / 2, y0, (x0 + x1) / 2, y1]]
    if not stroked:
        return []
    pairs = zip(points, points[1:] + points[:1] if closed else points[1:])
    return [
        [a[0], a[1], b[0], b[1]]
        for a, b in pairs
        if (abs(a[0] - b[0]) < 0.1 or abs(a[1] - b[1]) < 0.1) and math.dist(a, b) > 3
    ]


def clustered(values: list[float], tolerance: float = 1.25) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - statistics.mean(groups[-1]) > tolerance:
            groups.append([])
        groups[-1].append(value)
    return [statistics.mean(group) for group in groups]


def grid_tables(
    chars: list[dict[str, Any]], lines: list[list[float]]
) -> list[dict[str, Any]]:
    horizontal = [line for line in lines if abs(line[1] - line[3]) < 0.1]
    vertical = [line for line in lines if abs(line[0] - line[2]) < 0.1]
    xs = clustered([line[0] for line in vertical])
    ys = clustered([line[1] for line in horizontal])
    if len(xs) < 2 or len(ys) < 2:
        return []
    if len(xs) * len(ys) > 100_000:
        raise ValueError("drawn grid exceeds bounded 100,000 intersections")

    def covered(start: float, end: float, intervals: list[tuple[float, float]]) -> bool:
        reach = start
        for a, b in sorted(intervals):
            if b < reach - 1.5:
                continue
            if a > reach + 1.5:
                break
            reach = max(reach, b)
        return reach >= end - 1.5

    # Border presence on each elementary interval; missing internal borders join
    # elementary rectangles into a merged cell. Incomplete outer shapes abstain.
    h = [
        [
            covered(
                xs[c],
                xs[c + 1],
                [
                    (min(segment[0], segment[2]), max(segment[0], segment[2]))
                    for segment in horizontal
                    if abs(segment[1] - y) < 1.5
                ],
            )
            for c in range(len(xs) - 1)
        ]
        for y in ys
    ]
    v = [
        [
            covered(
                ys[r],
                ys[r + 1],
                [
                    (min(segment[1], segment[3]), max(segment[1], segment[3]))
                    for segment in vertical
                    if abs(segment[0] - x) < 1.5
                ],
            )
            for r in range(len(ys) - 1)
        ]
        for x in xs
    ]
    nr, nc = len(ys) - 1, len(xs) - 1
    seen: set[tuple[int, int]] = set()
    cells: list[dict[str, Any]] = []
    for row in range(nr):
        for col in range(nc):
            if (row, col) in seen:
                continue
            stack, component, leaks, cut = [(row, col)], set(), False, False
            while stack:
                r, c = stack.pop()
                if (r, c) in seen:
                    continue
                seen.add((r, c))
                component.add((r, c))
                for rr, cc, border in (
                    (r - 1, c, h[r][c]),
                    (r + 1, c, h[r + 1][c]),
                    (r, c - 1, v[c][r]),
                    (r, c + 1, v[c + 1][r]),
                ):
                    if border:
                        continue
                    if not (0 <= rr < nr and 0 <= cc < nc):
                        # A row taller than the page is cut by it: its cell
                        # leaves through the grid's top or bottom only, and
                        # the vertical rules run on to the cut.
                        if cc == c and v[c][r] and v[c + 1][r]:
                            cut = True
                        else:
                            leaks = True
                    elif (rr, cc) not in seen:
                        stack.append((rr, cc))
            r0, r1 = min(r for r, _ in component), max(r for r, _ in component)
            c0, c1 = min(c for _, c in component), max(c for _, c in component)
            if leaks or len(component) != (r1 - r0 + 1) * (c1 - c0 + 1):
                continue
            if cut and (c1 > c0 or (r0 > 0 and r1 < nr - 1)):
                continue
            cells.append(
                {
                    "row": r0,
                    "column": c0,
                    "row_span": r1 - r0 + 1,
                    "column_span": c1 - c0 + 1,
                    "box": [xs[c0], ys[r0], xs[c1 + 1], ys[r1 + 1]],
                    # The rectangle of the cell's first grid row: the row it
                    # belongs to, whatever the alignment of its text.
                    "row_box": [xs[c0], ys[r0], xs[c1 + 1], ys[r0 + 1]],
                    "text": "",
                    "_chars": [],
                }
            )
    if len(cells) < 2:
        return []
    lookup = {
        (r, c): cell
        for cell in cells
        for r in range(cell["row"], cell["row"] + cell["row_span"])
        for c in range(cell["column"], cell["column"] + cell["column_span"])
    }
    for char in chars:
        b = char["display_box"]
        r, c = (
            bisect_right(ys, (b[1] + b[3]) / 2) - 1,
            bisect_right(xs, (b[0] + b[2]) / 2) - 1,
        )
        if (r, c) in lookup:
            lookup[r, c]["_chars"].append(char)
    rows = [["" for _ in range(nc)] for _ in range(nr)]
    for cell in cells:
        glyphs = cell.pop("_chars")
        cell["text"] = ordered_text(glyphs)
        cell["source_indices"] = [
            c["source_index"]
            for c in glyphs
            if "source_index" in c and c["text"].strip()
        ]
        rows[cell["row"]][cell["column"]] = cell["text"]
    return [
        {
            "box": [xs[0], ys[0], xs[-1], ys[-1]],
            "rows": rows,
            "row_count": nr,
            "column_count": nc,
            "structured_cells": cells,
            "cells": [c["box"] for c in cells],
            "coordinate_frame": "displayed crop",
            "method": "drawn-grid-v1",
        }
    ]
