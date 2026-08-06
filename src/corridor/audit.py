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

from corridor.models import AuditLog, ReconfirmationReceipt
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
class ReconfirmationScope:
    """One canonical role-scoped source Evidence identity in a receipt."""

    role: str
    field_name: str | None
    evidence_link_id: int


@dataclass(frozen=True)
class ReconfirmationMove:
    """One exact source scope transferred to one EvidenceLink."""

    scope: ReconfirmationScope
    to_evidence_link_id: int


@dataclass(frozen=True)
class ReadinessAuditState:
    """Current and historical true states reconstructed from human acts."""

    current_evidence_ids: frozenset[int]
    ever_satisfying_evidence_ids: frozenset[int]


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
    operative_scopes: tuple[ReconfirmationScope, ...]
    scope_fingerprint: tuple[ReconfirmationScope, ...]
    moved_scopes: tuple[ReconfirmationMove, ...]
    transfer_receipt_valid: bool
    durable_successor_candidate_id: int | None
    durable_receipt_present: bool
    durable_receipt_matches: bool
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
            and self.transfer_receipt_valid
            and (
                not self.durable_receipt_present
                or self.durable_receipt_matches
            )
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
    durable_receipts = tuple(
        session.scalars(
            select(ReconfirmationReceipt)
            .where(ReconfirmationReceipt.dependency_id.in_(ids))
            .order_by(
                ReconfirmationReceipt.dependency_id,
                ReconfirmationReceipt.audit_log_id,
            )
        ).all()
    )
    receipts_by_audit_id = {
        receipt.audit_log_id: receipt for receipt in durable_receipts
    }
    normal_entries = tuple(
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == DEPENDENCY,
                AuditLog.entity_id.in_(ids),
                AuditLog.action == RECONFIRM_OPERATIVE_SUPPORT,
            )
        ).all()
    )
    entries_by_id = {entry.id: entry for entry in normal_entries}
    unresolved_receipt_ids = tuple(
        entry.id
        for entry in normal_entries
        if entry.id not in receipts_by_audit_id
    )
    if unresolved_receipt_ids:
        receipts_by_audit_id.update(
            {
                receipt.audit_log_id: receipt
                for receipt in session.scalars(
                    select(ReconfirmationReceipt).where(
                        ReconfirmationReceipt.audit_log_id.in_(
                            unresolved_receipt_ids
                        )
                    )
                ).all()
            }
        )
    receipt_audit_ids = tuple(receipts_by_audit_id)
    if receipt_audit_ids:
        entries_by_id.update(
            {
                entry.id: entry
                for entry in session.scalars(
                    select(AuditLog).where(AuditLog.id.in_(receipt_audit_ids))
                ).all()
            }
        )
    entries = tuple(
        sorted(
            entries_by_id.values(),
            key=lambda entry: (
                receipts_by_audit_id[entry.id].dependency_id
                if entry.id in receipts_by_audit_id
                else entry.entity_id,
                entry.id,
            ),
        )
    )
    for entry in entries:
        durable_receipt = receipts_by_audit_id.get(entry.id)
        dependency_id = (
            durable_receipt.dependency_id
            if durable_receipt is not None
            else entry.entity_id
        )
        if dependency_id not in grouped:
            continue
        before = entry.before_json if isinstance(entry.before_json, dict) else {}
        after = entry.after_json if isinstance(entry.after_json, dict) else {}
        durable_receipt_present = durable_receipt is not None
        durable_successor_candidate_id = (
            _positive_id(durable_receipt.successor_candidate_id)
            if durable_receipt is not None
            else None
        )
        new_evidence_link_id = _positive_id(
            after.get("new_evidence_link_id")
        )
        (
            operative_scopes,
            scope_fingerprint,
            moved_scopes,
            transfer_receipt_valid,
        ) = _parse_reconfirmation_transfer_receipt(
            before,
            after,
            new_evidence_link_id=new_evidence_link_id,
        )
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
        grouped[dependency_id].append(
            ReconfirmationRecord(
                audit_id=entry.id,
                dependency_id=dependency_id,
                comparison_id=_positive_id(after.get("comparison_id")),
                finding_id=_positive_id(after.get("finding_id")),
                predecessor_candidate_id=_positive_id(
                    after.get("predecessor_candidate_id")
                ),
                successor_candidate_id=_positive_id(
                    after.get("successor_candidate_id")
                ),
                new_evidence_link_id=new_evidence_link_id,
                origin_admission_audit_id=_positive_id(
                    after.get("origin_admission_audit_id")
                ),
                predecessor_reconfirmation_audit_id=_positive_id(
                    raw_predecessor_reconfirmation_id
                ),
                predecessor_reconfirmation_pointer_valid=(
                    predecessor_pointer_valid
                ),
                operative_scopes=operative_scopes,
                scope_fingerprint=scope_fingerprint,
                moved_scopes=moved_scopes,
                transfer_receipt_valid=transfer_receipt_valid,
                durable_successor_candidate_id=(
                    durable_successor_candidate_id
                ),
                durable_receipt_present=durable_receipt_present,
                durable_receipt_matches=(
                    durable_receipt is not None
                    and entry.action == RECONFIRM_OPERATIVE_SUPPORT
                    and entry.entity_type == DEPENDENCY
                    and entry.entity_id == durable_receipt.dependency_id
                    and durable_receipt.before_json == entry.before_json
                    and durable_receipt.after_json == entry.after_json
                    and durable_successor_candidate_id
                    == _positive_id(after.get("successor_candidate_id"))
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
        (
            record.durable_successor_candidate_id
            or record.successor_candidate_id
        )
        for records in records_by_dependency.values()
        for record in records
        if (
            record.durable_successor_candidate_id is not None
            or record.successor_candidate_id is not None
        )
    )


def readiness_evidence_ids_before_audit(
    session: Session,
    dependency_id: int,
    audit_id: int,
) -> frozenset[int] | None:
    """Reconstruct attributable readiness state immediately before an act.

    ``None`` means the durable history is malformed or internally
    inconsistent.  Reconfirmation consumers must treat that as unsafe rather
    than substituting the mutable present-day Evidence flag.
    """

    _dependency_ids((dependency_id,))
    if _positive_id(audit_id) is None:
        raise ValueError("audit id must be a positive integer")
    state = readiness_audit_state_before_audit(
        session, dependency_id, audit_id
    )
    return None if state is None else state.current_evidence_ids


def readiness_audit_state_before_audit(
    session: Session,
    dependency_id: int,
    audit_id: int,
) -> ReadinessAuditState | None:
    """Reconstruct current and ever-true readiness before one audit act."""

    _dependency_ids((dependency_id,))
    if _positive_id(audit_id) is None:
        raise ValueError("audit id must be a positive integer")
    return _readiness_state_from_audit(
        session,
        dependency_id,
        before_audit_id=audit_id,
    )


def current_readiness_evidence_ids_from_audit(
    session: Session,
    dependency_id: int,
) -> frozenset[int] | None:
    """Replay the complete attributable readiness history.

    This is the audit-side answer to what the mutable Evidence flags should
    say now.  A mismatch lets a safety-sensitive reader distinguish an
    attributable later toggle from an unaudited row mutation.
    """

    _dependency_ids((dependency_id,))
    state = _readiness_state_from_audit(
        session,
        dependency_id,
        before_audit_id=None,
    )
    return None if state is None else state.current_evidence_ids


def readiness_audit_states_for_dependencies(
    session: Session,
    dependency_ids: Iterable[int],
) -> dict[int, ReadinessAuditState | None]:
    """Batch-replay readiness, retaining later false judgments as history."""

    ids = _dependency_ids(dependency_ids)
    if not ids:
        return {}
    grouped: dict[int, list[AuditLog]] = {
        dependency_id: [] for dependency_id in ids
    }
    for entry in session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == DEPENDENCY,
            AuditLog.entity_id.in_(ids),
            AuditLog.action.in_(
                (
                    MARK_SATISFIES_REQUIREMENT,
                    RECONFIRM_OPERATIVE_SUPPORT,
                )
            ),
        )
        .order_by(AuditLog.entity_id, AuditLog.id)
    ):
        grouped[entry.entity_id].append(entry)
    return {
        dependency_id: _replay_readiness_entries(entries)
        for dependency_id, entries in grouped.items()
    }


