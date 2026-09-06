"""Score one run against the reference: align, classify every cell, summarize.

The reader never saw the workbook; this module is the only place the two meet.
A page passes only when every reference cell it covers is exact, every reader
cell lands on a reference cell, spans agree, and every string outside a table is
a header or footer. Anything else is a named failure class, never a percentage.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from corridor_pdf_reader.bootstrap.corpus import load_split

ERROR_CLASSES = (
    "value_mismatch",
    "missing_value",
    "missing_cell",
    "missing_row",
    "extra_value",
    "unaligned_row",
    "unaligned_table",
    "span_mismatch",
    "merged_cells",
    "unverifiable",
    "outside_table",
    "unexplained_text",
)
FOLD = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "­": "",
        "ﬁ": "fi",
        "ﬂ": "fl",
    }
)
RARE_KEY_ROWS = 200


def compact(text: str) -> str:
    return "".join(unicodedata.normalize("NFC", text).split())


def folded(key: str) -> str:
    return key.translate(FOLD).casefold()


def numeric_key(key: str) -> str | None:
    """Canonical number behind a display string, or None when it is not one."""
    text = key.replace(",", "").replace("$", "")
    if text in ("-", ""):
        return "0" if key else None
    negative = (text.startswith("(") and text.endswith(")")) or text.startswith("-") or text.endswith("-")
    text = text.strip("()+-")
    percent = text.endswith("%")
    text = text.rstrip("%")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", text):
        return None
    value = float(text)
    value = value / 100 if percent else value
    value = -value if negative else value
    canonical = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if canonical in ("", "-0") else canonical


class Sheet:
    """One printed worksheet as the scorer needs it."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.name: str = data["name"]
        self.display: dict[tuple[int, int], str] = {}
        self.keys: dict[tuple[int, int], str] = {}
        self.rows: dict[int, dict[int, str]] = defaultdict(dict)
        self.key_rows: dict[str, set[int]] = defaultdict(set)
        self.key_count: Counter[str] = Counter()
        for r, c, display, _kind in data["cells"]:
            key = compact(display)
            if not key:
                continue
            self.display[r, c] = display
            self.keys[r, c] = key
            self.rows[r][c] = key
            self.key_rows[key].add(r)
            self.key_count[key] += 1
        self.merged: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        self.covered: set[tuple[int, int]] = set()
        self.anchor_of: dict[tuple[int, int], int] = {}
        for r1, c1, r2, c2 in data.get("merged", []):
            self.merged[r1, c1] = (r1, c1, r2, c2)
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    self.anchor_of[r, c] = c1
                    if (r, c) != (r1, c1):
                        self.covered.add((r, c))
        self.header_footer: list[str] = data.get("header_footer", [])
        self.uncached: set[tuple[int, int]] = {(r, c) for r, c in data.get("uncached", [])}
        self.outside_keys: list[str] = [compact(text) for text in data.get("outside_text", []) if compact(text)]
        titles = data.get("print_titles")
        self.title_rows: set[int] = set(range(titles[0], titles[1] + 1)) if titles else set()
        self.sorted_rows = sorted(self.rows)


DATE_CODE = r"(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|[A-Za-z]+\d{1,2},\d{4}|\d{1,2}-?[A-Za-z]{3}-?\d{2,4}|[A-Za-z]+,[A-Za-z]+\d{1,2},\d{4})"
TIME_CODE = r"\d{1,2}:\d{2}(?::\d{2})?(?:[AaPp][Mm])?"


def header_footer_patterns(sheets: list[Sheet]) -> list[re.Pattern[str]]:
    """Regexes for what a header or footer section prints, in compact form.

    Codes become what they print: page numbers, a date, a time, a sheet name;
    only file-name and path codes stay open-ended. A section made of nothing
    but codes can therefore never explain arbitrary cell text.
    """
    names = "|".join(re.escape(compact(sheet.name)) for sheet in sheets) or "X"
    # A lone page number in the margin is pagination whether or not the
    # workbook still carries the footer template that printed it.
    patterns = [re.compile(r"(?:Page)?\d+(?:of\d+)?")]
    for sheet in sheets:
        for template in sheet.header_footer:
            for section in re.split(r"&[LCR]", template):
                section = re.sub(r'&"[^"]*"', "", section)
                section = re.sub(r"&K[0-9A-Fa-f]{6}", "", section)
                section = re.sub(r"&\d+", "", section)
                section = re.sub(r"&[BIUSEXYHG]", "", section)
                pattern = ""
                index = 0
                while index < len(section):
                    if section.startswith("&&", index):
                        pattern += re.escape("&")
                        index += 2
                    elif section[index] == "&" and index + 1 < len(section):
                        code = section[index + 1]
                        pattern += {
                            "P": r"\d+",
                            "N": r"\d+",
                            "D": DATE_CODE,
                            "T": TIME_CODE,
                            "A": f"(?:{names})",
                        }.get(code, r".+?")
                        index += 2
                    elif section[index].isspace():
                        index += 1
                    else:
                        pattern += re.escape(section[index])
                        index += 1
                if pattern:
                    patterns.append(re.compile(pattern))
    return patterns


def best_chain(options: list[list[tuple[int, float]]], strict: bool = True) -> dict[int, int]:
    """Heaviest mapping i -> r increasing in i and, strictly or not, in r.

    options[i] lists (r, weight) candidates for reader row i. Weighted longest
    increasing subsequence over a Fenwick tree of prefix maxima. Non-strict
    lets several reader columns refine one sheet column.
    """
    positions = sorted({r for candidates in options for r, _ in candidates})
    index = {r: k + 1 for k, r in enumerate(positions)}
    size = len(positions)
    tree_value = [0.0] * (size + 1)
    tree_pointer: list[tuple[int, int] | None] = [None] * (size + 1)

    def query(k: int) -> tuple[float, tuple[int, int] | None]:
        # Ties go to the smaller position: of two equal chains the one ending
        # on the earlier sheet row is the print's natural continuation.
        best: float = 0.0
        pointer: tuple[int, int] | None = None
        while k > 0:
            value, candidate = tree_value[k], tree_pointer[k]
            earlier = candidate is not None and pointer is not None and candidate[1] < pointer[1]
            if value > best or (value == best and earlier):
                best, pointer = value, candidate
            k -= k & -k
        return best, pointer

    def update(k: int, value: float, pointer: tuple[int, int]) -> None:
        while k <= size:
            existing = tree_pointer[k]
            if value > tree_value[k] or (value == tree_value[k] and existing is not None and pointer[1] < existing[1]):
                tree_value[k], tree_pointer[k] = value, pointer
            k += k & -k

    back: dict[tuple[int, int], tuple[int, int] | None] = {}
    for i, candidates in enumerate(options):
        pending = []
        for r, weight in sorted(candidates, key=lambda item: -item[0]):
            previous, pointer = query(index[r] - 1 if strict else index[r])
            back[i, r] = pointer
            pending.append((index[r], previous + weight, (i, r)))
        for k, value, pointer in pending:
            update(k, value, pointer)
    chain: dict[int, int] = {}
    _, pointer = query(size)
    while pointer is not None:
        chain[pointer[0]] = pointer[1]
        pointer = back[pointer]
    return chain


