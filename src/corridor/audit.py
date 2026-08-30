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

from corridor.models import (
    AuditLog,
    PolicyApproval,
    AutomaticCarryForwardReceipt,
    DependencyAdmissionOutcome,
    ReconfirmationReceipt,
    RevisionComparisonFinding,
)
from corridor.principals import (
    HumanPrincipal,
    InvalidHumanPrincipal,
    require_human_principal,
)

# Ledger mutations have a reader alongside their writer. A Candidate's entries
# belong to the Dependency it becomes; `trail_for_dependency` joins them.
# Statement-level Coordination Plans are read by their durable Commitment
# Lineage instead; scope never makes them Dependency history.
DEPENDENCY = "dependency"
CANDIDATE = "candidate"
MILESTONE = "milestone"
PROJECT = "project"
COMMITMENT_LINEAGE = "commitment_lineage"
# The registered source file itself, as the subject of a product-intake
# confirmation (#349). The confirmation binds the acting person to one exact
# Document; extraction and admission remain the machine's separate, later acts.
DOCUMENT = "document"

ENTITY_TYPES = frozenset(
    {DEPENDENCY, CANDIDATE, MILESTONE, PROJECT, COMMITMENT_LINEAGE, DOCUMENT}
)

# Every act this system records against the Ledger. `entity_type` was
# checked against its three constants while `action` stayed free text, so
# the column a reader filters and groups the history by was the one nothing
# spelled twice the same way.
ACCEPT_CANDIDATE = "accept_candidate"
MERGE_CANDIDATE = "merge_candidate"
EDIT_CANDIDATE = "edit_candidate"
REJECT_CANDIDATE = "reject_candidate"
RECONFIRM_OPERATIVE_SUPPORT = "reconfirm_operative_support"
AUTOMATIC_CARRY_FORWARD = "automatic_carry_forward"
AUTHORIZE_AUTOMATIC_CARRY_FORWARD = "authorize_automatic_carry_forward"
DISABLE_AUTOMATIC_CARRY_FORWARD = "disable_automatic_carry_forward"
RETIRE_LEGACY_LEDGER = "retire_legacy_ledger"
SET_RESOLUTION_STRATEGY = "set_resolution_strategy"
ASSIGN_INTERNAL_OWNER = "assign_internal_owner"
SET_NEXT_ACTION = "set_next_action"
COMPLETE_NEXT_ACTION = "complete_next_action"
CANCEL_NEXT_ACTION = "cancel_next_action"
SET_MILESTONE_IMPACT = "set_milestone_impact"
DEFER_WORK = "defer_work"
RESUME_WORK = "resume_work"
MARK_SATISFIES_REQUIREMENT = "mark_satisfies_requirement"
CONFIRM_DOCUMENTATION_INTERPRETATION = "confirm_documentation_interpretation"
LINK_MILESTONE = "link_milestone"
CREATE_MILESTONE = "create_milestone"
REVISE_MILESTONE = "revise_milestone"
ADMIT_EVENT = "admit_event"
ADMIT_DEPENDENCY = "admit_dependency"
# An exact same-Document extraction replay disposes a Candidate but does not
# admit another Dependency or add another claim.  Keeping the action distinct
# stops Admission lineage readers from treating it as a second source act.
REPLAY_DEPENDENCY_CANDIDATE = "replay_dependency_candidate"
SETTLE_DISPUTE = "settle_dispute"
ATTACH_STATEMENT = "attach_statement"
RECORD_VERBAL = "record_verbal"
DISMISS_DEPENDENCY = "dismiss_dependency"
COORDINATE_STATEMENT = "coordinate_statement"
UNDO_COORDINATED_STATEMENT = "undo_coordinated_statement"
SAVE_FOLLOW_UP_PLAN = "save_follow_up_plan"
UNDO_FOLLOW_UP_PLAN = "undo_follow_up_plan"
CORRECT_STATEMENT_SCOPE = "correct_statement_scope"
CORRECT_STATEMENT_FACTS = "correct_statement_facts"
MARK_STATEMENT_NOT_RELEVANT = "mark_statement_not_relevant"
RESTORE_STATEMENT_NOT_RELEVANT = "restore_statement_not_relevant"
KEEP_CANDIDATE_UNRESOLVED = "keep_candidate_unresolved"
# Compatibility name for the first statement-only caller. The stored action is
# candidate-generic because the same attributable receipt now covers Dependency
# Admission residue.
KEEP_STATEMENT_UNRESOLVED = KEEP_CANDIDATE_UNRESOLVED
# Server-observed proof that an attributable human principal reached one
# ordinary customer-facing HTTP route.  This is deliberately an AuditLog
# action rather than a second mutable request-log table: Product Proving can
# include it in the same transaction and the same protected write-set as the
# Project Record act it caused.
PRODUCT_PROVING_FRONTEND_REQUEST = "product_proving_frontend_request"
# Managed, attributable enrollment or re-designation of one project member
# (#331).  The act names the project it is scoped to and the operator who made
# it; a selectable assignee or an email address alone never grants access.
ENROLL_PROJECT_MEMBER = "enroll_project_member"
# The assigned person flagged that a new-assignment notification's assignment
# looks incorrect (#351, ADR-0035).  It is an attributable marker, not a
# mutation: the assignment and its Work Decision history stand until an
# authorized person changes them, so this records feedback and changes nothing.
FLAG_INCORRECT_ASSIGNMENT = "flag_incorrect_assignment"
# One person confirmed the registration of one uploaded source Document through
# the product (#349, ADR-0035). It is an AuditLog action rather than a second
# table so the confirmation shares the exact transaction and write-set as the
# Document registration it authorizes, and a rolled-back confirm records nothing.
CONFIRM_SOURCE_INTAKE = "confirm_source_intake"
# A person authorized one discovered reference for processing, declaring the
# document kind the observed bytes cannot state (#350). Discovery only proposes;
# this attributable act is what lets the fetch pass register the reference.
AUTHORIZE_DISCOVERED_REFERENCE = "authorize_discovered_reference"
# A person ran the explicit bounded parse recovery on one failed-parse document
# (#350). Append-only, so the prior failure and every recovery attempt are kept.
RECOVER_DOCUMENT_PARSE = "recover_document_parse"
# Nothing records these any more: the admission policies stopped asking
# for a signature (ADR-0029). They stay named because the audit log is
# append-only and still holds entries that carry them.
AUTHORIZE_EVENT_ADMISSION = "authorize_event_admission"
AUTHORIZE_DEPENDENCY_ADMISSION = "authorize_dependency_admission"
# ADR-0060 / #373. A condition is a field in its own words; these are the acts
# that move an open condition toward Ready. CLEAR_CONDITION covers a person's
# cited or verbal clear and the exact-and-mechanical automatic clear (recorded
# under the machine actor); DISMISS_CONDITION is a person retiring a
# misdetection with a reason. The entry itself is derived, so there is no
# "record condition" action — nothing is written to raise a condition.
CLEAR_CONDITION = "clear_condition"
DISMISS_CONDITION = "dismiss_condition"