def _readiness_state_from_audit(
    session: Session,
    dependency_id: int,
    *,
    before_audit_id: int | None,
) -> ReadinessAuditState | None:
    query = (
        select(AuditLog)
        .where(
            AuditLog.entity_type == DEPENDENCY,
            AuditLog.entity_id == dependency_id,
            AuditLog.action.in_(
                (
                    MARK_SATISFIES_REQUIREMENT,
                    RECONFIRM_OPERATIVE_SUPPORT,
                )
            ),
        )
        .order_by(AuditLog.id)
    )
    if before_audit_id is not None:
        query = query.where(AuditLog.id < before_audit_id)
    return _replay_readiness_entries(session.scalars(query).all())


def _replay_readiness_entries(
    entries: Iterable[AuditLog],
) -> ReadinessAuditState | None:
    readiness_by_evidence: dict[int, bool] = {}
    ever_satisfying: set[int] = set()
    for entry in entries:
        if not _is_attributable_human_principal(entry.human_principal):
            return None
        before = entry.before_json if isinstance(entry.before_json, dict) else {}
        after = entry.after_json if isinstance(entry.after_json, dict) else {}
        if entry.action == MARK_SATISFIES_REQUIREMENT:
            expected_keys = {"evidence_link_id", "satisfies"}
            if set(before) != expected_keys or set(after) != expected_keys:
                return None
            before_id = _positive_id(before.get("evidence_link_id"))
            after_id = _positive_id(after.get("evidence_link_id"))
            before_state = before.get("satisfies")
            after_state = after.get("satisfies")
            if (
                before_id is None
                or before_id != after_id
                or not isinstance(before_state, bool)
                or not isinstance(after_state, bool)
                or readiness_by_evidence.get(before_id, False)
                is not before_state
            ):
                return None
            readiness_by_evidence[before_id] = after_state
            if after_state:
                ever_satisfying.add(before_id)
            continue

        new_evidence_link_id = _positive_id(
            after.get("new_evidence_link_id")
        )
        operative_scopes, _, _, receipt_valid = (
            _parse_reconfirmation_transfer_receipt(
                before,
                after,
                new_evidence_link_id=new_evidence_link_id,
            )
        )
        if not receipt_valid or new_evidence_link_id is None:
            return None
        if any(scope.role == "readiness" for scope in operative_scopes):
            readiness_by_evidence[new_evidence_link_id] = True
            ever_satisfying.add(new_evidence_link_id)

    return ReadinessAuditState(
        current_evidence_ids=frozenset(
            evidence_link_id
            for evidence_link_id, satisfies in readiness_by_evidence.items()
            if satisfies
        ),
        ever_satisfying_evidence_ids=frozenset(ever_satisfying),
    )


