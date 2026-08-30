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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from sqlalchemy.exc import IntegrityError

from corridor import audit
from corridor.models import (
    AuditLog,
    CommitmentLineage,
    Dependency,
    FollowUpPlanReceipt,
    FollowUpPlanReversal,
    Milestone,
    ProjectRosterEntry,
    WorkDecision,
    WorkDecisionMilestoneImpact,
)
from corridor.principals import (
    HumanPrincipal,
    InvalidHumanPrincipal,
    require_human_principal,
)
from corridor.project_lock import lock_project
from corridor.statement_lifecycle import (
    CurrentStatementObservation,
    current_work_decision_filter,
    observe_current_statement,
)

ASSIGN_INTERNAL_OWNER = "assign_internal_owner"
SET_NEXT_ACTION = "set_next_action"
COMPLETE_NEXT_ACTION = "complete_next_action"
CANCEL_NEXT_ACTION = "cancel_next_action"
SET_MILESTONE_IMPACT = "set_milestone_impact"
DEFER_WORK = "defer_work"
RESUME_WORK = "resume_work"

INTERNAL_OWNER = "internal_owner"
NEXT_ACTION = "next_action"
MILESTONE_IMPACT = "milestone_impact"
DEFERRAL = "deferral"

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
DEFERRAL_REASONS = frozenset(
    {
        "waiting_for_information",
        "waiting_for_external_party",
        "assigned_to_someone_else",
    }
)

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
    _clear_deferral_if_current(session, coordination_subject, projection, recorder)
    observation = _return_observation(session, coordination_subject)
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
        return_observation=observation,
    )


