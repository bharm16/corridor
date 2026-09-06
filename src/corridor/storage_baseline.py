"""Measure the permanent copy chains the source-to-record spine replaces.

The storage redesign needs a baseline that survives the redesign itself.  This
module therefore names old table/column paths explicitly, measures them with
PostgreSQL rather than estimates, and freezes representative semantic outputs
as canonical JSON plus digests.  It is read-only: measuring a database never
creates a Report, release, Extraction Run, or Project Record row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.models import (
    ExternalReportRelease,
    ExtractionRun,
    ExtractorConfiguration,
    ReportRun,
)


# v2 adds the extractor-configuration family (#605). A v1 and a v2 body
# measure different member sets, so they are not comparable line for line;
# the version says so instead of leaving a reader to notice. v3 freezes a
# PDF's page text as whitespace-normalized text in content order (#740): v1
# and v2 froze PyMuPDF's block-sorted text, which no other reader reproduces,
# so their PDF members are not comparable with v3's either.
SCHEMA_VERSION = "corridor.storage-duplication-baseline.v3"
MINIMUM_REDUCTION_PERCENT = 50


@dataclass(frozen=True)
class BaselineSelection:
    """Exact existing rows whose externally meaningful semantics are frozen."""

    report_run_id: int | None = None
    release_id: int | None = None
    extraction_run_id: int | None = None
    coordination_report_path: Path | None = None
    release_path: Path | None = None


@dataclass(frozen=True)
class _Member:
    table: str
    column: str
    path: str
    query: str
    target_included: bool = True


def _column_member(table: str, column: str) -> _Member:
    """Measure one real PostgreSQL column named by this module's fixed catalog."""

    return _Member(
        table,
        column,
        "$",
        f"""
        select count({column})::bigint as rows,
               coalesce(sum(pg_column_size({column})), 0)::bigint as bytes
        from {table}
        """,
    )