AUTOMATIC_CARRY_FORWARD_ACTOR = "corridor:automatic-carry-forward"
DEPENDENCY_ADMISSION_ACTOR = "corridor:dependency-admission"
ACTIVE_RUN_DECLARATION_ACTOR = "corridor:active-run-declaration"

ACTIONS = frozenset(
    {
        ACCEPT_CANDIDATE,
        MERGE_CANDIDATE,
        EDIT_CANDIDATE,
        REJECT_CANDIDATE,
        RECONFIRM_OPERATIVE_SUPPORT,
        AUTOMATIC_CARRY_FORWARD,
        AUTHORIZE_AUTOMATIC_CARRY_FORWARD,
        DISABLE_AUTOMATIC_CARRY_FORWARD,
        RETIRE_LEGACY_LEDGER,
        SET_RESOLUTION_STRATEGY,
        ASSIGN_INTERNAL_OWNER,
        SET_NEXT_ACTION,
        COMPLETE_NEXT_ACTION,
        CANCEL_NEXT_ACTION,
        SET_MILESTONE_IMPACT,
        DEFER_WORK,
        RESUME_WORK,
        MARK_SATISFIES_REQUIREMENT,
        CONFIRM_DOCUMENTATION_INTERPRETATION,
        LINK_MILESTONE,
        CREATE_MILESTONE,
        REVISE_MILESTONE,
        ADMIT_EVENT,
        AUTHORIZE_EVENT_ADMISSION,
        ADMIT_DEPENDENCY,
        REPLAY_DEPENDENCY_CANDIDATE,
        SETTLE_DISPUTE,
        ATTACH_STATEMENT,
        RECORD_VERBAL,
        DISMISS_DEPENDENCY,
        COORDINATE_STATEMENT,
        UNDO_COORDINATED_STATEMENT,
        SAVE_FOLLOW_UP_PLAN,
        UNDO_FOLLOW_UP_PLAN,
        CORRECT_STATEMENT_SCOPE,
        CORRECT_STATEMENT_FACTS,
        MARK_STATEMENT_NOT_RELEVANT,
        RESTORE_STATEMENT_NOT_RELEVANT,
        KEEP_CANDIDATE_UNRESOLVED,
        PRODUCT_PROVING_FRONTEND_REQUEST,
        ENROLL_PROJECT_MEMBER,
        CONFIRM_SOURCE_INTAKE,
        AUTHORIZE_DISCOVERED_REFERENCE,
        RECOVER_DOCUMENT_PARSE,
        AUTHORIZE_DEPENDENCY_ADMISSION,
        CLEAR_CONDITION,
        DISMISS_CONDITION,
        FLAG_INCORRECT_ASSIGNMENT,
    }
)