def align_block(
    reader: list[tuple[int, set[str]]],
    sheet: list[tuple[int, set[str]]],
    prefer: str = "early",
) -> dict[int, int]:
    """Order-preserving alignment of a short gap, by key overlap.

    Ties between identical rows go to the sheet rows nearest the anchor the gap
    hangs from: the earliest rows for a gap after an anchor, the latest for a
    gap before one.
    """
    n, m = len(reader), len(sheet)
    if not n or not m:
        return {}

    def similarity(a: int, b: int) -> float:
        keys_a, keys_b = reader[a - 1][1], sheet[b - 1][1]
        union = len(keys_a | keys_b)
        overlap = len(keys_a & keys_b) / union if union else 0.0
        if overlap <= 0:
            return 0.0
        nudge = (m - b) if prefer == "early" else b
        return overlap + 0.001 + nudge * 1e-6

    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for a in range(1, n + 1):
        for b in range(1, m + 1):
            score[a][b] = max(score[a - 1][b - 1] + similarity(a, b), score[a - 1][b], score[a][b - 1])
    mapping: dict[int, int] = {}
    a, b = n, m
    while a > 0 and b > 0:
        gain = similarity(a, b)
        if gain > 0 and score[a][b] == score[a - 1][b - 1] + gain:
            mapping[reader[a - 1][0]] = sheet[b - 1][0]
            a, b = a - 1, b - 1
        elif score[a][b] == score[a - 1][b]:
            a -= 1
        else:
            b -= 1
    return mapping


