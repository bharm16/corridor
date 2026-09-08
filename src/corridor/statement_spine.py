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
from corridor.statement_values import CitedStatementEvidence
from corridor.fact_decisions import record_human_fact_decision
from corridor.facts import (
    append_recorded_applies_to_fact,
    append_recorded_statement_timing_fact,
    append_recorded_statement_wording_fact,
)
from corridor.models import (
    Candidate,
    CommitmentScopeMembership,
    DependencyEvent,
    Fact,
    FactAppliesTo,
    FactDecision,
    FactSource,
    SourceSegment,
)
from corridor.principals import HumanPrincipal
from corridor.prose_spans import prose_segment_filter


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
    evidence: CitedStatementEvidence | None = None,
) -> None:
    """Append the Cited statement's wording, timing, and scope decisions.

    Forward-only: the passage that supports the wording must already exist as a
    prose span; without it the statement stays legacy-only. Wording and timing
    always supersede the lineage's current decision; scope records no
    attributable Applies To no-op when it did not change.
    """

    segment = _cited_passage_segment(
        session, event.project_id, candidate_id, description, evidence
    )
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


def correct_statement_scope_on_spine(
    session: Session,
    *,
    event: DependencyEvent,
    scope_decision_id: int,
    recorder: HumanPrincipal,
) -> None:
    """Supersede the lineage's spine Applies To with the corrected scope.

    Forward-only: a statement not yet on the spine has no Applies To decision to
    correct. The wording and timing are untouched; the new Applies To Fact reuses
    the statement's own passage as its value source, and records no attributable
    no-op when the scope did not change.
    """

    subject_key = f"lineage:{event.commitment_lineage_id}"
    predecessor = current_decision_id(session, event.project_id, subject_key, "applies_to")
    if predecessor is None:
        return
    segment = _decision_value_segment(session, predecessor)
    if segment is None:
        return
    dependency_ids = tuple(sorted(_scope_decision_members(session, scope_decision_id)))
    if current_applies_to_members(session, event.project_id, subject_key) == dependency_ids:
        return
    applies = append_recorded_applies_to_fact(
        session,
        segment=segment,
        subject_key=subject_key,
        dependency_ids=dependency_ids,
        recorded_by=recorder.subject,
    )
    record_human_fact_decision(
        session,
        applies,
        principal=recorder,
        command_type="correct_statement_scope",
        idempotency_key=f"correct-statement-scope:{scope_decision_id}",
        expected_predecessor=predecessor,
    )


def mark_statement_do_not_add_on_spine(
    session: Session,
    *,
    candidate: Candidate,
    recorder: HumanPrincipal,
    disposition_id: int,
) -> None:
    """Record the Do Not Add disposition for one statement on the spine.

    Do Not Add is a statement-level record disposition: the decision's
    ``do_not_add`` disposition suppresses the subject's Project Record facts
    while it stands (ADR-0074 stage 3). The statement's ``statement_wording``
    fact anchors its Commitment Lineage; a legacy statement without one is
    materialized as part of this human save when its passage resolves, and
    stays legacy-only otherwise — never guessed.

    The anchor is candidate-keyed (``candidate:{id}``), never lineage-keyed:
    only a pending statement can be marked Not Relevant, and coordination —
    which is what creates the ``lineage:`` spine subject — is exactly what a
    pending statement has not had. The suppression clause therefore always
    matches the subject the statement's facts actually live under.
    """

    fact = _candidate_wording_fact(session, candidate)
    if fact is None:
        fact = _materialize_candidate_wording_fact(session, candidate, recorder)
    if fact is None:
        return
    predecessor = current_decision_id(
        session, candidate.project_id, fact.subject_key, "statement_wording"
    )
    record_human_fact_decision(
        session,
        fact,
        principal=recorder,
        command_type="mark_do_not_add",
        disposition="do_not_add",
        idempotency_key=f"mark-do-not-add:{disposition_id}",
        expected_predecessor=predecessor,
    )


