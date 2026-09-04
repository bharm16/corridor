"""The plain current and as-of Project Record projections.

ADR-0075's spine ends in "current and as-of projections", and these two reads
are that ending: the effective typed decisions of a project now, and the ones
effective at one accepted revision.  They project and never write, so nothing
here is a cache and nothing here can make a value effective.

They lived in ``current_record`` beside the pre-cutover four-reader equivalence
gate, which renders the Coordination Report, the briefing, the workbook and the
release PDF.  That gate's imports are the whole rendering stack, so every
consumer of the projection inherited a dependency on the weekly report — and
``report`` reads ``changes``, so the report diff could not read the projection
it is supposed to be rebuilt from without an import cycle (#603).  Splitting the
reader from the gate that consumes it removes that: the projection is the lower
thing, and the renderers, the equivalence gate and the diff all sit above it.
``current_record`` re-exports both names, so its callers are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.models import (
    FactAppliesTo,
    FactClosureResult,
    FactClosureSource,
    FactStatementTiming,
)


@dataclass(frozen=True)
class CurrentStatementTiming:
    """One projected member of a statement_timing decision's satellite."""

    timing_role: str
    text: str
    precision: str
    start_date: date | None
    end_date: date | None


@dataclass(frozen=True)
class CurrentRecordValue:
    project_id: int
    dependency_id: int | None
    subject_key: str
    fact_type: str
    text_value: str | None
    date_value: date | None
    date_range_start: date | None
    date_range_end: date | None
    external_org_value_id: int | None
    document_value_id: int | None
    decision_id: int
    fact_id: int
    revision_id: int
    applies_to_dependency_ids: tuple[int, ...] = ()
    closure_kind: str | None = None
    closure_successor_dependency_id: int | None = None
    closure_governing_source_segment_ids: tuple[int, ...] = ()
    statement_timings: tuple[CurrentStatementTiming, ...] = ()


def read_current_project_record(
    session: Session, project_id: int
) -> tuple[CurrentRecordValue, ...]:
    rows = session.execute(
        text(
            "select project_id, dependency_id, subject_key, fact_type, text_value, "
            "date_value, date_range_start, date_range_end, external_org_value_id, "
            "document_value_id, decision_id, fact_id, revision_id "
            "from current_project_record "
            "where project_id = :project_id order by dependency_id, fact_type"
        ),
        {"project_id": project_id},
    ).all()
    return _attach_structured_values(
        session, tuple(CurrentRecordValue(*row) for row in rows)
    )


def read_project_record_as_of_revision(
    session: Session, project_id: int, revision_id: int
) -> tuple[CurrentRecordValue, ...]:
    rows = session.execute(
        text(
            "select decisions.project_id, candidates.merged_into, decisions.subject_key, "
            "decisions.fact_type, facts.text_value, facts.date_value, "
            "facts.date_range_start, facts.date_range_end, "
            "facts.external_org_value_id, facts.document_value_id, "
            "decisions.id, facts.id, decisions.revision_id "
            "from fact_decisions decisions "
            "join facts on facts.id = decisions.fact_id "
            "left join fact_decisions successor on successor.id = decisions.superseded_by "
            "left join extracted_proposals proposals "
            "on proposals.project_id = facts.project_id "
            "and proposals.document_id = facts.document_id "
            "and proposals.extraction_run_id = facts.extraction_run_id "
            "and proposals.subject_key = facts.subject_key "
            "left join candidates on candidates.id = proposals.candidate_id "
            "where decisions.project_id = :project_id "
            "and decisions.revision_id <= :revision_id "
            "and (successor.id is null or successor.revision_id > :revision_id) "
            "and decisions.disposition = 'include' "
            "and not exists ("
            "  select 1 from fact_decisions suppression "
            "  left join fact_decisions lifted on lifted.id = suppression.superseded_by "
            "  where suppression.project_id = decisions.project_id "
            "  and suppression.subject_key = decisions.subject_key "
            "  and suppression.fact_type = 'statement_wording' "
            "  and suppression.disposition = 'do_not_add' "
            "  and suppression.revision_id <= :revision_id "
            "  and (lifted.id is null or lifted.revision_id > :revision_id)) "
            "order by candidates.merged_into, decisions.fact_type"
        ),
        {"project_id": project_id, "revision_id": revision_id},
    ).all()
    return _attach_structured_values(
        session, tuple(CurrentRecordValue(*row) for row in rows)
    )


def _attach_structured_values(
    session: Session, values: tuple[CurrentRecordValue, ...]
) -> tuple[CurrentRecordValue, ...]:
    fact_ids = tuple(value.fact_id for value in values)
    if not fact_ids:
        return values
    applies_to: dict[int, list[int]] = {}
    for fact_id, dependency_id in session.execute(
        select(FactAppliesTo.fact_id, FactAppliesTo.dependency_id)
        .where(FactAppliesTo.fact_id.in_(fact_ids))
        .order_by(FactAppliesTo.fact_id, FactAppliesTo.ordinal)
    ):
        applies_to.setdefault(fact_id, []).append(dependency_id)
    closures = {
        row.fact_id: row
        for row in session.scalars(
            select(FactClosureResult).where(FactClosureResult.fact_id.in_(fact_ids))
        )
    }
    closure_sources: dict[int, list[int]] = {}
    for fact_id, source_segment_id in session.execute(
        select(FactClosureSource.fact_id, FactClosureSource.source_segment_id)
        .where(FactClosureSource.fact_id.in_(fact_ids))
        .order_by(FactClosureSource.fact_id, FactClosureSource.ordinal)
    ):
        closure_sources.setdefault(fact_id, []).append(source_segment_id)
    timings: dict[int, list[CurrentStatementTiming]] = {}
    for row in session.scalars(
        select(FactStatementTiming)
        .where(FactStatementTiming.fact_id.in_(fact_ids))
        .order_by(FactStatementTiming.fact_id, FactStatementTiming.timing_role)
    ):
        timings.setdefault(row.fact_id, []).append(
            CurrentStatementTiming(
                timing_role=row.timing_role,
                text=row.text,
                precision=row.precision,
                start_date=row.start_date,
                end_date=row.end_date,
            )
        )
    return tuple(
        replace(
            value,
            applies_to_dependency_ids=tuple(applies_to.get(value.fact_id, ())),
            closure_kind=(
                closures[value.fact_id].closure_kind
                if value.fact_id in closures
                else None
            ),
            closure_successor_dependency_id=(
                closures[value.fact_id].successor_dependency_id
                if value.fact_id in closures
                else None
            ),
            closure_governing_source_segment_ids=tuple(
                closure_sources.get(value.fact_id, ())
            ),
            statement_timings=tuple(timings.get(value.fact_id, ())),
        )
        for value in values
    )