@dataclass(frozen=True)
class AdmissionRecord:
    """Typed Admission history without exposing audit storage or JSON shape."""

    audit_id: int
    dependency_id: int
    action: str
    actor: str
    candidate_id: int | None
    fields: dict[str, Any] | None
    human_principal: str | None
    # ADMIT_DEPENDENCY only: whether an immutable DependencyAdmissionOutcome
    # names this exact (candidate, dependency) act. The machine identity is
    # honest only while its durable receipt exists (mirrors Carry-Forward).
    durable_receipt_present: bool = False
    durable_outcome: str | None = None

    @property
    def candidate_link_valid(self) -> bool:
        """Whether the record names one possible database Candidate id."""

        return self.candidate_id is not None

    @property
    def attributable(self) -> bool:
        """Whether the record has an honest human or machine identity."""

        if self.action == ADMIT_DEPENDENCY:
            return (
                self.actor == DEPENDENCY_ADMISSION_ACTOR
                and self.human_principal is None
                and self.durable_receipt_present
                and self.durable_outcome in ("admitted", "merged")
            )
        return (
            _is_attributable_human_principal(self.human_principal)
            and self.actor == self.human_principal
        )


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
class SupportTransferRecord:
    """Typed identity of one human or released-policy support transfer.

    This is the canonical mixed-history read model. Human Reconfirmation
    remains a distinct action at the write seam; Automatic Carry-Forward is a
    separate machine action. Both can appear in one lineage, so the shared
    readback type must stay actor-neutral.
    """

    audit_id: int
    dependency_id: int
    action: str
    actor: str
    comparison_id: int | None
    finding_id: int | None
    predecessor_candidate_id: int | None
    successor_candidate_id: int | None
    new_evidence_link_id: int | None
    origin_admission_audit_id: int | None
    predecessor_support_transfer_audit_id: int | None
    predecessor_support_transfer_pointer_valid: bool
    operative_scopes: tuple[ReconfirmationScope, ...]
    scope_fingerprint: tuple[ReconfirmationScope, ...]
    moved_scopes: tuple[ReconfirmationMove, ...]
    transfer_receipt_valid: bool
    durable_successor_candidate_id: int | None
    durable_receipt_present: bool
    durable_receipt_matches: bool
    policy_approval_id: int | None
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
            and self.predecessor_support_transfer_pointer_valid
            and self.transfer_receipt_valid
            and (not self.durable_receipt_present or self.durable_receipt_matches)
            and (
                self.action != AUTOMATIC_CARRY_FORWARD
                or (self.durable_receipt_present and self.durable_receipt_matches)
            )
        )

    @property
    def attributable(self) -> bool:
        """Whether the transfer has an honest human or machine identity."""

        if self.action == RECONFIRM_OPERATIVE_SUPPORT:
            return (
                _is_attributable_human_principal(self.human_principal)
                and self.actor == self.human_principal
            )
        if self.action == AUTOMATIC_CARRY_FORWARD:
            return (
                self.actor == AUTOMATIC_CARRY_FORWARD_ACTOR
                and self.human_principal is None
                and self.durable_receipt_present
                and self.durable_receipt_matches
            )
        return False

    @property
    def predecessor_reconfirmation_audit_id(self) -> int | None:
        """Compatibility name for the generalized predecessor pointer."""

        return self.predecessor_support_transfer_audit_id


