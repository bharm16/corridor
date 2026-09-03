"""The source-append commands: how the application appends to the spine (#492).

The runtime capabilities hold no ``INSERT`` on ``source_segments``, ``facts``,
their typed satellites, the Extracted Proposal tables, or the append receipts.
Each append is a ``SECURITY DEFINER`` command owned by ``corridor_source_append``
that enforces project scope on every typed reference, the digest of every
exact text it stores or cites, locator identity, and idempotent replay.  This
module is the one place the application calls those commands; the appenders in
``facts.py`` and ``source_segments.py`` shape the values and call here.

An ORM write to any of those tables, from any module, is refused by the
database; the boundary is not a convention this module asks callers to keep
(ADR-0076 as amended by ADR-0083).  Accepted-record decisions are a separate
command family in ``fact_decisions.py`` and never pass through here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
import json

from sqlalchemy import BigInteger, bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session

from corridor.models import (
    ExtractedProposal,
    Fact,
    SourceFactAppendReceipt,
    SourceSegment,
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


def append_source_segments(
    session: Session,
    *,
    project_id: int,
    document_id: int | None,
    statement_id: int | None,
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
        }
        for segment in segments
    ]
    appended = session.scalar(
        select(
            func.append_source_segments(
                project_id, document_id, statement_id, _jsonb(payload)
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
    fact_type: str,
    subject_kind: str,
    subject_key: str,
    transformation: str,
    recorded_by: str,
    content_sha256: str,
    sources: Sequence[tuple[str, int]],
    text_value: str | None = None,
    date_value: date | None = None,
    external_org_value_id: int | None = None,
    document_value_id: int | None = None,
    applies_to: Sequence[int] | None = None,
    closure: ClosureValues | None = None,
    timings: Sequence[TimingValues] | None = None,
) -> Fact:
    """Append one Fact with its role-tagged sources and typed satellites."""

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
                fact_type,
                subject_kind,
                subject_key,
                text_value,
                date_value,
                external_org_value_id,
                document_value_id,
                transformation,
                recorded_by,
                content_sha256,
                _jsonb(
                    [
                        {"role": role, "source_segment_id": segment_id, "ordinal": 1}
                        for role, segment_id in sources
                    ]
                ),
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


def _jsonb(value: object):
    return cast(bindparam(None, json.dumps(value)), JSONB)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