def defer_work(
    session: Session,
    subject: SubjectInput,
    *,
    reason: str,
    return_date: date,
    principal: HumanPrincipal,
) -> WorkDecision:
    """Record why immediate work can wait and the date it must return.

    This is deliberately not an unknown Action Due Date.  The latter says
    the project does not know when its next step belongs; a deferral is a
    positive, attributable decision to revisit a Work Item on one date.
    """
    recorder = require_human_principal(principal)
    if not isinstance(return_date, date):
        raise ValueError("a deferral needs a return date")
    deferral_reason = _reason(
        reason,
        DEFERRAL_REASONS,
        "a deferral needs a structured reason",
    )
    coordination_subject, projection = _locked_subject(session, subject)
    observation = _return_observation(session, coordination_subject)
    tail = _consistent_tail(
        session,
        coordination_subject,
        DEFERRAL,
        _projected_deferral(projection),
    )
    after_value = _deferral_value(deferral_reason, return_date)
    if tail is not None and tail.after_value == after_value:
        return tail

    projection.deferral_reason = deferral_reason
    projection.deferral_return_date = return_date
    return _append(
        session,
        coordination_subject,
        field=DEFERRAL,
        decision_type=DEFER_WORK,
        after_value=after_value,
        tail=tail,
        recorder=recorder,
        audit_action=audit.DEFER_WORK,
        deferral_reason=deferral_reason,
        deferral_return_date=return_date,
        return_observation=observation,
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
    cancellation_reason: str | None = None,
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
    observation = observe_current_statement(session, projection.id)
    if observation is None or observation.event.event_type != "committed_date_change":
        raise ValueError("Milestone Impact belongs only to a Committed Date Change")
    milestones = session.scalars(
        select(Milestone).where(Milestone.id.in_(ids))
    ).all() if ids else []
    if len(milestones) != len(ids) or any(
        milestone.project_id != projection.project_id for milestone in milestones
    ):
        raise ValueError("Milestone Impact must name registered project Milestones")
    if impact == "not_yet_known" and (
        not projection.internal_owner
        or not projection.next_action
        or (
            projection.action_due_date is None
            and projection.action_due_date_reason is None
        )
    ):
        raise ValueError(
            "an unknown Milestone Impact remains coordinated work with an owner, "
            "action, and Action Due Date or unknown-date reason"
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
    """The current impact tail, only while the fact remains a Date Change.

    A factual successor may correctly re-derive a former Committed Date
    Change as a plain Commitment.  Its historical impact receipt remains
    auditable, but it is not current coordination state for a fact that no
    longer has a changed date.
    """
    coordination_subject = _coerce_subject(subject)
    if coordination_subject.commitment_lineage_id is None:
        return None
    observation = observe_current_statement(
        session, coordination_subject.commitment_lineage_id
    )
    if observation is None or observation.event.event_type != "committed_date_change":
        return None
    return observation.milestone_impact_decision


def current_deferral_decision(
    session: Session, subject: SubjectInput
) -> WorkDecision | None:
    """The current explicit deferral, if immediate work was deliberately delayed."""
    return _tail(session, _coerce_subject(subject), DEFERRAL)


def current_statement_decision_tails(
    session: Session,
    commitment_lineage_ids: Iterable[int],
    *,
    fields: Iterable[str],
) -> dict[tuple[int, str], WorkDecision]:
    """Return current statement-plan tails for a population in one read.

    Population readers use the same lifecycle and successor predicates as the
    single-subject services above.  This is only a batched read of attributable
    Work Decisions; it neither trusts nor repairs their projection caches.
    """
    lineage_ids = frozenset(commitment_lineage_ids)
    requested_fields = frozenset(fields)
    if not lineage_ids or not requested_fields:
        return {}
    supported_fields = {INTERNAL_OWNER, NEXT_ACTION, MILESTONE_IMPACT, DEFERRAL}
    if not requested_fields <= supported_fields:
        raise ValueError("unknown Work Decision field")

    successor = aliased(WorkDecision)
    decisions = session.scalars(
        select(WorkDecision)
        .where(
            WorkDecision.commitment_lineage_id.in_(lineage_ids),
            WorkDecision.field.in_(requested_fields),
            current_work_decision_filter(WorkDecision.id),
            ~select(successor.id)
            .where(successor.predecessor_decision_id == WorkDecision.id)
            .exists(),
        )
        .order_by(WorkDecision.id)
    ).all()
    return {
        (decision.commitment_lineage_id, decision.field): decision
        for decision in decisions
        if decision.commitment_lineage_id is not None
    }


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

    tail = _consistent_tail(
        session,
        coordination_subject,
        NEXT_ACTION,
        _projected_composite(projection),
    )
    _assert_next_action_reason_consistent(projection, tail)
    if tail is None or tail.after_value is None:
        raise ValueError(f"{_subject_label(coordination_subject)} has no current Next Action")

    if not has_successor:
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

    with session.begin_nested():
        projection.next_action = None
        projection.action_due_date = None
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
    if observe_current_statement(session, lineage.id) is None:
        raise ValueError("only an accepted Commitment or Committed Date Change may coordinate")
    return coordination_subject, lineage


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
            current_work_decision_filter(WorkDecision.id),
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
    deferral_reason: str | None = None,
    deferral_return_date: date | None = None,
    return_observation: CurrentStatementObservation | None = None,
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
        deferral_reason=deferral_reason,
        deferral_return_date=deferral_return_date,
        observed_statement_event_id=(
            return_observation.statement_event_id if return_observation else None
        ),
        observed_scope_decision_id=(
            return_observation.scope_decision_id if return_observation else None
        ),
        observed_milestone_impact_decision_id=(
            return_observation.milestone_impact_decision_id
            if return_observation
            else None
        ),
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
    projected_reason = projection.action_due_date_reason
    chained_reason = tail.action_due_date_reason if tail is not None else None
    if projected_reason != chained_reason:
        raise ValueError(
            "the next_action projection diverged from its Work Decision receipts"
        )


def _clear_deferral_if_current(
    session: Session,
    subject: CoordinationSubject,
    projection: SubjectProjection,
    recorder: HumanPrincipal,
) -> WorkDecision | None:
    """A new Next Action resumes work instead of leaving an old deferral live."""
    tail = _consistent_tail(
        session,
        subject,
        DEFERRAL,
        _projected_deferral(projection),
    )
    if tail is None or tail.after_value is None:
        return None
    projection.deferral_reason = None
    projection.deferral_return_date = None
    return _append(
        session,
        subject,
        field=DEFERRAL,
        decision_type=RESUME_WORK,
        after_value=None,
        tail=tail,
        recorder=recorder,
        audit_action=audit.RESUME_WORK,
    )


def _due_date_reason(due_date: date | None, reason: str | None) -> str | None:
    if due_date is not None:
        if reason is not None:
            raise ValueError("a dated Next Action cannot also claim an unknown-date reason")
        return None
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


def _deferral_value(reason: str, return_date: date) -> str:
    return json.dumps(
        {"reason": reason, "return_date": return_date.isoformat()},
        sort_keys=True,
        separators=(",", ":"),
    )


def _projected_deferral(projection: SubjectProjection) -> str | None:
    if projection.deferral_reason is None and projection.deferral_return_date is None:
        return None
    if projection.deferral_reason is None or projection.deferral_return_date is None:
        raise ValueError("the deferral projection lacks its reason or return date")
    return _deferral_value(projection.deferral_reason, projection.deferral_return_date)


def _return_observation(
    session: Session, subject: CoordinationSubject
) -> CurrentStatementObservation | None:
    """The current statement state a future return condition must watch."""
    if subject.commitment_lineage_id is None:
        return None
    return observe_current_statement(session, subject.commitment_lineage_id)


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


# --- The grouped Constraint Follow-up Plan (#333, ADR-0035/0038) ------------
#
# The Constraint page used to save Assigned To and Next Action as two
# independent free-text submits.  One roster-backed Save now commits the
# affected Coordination Decisions and their grouping receipt atomically, or
# nothing.  The writers above remain the only plan authority; this command
# only composes them under one optimistic predecessor check and one receipt.

UNDO_FOLLOW_UP_PLAN = "undo_follow_up_plan"

# The bounded project-language Next Action choices for a Constraint subject.
# They reuse the sentence shapes of the guided statement flow's structured
# choices with the adopted customer label "constraint" as the subject
# (ADR-0048); free text is never the mechanism for a structured choice.
FOLLOW_UP_NEXT_ACTION_CHOICES = (
    "Confirm the stated timing with the organization",
    "Coordinate this constraint with the organization",
    "Obtain additional supporting documents for this constraint",
)


class FollowUpPlanRefusal(ValueError):
    """The grouped Save or Undo refused; nothing was written."""


class StaleFollowUpPlan(FollowUpPlanRefusal):
    """A predecessor the screen showed is no longer current."""


class FollowUpPlanUndoRefusal(FollowUpPlanRefusal):
    """The grouped Undo refused; the original decisions stand."""


@dataclass(frozen=True)
class FollowUpPlanPredecessors:
    """The exact plan chain tails the coordinator read before pressing Save."""

    internal_owner_decision_id: int | None = None
    next_action_decision_id: int | None = None

    def as_json(self) -> dict[str, int | None]:
        return {
            "internal_owner_decision_id": self.internal_owner_decision_id,
            "next_action_decision_id": self.next_action_decision_id,
        }


@dataclass(frozen=True)
class FollowUpPlanDraft:
    """The one public command input for a Constraint Follow-up Plan Save."""

    dependency_id: int
    internal_owner_roster_entry_id: int
    next_action: str
    action_due_date: date | None
    action_due_date_unknown_reason: str | None
    expected: FollowUpPlanPredecessors = FollowUpPlanPredecessors()


@dataclass(frozen=True)
class FollowUpPlanResult:
    """The separately attributable rows created by one grouped plan Save.

    A decision is ``None`` when that chain already recorded the same choice;
    the receipt then names only the decisions this Save actually appended.
    """

    receipt: FollowUpPlanReceipt
    internal_owner_decision: WorkDecision | None
    next_action_decision: WorkDecision | None


def save_follow_up_plan(
    session: Session,
    draft: FollowUpPlanDraft,
    *,
    principal: HumanPrincipal,
) -> FollowUpPlanResult:
    """Record one Constraint's Follow-up Plan as one atomic, stale-checked act."""
    recorder = require_human_principal(principal)
    try:
        with session.begin_nested():
            subject, dependency = _locked_subject(session, draft.dependency_id)
            assert isinstance(dependency, Dependency)
            roster_entry = _require_active_roster_entry(
                session, dependency.project_id, draft.internal_owner_roster_entry_id
            )
            action = str(draft.next_action or "").strip()
            if action not in FOLLOW_UP_NEXT_ACTION_CHOICES:
                raise FollowUpPlanRefusal(
                    "Next Action must be one structured project-language choice"
                )
            owner_tail = _tail(session, subject, INTERNAL_OWNER)
            action_tail = _tail(session, subject, NEXT_ACTION)
            _require_current_plan_predecessors(
                dependency, owner_tail, action_tail, draft.expected
            )
            deferral_tail_before = _tail(session, subject, DEFERRAL)

            owner_decision = assign_internal_owner(
                session, subject, roster_entry.display_name, principal=recorder
            )
            action_decision = set_next_action(
                session,
                subject,
                action,
                due_date=draft.action_due_date,
                due_date_unknown_reason=draft.action_due_date_unknown_reason,
                principal=recorder,
            )
            owner_changed = owner_decision.id != _decision_row_id(owner_tail)
            action_changed = action_decision.id != _decision_row_id(action_tail)
            deferral_tail_after = _tail(session, subject, DEFERRAL)
            resumed_deferral = (
                deferral_tail_after
                if deferral_tail_after is not None
                and _decision_row_id(deferral_tail_after)
                != _decision_row_id(deferral_tail_before)
                and deferral_tail_after.decision_type == RESUME_WORK
                else None
            )
            if not owner_changed and not action_changed and resumed_deferral is None:
                raise FollowUpPlanRefusal(
                    "this Follow-up Plan already records these exact choices; "
                    "nothing would change"
                )

            audit_entry = audit.record(
                session,
                principal=recorder,
                action=audit.SAVE_FOLLOW_UP_PLAN,
                entity_type=audit.DEPENDENCY,
                entity_id=dependency.id,
                after={
                    "internal_owner_decision_id": (
                        owner_decision.id if owner_changed else None
                    ),
                    "next_action_decision_id": (
                        action_decision.id if action_changed else None
                    ),
                    "resumed_deferral_decision_id": (
                        resumed_deferral.id if resumed_deferral is not None else None
                    ),
                    "internal_owner_roster_entry_id": roster_entry.id,
                },
            )
            receipt = FollowUpPlanReceipt(
                dependency_id=dependency.id,
                internal_owner_roster_entry_id=roster_entry.id,
                internal_owner_decision_id=(
                    owner_decision.id if owner_changed else None
                ),
                next_action_decision_id=(
                    action_decision.id if action_changed else None
                ),
                resumed_deferral_decision_id=(
                    resumed_deferral.id if resumed_deferral is not None else None
                ),
                audit_log_id=audit_entry.id,
                expected_predecessors_json=draft.expected.as_json(),
                recorded_by=recorder.subject,
            )
            session.add(receipt)
            session.flush([receipt])
    except FollowUpPlanRefusal:
        raise
    except (ValueError, IntegrityError) as exc:
        raise FollowUpPlanRefusal(str(exc)) from exc
    return FollowUpPlanResult(
        receipt=receipt,
        internal_owner_decision=owner_decision if owner_changed else None,
        next_action_decision=action_decision if action_changed else None,
    )


def undo_follow_up_plan(
    session: Session,
    receipt_id: int,
    *,
    principal: HumanPrincipal,
) -> FollowUpPlanReversal:
    """Append the exact compensation for one immediately undoable plan Save.

    The original decisions are preserved; the reversal appends one successor
    decision per grouped chain restoring the value each chain held before the
    Save.  It refuses atomically once any later act depends on a result of
    the Save, and it never cascades through later work.
    """
    recorder = require_human_principal(principal)
    receipt = session.get(FollowUpPlanReceipt, receipt_id)
    if receipt is None:
        raise FollowUpPlanUndoRefusal("the grouped plan Save receipt no longer exists")
    try:
        with session.begin_nested():
            subject, dependency = _locked_subject(session, receipt.dependency_id)
            assert isinstance(dependency, Dependency)
            if (
                session.scalar(
                    select(FollowUpPlanReversal.id).where(
                        FollowUpPlanReversal.receipt_id == receipt.id
                    )
                )
                is not None
            ):
                raise FollowUpPlanUndoRefusal("this grouped Save was already undone")
            undone = {
                INTERNAL_OWNER: receipt.internal_owner_decision_id,
                NEXT_ACTION: receipt.next_action_decision_id,
                DEFERRAL: receipt.resumed_deferral_decision_id,
            }
            tails: dict[str, WorkDecision] = {}
            for field_name, decision_id in undone.items():
                if decision_id is None:
                    continue
                tail = _tail(session, subject, field_name)
                if tail is None or tail.id != decision_id:
                    raise FollowUpPlanUndoRefusal(
                        "later Follow-up Plan work depends on this Save; "
                        "use a targeted correction instead"
                    )
                tails[field_name] = tail
            _require_no_later_plan_reference(session, receipt)

            reversal_decisions: dict[str, WorkDecision] = {}
            if INTERNAL_OWNER in tails:
                tail = tails[INTERNAL_OWNER]
                restored = tail.before_value
                dependency.internal_owner = restored
                reversal_decisions[INTERNAL_OWNER] = _append(
                    session,
                    subject,
                    field=INTERNAL_OWNER,
                    decision_type=UNDO_FOLLOW_UP_PLAN,
                    after_value=restored,
                    tail=tail,
                    recorder=recorder,
                    audit_action=audit.UNDO_FOLLOW_UP_PLAN,
                )
            if NEXT_ACTION in tails:
                tail = tails[NEXT_ACTION]
                restored = tail.before_value
                predecessor = (
                    session.get(WorkDecision, tail.predecessor_decision_id)
                    if tail.predecessor_decision_id is not None
                    else None
                )
                restored_reason = (
                    predecessor.action_due_date_reason
                    if predecessor is not None
                    else None
                )
                if restored is None:
                    dependency.next_action = None
                    dependency.action_due_date = None
                    dependency.action_due_date_reason = None
                else:
                    composite = json.loads(restored)
                    dependency.next_action = composite["action"]
                    dependency.action_due_date = (
                        date.fromisoformat(composite["due_date"])
                        if composite["due_date"]
                        else None
                    )
                    dependency.action_due_date_reason = restored_reason
                reversal_decisions[NEXT_ACTION] = _append(
                    session,
                    subject,
                    field=NEXT_ACTION,
                    decision_type=UNDO_FOLLOW_UP_PLAN,
                    after_value=restored,
                    tail=tail,
                    recorder=recorder,
                    audit_action=audit.UNDO_FOLLOW_UP_PLAN,
                    action_due_date_reason=(
                        restored_reason if restored is not None else None
                    ),
                )
            if DEFERRAL in tails:
                tail = tails[DEFERRAL]
                restored = tail.before_value
                if restored is not None:
                    composite = json.loads(restored)
                    dependency.deferral_reason = composite["reason"]
                    dependency.deferral_return_date = date.fromisoformat(
                        composite["return_date"]
                    )
                    reversal_decisions[DEFERRAL] = _append(
                        session,
                        subject,
                        field=DEFERRAL,
                        decision_type=UNDO_FOLLOW_UP_PLAN,
                        after_value=restored,
                        tail=tail,
                        recorder=recorder,
                        audit_action=audit.UNDO_FOLLOW_UP_PLAN,
                        deferral_reason=composite["reason"],
                        deferral_return_date=date.fromisoformat(
                            composite["return_date"]
                        ),
                    )

            audit_entry = audit.record(
                session,
                principal=recorder,
                action=audit.UNDO_FOLLOW_UP_PLAN,
                entity_type=audit.DEPENDENCY,
                entity_id=dependency.id,
                before={"follow_up_plan_receipt_id": receipt.id},
                after={
                    "follow_up_plan_receipt_id": receipt.id,
                    "reversal_decision_ids": sorted(
                        decision.id for decision in reversal_decisions.values()
                    ),
                },
            )
            reversal = FollowUpPlanReversal(
                receipt_id=receipt.id,
                internal_owner_reversal_decision_id=_decision_row_id(
                    reversal_decisions.get(INTERNAL_OWNER)
                ),
                next_action_reversal_decision_id=_decision_row_id(
                    reversal_decisions.get(NEXT_ACTION)
                ),
                deferral_reversal_decision_id=_decision_row_id(
                    reversal_decisions.get(DEFERRAL)
                ),
                audit_log_id=audit_entry.id,
                recorded_by=recorder.subject,
            )
            session.add(reversal)
            session.flush([reversal])
    except FollowUpPlanUndoRefusal:
        raise
    except (ValueError, IntegrityError) as exc:
        raise FollowUpPlanUndoRefusal(str(exc)) from exc
    return reversal


def current_follow_up_plan_receipt(
    session: Session, dependency_id: int
) -> FollowUpPlanReceipt | None:
    """The newest grouped plan Save for this Constraint that was not undone."""
    return session.scalar(
        select(FollowUpPlanReceipt)
        .where(
            FollowUpPlanReceipt.dependency_id == dependency_id,
            ~select(FollowUpPlanReversal.id)
            .where(FollowUpPlanReversal.receipt_id == FollowUpPlanReceipt.id)
            .exists(),
        )
        .order_by(FollowUpPlanReceipt.id.desc())
        .limit(1)
    )


def _require_active_roster_entry(
    session: Session, project_id: int, roster_entry_id: int
) -> ProjectRosterEntry:
    """Assigned To is a stable roster identity, never a caller-supplied string."""
    if isinstance(roster_entry_id, bool) or not isinstance(roster_entry_id, int):
        raise FollowUpPlanRefusal(
            "Assigned To must be selected from this project's active roster"
        )
    entry = session.get(ProjectRosterEntry, roster_entry_id)
    if entry is None or entry.project_id != project_id or not entry.active:
        raise FollowUpPlanRefusal(
            "Assigned To must be selected from this project's active roster"
        )
    try:
        HumanPrincipal(entry.principal_subject)
    except InvalidHumanPrincipal as exc:
        raise FollowUpPlanRefusal(
            "the selected project roster entry has no valid human identity"
        ) from exc
    return entry


def _require_current_plan_predecessors(
    dependency: Dependency,
    owner_tail: WorkDecision | None,
    action_tail: WorkDecision | None,
    expected: FollowUpPlanPredecessors,
) -> None:
    if (
        _decision_row_id(owner_tail) == expected.internal_owner_decision_id
        and _decision_row_id(action_tail) == expected.next_action_decision_id
    ):
        return
    current = []
    current.append(
        f"Assigned To is now {dependency.internal_owner}"
        if dependency.internal_owner
        else "Assigned To is now unassigned"
    )
    current.append(
        f"Next Action is now {dependency.next_action}"
        if dependency.next_action
        else "no Next Action is recorded"
    )
    raise StaleFollowUpPlan(
        "someone changed this Follow-up Plan after this screen was read; "
        "nothing was saved. " + "; ".join(current) + "."
    )


def _require_no_later_plan_reference(
    session: Session, receipt: FollowUpPlanReceipt
) -> None:
    """Refuse Undo once a later recorded act names an exact result of this Save."""
    referenced_ids = {
        "follow_up_plan_receipt_id": {receipt.id},
        "work_decision_id": {
            receipt.internal_owner_decision_id,
            receipt.next_action_decision_id,
            receipt.resumed_deferral_decision_id,
        }
        - {None},
    }
    later_audits = session.scalars(
        select(AuditLog).where(
            AuditLog.id > receipt.audit_log_id,
            AuditLog.action != audit.PRODUCT_PROVING_FRONTEND_REQUEST,
        )
    )
    if any(
        audit.references_typed_ids(entry.after_json, referenced_ids)
        or audit.references_typed_ids(entry.before_json, referenced_ids)
        for entry in later_audits
    ):
        raise FollowUpPlanUndoRefusal(
            "a later recorded act depends on this Save; "
            "use a targeted correction instead"
        )


def _decision_row_id(decision: WorkDecision | None) -> int | None:
    return decision.id if decision is not None else None
