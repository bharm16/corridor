"""Attributable project decisions for one Coordination Subject (ADR-0038).

Assertions say what a document said and Derivations say what the rules
computed.  This module is the only writer for what the project decided:
Internal Owner, Next Action, and Milestone Impact.  Its immutable typed
receipts are authoritative; the subject projections are deliberately
defended caches of the independent receipt-chain tails.

A Coordination Subject is exactly one Dependency or accepted Commitment
Lineage.  Statement scope never manufactures a Dependency plan, and an
internal action never changes, closes, or proves the External Party fact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from corridor import audit
from corridor.models import (
    CommitmentLineage,
    Dependency,
    DependencyEvent,
    Milestone,
    WorkDecision,
    WorkDecisionMilestoneImpact,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project

ASSIGN_INTERNAL_OWNER = "assign_internal_owner"
SET_NEXT_ACTION = "set_next_action"
COMPLETE_NEXT_ACTION = "complete_next_action"
CANCEL_NEXT_ACTION = "cancel_next_action"
SET_MILESTONE_IMPACT = "set_milestone_impact"

INTERNAL_OWNER = "internal_owner"
NEXT_ACTION = "next_action"
MILESTONE_IMPACT = "milestone_impact"

MILESTONE_IMPACT_STATES = frozenset(
    {"affects", "does_not_affect", "not_yet_known"}
)
UNKNOWN_DUE_DATE_REASONS = frozenset(
    {
        "awaiting_external_information",
        "awaiting_schedule_information",
        "date_not_yet_known",
    }
)
NO_FOLLOW_UP_REASONS = frozenset(
    {
        "awaiting_external_information",
        "no_immediate_follow_up",
        "return_condition_recorded",
    }
)
CANCELLATION_REASONS = frozenset(
    {"no_longer_needed", "superseded", "recorded_in_error"}
)

# Existing callers predate structured reason capture.  Keeping the defaults
# yields a durable explicit value rather than silently exempting their old
# behavior from the new receipt contract.
_LEGACY_UNKNOWN_DUE_DATE_REASON = "date_not_yet_known"
_LEGACY_NO_FOLLOW_UP_REASON = "no_immediate_follow_up"
_LEGACY_CANCELLATION_REASON = "no_longer_needed"


@dataclass(frozen=True)
class CoordinationSubject:
    """The single thing a Work Decision is about.

    ``int`` Dependency ids remain accepted by the public service APIs for
    compatibility.  New callers use this discriminated value so a statement
    plan cannot accidentally acquire both a lineage and a Dependency subject.
    """

    dependency_id: int | None = None
    commitment_lineage_id: int | None = None

    def __post_init__(self) -> None:
        subjects = (self.dependency_id, self.commitment_lineage_id)
        if sum(subject is not None for subject in subjects) != 1:
            raise ValueError(
                "a Coordination Subject is exactly one Dependency or Commitment Lineage"
            )
        for identity in subjects:
            if identity is not None and (
                isinstance(identity, bool)
                or not isinstance(identity, int)
                or identity <= 0
            ):
                raise ValueError("a Coordination Subject needs one positive identity")

    @classmethod
    def dependency(cls, dependency_id: int) -> "CoordinationSubject":
        return cls(dependency_id=dependency_id)

    @classmethod
    def statement(cls, commitment_lineage_id: int) -> "CoordinationSubject":
        return cls(commitment_lineage_id=commitment_lineage_id)


SubjectInput = CoordinationSubject | int
SubjectProjection = Dependency | CommitmentLineage


def assign_internal_owner(
    session: Session,
    subject: SubjectInput,
    owner: str,
    *,
    principal: HumanPrincipal,
) -> WorkDecision:
    """Record who the project holds accountable for this subject's follow-up."""
    recorder = require_human_principal(principal)
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("an Internal Owner is a named person, not a blank")
    owner = owner.strip()

    coordination_subject, projection = _locked_subject(session, subject)
    tail = _consistent_tail(
        session, coordination_subject, INTERNAL_OWNER, projection.internal_owner
    )
    if tail is not None and tail.after_value == owner:
        return tail

    projection.internal_owner = owner
    return _append(
        session,
        coordination_subject,
        field=INTERNAL_OWNER,
        decision_type=ASSIGN_INTERNAL_OWNER,
        after_value=owner,
        tail=tail,
        recorder=recorder,
        audit_action=audit.ASSIGN_INTERNAL_OWNER,
    )


