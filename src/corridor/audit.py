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

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import AuditLog
from corridor.principals import (
    HumanPrincipal,
    InvalidHumanPrincipal,
    require_human_principal,
)

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
RECONFIRM_OPERATIVE_SUPPORT = "reconfirm_operative_support"
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
        RECONFIRM_OPERATIVE_SUPPORT,
        SET_RESOLUTION_STRATEGY,
        MARK_SATISFIES_REQUIREMENT,
        LINK_MILESTONE,
        CREATE_MILESTONE,
        REVISE_MILESTONE,
    }
)


@dataclass(frozen=True)
class AdmissionRecord:
    """Typed Admission history without exposing audit storage or JSON shape."""

    audit_id: int
    dependency_id: int
    action: str
    candidate_id: int | None
    fields: dict[str, Any] | None
    human_principal: str | None

    @property
    def candidate_link_valid(self) -> bool:
        """Whether the record names one possible database Candidate id."""

        return self.candidate_id is not None

    @property
    def attributable(self) -> bool:
        """Whether the stored actor is a valid stable human subject."""

        return _is_attributable_human_principal(self.human_principal)


@dataclass(frozen=True)
class ReconfirmationRecord:
    """Typed identity of one attributable support Reconfirmation decision."""

    audit_id: int
    dependency_id: int
    comparison_id: int | None
    finding_id: int | None
    predecessor_candidate_id: int | None
    successor_candidate_id: int | None
    new_evidence_link_id: int | None
    origin_admission_audit_id: int | None
    predecessor_reconfirmation_audit_id: int | None
    predecessor_reconfirmation_pointer_valid: bool
    human_principal: str | None

    @property
    def identity_valid(self) -> bool:
        """Whether every exact identity field is present and well shaped."""

        return (
            self.comparison_id is not None
            and self.finding_id is not None
            and self.predecessor_candidate_id is not None
            and self.successor_candidate_id is not None
            and self.new_evidence_link_id is not None
            and self.origin_admission_audit_id is not None
            and self.predecessor_reconfirmation_pointer_valid
        )

    @property
    def attributable(self) -> bool:
        """Whether the stored actor is a valid stable human subject."""

        return _is_attributable_human_principal(self.human_principal)


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


def admission_records_for_dependencies(
    session: Session, dependency_ids: Iterable[int]
) -> dict[int, tuple[AdmissionRecord, ...]]:
    """Read typed Admission decisions for many Dependencies in one query.

    Malformed legacy JSON is retained as a record with ``candidate_id=None``
    or ``fields=None``.  A safety-sensitive consumer can therefore fail closed
    without learning the storage schema or silently losing corrupt history.
    """

    ids = _dependency_ids(dependency_ids)
    if not ids:
        return {}
    grouped: dict[int, list[AdmissionRecord]] = {
        dependency_id: [] for dependency_id in ids
    }
    entries = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == DEPENDENCY,
            AuditLog.entity_id.in_(ids),
            AuditLog.action.in_((ACCEPT_CANDIDATE, MERGE_CANDIDATE)),
        )
        .order_by(AuditLog.entity_id, AuditLog.id)
    ).all()
    for entry in entries:
        after = entry.after_json if isinstance(entry.after_json, dict) else {}
        raw_fields = after.get("fields")
        grouped[entry.entity_id].append(
            AdmissionRecord(
                audit_id=entry.id,
                dependency_id=entry.entity_id,
                action=entry.action,
                candidate_id=_positive_id(after.get("candidate_id")),
                fields=(
                    deepcopy(raw_fields) if isinstance(raw_fields, dict) else None
                ),
                human_principal=entry.human_principal,
            )
        )
    return {
        dependency_id: tuple(records)
        for dependency_id, records in grouped.items()
    }


def reconfirmation_records_for_dependencies(
    session: Session, dependency_ids: Iterable[int]
) -> dict[int, tuple[ReconfirmationRecord, ...]]:
    """Read exact Reconfirmation identities for many Dependencies at once.

    The nullable predecessor pointer is valid only when the key is explicitly
    present.  That distinction makes pre-lineage or partially written audit
    JSON fail closed instead of being mistaken for the first link in a chain.
    """

    ids = _dependency_ids(dependency_ids)
    if not ids:
        return {}
    grouped: dict[int, list[ReconfirmationRecord]] = {
        dependency_id: [] for dependency_id in ids
    }
    entries = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == DEPENDENCY,
            AuditLog.entity_id.in_(ids),
            AuditLog.action == RECONFIRM_OPERATIVE_SUPPORT,
        )
        .order_by(AuditLog.entity_id, AuditLog.id)
    ).all()
    for entry in entries:
        after = entry.after_json if isinstance(entry.after_json, dict) else {}
        raw_predecessor_reconfirmation_id = after.get(
            "predecessor_reconfirmation_audit_id"
        )
        predecessor_pointer_valid = (
            "predecessor_reconfirmation_audit_id" in after
            and (
                raw_predecessor_reconfirmation_id is None
                or _positive_id(raw_predecessor_reconfirmation_id) is not None
            )
        )
        grouped[entry.entity_id].append(
            ReconfirmationRecord(
                audit_id=entry.id,
                dependency_id=entry.entity_id,
                comparison_id=_positive_id(after.get("comparison_id")),
                finding_id=_positive_id(after.get("finding_id")),
                predecessor_candidate_id=_positive_id(
                    after.get("predecessor_candidate_id")
                ),
                successor_candidate_id=_positive_id(
                    after.get("successor_candidate_id")
                ),
                new_evidence_link_id=_positive_id(
                    after.get("new_evidence_link_id")
                ),
                origin_admission_audit_id=_positive_id(
                    after.get("origin_admission_audit_id")
                ),
                predecessor_reconfirmation_audit_id=_positive_id(
                    raw_predecessor_reconfirmation_id
                ),
                predecessor_reconfirmation_pointer_valid=(
                    predecessor_pointer_valid
                ),
                human_principal=entry.human_principal,
            )
        )
    return {
        dependency_id: tuple(records)
        for dependency_id, records in grouped.items()
    }


def reconfirmed_successor_candidate_ids(
    records_by_dependency: Mapping[
        int, tuple[ReconfirmationRecord, ...]
    ],
) -> frozenset[int]:
    """Candidates named by a Reconfirmation, including corrupt old records.

    A malformed surrounding identity must not put an already-reconfirmed
    Candidate back into ordinary Admission.  The candidate id itself remains
    usable for exclusion only when it is a strict positive integer.
    """

    return frozenset(
        record.successor_candidate_id
        for records in records_by_dependency.values()
        for record in records
        if record.successor_candidate_id is not None
    )


def _dependency_ids(dependency_ids: Iterable[int]) -> tuple[int, ...]:
    ids = tuple(dict.fromkeys(dependency_ids))
    if any(_positive_id(dependency_id) is None for dependency_id in ids):
        raise ValueError("dependency ids must be positive integers")
    return ids


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _is_attributable_human_principal(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        HumanPrincipal(value)
    except InvalidHumanPrincipal:
        return False
    return True
