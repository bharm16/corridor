"""Source-only text fragments and alignment evidence for partially ruled tables."""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Any

from corridor_pdf_reader.replacement.layout import clustered, grid_tables, ordered_text
from corridor_pdf_reader.replacement.table_structure import source_indices


def union(boxes: list[list[float]]) -> list[float]:
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def numeric(text: str) -> bool:
    return bool(re.fullmatch(r"[\s$€£¥+\-−().,%\d]+", text)) and any(
        c.isdigit() for c in text
    )


def fragment(chars: list[dict[str, Any]]) -> dict[str, Any]:
    size = statistics.median(
        c.get("font_size", max(1, c["display_box"][3] - c["display_box"][1]))
        for c in chars
    )
    return {
        "box": union([c.get("ink_display_box", c["display_box"]) for c in chars]),
        "text": ordered_text(chars),
        "chars": chars,
        "font_size": size,
        "bold": sum(
            "bold" in c.get("font_name", "").lower()
            or 600 <= c.get("font_weight", 0) <= 1000
            for c in chars
        )
        > len(chars) / 2,
    }


def group_rows(fragments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for f in sorted(fragments, key=lambda f: (f["box"][1] + f["box"][3]) / 2):
        cy = (f["box"][1] + f["box"][3]) / 2
        if rows:
            row = rows[-1]
            mid = statistics.median((x["box"][1] + x["box"][3]) / 2 for x in row)
            threshold = max(
                1,
                min(f["font_size"], statistics.median(x["font_size"] for x in row))
                * 0.45,
            )
        if not rows or abs(cy - mid) > threshold:
            rows.append([])
        rows[-1].append(f)
    return rows


def horizontal_fragments(chars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    objects: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in chars:
        if round(c.get("angle", 0)) % 360 == 0 and c["text"].strip():
            objects[c.get("object_id", -1)].append(c)
    pieces: list[dict[str, Any]] = []
    for group in objects.values():
        size = statistics.median(
            c.get("font_size", max(1, c["display_box"][3] - c["display_box"][1]))
            for c in group
        )
        lines: list[list[dict[str, Any]]] = []
        for c in sorted(
            group,
            key=lambda c: (
                (
                    c.get("ink_display_box", c["display_box"])[1]
                    + c.get("ink_display_box", c["display_box"])[3]
                )
                / 2,
                c["display_box"][0],
            ),
        ):
            b = c.get("ink_display_box", c["display_box"])
            cy = (b[1] + b[3]) / 2
            previous = (
                lines[-1][0].get("ink_display_box", lines[-1][0]["display_box"])
                if lines
                else None
            )
            if previous is None or abs(cy - (previous[1] + previous[3]) / 2) > max(
                1, size * 0.45
            ):
                lines.append([])
            lines[-1].append(c)
        for line in lines:
            parts: list[list[dict[str, Any]]] = []
            last = None
            for c in sorted(
                line, key=lambda c: c.get("ink_display_box", c["display_box"])[0]
            ):
                b = c.get("ink_display_box", c["display_box"])
                if last is None or b[0] - last[2] > max(2, size * 0.7):
                    parts.append([])
                parts[-1].append(c)
                last = b
            pieces.extend(fragment(part) for part in parts)
    rows = group_rows(pieces)
    merged = []
    for row in rows:
        pending = None
        for part in sorted(row, key=lambda f: f["box"][0]):
            if pending is not None:
                gap = part["box"][0] - pending["box"][2]
                if (
                    -0.3
                    <= gap
                    <= max(2, min(part["font_size"], pending["font_size"]) * 0.7)
                ):
                    pending = fragment(pending["chars"] + part["chars"])
                    continue
                if pending["text"].strip() in ("$", "€", "£", "¥") and (
                    numeric(part["text"]) or part["text"].strip() in ("-", "–", "—")
                ):
                    pending = fragment(pending["chars"] + part["chars"])
                    continue
                merged.append(pending)
            pending = part
        if pending is not None:
            merged.append(pending)
    return merged


def refine_drawn_alignment(
    tables: list[dict[str, Any]],
    chars: list[dict[str, Any]],
    lines: list[list[float]],
    fragments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add only supported missing separators inside existing table regions."""
    extra = []
    for table in tables:
        box = table["box"]
        local = [
            f
            for f in fragments
            if box[0] <= (f["box"][0] + f["box"][2]) / 2 <= box[2]
            and box[1] <= (f["box"][1] + f["box"][3]) / 2 <= box[3]
        ]
        if not local:
            continue
        size = statistics.median(f["font_size"] for f in local)
        ys = clustered(
            [v for c in table["structured_cells"] for v in (c["box"][1], c["box"][3])]
        )
        xs = clustered(
            [v for c in table["structured_cells"] for v in (c["box"][0], c["box"][2])]
        )
        for top, bottom in zip(ys, ys[1:]):
            if bottom - top < size * 3:
                continue
            rows = group_rows(
                [f for f in local if top < (f["box"][1] + f["box"][3]) / 2 < bottom]
            )
            supported = []
            for row in rows:
                columns = {
                    next(
                        (
                            i
                            for i in range(len(xs) - 1)
                            if xs[i] <= (f["box"][0] + f["box"][2]) / 2 <= xs[i + 1]
                        ),
                        -1,
                    )
                    for f in row
                }
                if len(columns - {-1}) >= 2:
                    supported.append(
                        statistics.median((f["box"][1] + f["box"][3]) / 2 for f in row)
                    )
            if (
                len(supported) >= 2
                and min(b - a for a, b in zip(supported, supported[1:])) > size * 1.15
            ):
                extra.extend(
                    [
                        [box[0], (a + b) / 2, box[2], (a + b) / 2]
                        for a, b in zip(supported, supported[1:])
                    ]
                )
        for left, right in zip(xs, xs[1:]):
            if right - left < size * 5:
                continue
            gaps = []
            for row in group_rows(
                [f for f in local if left < (f["box"][0] + f["box"][2]) / 2 < right]
            ):
                row = sorted(row, key=lambda f: f["box"][0])
                for first, second in zip(row, row[1:]):
                    if second["box"][0] - first["box"][2] > size * 0.7:
                        gaps.append(
                            (
                                (first["box"][2] + second["box"][0]) / 2,
                                (first["box"][1] + first["box"][3]) / 2,
                            )
                        )
            for cut in clustered([g[0] for g in gaps], tolerance=2):
                support = {round(g[1], 1) for g in gaps if abs(g[0] - cut) <= 2}
                if len(support) < 2:
                    continue
                for top, bottom in zip(ys, ys[1:]):
                    crossing = any(
                        f["box"][0] + 0.5 < cut < f["box"][2] - 0.5
                        and top < (f["box"][1] + f["box"][3]) / 2 < bottom
                        for f in local
                    )
                    supported_here = any(
                        abs(x - cut) <= 2 and top < y < bottom for x, y in gaps
                    )
                    if supported_here and not crossing:
                        extra.append([cut, top, cut, bottom])
    if not extra:
        return tables
    rebuilt = grid_tables(chars, lines + extra)
    for table in rebuilt:
        table["method"] = "drawn-grid+aligned-text-v1"
        table["inferred_borders"] = [
            line
            for line in extra
            if table["box"][0] - 2 <= line[0] <= table["box"][2] + 2
            and table["box"][1] - 2 <= line[1] <= table["box"][3] + 2
        ]
    return rebuilt if source_indices(tables) <= source_indices(rebuilt) else tables


def aligned_tables(
    fragments: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    page_size: tuple[float, float],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Recover aligned table regions with headers or repeated label/value rows."""
    if not fragments:
        return existing
    row_groups = group_rows(fragments)
    key = f"{page_size[0]:.1f}x{page_size[1]:.1f}"
    header_rows = [
        sorted(row, key=lambda f: f["box"][0])
        for row in row_groups
        if len(row) >= 2
        and sum(f["bold"] for f in row) >= len(row) / 2
        and min(f["box"][1] for f in row) < page_size[1] * 0.4
    ]
    for table in existing:
        first_cells = [c for c in table["structured_cells"] if c["row"] == 0]
        if not first_cells:
            continue
        first_bottom = min(c["box"][3] for c in first_cells)
        header_rows.extend(
            sorted(row, key=lambda f: f["box"][0])
            for row in row_groups
            if len(row) > table["column_count"]
            and table["box"][1]
            <= statistics.median((f["box"][1] + f["box"][3]) / 2 for f in row)
            <= first_bottom
        )
    header = max(header_rows, key=len, default=None)
    profile = None
    if header:
        profile = {
            "anchors": [(f["box"][0], f["box"][2]) for f in header],
            "labels": [f["text"] for f in header],
            "size": statistics.median(f["font_size"] for f in header),
        }
    else:
        ledger = []
        for row in row_groups:
            ordered = sorted(row, key=lambda f: f["box"][0])
            if (
                2 <= len(ordered) <= 4
                and not numeric(ordered[0]["text"])
                and numeric(ordered[-1]["text"])
                and ordered[-1]["box"][0] - ordered[-2]["box"][2]
                > ordered[0]["font_size"] * 2
            ):
                ledger.append(ordered)
        if len(ledger) >= 3:
            rights = clustered([r[-1]["box"][2] for r in ledger], tolerance=3)
            right = max(
                rights, key=lambda x: sum(abs(r[-1]["box"][2] - x) <= 3 for r in ledger)
            )
            ledger = [r for r in ledger if abs(r[-1]["box"][2] - right) <= 3]
            if len(ledger) >= 3:
                count = statistics.mode(len(row) for row in ledger)
                complete = [row for row in ledger if len(row) == count]
                profile = {
                    "anchors": [
                        (
                            min(r[i]["box"][0] for r in complete),
                            statistics.median(r[i]["box"][2] for r in complete),
                        )
                        for i in range(count)
                    ],
                    "labels": [None] * count,
                    "size": statistics.median(r[0]["font_size"] for r in ledger),
                    "ledger_range": [
                        min(f["box"][1] for r in ledger for f in r),
                        max(f["box"][3] for r in ledger for f in r),
                    ],
                }
    if header is None and key in state:
        previous = state[key]
        # Page dimensions are only a cache key. Repeated rows must support at
        # least half the prior columns (and at least three); fully blank columns
        # are allowed. A two-column ledger cannot satisfy a prior wide profile.
        support = [0] * len(previous["anchors"])
        complete_rows = 0
        required_columns = max(3, len(support) / 2)
        for row in row_groups:
            matched = set()
            for f in row:
                distances = [
                    min(
                        abs(f["box"][0] - a[0]),
                        abs(f["box"][2] - a[1]),
                        abs(f["box"][0] + f["box"][2] - a[0] - a[1]) / 2,
                    )
                    for a in previous["anchors"]
                ]
                column = min(range(len(distances)), key=distances.__getitem__)
                if distances[column] <= previous["size"] * 1.5:
                    matched.add(column)
            for column in matched:
                support[column] += 1
            complete_rows += len(matched) >= required_columns
        if sum(n >= 3 for n in support) >= required_columns and complete_rows >= 3:
            profile = {
                **previous,
                "continuation_evidence": {
                    "column_support_rows": support,
                    "supported_rows": complete_rows,
                    "required_columns_per_row": required_columns,
                },
            }
    if profile is None:
        return existing
    anchors = profile["anchors"]
    size = profile["size"]
    top = (
        min(f["box"][1] for f in header)
        if header
        else profile.get("ledger_range", [0, page_size[1]])[0]
    )
    bottom = profile.get("ledger_range", [0, page_size[1]])[1]

    def column_for(f: dict[str, Any]) -> int:
        return min(
            range(len(anchors)),
            key=lambda i: min(
                abs(f["box"][0] - anchors[i][0]), abs(f["box"][2] - anchors[i][1])
            ),
        )

    body_fragments = []
    for f in fragments:
        objects: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for c in f["chars"]:
            objects[c.get("object_id", -1)].append(c)
        pieces = [fragment(group) for group in objects.values()]
        body_fragments.extend(
            pieces if len({column_for(piece) for piece in pieces}) > 1 else [f]
        )
    assigned = []
    for f in body_fragments:
        if (
            f["box"][3] < top - 1
            or f["box"][1] > bottom + 1
            or not size * 0.75 <= f["font_size"] <= size * 1.25
        ):
            continue
        col = column_for(f)
        # Long page prose must not become a cell spanning the next column.
        if col + 1 < len(anchors) and f["box"][2] > anchors[col + 1][0] + size:
            continue
        assigned.append({**f, "column": col})
    grouped = group_rows(assigned)
    supported = [r for r in grouped if len({f["column"] for f in r}) >= 2]
    if len(supported) < 3:
        return existing
    last_supported = max(f["box"][3] for r in supported for f in r)
    grouped = [
        r for r in grouped if min(f["box"][1] for f in r) <= last_supported + size * 0.5
    ]
    records: list[list[dict[str, Any]]] = []
    for row in grouped:
        if records:
            previous = records[-1]
            previous_cols = {f["column"] for f in previous}
            cols = {f["column"] for f in row}
            prev_box = union([f["box"] for f in previous])
            box = union([f["box"] for f in row])
            delta = (box[1] + box[3] - prev_box[1] - prev_box[3]) / 2
            if not (cols & previous_cols) and delta < size * 0.9:
                previous.extend(row)
                continue
            if (
                len(row) == 1
                and not row[0]["bold"]
                and row[0]["column"] in previous_cols
                and box[1] - prev_box[3] < size * 0.45
            ):
                previous.extend(row)
                continue
        records.append(list(row))
    if header:
        parents = [
            c
            for t in existing
            for c in t["structured_cells"]
            if any(
                c["box"][0] <= (h["box"][0] + h["box"][2]) / 2 <= c["box"][2]
                and c["box"][1] <= (h["box"][1] + h["box"][3]) / 2 <= c["box"][3]
                for h in header
            )
        ]
        for f in fragments:
            if f["box"][3] < top - 1 and any(
                c["box"][0] <= (f["box"][0] + f["box"][2]) / 2 <= c["box"][2]
                and c["box"][1] <= (f["box"][1] + f["box"][3]) / 2 <= c["box"][3]
                for c in parents
            ):
                records[0].append({**f, "column": column_for(f)})
    extents = []
    for i, (left, right) in enumerate(anchors):
        fs = [f for row in records for f in row if f["column"] == i]
        extents.append(
            [
                min([left] + [f["box"][0] for f in fs]),
                max([right] + [f["box"][2] for f in fs]),
            ]
        )
    xs = (
        [extents[0][0] - 2]
        + [(extents[i][1] + extents[i + 1][0]) / 2 for i in range(len(extents) - 1)]
        + [extents[-1][1] + 2]
    )
    if any(a >= b for a, b in zip(xs, xs[1:])):
        return existing
    row_boxes = [union([f["box"] for f in row]) for row in records]
    ys = (
        [row_boxes[0][1] - 2]
        + [(row_boxes[i][3] + row_boxes[i + 1][1]) / 2 for i in range(len(records) - 1)]
        + [row_boxes[-1][3] + 2]
    )
    table_box = [xs[0], ys[0], xs[-1], ys[-1]]
    # Prefer explicit, already complete grids. An aligned wider table may
    # extend a partial grid whose rightmost columns have no drawn borders.
    from corridor_pdf_reader.replacement.table_structure import overlap

    overlapping = [t for t in existing if overlap(t["box"], table_box) > 0.7]
    if any(t["column_count"] >= len(anchors) for t in overlapping):
        return existing
    cells = []
    data = [[""] * len(anchors) for _ in records]
    for ri, row in enumerate(records):
        for ci in range(len(anchors)):
            glyphs = [c for f in row if f["column"] == ci for c in f["chars"]]
            text = ordered_text(glyphs)
            data[ri][ci] = text
            cells.append(
                {
                    "row": ri,
                    "column": ci,
                    "row_span": 1,
                    "column_span": 1,
                    "box": [xs[ci], ys[ri], xs[ci + 1], ys[ri + 1]],
                    "text": text,
                    "source_indices": [
                        c["source_index"]
                        for c in glyphs
                        if "source_index" in c and c["text"].strip()
                    ],
                }
            )
    table = {
        "box": table_box,
        "row_count": len(records),
        "column_count": len(anchors),
        "rows": data,
        "structured_cells": cells,
        "cells": [c["box"] for c in cells],
        "method": "aligned-text-columns-v1",
        "column_labels": profile["labels"],
        "continuation_evidence": profile.get("continuation_evidence"),
        "coordinate_frame": "displayed crop",
    }
    if not source_indices(overlapping) <= source_indices([table]):
        return existing
    if header and len(anchors) >= 3:
        state[key] = {k: v for k, v in profile.items() if k != "ledger_range"}
    return [t for t in existing if t not in overlapping] + [table]