def set_next_action(
    session: Session,
    subject: SubjectInput,
    action: str,
    *,
    due_date: date | None = None,
    due_date_unknown_reason: str | None = None,
    principal: HumanPrincipal,
) -> WorkDecision:
    """Record the project's next step and either its date or why it is unknown."""
    recorder = require_human_principal(principal)
    if not isinstance(action, str) or not action.strip():
        raise ValueError("a Next Action is a stated step, not a blank")
    if due_date is not None and not isinstance(due_date, date):
        raise ValueError("an Action Due Date must be a date")
    reason = _due_date_reason(due_date, due_date_unknown_reason)
    composite = _composite(action.strip(), due_date)

    coordination_subject, projection = _locked_subject(session, subject)
    tail = _consistent_tail(
        session,
        coordination_subject,
        NEXT_ACTION,
        _projected_composite(projection),
    )
    _assert_next_action_reason_consistent(projection, tail)
    if (
        tail is not None
        and tail.after_value == composite
        and tail.action_due_date_reason == reason
    ):
        return tail

    projection.next_action = action.strip()
    projection.action_due_date = due_date
    if isinstance(projection, CommitmentLineage):
        projection.action_due_date_reason = reason
    return _append(
        session,
        coordination_subject,
        field=NEXT_ACTION,
        decision_type=SET_NEXT_ACTION,
        after_value=composite,
        tail=tail,
        recorder=recorder,
        audit_action=audit.SET_NEXT_ACTION,
        action_due_date_reason=reason,
    )


def complete_next_action(
    session: Session,
    subject: SubjectInput,
    *,
    principal: HumanPrincipal,
    successor_action: str | None = None,
    successor_due_date: date | None = None,
    successor_due_date_unknown_reason: str | None = None,
    no_follow_up_reason: str | None = None,
    note: str | None = None,
) -> WorkDecision:
    """Record completion without pretending it proves an External Party fact."""
    return _close_next_action(
        session,
        subject,
        principal=principal,
        decision_type=COMPLETE_NEXT_ACTION,
        audit_action=audit.COMPLETE_NEXT_ACTION,
        successor_action=successor_action,
        successor_due_date=successor_due_date,
        successor_due_date_unknown_reason=successor_due_date_unknown_reason,
        no_follow_up_reason=no_follow_up_reason,
        cancellation_reason=None,
        note=note,
    )


def cancel_next_action(
    session: Session,
    subject: SubjectInput,
    *,
    principal: HumanPrincipal,
    successor_action: str | None = None,
    successor_due_date: date | None = None,
    successor_due_date_unknown_reason: str | None = None,
    no_follow_up_reason: str | None = None,
    cancellation_reason: str | None = _LEGACY_CANCELLATION_REASON,
    note: str | None = None,
) -> WorkDecision:
    """Withdraw an action with a structured reason, never an external closure."""
    return _close_next_action(
        session,
        subject,
        principal=principal,
        decision_type=CANCEL_NEXT_ACTION,
        audit_action=audit.CANCEL_NEXT_ACTION,
        successor_action=successor_action,
        successor_due_date=successor_due_date,
        successor_due_date_unknown_reason=successor_due_date_unknown_reason,
        no_follow_up_reason=no_follow_up_reason,
        cancellation_reason=cancellation_reason,
        note=note,
    )


