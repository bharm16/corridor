"""Own current-state reads for append-only guided statement lifecycle acts.

Guided coordination records distinct statement, scope, Work Decision, and
Candidate disposition facts.  The earlier per-reader filters left a reversed
Save publishable wherever a reader forgot to reimplement the exclusion.  Undo
is therefore a compensating act over one exact group, never a delete or update,
and every current-state reader now shares these filters and lineage lookup.

The filters and the two single-row readers take an optional instant.  Without
one they read the current state; with one they read the state as it stood at
that instant, counting only the rows appended by then.  The Evidence
Investigator's cutoff capture used to spell that as-of form as its own queries
beside these, and the two drifted; one rule with a time parameter keeps the
current reading and the as-of reading the same reading.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import aliased

from corridor.models import (
    CandidateDisposition,
    ExternalPartyStatement,
    CommitmentScopeDecision,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
    WorkDecision,
)


@dataclass(frozen=True)
class CurrentStatementObservation:
    """The current External Party fact and project response it can invalidate."""

    event: ExternalPartyStatement
    scope_decision: CommitmentScopeDecision | None
    milestone_impact_decision: WorkDecision | None

    @property
    def statement_event_id(self) -> int:
        return self.event.id

    @property
    def scope_decision_id(self) -> int | None:
        return self.scope_decision.id if self.scope_decision is not None else None

    @property
    def milestone_impact_decision_id(self) -> int | None:
        return (
            self.milestone_impact_decision.id
            if self.milestone_impact_decision is not None
            else None
        )


def current_statement_event_filter(event_id, *, as_of: datetime | None = None):
    """Keep only unreversed statement tails, never their stale predecessors."""
    successor = ExternalPartyStatement.__table__.alias("successor")
    reversed_ = (
        select(StatementCoordinationReversal.id)
        .join(
            StatementCoordinationReceipt,
            StatementCoordinationReceipt.id == StatementCoordinationReversal.receipt_id,
        )
        .where(StatementCoordinationReceipt.dependency_event_id == event_id)
    )
    superseded = select(successor.c.id).where(
        successor.c.supersedes_event_id == event_id
    )
    if as_of is not None:
        reversed_ = reversed_.where(StatementCoordinationReversal.created_at <= as_of)
        superseded = superseded.where(successor.c.created_at <= as_of)
    return ~exists(reversed_) & ~superseded.exists()


def current_lineage_statement(
    session, commitment_lineage_id: int, *, as_of: datetime | None = None
) -> ExternalPartyStatement | None:
    """Return the current, unreversed statement at one lineage tail."""
    query = select(ExternalPartyStatement).where(
        ExternalPartyStatement.commitment_lineage_id == commitment_lineage_id,
        current_statement_event_filter(ExternalPartyStatement.id, as_of=as_of),
    )
    if as_of is not None:
        query = query.where(ExternalPartyStatement.created_at <= as_of)
    return session.scalar(query.order_by(ExternalPartyStatement.id))


def observe_current_statement(
    session, commitment_lineage_id: int
) -> CurrentStatementObservation | None:
    """Return the current fact a statement Coordination Plan is answering."""
    return observe_current_statements(session, (commitment_lineage_id,)).get(
        commitment_lineage_id
    )


def observe_current_statements(
    session, commitment_lineage_ids: Iterable[int]
) -> dict[int, CurrentStatementObservation]:
    """Return current statement observations with one bounded read per fact kind."""
    lineage_ids = tuple(dict.fromkeys(commitment_lineage_ids))
    if not lineage_ids:
        return {}
    events = session.scalars(
        select(ExternalPartyStatement)
        .where(
            ExternalPartyStatement.commitment_lineage_id.in_(lineage_ids),
            ExternalPartyStatement.event_type.in_(
                ("commitment", "committed_date_change")
            ),
            ExternalPartyStatement.attribution_state == "resolved",
            ExternalPartyStatement.stated_external_org_id.is_not(None),
            current_statement_event_filter(ExternalPartyStatement.id),
        )
        .order_by(
            ExternalPartyStatement.commitment_lineage_id, ExternalPartyStatement.id
        )
    ).all()
    events_by_lineage = {
        event.commitment_lineage_id: event
        for event in events
        if event.commitment_lineage_id is not None
    }
    if not events_by_lineage:
        return {}

    scope_successor = aliased(CommitmentScopeDecision)
    scopes = session.scalars(
        select(CommitmentScopeDecision)
        .where(
            CommitmentScopeDecision.event_id.in_(
                event.id for event in events_by_lineage.values()
            ),
            current_scope_decision_filter(CommitmentScopeDecision.id),
            ~select(scope_successor.id)
            .where(
                scope_successor.supersedes_scope_decision_id
                == CommitmentScopeDecision.id
            )
            .exists(),
        )
        .order_by(
            CommitmentScopeDecision.event_id,
            CommitmentScopeDecision.id,
        )
    ).all()
    scopes_by_event = {scope.event_id: scope for scope in scopes}

    date_change_lineage_ids = tuple(
        lineage_id
        for lineage_id, event in events_by_lineage.items()
        if event.event_type == "committed_date_change"
    )
    impacts_by_lineage: dict[int, WorkDecision] = {}
    if date_change_lineage_ids:
        impact_successor = aliased(WorkDecision)
        impacts = session.scalars(
            select(WorkDecision)
            .where(
                WorkDecision.commitment_lineage_id.in_(date_change_lineage_ids),
                WorkDecision.field == "milestone_impact",
                current_work_decision_filter(WorkDecision.id),
                ~select(impact_successor.id)
                .where(impact_successor.predecessor_decision_id == WorkDecision.id)
                .exists(),
            )
            .order_by(WorkDecision.commitment_lineage_id, WorkDecision.id)
        ).all()
        impacts_by_lineage = {
            impact.commitment_lineage_id: impact
            for impact in impacts
            if impact.commitment_lineage_id is not None
        }
        from corridor.coordination_history import compatibility_statement_tails

        native_lineages, native = compatibility_statement_tails(
            session, date_change_lineage_ids, ("milestone_impact",)
        )
        impacts_by_lineage = {
            key: value for key, value in impacts_by_lineage.items() if key not in native_lineages
        }
        impacts_by_lineage.update({key[0]: value for key, value in native.items()})
    return {
        lineage_id: CurrentStatementObservation(
            event,
            scopes_by_event.get(event.id),
            impacts_by_lineage.get(lineage_id),
        )
        for lineage_id, event in events_by_lineage.items()
    }


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
                StatementCoordinationReceipt.internal_owner_decision_id
                == work_decision_id,
                StatementCoordinationReceipt.next_action_decision_id
                == work_decision_id,
                StatementCoordinationReceipt.milestone_impact_decision_id
                == work_decision_id,
            )
        )
    )


def current_candidate_disposition_filter(
    disposition_id, *, as_of: datetime | None = None
):
    """Exclude a disposition once its explicit restoration has been appended."""
    reversed_ = (
        select(StatementCoordinationReversal.id)
        .outerjoin(
            StatementCoordinationReceipt,
            StatementCoordinationReceipt.id == StatementCoordinationReversal.receipt_id,
        )
        .where(
            or_(
                StatementCoordinationReversal.candidate_disposition_id
                == disposition_id,
                StatementCoordinationReceipt.candidate_disposition_id == disposition_id,
            )
        )
    )
    if as_of is not None:
        reversed_ = reversed_.where(StatementCoordinationReversal.created_at <= as_of)
    return ~exists(reversed_)


def current_candidate_disposition(
    session, candidate_id: int, *, as_of: datetime | None = None
) -> CandidateDisposition | None:
    """Return the one unreversed disposition, if the Candidate has one."""
    query = select(CandidateDisposition).where(
        CandidateDisposition.candidate_id == candidate_id,
        current_candidate_disposition_filter(CandidateDisposition.id, as_of=as_of),
    )
    if as_of is not None:
        query = query.where(CandidateDisposition.created_at <= as_of)
    return session.scalar(query.order_by(CandidateDisposition.id))