# Compatibility alias for older callers. New mixed-history consumers should use
# `SupportTransferRecord`.
ReconfirmationRecord = SupportTransferRecord


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


def references_typed_ids(value: object, referenced_ids: dict[str, set[int]]) -> bool:
    """Whether appended audit detail names one of these exact typed ids.

    Compensating commands use this to refuse an Undo after a later recorded
    act depends on a result of the grouped Save.  The match is typed key to
    integer id, never substring guessing, so unrelated numbers cannot block
    a legitimate reversal.
    """
    if isinstance(value, dict):
        return any(
            key in referenced_ids
            and isinstance(item, int)
            and item in referenced_ids[key]
            for key, item in value.items()
        ) or any(references_typed_ids(item, referenced_ids) for item in value.values())
    if isinstance(value, list):
        return any(references_typed_ids(item, referenced_ids) for item in value)
    return False


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


def trail_for_commitment_lineage(
    session: Session, commitment_lineage_id: int
) -> list[AuditLog]:
    """Read one statement-level Coordination Plan's appended decisions.

    The lineage is the subject identity, so this deliberately does not follow
    its Dependency scope links. Those describe the External Party fact's
    application, not copied project-team decisions (ADR-0038).
    """

    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == COMMITMENT_LINEAGE,
                AuditLog.entity_id == commitment_lineage_id,
            )
            .order_by(AuditLog.ts, AuditLog.id)
        ).all()
    )


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
            AuditLog.action.in_((ACCEPT_CANDIDATE, MERGE_CANDIDATE, ADMIT_DEPENDENCY)),
        )
        .order_by(AuditLog.entity_id, AuditLog.id)
    ).all()
    outcomes_by_pair: dict[tuple[int, int], list[str]] = {}
    for outcome in session.scalars(
        select(DependencyAdmissionOutcome).where(
            DependencyAdmissionOutcome.dependency_id.in_(ids),
            DependencyAdmissionOutcome.outcome.in_(("admitted", "merged")),
        )
    ):
        outcomes_by_pair.setdefault(
            (outcome.candidate_id, outcome.dependency_id), []
        ).append(outcome.outcome)
    for entry in entries:
        after = entry.after_json if isinstance(entry.after_json, dict) else {}
        raw_fields = after.get("fields")
        candidate_id = _positive_id(after.get("candidate_id"))
        durable_outcomes = outcomes_by_pair.get(
            (candidate_id, entry.entity_id), []
        )
        durable_outcome = (
            durable_outcomes[0] if len(durable_outcomes) == 1 else None
        )
        grouped[entry.entity_id].append(
            AdmissionRecord(
                audit_id=entry.id,
                dependency_id=entry.entity_id,
                action=entry.action,
                actor=entry.actor,
                candidate_id=candidate_id,
                fields=(deepcopy(raw_fields) if isinstance(raw_fields, dict) else None),
                human_principal=entry.human_principal,
                durable_receipt_present=durable_outcome is not None,
                durable_outcome=durable_outcome,
            )
        )
    return {dependency_id: tuple(records) for dependency_id, records in grouped.items()}


