"""The audit trail: written and read through one interface.

`models.AuditLog` states the invariant — *"Append-only. Every ledger
mutation writes here"* — and nothing enforced it. Six sites built the row
by hand with free-text `action` strings and hand-typed `entity_type`
values, and `milestones` mutated the Ledger without building one at all.

Writing and reading live together here deliberately. They had already
drifted: edit-then-accept audits against the **Candidate**, because the
reviewer edits before the Dependency exists, and the only reader queried
`entity_type == "dependency"` — so the record of what the extractor
originally said, which the route's own docstring promises survives, never
appeared on the Dependency it produced. A seam that owns the write and
not the read cannot stop that happening again.

Both columns a reader searches by are closed vocabularies now, and `record`
flushes so an entry is readable the moment it is written. What is *not*
enforced here, and is worth stating rather than implying: nothing can make
an arbitrary function call `record`. `tests/test_audit.py` walks the
mutating entry points and is still the thing that notices a new one.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import AuditLog
from corridor.principals import HumanPrincipal, require_human_principal

# The two entities a ledger mutation is recorded against. A Candidate's
# entries belong to the Dependency it becomes; `trail_for_dependency`
# joins them.
DEPENDENCY = "dependency"
CANDIDATE = "candidate"
MILESTONE = "milestone"

ENTITY_TYPES = frozenset({DEPENDENCY, CANDIDATE, MILESTONE})

# Every act this system records against the Ledger. `entity_type` was
# checked against its three constants while `action` stayed free text, so
# the column a reader filters and groups the history by was the one nothing
# spelled twice the same way.
ACCEPT_CANDIDATE = "accept_candidate"
MERGE_CANDIDATE = "merge_candidate"
EDIT_CANDIDATE = "edit_candidate"
REJECT_CANDIDATE = "reject_candidate"
SET_RESOLUTION_STRATEGY = "set_resolution_strategy"
MARK_SATISFIES_REQUIREMENT = "mark_satisfies_requirement"
LINK_MILESTONE = "link_milestone"
CREATE_MILESTONE = "create_milestone"
REVISE_MILESTONE = "revise_milestone"

ACTIONS = frozenset(
    {
        ACCEPT_CANDIDATE,
        MERGE_CANDIDATE,
        EDIT_CANDIDATE,
        REJECT_CANDIDATE,
        SET_RESOLUTION_STRATEGY,
        MARK_SATISFIES_REQUIREMENT,
        LINK_MILESTONE,
        CREATE_MILESTONE,
        REVISE_MILESTONE,
    }
)


def record(
    session: Session,
    *,
    actor: str | None = None,
    principal: HumanPrincipal | None = None,
    action: str,
    entity_type: str,
    entity_id: int,
    before: dict | None = None,
    after: dict | None = None,
) -> AuditLog:
    """Record one ledger mutation. Append-only, never updated.

    Flushes. The caller used to have to remember, because the entry is only
    reachable to a reader once it is in the database — and "mutate, record,
    flush" spread over three statements in five modules is three chances to
    write two of them.
    """
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unknown audit entity {entity_type!r}")
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    if (actor is None) == (principal is None):
        raise ValueError("pass exactly one of actor= or principal=")
    principal_subject = None
    if principal is not None:
        principal = require_human_principal(principal)
        principal_subject = principal.subject
        actor = principal.subject
    assert actor is not None
    entry = AuditLog(
        actor=actor,
        human_principal=principal_subject,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=before,
        after_json=after,
    )
    session.add(entry)
    session.flush()
    return entry


def trail_for_dependency(session: Session, dependency_id: int) -> list[AuditLog]:
    """This Dependency's history, including the Candidate it came from.

    `accept_candidate` and `merge_candidate` record the candidate id they
    resolved, which is the join: every entry written against that
    Candidate — the reviewer's edits above all — belongs to this record's
    history and was previously unreachable from it.

    Ordered by time, so the extractor's original reading precedes the
    edit that changed it and the acceptance that followed.
    """
    entries = list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == DEPENDENCY,
                AuditLog.entity_id == dependency_id,
            )
            .order_by(AuditLog.ts, AuditLog.id)
        ).all()
    )

    candidate_ids = {
        (entry.after_json or {}).get("candidate_id")
        for entry in entries
        if (entry.after_json or {}).get("candidate_id")
    }
    if candidate_ids:
        entries += session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == CANDIDATE,
                AuditLog.entity_id.in_(candidate_ids),
            )
        ).all()

    entries.sort(key=lambda e: (e.ts, e.id))
    return entries
