"""The source-append commands: how the application appends to the spine (#492).

The runtime capabilities hold no ``INSERT`` on ``source_segments``, ``facts``,
their typed satellites, the Extracted Proposal tables, or the append receipts.
Each append is a ``SECURITY DEFINER`` command owned by ``corridor_source_append``
that enforces project scope on every typed reference, the digest of every
exact text it stores or cites, locator identity, and idempotent replay.  This
module is the one place the application calls those commands; the appenders in
``facts.py``, ``source_segments.py``, and ``support_assessments.py`` shape the
values and call here.  A Fact value arrives only as a ``MaterializedValue``
sealed by ``materializer.py`` from a Source Segment's exact text (#446): this
command has no parameter that takes a value literal.

An ORM write to any of those tables, from any module, is refused by the
database; the boundary is not a convention this module asks callers to keep
(ADR-0076 as amended by ADR-0083).  Accepted-record decisions are a separate
command family in ``fact_decisions.py`` and never pass through here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Any

from sqlalchemy import BigInteger, bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session

from corridor.materializer import MaterializedValue
from corridor.models import (
    ExtractedProposal,
    Fact,
    RecordedVerbalOrigin,
    SourceFactAppendReceipt,
    SourceSegment,
    SupportAssessment,
)


@dataclass(frozen=True)
class SegmentValues:
    """One segment as the append command receives it: exact text plus locator."""

    kind: str
    exact_text: str
    content_sha256: str
    ordinal: int
    sheet_name: str | None = None
    cell_range: str | None = None
    page_no: int | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    rendition_sha256: str | None = None
    reading_sha256: str | None = None
    reader_identity: dict | None = None
    location_json: dict | None = None
    span_stream: str | None = None
    table_index: int | None = None
    cell_row: int | None = None
    cell_column: int | None = None
    row_span: int | None = None
    column_span: int | None = None


@dataclass(frozen=True)
class ClosureValues:
    """The typed closure satellite of one closure_result Fact."""

    closure_kind: str
    successor_dependency_id: int | None
    governing_source_segment_ids: tuple[int, ...]


@dataclass(frozen=True)
class TimingValues:
    """One stated timing member of a statement_timing Fact."""

    role: str
    text: str
    precision: str
    start_date: date | None
    end_date: date | None


def append_recorded_verbal_origin(
    session: Session,
    *,
    project_id: int,
    recorded_by: str,
    recorded_at: datetime,
    conversation_date: date | None,
    exact_text: str,
    content_sha256: str,
    corrects_origin_id: int | None = None,
    legacy_statement_id: int | None = None,
) -> RecordedVerbalOrigin:
    """Append one recorder's attestation, or return the one a replay already wrote.

    A Recorded Verbal Statement has no source Document to dereference, so its
    origin *is* the identity every recorded-verbal segment and Fact hangs from
    (#512, ADR-0081 stage 1).  While the dual-write of ADR-0081 stages 1
    through 5 still writes the legacy statement, that statement is the replay
    key and the command records the compatibility mapping beside the origin.
    """

    origin_id = session.scalar(
        select(
            func.append_recorded_verbal_origin(
                project_id,
                recorded_by,
                recorded_at,
                conversation_date,
                exact_text,
                content_sha256,
                corrects_origin_id,
                legacy_statement_id,
            )
        )
    )
    return session.get_one(RecordedVerbalOrigin, int(origin_id))


def append_source_segments(
    session: Session,
    *,
    project_id: int,
    document_id: int | None,
    recorded_verbal_origin_id: int | None,
    segments: Sequence[SegmentValues],
) -> tuple[SourceSegment, ...]:
    """Append one rendition's segments, in ordinal order, at most once each."""

    if not segments:
        return ()
    payload = [
        {
            "kind": segment.kind,
            "exact_text": segment.exact_text,
            "content_sha256": segment.content_sha256,
            "ordinal": segment.ordinal,
            "sheet_name": segment.sheet_name,
            "cell_range": segment.cell_range,
            "page_no": segment.page_no,
            "start_offset": segment.start_offset,
            "end_offset": segment.end_offset,
            "rendition_sha256": segment.rendition_sha256,
            "reading_sha256": segment.reading_sha256,
            "reader_identity": segment.reader_identity,
            "location_json": segment.location_json,
            "span_stream": segment.span_stream,
            "table_index": segment.table_index,
            "cell_row": segment.cell_row,
            "cell_column": segment.cell_column,
            "row_span": segment.row_span,
            "column_span": segment.column_span,
        }
        for segment in segments
    ]
    appended = session.scalar(
        select(
            func.append_source_segments(
                project_id, document_id, recorded_verbal_origin_id, _jsonb(payload)
            )
        )
    )
    rows = session.scalars(
        select(SourceSegment)
        .where(SourceSegment.id.in_(list(appended)))
        .order_by(SourceSegment.ordinal, SourceSegment.id)
    ).all()
    return tuple(rows)


def append_fact(
    session: Session,
    *,
    project_id: int,
    document_id: int | None,
    extraction_run_id: int | None,
    subject_kind: str,
    subject_key: str,
    recorded_by: str,
    content_sha256: str,
    value: MaterializedValue,
    applies_to: Sequence[int] | None = None,
    closure: ClosureValues | None = None,
    timings: Sequence[TimingValues] | None = None,
) -> Fact:
    """Append one materialized Fact with its role-tagged sources and typed satellites."""

    if not isinstance(value, MaterializedValue):
        raise TypeError("a Source Fact value must be materialized from a Source Segment")
    source_ordinals: Counter[str] = Counter()
    sources: list[dict[str, str | int]] = []
    for role, segment_id in value.source_links:
        source_ordinals[role] += 1
        sources.append({
            "role": role,
            "source_segment_id": segment_id,
            "ordinal": source_ordinals[role],
        })
    satellites: dict[str, object] = {}
    if applies_to is not None:
        satellites["applies_to"] = list(applies_to)
    if closure is not None:
        satellites["closure"] = {
            "closure_kind": closure.closure_kind,
            "successor_dependency_id": closure.successor_dependency_id,
            "governing_source_segment_ids": list(
                closure.governing_source_segment_ids
            ),
        }
    if timings is not None:
        satellites["timings"] = [
            {
                "role": timing.role,
                "text": timing.text,
                "precision": timing.precision,
                "start_date": _iso(timing.start_date),
                "end_date": _iso(timing.end_date),
            }
            for timing in timings
        ]
    fact_id = session.scalar(
        select(
            func.append_fact(
                project_id,
                document_id,
                extraction_run_id,
                value.fact_type,
                subject_kind,
                subject_key,
                value.text_value,
                value.date_value,
                value.external_org_value_id,
                value.document_value_id,
                value.transformation,
                recorded_by,
                content_sha256,
                _jsonb(sources),
                _jsonb(satellites),
            )
        )
    )
    return session.get_one(Fact, int(fact_id))


def append_extracted_proposal(
    session: Session,
    *,
    project_id: int,
    document_id: int,
    extraction_run_id: int,
    candidate_id: int,
    kind: str,
    subject_key: str,
    candidate_metadata: dict,
    fact_ids: Sequence[int],
) -> ExtractedProposal:
    """Append one proposal identity over the Facts of one subject, in order."""

    proposal_id = session.scalar(
        select(
            func.append_extracted_proposal(
                project_id,
                document_id,
                extraction_run_id,
                candidate_id,
                kind,
                subject_key,
                _jsonb(candidate_metadata),
                cast(bindparam(None, list(fact_ids)), ARRAY(BigInteger)),
            )
        )
    )
    return session.get_one(ExtractedProposal, int(proposal_id))


def append_source_fact_receipt(
    session: Session,
    *,
    project_id: int,
    document_id: int,
    extraction_run_id: int,
    idempotency_key: str,
    content_sha256: str,
) -> SourceFactAppendReceipt:
    """Bind one scoped append's key to its content, once."""

    receipt_id = session.scalar(
        select(
            func.append_source_fact_receipt(
                project_id,
                document_id,
                extraction_run_id,
                idempotency_key,
                content_sha256,
            )
        )
    )
    return session.get_one(SourceFactAppendReceipt, int(receipt_id))


def append_support_assessment(
    session: Session,
    *,
    project_id: int,
    proposition_kind: str,
    fact_id: int | None,
    extracted_proposal_id: int | None,
    source_segment_ids: Sequence[int],
    evidence_role: str,
    assessment: str,
    human_principal: str | None,
    released_policy: str | None,
    ruleset_version: str | None,
    assessed_at: datetime | None = None,
    supersedes_id: int | None = None,
) -> SupportAssessment:
    """Append one Support Assessment, or return the row a replay already wrote.

    The command derives the content digest itself, so an identical call is
    the same assessment; a different assessment of a proposition and role
    that already has an effective one must name it in ``supersedes_id``.
    """

    assessment_id = session.scalar(
        select(
            func.append_support_assessment(
                project_id,
                proposition_kind,
                fact_id,
                extracted_proposal_id,
                cast(bindparam(None, list(source_segment_ids)), ARRAY(BigInteger)),
                evidence_role,
                assessment,
                human_principal,
                released_policy,
                ruleset_version,
                assessed_at,
                supersedes_id,
            )
        )
    )
    # A supersession changed the predecessor inside the command; the identity
    # map must not keep serving its pre-supersession state.
    session.expire_all()
    return session.get_one(SupportAssessment, int(assessment_id))


def append_proposed_deltas(
    session: Session,
    *,
    project_id: int,
    source_family: str,
    source_revision: str,
    document_id: int | None = None,
    statement_id: int | None = None,
    deltas: Sequence[dict[str, Any]],
) -> tuple[int, ...]:
    """Append one atomic delta group through the source-append command (#518)."""

    if not deltas:
        return ()

    appended = session.scalar(
        select(
            func.append_proposed_deltas(
                project_id,
                source_family,
                source_revision,
                document_id,
                statement_id,
                _jsonb(deltas),
            )
        )
    )
    return tuple(appended) if appended else ()


def _jsonb(value: object):
    return cast(bindparam(None, json.dumps(value)), JSONB)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