def table_rows(table: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cell in table["cells"]:
        rows[cell["row"]].append(cell)
    return rows


def align_rows(
    rows: dict[int, list[dict[str, Any]]], sheet: Sheet, covered: set[int] | None = None
) -> dict[int, int]:
    """Reader row index -> sheet row, anchored by rare keys, gaps filled in order.

    `covered` holds sheet rows earlier pages already matched; a gap of
    look-alike rows prefers rows still unmatched, since a print continues
    where the previous page stopped, and repeated title rows are always free.
    """
    taken = (covered or set()) - sheet.title_rows
    reader_keys: dict[int, set[str]] = {}
    options: list[list[tuple[int, float]]] = []
    ordered = sorted(rows)
    for i in ordered:
        keys = {compact(cell["text"]) for cell in rows[i]} - {""}
        reader_keys[i] = keys
        weights: dict[int, float] = defaultdict(float)
        for key in keys:
            candidates = sheet.key_rows.get(key)
            if not candidates or len(candidates) > RARE_KEY_ROWS:
                continue
            weight = 1.0 / len(candidates)
            for r in candidates:
                weights[r] += weight
        # A look-alike row already matched on an earlier page loses ties to a
        # fresh one and, with two candidates, stops being an anchor.
        options.append([(r, weight * (0.9 if r in taken else 1.0)) for r, weight in weights.items()])
    chain = best_chain(options)
    anchors: dict[int, int] = {}
    for position, i in enumerate(ordered):
        target = chain.get(position)
        if target is None:
            continue
        if dict(options[position])[target] >= 0.5:
            anchors[i] = target
    mapping = dict(anchors)
    anchor_positions = [ordered.index(i) for i in sorted(anchors)]
    bounds = [-1] + anchor_positions + [len(ordered)]
    for left, right in zip(bounds, bounds[1:]):
        gap = [ordered[p] for p in range(left + 1, right) if reader_keys[ordered[p]]]
        if not gap:
            continue
        low: int | None = anchors[ordered[left]] + 1 if left >= 0 else None
        high: int | None = anchors[ordered[right]] - 1 if right < len(ordered) else None
        if low is None and high is None:
            window = list(sheet.sorted_rows)
        elif low is None:
            window = [r for r in sheet.sorted_rows if high is not None and r <= high][-(3 * len(gap) + 5) :]
        elif high is None:
            window = [r for r in sheet.sorted_rows if r >= low][: 3 * len(gap) + 5]
        else:
            window = [r for r in sheet.sorted_rows if low <= r <= high]
        fresh = [r for r in window if r not in taken]
        if len(fresh) >= len(gap):
            window = fresh
        block = align_block(
            [(i, reader_keys[i]) for i in gap],
            [(r, set(sheet.rows[r].values())) for r in window],
            prefer="late" if low is None and high is not None else "early",
        )
        mapping.update(block)
    # Rows no key could place: between two placed rows, as many unplaced
    # reader rows as unplaced sheet rows correspond in order, and a row that
    # is only the hashes of a too-narrow column takes the sheet row next to
    # its placed neighbour, so the hashes can stand for that row's value.
    placed = sorted(mapping)
    pairs = [(placed[k], placed[k + 1]) for k in range(len(placed) - 1)]
    for i_a, i_b in pairs:
        between = [i for i in ordered if i_a < i < i_b and reader_keys[i] and i not in mapping]
        rows_between = [r for r in sheet.sorted_rows if mapping[i_a] < r < mapping[i_b] and r not in taken]
        if between and len(between) == len(rows_between):
            mapping.update(zip(between, rows_between, strict=True))
    # A page of look-alike rows (a column of zeros) anchors nothing: its rows
    # follow the print's order through the sheet rows still unmatched whose
    # values hold each reader row's keys.
    if not mapping and taken:
        cursor = 0
        fresh_rows = [r for r in sheet.sorted_rows if r not in taken]
        for i in ordered:
            if not reader_keys[i]:
                continue
            for k in range(cursor, len(fresh_rows)):
                if reader_keys[i] == set(sheet.rows[fresh_rows[k]].values()):
                    mapping[i] = fresh_rows[k]
                    cursor = k + 1
                    break
    hashes = [i for i in ordered if i not in mapping and reader_keys[i] and all(set(key) == {"#"} for key in reader_keys[i])]
    if placed:
        first, last = placed[0], placed[-1]
        before = [r for r in sheet.sorted_rows if r < mapping[first] and r not in taken]
        for i in reversed([i for i in hashes if i < first]):
            if before:
                mapping[i] = before.pop()
        after = [r for r in sheet.sorted_rows if r > mapping[last] and r not in taken]
        for i in [i for i in hashes if i > last]:
            if after:
                mapping[i] = after.pop(0)
    return mapping


def align_columns(
    rows: dict[int, list[dict[str, Any]]], row_map: dict[int, int], sheet: Sheet
) -> dict[int, int]:
    """Reader column -> sheet column from unambiguous votes, then positional fill."""
    votes: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for i, r in row_map.items():
        reader_counts = Counter(compact(cell["text"]) for cell in rows[i])
        sheet_row = sheet.rows[r]
        sheet_counts = Counter(sheet_row.values())
        for cell in rows[i]:
            key = compact(cell["text"])
            if not key or reader_counts[key] != 1 or sheet_counts[key] != 1:
                continue
            c = next(col for col, value in sheet_row.items() if value == key)
            # A merged cell's value may sit on any column of its range, so its
            # word counts half: enough to map a column nothing else claims,
            # never enough to outvote a plain cell.
            votes[cell["column"]][c] += 0.5 if (r, c) in sheet.merged else 1.0
    columns = sorted(votes)
    chain = best_chain([[(c, float(n)) for c, n in votes[j].items()] for j in columns], strict=False)
    mapping = {columns[position]: c for position, c in chain.items()}
    content_reader = sorted(
        {cell["column"] for i in row_map for cell in rows[i] if compact(cell["text"])}
    )
    content_sheet = sorted({c for r in row_map.values() for c in sheet.rows[r]})
    mapped = sorted(mapping)
    edges: list[tuple[int | None, int | None]] = [(None, None)]
    edges += [(j, mapping[j]) for j in mapped]
    edges.append((None, None))
    for (j1, c1), (j2, c2) in zip(edges, edges[1:]):
        gap_reader = [j for j in content_reader if (j1 is None or j > j1) and (j2 is None or j < j2)]
        gap_sheet = [c for c in content_sheet if (c1 is None or c > c1) and (c2 is None or c < c2)]
        if gap_reader and len(gap_reader) == len(gap_sheet):
            mapping.update(zip(gap_reader, gap_sheet))
    return mapping


CLIPPED = ("clipped_prefix", "clipped_suffix", "clipped_middle", "clipped_evidence", "clipped_edge", "overflow_hashes", "rounded_to_width", "epoch_zero")
ARTIFACT_SUBS = ("print_area_overflow",)


def fragment_of_cell(key: str, sheets: list["Sheet"]) -> bool:
    """A piece of some cell's text: what a page break through a wide sheet
    leaves on the continuation page."""
    return len(key) >= 3 and any(key in other for sheet in sheets for other in sheet.key_count)


def overflow_from_outside(key: str, sheets: list["Sheet"]) -> bool:
    """A printed piece of a cell the print area leaves out: Excel lets text
    overflow into the printed area from a cell beyond its edge."""
    return len(key) >= 3 and any(key in other for sheet in sheets for other in sheet.outside_keys)


def clipped_explains(ref_key: str, got_key: str, box: list[float], clipped_runs: list[dict[str, Any]]) -> bool:
    """Do the runs the reader hid, in this cell's column or on its line, make up the shortfall?"""
    if not clipped_runs or got_key not in ref_key:
        return False
    centre = (box[1] + box[3]) / 2
    nearby = [
        run
        for run in clipped_runs
        if (run["box"][2] > box[0] - 2 and run["box"][0] < box[2] + 2) or run["box"][1] <= centre <= run["box"][3]
    ]
    if not nearby:
        return False
    # Wrapped lines hide above and below; a print-area cut hides to the left
    # and right. Either way a contiguous stretch of the hidden runs, in their
    # order, joins the visible text into the cell's whole value.
    def letters(key: str) -> str:
        return "".join(ch for ch in key if ch.isalnum())

    target = letters(ref_key)
    for order in (lambda run: (run["box"][1], run["box"][0]), lambda run: run["box"][0]):
        keys = [letters(compact(run["text"])) for run in sorted(nearby, key=order)]
        for start in range(len(keys) + 1):
            for split in range(start, len(keys) + 1):
                for stop in range(split, len(keys) + 1):
                    if target == "".join(keys[start:split]) + letters(got_key) + "".join(keys[split:stop]):
                        return True
    return False


def cut_at_boundary(cell: dict[str, Any], row_cells: list[dict[str, Any]], table_right: float, wall: float | None = None, missing: int = 0) -> bool:
    """Does the cell's text end where the row's next cell, the table, or the
    column's wall (the widest of its cells) begins? Text the print could not
    have fitted before the next cell was cut, not lost: the missing letters,
    at the visible letters' width, would run into it."""
    right = cell["box"][2]
    if table_right - 4.0 <= right or (wall is not None and wall - 2.0 <= right):
        return True
    ahead = [other["box"][0] for other in row_cells if other is not cell and other["box"][0] >= right - 1.0]
    if any(start - right <= 4.0 for start in ahead):
        return True
    visible = len(compact(cell["text"]))
    if missing and visible:
        room = (min(ahead) if ahead else table_right) - right
        return missing * (right - cell["box"][0]) / visible > room + 2.0
    return False


def clipped_line_keys(clipped_runs: list[dict[str, Any]]) -> set[str]:
    """Every key the hidden runs spell: each run, and each stretch of runs
    that share a line, read left to right."""
    keys: set[str] = set()
    remaining = sorted(clipped_runs, key=lambda run: (run["box"][1], run["box"][0]))
    while remaining:
        first = remaining[0]
        centre = (first["box"][1] + first["box"][3]) / 2
        line = [run for run in remaining if run["box"][1] <= centre <= run["box"][3] or first["box"][1] <= (run["box"][1] + run["box"][3]) / 2 <= first["box"][3]]
        remaining = [run for run in remaining if run not in line]
        texts = [compact(run["text"]) for run in sorted(line, key=lambda run: run["box"][0])]
        for start in range(len(texts)):
            for stop in range(start + 1, min(len(texts), start + 8) + 1):
                keys.add("".join(texts[start:stop]))
    keys.discard("")
    return keys


def classify_mismatch(ref_display: str, got_text: str) -> str:
    """Name the mismatch; clipped text is what Excel printed of a too-short row."""
    ref_key, got_key = compact(ref_display), compact(got_text)
    if got_key and set(got_key) == {"#"} and (numeric_key(ref_key) is not None or (any(ch.isdigit() for ch in ref_key) and not any(ch.isalpha() for ch in ref_key))):
        # Excel prints hashes for a number or date wider than its column; the
        # PDF never held the value.
        return "overflow_hashes"
    if numeric_key(ref_key) is not None and numeric_key(ref_key) == numeric_key(got_key):
        return "format_only"
    if re.fullmatch(r"0?1/0?0/(19)?00", ref_key) and re.fullmatch(r"12/30/(18)?99", got_key):
        # Serial 0 is 1/0/1900 to Excel and 12/30/1899 to LibreOffice.
        return "epoch_zero"
    stored, shown = numeric_key(ref_key), numeric_key(got_key)
    if stored is not None and shown is not None and "." in ref_key:
        # Excel's General format shows as many decimals as the column is
        # wide: the print holds the stored value rounded to fewer places.
        decimals = len(got_key.split(".")[1].rstrip("%")) if "." in got_key else 0
        if len(ref_key.split(".")[1].rstrip("%")) > decimals and abs(round(float(stored), decimals) - float(shown)) < 1e-9:
            return "rounded_to_width"
    if folded(ref_key) == folded(got_key):
        return "unicode_variant"
    if ref_key in got_key:
        return "reader_has_more"
    if got_key in ref_key:
        words = ref_display.split()
        for count in range(1, len(words)):
            if compact(" ".join(words[:count])) == got_key:
                return "clipped_prefix"
            if compact(" ".join(words[count:])) == got_key:
                return "clipped_suffix"
        # Lines hidden above and below: the print shows a run of whole words
        # from the middle of the cell.
        for start in range(1, len(words)):
            for end in range(start + 1, len(words)):
                if compact(" ".join(words[start:end])) == got_key:
                    return "clipped_middle"
        return "reader_has_less"
    return "different"


def score_table(
    page_number: int,
    table_index: int,
    table: dict[str, Any],
    sheets: list[Sheet],
    outside_keys: set[str],
    covered_rows: dict[str, set[int]] | None = None,
    patterns: list[re.Pattern[str]] | None = None,
    page_height: float = 0.0,
    clipped_runs: list[dict[str, Any]] | None = None,
    page_width: float = 0.0,
) -> dict[str, Any]:
    patterns = patterns or []
    clipped_runs = clipped_runs or []

    def margin(box: list[float]) -> bool:
        return page_height > 0 and (box[1] < 0.12 * page_height or box[3] > 0.88 * page_height)

    rows = table_rows(table)
    filled_cells = [cell for cell in table["cells"] if compact(cell["text"])]
    keys_all = [compact(cell["text"]) for cell in filled_cells]
    if not keys_all:
        return {"table": table_index, "sheet": None, "errors": [], "exact": [], "skipped": "empty"}
    if all(
        margin(cell["box"]) and any(pattern.fullmatch(compact(cell["text"])) for pattern in patterns)
        for cell in filled_cells
    ):
        return {"table": table_index, "sheet": None, "errors": [], "exact": [], "skipped": "pagination"}
    hits = {sheet.name: sum(1 for key in keys_all if key in sheet.key_count) for sheet in sheets}
    sheet = max(sheets, key=lambda s: hits[s.name])
    if hits[sheet.name] == 0 and all(fragment_of_cell(key, sheets) for key in keys_all):
        # A page past a horizontal page break holds only the ends of cells
        # wider than the page: text, not cells, and all of it the workbook's.
        return {"table": table_index, "sheet": None, "errors": [], "exact": [], "skipped": "continuation", "cells": len(keys_all)}
    if hits[sheet.name] == 0:
        return {
            "table": table_index,
            "sheet": None,
            "errors": [
                {"class": "unaligned_table", "sub": "no_key_in_workbook", "page": page_number, "table": table_index, "got": text}
                for text in keys_all[:5]
            ],
            "exact": [],
            "cells": len(keys_all),
        }
    already = covered_rows.setdefault(sheet.name, set()) if covered_rows is not None else set()

    def pagination(cells: list[dict[str, Any]]) -> bool:
        """A lone cell in the page's margin that reads like the workbook's
        header or footer is pagination, not sheet content."""
        filled = [cell for cell in cells if compact(cell["text"])]
        return len(filled) == 1 and margin(filled[0]["box"]) and any(
            pattern.fullmatch(compact(filled[0]["text"])) for pattern in patterns
        )

    row_map = align_rows({i: cells for i, cells in rows.items() if not pagination(cells)}, sheet, already)
    # A page number in the margin never anchors a row, but a sheet cell that
    # holds one is a cell all the same: it takes the one sheet row between its
    # neighbours' rows that holds nothing but that value.
    for i in sorted(rows):
        if i in row_map or not pagination(rows[i]):
            continue
        key = compact(next(cell["text"] for cell in rows[i] if compact(cell["text"])))
        before = [row_map[j] for j in row_map if j < i]
        after = [row_map[j] for j in row_map if j > i]
        floor, ceiling = (max(before) if before else -1), (min(after) if after else 10**9)
        lone = [
            r
            for r in sheet.sorted_rows
            if floor < r < ceiling and r not in already and list(sheet.rows[r].values()) == [key]
        ]
        if lone:
            row_map[i] = lone[0]
    already.update(row_map.values())
    col_map = align_columns(rows, row_map, sheet)
    mapped_sheet_columns = set(col_map.values())
    errors: list[dict[str, Any]] = []
    exact: list[tuple[str, int, int]] = []
    table_keys = set(keys_all)

    def error(cls: str, sub: str, **fields: Any) -> None:
        errors.append({"class": cls, "sub": sub, "page": page_number, "table": table_index, "sheet": sheet.name, **fields})

    def where(key: str, r: int) -> str:
        if key in outside_keys:
            return "outside_table"
        if key in table_keys:
            return "in_table_elsewhere"
        return "absent"

    unreported_merges: list[int] = []
    absorbed: set[tuple[int, int]] = set()
    matched_rows = sorted(row_map)
    table_right = max((cell["box"][2] for cell in filled_cells), default=0.0)
    table_left = min((cell["box"][0] for cell in filled_cells), default=0.0)
    # A column whose text is clipped to its width leaves several cells ending
    # at the same edge: that edge is the column's wall.
    walls: dict[int, float] = {}
    for column in {cell["column"] for cell in filled_cells}:
        rights = sorted(cell["box"][2] for cell in filled_cells if cell["column"] == column)
        if len(rights) >= 3 or (len(rights) == 2 and rights[-1] - rights[-2] <= 2.0):
            walls[column] = rights[-1]
    row_of_cell: dict[tuple[int, int], dict[str, Any]] = {}
    for i, cells in rows.items():
        for cell in cells:
            for di in range(cell["row_span"]):
                for dj in range(cell["column_span"]):
                    row_of_cell[i + di, cell["column"] + dj] = cell
    for i in sorted(rows):
        r = row_map.get(i)
        cells = rows[i]
        if r is None:
            filled = [cell for cell in cells if compact(cell["text"])]
            if pagination(cells):
                continue
            # A row taller than the page continues on the next: the leading
            # cells of a page are the ends of the previous page's last row, and
            # the trailing cells the starts of the next page's first.
            body_matched = [j for j in matched_rows if row_map[j] not in sheet.title_rows]
            if filled and body_matched and (i < body_matched[0] or i > body_matched[-1]):
                leading = i < body_matched[0]
                edge = row_map[body_matched[0] if leading else body_matched[-1]]
                near = [rr for rr in sheet.sorted_rows if (rr < edge if leading else rr > edge)]
                near = near[-3:] if leading else near[:3]
                split_row = None
                for rr in near:
                    found: dict[int, int] = {}
                    for cell in filled:
                        columns = [
                            c
                            for c in sheet.rows[rr]
                            if c not in found.values()
                            and classify_mismatch(sheet.display[rr, c], cell["text"]) == ("clipped_suffix" if leading else "clipped_prefix")
                        ]
                        if columns:
                            found[id(cell)] = columns[0]
                    if len(found) == len(filled):
                        split_row = (rr, found)
                        break
                if split_row is not None:
                    rr, found = split_row
                    for cell in filled:
                        c = found[id(cell)]
                        error("value_mismatch", "clipped_suffix" if leading else "clipped_prefix", r=rr, c=c, i=i, j=cell["column"], ref=sheet.display[rr, c][:80], got=cell["text"][:80])
                    continue
            # Text the workbook never held, printed before the first matched
            # row or after the last, is prose around the table (a document's
            # title block, a footer); it is noted, and the gate ignores it.
            prose = bool(matched_rows) and (i < matched_rows[0] or i > matched_rows[-1]) and not any(
                compact(cell["text"]) in other.key_count for cell in filled for other in sheets
            )
            for cell in cells:
                key = compact(cell["text"])
                if key and overflow_from_outside(key, sheets):
                    error("unverifiable", "print_area_overflow", r=None, c=None, i=i, j=cell["column"], got=cell["text"][:80])
                elif key and prose:
                    error("prose_row", "outside_workbook", r=None, c=None, i=i, j=cell["column"], got=cell["text"][:80])
                elif key:
                    error(
                        "unaligned_row",
                        "misplaced" if key in sheet.key_count else "not_in_workbook",
                        r=None, c=None, i=i, j=cell["column"], got=cell["text"][:80],
                    )
            continue
        sheet_row = sheet.rows[r]
        judged: set[int] = set()
        for got_cell in cells:
            got_key = compact(got_cell["text"])
            # Several reader columns may refine one sheet column, so a span
            # over them is still one sheet column.
            spanned = list(
                dict.fromkeys(
                    col_map[got_cell["column"] + dj]
                    for dj in range(got_cell["column_span"])
                    if got_cell["column"] + dj in col_map
                )
            )
            # A merged range is one cell: a reader cell on any of its columns
            # is that cell, so its columns resolve to the range's anchor. An
            # empty reader cell inside a range says nothing about the range;
            # the cell that carries the text answers for it.
            anchored = list(dict.fromkeys(sheet.anchor_of.get((r, c), c) for c in spanned))
            if not got_key:
                plain = [c for c in spanned if (r, c) not in sheet.anchor_of]
                judged.update(plain)
                anchored = plain
            else:
                judged.update(anchored)
                if any((r, c) in sheet.anchor_of for c in spanned):
                    anchor_columns = {
                        col
                        for c in spanned
                        if (r, c) in sheet.anchor_of
                        for col in range(sheet.merged[r, sheet.anchor_of[r, c]][1], sheet.merged[r, sheet.anchor_of[r, c]][3] + 1)
                    } if all((r, sheet.anchor_of[r, c]) in sheet.merged for c in spanned if (r, c) in sheet.anchor_of) else set()
                    if anchor_columns and not anchor_columns <= set(spanned):
                        unreported_merges.append(r)
            refs = [c for c in anchored if c in sheet_row and (r, c) not in sheet.covered]
            if not refs and got_key and set(got_key) == {"#"}:
                # Hashes vote for no column, so their column stays unmapped;
                # they stand for the row's next number or date still unjudged.
                numeric = [
                    c
                    for c in sorted(sheet_row)
                    if c not in judged and (r, c) not in sheet.covered and classify_mismatch(sheet.display[r, c], got_cell["text"]) == "overflow_hashes"
                ]
                if numeric:
                    judged.add(numeric[0])
                    error("value_mismatch", "overflow_hashes", r=r, c=numeric[0], i=i, j=got_cell["column"], ref=sheet.display[r, numeric[0]][:80], got=got_cell["text"][:80])
                    continue
            if not refs:
                if got_key and any((r, c) in sheet.uncached for c in spanned):
                    error("unverifiable", "uncached_formula", r=r, c=spanned[0], i=i, j=got_cell["column"], got=got_cell["text"][:80])
                elif got_key and overflow_from_outside(got_key, sheets):
                    error("unverifiable", "print_area_overflow", r=r, c=spanned[0] if spanned else None, i=i, j=got_cell["column"], got=got_cell["text"][:80])
                elif got_key:
                    # The row holds this text in a column no reader column
                    # maps: the print never showed that column beside its
                    # neighbour, so the reader folded the two into one. The
                    # cell is read whole and in its row; the fold is noted.
                    unmapped = [
                        c
                        for c, k in sheet_row.items()
                        if k == got_key and (r, c) not in sheet.covered and c not in judged and (not spanned or c not in mapped_sheet_columns or len(sheet_row) == 1)
                    ]
                    if unmapped:
                        judged.add(unmapped[0])
                        exact.append((sheet.name, r, unmapped[0]))
                        error("columns_folded", "unmapped_column", r=r, c=unmapped[0], i=i, j=got_cell["column"], ref=sheet.display[r, unmapped[0]][:80], got=got_cell["text"][:80])
                        continue
                    if got_key in sheet_row.values():
                        sub = "wrong_column"
                    elif any(got_key in sheet.rows[rr].values() for rr in (r - 1, r + 1, r - 2, r + 2) if rr in sheet.rows):
                        sub = "wrong_row"
                    elif got_key in sheet.key_count:
                        sub = "misplaced"
                    else:
                        sub = "not_in_workbook"
                    error("extra_value", sub, r=r, c=spanned[0] if spanned else None, i=i, j=got_cell["column"], got=got_cell["text"][:80])
                continue
            if len(refs) > 1:
                joined = "".join(sheet_row[c] for c in refs)
                error(
                    "merged_cells",
                    "text_intact" if got_key == joined else "text_differs",
                    r=r, c=refs[0], i=i, j=got_cell["column"], ref=" | ".join(sheet.display[r, c][:40] for c in refs), got=got_cell["text"][:80], columns=refs,
                )
                continue
            c = refs[0]
            ref_key = sheet_row[c]
            ref_display = sheet.display[r, c]
            if not got_key:
                error("missing_value", where(ref_key, r), r=r, c=c, i=i, j=got_cell["column"], ref=ref_display[:80])
                continue
            if got_key != ref_key:
                # No rule was drawn between this cell and its neighbour above
                # or below, so the print shows their texts as one cell.
                neighbour = None
                for rr in (r - 1, r + 1):
                    if rr in sheet.rows and c in sheet.rows[rr] and (rr, c) not in sheet.covered:
                        other = sheet.rows[rr][c]
                        if got_key == (other + ref_key if rr < r else ref_key + other):
                            neighbour = rr
                if neighbour is not None:
                    absorbed.add((neighbour, c))
                    exact.append((sheet.name, r, c))
                    exact.append((sheet.name, neighbour, c))
                    error("merged_rows", "text_intact", r=r, c=c, i=i, j=got_cell["column"], ref=ref_display[:80], got=got_cell["text"][:80], rows=[min(r, neighbour), max(r, neighbour)])
                    continue
                sub = classify_mismatch(ref_display, got_cell["text"])
                if sub in ("reader_has_less", *CLIPPED) and clipped_explains(ref_key, got_key, got_cell["box"], clipped_runs):
                    sub = "clipped_evidence"
                elif sub == "reader_has_less" and got_key in ref_key and (got_cell["box"][0] <= 3.0 or (page_width > 0 and got_cell["box"][2] >= page_width - 3.0)):
                    # Cut at the page edge: the print area starts or ends
                    # inside this cell's text.
                    sub = "clipped_edge"
                elif sub == "reader_has_less" and ref_key.startswith(got_key) and (got_cell.get("cut") == "right" or cut_at_boundary(got_cell, cells, table_right, walls.get(got_cell["column"]), len(ref_key) - len(got_key))):
                    # Cut mid-word at a rule, the next cell or the table's
                    # right edge: the print drew only what fitted the cell.
                    sub = "clipped_edge"
                elif sub == "reader_has_less" and ref_key.endswith(got_key) and (got_cell.get("cut") == "left" or got_cell["box"][0] <= table_left + 2.0):
                    sub = "clipped_edge"
                error("value_mismatch", sub, r=r, c=c, i=i, j=got_cell["column"], ref=ref_display[:80], got=got_cell["text"][:80])
                continue
            exact.append((sheet.name, r, c))
            anchor = sheet.merged.get((r, c))
            expected_cols = set(range(anchor[1], anchor[3] + 1)) if anchor else {c}
            expected_cols &= mapped_sheet_columns
            got_cols = set(spanned)
            # Inside a merged range any subset of its columns is the same cell;
            # only reaching outside the range, or past a plain cell, is wrong.
            if got_cols != expected_cols and not (anchor and got_cols <= expected_cols):
                error("span_mismatch", "wider" if len(got_cols) > len(expected_cols) else "narrower", r=r, c=c, i=i, j=got_cell["column"], ref=ref_display[:80], expected=sorted(expected_cols), got_columns=sorted(got_cols))
            expected_rows = set(range(anchor[0], anchor[2] + 1)) if anchor else {r}
            expected_rows &= set(row_map.values())
            got_rows = {row_map[i + di] for di in range(got_cell["row_span"]) if i + di in row_map}
            # As with columns, text inside a range merged down several rows
            # covers some of them; reaching past the range is the fault.
            if anchor and got_rows < expected_rows:
                unreported_merges.append(r)
            if got_rows != expected_rows and not (anchor and got_rows <= expected_rows):
                error("span_mismatch", "rows", r=r, c=c, i=i, j=got_cell["column"], ref=ref_display[:80], expected=sorted(expected_rows), got_rows=sorted(got_rows))
        for c in sorted(set(sheet_row) & mapped_sheet_columns):
            if c in judged or (r, c) in sheet.covered:
                continue
            error("missing_cell", where(sheet_row[c], r), r=r, c=c, i=i, j=None, ref=sheet.display[r, c][:80])
    body = set(row_map.values()) - sheet.title_rows
    if body:
        aligned = set(row_map.values())
        low, high = min(body), max(body)
        hidden = clipped_line_keys(clipped_runs)
        for r in sheet.sorted_rows:
            if r < low or r > high or r in aligned:
                continue
            wanted = [(c, key) for c, key in sheet.rows[r].items() if c in mapped_sheet_columns and (r, c) not in sheet.covered]
            if wanted and all(key in hidden for _, key in wanted):
                # Excel drew the row's text and hid it behind a row too short
                # to show it; the reader kept every hidden run.
                for c, key in wanted:
                    error("value_mismatch", "clipped_evidence", r=r, c=c, i=None, j=None, ref=sheet.display[r, c][:80], got="")
                continue
            for c, key in wanted:
                error("missing_row", where(key, r), r=r, c=c, i=None, j=None, ref=sheet.display[r, c][:80])
    errors = [
        err
        for err in errors
        if not (err["class"] in ("missing_cell", "missing_row") and (err.get("r"), err.get("c")) in absorbed)
    ]
    return {
        "table": table_index,
        "sheet": sheet.name,
        "rows_aligned": len(row_map),
        "rows_total": len(rows),
        "columns_mapped": len(col_map),
        "columns_refined": len(col_map) - len(set(col_map.values())),
        "merges_unreported": len(unreported_merges),
        "errors": errors,
        "exact": exact,
        "cells": len(keys_all),
    }


def score_pair(reference: dict[str, Any], read: dict[str, Any]) -> dict[str, Any]:
    sheets = [Sheet(data) for data in reference["sheets"]]
    patterns = header_footer_patterns(sheets)
    all_keys: Counter[str] = Counter()
    for sheet in sheets:
        all_keys.update(sheet.key_count)
    exact_hits: Counter[tuple[str, int, int]] = Counter()
    outside_seen: set[str] = set()
    mismatched: set[tuple[str, int, int]] = set()
    clipped: set[tuple[str, int, int]] = set()
    covered_rows: dict[str, set[int]] = {}
    pages_out = []
    for page in read["pages"]:
        outside_keys = {compact(item["text"]) for item in page["outside"]} - {""}
        outside_seen.update(outside_keys)
        errors: list[dict[str, Any]] = []
        height = page["size"][1] if page["rotation"] % 180 == 0 else page["size"][0]
        width = page["size"][0] if page["rotation"] % 180 == 0 else page["size"][1]
        table_results = [
            score_table(page["number"], index, table, sheets, outside_keys, covered_rows, patterns, height, page.get("clipped", []), width)
            for index, table in enumerate(page["tables"])
        ]
        continuation = all(result.get("skipped") == "continuation" for result in table_results)
        table_left = min((table["box"][0] for table in page["tables"]), default=0.0)
        # A footer drawn as several runs ("Page", "1", "of", "4") reads as
        # one line: runs in the margin whose vertical ranges overlap.
        pagination_lines: set[int] = set()
        margin_items = [
            item for item in page["outside"]
            if compact(item["text"]) and (item["box"][1] < 0.12 * height or item["box"][3] > 0.88 * height)
        ]
        for item in margin_items:
            centre = (item["box"][1] + item["box"][3]) / 2
            line = sorted((other for other in margin_items if other["box"][1] <= centre <= other["box"][3]), key=lambda other: other["box"][0])
            joined = "".join(compact(other["text"]) for other in line)
            if len(line) > 1 and any(pattern.fullmatch(joined) for pattern in patterns):
                pagination_lines.update(id(other) for other in line)
        for item in page["outside"]:
            key = compact(item["text"])
            if not key:
                continue
            box = item["box"]
            edge = box[1] < 0.12 * height or box[3] > 0.88 * height
            if edge and (id(item) in pagination_lines or any(pattern.fullmatch(key) for pattern in patterns)):
                continue
            if key in all_keys:
                errors.append({"class": "outside_table", "sub": "cell_text_outside", "page": page["number"], "got": item["text"][:80]})
            elif overflow_from_outside(key, sheets) or ((continuation or box[0] <= table_left + 2.0) and fragment_of_cell(key, sheets)):
                # The tail of a cell cut by the page break before this page
                # lands at the table's left edge.
                continue
            else:
                sub = "numeric" if numeric_key(key) is not None else ("edge" if edge else "text")
                errors.append({"class": "unexplained_text", "sub": sub, "page": page["number"], "got": item["text"][:80]})
        tables_out = []
        for result in table_results:
            for hit in result["exact"]:
                exact_hits[hit] += 1
            for err in result["errors"]:
                if err["class"] == "value_mismatch" and err["sub"] in CLIPPED:
                    clipped.add((err["sheet"], err["r"], err["c"]))
                elif err["class"] == "value_mismatch":
                    mismatched.add((err["sheet"], err["r"], err["c"]))
            errors.extend(result["errors"])
            tables_out.append({k: v for k, v in result.items() if k not in ("errors", "exact")})
        failing = [
            err
            for err in errors
            if not (err["class"] == "value_mismatch" and err["sub"] in CLIPPED) and err["class"] not in ("unverifiable", "prose_row", "columns_folded", "merged_rows")
        ]
        pages_out.append(
            {
                "number": page["number"],
                "pass": not failing,
                "tables": tables_out,
                "errors": errors,
                "counts": dict(Counter(err["class"] for err in errors)),
            }
        )
    uncovered = []
    for sheet in sheets:
        for (r, c), key in sheet.keys.items():
            if exact_hits[sheet.name, r, c] or (sheet.name, r, c) in clipped:
                continue
            if (sheet.name, r, c) in mismatched:
                sub = "value_mismatch"
            elif key in outside_seen:
                sub = "outside_table"
            else:
                sub = "absent"
            uncovered.append({"sheet": sheet.name, "r": r, "c": c, "ref": sheet.display[r, c][:80], "sub": sub})
    counts: Counter[str] = Counter()
    for page in pages_out:
        counts.update(page["counts"])
    reference_cells = sum(len(sheet.keys) for sheet in sheets)
    return {
        "key": reference["key"],
        "pass": all(page["pass"] for page in pages_out) and not uncovered and bool(pages_out),
        "pages": pages_out,
        "pages_pass": sum(1 for page in pages_out if page["pass"]),
        "reference_cells": reference_cells,
        "exact_cells": sum(1 for sheet in sheets for coord in sheet.keys if exact_hits[sheet.name, *coord]),
        "uncovered": uncovered,
        "uncovered_counts": dict(Counter(item["sub"] for item in uncovered)),
        "clipped_cells": len(clipped),
        "counts": dict(counts),
    }


def summarize(results: list[dict[str, Any]], meta: dict[str, dict[str, Any]], read_errors: dict[str, str]) -> dict[str, Any]:
    by_class: Counter[str] = Counter()
    by_sub: Counter[str] = Counter()
    pages_with: Counter[str] = Counter()
    pairs_with: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_family: dict[str, Counter[str]] = defaultdict(Counter)
    for result in results:
        family = meta[result["key"]]["family"]
        by_family[family]["pairs"] += 1
        by_family[family]["pairs_pass"] += int(result["pass"])
        by_family[family]["pages"] += len(result["pages"])
        by_family[family]["pages_pass"] += result["pages_pass"]
        by_family[family]["reference_cells"] += result["reference_cells"]
        by_family[family]["exact_cells"] += result["exact_cells"]
        seen_pair: set[str] = set()
        for page in result["pages"]:
            for cls in page["counts"]:
                pages_with[cls] += 1
            for err in page["errors"]:
                by_class[err["class"]] += 1
                sub = f"{err['class']}/{err['sub']}"
                by_sub[sub] += 1
                seen_pair.add(err["class"])
                if len(examples[sub]) < 5:
                    examples[sub].append({"pair": result["key"], "pdf": meta[result["key"]]["pdf"], **{k: v for k, v in err.items() if k not in ("class", "sub")}})
        for item in result["uncovered"]:
            sub = f"uncovered/{item['sub']}"
            by_sub[sub] += 1
            by_class["uncovered"] += 1
            seen_pair.add("uncovered")
            if len(examples[sub]) < 5:
                examples[sub].append({"pair": result["key"], "pdf": meta[result["key"]]["pdf"], **item})
        for cls in seen_pair:
            pairs_with[cls] += 1
    worst = sorted(
        results,
        key=lambda r: -(sum(r["counts"].values()) + len(r["uncovered"])),
    )[:25]
    return {
        "pairs": len(results),
        "pairs_pass": sum(1 for r in results if r["pass"]),
        "pages": sum(len(r["pages"]) for r in results),
        "pages_pass": sum(r["pages_pass"] for r in results),
        "reference_cells": sum(r["reference_cells"] for r in results),
        "exact_cells": sum(r["exact_cells"] for r in results),
        "read_errors": read_errors,
        "by_class": dict(by_class.most_common()),
        "by_subclass": dict(by_sub.most_common()),
        "pages_with_class": dict(pages_with.most_common()),
        "pairs_with_class": dict(pairs_with.most_common()),
        "by_family": {family: dict(counter) for family, counter in sorted(by_family.items())},
        "worst_pairs": [
            {
                "key": r["key"],
                "pdf": meta[r["key"]]["pdf"],
                "family": meta[r["key"]]["family"],
                "pages": len(r["pages"]),
                "errors": sum(r["counts"].values()),
                "uncovered": len(r["uncovered"]),
                "counts": r["counts"],
            }
            for r in worst
        ],
        "examples": dict(examples),
    }


def markdown(summary: dict[str, Any], run: str, holdout_included: bool) -> str:
    lines = [
        f"# Loop score: {run}",
        "",
        f"Holdout included: {holdout_included}. A pair passes only when every page passes and every reference cell is exact somewhere.",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Pairs passing | {summary['pairs_pass']} / {summary['pairs']} |",
        f"| Pages passing | {summary['pages_pass']} / {summary['pages']} |",
        f"| Reference cells exact | {summary['exact_cells']} / {summary['reference_cells']} |",
        f"| Documents the reader failed on | {len(summary['read_errors'])} |",
        "",
        "## Failure classes (cells)",
        "",
        "| Class | Cells | Pages | Pairs |",
        "|---|---:|---:|---:|",
    ]
    for cls, count in summary["by_class"].items():
        lines.append(f"| {cls} | {count} | {summary['pages_with_class'].get(cls, '')} | {summary['pairs_with_class'].get(cls, '')} |")
    lines += ["", "## Subclasses", "", "| Subclass | Cells |", "|---|---:|"]
    for sub, count in summary["by_subclass"].items():
        lines.append(f"| {sub} | {count} |")
    lines += ["", "## By producer family", "", "| Family | Pairs pass | Pages pass | Cells exact |", "|---|---:|---:|---:|"]
    for family, counter in summary["by_family"].items():
        lines.append(
            f"| {family} | {counter['pairs_pass']}/{counter['pairs']} | {counter['pages_pass']}/{counter['pages']} | {counter['exact_cells']}/{counter['reference_cells']} |"
        )
    lines += ["", "## Worst pairs", "", "| Key | PDF | Family | Pages | Errors | Uncovered |", "|---|---|---|---:|---:|---:|"]
    for item in summary["worst_pairs"]:
        lines.append(f"| {item['key']} | {item['pdf'][:50]} | {item['family']} | {item['pages']} | {item['errors']} | {item['uncovered']} |")
    lines += ["", "## Examples", ""]
    for sub, items in summary["examples"].items():
        lines.append(f"### {sub}")
        lines.append("")
        for item in items:
            lines.append("- " + json.dumps(item, ensure_ascii=False)[:300])
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="run directory holding reads/")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--include-holdout", action="store_true")
    parser.add_argument("--keys", nargs="*")
    args = parser.parse_args()
    split = load_split()
    scores = args.run / "scores"
    # Per-pair files from an earlier scoring of another corpus root must not
    # linger beside this one.
    if scores.exists():
        for stale in scores.glob("*.json"):
            stale.unlink()
    scores.mkdir(exist_ok=True)
    receipts = json.loads((args.run / "read-receipts.json").read_text())["receipts"]
    read_errors = {r["key"]: r["error"] for r in receipts if "error" in r}
    results = []
    for key, record in sorted(split.items()):
        if record["holdout"] and not args.include_holdout:
            continue
        if args.keys and key not in args.keys:
            continue
        read_path = args.run / "reads" / f"{key}.json"
        reference_path = args.reference / f"{key}.json"
        if not read_path.exists() or not reference_path.exists():
            continue
        result = score_pair(json.loads(reference_path.read_text()), json.loads(read_path.read_text()))
        (scores / f"{key}.json").write_text(json.dumps(result) + "\n")
        results.append(result)
    summary = summarize(results, split, {k: v for k, v in read_errors.items() if k in {r["key"] for r in results} or not split[k]["holdout"]})
    summary["holdout_included"] = args.include_holdout
    (args.run / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    (args.run / "SUMMARY.md").write_text(markdown(summary, args.run.name, args.include_holdout))
    print(
        f"pairs {summary['pairs_pass']}/{summary['pairs']} pass, pages {summary['pages_pass']}/{summary['pages']}, "
        f"cells exact {summary['exact_cells']}/{summary['reference_cells']} -> {args.run / 'SUMMARY.md'}"
    )


if __name__ == "__main__":
    main()
