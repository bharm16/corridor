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
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import AuditLog

# The two entities a ledger mutation is recorded against. A Candidate's
# entries belong to the Dependency it becomes; `trail_for_dependency`
# joins them.
DEPENDENCY = "dependency"
CANDIDATE = "candidate"
MILESTONE = "milestone"


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int,
    before: dict | None = None,
    after: dict | None = None,
) -> AuditLog:
    """Record one ledger mutation. Append-only, never updated."""
    if entity_type not in (DEPENDENCY, CANDIDATE, MILESTONE):
        raise ValueError(f"unknown audit entity {entity_type!r}")
    entry = AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=before,
        after_json=after,
    )
    session.add(entry)
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