_FAMILIES: tuple[tuple[str, tuple[_Member, ...]], ...] = (
    (
        "cited_quote_copies",
        (
            _Member(
                "candidates",
                "payload_json",
                "$.citations[*].quote",
                """
                select count(*)::bigint as rows,
                       coalesce(sum(octet_length(convert_to(citation->>'quote', 'UTF8'))), 0)::bigint as bytes
                from candidates candidate
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(candidate.payload_json->'citations') = 'array'
                         then candidate.payload_json->'citations' else '[]'::jsonb end
                ) citation
                where jsonb_typeof(citation->'quote') = 'string'
                """,
            ),
            _Member(
                "extraction_runs",
                "candidate_inputs_json",
                "$[*].payload_json.citations[*].quote",
                """
                select count(*)::bigint as rows,
                       coalesce(sum(octet_length(convert_to(citation->>'quote', 'UTF8'))), 0)::bigint as bytes
                from extraction_runs run
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(run.candidate_inputs_json) = 'array'
                         then run.candidate_inputs_json else '[]'::jsonb end
                ) candidate
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(candidate->'payload_json'->'citations') = 'array'
                         then candidate->'payload_json'->'citations' else '[]'::jsonb end
                ) citation
                where jsonb_typeof(citation->'quote') = 'string'
                """,
                False,
            ),
            _Member(
                "evidence_links",
                "quote",
                "$",
                """
                select count(quote)::bigint as rows,
                       coalesce(sum(octet_length(convert_to(quote, 'UTF8'))), 0)::bigint as bytes
                from evidence_links
                """,
            ),
            _Member(
                "statement_coordination_receipts",
                "accepted_facts_json",
                "$.evidence[*].quote",
                """
                select count(*)::bigint as rows,
                       coalesce(sum(octet_length(convert_to(evidence->>'quote', 'UTF8'))), 0)::bigint as bytes
                from statement_coordination_receipts receipt
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(receipt.accepted_facts_json->'evidence') = 'array'
                         then receipt.accepted_facts_json->'evidence' else '[]'::jsonb end
                ) evidence
                where jsonb_typeof(evidence->'quote') = 'string'
                """,
            ),
        ),
    ),
    (
        "accepted_field_map_copies",
        (
            _Member(
                "candidates",
                "payload_json",
                "$.fields",
                """
                select count(*)::bigint as rows,
                       coalesce(sum(pg_column_size(payload_json->'fields')), 0)::bigint as bytes
                from candidates
                where jsonb_typeof(payload_json->'fields') = 'object'
                """,
            ),
            _Member(
                "extraction_runs",
                "candidate_inputs_json",
                "$[*].payload_json.fields",
                """
                select count(*)::bigint as rows,
                       coalesce(sum(pg_column_size(candidate->'payload_json'->'fields')), 0)::bigint as bytes
                from extraction_runs run
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(run.candidate_inputs_json) = 'array'
                         then run.candidate_inputs_json else '[]'::jsonb end
                ) candidate
                where jsonb_typeof(candidate->'payload_json'->'fields') = 'object'
                """,
                False,
            ),
            _Member(
                "assertions",
                "asserted_value",
                "$",
                """
                select count(asserted_value)::bigint as rows,
                       coalesce(sum(octet_length(convert_to(asserted_value, 'UTF8'))), 0)::bigint as bytes
                from assertions
                """,
            ),
            _column_member("dependencies", "source_ref"),
            _column_member("dependencies", "external_org_id"),
            _column_member("dependencies", "title"),
            _column_member("dependencies", "location_desc"),
            _column_member("dependencies", "station_from"),
            _column_member("dependencies", "station_to"),
            _column_member("dependencies", "external_contact"),
            _column_member("dependencies", "resolution_strategy"),
            _column_member("dependencies", "cost_responsibility"),
            _column_member("dependencies", "notes"),
            _Member(
                "audit_log",
                "before_json",
                "$.fields",
                """
                select count(*)::bigint as rows,
                       coalesce(sum(pg_column_size(before_json->'fields')), 0)::bigint as bytes
                from audit_log
                where jsonb_typeof(before_json->'fields') = 'object'
                """,
            ),
            _Member(
                "audit_log",
                "after_json",
                "$.fields",
                """
                select count(*)::bigint as rows,
                       coalesce(sum(pg_column_size(after_json->'fields')), 0)::bigint as bytes
                from audit_log
                where jsonb_typeof(after_json->'fields') = 'object'
                """,
            ),
        ),
    ),
    (
        "released_pdf_copies",
        (
            _Member(
                "external_report_artifacts",
                "pdf_bytes",
                "$",
                """
                select count(pdf_bytes)::bigint as rows,
                       coalesce(sum(octet_length(pdf_bytes)), 0)::bigint as bytes
                from external_report_artifacts
                """,
                False,
            ),
            _Member(
                "external_report_releases",
                "pdf_bytes",
                "$",
                """
                select count(pdf_bytes)::bigint as rows,
                       coalesce(sum(octet_length(pdf_bytes)), 0)::bigint as bytes
                from external_report_releases
                """,
            ),
        ),
    ),
    (
        # The family key is retained verbatim because recorded baselines in
        # artifacts/storage-baseline are keyed by it and a rename would make
        # this measurement incomparable with them. The name is now wrong about
        # what it measures: ADR-0092 makes these two columns a dated
        # occurrence's own Report Reading payload rather than a copy of state
        # another row owns. What is measured here is how much retained report
        # evidence the database holds, which is still worth knowing.
        "report_snapshot_copies",
        (
            _Member(
                "report_runs",
                "snapshot_json",
                "$",
                """
                select count(snapshot_json)::bigint as rows,
                       coalesce(sum(pg_column_size(snapshot_json)), 0)::bigint as bytes
                from report_runs
                """,
            ),
            _Member(
                "scheduled_report_publications",
                "snapshot_json",
                "$",
                """
                select count(snapshot_json)::bigint as rows,
                       coalesce(sum(pg_column_size(snapshot_json)), 0)::bigint as bytes
                from scheduled_report_publications
                """,
            ),
        ),
    ),
    (
        "run_payload_snapshots",
        (
            _Member(
                "extraction_runs",
                "candidate_inputs_json",
                "$",
                """
                select count(candidate_inputs_json)::bigint as rows,
                       coalesce(sum(pg_column_size(candidate_inputs_json)), 0)::bigint as bytes
                from extraction_runs
                """,
            ),
        ),
    ),
    (
        # #605. Every run of one deployed extractor seals a byte-identical
        # receipt, so a copy per run is one value written once per attempt.
        # The registry is the single owner that replaces those copies and is
        # therefore measured but excluded from the removable target: storing
        # a configuration once is the destination, not the duplication.
        "run_extractor_config_copies",
        (
            _Member(
                "extraction_runs",
                "extractor_config_json",
                "$",
                """
                select count(extractor_config_json)::bigint as rows,
                       coalesce(sum(pg_column_size(extractor_config_json)), 0)::bigint as bytes
                from extraction_runs
                """,
            ),
            _Member(
                "extractor_configurations",
                "config_json",
                "$",
                """
                select count(config_json)::bigint as rows,
                       coalesce(sum(pg_column_size(config_json)), 0)::bigint as bytes
                from extractor_configurations
                """,
                False,
            ),
        ),
    ),
)


