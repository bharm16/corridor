"""Own current-state reads for append-only guided statement lifecycle acts.

Guided coordination records distinct statement, scope, Work Decision, and
Candidate disposition facts.  The earlier per-reader filters left a reversed
Save publishable wherever a reader forgot to reimplement the exclusion.  Undo
is therefore a compensating act over one exact group, never a delete or update,
and every current-state reader now shares these filters and lineage lookup.
"""

from __future__ import annotations

from sqlalchemy import exists, or_, select

from corridor.models import (
    CandidateDisposition,
    DependencyEvent,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
)


def current_statement_event_filter(event_id):
    """Keep only unreversed statement tails, never their stale predecessors."""
    successor = DependencyEvent.__table__.alias("successor")
    return (
        ~exists(
            select(StatementCoordinationReversal.id)
            .join(
                StatementCoordinationReceipt,
                StatementCoordinationReceipt.id
                == StatementCoordinationReversal.receipt_id,
            )
            .where(StatementCoordinationReceipt.dependency_event_id == event_id)
        )
        & ~select(successor.c.id)
        .where(successor.c.supersedes_event_id == event_id)
        .exists()
    )


def current_lineage_statement(
    session, commitment_lineage_id: int
) -> DependencyEvent | None:
    """Return the current, unreversed statement at one lineage tail."""
    return session.scalar(
        select(DependencyEvent)
        .where(
            DependencyEvent.commitment_lineage_id == commitment_lineage_id,
            current_statement_event_filter(DependencyEvent.id),
        )
        .order_by(DependencyEvent.id)
    )


def current_scope_decision_filter(scope_decision_id):
    """Exclude the scope decision belonging to an undone grouped Save."""
    return ~exists(
        select(StatementCoordinationReversal.id)
        .join(
            StatementCoordinationReceipt,
            StatementCoordinationReceipt.id == StatementCoordinationReversal.receipt_id,
        )
        .where(StatementCoordinationReceipt.scope_decision_id == scope_decision_id)
    )


def current_work_decision_filter(work_decision_id):
    """Exclude the plan decisions created by an undone grouped Save."""
    return ~exists(
        select(StatementCoordinationReversal.id)
        .join(
            StatementCoordinationReceipt,
            StatementCoordinationReceipt.id == StatementCoordinationReversal.receipt_id,
        )
        .where(
            or_(
                StatementCoordinationReceipt.internal_owner_decision_id == work_decision_id,
                StatementCoordinationReceipt.next_action_decision_id == work_decision_id,
                StatementCoordinationReceipt.milestone_impact_decision_id == work_decision_id,
            )
        )
    )


def current_candidate_disposition_filter(disposition_id):
    """Exclude a disposition once its explicit restoration has been appended."""
    return ~exists(
        select(StatementCoordinationReversal.id)
        .outerjoin(
            StatementCoordinationReceipt,
            StatementCoordinationReceipt.id == StatementCoordinationReversal.receipt_id,
        )
        .where(
            or_(
                StatementCoordinationReversal.candidate_disposition_id == disposition_id,
                StatementCoordinationReceipt.candidate_disposition_id == disposition_id,
            )
        )
    )


def current_candidate_disposition(session, candidate_id: int) -> CandidateDisposition | None:
    """Return the one unreversed disposition, if the Candidate has one."""
    return session.scalar(
        select(CandidateDisposition)
        .where(
            CandidateDisposition.candidate_id == candidate_id,
            current_candidate_disposition_filter(CandidateDisposition.id),
        )
        .order_by(CandidateDisposition.id)
    )