def set_milestone_impact(
    session: Session,
    subject: CoordinationSubject,
    impact: str,
    *,
    milestone_ids: tuple[int, ...] | list[int] = (),
    principal: HumanPrincipal,
) -> WorkDecision:
    """Record a Committed Date Change's exact Milestone Impact decision."""
    recorder = require_human_principal(principal)
    coordination_subject = _coerce_subject(subject)
    if coordination_subject.commitment_lineage_id is None:
        raise ValueError("Milestone Impact belongs only to a Committed Date Change")
    if impact not in MILESTONE_IMPACT_STATES:
        raise ValueError("Milestone Impact must affect, not affect, or remain unknown")
    ids = tuple(milestone_ids)
    if len(ids) != len(set(ids)) or any(
        isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0
        for identity in ids
    ):
        raise ValueError("Milestone Impact needs distinct registered Milestones")
    if impact == "affects" and not ids:
        raise ValueError("an affecting Milestone Impact must name Milestones")
    if impact != "affects" and ids:
        raise ValueError("only an affecting Milestone Impact names Milestones")

    _, projection = _locked_subject(session, coordination_subject)
    assert isinstance(projection, CommitmentLineage)
    current_statement = _current_statement(session, projection.id)
    if current_statement is None or current_statement.event_type != "committed_date_change":
        raise ValueError("Milestone Impact belongs only to a Committed Date Change")
    milestones = session.scalars(
        select(Milestone).where(Milestone.id.in_(ids))
    ).all() if ids else []
    if len(milestones) != len(ids) or any(
        milestone.project_id != projection.project_id for milestone in milestones
    ):
        raise ValueError("Milestone Impact must name registered project Milestones")
    if impact == "not_yet_known" and (
        not projection.internal_owner or not projection.next_action
    ):
        raise ValueError(
            "an unknown Milestone Impact remains coordinated work with an owner and action"
        )

    canonical_ids = tuple(sorted(ids))
    after_value = _milestone_impact_value(impact, canonical_ids)
    tail = _consistent_tail(
        session,
        coordination_subject,
        MILESTONE_IMPACT,
        _projected_milestone_impact(projection),
    )
    if tail is not None and tail.after_value == after_value:
        return tail

    projection.milestone_impact = impact
    projection.milestone_ids = list(canonical_ids)
    decision = _append(
        session,
        coordination_subject,
        field=MILESTONE_IMPACT,
        decision_type=SET_MILESTONE_IMPACT,
        after_value=after_value,
        tail=tail,
        recorder=recorder,
        audit_action=audit.SET_MILESTONE_IMPACT,
    )
    for milestone_id in canonical_ids:
        session.add(
            WorkDecisionMilestoneImpact(
                work_decision_id=decision.id,
                milestone_id=milestone_id,
            )
        )
    session.flush()
    return decision


def current_internal_owner_decision(
    session: Session, subject: SubjectInput
) -> WorkDecision | None:
    """The Internal Owner chain tail for one subject."""
    return _tail(session, _coerce_subject(subject), INTERNAL_OWNER)


def current_next_action_decision(
    session: Session, subject: SubjectInput
) -> WorkDecision | None:
    """The Next Action chain tail for one subject."""
    return _tail(session, _coerce_subject(subject), NEXT_ACTION)


def current_milestone_impact_decision(
    session: Session, subject: CoordinationSubject
) -> WorkDecision | None:
    """The Milestone Impact chain tail for a statement subject."""
    return _tail(session, _coerce_subject(subject), MILESTONE_IMPACT)