def support_transfer_records_for_dependencies(
    session: Session, dependency_ids: Iterable[int]
) -> dict[int, tuple[SupportTransferRecord, ...]]:
    """Read exact support-transfer identities for many Dependencies at once.

    Human Reconfirmation and Automatic Carry-Forward remain distinct acts and
    durable receipt types.  They meet only in this read model because either
    can be the predecessor of the other.  Legacy human receipts may use the
    old pointer key; new records carry the actor-neutral key as well.
    """

    ids = _dependency_ids(dependency_ids)
    if not ids:
        return {}
    grouped: dict[int, list[SupportTransferRecord]] = {
        dependency_id: [] for dependency_id in ids
    }
    human_receipts = tuple(
        session.scalars(
            select(ReconfirmationReceipt)
            .where(ReconfirmationReceipt.dependency_id.in_(ids))
            .order_by(
                ReconfirmationReceipt.dependency_id,
                ReconfirmationReceipt.audit_log_id,
            )
        ).all()
    )
    automatic_receipts = tuple(
        session.scalars(
            select(AutomaticCarryForwardReceipt)
            .where(AutomaticCarryForwardReceipt.dependency_id.in_(ids))
            .order_by(
                AutomaticCarryForwardReceipt.dependency_id,
                AutomaticCarryForwardReceipt.audit_log_id,
            )
        ).all()
    )
    receipts_by_audit_id: dict[int, tuple[str, object] | None] = {
        receipt.audit_log_id: ("human", receipt) for receipt in human_receipts
    }
    for receipt in automatic_receipts:
        if receipt.audit_log_id in receipts_by_audit_id:
            receipts_by_audit_id[receipt.audit_log_id] = None
        else:
            receipts_by_audit_id[receipt.audit_log_id] = (
                "automatic",
                receipt,
            )
    normal_entries = tuple(
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == DEPENDENCY,
                AuditLog.entity_id.in_(ids),
                AuditLog.action.in_(
                    (
                        RECONFIRM_OPERATIVE_SUPPORT,
                        AUTOMATIC_CARRY_FORWARD,
                    )
                ),
            )
        ).all()
    )
    unresolved_receipt_ids = tuple(
        entry.id for entry in normal_entries if entry.id not in receipts_by_audit_id
    )
    if unresolved_receipt_ids:
        for receipt in session.scalars(
            select(ReconfirmationReceipt).where(
                ReconfirmationReceipt.audit_log_id.in_(unresolved_receipt_ids)
            )
        ).all():
            receipts_by_audit_id[receipt.audit_log_id] = (
                "human",
                receipt,
            )
        for receipt in session.scalars(
            select(AutomaticCarryForwardReceipt).where(
                AutomaticCarryForwardReceipt.audit_log_id.in_(unresolved_receipt_ids)
            )
        ).all():
            if receipt.audit_log_id in receipts_by_audit_id:
                receipts_by_audit_id[receipt.audit_log_id] = None
            else:
                receipts_by_audit_id[receipt.audit_log_id] = (
                    "automatic",
                    receipt,
                )
    entries_by_id = {entry.id: entry for entry in normal_entries}
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
                receipts_by_audit_id[entry.id][1].dependency_id
                if (
                    entry.id in receipts_by_audit_id
                    and receipts_by_audit_id[entry.id] is not None
                )
                else entry.entity_id,
                entry.id,
            ),
        )
    )
    for entry in entries:
        receipt_binding = receipts_by_audit_id.get(entry.id)
        receipt_kind = receipt_binding[0] if receipt_binding is not None else None
        durable_receipt = receipt_binding[1] if receipt_binding is not None else None
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
        new_evidence_link_id = _positive_id(after.get("new_evidence_link_id"))
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
        (
            predecessor_support_transfer_audit_id,
            predecessor_pointer_valid,
        ) = _support_transfer_predecessor_pointer(
            after,
            action=entry.action,
        )
        policy_approval_id = (
            _positive_id(durable_receipt.policy_approval_id)
            if receipt_kind == "automatic"
            and durable_receipt.policy_approval_id is not None
            else None
        )
        durable_receipt_matches = False
        if receipt_kind == "human":
            durable_receipt_matches = (
                entry.action == RECONFIRM_OPERATIVE_SUPPORT
                and entry.entity_type == DEPENDENCY
                and entry.entity_id == durable_receipt.dependency_id
                and durable_receipt.before_json == entry.before_json
                and durable_receipt.after_json == entry.after_json
                and durable_successor_candidate_id
                == _positive_id(after.get("successor_candidate_id"))
            )
        elif receipt_kind == "automatic":
            durable_receipt_matches = _automatic_receipt_matches(
                session,
                entry=entry,
                receipt=durable_receipt,
                predecessor_support_transfer_audit_id=(
                    predecessor_support_transfer_audit_id
                ),
            )
        elif entry.id in receipts_by_audit_id:
            # One audit id bound to both durable receipt types is corrupt.
            durable_receipt_matches = False

        durable_receipt_present = entry.id in receipts_by_audit_id
        if receipt_binding is None and durable_receipt_present:
            durable_successor_candidate_id = None
            policy_approval_id = None
        grouped[dependency_id].append(
            SupportTransferRecord(
                audit_id=entry.id,
                dependency_id=dependency_id,
                action=entry.action,
                actor=entry.actor,
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
                predecessor_support_transfer_audit_id=(
                    predecessor_support_transfer_audit_id
                ),
                predecessor_support_transfer_pointer_valid=(predecessor_pointer_valid),
                operative_scopes=operative_scopes,
                scope_fingerprint=scope_fingerprint,
                moved_scopes=moved_scopes,
                transfer_receipt_valid=transfer_receipt_valid,
                durable_successor_candidate_id=(durable_successor_candidate_id),
                durable_receipt_present=durable_receipt_present,
                durable_receipt_matches=durable_receipt_matches,
                policy_approval_id=policy_approval_id,
                human_principal=entry.human_principal,
            )
        )
    return {dependency_id: tuple(records) for dependency_id, records in grouped.items()}


