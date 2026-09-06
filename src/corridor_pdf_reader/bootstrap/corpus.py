"""The true-pairs corpus: one record per unique PDF/workbook pair.

The manifest in the true-pairs directory is the only source of pair identity.
Folders that file the same bytes twice are skipped through `duplicate_of`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

TRUE_PAIRS = Path(
    os.environ.get(
        "TRUE_PAIRS_ROOT",
        "/Users/bryceharmon/Desktop/utility-conflict-matrices/PDF-Spreadsheet-Pairs/true-pairs",
    )
)
HERE = Path(__file__).resolve().parent
# The sealed split for the corpus at TRUE_PAIRS_ROOT; another corpus (the
# synthetic set) carries its own split file.
PAIRS_FILE = Path(os.environ.get("TRUE_PAIRS_SPLIT", str(HERE / "pairs.json")))


@dataclass(frozen=True)
class Pair:
    key: str
    folder: str
    pdf: str
    spreadsheet: str
    tier: str
    reading: str
    pages: int
    visible_cells: int
    printed_sheets: tuple[str, ...]
    unprinted_sheets: tuple[str, ...]
    pdf_sha256: str
    book_sha256: str

    @property
    def pdf_path(self) -> Path:
        return TRUE_PAIRS / self.folder / self.pdf

    @property
    def book_path(self) -> Path:
        return TRUE_PAIRS / self.folder / self.spreadsheet

    @property
    def agency(self) -> str:
        return self.folder.split("_", 1)[0]


def _sheet_list(value: str) -> tuple[str, ...]:
    return tuple(name.strip() for name in value.split(";") if name.strip())


def load_pairs(root: Path = TRUE_PAIRS) -> list[Pair]:
    pairs: list[Pair] = []
    with open(root / "MANIFEST.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["duplicate_of"]:
                continue
            key = hashlib.sha256(
                (row["pdf_sha256"] + row["book_sha256"]).encode()
            ).hexdigest()[:16]
            pairs.append(
                Pair(
                    key=key,
                    folder=row["folder"],
                    pdf=row["pdf"],
                    spreadsheet=row["spreadsheet"],
                    tier=row["tier"],
                    reading=row["reading"],
                    pages=int(row["pdf_pages"]),
                    visible_cells=int(row["visible_cells"]),
                    printed_sheets=_sheet_list(row["printed_sheets"]),
                    unprinted_sheets=_sheet_list(row["unprinted_sheets"]),
                    pdf_sha256=row["pdf_sha256"],
                    book_sha256=row["book_sha256"],
                )
            )
    keys = [pair.key for pair in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("pair keys collide; the manifest lists one pair twice")
    return pairs


def load_split() -> dict[str, dict[str, object]]:
    """The sealed split written by `bootstrap.split`, keyed by pair key.

    Only pairs present in the current corpus root are returned, so pointing
    `TRUE_PAIRS_ROOT` at a nested subset keeps the same keys and holdout flags.
    """
    records = json.loads(PAIRS_FILE.read_text())["pairs"]
    present = {pair.key for pair in load_pairs()}
    return {record["key"]: record for record in records if record["key"] in present}