def _parse_reconfirmation_transfer_receipt(
    before: dict,
    after: dict,
    *,
    new_evidence_link_id: int | None,
) -> tuple[
    tuple[ReconfirmationScope, ...],
    tuple[ReconfirmationScope, ...],
    tuple[ReconfirmationMove, ...],
    bool,
]:
    operative_scopes, operative_valid = _parse_scope_dicts(
        before.get("operative_scopes"), evidence_key="evidence_link_id"
    )
    scope_fingerprint, fingerprint_valid = _parse_scope_fingerprint(
        after.get("scope_fingerprint")
    )
    moved_scopes, moves_valid = _parse_moves(after.get("moved_scopes"))
    move_sources = tuple(move.scope for move in moved_scopes)
    receipt_valid = (
        operative_valid
        and fingerprint_valid
        and moves_valid
        and bool(operative_scopes)
        and new_evidence_link_id is not None
        and operative_scopes == scope_fingerprint == move_sources
        and all(
            move.to_evidence_link_id == new_evidence_link_id
            for move in moved_scopes
        )
    )
    return (
        operative_scopes,
        scope_fingerprint,
        moved_scopes,
        receipt_valid,
    )


def _parse_scope_dicts(
    value: object, *, evidence_key: str
) -> tuple[tuple[ReconfirmationScope, ...], bool]:
    if not isinstance(value, list):
        return (), False
    parsed: list[ReconfirmationScope] = []
    expected_keys = {"role", "field_name", evidence_key}
    for item in value:
        if not isinstance(item, dict) or set(item) != expected_keys:
            return (), False
        scope = _parse_scope(
            item.get("role"),
            item.get("field_name"),
            item.get(evidence_key),
        )
        if scope is None:
            return (), False
        parsed.append(scope)
    canonical = tuple(sorted(parsed, key=_scope_receipt_sort_key))
    return canonical, (
        tuple(parsed) == canonical
        and len(set(parsed)) == len(parsed)
        and _publication_scope_owners_are_unique(parsed)
    )


