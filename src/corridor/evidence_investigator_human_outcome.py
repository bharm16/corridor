"""The independent human outcome of one frozen Evidence Investigator case, as of an instant.

A frozen case is later decided by a coordinator without reference to the hidden
run: the Candidate is marked Not Relevant or accepted through a grouped Save
with a Commitment Scope, the Save may be undone or its facts corrected, and the
review's start and end are observed on the server. Two production paths record
that outcome against the frozen case: the one-time shadow capture reads it at
the moment a human runs it, and the scheduled cutoff capture reads it as it
stood at a declared cutoff. They were built as two implementations of the same
fact and drifted: one counted a stratum the evaluator never counted and that no
scope mode produces, one defined "later corrected" through the shared lineage
reader while the other looked only for a second Save receipt and so never saw a
factual correction, and the review-time cap and the outcome digest were each
spelled twice.

This module is that one reading with a time parameter. ``as_of=None`` reads the
current state; an instant reads only the human acts appended by then, through
the same lifecycle filters every other current-state reader uses. It computes
the disposition, the grouped Save's scope, the reversals, correction, undo,
unresolved, the bounded review duration, the measurement strata, and the
``human_outcome_identity`` digest, and it refuses with a reason when an accepted
disposition has no grouped Save or the acts cross the frozen project. It writes
nothing: each writer keeps its own record, immutability rule, and duplicate
refusal, and binds the outcome to the frozen case's own execution run
(ADR-0024). The strata vocabulary is declared here once so the evaluator counts
exactly what the reading can produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.evidence_investigator_runtime import sha256_json
from corridor.models import (
    Candidate,
    CandidateDisposition,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventScopeDecision,
    EvidenceInvestigationPacketReceipt,
    EvidenceInvestigationReviewObservation,
    EvidenceInvestigationShadowCase,
    EvidenceInvestigationShadowExecution,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
)
from corridor.statement_lifecycle import (
    current_candidate_disposition,
    current_lineage_statement,
)


HUMAN_OUTCOME_STRATA = (
    "single_dependency",
    "multiple_dependencies",
    "unknown_scope",
    "not_relevant",
    "party_ambiguity",
    "timing_ambiguity",
    "later_corrected",
    "unresolved",
)
# A review longer than four hours is a boundary the reviewer forgot to close,
# not a review duration, and is recorded as no duration.
MAX_REVIEW_SECONDS = 14_400


class HumanOutcomeUnreadable(ValueError):
    """The retained human acts cannot be read as one outcome for this case."""

    def __init__(self, reason: str, sentence: str) -> None:
        super().__init__(sentence)
        self.reason = reason


@dataclass(frozen=True)
class HumanOutcome:
    """What the coordinator had done with one frozen case as of the instant."""

    candidate_id: int
    run_id: int | None
    candidate_disposition: str | None
    candidate_disposition_id: int | None
    last_candidate_disposition_id: int | None
    statement_coordination_receipt_id: int | None
    scope_decision_id: int | None
    scope_mode: str | None
    selected_dependency_ids: tuple[int, ...]
    reversal_ids: tuple[int, ...]
    correction: bool
    undo: bool
    unresolved: bool
    review_seconds: float | None
    strata: tuple[str, ...]
    human_outcome_identity: str


def read_human_outcome(
    session: Session,
    shadow_case: EvidenceInvestigationShadowCase,
    *,
    as_of: datetime | None = None,
) -> HumanOutcome:
    """Read the frozen case's independent human outcome as of ``as_of`` (or now).

    Time-varying human facts are read through the lifecycle filters bounded to
    the instant; the per-receipt facts a Save fixed atomically and the frozen
    run's packet are read as they stand, because both are immutable.
    """

    candidate = session.get(Candidate, shadow_case.candidate_id)
    if candidate is None or candidate.project_id != shadow_case.project_id:
        raise HumanOutcomeUnreadable(
            "cross_project_or_missing_candidate",
            "shadow Candidate is outside its frozen project",
        )
    run_id = session.scalar(
        select(EvidenceInvestigationShadowExecution.run_id).where(
            EvidenceInvestigationShadowExecution.shadow_case_id == shadow_case.id
        )
    )

    disposition = current_candidate_disposition(session, candidate.id, as_of=as_of)
    last_disposition_query = select(CandidateDisposition.id).where(
        CandidateDisposition.candidate_id == candidate.id
    )
    if as_of is not None:
        last_disposition_query = last_disposition_query.where(
            CandidateDisposition.created_at <= as_of
        )
    last_disposition_id = session.scalar(
        last_disposition_query.order_by(CandidateDisposition.id.desc()).limit(1)
    )

    receipt = None
    scope = None
    selected_dependency_ids: tuple[int, ...] = ()
    correction = False
    if disposition is not None and disposition.disposition == "accepted":
        receipt = session.scalar(
            select(StatementCoordinationReceipt).where(
                StatementCoordinationReceipt.candidate_disposition_id == disposition.id
            )
        )
        if receipt is None:
            raise HumanOutcomeUnreadable(
                "accepted_outcome_missing_receipt",
                "accepted shadow outcome has no grouped Save receipt",
            )
        event = session.get(DependencyEvent, receipt.dependency_event_id)
        scope = session.get(DependencyEventScopeDecision, receipt.scope_decision_id)
        if event is None or scope is None or event.project_id != shadow_case.project_id:
            raise HumanOutcomeUnreadable(
                "accepted_outcome_crosses_project",
                "shadow outcome crosses its frozen project",
            )
        selected_dependency_ids = tuple(
            session.scalars(
                select(DependencyEventScope.dependency_id)
                .where(DependencyEventScope.scope_decision_id == scope.id)
                .order_by(DependencyEventScope.dependency_id)
            ).all()
        )
        current = current_lineage_statement(
            session, receipt.commitment_lineage_id, as_of=as_of
        )
        correction = current is not None and current.id != receipt.dependency_event_id

    reversal_query = select(StatementCoordinationReversal.id).where(
        StatementCoordinationReversal.candidate_id == candidate.id
    )
    if as_of is not None:
        reversal_query = reversal_query.where(
            StatementCoordinationReversal.created_at <= as_of
        )
    reversal_ids = tuple(
        session.scalars(reversal_query.order_by(StatementCoordinationReversal.id)).all()
    )
    undo = bool(reversal_ids)
    unresolved = disposition is None

    observation_query = select(EvidenceInvestigationReviewObservation).where(
        EvidenceInvestigationReviewObservation.shadow_case_id == shadow_case.id
    )
    if as_of is not None:
        observation_query = observation_query.where(
            EvidenceInvestigationReviewObservation.observed_at <= as_of
        )
    observations = {
        item.boundary: item for item in session.scalars(observation_query).all()
    }
    review_seconds = None
    if "start" in observations and "end" in observations:
        elapsed = (
            observations["end"].observed_at - observations["start"].observed_at
        ).total_seconds()
        if 0 <= elapsed <= MAX_REVIEW_SECONDS:
            review_seconds = elapsed

    strata: set[str] = set()
    if disposition is not None and disposition.disposition == "not_relevant":
        strata.add("not_relevant")
    if scope is not None:
        if scope.scope_mode == "unknown":
            strata.add("unknown_scope")
        elif len(selected_dependency_ids) == 1:
            strata.add("single_dependency")
        elif len(selected_dependency_ids) > 1:
            strata.add("multiple_dependencies")
    if correction:
        strata.add("later_corrected")
    if unresolved:
        strata.add("unresolved")
    packet = (
        session.scalar(
            select(EvidenceInvestigationPacketReceipt).where(
                EvidenceInvestigationPacketReceipt.run_id == run_id
            )
        )
        if run_id is not None
        else None
    )
    if packet is not None:
        if len(packet.packet_json.get("possible_parties") or []) > 1:
            strata.add("party_ambiguity")
        questions = " ".join(packet.packet_json.get("human_questions") or []).casefold()
        if "timing" in questions or "date" in questions:
            strata.add("timing_ambiguity")

    return HumanOutcome(
        candidate_id=candidate.id,
        run_id=run_id,
        candidate_disposition=disposition.disposition if disposition else None,
        candidate_disposition_id=disposition.id if disposition else None,
        last_candidate_disposition_id=last_disposition_id,
        statement_coordination_receipt_id=receipt.id if receipt else None,
        scope_decision_id=scope.id if scope else None,
        scope_mode=scope.scope_mode if scope else None,
        selected_dependency_ids=selected_dependency_ids,
        reversal_ids=reversal_ids,
        correction=correction,
        undo=undo,
        unresolved=unresolved,
        review_seconds=review_seconds,
        strata=tuple(sorted(strata)),
        # The digest existing rows carry: these five fields, in this spelling.
        human_outcome_identity=sha256_json(
            {
                "candidate_id": candidate.id,
                "candidate_disposition_id": disposition.id if disposition else None,
                "statement_coordination_receipt_id": receipt.id if receipt else None,
                "reversal_ids": list(reversal_ids),
                "unresolved": unresolved,
            }
        ),
    )