def reconfirmation_records_for_dependencies(
    session: Session, dependency_ids: Iterable[int]
) -> dict[int, tuple[SupportTransferRecord, ...]]:
    """Compatibility wrapper for the pre-generalization read model name."""

    return support_transfer_records_for_dependencies(session, dependency_ids)


def _support_transfer_predecessor_pointer(
    after: dict,
    *,
    action: str,
) -> tuple[int | None, bool]:
    generic_key = "predecessor_support_transfer_audit_id"
    legacy_key = "predecessor_reconfirmation_audit_id"
    generic_present = generic_key in after
    legacy_present = legacy_key in after
    # A human receipt that carries the legacy key is interpreted by that key.
    # This preserves exact readback of pre-generalization history. New sealed
    # human receipts carry both values, and their durable JSON equality catches
    # any later disagreement between them.
    if action == RECONFIRM_OPERATIVE_SUPPORT and legacy_present:
        raw_legacy = after.get(legacy_key)
        legacy_id = _positive_id(raw_legacy)
        return legacy_id, raw_legacy is None or legacy_id is not None
    if generic_present:
        raw_generic = after.get(generic_key)
        generic_id = _positive_id(raw_generic)
        valid = raw_generic is None or generic_id is not None
        return generic_id, valid
    return None, False