def _parse_scope_fingerprint(
    value: object,
) -> tuple[tuple[ReconfirmationScope, ...], bool]:
    if not isinstance(value, list):
        return (), False
    parsed: list[ReconfirmationScope] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 3:
            return (), False
        scope = _parse_scope(item[0], item[1], item[2])
        if scope is None:
            return (), False
        parsed.append(scope)
    canonical = tuple(sorted(parsed, key=_scope_receipt_sort_key))
    return canonical, (
        tuple(parsed) == canonical
        and len(set(parsed)) == len(parsed)
        and _publication_scope_owners_are_unique(parsed)
    )


def _parse_moves(
    value: object,
) -> tuple[tuple[ReconfirmationMove, ...], bool]:
    if not isinstance(value, list):
        return (), False
    parsed: list[ReconfirmationMove] = []
    expected_keys = {
        "role",
        "field_name",
        "from_evidence_link_id",
        "to_evidence_link_id",
    }
    for item in value:
        if not isinstance(item, dict) or set(item) != expected_keys:
            return (), False
        scope = _parse_scope(
            item.get("role"),
            item.get("field_name"),
            item.get("from_evidence_link_id"),
        )
        target_id = _positive_id(item.get("to_evidence_link_id"))
        if scope is None or target_id is None:
            return (), False
        parsed.append(
            ReconfirmationMove(
                scope=scope,
                to_evidence_link_id=target_id,
            )
        )
    canonical = tuple(
        sorted(
            parsed,
            key=lambda move: (
                *_scope_receipt_sort_key(move.scope),
                move.to_evidence_link_id,
            ),
        )
    )
    return canonical, (
        tuple(parsed) == canonical
        and len({move.scope for move in parsed}) == len(parsed)
        and _publication_scope_owners_are_unique(
            [move.scope for move in parsed]
        )
    )


def _publication_scope_owners_are_unique(
    scopes: Iterable[ReconfirmationScope],
) -> bool:
    """A publication role/field has exactly one operative owner."""

    publication_owners = [
        (scope.role, scope.field_name)
        for scope in scopes
        if scope.role == "publication"
    ]
    return len(publication_owners) == len(set(publication_owners))


def _parse_scope(
    role: object, field_name: object, evidence_link_id: object
) -> ReconfirmationScope | None:
    if not isinstance(role, str) or role not in {"publication", "readiness"}:
        return None
    if field_name is not None and (
        not isinstance(field_name, str)
        or not field_name
        or field_name != field_name.strip()
        or len(field_name) > 64
    ):
        return None
    if role == "readiness" and field_name is not None:
        return None
    parsed_evidence_link_id = _positive_id(evidence_link_id)
    if parsed_evidence_link_id is None:
        return None
    return ReconfirmationScope(
        role=role,
        field_name=field_name,
        evidence_link_id=parsed_evidence_link_id,
    )


def _scope_receipt_sort_key(
    scope: ReconfirmationScope,
) -> tuple[str, str, int]:
    return (scope.role, scope.field_name or "", scope.evidence_link_id)


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
