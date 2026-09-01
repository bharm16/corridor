"""Dual-write a Cited statement's guided decisions onto the spine (#451).

The guided coordination flows keep their legacy writes and add the spine
decision beside them in the same atomic act (ADR-0074). A Cited statement is
lineage-keyed exactly as a verbal is: the wording fact anchors the Commitment
Lineage, so a correction supersedes the lineage's current decision rather than
standing beside it. The typed facts are source-neutral — here their value source
is the exact Meeting Notes passage that supports the statement, located among the
prose spans the extraction already recorded; a statement whose passage cannot be
resolved stays legacy-only until cutover (#458), never guessed.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.external_statements import StatementTiming
from corridor.fact_decisions import record_human_fact_decision
from corridor.facts import (
    append_recorded_applies_to_fact,
    append_recorded_statement_timing_fact,
    append_recorded_statement_wording_fact,
)
from corridor.models import (
    CommitmentScopeMembership,
    DependencyEvent,
    Fact,
    FactAppliesTo,
    FactDecision,
    FactSource,
    SourceSegment,
)
from corridor.principals import HumanPrincipal


def record_cited_statement_on_spine(
    session: Session,
    *,
    event: DependencyEvent,
    candidate_id: int,
    description: str,
    new_timing: StatementTiming,
    previous_timing: StatementTiming | None,
    recorder: HumanPrincipal,
    command_type: str,
) -> None:
    """Append the Cited statement's wording, timing, and scope decisions.

    Forward-only: the passage that supports the wording must already exist as a
    prose span; without it the statement stays legacy-only. Wording and timing
    always supersede the lineage's current decision; scope records no
    attributable Applies To no-op when it did not change.
    """

    segment = _cited_passage_segment(session, event.project_id, candidate_id, description)
    if segment is None:
        return
    subject_key = f"lineage:{event.commitment_lineage_id}"
    operation = f"{command_type}:{event.id}"
    wording = append_recorded_statement_wording_fact(
        session,
        segment=segment,
        subject_key=subject_key,
        description=description,
        recorded_by=recorder.subject,
    )
    _decide(session, event.project_id, subject_key, wording, recorder, command_type, operation)
    timings = (("new", new_timing),) + (
        (("previous", previous_timing),) if previous_timing is not None else ()
    )
    timing = append_recorded_statement_timing_fact(
        session,
        segment=segment,
        subject_key=subject_key,
        timings=timings,
        recorded_by=recorder.subject,
    )
    _decide(session, event.project_id, subject_key, timing, recorder, command_type, operation)
    dependency_ids = tuple(sorted(event_scope_dependency_ids(session, event)))
    current_scope = current_applies_to_members(session, event.project_id, subject_key)
    if current_scope is None or current_scope != dependency_ids:
        applies = append_recorded_applies_to_fact(
            session,
            segment=segment,
            subject_key=subject_key,
            dependency_ids=dependency_ids,
            recorded_by=recorder.subject,
        )
        _decide(
            session, event.project_id, subject_key, applies, recorder, command_type, operation
        )


def _decide(
    session: Session,
    project_id: int,
    subject_key: str,
    fact: Fact,
    recorder: HumanPrincipal,
    command_type: str,
    operation: str,
) -> None:
    predecessor = current_decision_id(session, project_id, subject_key, fact.fact_type)
    record_human_fact_decision(
        session,
        fact,
        principal=recorder,
        command_type=command_type,
        idempotency_key=f"{operation}:{fact.fact_type}",
        expected_predecessor=predecessor,
    )


def current_decision_id(
    session: Session, project_id: int, subject_key: str, fact_type: str
) -> int | None:
    return session.scalar(
        select(FactDecision.id).where(
            FactDecision.project_id == project_id,
            FactDecision.subject_key == subject_key,
            FactDecision.fact_type == fact_type,
            FactDecision.superseded_by.is_(None),
        )
    )


def current_applies_to_members(
    session: Session, project_id: int, subject_key: str
) -> tuple[int, ...] | None:
    current = session.scalar(
        select(FactDecision).where(
            FactDecision.project_id == project_id,
            FactDecision.subject_key == subject_key,
            FactDecision.fact_type == "applies_to",
            FactDecision.superseded_by.is_(None),
        )
    )
    if current is None:
        return None
    return tuple(
        sorted(
            session.scalars(
                select(FactAppliesTo.dependency_id).where(
                    FactAppliesTo.fact_id == current.fact_id
                )
            ).all()
        )
    )


def event_scope_dependency_ids(
    session: Session, event: DependencyEvent
) -> tuple[int, ...]:
    from corridor.statement_lifecycle import observe_current_statement

    observation = (
        observe_current_statement(session, event.commitment_lineage_id)
        if event.commitment_lineage_id is not None
        else None
    )
    scope_decision = observation.scope_decision if observation is not None else None
    if scope_decision is None:
        return ()
    return tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id).where(
                CommitmentScopeMembership.scope_decision_id == scope_decision.id
            )
        ).all()
    )


def _cited_passage_segment(
    session: Session, project_id: int, candidate_id: int, description: str
) -> SourceSegment | None:
    """The exact prose span supporting a Cited statement's wording, if resolvable.

    The extraction recorded a ``statement_wording`` source Fact for the Candidate
    over one value-source prose span; that span is the passage. It only serves as
    the value source when it still contains the recorded description — a corrected
    wording that no longer appears in that span resolves to no passage rather than
    to a mismatched one.
    """

    segment = session.scalar(
        select(SourceSegment)
        .join(FactSource, FactSource.source_segment_id == SourceSegment.id)
        .join(Fact, Fact.id == FactSource.fact_id)
        .where(
            Fact.project_id == project_id,
            Fact.subject_key == f"candidate:{candidate_id}",
            Fact.fact_type == "statement_wording",
            FactSource.role == "value_source",
            SourceSegment.kind == "prose_span",
        )
        .order_by(SourceSegment.id)
    )
    if segment is not None and description in segment.exact_text:
        return segment
    return None