def _automatic_receipt_matches(
    session: Session,
    *,
    entry: AuditLog,
    receipt: AutomaticCarryForwardReceipt,
    predecessor_support_transfer_audit_id: int | None,
) -> bool:
    """Read back the durable machine binding without trusting audit JSON."""

    before = entry.before_json if isinstance(entry.before_json, dict) else {}
    after = entry.after_json if isinstance(entry.after_json, dict) else {}
    approval = (
        session.get(PolicyApproval, receipt.policy_approval_id)
        if receipt.policy_approval_id is not None
        else None
    )
    finding = session.get(
        RevisionComparisonFinding,
        receipt.finding_id,
    )
    authorization_matches: tuple[AuditLog, ...] = ()
    if approval is not None and _is_attributable_human_principal(approval.approved_by):
        expected_authorization = {
            "policy_approval_id": approval.id,
            "policy_version": approval.policy_version,
            "policy_sha256": approval.policy_sha256,
        }
        authorization_matches = tuple(
            entry
            for entry in session.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == PROJECT,
                    AuditLog.entity_id == approval.project_id,
                    AuditLog.action == AUTHORIZE_AUTOMATIC_CARRY_FORWARD,
                )
            ).all()
            if (
                entry.actor == approval.approved_by
                and entry.human_principal == approval.approved_by
                and entry.after_json == expected_authorization
            )
        )
    historical_policy_valid = (
        approval is not None
        and approval.project_id == receipt.project_id
        and len(authorization_matches) == 1
        and _positive_id(after.get("policy_approval_id")) == receipt.policy_approval_id
        and receipt.policy_version == approval.policy_version
        and receipt.policy_sha256 == approval.policy_sha256
    )
    released_policy_valid = (
        receipt.policy_approval_id is None
        and "policy_approval_id" not in after
        and after.get("policy_version") == receipt.policy_version
        and after.get("policy_sha256") == receipt.policy_sha256
        and bool(receipt.policy_version)
        and len(receipt.policy_sha256) == 64
    )
    return (
        entry.action == AUTOMATIC_CARRY_FORWARD
        and entry.entity_type == DEPENDENCY
        and entry.entity_id == receipt.dependency_id
        and entry.actor == AUTOMATIC_CARRY_FORWARD_ACTOR
        and entry.human_principal is None
        and receipt.before_json == before
        and receipt.after_json == after
        and (historical_policy_valid or released_policy_valid)
        and finding is not None
        and finding.state == "unchanged"
        and finding.predecessor_candidate_ids == [receipt.predecessor_candidate_id]
        and finding.successor_candidate_ids == [receipt.successor_candidate_id]
        and _positive_id(after.get("comparison_id")) == receipt.comparison_id
        and _positive_id(after.get("finding_id")) == receipt.finding_id
        and _positive_id(after.get("predecessor_candidate_id"))
        == receipt.predecessor_candidate_id
        and _positive_id(after.get("successor_candidate_id"))
        == receipt.successor_candidate_id
        and _positive_id(after.get("new_evidence_link_id"))
        == receipt.new_evidence_link_id
        and _positive_id(after.get("origin_admission_audit_id"))
        == receipt.origin_admission_audit_id
        and predecessor_support_transfer_audit_id
        == receipt.predecessor_support_transfer_audit_id
    )