def _close_next_action(
    session: Session,
    subject: SubjectInput,
    *,
    principal: HumanPrincipal,
    decision_type: str,
    audit_action: str,
    successor_action: str | None,
    successor_due_date: date | None,
    successor_due_date_unknown_reason: str | None,
    no_follow_up_reason: str | None,
    cancellation_reason: str | None,
    note: str | None,
) -> WorkDecision:
    recorder = require_human_principal(principal)
    coordination_subject, projection = _locked_subject(session, subject)
    has_successor = isinstance(successor_action, str) and bool(successor_action.strip())
    if successor_action is not None and not has_successor:
        raise ValueError("a successor Next Action is a stated step, not a blank")
    if has_successor and no_follow_up_reason is not None:
        raise ValueError("an action cannot have both a successor and no-follow-up reason")
    if not has_successor:
        no_follow_up_reason = (
            _LEGACY_NO_FOLLOW_UP_REASON
            if no_follow_up_reason is None
            else no_follow_up_reason
        )
        no_follow_up_reason = _reason(
            no_follow_up_reason,
            NO_FOLLOW_UP_REASONS,
            "a completed or cancelled action needs a structured no-follow-up reason",
        )
    if decision_type == CANCEL_NEXT_ACTION:
        cancellation_reason = _reason(
            cancellation_reason,
            CANCELLATION_REASONS,
            "a cancelled action needs a structured cancellation reason",
        )
    elif cancellation_reason is not None:
        raise ValueError("only a cancelled action carries a cancellation reason")
    note = _note(note)

    tail = _consistent_tail(
        session,
        coordination_subject,
        NEXT_ACTION,
        _projected_composite(projection),
    )
    if tail is None or tail.after_value is None:
        raise ValueError(f"{_subject_label(coordination_subject)} has no current Next Action")

    with session.begin_nested():
        projection.next_action = None
        projection.action_due_date = None
        if isinstance(projection, CommitmentLineage):
            projection.action_due_date_reason = None
        decision = _append(
            session,
            coordination_subject,
            field=NEXT_ACTION,
            decision_type=decision_type,
            after_value=None,
            tail=tail,
            recorder=recorder,
            audit_action=audit_action,
            no_follow_up_reason=no_follow_up_reason,
            cancellation_reason=cancellation_reason,
            note=note,
        )
        if has_successor:
            set_next_action(
                session,
                coordination_subject,
                successor_action.strip(),
                due_date=successor_due_date,
                due_date_unknown_reason=successor_due_date_unknown_reason,
                principal=recorder,
            )
    return decision


def _locked_subject(
    session: Session, subject: SubjectInput
) -> tuple[CoordinationSubject, SubjectProjection]:
    coordination_subject = _coerce_subject(subject)
    if coordination_subject.dependency_id is not None:
        dependency = session.get(Dependency, coordination_subject.dependency_id)
        if dependency is None:
            raise ValueError(f"dependency {coordination_subject.dependency_id} does not exist")
        lock_project(session, dependency.project_id)
        session.refresh(dependency)
        if dependency.dismissed_at is not None:
            raise ValueError(
                f"{dependency.ref_code} was dismissed — no Work Decision can be recorded "
                "on a record nobody is working"
            )
        return coordination_subject, dependency

    lineage = session.get(CommitmentLineage, coordination_subject.commitment_lineage_id)
    if lineage is None:
        raise ValueError(
            f"Commitment Lineage {coordination_subject.commitment_lineage_id} does not exist"
        )
    lock_project(session, lineage.project_id)
    session.refresh(lineage)
    if _current_statement(session, lineage.id) is None:
        raise ValueError("only an accepted Commitment or Committed Date Change may coordinate")
    return coordination_subject, lineage


def _current_statement(
    session: Session, commitment_lineage_id: int
) -> DependencyEvent | None:
    superseding = DependencyEvent.__table__.alias("superseding")
    return session.scalar(
        select(DependencyEvent)
        .where(
            DependencyEvent.commitment_lineage_id == commitment_lineage_id,
            DependencyEvent.event_type.in_(("commitment", "committed_date_change")),
            DependencyEvent.attribution_state == "resolved",
            DependencyEvent.stated_external_org_id.is_not(None),
            ~select(superseding.c.id)
            .where(superseding.c.supersedes_event_id == DependencyEvent.id)
            .exists(),
        )
        .order_by(DependencyEvent.id)
    )


def _coerce_subject(subject: SubjectInput) -> CoordinationSubject:
    if isinstance(subject, CoordinationSubject):
        return subject
    if isinstance(subject, bool) or not isinstance(subject, int):
        raise ValueError("a Work Decision needs a Coordination Subject")
    return CoordinationSubject.dependency(subject)


def _consistent_tail(
    session: Session,
    subject: CoordinationSubject,
    field: str,
    projected: str | None,
) -> WorkDecision | None:
    """Return a chain tail only when its projection has not been bypassed."""
    tail = _tail(session, subject, field)
    chained = tail.after_value if tail is not None else None
    if projected != chained:
        raise ValueError(f"the {field} projection diverged from its Work Decision receipts")
    return tail


