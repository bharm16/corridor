"""Attributable project decisions about a Dependency's coordination state.

The third provenance class (ADR-0025): an Assertion is what a document
said, a Derivation is what the rules computed, a Work Decision is what the
project decided. This module is the one seam that records them — web forms,
report cells and Exceptions consume it; the receipt rows, the current-value
projection and the audit pointer are its implementation.

The boundaries are structural. This seam writes coordination fields only —
the Internal Owner, and the Next Action with its Action Due Date — so a
Work Decision has no path to Criticality, a Resolution Strategy, readiness,
or any External Party claim. Attribution proves who decided, not that any
work occurred: completion records the project's judgment that its own
action was carried out, never proof of external fact.
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from corridor import audit
from corridor.models import Dependency, WorkDecision
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project

ASSIGN_INTERNAL_OWNER = "assign_internal_owner"
SET_NEXT_ACTION = "set_next_action"
COMPLETE_NEXT_ACTION = "complete_next_action"
CANCEL_NEXT_ACTION = "cancel_next_action"

INTERNAL_OWNER = "internal_owner"
NEXT_ACTION = "next_action"


def assign_internal_owner(
    session: Session,
    dependency_id: int,
    owner: str,
    *,
    principal: HumanPrincipal,
) -> WorkDecision:
    """Record that the project holds ``owner`` accountable for follow-up.

    The recording principal and the Internal Owner are different facts on
    the receipt, and one person may lawfully be both: self-assignment is
    recorded like any other assignment. Assigning the owner already current
    records nothing new. A blank owner is a refusal, not a clearing —
    withdrawal is a distinct decision this seam does not yet speak.
    """
    recorder = require_human_principal(principal)
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("an Internal Owner is a named person, not a blank")
    owner = owner.strip()

    dependency = _locked_dependency(session, dependency_id)
    tail = _consistent_tail(
        session, dependency, INTERNAL_OWNER, dependency.internal_owner
    )
    if tail is not None and tail.after_value == owner:
        return tail

    dependency.internal_owner = owner
    return _append(
        session,
        dependency,
        field=INTERNAL_OWNER,
        decision_type=ASSIGN_INTERNAL_OWNER,
        after_value=owner,
        tail=tail,
        recorder=recorder,
        audit_action=audit.ASSIGN_INTERNAL_OWNER,
    )


def set_next_action(
    session: Session,
    dependency_id: int,
    action: str,
    *,
    due_date: date | None = None,
    principal: HumanPrincipal,
) -> WorkDecision:
    """Record the step the project decided must happen next.

    The Next Action carries its Action Due Date when the project sets one
    (CONTEXT.md), so one submit is one receipt covering both. The Action
    Due Date is a project-controlled date: neither the Need Date nor a
    Committed Date.
    """
    recorder = require_human_principal(principal)
    if not isinstance(action, str) or not action.strip():
        raise ValueError("a Next Action is a stated step, not a blank")
    if due_date is not None and not isinstance(due_date, date):
        raise ValueError("an Action Due Date must be a date")
    composite = _composite(action.strip(), due_date)

    dependency = _locked_dependency(session, dependency_id)
    tail = _consistent_tail(
        session,
        dependency,
        NEXT_ACTION,
        _projected_composite(dependency),
    )
    if tail is not None and tail.after_value == composite:
        return tail

    dependency.next_action = action.strip()
    dependency.action_due_date = due_date
    return _append(
        session,
        dependency,
        field=NEXT_ACTION,
        decision_type=SET_NEXT_ACTION,
        after_value=composite,
        tail=tail,
        recorder=recorder,
        audit_action=audit.SET_NEXT_ACTION,
    )


def complete_next_action(
    session: Session, dependency_id: int, *, principal: HumanPrincipal
) -> WorkDecision:
    """Record the project's judgment that its current action was carried out.

    Still not proof of external fact — that bar belongs to Evidence. Leaves
    no current Next Action unless a successor decision records one.
    """
    return _close_next_action(
        session,
        dependency_id,
        principal=principal,
        decision_type=COMPLETE_NEXT_ACTION,
        audit_action=audit.COMPLETE_NEXT_ACTION,
    )


def cancel_next_action(
    session: Session, dependency_id: int, *, principal: HumanPrincipal
) -> WorkDecision:
    """Withdraw the current action as no-longer-intended.

    Distinct from completion on purpose: a step that stopped mattering and
    a step that was carried out are different facts about the project.
    """
    return _close_next_action(
        session,
        dependency_id,
        principal=principal,
        decision_type=CANCEL_NEXT_ACTION,
        audit_action=audit.CANCEL_NEXT_ACTION,
    )


def current_internal_owner_decision(
    session: Session, dependency_id: int
) -> WorkDecision | None:
    """The owner chain's tail: the decision nothing has superseded."""
    return _tail(session, dependency_id, INTERNAL_OWNER)