def reconfirmed_successor_candidate_ids(
    records_by_dependency: Mapping[int, tuple[SupportTransferRecord, ...]],
) -> frozenset[int]:
    """Candidates named by a support transfer, including corrupt old records.

    A malformed surrounding identity must not put an already-reconfirmed
    Candidate back into ordinary Admission.  The candidate id itself remains
    usable for exclusion only when it is a strict positive integer.
    """

    return frozenset(
        (record.durable_successor_candidate_id or record.successor_candidate_id)
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
    state = readiness_audit_state_before_audit(session, dependency_id, audit_id)
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
    grouped: dict[int, list[AuditLog]] = {dependency_id: [] for dependency_id in ids}
    transfers = support_transfer_records_for_dependencies(session, ids)
    transfers_by_audit_id = {
        record.audit_id: record for records in transfers.values() for record in records
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
                    AUTOMATIC_CARRY_FORWARD,
                )
            ),
        )
        .order_by(AuditLog.entity_id, AuditLog.id)
    ):
        grouped[entry.entity_id].append(entry)
    return {
        dependency_id: _replay_readiness_entries(
            entries,
            transfers_by_audit_id=transfers_by_audit_id,
        )
        for dependency_id, entries in grouped.items()
    }


def _readiness_state_from_audit(
    session: Session,
    dependency_id: int,
    *,
    before_audit_id: int | None,
) -> ReadinessAuditState | None:
    transfers = support_transfer_records_for_dependencies(
        session, (dependency_id,)
    ).get(dependency_id, ())
    transfers_by_audit_id = {record.audit_id: record for record in transfers}
    query = (
        select(AuditLog)
        .where(
            AuditLog.entity_type == DEPENDENCY,
            AuditLog.entity_id == dependency_id,
            AuditLog.action.in_(
                (
                    MARK_SATISFIES_REQUIREMENT,
                    RECONFIRM_OPERATIVE_SUPPORT,
                    AUTOMATIC_CARRY_FORWARD,
                )
            ),
        )
        .order_by(AuditLog.id)
    )
    if before_audit_id is not None:
        query = query.where(AuditLog.id < before_audit_id)
    return _replay_readiness_entries(
        session.scalars(query).all(),
        transfers_by_audit_id=transfers_by_audit_id,
    )


def _replay_readiness_entries(
    entries: Iterable[AuditLog],
    *,
    transfers_by_audit_id: Mapping[int, SupportTransferRecord],
) -> ReadinessAuditState | None:
    readiness_by_evidence: dict[int, bool] = {}
    ever_satisfying: set[int] = set()
    for entry in entries:
        before = entry.before_json if isinstance(entry.before_json, dict) else {}
        after = entry.after_json if isinstance(entry.after_json, dict) else {}
        if entry.action == MARK_SATISFIES_REQUIREMENT:
            if (
                not _is_attributable_human_principal(entry.human_principal)
                or entry.actor != entry.human_principal
            ):
                return None
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
                or readiness_by_evidence.get(before_id, False) is not before_state
            ):
                return None
            readiness_by_evidence[before_id] = after_state
            if after_state:
                ever_satisfying.add(before_id)
            continue

        transfer = transfers_by_audit_id.get(entry.id)
        if transfer is None or not transfer.identity_valid or not transfer.attributable:
            return None
        new_evidence_link_id = _positive_id(after.get("new_evidence_link_id"))
        operative_scopes, _, _, receipt_valid = _parse_reconfirmation_transfer_receipt(
            before,
            after,
            new_evidence_link_id=new_evidence_link_id,
        )
        if not receipt_valid or new_evidence_link_id is None:
            return None
        readiness_sources = tuple(
            scope.evidence_link_id
            for scope in operative_scopes
            if scope.role == "readiness"
        )
        if readiness_sources:
            if any(
                readiness_by_evidence.get(evidence_link_id) is not True
                for evidence_link_id in readiness_sources
            ):
                return None
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
            move.to_evidence_link_id == new_evidence_link_id for move in moved_scopes
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
        and _publication_scope_owners_are_unique([move.scope for move in parsed])
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
