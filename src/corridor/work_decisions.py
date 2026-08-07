"""Attributable project decisions about a Dependency's coordination state.

The third provenance class (ADR-0025): an Assertion is what a document
said, a Derivation is what the rules computed, a Work Decision is what the
project decided. This module is the one seam that records them — web forms,
report cells and Exceptions consume it; the receipt rows, the current-value
projection and the audit pointer are its implementation.

The boundaries are structural. This seam writes coordination fields only —
today, the Internal Owner — so a Work Decision has no path to Criticality,
a Resolution Strategy, readiness, or any External Party claim. Attribution
proves who decided, not that any work occurred.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from corridor import audit
from corridor.models import Dependency, WorkDecision
from corridor.principals import require_human_principal
from corridor.project_lock import lock_project

ASSIGN_INTERNAL_OWNER = "assign_internal_owner"
INTERNAL_OWNER = "internal_owner"


def assign_internal_owner(
    session: Session,
    dependency_id: int,
    owner: str,
    *,
    principal,
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

    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise ValueError(f"dependency {dependency_id} does not exist")
    lock_project(session, dependency.project_id)

    tail = current_internal_owner_decision(session, dependency_id)
    projected = dependency.internal_owner
    chained = tail.after_value if tail is not None else None
    if projected != chained:
        raise ValueError(
            "the Internal Owner projection diverged from its Work Decision "
            "receipts"
        )
    if chained == owner:
        assert tail is not None
        return tail

    decision = WorkDecision(
        dependency_id=dependency_id,
        decision_type=ASSIGN_INTERNAL_OWNER,
        field=INTERNAL_OWNER,
        before_value=chained,
        after_value=owner,
        recorded_by=recorder.subject,
        predecessor_decision_id=tail.id if tail is not None else None,
    )
    session.add(decision)
    dependency.internal_owner = owner
    session.flush([decision])
    # A pointer, never the payload: the typed receipt is the record.
    audit.record(
        session,
        principal=recorder,
        action=audit.ASSIGN_INTERNAL_OWNER,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency_id,
        after={"work_decision_id": decision.id},
    )
    return decision


def current_internal_owner_decision(
    session: Session, dependency_id: int
) -> WorkDecision | None:
    """The chain tail: the decision no later decision has superseded."""
    successor = aliased(WorkDecision)
    return session.scalar(
        select(WorkDecision).where(
            WorkDecision.dependency_id == dependency_id,
            WorkDecision.field == INTERNAL_OWNER,
            ~select(successor.id)
            .where(successor.predecessor_decision_id == WorkDecision.id)
            .exists(),
        )
    )
