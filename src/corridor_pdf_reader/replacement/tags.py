"""Excel's own table tags: cell membership from the PDF structure tree.

Excel writes a Document > Part > Table > TR > TD tree whose leaves are
marked-content ids, and every text object it draws carries that id. That is
an exact statement of which glyphs form one cell and which cells form one row,
with no model involved. The tree gives no column identity (empty cells are
omitted and a sheet splits into several Table elements at blank rows or
columns), so columns come from the drawn rules and glyph geometry here.

The tree is read document-wide with pypdf because PDFium's per-page loader
returns nothing for most of these files; a tree that cannot be parsed simply
yields no tagged cells and the page falls back to the drawn grid.
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from corridor_pdf_reader.replacement.layout import clustered, grid_tables, ordered_text

Cell = list[tuple[int, int]]  # (page index, mcid)


def struct_cells(path: Path) -> dict[int, list[list[Cell]]]:
    """page index -> list of tagged rows (each a list of cells of (page, mcid)).

    Rows keep document order. An unreadable or absent tree gives an empty map.
    """
    try:
        return _struct_cells(path)
    except Exception:
        return {}


def _struct_cells(path: Path) -> dict[int, list[list[Cell]]]:
    from pypdf import PdfReader
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        IndirectObject,
        NumberObject,
    )

    reader = PdfReader(str(path), strict=False)
    root = reader.trailer["/Root"].get("/StructTreeRoot")
    if root is None:
        return {}
    page_index: dict[tuple[int, int], int] = {}
    for index, page in enumerate(reader.pages):
        ref = page.indirect_reference
        if ref is not None:
            page_index[ref.idnum, ref.generation] = index

    def page_of(node: Any, inherited: int | None) -> int | None:
        if not isinstance(node, DictionaryObject) or "/Pg" not in node:
            return inherited
        ref = node.raw_get("/Pg")
        if isinstance(ref, IndirectObject):
            return page_index.get((ref.idnum, ref.generation), inherited)
        resolved = node.get("/Pg")
        indirect = getattr(resolved, "indirect_reference", None)
        if indirect is not None:
            return page_index.get((indirect.idnum, indirect.generation), inherited)
        return inherited

    rows: list[list[Cell]] = []
    state: dict[str, Any] = {"row": None, "cell": None}

    def add(page: int | None, mcid: int) -> None:
        cell = state["cell"]
        if cell is not None and page is not None:
            cell.append((page, mcid))

    def visit(node: Any, page: int | None, depth: int) -> None:
        if depth > 64:
            return
        try:
            node = node.get_object() if isinstance(node, IndirectObject) else node
        except Exception:
            return
        if isinstance(node, ArrayObject):
            for kid in node:
                visit(kid, page, depth + 1)
            return
        if isinstance(node, NumberObject) or (isinstance(node, int) and not isinstance(node, bool)):
            add(page, int(node))
            return
        if not isinstance(node, DictionaryObject):
            return
        kind = node.get("/Type")
        if kind == "/MCR":
            mcid = node.get("/MCID")
            if mcid is not None:
                add(page_of(node, page), int(mcid))
            return
        if kind == "/OBJR":
            return
        page = page_of(node, page)
        role = str(node.get("/S", ""))
        saved = dict(state)
        if role == "/TR":
            state["row"] = []
            state["cell"] = None
            rows.append(state["row"])
        elif role in ("/TD", "/TH") and state["row"] is not None:
            state["cell"] = []
            state["row"].append(state["cell"])
        elif role == "/P" and state["cell"] is None:
            # A paragraph outside any table cell is Excel's tag for a cell that
            # stands alone: titles and labels above a grid. One row, one cell,
            # with wrapped lines kept together by their shared ids.
            standalone: Cell = []
            state["cell"] = standalone
            rows.append([standalone])
        try:
            kids = node.get("/K")
        except Exception:
            kids = None
        if kids is not None:
            visit(kids, page, depth + 1)
        state.update(saved)

    visit(root, None, 0)
    by_page: dict[int, list[list[Cell]]] = defaultdict(list)
    for row in rows:
        pages = [page for cell in row for page, _ in cell]
        if not pages:
            continue
        page = max(set(pages), key=pages.count)
        by_page[page].append([[item for item in cell if item[0] == page] for cell in row])
    return dict(by_page)


def _bands(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge intervals that overlap substantially into ordered bands.

    Two rows of tagged cells are one band when one's centre lies inside the
    other or they share at least half of the shorter one; the ascender space
    of a three-line header brushing the row above it is no overlap.
    """
    bands: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if bands:
            lo, hi = bands[-1]
            overlap = min(hi, end) - start
            inside = lo <= (start + end) / 2 <= hi or start <= (lo + hi) / 2 <= end
            if inside or overlap >= 0.5 * min(hi - lo, end - start):
                bands[-1] = (lo, max(hi, end))
                continue
        bands.append((start, end))
    return bands


