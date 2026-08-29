"""Prove that every detected matrix row reached an explicit disposition.

Previously, readers exposed only their Candidate count. That approach was
rejected because it cannot distinguish a truly blank matrix from a reader that
silently dropped a row before proposal creation. Both matrix readers use this
module to key rows at detection, record extracted, blank, or reasoned-skip
outcomes, and fail before a quiet success when coverage is incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACCOUNTING_REQUIRED_READERS = frozenset({"sheet_native_v2", "matrix_tiered_v4"})
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "reader_version",
        "reader_path",
        "detected_row_count",
        "accounted_row_count",
        "extracted_row_count",
        "blank_row_count",
        "skipped_row_count",
        "unaccounted_rows",
        "rows",
    }
)

class RowAccountingFailure(RuntimeError):
    """Detected matrix rows were not completely and consistently accounted."""

    def __init__(self, message: str, receipt: dict[str, Any]):
        super().__init__(message)
        self.receipt = receipt


class AccountedCandidates(list):
    """Candidate list carrying the exact immutable row-accounting receipt."""

    def __init__(self, values, *, row_accounting: dict[str, Any]):
        super().__init__(values)
        self.row_accounting = row_accounting


@dataclass
class RowAccounting:
    reader_version: str
    reader_path: str
    _detected: list[str] = field(default_factory=list)
    _rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def detect(self, row_id: str, *, page: int, row_number: int) -> None:
        if not row_id or row_id in self._detected:
            raise RowAccountingFailure(
                "matrix row detection identity is empty or duplicated",
                self._receipt(),
            )
        self._detected.append(row_id)
        self._rows.setdefault(
            row_id,
            {
                "row_id": row_id,
                "page": page,
                "row_number": row_number,
                "disposition": None,
                "reason": None,
            },
        )

    def account(self, row_id: str, *, disposition: str, reason: str) -> None:
        if row_id not in self._detected:
            raise RowAccountingFailure(
                f"matrix row {row_id!r} was accounted before detection",
                self._receipt(),
            )
        if disposition not in {"extracted", "blank", "skipped"} or not reason:
            raise RowAccountingFailure(
                f"matrix row {row_id!r} has an invalid disposition",
                self._receipt(),
            )
        if self._rows[row_id]["disposition"] is not None:
            raise RowAccountingFailure(
                f"matrix row {row_id!r} was accounted more than once",
                self._receipt(),
            )
        self._rows[row_id]["disposition"] = disposition
        self._rows[row_id]["reason"] = reason

    def finish(self, candidates) -> AccountedCandidates:
        receipt = self._receipt()
        unaccounted = receipt["unaccounted_rows"]
        if unaccounted:
            raise RowAccountingFailure(
                "matrix row accounting failed: "
                f"detected {receipt['detected_row_count']}, "
                f"accounted {receipt['accounted_row_count']}, "
                f"unaccounted {', '.join(unaccounted)}",
                receipt,
            )
        if receipt["extracted_row_count"] != len(candidates):
            raise RowAccountingFailure(
                "matrix row accounting failed: extracted row count does not "
                "match Candidate count",
                receipt,
            )
        return AccountedCandidates(candidates, row_accounting=receipt)

    def _receipt(self) -> dict[str, Any]:
        rows = [self._rows[row_id] for row_id in self._detected]
        accounted = [row for row in rows if row["disposition"] is not None]
        return {
            "schema_version": "matrix-row-accounting-v1",
            "reader_version": self.reader_version,
            "reader_path": self.reader_path,
            "detected_row_count": len(rows),
            "accounted_row_count": len(accounted),
            "extracted_row_count": sum(
                row["disposition"] == "extracted" for row in accounted
            ),
            "blank_row_count": sum(
                row["disposition"] == "blank" for row in accounted
            ),
            "skipped_row_count": sum(
                row["disposition"] == "skipped" for row in accounted
            ),
            "unaccounted_rows": [
                row["row_id"] for row in rows if row["disposition"] is None
            ],
            "rows": rows,
        }


def validate_row_accounting(
    receipt: dict[str, Any] | None,
    *,
    prompt_version: str,
    outcome: str,
    candidate_count: int,
) -> dict[str, Any] | None:
    """Validate one durable Extraction Run accounting receipt."""

    if receipt is None:
        if outcome == "completed" and prompt_version in ACCOUNTING_REQUIRED_READERS:
            raise ValueError(
                f"completed {prompt_version} Extraction Run requires row accounting"
            )
        return None
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        raise ValueError("matrix row accounting receipt shape is invalid")
    if (
        receipt.get("schema_version") != "matrix-row-accounting-v1"
        or receipt.get("reader_version") != prompt_version
        or receipt.get("reader_path")
        not in {"spreadsheet_cells", "page_geometry_and_transcription"}
    ):
        raise ValueError("matrix row accounting reader identity is invalid")
    count_names = (
        "detected_row_count",
        "accounted_row_count",
        "extracted_row_count",
        "blank_row_count",
        "skipped_row_count",
    )
    if any(
        isinstance(receipt.get(name), bool)
        or not isinstance(receipt.get(name), int)
        or receipt[name] < 0
        for name in count_names
    ):
        raise ValueError("matrix row accounting counts are invalid")
    rows = receipt.get("rows")
    unaccounted = receipt.get("unaccounted_rows")
    if not isinstance(rows, list) or not isinstance(unaccounted, list):
        raise ValueError("matrix row accounting rows are invalid")
    if len(rows) != receipt["detected_row_count"]:
        raise ValueError("matrix row accounting detected count does not match rows")
    if len(unaccounted) != receipt["detected_row_count"] - receipt["accounted_row_count"]:
        raise ValueError("matrix row accounting discrepancy count is invalid")
    if (
        receipt["accounted_row_count"]
        != receipt["extracted_row_count"]
        + receipt["blank_row_count"]
        + receipt["skipped_row_count"]
    ):
        raise ValueError("matrix row accounting dispositions do not sum")
    seen: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"row_id", "page", "row_number", "disposition", "reason"}
            or not isinstance(row.get("row_id"), str)
            or not row["row_id"]
            or row["row_id"] in seen
            or isinstance(row.get("page"), bool)
            or not isinstance(row.get("page"), int)
            or row["page"] <= 0
            or isinstance(row.get("row_number"), bool)
            or not isinstance(row.get("row_number"), int)
            or row["row_number"] <= 0
            or row.get("disposition") not in {None, "extracted", "blank", "skipped"}
            or (
                row.get("disposition") is not None
                and (not isinstance(row.get("reason"), str) or not row["reason"])
            )
        ):
            raise ValueError("matrix row accounting row entry is invalid")
        seen.add(row["row_id"])
    expected_unaccounted = [
        row["row_id"] for row in rows if row["disposition"] is None
    ]
    if unaccounted != expected_unaccounted:
        raise ValueError("matrix row accounting unaccounted identities do not match")
    if outcome == "completed":
        if unaccounted or receipt["accounted_row_count"] != receipt["detected_row_count"]:
            raise ValueError("completed matrix Extraction Run has unaccounted rows")
        if receipt["extracted_row_count"] != candidate_count:
            raise ValueError(
                "completed matrix row accounting does not match Candidate count"
            )
    elif prompt_version in ACCOUNTING_REQUIRED_READERS and not unaccounted:
        raise ValueError(
            "failed accounted matrix run must retain its row discrepancy"
        )
    return receipt
