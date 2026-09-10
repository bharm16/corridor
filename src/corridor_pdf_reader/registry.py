"""The paired-rendition Reference Dataset, registered where every measurement can read it (#731).

The 333-pair corpus, its sealed split, its answer keys and the scorer's
taxonomy existed before this module, spread over the corpus directory's
`MANIFEST.csv` and `EXCLUDED.csv`, the imported `bootstrap/pairs.json`,
`bootstrap/README.md` and `score.py`, and the loop log. An Extraction
Measurement names its Reference Dataset with its origin, coverage, shared
dependencies and limits (docs/operations/CONTEXT.md), and a claim spread over
six files in three places cannot be named by a receipt. So the registration
is one machine-readable file, `gold/pdf-pairs/v1/dataset.json`, built from
those sources rather than retyped (`build_registration`), digested, and
cited by every receipt the measurement command writes.

Three ledgers sit beside it, in the shape `gold/pdf/v1` already uses:
`holdout-access.jsonl` (ADR-0008: every access to the spent holdout, the
three that happened before this module included, appended and never
edited; appending, reading and validating it belong to
`corridor.holdout_ledger`, which owns the one schema both spent datasets
are recorded in), `receipts.json` (the baseline receipts with their configuration
identities, referenced by path and digest, never copied) and
`receipts/<run>/` (what the measurement command retains).

The registry lives under `gold/` and not inside the package's `receipts/`
because the package is the reader imported unchanged from commit c39363e and
its receipts are that import's evidence, while the registration, the ledger
and the harness's own baselines are Corridor's, grow with every measurement,
and belong beside the other frozen evaluation asset and its holdout ledger.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from corridor import holdout_ledger
from corridor_pdf_reader import provenance

PACKAGE_ROOT = provenance.PACKAGE_ROOT
REPO_ROOT = PACKAGE_ROOT.parents[1]
REGISTRY_ROOT = REPO_ROOT / "gold" / "pdf-pairs" / "v1"
REGISTRATION = REGISTRY_ROOT / "dataset.json"
HOLDOUT_LEDGER = REGISTRY_ROOT / "holdout-access.jsonl"
RECEIPT_INDEX = REGISTRY_ROOT / "receipts.json"
RETAINED_RECEIPTS = REGISTRY_ROOT / "receipts"

DATASET_SCHEMA = "corridor.pdf-pairs-dataset.v1"
LEDGER_SCHEMA = holdout_ledger.SCHEMA
INDEX_SCHEMA = "corridor.pdf-pairs-receipts.v1"
RECEIPT_SCHEMA = "corridor.pdf-pairs-receipt.v1"
DATASET_VERSION = "2026-09-06.1"

SPLIT_FILE = PACKAGE_ROOT / "bootstrap" / "pairs.json"
KEY_MANIFEST = PACKAGE_ROOT / "receipts" / "loop-reference-v6.manifest.json"

# The issue's words. Every receipt carries this paragraph unchanged.
LIMITS = (
    "Paired renditions are the measurement method, not a prerequisite for "
    "reading a production PDF. Cell comparison does not independently "
    "establish prose outside tables, raster fidelity, routing correctness, "
    "source-locator replay, malicious-document handling, production "
    "concurrency, or operational cost and coordinator burden. Those belong "
    "to their own tickets."
)

GROUPING_RULE = (
    "Originals, clean scan twins, degraded twins and closely related "
    "revisions stay together across development and holdout. A scan twin of "
    "a development workbook is not holdout evidence."
)

HOLDOUT_POLICY = (
    "The holdout was scored at loop-007 and at loop-020, and again by the "
    "2026-09-06 reproduction inside Corridor; every access is a line of "
    "holdout-access.jsonl. Imported holdout cases are spent for the frozen "
    "reader. Re-running the frozen implementation is a reproducibility and "
    "regression check, not a new generalization claim. Changing the reader "
    "after inspecting those outcomes requires a new held-out evaluation for "
    "a new generalization claim (ADR-0008). The measurement command refuses "
    "a holdout run without an explicit flag, actor and reason, and appends "
    "every access it makes, with its result, to the ledger."
)

# What passing means, in the issue's clauses (bootstrap/README.md, score.py).
GATE = (
    "every reader cell lands on a reference cell with the same display text",
    "every reference cell in the columns the page maps is present",
    "spans agree with the workbook's merged ranges",
    "every string outside a table is header or footer text",
    "a pair passes when every page passes and every reference cell is exact on some page",
)

# Every class and subclass the scorer emits, with the meaning bootstrap/README.md
# and score.py give it and whether the gate counts it. tests/test_pdf_pairs_registry.py
# holds this table to the scorer's source, both ways.
TAXONOMY: dict[str, Any] = {
    "value_mismatch": {
        "meaning": "a reader cell aligned to a reference cell whose display text differs",
        "subclasses": {
            "different": {"failing": True, "meaning": "the reader's text is not the workbook's"},
            "format_only": {"failing": True, "meaning": "the same number shown in another format"},
            "unicode_variant": {"failing": True, "meaning": "the same text once punctuation variants and case are folded"},
            "reader_has_more": {"failing": True, "meaning": "the reader's text contains the reference text and more"},
            "reader_has_less": {"failing": True, "meaning": "the reader's text is part of the reference text and nothing on the page explains the cut"},
            "clipped_prefix": {"failing": False, "meaning": "the print shows the leading whole words of a wrapped cell Excel cut to the row height, or the start of a row split across pages"},
            "clipped_suffix": {"failing": False, "meaning": "the print shows the trailing whole words of a cut cell, or the end of a row split across pages"},
            "clipped_middle": {"failing": False, "meaning": "lines hidden above and below: the print shows whole words from the middle of the cell"},
            "clipped_evidence": {"failing": False, "meaning": "the shortfall is made up by runs the reader kept as hidden behind a clip, in the cell's column or on its line"},
            "clipped_edge": {"failing": False, "meaning": "text cut mid-word at the page edge, a rule, the next cell, the table's edge or a column wall; the reader's cut mark or the geometry proves it"},
            "overflow_hashes": {"failing": False, "meaning": "Excel printed hash marks for a number or date wider than its column; the PDF never held the value"},
            "rounded_to_width": {"failing": False, "meaning": "a General-format number the column was too narrow to show in full"},
            "epoch_zero": {"failing": False, "meaning": "serial 0 shown as 1/0/1900 by Excel and 12/30/1899 by LibreOffice"},
        },
    },
    "missing_value": {
        "meaning": "a reader cell aligned to a reference cell holds no text",
        "subclasses": {
            "outside_table": {"failing": True, "meaning": "the text is on the page, outside every table"},
            "in_table_elsewhere": {"failing": True, "meaning": "the text appears in another cell of the table"},
            "absent": {"failing": True, "meaning": "the text is nowhere on the page"},
        },
    },
    "missing_cell": {
        "meaning": "a reference cell in a mapped column has no reader cell in its aligned row",
        "subclasses": {
            "outside_table": {"failing": True, "meaning": "the text is on the page, outside every table"},
            "in_table_elsewhere": {"failing": True, "meaning": "the text appears in another cell of the table"},
            "absent": {"failing": True, "meaning": "the text is nowhere on the page"},
        },
    },
    "missing_row": {
        "meaning": "a reference row inside the table's aligned span has no reader row",
        "subclasses": {
            "outside_table": {"failing": True, "meaning": "the row's text is on the page, outside every table"},
            "in_table_elsewhere": {"failing": True, "meaning": "the row's text appears in another cell of the table"},
            "absent": {"failing": True, "meaning": "the row's text is nowhere on the page"},
        },
    },
    "extra_value": {
        "meaning": "a reader cell in an aligned row whose text no reference cell of the mapped columns holds",
        "subclasses": {
            "wrong_column": {"failing": True, "meaning": "the row holds the text in a column the reader placed it outside of"},
            "wrong_row": {"failing": True, "meaning": "a row within two rows above or below holds the text"},
            "misplaced": {"failing": True, "meaning": "the text is elsewhere in the sheet"},
            "not_in_workbook": {"failing": True, "meaning": "the text is nowhere in the workbook"},
        },
    },
    "unaligned_row": {
        "meaning": "a reader row inside the table's span that aligns to no reference row",
        "subclasses": {
            "misplaced": {"failing": True, "meaning": "its text is in the sheet, in rows the alignment did not take"},
            "not_in_workbook": {"failing": True, "meaning": "its text is nowhere in the workbook"},
        },
    },
    "unaligned_table": {
        "meaning": "a table none of whose cells is in the workbook",
        "subclasses": {
            "no_key_in_workbook": {"failing": True, "meaning": "no cell text of the table appears in any printed sheet"},
        },
    },
    "span_mismatch": {
        "meaning": "an exact cell whose span disagrees with the workbook's merged range",
        "subclasses": {
            "wider": {"failing": True, "meaning": "the reader cell spans more sheet columns than the range"},
            "narrower": {"failing": True, "meaning": "the reader cell spans fewer sheet columns than the range"},
            "rows": {"failing": True, "meaning": "the reader cell's row span reaches past the range"},
        },
    },
    "merged_cells": {
        "meaning": "one reader cell covering two reference cells of the same row",
        "subclasses": {
            "text_intact": {"failing": True, "meaning": "the cell holds both texts joined in order"},
            "text_differs": {"failing": True, "meaning": "the cell's text is not the two texts joined"},
        },
    },
    "outside_table": {
        "meaning": "a workbook cell's text left outside every table",
        "subclasses": {
            "cell_text_outside": {"failing": True, "meaning": "the string outside every table is a reference cell's display text"},
        },
    },
    "unexplained_text": {
        "meaning": "text outside every table that is neither header or footer nor a workbook cell",
        "subclasses": {
            "numeric": {"failing": True, "meaning": "a number the workbook does not hold"},
            "edge": {"failing": True, "meaning": "text in the page's margin that matches no header or footer pattern"},
            "text": {"failing": True, "meaning": "text in the page's body that the workbook does not hold"},
        },
    },
    "unverifiable": {
        "meaning": "text the PDF shows that no reader of the workbook can check",
        "subclasses": {
            "uncached_formula": {"failing": False, "meaning": "the result of a formula the workbook stores without a cached value"},
            "print_area_overflow": {"failing": False, "meaning": "text that overflowed into the page from a cell outside the print area"},
        },
    },
    "columns_folded": {
        "meaning": "a cell read whole and in its row, in a sheet column no reader column maps",
        "subclasses": {
            "unmapped_column": {"failing": False, "meaning": "the print never showed that column beside its neighbour, so the reader folded the two into one"},
        },
    },
    "merged_rows": {
        "meaning": "two rows' cells drawn with no rule between them",
        "subclasses": {
            "text_intact": {"failing": False, "meaning": "read as one cell holding both texts in order"},
        },
    },
    "prose_row": {
        "meaning": "rows of text the workbook never held, printed before the first matched row or after the last",
        "subclasses": {
            "outside_workbook": {"failing": False, "meaning": "a document's title block or footer around the table"},
        },
    },
}

# Pair-level: reference cells never found exact on any page. Any of them fails the pair.
UNCOVERED: dict[str, str] = {
    "value_mismatch": "the cell was found but its text mismatched everywhere it was found",
    "outside_table": "its text appeared only outside every table",
    "absent": "its text was never seen on any page",
}

# Tables the scorer sets aside without an error.
SKIPPED_TABLES: dict[str, str] = {
    "empty": "no filled cell",
    "pagination": "every cell is header or footer text in the page's margin",
    "continuation": "a page past a horizontal page break holding only the ends of cells wider than the page; text, not cells, all of it the workbook's",
}

# Recorded on each table, never failing.
TABLE_NOTES: dict[str, str] = {
    "columns_refined": "reader columns that refine one sheet column without ever colliding",
    "merges_unreported": "merged ranges the reader covered only partly",
}

# Rows and outside strings the scorer explains as pagination rather than content.
PAGINATION: dict[str, str] = {
    "pagination_lines": "runs in the page's margin whose vertical ranges overlap, joined into one line that matches the workbook's header or footer pattern",
    "lone_page_number_cells": "a lone margin cell reading like the header or footer never anchors a row; a sheet cell holding such a value still matches the one sheet row between its neighbours that holds nothing else",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def relative(path: Path) -> str:
    """A path as receipts record it: relative to the repository when inside it."""
    resolved = path.resolve()
    return str(resolved.relative_to(REPO_ROOT)) if resolved.is_relative_to(REPO_ROOT) else str(resolved)


def pair_key(pdf_sha256: str, book_sha256: str) -> str:
    """The pair identity `bootstrap.corpus` derives: the two digests, hashed, first 16 hex."""
    return hashlib.sha256((pdf_sha256 + book_sha256).encode()).hexdigest()[:16]


def build_registration(
    corpus_root: Path,
    split_file: Path = SPLIT_FILE,
    key_manifest: Path = KEY_MANIFEST,
    *,
    frozen_by: str,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    """The registration, from MANIFEST.csv, EXCLUDED.csv, pairs.json and the key manifest.

    Nothing about a pair is typed here: digests, page counts and sheet names
    come from the corpus manifest, the producer family and the holdout flag
    from the sealed split (which took the family from Poppler's pdfinfo when
    it was sealed), and the answer-key digests from the manifest #729
    retained.
    """
    split = json.loads(split_file.read_text(encoding="utf-8"))
    records = {record["key"]: record for record in split["pairs"]}
    pairs: list[dict[str, Any]] = []
    with open(corpus_root / "MANIFEST.csv", newline="") as handle:
        manifest_rows = list(csv.DictReader(handle))
    for row in manifest_rows:
        if row["duplicate_of"]:
            continue
        key = pair_key(row["pdf_sha256"], row["book_sha256"])
        record = records[key]
        if (record["folder"], record["pdf"], record["spreadsheet"]) != (row["folder"], row["pdf"], row["spreadsheet"]):
            raise ValueError(f"split record for {key} names another pair than the manifest")
        pairs.append(
            {
                "key": key,
                "folder": row["folder"],
                "pdf": row["pdf"],
                "spreadsheet": row["spreadsheet"],
                "agency": row["folder"].split("_", 1)[0],
                "family": record["family"],
                "tagged": record["tagged"],
                "tier": row["tier"],
                "reading": row["reading"],
                "pages": int(row["pdf_pages"]),
                "visible_cells": int(row["visible_cells"]),
                "printed_sheets": [name.strip() for name in row["printed_sheets"].split(";") if name.strip()],
                "unprinted_sheets": [name.strip() for name in row["unprinted_sheets"].split(";") if name.strip()],
                "pdf_sha256": row["pdf_sha256"],
                "book_sha256": row["book_sha256"],
                "holdout": record["holdout"],
            }
        )
    pairs.sort(key=lambda pair: pair["key"])
    exclusions: list[dict[str, Any]] = []
    with open(corpus_root / "EXCLUDED.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            exclusions.append(
                {
                    "folder": row["folder"],
                    "pdf": row["pdf"],
                    "spreadsheet": row["spreadsheet"],
                    "pdf_sha256": row["pdf_sha256"],
                    "book_sha256": row["book_sha256"],
                    "reason": row["reason"],
                    "duplicate_of": row["duplicate_of"] or None,
                }
            )
    keys = json.loads(key_manifest.read_text(encoding="utf-8"))
    key_digests = {entry["path"]: entry["sha256"] for entry in keys["entries"]}
    missing_keys = [pair["key"] for pair in pairs if f"{pair['key']}.json" not in key_digests]
    if missing_keys:
        raise ValueError(f"the key manifest holds no answer key for {missing_keys}")
    development = [pair for pair in pairs if not pair["holdout"]]
    holdout = [pair for pair in pairs if pair["holdout"]]
    families = sorted({pair["family"] for pair in pairs})
    agencies = sorted({pair["agency"] for pair in pairs})
    return {
        "schema_version": DATASET_SCHEMA,
        "dataset_version": DATASET_VERSION,
        "frozen_at": frozen_at or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "frozen_by": frozen_by,
        "what": (
            "The paired-rendition Reference Dataset: 333 pairs of a structured original (a workbook) "
            "and its printed rendition (a PDF) whose print shows the workbook as it now stands. The "
            "reader under measurement reads only the PDF; the answer key is built from the workbook alone."
        ),
        "origin": {
            "corpus": "utility-conflict-matrices/PDF-Spreadsheet-Pairs/true-pairs, the exact subset (bootstrap/README.md)",
            "selection": (
                "of 524 unique pairs, those whose PDF prints the workbook exactly under the reader-independent "
                "text check in bootstrap/exactness.py, less the revisions the scorer later found (EXCLUDED.csv)"
            ),
            "agencies": agencies,
            "producer_families": families,
            "family_source": "Poppler's pdfinfo Producer or Creator, read once when the split was sealed (bootstrap/split.py)",
        },
        "corpus": {
            "root": str(corpus_root),
            "manifest": "MANIFEST.csv",
            "manifest_sha256": sha256_file(corpus_root / "MANIFEST.csv"),
            "excluded": "EXCLUDED.csv",
            "excluded_sha256": sha256_file(corpus_root / "EXCLUDED.csv"),
            "pairs": len(pairs),
            "files": 2 * len(pairs),
            "duplicate_listings_in_manifest": sum(1 for row in manifest_rows if row["duplicate_of"]),
        },
        "expected_values": {
            "independently_established": True,
            "how": (
                "the workbook's own display strings under its own number formats, built by bootstrap.reference "
                "from the workbook bytes with openpyxl, xlrd and SSF (node); the reader reads the PDF with "
                "pypdfium2 and pypdf and never sees the workbook"
            ),
            "shared_dependencies_with_the_reader": "none: no parser, table detector or engine is shared between the key and the reader",
            "corrections": "every correction made to the key is logged in bootstrap/README.md and changed the score without touching the reader",
            "caveat": (
                "the key is what the workbook displays, not human gold: a pair whose workbook was edited after "
                "printing is excluded as a revision, never repaired from the PDF side (ADR-0023)"
            ),
        },
        "reference": {
            "name": "loop-reference-v6",
            "manifest": relative(key_manifest),
            "manifest_sha256": sha256_file(key_manifest),
            "keys": len(pairs),
            "note": "the answer keys are not committed; bootstrap.reference rebuilds them and the measurement command refuses any key whose digest is not the retained one",
        },
        "split": {
            "file": relative(split_file),
            "sha256": sha256_file(split_file),
            "seed": split["seed"],
            "holdout_fraction": split["holdout_fraction"],
            "strata": split["strata"],
            "grouping_rule": GROUPING_RULE,
            "development": {"pairs": len(development), "pages": sum(pair["pages"] for pair in development)},
            "holdout": {"pairs": len(holdout), "pages": sum(pair["pages"] for pair in holdout)},
            "note": (
                "pairs.json seals the split over all 524 unique pairs; the keys and holdout flags carry over to "
                "this subset unchanged, so the 333 pairs are the 524-pair split restricted to the exact set"
            ),
        },
        "holdout_policy": HOLDOUT_POLICY,
        "gate": list(GATE),
        "taxonomy": {
            "classes": TAXONOMY,
            "uncovered": UNCOVERED,
            "skipped_tables": SKIPPED_TABLES,
            "table_notes": TABLE_NOTES,
            "pagination": PAGINATION,
            "rule": (
                "a page fails on any error whose subclass is failing; a pair fails when any page fails or any "
                "reference cell is uncovered; nothing is a percentage, every miss has a class"
            ),
        },
        "limits": LIMITS,
        "pairs": pairs,
        "exclusions": exclusions,
    }


def load_registration(path: Path = REGISTRATION) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != DATASET_SCHEMA:
        raise ValueError(f"{path} is not a {DATASET_SCHEMA} registration")
    return document


def registration_digest(path: Path = REGISTRATION) -> str:
    """The digest every receipt cites: the registration file's bytes."""
    return sha256_file(path)


