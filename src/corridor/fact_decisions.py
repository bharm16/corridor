"""Typed Record Inclusion decisions and Project Record revisions.

Source Facts remain observations.  This module is the authority boundary that
projects one eligible Stationing Fact into the Project Record under one released
policy, records one atomic revision, and moves effectiveness without rewriting
either Fact or predecessor decision (ADR-0070, ADR-0071).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from corridor.facts import FACT_TYPE_CONTRACTS
from corridor.models import (
    ActiveExtractionRun,
    Fact,
    FactDecision,
    FactDisposition,
    Candidate,
    ExtractedProposal,
    ExtractedProposalFact,
    FactSource,
    ProjectRecordRevision,
    SourceSegment,
)
from corridor.project_lock import lock_project


STATIONING_INCLUSION_POLICY = "stationing-record-inclusion-v1"


class FactDecisionRefused(ValueError):
    """A caller cannot establish an authorized typed Record Inclusion decision."""


@dataclass(frozen=True)
class InclusionDecisionResult:
    revision: ProjectRecordRevision
    decision: FactDecision
    created: bool


def include_stationing_fact_by_policy(
    session: Session,
    fact: Fact,
    *,
    idempotency_key: str,
) -> InclusionDecisionResult:
    """Atomically include one eligible Current Production Run Stationing Fact."""

    if not idempotency_key.strip():
        raise FactDecisionRefused("Record Inclusion idempotency key is required")
    contract = FACT_TYPE_CONTRACTS.get(fact.fact_type)
    if contract is None:
        raise FactDecisionRefused("Fact type has no released inclusion contract")
    active_run = session.scalar(
        select(ActiveExtractionRun.extraction_run_id).where(
            ActiveExtractionRun.document_id == fact.document_id
        )
    )
    if active_run != fact.extraction_run_id:
        raise FactDecisionRefused("Fact is not from the Current Production Run")
    segment_kinds = set(
        session.scalars(
            select(SourceSegment.kind)
            .join(FactSource, FactSource.source_segment_id == SourceSegment.id)
            .where(FactSource.fact_id == fact.id, FactSource.role == "value_source")
        ).all()
    )
    if not segment_kinds or not segment_kinds <= contract.automatic_segment_kinds:
        raise FactDecisionRefused("Fact support is not eligible for automatic inclusion")

    lock_project(session, fact.project_id)
    existing_revision = session.scalar(
        select(ProjectRecordRevision).where(
            ProjectRecordRevision.project_id == fact.project_id,
            ProjectRecordRevision.idempotency_key == idempotency_key,
        )
    )
    if existing_revision is not None:
        decision = session.scalar(
            select(FactDecision).where(FactDecision.revision_id == existing_revision.id)
        )
        if decision is None or decision.fact_id != fact.id:
            raise FactDecisionRefused(
                "Record Inclusion key is already bound to different content"
            )
        return InclusionDecisionResult(existing_revision, decision, False)

    current = session.scalar(
        select(FactDecision).where(
            FactDecision.project_id == fact.project_id,
            FactDecision.subject_key == fact.subject_key,
            FactDecision.fact_type == fact.fact_type,
            FactDecision.superseded_by.is_(None),
        )
    )
    if current is not None and current.fact_id == fact.id:
        revision = session.get(ProjectRecordRevision, current.revision_id)
        return InclusionDecisionResult(revision, current, False)

    outcome = session.scalar(
        select(
            func.include_stationing_fact_decision(
                fact.project_id,
                fact.id,
                fact.subject_key,
                fact.fact_type,
                idempotency_key,
                STATIONING_INCLUSION_POLICY,
            )
        )
    )
    session.expire_all()
    revision = session.get(ProjectRecordRevision, int(outcome["revision_id"]))
    decision = session.get(FactDecision, int(outcome["decision_id"]))
    return InclusionDecisionResult(revision, decision, bool(outcome["created"]))


def include_current_stationing_facts(
    session: Session, project_id: int
) -> tuple[InclusionDecisionResult, ...]:
    """Run released Stationing inclusion over current, undisposed source Facts."""

    facts = session.scalars(
        select(Fact)
        .join(
            ActiveExtractionRun,
            (ActiveExtractionRun.document_id == Fact.document_id)
            & (ActiveExtractionRun.extraction_run_id == Fact.extraction_run_id),
        )
        .join(ExtractedProposalFact, ExtractedProposalFact.fact_id == Fact.id)
        .join(
            ExtractedProposal,
            ExtractedProposal.id == ExtractedProposalFact.proposal_id,
        )
        .join(Candidate, Candidate.id == ExtractedProposal.candidate_id)
        .outerjoin(
            FactDisposition,
            FactDisposition.predecessor_fact_id == Fact.id,
        )
        .where(
            Fact.project_id == project_id,
            Fact.fact_type.in_(tuple(FACT_TYPE_CONTRACTS)),
            FactDisposition.id.is_(None),
            Candidate.state.in_(("accepted", "merged")),
            Candidate.merged_into.is_not(None),
        )
        .order_by(Fact.id)
    ).all()
    return tuple(
        include_stationing_fact_by_policy(
            session,
            fact,
            idempotency_key=f"{STATIONING_INCLUSION_POLICY}:{fact.content_sha256}",
        )
        for fact in facts
    )

def current_fact_decisions(session: Session, project_id: int) -> tuple[FactDecision, ...]:
    return tuple(
        session.scalars(
            select(FactDecision)
            .where(
                FactDecision.project_id == project_id,
                FactDecision.superseded_by.is_(None),
            )
            .order_by(FactDecision.subject_key, FactDecision.fact_type)
        ).all()
    )


def fact_decisions_as_of_revision(
    session: Session, project_id: int, revision_id: int
) -> tuple[FactDecision, ...]:
    successor = aliased(FactDecision)
    return tuple(
        session.scalars(
            select(FactDecision)
            .outerjoin(successor, successor.id == FactDecision.superseded_by)
            .where(
                FactDecision.project_id == project_id,
                FactDecision.revision_id <= revision_id,
                (successor.id.is_(None)) | (successor.revision_id > revision_id),
            )
            .order_by(FactDecision.subject_key, FactDecision.fact_type)
        ).all()
    )