def _tail(
    session: Session, subject: CoordinationSubject, field: str
) -> WorkDecision | None:
    successor = aliased(WorkDecision)
    clause = (
        WorkDecision.dependency_id == subject.dependency_id
        if subject.dependency_id is not None
        else WorkDecision.commitment_lineage_id == subject.commitment_lineage_id
    )
    return session.scalar(
        select(WorkDecision)
        .where(
            clause,
            WorkDecision.field == field,
            ~select(successor.id)
            .where(successor.predecessor_decision_id == WorkDecision.id)
            .exists(),
        )
        .order_by(WorkDecision.id)
    )


def _append(
    session: Session,
    subject: CoordinationSubject,
    *,
    field: str,
    decision_type: str,
    after_value: str | None,
    tail: WorkDecision | None,
    recorder: HumanPrincipal,
    audit_action: str,
    action_due_date_reason: str | None = None,
    no_follow_up_reason: str | None = None,
    cancellation_reason: str | None = None,
    note: str | None = None,
) -> WorkDecision:
    decision = WorkDecision(
        dependency_id=subject.dependency_id,
        commitment_lineage_id=subject.commitment_lineage_id,
        decision_type=decision_type,
        field=field,
        before_value=tail.after_value if tail is not None else None,
        after_value=after_value,
        recorded_by=recorder.subject,
        predecessor_decision_id=tail.id if tail is not None else None,
        action_due_date_reason=action_due_date_reason,
        no_follow_up_reason=no_follow_up_reason,
        cancellation_reason=cancellation_reason,
        note=note,
    )
    session.add(decision)
    session.flush([decision])
    audit.record(
        session,
        principal=recorder,
        action=audit_action,
        entity_type=(
            audit.DEPENDENCY
            if subject.dependency_id is not None
            else audit.COMMITMENT_LINEAGE
        ),
        entity_id=_subject_identity(subject),
        after={
            "work_decision_id": decision.id,
            **(
                {"commitment_lineage_id": subject.commitment_lineage_id}
                if subject.commitment_lineage_id is not None
                else {}
            ),
        },
    )
    return decision


def _assert_next_action_reason_consistent(
    projection: SubjectProjection, tail: WorkDecision | None
) -> None:
    if not isinstance(projection, CommitmentLineage):
        return
    projected_reason = projection.action_due_date_reason
    chained_reason = tail.action_due_date_reason if tail is not None else None
    if projected_reason != chained_reason:
        raise ValueError(
            "the next_action projection diverged from its Work Decision receipts"
        )


def _due_date_reason(due_date: date | None, reason: str | None) -> str | None:
    if due_date is not None:
        if reason is not None:
            raise ValueError("a dated Next Action cannot also claim an unknown-date reason")
        return None
    if reason is None:
        return _LEGACY_UNKNOWN_DUE_DATE_REASON
    return _reason(
        reason,
        UNKNOWN_DUE_DATE_REASONS,
        "an undated Next Action needs a structured unknown-date reason",
    )


def _reason(value: str | None, allowed: frozenset[str], message: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(message)
    return value


def _note(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a Work Decision note must be meaningful when supplied")
    return value.strip()


def _composite(action: str, due_date: date | None) -> str:
    """One canonical Next Action value; its structured reason is on the receipt."""
    return json.dumps(
        {"action": action, "due_date": due_date.isoformat() if due_date else None},
        sort_keys=True,
        separators=(",", ":"),
    )


def _projected_composite(projection: SubjectProjection) -> str | None:
    if projection.next_action is None and projection.action_due_date is None:
        return None
    return _composite(projection.next_action or "", projection.action_due_date)


def _milestone_impact_value(impact: str, milestone_ids: tuple[int, ...]) -> str:
    return json.dumps(
        {"milestone_ids": list(milestone_ids), "state": impact},
        sort_keys=True,
        separators=(",", ":"),
    )


def _projected_milestone_impact(lineage: CommitmentLineage) -> str | None:
    if lineage.milestone_impact is None:
        return None
    return _milestone_impact_value(
        lineage.milestone_impact,
        tuple(sorted(lineage.milestone_ids or [])),
    )


def _subject_identity(subject: CoordinationSubject) -> int:
    return subject.dependency_id or subject.commitment_lineage_id or 0


def _subject_label(subject: CoordinationSubject) -> str:
    if subject.dependency_id is not None:
        return f"dependency {subject.dependency_id}"
    return f"Commitment Lineage {subject.commitment_lineage_id}"