def registration_identity(path: Path = REGISTRATION) -> dict[str, Any]:
    document = load_registration(path)
    return {
        "file": relative(path),
        "schema_version": document["schema_version"],
        "dataset_version": document["dataset_version"],
        "sha256": registration_digest(path),
        "corpus_manifest_sha256": document["corpus"]["manifest_sha256"],
        "split_sha256": document["split"]["sha256"],
        "reference_manifest_sha256": document["reference"]["manifest_sha256"],
    }


def append_holdout_access(entry: dict[str, Any], ledger: Path = HOLDOUT_LEDGER) -> dict[str, Any]:
    """Append one access to this dataset's ledger file (ADR-0008, one shared ledger)."""
    return holdout_ledger.append(entry, ledger=ledger)


def load_holdout_ledger(ledger: Path = HOLDOUT_LEDGER) -> list[dict[str, Any]]:
    return holdout_ledger.read(ledger)


def load_receipt_index(index: Path = RECEIPT_INDEX) -> dict[str, Any]:
    if not index.is_file():
        return {"schema_version": INDEX_SCHEMA, "receipts": []}
    document = json.loads(index.read_text(encoding="utf-8"))
    if document.get("schema_version") != INDEX_SCHEMA:
        raise ValueError(f"{index} is not a {INDEX_SCHEMA} index")
    return document


