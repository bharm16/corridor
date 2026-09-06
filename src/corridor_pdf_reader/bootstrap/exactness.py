"""Decide, per pair, whether the PDF prints the workbook as it now stands.

The test uses only extracted text, never the table reader's alignment: every
printed workbook cell must appear somewhere in the PDF's text, and every
string printed on the PDF must be explained by some workbook cell. Print
artifacts the PDF cannot avoid are excluded first: pagination, hashes for
too-wide numbers, results of formulas without a cached value, and glyph runs
shorter than three characters. A pair with a shortfall in either direction is
a different Document Revision on one side, not a reader failure.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from corridor_pdf_reader.bootstrap.corpus import load_split
from corridor_pdf_reader.bootstrap.score import Sheet, compact, header_footer_patterns, numeric_key

PAGINATION = re.compile(r"(?:Page)?\d+(?:of\d+)?|Page")
MIN_LENGTH = 3
CLIP_EVIDENCE = 5
DATE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$")


def date_key(key: str) -> str | None:
    """A printed date under any short-date format, as one comparable key."""
    match = DATE.fullmatch(key)
    if not match:
        return None
    month, day, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000 if year < 70 else 1900
    return f"{month}/{day}/{year}"


def printed_strings(page: dict[str, Any]) -> list[str]:
    strings = [cell["text"] for table in page["tables"] for cell in sorted(table["cells"], key=lambda c: (c["row"], c["column"]))]
    strings += [item["text"] for item in page["outside"]]
    return strings


def page_blobs(page: dict[str, Any]) -> list[str]:
    """Three readings of the page's text: reading order, band by band, and
    column by column, so the lines of a wrapped cell sit adjacent in one of them."""
    ordered = compact("".join(printed_strings(page)))
    boxes = [(item["box"], item["text"]) for item in page["outside"]]
    boxes += [(cell["box"], cell["text"]) for table in page["tables"] for cell in table["cells"]]
    by_band = compact("".join(text for _, _, text in sorted((round(box[1] / 4), box[0], text) for box, text in boxes)))
    by_column = compact("".join(text for _, _, text in sorted((round(box[0] / 3), box[1], text) for box, text in boxes)))
    return [ordered, by_band, by_column]


def check_pair(reference: dict[str, Any], read: dict[str, Any]) -> dict[str, Any]:
    sheets = [Sheet(data) for data in reference["sheets"]]
    patterns = header_footer_patterns(sheets)
    uncached = {(sheet.name, r, c) for sheet in sheets for r, c in sheet.uncached}
    workbook: Counter[str] = Counter()
    numbers: set[str] = set()
    dates: set[str] = set()
    for sheet in sheets:
        for (r, c), key in sheet.keys.items():
            if (sheet.name, r, c) in uncached:
                continue
            workbook[key] += 1
            number = numeric_key(key)
            if number is not None:
                numbers.add(number)
            date = date_key(key)
            if date is not None:
                dates.add(date)
    long_keys = sorted({key for key in workbook if len(key) >= MIN_LENGTH}, key=len)
    # Cells outside the print area explain what overflows into it, no more.
    overflow = [compact(text) for data in reference["sheets"] for text in data.get("outside_text", [])]
    key_blob = "\x00".join(long_keys + [key for key in overflow if len(key) >= MIN_LENGTH])
    printed: Counter[str] = Counter()
    blobs: list[str] = []
    for page in read["pages"]:
        for text in printed_strings(page):
            key = compact(text)
            if key:
                printed[key] += 1
        blobs.extend(page_blobs(page))
    all_text = "\x00".join(blobs)
    printed_dates = {date_key(key) for key in printed} - {None}
    evidence = [key for key in printed if len(key) >= CLIP_EVIDENCE]
    hashes = any(set(key) == {"#"} for key in printed)
    missing = []
    clipped = []
    for key in workbook:
        if len(key) < MIN_LENGTH or key in printed or key in all_text:
            continue
        if date_key(key) in printed_dates:
            continue
        if hashes and (numeric_key(key) is not None or date_key(key) is not None):
            # The page prints hashes somewhere: a number too wide for its
            # column, which could be this one.
            clipped.append(key)
            continue
        # Excel prints only the lines of a wrapped cell that fit its row, and
        # cuts text at a cell edge: a printed run that is a piece of the cell
        # is the print of that cell, not its absence.
        if any(run in key for run in evidence):
            clipped.append(key)
            continue
        missing.append(key)
    unexplained = []
    unverifiable = []
    for key in printed:
        if len(key) < MIN_LENGTH or key in workbook or set(key) == {"#"} or PAGINATION.fullmatch(key):
            continue
        if any(pattern.fullmatch(key) for pattern in patterns):
            continue
        number = numeric_key(key)
        if number is not None and number in numbers:
            continue
        if date_key(key) in dates:
            continue
        if key in key_blob:
            continue
        if any(other in key for other in long_keys if len(other) <= len(key)):
            continue
        if number is not None and uncached:
            # A number the workbook cannot show: it stores formulas without
            # their results, so the print may be the only copy of this value.
            unverifiable.append(key)
            continue
        unexplained.append(key)
    cells = sum(workbook.values())
    return {
        "key": reference["key"],
        "workbook_cells": cells,
        "uncached_formulas": len(uncached),
        "missing_from_pdf": len(missing),
        "clipped_in_pdf": len(clipped),
        "unexplained_in_pdf": len(unexplained),
        "unverifiable_in_pdf": len(unverifiable),
        "missing_examples": missing[:6],
        "unexplained_examples": unexplained[:6],
        "exact": not missing and not unexplained,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="run directory holding reads/")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="JSON file to write")
    args = parser.parse_args()
    split = load_split()
    results = []
    for key, record in sorted(split.items()):
        read_path = args.run / "reads" / f"{key}.json"
        reference_path = args.reference / f"{key}.json"
        if not read_path.exists() or not reference_path.exists():
            continue
        result = check_pair(json.loads(reference_path.read_text()), json.loads(read_path.read_text()))
        result.update({k: record[k] for k in ("folder", "pdf", "spreadsheet", "family", "holdout", "pages")})
        results.append(result)
    args.output.write_text(json.dumps(results, indent=1) + "\n")
    exact = [r for r in results if r["exact"]]
    print(f"{len(exact)} of {len(results)} pairs print the workbook exactly -> {args.output}")


if __name__ == "__main__":
    main()