def build_storage_baseline(
    session: Session,
    *,
    selection: BaselineSelection | None = None,
) -> dict[str, Any]:
    """Measure named copy families and freeze three exact semantic outputs."""

    chosen = selection or BaselineSelection()
    families = [_measure_family(session, name, members) for name, members in _FAMILIES]
    # The whole Extraction Run payload member already contains its nested quote
    # and field paths. Those paths remain visible in their logical families but
    # are excluded here so the cutover target is a physical-byte metric rather
    # than a gross sum that counts the same JSON bytes twice.
    known_duplication_bytes = sum(family["target_bytes"] for family in families)
    representative_outputs = {
        "coordination_report": _freeze_report_run(
            session,
            _selected_id(session, ReportRun, chosen.report_run_id),
            fallback_path=chosen.coordination_report_path,
        ),
        "release": _freeze_release(
            session,
            _selected_id(session, ExternalReportRelease, chosen.release_id),
            fallback_path=chosen.release_path,
        ),
        "extraction_run": _freeze_extraction_run(
            session, _selected_id(session, ExtractionRun, chosen.extraction_run_id)
        ),
    }
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "database": str(session.scalar(text("select current_database()"))),
        "families": families,
        "known_duplication_bytes": known_duplication_bytes,
        "metric_definition": (
            "Sum of measured removable duplicate-bearing values. Excludes "
            "nested Extraction Run quote and field paths already counted by "
            "the whole candidate_inputs_json snapshot member, excludes "
            "artifact-owned PDF bytes because the artifact is the one durable "
            "content owner retained by the single-store release design, and "
            "excludes registry-owned extractor configuration because storing "
            "one configuration once is the destination, not the duplication."
        ),
        "representative_outputs": representative_outputs,
        "target": {
            "metric": "known_duplication_bytes",
            "minimum_reduction_percent": MINIMUM_REDUCTION_PERCENT,
            "baseline_bytes": known_duplication_bytes,
            "maximum_cutover_bytes": known_duplication_bytes // 2,
        },
    }
    body["sha256"] = _digest(body)
    return body


def _measure_family(
    session: Session, name: str, members: tuple[_Member, ...]
) -> dict[str, Any]:
    measured = [_measure_member(session, member) for member in members]
    return {
        "family": name,
        "rows": sum(member["rows"] for member in measured),
        "bytes": sum(member["bytes"] for member in measured),
        "target_bytes": sum(
            member["bytes"] for member in measured if member["target_included"]
        ),
        "members": measured,
    }


def _measure_member(session: Session, member: _Member) -> dict[str, Any]:
    present = bool(
        session.scalar(
            text(
                """
                select exists (
                    select 1
                    from information_schema.columns
                    where table_schema = 'public'
                      and table_name = :table_name
                      and column_name = :column_name
                )
                """
            ),
            {"table_name": member.table, "column_name": member.column},
        )
    )
    if present:
        row = session.execute(text(member.query)).mappings().one()
        rows = int(row["rows"])
        size = int(row["bytes"])
    else:
        rows = 0
        size = 0
    return {
        "table": member.table,
        "column": member.column,
        "path": member.path,
        "rows": rows,
        "bytes": size,
        "present": present,
        "target_included": member.target_included,
    }


def _selected_id(session: Session, model, requested: int | None) -> int | None:
    if requested is not None:
        return requested
    return session.scalar(select(model.id).order_by(model.id.desc()).limit(1))


