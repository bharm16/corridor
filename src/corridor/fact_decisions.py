"""Typed Record Inclusion decisions and Project Record revisions.

Source Facts remain observations.  This module is the authority boundary that
projects one eligible structured-cell Fact into the Project Record under one released
policy, records one atomic revision, and moves effectiveness without rewriting
either Fact or predecessor decision (ADR-0070, ADR-0071).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased

from corridor.fact_types import (
    FACT_TYPE_CONTRACTS,
    inclusion_rule_admits_human_record_decision,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.models import (
    ActiveExtractionRun,
    Fact,
    FactAppliesTo,
    FactClosureResult,
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
from corridor.refusals import STALE, database_refusal_kind


STRUCTURED_CELL_INCLUSION_POLICY = "structured-cell-record-inclusion-v1"
# Kept until the final Project Record cutover so already-merged #435/#436
# callers retain their public name while the implementation is no longer
# Stationing-only.
STATIONING_INCLUSION_POLICY = STRUCTURED_CELL_INCLUSION_POLICY


class FactDecisionRefused(ValueError):
    """A caller cannot establish an authorized typed Record Inclusion decision."""


class StaleHumanDecision(FactDecisionRefused):
    """The predecessor a Human Record Decision named was superseded first."""


# One command_type per Human Record Decision, mirrored in the SQL guard
# (migration 4a5b6c7d8e9f). The record command writes a human_principal
# revision; automatic inclusion keeps writing a released_policy revision.
HUMAN_DECISION_COMMANDS = frozenset(
    {
        "record_verbal_statement",
        "coordinate_statement",
        "correct_statement_scope",
        "correct_statement_facts",
        "mark_do_not_add",
        "restore_do_not_add",
        "resolve_discrepancy",
        "designate_support",
        "resolve_support",
    }
)

# What the Project Record does with the decided fact (ADR-0074 stage 3):
# 'include' projects it, 'do_not_add' suppresses the statement's facts, and
# 'restore' compensates a predecessor while contributing nothing itself.
HUMAN_DECISION_DISPOSITIONS = frozenset({"include", "do_not_add", "restore"})


@dataclass(frozen=True)
class InclusionDecisionResult:
    revision: ProjectRecordRevision
    decision: FactDecision
    created: bool


@dataclass(frozen=True)
class HumanDecisionResult:
    revision: ProjectRecordRevision
    decision: FactDecision
    created: bool


def record_human_fact_decision(
    session: Session,
    fact: Fact,
    *,
    principal: HumanPrincipal,
    command_type: str,
    idempotency_key: str,
    expected_predecessor: int | None = None,
    disposition: str = "include",
) -> HumanDecisionResult:
    """Record one attributable Human Record Decision on the spine (ADR-0070).

    Writes an atomic revision carrying the human principal (no released policy)
    and a typed decision, mirroring the automatic policy path. Correcting or
    reversing supersedes the named ``expected_predecessor``; a first decision
    passes ``None``. Because the set-valued human types have no effectiveness
    index, a stale predecessor is refused here rather than by the database
    unique constraint. The ``disposition`` says what the Project Record does
    with the fact; a compensating decision may re-decide the same fact its
    superseded predecessor decided (ADR-0074 stage 3).
    """

    if not idempotency_key.strip():
        raise FactDecisionRefused("Human Record Decision idempotency key is required")
    if command_type not in HUMAN_DECISION_COMMANDS:
        raise FactDecisionRefused(
            f"unrecognized human record decision command {command_type!r}"
        )
    if disposition not in HUMAN_DECISION_DISPOSITIONS:
        raise FactDecisionRefused(
            f"unrecognized human record decision disposition {disposition!r}"
        )
    contract = FACT_TYPE_CONTRACTS.get(fact.fact_type)
    if contract is None:
        raise FactDecisionRefused("Fact type has no released inclusion contract")
    if not inclusion_rule_admits_human_record_decision(contract.inclusion_rule):
        raise FactDecisionRefused(
            "Fact type is not settled by a Human Record Decision"
        )
    actor = require_human_principal(principal).subject

    lock_project(session, fact.project_id)
    try:
        outcome = session.scalar(
            select(
                func.record_human_fact_decision(
                    fact.project_id,
                    fact.id,
                    fact.subject_key,
                    fact.fact_type,
                    command_type,
                    disposition,
                    actor,
                    idempotency_key,
                    expected_predecessor,
                )
            )
        )
    except DBAPIError as exc:
        if database_refusal_kind(exc) == STALE:
            raise StaleHumanDecision(
                "Human Record Decision predecessor was superseded first"
            ) from exc
        raise
    session.expire_all()
    revision = session.get(ProjectRecordRevision, int(outcome["revision_id"]))
    decision = session.get(FactDecision, int(outcome["decision_id"]))
    return HumanDecisionResult(revision, decision, bool(outcome["created"]))


def include_structured_cell_fact_by_policy(
    session: Session,
    fact: Fact,
    *,
    idempotency_key: str,
) -> InclusionDecisionResult:
    """Atomically include one eligible Current Production Run cell Fact."""

    if not idempotency_key.strip():
        raise FactDecisionRefused("Record Inclusion idempotency key is required")
    contract = FACT_TYPE_CONTRACTS.get(fact.fact_type)
    if contract is None:
        raise FactDecisionRefused("Fact type has no released inclusion contract")
    if fact.fact_type == "external_org" and fact.external_org_value_id is None:
        raise FactDecisionRefused(
            "External Organization wording needs one exact registered alias"
        )
    if fact.fact_type == "applies_to" and session.scalar(
        select(func.count()).select_from(FactAppliesTo).where(
            FactAppliesTo.fact_id == fact.id
        )
    ) == 0:
        raise FactDecisionRefused("Applies To Fact has no scoped reference members")
    if fact.fact_type == "closure_result":
        closure_kind = session.scalar(
            select(FactClosureResult.closure_kind).where(
                FactClosureResult.fact_id == fact.id
            )
        )
        if closure_kind != "source_marked_resolved":
            raise FactDecisionRefused(
                "only a source marked-resolution result is policy eligible"
            )
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
            func.include_structured_cell_fact_decision(
                fact.project_id,
                fact.id,
                fact.subject_key,
                fact.fact_type,
                idempotency_key,
                STRUCTURED_CELL_INCLUSION_POLICY,
            )
        )
    )
    session.expire_all()
    revision = session.get(ProjectRecordRevision, int(outcome["revision_id"]))
    decision = session.get(FactDecision, int(outcome["decision_id"]))
    return InclusionDecisionResult(revision, decision, bool(outcome["created"]))


def include_current_structured_cell_facts(
    session: Session, project_id: int
) -> tuple[InclusionDecisionResult, ...]:
    """Run released structured-cell inclusion over current source Facts."""

    automatic_types = tuple(
        name
        for name, contract in FACT_TYPE_CONTRACTS.items()
        if "spreadsheet_cell" in contract.automatic_segment_kinds
    )

    return _include_current_facts(session, project_id, automatic_types)


def _include_current_facts(
    session: Session, project_id: int, fact_types: tuple[str, ...]
) -> tuple[InclusionDecisionResult, ...]:
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
            Fact.fact_type.in_(fact_types),
            or_(
                Fact.fact_type != "external_org",
                Fact.external_org_value_id.is_not(None),
            ),
            FactDisposition.id.is_(None),
            Candidate.state.in_(("accepted", "merged")),
            Candidate.merged_into.is_not(None),
        )
        .order_by(Fact.id)
    ).all()
    return tuple(
        include_structured_cell_fact_by_policy(
            session,
            fact,
            idempotency_key=(
                f"{STRUCTURED_CELL_INCLUSION_POLICY}:{fact.content_sha256}"
            ),
        )
        for fact in facts
    )


def include_stationing_fact_by_policy(
    session: Session,
    fact: Fact,
    *,
    idempotency_key: str,
) -> InclusionDecisionResult:
    """Compatibility seam for the Stationing slice merged before #449."""

    return include_structured_cell_fact_by_policy(
        session, fact, idempotency_key=idempotency_key
    )


def include_current_stationing_facts(
    session: Session, project_id: int
) -> tuple[InclusionDecisionResult, ...]:
    """Compatibility seam preserving the original Stationing-only behavior."""

    return _include_current_facts(
        session, project_id, ("station_from", "station_to")
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