def register_receipt(entry: dict[str, Any], index: Path = RECEIPT_INDEX) -> dict[str, Any]:
    """Add one receipt to the index, by path and digest; the receipt's files stay where they are."""
    document = load_receipt_index(index)
    for field in ("run", "role", "path", "configuration", "measures", "files"):
        if field not in entry:
            raise ValueError(f"a receipt index entry needs {field}")
    if any(existing["run"] == entry["run"] for existing in document["receipts"]):
        raise ValueError(f"the index already holds a receipt named {entry['run']}")
    document["receipts"].append(entry)
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return document


def digest_files(root: Path, names: list[str]) -> dict[str, str]:
    """`{relative name: sha256}` for the named files under `root`, for an index entry."""
    return {name: sha256_file(root / name) for name in names}


def baseline_receipt(index: Path = RECEIPT_INDEX) -> dict[str, Any] | None:
    """The harness's registered baseline, if one has been retained."""
    for entry in load_receipt_index(index)["receipts"]:
        if entry["role"] == "baseline":
            return entry
    return None


def main(argv: list[str] | None = None) -> int:
    """Write the registration from the corpus and the sealed split (a one-time act, repeatable)."""
    import argparse

    from corridor_pdf_reader.bootstrap.corpus import TRUE_PAIRS

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--corpus", type=Path, default=TRUE_PAIRS)
    parser.add_argument("--output", type=Path, default=REGISTRATION)
    parser.add_argument("--frozen-by", required=True)
    parser.add_argument("--frozen-at", help="ISO-8601 instant; defaults to now")
    args = parser.parse_args(argv)
    document = build_registration(args.corpus, frozen_by=args.frozen_by, frozen_at=args.frozen_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"{document['corpus']['pairs']} pairs ({document['split']['development']['pairs']} development, "
        f"{document['split']['holdout']['pairs']} holdout), {len(document['exclusions'])} exclusions -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