def restore_statement_do_not_add_on_spine(
    session: Session,
    *,
    candidate: Candidate,
    recorder: HumanPrincipal,
    reversal_id: int,
) -> None:
    """Compensate an active Do Not Add decision on the statement's lineage.

    The compensating decision re-decides the same wording anchor with the
    ``restore`` disposition, restoring the predecessor state without implying
    inclusion (ADR-0074 stage 3). A statement that never reached the spine
    stays legacy-only.
    """

    subject_key = f"candidate:{candidate.id}"
    current = session.scalar(
        select(FactDecision).where(
            FactDecision.project_id == candidate.project_id,
            FactDecision.subject_key == subject_key,
            FactDecision.fact_type == "statement_wording",
            FactDecision.superseded_by.is_(None),
        )
    )
    if current is None or current.disposition != "do_not_add":
        return
    fact = session.get(Fact, current.fact_id)
    record_human_fact_decision(
        session,
        fact,
        principal=recorder,
        command_type="restore_do_not_add",
        disposition="restore",
        idempotency_key=f"restore-do-not-add:{reversal_id}",
        expected_predecessor=current.id,
    )


def _candidate_wording_fact(session: Session, candidate: Candidate) -> Fact | None:
    return session.scalar(
        select(Fact)
        .where(
            Fact.project_id == candidate.project_id,
            Fact.subject_key == f"candidate:{candidate.id}",
            Fact.fact_type == "statement_wording",
        )
        .order_by(Fact.id.desc())
        .limit(1)
    )


def _materialize_candidate_wording_fact(
    session: Session, candidate: Candidate, recorder: HumanPrincipal
) -> Fact | None:
    """Forward-write the wording anchor for a legacy statement, if resolvable."""

    fields = (candidate.payload_json or {}).get("fields") or {}
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    citations = (candidate.payload_json or {}).get("citations") or []
    evidence = None
    if len(citations) == 1 and isinstance(citations[0], dict):
        citation = citations[0]
        page_no = citation.get("page")
        document_id = citation.get("document_id")
        if (
            citation.get("verified") is True
            and isinstance(document_id, int)
            and not isinstance(page_no, bool)
            and isinstance(page_no, int)
        ):
            evidence = CitedStatementEvidence(document_id, page_no, description)
    segment = _cited_passage_segment(
        session, candidate.project_id, candidate.id, description, evidence
    )
    if segment is None:
        return None
    return append_recorded_statement_wording_fact(
        session,
        segment=segment,
        subject_key=f"candidate:{candidate.id}",
        description=description,
        recorded_by=recorder.subject,
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


def _decision_value_segment(
    session: Session, decision_id: int
) -> SourceSegment | None:
    decision = session.get(FactDecision, decision_id)
    if decision is None:
        return None
    source = session.scalar(
        select(FactSource).where(
            FactSource.fact_id == decision.fact_id,
            FactSource.role == "value_source",
        )
    )
    if source is None:
        return None
    return session.get(SourceSegment, source.source_segment_id)


def _scope_decision_members(
    session: Session, scope_decision_id: int
) -> tuple[int, ...]:
    return tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id).where(
                CommitmentScopeMembership.scope_decision_id == scope_decision_id
            )
        ).all()
    )


def _cited_passage_segment(
    session: Session,
    project_id: int,
    candidate_id: int,
    description: str,
    evidence: CitedStatementEvidence | None = None,
) -> SourceSegment | None:
    """The exact prose span supporting a Cited statement's wording, if resolvable.

    The extraction recorded a ``statement_wording`` source Fact for the Candidate
    over one value-source prose span; that span is the passage when it still
    contains the recorded description (the initial inclusion). A corrected wording
    no longer appears there, so the cited evidence for the correction resolves the
    passage instead: the one prose span on the cited page that contains the new
    description. Anything ambiguous or absent resolves to no passage rather than
    to a mismatched one — the statement stays legacy-only.
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
            prose_segment_filter(SourceSegment),
        )
        .order_by(SourceSegment.id)
    )
    if segment is not None and description in segment.exact_text:
        return segment
    if evidence is None:
        return None
    matches = [
        candidate_segment
        for candidate_segment in session.scalars(
            select(SourceSegment).where(
                SourceSegment.project_id == project_id,
                SourceSegment.document_id == evidence.document_id,
                prose_segment_filter(SourceSegment),
                SourceSegment.page_no == evidence.page_no,
            )
        ).all()
        if description in candidate_segment.exact_text
    ]
    return matches[0] if len(matches) == 1 else None