def current_next_action_decision(
    session: Session, dependency_id: int
) -> WorkDecision | None:
    """The action chain's tail: the decision nothing has superseded."""
    return _tail(session, dependency_id, NEXT_ACTION)


def _close_next_action(
    session: Session,
    dependency_id: int,
    *,
    principal: HumanPrincipal,
    decision_type: str,
    audit_action: str,
) -> WorkDecision:
    recorder = require_human_principal(principal)
    dependency = _locked_dependency(session, dependency_id)
    tail = _consistent_tail(
        session,
        dependency,
        NEXT_ACTION,
        _projected_composite(dependency),
    )
    if tail is None or tail.after_value is None:
        raise ValueError(
            f"dependency {dependency_id} has no current Next Action"
        )

    dependency.next_action = None
    dependency.action_due_date = None
    return _append(
        session,
        dependency,
        field=NEXT_ACTION,
        decision_type=decision_type,
        after_value=None,
        tail=tail,
        recorder=recorder,
        audit_action=audit_action,
    )


def _locked_dependency(session: Session, dependency_id: int) -> Dependency:
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise ValueError(f"dependency {dependency_id} does not exist")
    lock_project(session, dependency.project_id)
    return dependency


def _consistent_tail(
    session: Session,
    dependency: Dependency,
    field: str,
    projected: str | None,
) -> WorkDecision | None:
    """The chain tail, refusing when the projection disagrees with it.

    A projection that diverged from its receipts can only be a write that
    went around this seam — extending the chain on top of it would launder
    the tampering into history.
    """
    tail = _tail(session, dependency.id, field)
    chained = tail.after_value if tail is not None else None
    if projected != chained:
        raise ValueError(
            f"the {field} projection diverged from its Work Decision receipts"
        )
    return tail


def _tail(
    session: Session, dependency_id: int, field: str
) -> WorkDecision | None:
    successor = aliased(WorkDecision)
    return session.scalar(
        select(WorkDecision).where(
            WorkDecision.dependency_id == dependency_id,
            WorkDecision.field == field,
            ~select(successor.id)
            .where(successor.predecessor_decision_id == WorkDecision.id)
            .exists(),
        )
    )


def _append(
    session: Session,
    dependency: Dependency,
    *,
    field: str,
    decision_type: str,
    after_value: str | None,
    tail: WorkDecision | None,
    recorder: HumanPrincipal,
    audit_action: str,
) -> WorkDecision:
    decision = WorkDecision(
        dependency_id=dependency.id,
        decision_type=decision_type,
        field=field,
        before_value=tail.after_value if tail is not None else None,
        after_value=after_value,
        recorded_by=recorder.subject,
        predecessor_decision_id=tail.id if tail is not None else None,
    )
    session.add(decision)
    session.flush([decision])
    # A pointer, never the payload: the typed receipt is the record.
    audit.record(
        session,
        principal=recorder,
        action=audit_action,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={"work_decision_id": decision.id},
    )
    return decision


def _composite(action: str, due_date: date | None) -> str:
    """One canonical value for one decision: the action and its date."""
    return json.dumps(
        {"action": action, "due_date": due_date.isoformat() if due_date else None},
        sort_keys=True,
        separators=(",", ":"),
    )


def _projected_composite(dependency: Dependency) -> str | None:
    if dependency.next_action is None and dependency.action_due_date is None:
        return None
    return _composite(
        dependency.next_action or "",
        dependency.action_due_date,
    )
