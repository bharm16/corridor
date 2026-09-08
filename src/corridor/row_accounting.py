"""Prove detected source units reached an explicit completeness disposition.

Previously, readers exposed only their Candidate count. That approach was
rejected because it cannot distinguish a truly blank matrix from a reader that
silently dropped a row before proposal creation. Both matrix readers use this
module to key rows at detection, record extracted, blank, or reasoned-skip
outcomes, and fail before a quiet success when coverage is incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACCOUNTING_REQUIRED_READERS = frozenset(
    {"sheet_native_v2", "matrix_tiered_v4", "matrix_structure_ids_v1", "prose_interpretation_v1"}
)
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
_PROSE_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "reader_version",
        "reader_path",
        "document_id",
        "detected_segment_count",
        "read_segment_count",
        "proposed_fact_count",
        "unread_segment_ids",
        "proposed_subject_candidate_ids",
        "unproposed_subject_candidate_ids",
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
    if isinstance(receipt, dict) and receipt.get("schema_version") == (
        "native-matrix-row-accounting-v1"
    ):
        return _validate_native_matrix_accounting(
            receipt, prompt_version=prompt_version,
            outcome=outcome, candidate_count=candidate_count,
        )
    if isinstance(receipt, dict) and receipt.get("schema_version") == (
        "prose-segment-accounting-v1"
    ):
        return _validate_prose_accounting(
            receipt,
            prompt_version=prompt_version,
            outcome=outcome,
            candidate_count=candidate_count,
        )
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        raise ValueError("matrix row accounting receipt shape is invalid")
    if (
        receipt.get("schema_version") != "matrix-row-accounting-v1"
        or receipt.get("reader_version") != prompt_version
        or receipt.get("reader_path")
        not in {"spreadsheet_cells", "page_geometry_and_transcription", "native_matrix_cells"}
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


def _validate_native_matrix_accounting(receipt, *, prompt_version, outcome, candidate_count):
    """Native zero-based row positions and field outcomes have distinct scope."""
    if (set(receipt) != _RECEIPT_KEYS | {"native_mapping", "field_materialization"}
            or prompt_version != "matrix_structure_ids_v1"
            or receipt.get("reader_path") != "native_matrix_cells"
            or outcome != "completed"):
        raise ValueError("native matrix accounting identity or shape is invalid")
    rows = receipt.get("rows")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or type(row.get("row_number")) is not int or row["row_number"] < 0
        for row in rows
    ):
        raise ValueError("native matrix source row positions are invalid")
    # Reuse the existing completeness proof without changing its historical
    # one-based reader contract. The stored native positions remain zero-based.
    base = {key: receipt[key] for key in _RECEIPT_KEYS}
    base["schema_version"] = "matrix-row-accounting-v1"
    base["rows"] = [{**row, "row_number": row["row_number"] + 1} for row in rows]
    validate_row_accounting(base, prompt_version=prompt_version, outcome=outcome, candidate_count=candidate_count)
    mapping = receipt["native_mapping"]
    if not isinstance(mapping, dict) or set(mapping) != {"identity", "reading_sha256", "pages"}:
        raise ValueError("native matrix mapping receipt is invalid")
    for key in ("identity", "reading_sha256"):
        value = mapping[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("native matrix mapping digest is invalid")
    expected = set()
    for page in mapping["pages"]:
        for row in page["reading"]["rows"]:
            for name in row["fields"]:
                key = (page["number"], row["row_id"], name)
                if key in expected:
                    raise ValueError("native matrix repeats a field occurrence")
                expected.add(key)
    dispositions = {row["row_id"]: row["disposition"] for row in rows}
    outcomes = receipt["field_materialization"]
    if not isinstance(outcomes, list):
        raise ValueError("native field outcomes must be a list")
    seen = set()
    required = {"row_id", "local_row_id", "page", "field", "status", "reason", "value_source_ids", "context_source_ids"}
    for field in outcomes:
        if not isinstance(field, dict) or set(field) != required:
            raise ValueError("native field outcome shape is invalid")
        key = (field["page"], field["local_row_id"], field["field"])
        if key not in expected or key in seen or field["row_id"] not in dispositions:
            raise ValueError("native field outcome is missing, repeated or out of scope")
        if (field["status"] not in {"materialized", "refused", "not_extracted"}
                or not isinstance(field["reason"], str) or not field["reason"]
                or (field["status"] == "not_extracted") != (dispositions[field["row_id"]] != "extracted")):
            raise ValueError("native field outcome contradicts its row disposition")
        for name in ("value_source_ids", "context_source_ids"):
            ids = field[name]
            if (not isinstance(ids, list) or any(type(value) is not int or value <= 0 for value in ids)
                    or len(ids) != len(set(ids))):
                raise ValueError("native field sources are invalid")
        if not field["value_source_ids"]:
            raise ValueError("native field needs its value source")
        seen.add(key)
    if seen != expected:
        raise ValueError("native field materialization outcomes are incomplete")
    return receipt


def _validate_prose_accounting(
    receipt: dict[str, Any],
    *,
    prompt_version: str,
    outcome: str,
    candidate_count: int,
) -> dict[str, Any]:
    if set(receipt) != _PROSE_RECEIPT_KEYS:
        raise ValueError("prose segment accounting receipt shape is invalid")
    if (
        receipt.get("reader_version") != prompt_version
        or receipt.get("reader_path") != "prose_interpretation"
        or outcome != "completed"
    ):
        raise ValueError("prose segment accounting reader identity is invalid")
    document_id = receipt.get("document_id")
    count_names = (
        "detected_segment_count",
        "read_segment_count",
        "proposed_fact_count",
    )
    if (
        isinstance(document_id, bool)
        or not isinstance(document_id, int)
        or document_id <= 0
        or any(
            isinstance(receipt.get(name), bool)
            or not isinstance(receipt.get(name), int)
            or receipt[name] < 0
            for name in count_names
        )
    ):
        raise ValueError("prose segment accounting counts are invalid")
    id_lists = (
        "unread_segment_ids",
        "proposed_subject_candidate_ids",
        "unproposed_subject_candidate_ids",
    )
    for name in id_lists:
        values = receipt.get(name)
        if (
            not isinstance(values, list)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                for value in values
            )
            or values != sorted(set(values))
        ):
            raise ValueError("prose segment accounting identities are invalid")
    if (
        receipt["read_segment_count"] + len(receipt["unread_segment_ids"])
        != receipt["detected_segment_count"]
        or receipt["proposed_fact_count"] != candidate_count
        or set(receipt["proposed_subject_candidate_ids"])
        & set(receipt["unproposed_subject_candidate_ids"])
    ):
        raise ValueError("prose segment accounting completeness is inconsistent")
    return receipt