def tag_tables(
    chars: list[dict[str, Any]],
    lines: list[list[float]],
    tagged_rows: list[list[Cell]],
) -> list[dict[str, Any]]:
    """One table for the page: tagged cells plus drawn-grid cells for untagged glyphs.

    Excel leaves repeated header rows and some title text untagged, so glyphs no
    TD claims are cut by the drawn rules and joined into the same grid.
    """
    by_mcid: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for char in chars:
        mcid = char.get("mcid", -1)
        if mcid >= 0:
            by_mcid[mcid].append(char)
    records: list[dict[str, Any]] = []
    for row_index, row in enumerate(tagged_rows):
        for cell in row:
            glyphs = [c for _, mcid in cell for c in by_mcid.get(mcid, [])]
            if glyphs:
                records.append({"tr": row_index, "glyphs": glyphs, "box": _union(glyphs)})
    owned = {c["source_index"] for record in records for c in record["glyphs"]}
    # Marked content no structure element claims (Excel repeats print titles
    # on later pages without tagging them) still carries one MCID per cell,
    # so those glyphs are one cell each, lines and all.
    orphans: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in chars:
        if c["source_index"] not in owned and c.get("mcid", -1) >= 0 and not c.get("artifact"):
            orphans[c["mcid"]].append(c)
    for glyphs in orphans.values():
        pieces = [piece for piece in _split_run(glyphs) if ordered_text(piece).strip()]
        if pieces:
            cell_glyphs = [g for piece in pieces for g in piece]
            records.append({"tr": None, "glyphs": cell_glyphs, "box": _union(cell_glyphs)})
        owned.update(c["source_index"] for c in glyphs)
    leftover = [c for c in chars if c["source_index"] not in owned and not c.get("artifact")]
    by_index = {c["source_index"]: c for c in leftover}
    if leftover:
        for table in grid_tables(leftover, lines):
            for cell in table["structured_cells"]:
                glyphs = [by_index[i] for i in cell["source_indices"] if i in by_index]
                if glyphs and cell["text"].strip():
                    # The drawn rectangle places the cell in its row and tells
                    # how many rows it spans; the text may sit anywhere in it.
                    records.append({"tr": None, "glyphs": glyphs, "box": _union(glyphs), "frame": cell["row_box"], "extent": cell["box"]})
                    owned.update(c["source_index"] for c in glyphs)
    # Glyphs neither a tag nor a rule claims are cells too: each text run the
    # authoring application drew stands alone, so it becomes one cell. A run
    # that is only a currency sign belongs with the amount printed beside it.
    runs: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in chars:
        if c["source_index"] not in owned and not c.get("artifact"):
            runs[c["object_id"]].append(c)
    loose: list[dict[str, Any]] = []
    for glyphs in runs.values():
        for piece in _split_run(glyphs):
            loose.append({"tr": None, "glyphs": piece, "box": _union(piece)})
    loose = _join_superscripts(loose)
    loose = _join_adjacent(loose, lines)
    records.extend(_join_stacked_lines(loose, lines))
    records = _join_currency(records, lines)
    if len(records) < 2:
        return []
    # Rows: tagged rows from several Table elements can sit side by side, so a
    # page row is a band of vertically overlapping TR extents. Untagged cells
    # join the band their centre falls in, or the single band that shares
    # their drawn row frame, provided no member of it overlaps them in x: two
    # cells in one column are never one row. Otherwise they open a band.
    tr_extent: dict[int, tuple[float, float]] = {}
    by_tr: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["tr"] is not None:
            by_tr[record["tr"]].append(record)
    ys = clustered([line[1] for line in lines if abs(line[1] - line[3]) < 0.1])
    vertical = [line for line in lines if abs(line[0] - line[2]) < 0.1]
    ordinary_extent: dict[int, tuple[float, float]] = {}
    for tr, members in by_tr.items():
        # A cell merged down several rows sits in the first row's TR; the
        # row's height is that of its ordinary cells, so a cell far taller
        # than the row's typical cell does not stretch the row.
        heights = sorted(m["box"][3] - m["box"][1] for m in members)
        typical = heights[len(heights) // 2]
        ordinary = [m for m in members if m["box"][3] - m["box"][1] <= 2.5 * typical] if len(members) >= 3 else members
        ordinary_extent[tr] = (min(m["box"][1] for m in ordinary), max(m["box"][3] for m in ordinary))
    for tr, (lo, hi) in ordinary_extent.items():
        # In a ruled table the row is the frame of rules around its cells,
        # whatever their vertical alignment; but a frame that also holds
        # another TR whose cells share this one's columns spans two rows,
        # and then the cells' own height stands.
        frame = _rule_frame((lo + hi) / 2, ys)
        if frame is None or frame[0] > lo + 0.5 or hi - 0.5 > frame[1]:
            tr_extent[tr] = (lo, hi)
            continue
        ruled = any(min(line[1], line[3]) <= (frame[0] + frame[1]) / 2 <= max(line[1], line[3]) for line in vertical)
        rival = any(
            other != tr
            and frame[0] <= (olo + ohi) / 2 <= frame[1]
            and any(
                min(m["box"][2], n["box"][2]) - max(m["box"][0], n["box"][0]) > 1.0
                for m in by_tr[tr]
                for n in by_tr[other]
            )
            for other, (olo, ohi) in ordinary_extent.items()
        )
        tr_extent[tr] = frame if ruled and not rival else (lo, hi)
    bands: list[dict[str, Any]] = [{"lo": lo, "hi": hi, "members": []} for lo, hi in _bands(list(tr_extent.values()))]
    for record in records:
        if record["tr"] is not None:
            lo, hi = tr_extent[record["tr"]]
            centre = (lo + hi) / 2
            band = next(b for b in bands if b["lo"] <= centre <= b["hi"] + 0.5)
            band["members"].append(record)
    ys = clustered([line[1] for line in lines if abs(line[1] - line[3]) < 0.1])
    for record in sorted((r for r in records if r["tr"] is None), key=lambda r: (r["box"][1], r["box"][0])):
        rect = record.get("frame", record["box"])
        centre = (rect[1] + rect[3]) / 2
        home = next((b for b in bands if b["lo"] <= centre <= b["hi"] + 0.5), None)
        framed_join = False
        if home is None:
            frame = _rule_frame(centre, ys)
            if frame is not None:
                framed = [b for b in bands if frame[0] <= (b["lo"] + b["hi"]) / 2 <= frame[1]]
                if len(framed) == 1 and not any(
                    min(m["box"][2], record["box"][2]) - max(m["box"][0], record["box"][0]) > 1.0
                    for m in framed[0]["members"]
                ):
                    home = framed[0]
                    framed_join = True
        if home is None:
            home = {"lo": rect[1], "hi": rect[3], "members": []}
            bands.append(home)
        elif framed_join:
            # Only a rule-framed join may widen a band; a join by centre must
            # not, or a tall run would swallow the rows beneath it one by one.
            home["lo"], home["hi"] = min(home["lo"], record["box"][1]), max(home["hi"], record["box"][3])
        home["members"].append(record)
    # A band that lies inside another is the same row unless their members
    # overlap in x: a one-line cell set at the top of a tall row, for example.
    bands.sort(key=lambda b: (b["lo"] - b["hi"], b["lo"]))
    merged: list[dict[str, Any]] = []
    for band in bands:
        host = next(
            (
                other
                for other in merged
                if other["lo"] - 0.5 <= band["lo"] and band["hi"] <= other["hi"] + 0.5
                and not any(
                    min(m["box"][2], n["box"][2]) - max(m["box"][0], n["box"][0]) > 1.0
                    for m in other["members"]
                    for n in band["members"]
                )
            ),
            None,
        )
        if host is None:
            merged.append(band)
        else:
            host["members"].extend(band["members"])
    bands = sorted(merged, key=lambda b: b["lo"])
    for row_index, band in enumerate(bands):
        for record in band["members"]:
            record["row"] = row_index
    row_bands = [(b["lo"], b["hi"]) for b in bands]
    # Columns: drawn vertical rules cut the page into intervals; cells that share
    # an interval in one row are split by their order inside it.
    vertical = [line for line in lines if abs(line[0] - line[2]) < 0.1]
    xs = clustered([line[0] for line in vertical]) if vertical else []
    segments_at: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for line in vertical:
        k = min(range(len(xs)), key=lambda k: abs(xs[k] - line[0]))
        if abs(xs[k] - line[0]) < 1.5:
            segments_at[k].append((min(line[1], line[3]), max(line[1], line[3])))
    ys = clustered([line[1] for line in lines if abs(line[1] - line[3]) < 0.1])
    # How the page's cells sit against each rule: a label that hugs rules on
    # both sides follows the alignment its neighbours use there.
    padding = 3.5
    left_hugs: Counter[int] = Counter()
    right_hugs: Counter[int] = Counter()
    for record in records:
        for k, x in enumerate(xs):
            if abs(record["box"][0] - x) <= padding:
                left_hugs[k] += 1
            if abs(record["box"][2] - x) <= padding:
                right_hugs[k] += 1
    for record in records:
        _place(record, xs, ys, segments_at, left_hugs, right_hugs)
    # Within one rule interval, columns come from alignment: a spreadsheet
    # print sets every cell's text against its column's left edge, right edge
    # or centre, so cells sharing an edge share a column, never two cells of
    # one row. Each group becomes a sub-column ordered by its centre.
    by_interval: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_interval[record["interval"]].append(record)
    column_index: dict[tuple[int, int], int] = {}
    ordinal = 0
    for interval in sorted(set(by_interval) | set(range(len(xs) + 1))):
        members = by_interval.get(interval, [])
        groups = _sub_columns(members, bounded=1 <= interval < len(xs))
        for rank, group in enumerate(groups):
            column_index[interval, rank] = ordinal
            for record in group:
                record["column"] = ordinal
            ordinal += 1
        if not groups:
            ordinal += 1
    structured = []
    for record in records:
        entry = {
            "row": record["row"],
            "column": record["column"],
            "row_span": _row_span(record, row_bands),
            "column_span": record["span"],
            "box": record["box"],
            "text": ordered_text(record["glyphs"]),
            "source_indices": [c["source_index"] for c in record["glyphs"]],
        }
        cut = _cut_side(record, vertical)
        if cut:
            entry["cut"] = cut
        structured.append(entry)
    structured.sort(key=lambda c: (c["row"], c["column"]))
    # Provenance in the name: a page of loose text runs is not a table a
    # consumer should trust as one.
    if any(record["tr"] is not None for record in records):
        method = "excel-tags-v1"
    elif xs:
        method = "drawn-grid+runs-v1"
    else:
        method = "text-runs-v1"
    columns = 1 + max(c["column"] for c in structured)
    rows_text = [["" for _ in range(columns)] for _ in row_bands]
    for item in structured:
        rows_text[item["row"]][item["column"]] = item["text"]
    return [
        {
            "box": _union(structured),
            "rows": rows_text,
            "row_count": len(row_bands),
            "column_count": columns,
            "structured_cells": structured,
            "cells": [],
            "coordinate_frame": "displayed crop, PDF points, top-left origin",
            "method": method,
        }
    ]


def _sub_columns(members: list[dict[str, Any]], bounded: bool) -> list[list[dict[str, Any]]]:
    """Columns inside one rule interval.

    A rule-bounded interval is one column unless rows show more cells side by
    side, and then the edge groups are merged back down to that count, nearest
    centres first, never joining cells of one row. An unbounded stretch has no
    rules to trust, so its edge groups stand as columns.
    """
    groups = _edge_groups(members)
    if not bounded or len(groups) <= 1:
        return groups
    per_row: Counter[int] = Counter(member["row"] for member in members)
    supported = [k for k in set(per_row.values()) if sum(1 for n in per_row.values() if n >= k) >= 2]
    target = max(supported) if supported else 1
    while len(groups) > target:
        best: tuple[float, int] | None = None
        for k in range(len(groups) - 1):
            left, right = groups[k], groups[k + 1]
            if {m["row"] for m in left} & {m["row"] for m in right}:
                continue
            distance = abs(_left(left) - _left(right))
            if best is None or distance < best[0]:
                best = (distance, k)
        if best is None:
            break
        groups[best[1] : best[1] + 2] = [groups[best[1]] + groups[best[1] + 1]]
    return groups


def _numeric(text: str) -> bool:
    stripped = "".join(ch for ch in text if ch not in "$,()%-/:. \n\t")
    return bool(stripped) and stripped.isdigit()


def _left(group: list[dict[str, Any]]) -> float:
    return statistics.median(m["box"][0] for m in group)


def _edge_groups(members: list[dict[str, Any]], tolerance: float = 1.5) -> list[list[dict[str, Any]]]:
    """Cells of one interval grouped by a shared left edge, right edge or centre.

    Union-find over edge buckets; a union that would put two cells of the same
    row together is refused, since one row holds one cell per column. Groups
    come back ordered by their median centre.
    """
    parent = list(range(len(members)))
    rows: list[set[int]] = [{member["row"]} for member in members]

    def find(k: int) -> int:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def unite(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb or rows[ra] & rows[rb]:
            return
        parent[rb] = ra
        rows[ra] |= rows[rb]

    # Numbers sit against their column's right edge, text against its left;
    # either may be centred. Only the edges a cell can be aligned to count, so
    # a label that happens to end where a date column ends is not joined to it.
    buckets: dict[tuple[str, int], list[int]] = defaultdict(list)
    for k, member in enumerate(members):
        box = member["box"]
        text = ordered_text(member["glyphs"])
        edges = [("c", (box[0] + box[2]) / 2), ("r", box[2]) if _numeric(text) else ("l", box[0])]
        for name, value in edges:
            slot = round(value / tolerance)
            buckets[name, slot].append(k)
            buckets[name, slot + 1].append(k)
    # Shared edges unite first; a shared centre is weaker evidence, since a
    # short word can sit under the middle of a wide centred title by chance.
    for phase in ("l", "r", "c"):
        for (name, _), bucket in buckets.items():
            if name == phase:
                for k in bucket[1:]:
                    unite(bucket[0], k)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for k, member in enumerate(members):
        grouped[find(k)].append(member)
    groups = list(grouped.values())
    groups.sort(key=lambda group: statistics.median(m["box"][0] for m in group))
    return groups


def _split_run(glyphs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Cut one text run into lines, and each line where a gap wider than a
    few spaces separates cells.

    A print driver may coalesce neighbouring cells on one baseline into one
    object, and a PostScript print may put a whole column of cells into one
    object line by line; the spreadsheet never puts that much space inside one
    cell's text, and a wrapped cell is reassembled later from its lines.
    """
    lines: list[list[dict[str, Any]]] = []
    for glyph in sorted(glyphs, key=lambda g: ((g["box"][1] + g["box"][3]) / 2, g["box"][0])):
        centre = (glyph["box"][1] + glyph["box"][3]) / 2
        # A comma is two points tall and a bad font metric can be forty: the
        # line's typical glyph height sets the reach.
        if lines and abs(centre - _line_centre(lines[-1])) <= 0.5 * max(
            statistics.median(g["box"][3] - g["box"][1] for g in lines[-1]), 1.0
        ) + 1.0:
            lines[-1].append(glyph)
        else:
            lines.append([glyph])
    pieces: list[list[dict[str, Any]]] = []
    for line in lines:
        ordered = sorted(line, key=lambda g: g["box"][0])
        current = [ordered[0]]
        for previous, glyph in zip(ordered, ordered[1:]):
            height = max(previous["box"][3] - previous["box"][1], 1.0)
            if glyph["box"][0] - previous["box"][2] > max(6.0, 1.6 * height):
                pieces.append(current)
                current = [glyph]
            else:
                current.append(glyph)
        pieces.append(current)
    return pieces


def _line_centre(line: list[dict[str, Any]]) -> float:
    return sum((g["box"][1] + g["box"][3]) / 2 for g in line) / len(line)


def _join_superscripts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A tiny raised run right after a run is a footnote mark of that cell."""
    taken: set[int] = set()
    for mark in records:
        text = ordered_text(mark["glyphs"]).strip()
        height = mark["box"][3] - mark["box"][1]
        if len(text) > 2 or not text or height <= 0:
            continue
        hosts = [
            other
            for other in records
            if other is not mark
            and id(other) not in taken
            and (other["box"][3] - other["box"][1]) >= 1.3 * height
            and (-1.0 <= mark["box"][0] - other["box"][2] <= 2.5 or -1.0 <= other["box"][0] - mark["box"][2] <= 2.5)
            and other["box"][1] <= mark["box"][3] <= other["box"][3] + 1.0
            and mark["box"][1] < other["box"][1] + 0.5 * (other["box"][3] - other["box"][1])
        ]
        if hosts:
            host = min(hosts, key=lambda o: min(abs(mark["box"][0] - o["box"][2]), abs(o["box"][0] - mark["box"][2])))
            # Set the mark on the host's line so it reads after the host's
            # last glyph rather than as a line of its own above it.
            for glyph in mark["glyphs"]:
                for key in ("display_box", "ink_display_box"):
                    if key in glyph:
                        box = list(glyph[key])
                        glyph[key] = [box[0], host["box"][1], box[2], host["box"][3]]
            host["glyphs"] = host["glyphs"] + mark["glyphs"]
            host["box"] = _union(host["glyphs"])
            taken.add(id(mark))
    return [r for r in records if id(r) not in taken]


def _join_adjacent(records: list[dict[str, Any]], lines: list[list[float]]) -> list[dict[str, Any]]:
    """Runs on one line no farther apart than a space are words of one cell.

    PostScript prints often draw every word as its own run. Cells of one row
    sit at least a cell's padding apart, and a vertical rule between two runs
    keeps them apart whatever the gap.
    """
    vertical = [line for line in lines if abs(line[0] - line[2]) < 0.1]
    ordered = sorted(records, key=lambda r: (r["box"][1], r["box"][0]))
    taken: set[int] = set()
    for left in ordered:
        if id(left) in taken:
            continue
        current = left
        while True:
            centre = (current["box"][1] + current["box"][3]) / 2
            # The reach is a fraction of the line's height, taken from the
            # taller run: a word of x-height letters is no shorter a line. A
            # sentence of prose may end in two spaces; a cell never does.
            candidates = [
                other
                for other in ordered
                if other is not current
                and id(other) not in taken
                and _same_line(current, other)
                and -0.5 <= other["box"][0] - current["box"][2] <= _reach(current, other)
                and not any(
                    current["box"][2] - 0.5 < line[0] < other["box"][0] + 0.5
                    and min(line[1], line[3]) <= centre <= max(line[1], line[3])
                    for line in vertical
                )
            ]
            if not candidates:
                break
            right = min(candidates, key=lambda o: o["box"][0])
            current["glyphs"] = current["glyphs"] + right["glyphs"]
            current["box"] = _union(current["glyphs"])
            taken.add(id(right))
    return [r for r in records if id(r) not in taken]


def _reach(left: dict[str, Any], right: dict[str, Any]) -> float:
    """How far apart two runs on one line may sit and still be one cell."""
    height = max(left["box"][3] - left["box"][1], right["box"][3] - right["box"][1])
    return height if _prose(left, right) else max(2.5, 0.55 * height)


def _prose(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """A sentence closed by punctuation and followed by more words: a footnote, not two cells."""
    first = ordered_text(left["glyphs"]).strip()
    second = ordered_text(right["glyphs"]).strip()
    if not first or not second or first[-1] not in ".:;!?" or not second[0].isalpha():
        return False
    both = first + " " + second
    return len(both) >= 40 and len(both.split()) >= 6


def _join_stacked_lines(records: list[dict[str, Any]], lines: list[list[float]]) -> list[dict[str, Any]]:
    """Lines of one wrapped cell drawn as separate runs become one cell.

    Two runs stack into one cell when they share a left edge, follow at one
    line's pitch, have no horizontal rule drawn between them, and either lie
    in the same rule frame or the lower line holds nothing else. Rows of a
    ruled table have a rule between them; rows of a borderless list have
    values beside their labels. A wrapped cell has neither.
    """
    ys = clustered([line[1] for line in lines if abs(line[1] - line[3]) < 0.1])
    vertical = [line for line in lines if abs(line[0] - line[2]) < 0.1]
    ordered = sorted(records, key=lambda r: (r["box"][1], r["box"][0]))
    taken: set[int] = set()

    def table_row(frame: tuple[float, float]) -> bool:
        """A frame is a table row only when vertical rules cross it; two
        distant underlines around a block of lines frame nothing."""
        centre = (frame[0] + frame[1]) / 2
        return any(min(line[1], line[3]) <= centre <= max(line[1], line[3]) for line in vertical)

    def alone(other: dict[str, Any], current: dict[str, Any]) -> bool:
        top, bottom = other["box"][1], other["box"][3]
        return not any(
            r is not other and r is not current and id(r) not in taken and top <= (r["box"][1] + r["box"][3]) / 2 <= bottom
            for r in ordered
        )

    for upper in ordered:
        if id(upper) in taken:
            continue
        current = upper
        while True:
            last_line = _line_height(current)
            frame = _rule_frame((current["box"][1] + current["box"][3]) / 2, ys)
            if frame is not None and not table_row(frame):
                frame = None
            candidates = []
            for other in ordered:
                if other is current or id(other) in taken:
                    continue
                gap = other["box"][1] - current["box"][3]
                if not (-0.5 <= gap <= 0.6 * last_line):
                    continue
                if any(current["box"][3] - 0.5 <= y <= other["box"][1] + 0.5 for y in ys):
                    continue
                other_centre = (other["box"][1] + other["box"][3]) / 2
                framed = frame is not None and frame[0] <= other_centre <= frame[1]
                # Wrapped lines share the cell's alignment edge. Inside a ruled
                # row that edge may be the left, the centre or the right; in
                # the open, only left-aligned lines that both stand alone
                # stack, since stacked centred titles are rows of their own
                # and a value beside either line marks a row of a list.
                left = abs(other["box"][0] - current["box"][0]) <= 1.5
                centred = abs((other["box"][0] + other["box"][2]) - (current["box"][0] + current["box"][2])) <= 3.0
                right = abs(other["box"][2] - current["box"][2]) <= 1.5
                if framed and (left or centred or right):
                    candidates.append(other)
                elif left and alone(other, current) and alone(current, other):
                    candidates.append(other)
            if not candidates:
                break
            below = min(candidates, key=lambda o: o["box"][1])
            current["glyphs"] = current["glyphs"] + below["glyphs"]
            current["box"] = _union(current["glyphs"])
            taken.add(id(below))
    return [r for r in records if id(r) not in taken]


def _line_height(record: dict[str, Any]) -> float:
    """The height of one line of a run, from its tallest glyph."""
    return max((g["box"][3] - g["box"][1] for g in record["glyphs"]), default=1.0)


def _join_currency(records: list[dict[str, Any]], lines: list[list[float]]) -> list[dict[str, Any]]:
    """A run that is only a currency sign belongs with the amount beside it.

    Accounting formats print the sign at the cell's left edge and the number at
    its right, as two runs. They join when they share a band and no vertical
    rule crosses that band between them; rules from other blocks of the page
    lie between them without separating them. Any other lone symbol (a bullet,
    a dash) joins only a run that starts right beside it.
    """
    vertical = [line for line in lines if abs(line[0] - line[2]) < 0.1]

    def symbol(record: dict[str, Any]) -> bool:
        text = ordered_text(record["glyphs"]).strip()
        return bool(text) and not any(ch.isalnum() for ch in text)

    def amount_like(record: dict[str, Any]) -> bool:
        text = ordered_text(record["glyphs"]).strip()
        return text == "-" or _numeric(text)

    signs = [r for r in records if symbol(r) and ordered_text(r["glyphs"]).strip() != "-"]
    taken: set[int] = set()
    for sign in signs:
        centre = (sign["box"][1] + sign["box"][3]) / 2
        currency = ordered_text(sign["glyphs"]).strip() in ("$", "€", "£")
        reach = 1e9 if currency else 8.0
        # A currency sign takes only an amount or the dash of an accounting
        # zero; a lone dash is that zero and never reaches out itself.
        partners = [
            other
            for other in records
            if other is not sign
            and id(other) not in taken
            and _same_line(sign, other)
            and -1.0 <= other["box"][0] - sign["box"][2] <= reach
            and (amount_like(other) if currency else not symbol(other))
        ]
        if not partners:
            continue
        partner = min(partners, key=lambda o: o["box"][0])
        blocked = any(
            sign["box"][2] < line[0] < partner["box"][0]
            and min(line[1], line[3]) <= centre <= max(line[1], line[3])
            for line in vertical
        )
        if blocked:
            continue
        partner["glyphs"] = sign["glyphs"] + partner["glyphs"]
        partner["box"] = _union(partner["glyphs"])
        taken.add(id(sign))
    return [r for r in records if id(r) not in taken]


def _same_line(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Either run's vertical centre lies inside the other: a dash is thin."""
    for first, second in ((a, b), (b, a)):
        centre = (first["box"][1] + first["box"][3]) / 2
        if second["box"][1] <= centre <= second["box"][3]:
            return True
    return False


def _rule_frame(centre: float, ys: list[float]) -> tuple[float, float] | None:
    """The horizontal rules directly above and below a point, if both exist."""
    above = [y for y in ys if y <= centre]
    below = [y for y in ys if y > centre]
    if not above or not below:
        return None
    return above[-1], below[0]


def _union(items: list[dict[str, Any]]) -> list[float]:
    boxes: list[list[float]] = [
        item["ink_display_box"] if "ink_display_box" in item else item["display_box"] if "display_box" in item else item["box"]
        for item in items
    ]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _cut_side(record: dict[str, Any], vertical: list[list[float]]) -> str | None:
    """Text that stops in a word within a glyph of a vertical rule was cut by
    that rule: a print that draws only what fits leaves no hidden glyphs."""
    text = ordered_text(record["glyphs"])
    if not text.strip():
        return None
    centre = (record["box"][1] + record["box"][3]) / 2
    widths = sorted(g["box"][2] - g["box"][0] for g in record["glyphs"] if g["text"].strip())
    reach = max(2.0, 1.2 * widths[len(widths) // 2]) if widths else 2.0
    crossing = [line[0] for line in vertical if min(line[1], line[3]) <= centre <= max(line[1], line[3])]
    if text.rstrip()[-1].isalnum() and any(record["box"][2] - 0.5 <= x <= record["box"][2] + reach for x in crossing):
        return "right"
    if text.lstrip()[0].isalnum() and any(record["box"][0] - reach <= x <= record["box"][0] + 0.5 for x in crossing):
        return "left"
    return None


def _row_span(record: dict[str, Any], bands: list[tuple[float, float]]) -> int:
    """Rows below the record's own that its box covers substantially: half of
    the band or of the record's line, whichever is shorter."""
    span = 1
    line = _line_height(record)
    box = record.get("extent", record["box"])
    for k, (a, b) in enumerate(bands):
        overlap = min(b, box[3]) - max(a, box[1])
        if k > record["row"] and overlap >= 0.5 * min(b - a, line):
            span = k - record["row"] + 1
    return span


def _place(
    record: dict[str, Any],
    xs: list[float],
    ys: list[float],
    segments_at: dict[int, list[tuple[float, float]]],
    left_hugs: Counter[int] | None = None,
    right_hugs: Counter[int] | None = None,
) -> None:
    """Decide the rule interval a cell belongs to and how many it spans.

    Excel lets text overflow past its own cell into empty neighbours: leftwards
    for right-aligned text, rightwards for left-aligned text, so alignment
    decides the home interval. Merged cells are almost always merged and
    centred, so text centred over several intervals with no rule drawn through
    its band is one merged cell; anything else is one cell that overflows.
    Excel's tags carry no span attribute.
    """
    box = record["box"]
    first = bisect_right(xs, box[0] + 0.75) if xs else 0
    last = bisect_right(xs, box[2] - 0.75) if xs else 0
    record["interval"], record["span"] = first, 1
    if not xs or last <= first:
        # A cell inside one interval is that cell. A merged range holding a
        # short right-aligned number is indistinguishable from a row whose
        # borders are simply not drawn there, so no rule-gap inference.
        return
    centre_y = (box[1] + box[3]) / 2
    crossed = any(a <= centre_y <= b for k in range(first, last) for a, b in segments_at.get(k, []))
    left_gap = box[0] - xs[first - 1] if first >= 1 else None
    right_gap = xs[last] - box[2] if last < len(xs) else None
    # Text set against a rule at the authoring application's cell padding is
    # aligned to that rule; that decides before anything else, since overflow
    # text can end anywhere, including near another rule.
    padding = 3.5
    left_hug = left_gap is not None and left_gap <= padding
    right_hug = right_gap is not None and right_gap <= padding
    if left_hug and right_hug:
        # Both rules at the padding: side with the neighbours. Cells that end
        # at the right rule are right-aligned there; cells that start at the
        # left rule are left-aligned there. Numbers break a tie rightwards.
        rights = (right_hugs or Counter())[last] - 1
        lefts = (left_hugs or Counter())[first - 1] - 1
        if rights > lefts or (rights == lefts and _numeric(ordered_text(record["glyphs"]))):
            record["interval"] = last
        return
    if left_hug:
        return
    if right_hug:
        record["interval"] = last
        return
    centred = left_gap is not None and right_gap is not None and abs(left_gap - right_gap) <= 2.5
    if not crossed and centred:
        record["span"] = last - first + 1
        return
    if right_gap is not None and (left_gap is None or right_gap < left_gap):
        record["interval"] = last