def _freeze_report_run(
    session: Session,
    source_id: int | None,
    *,
    fallback_path: Path | None = None,
) -> dict[str, Any]:
    row = session.get(ReportRun, source_id) if source_id is not None else None
    if row is None:
        if fallback_path is not None:
            return _freeze_pdf(fallback_path)
        return _unavailable(source_id)
    content = {
        "project_id": row.project_id,
        "ruleset_version": row.ruleset_version,
        "document_only": row.document_only,
        "snapshot": row.snapshot_json,
    }
    return _frozen(row.id, content)


def _freeze_release(
    session: Session,
    source_id: int | None,
    *,
    fallback_path: Path | None = None,
) -> dict[str, Any]:
    row = (
        session.get(ExternalReportRelease, source_id)
        if source_id is not None
        else None
    )
    if row is None:
        if fallback_path is not None:
            return _freeze_pdf(fallback_path)
        return _unavailable(source_id)
    content = {
        "project_id": row.project_id,
        "artifact_id": row.artifact_id,
        "artifact_name": row.artifact_name,
        "format": row.format,
        "pdf_sha256": row.pdf_sha256,
        "evaluated_on": row.evaluated_on,
        "ruleset_version": row.ruleset_version,
        "evaluation_context": row.evaluation_context_json,
        "provenance_mode": row.provenance_mode,
        "record_context": row.record_context_json,
        "released_by": row.released_by,
        "released_by_display": row.released_by_display,
    }
    return _frozen(row.id, content)


def _freeze_extraction_run(
    session: Session, source_id: int | None
) -> dict[str, Any]:
    row = session.get(ExtractionRun, source_id) if source_id is not None else None
    if row is None:
        return _unavailable(source_id)
    # The sealed configuration is stored once by digest and referenced by the
    # run (#605), so the frozen semantics read it from wherever it lives
    # rather than only from the per-run copy that is going away.
    stored_config = (
        session.get(ExtractorConfiguration, row.extractor_config_sha256)
        if row.extractor_config_sha256 is not None
        else None
    )
    content = {
        "document_id": row.document_id,
        "prompt_version": row.prompt_version,
        "outcome": row.outcome,
        "candidate_count": row.candidate_count,
        "page_errors": row.page_errors,
        "model": row.model,
        "schema_version": row.schema_version,
        "error_detail": row.error_detail,
        "candidate_inputs": row.candidate_inputs_json,
        "prompt_sha256": row.prompt_sha256,
        "schema_sha256": row.schema_sha256,
        "postprocessor_sha256": row.postprocessor_sha256,
        "extractor_config_sha256": row.extractor_config_sha256,
        "extractor_config": (
            stored_config.config_json
            if stored_config is not None
            else row.extractor_config_json
        ),
        "token_usage": row.token_usage_json,
        "row_accounting": row.row_accounting_json,
    }
    return _frozen(row.id, content)


def _frozen(source_id: int, content: dict[str, Any]) -> dict[str, Any]:
    normalized = _json_value(content)
    return {
        "available": True,
        "source_id": source_id,
        "content": normalized,
        "sha256": _digest(normalized),
    }


def _freeze_pdf(path: Path) -> dict[str, Any]:
    """Freeze visible PDF semantics separately from encoding-specific bytes.

    The frozen text is each page's text in content order with runs of
    whitespace collapsed to one space: the reading PyMuPDF and pypdf agree on
    byte for byte across the retained sealed exports, where line breaks and
    block order are theirs and the words are the document's.
    """

    raw = path.read_bytes()
    artifact_digest = sha256(raw).hexdigest()
    pages = PdfReader(BytesIO(raw)).pages
    content = {
        "media_type": "application/pdf",
        "page_count": len(pages),
        "pages": [
            {
                "page_number": page_number,
                "text": " ".join(page.extract_text().split()),
            }
            for page_number, page in enumerate(pages, start=1)
        ],
    }
    return {
        "available": True,
        "source_id": None,
        "source_path": str(path),
        "content": content,
        "artifact": {
            "bytes": len(raw),
            "sha256": artifact_digest,
        },
        "sha256": _digest(content),
    }


def _unavailable(source_id: int | None) -> dict[str, Any]:
    content = {"reason": "no matching retained row"}
    return {
        "available": False,
        "source_id": source_id,
        "content": content,
        "sha256": _digest(content),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Stable bytes used for both output files and semantic digests."""

    return (
        json.dumps(_json_value(value), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _digest(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()
