"""Derive standard documentation fields from current cited project facts.

The former closure bar was a free-text requirement plus a per-passage toggle.
That made every record ask a person to repeat a comparison the system could
already make, while retaining neither a stable requirement nor the source set
for the answer.  ADR-0052 replaces it with one small agency-neutral standard:
the resolution method and cost responsibility select fields, machine fields
are predicates over current cited passages, and the only stored human acts are
cited confirmations of an approval-letter interpretation: the clean-letter
confirm and ADR-0060's optional condition-immaterial override.  A hedged
letter records as conditional by itself at read time — staying not ready
costs no click.

This module deliberately does not decide whether a document is authentic,
whether work happened in the field, or whether a contract was accepted.  It
only derives whether the exact current Project Record fills its required
documentation fields.  The legacy sufficiency rows remain readable until the
first structured confirmation takes over; they are never rewritten.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, undefer

from corridor import audit, condition_tracking
from corridor.condition_tracking import ConditionEntry, ConditionLink, FieldCandidate
from corridor.models import (
    Dependency,
    DependencyEvidenceSufficiency,
    DocumentationFieldConfirmation,
    Document,
    EvidenceLink,
    ProjectRosterEntry,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.work_decisions import assign_internal_owner, set_next_action


AS_BUILT = "as_built"
APPROVAL_INTERPRETATION = "approval_interpretation"
ABANDONMENT_DOCUMENTATION = "abandonment_documentation"
EXECUTED_AGREEMENT_REFERENCE = "executed_agreement_reference"

_FIELD_LABELS = {
    AS_BUILT: "As-built covering this location is on file",
    APPROVAL_INTERPRETATION: "Organization approval is confirmed",
    ABANDONMENT_DOCUMENTATION: "Abandonment documentation is on file",
    EXECUTED_AGREEMENT_REFERENCE: "Executed agreement reference is on file",
}

# The identifying words a condition may use to name one of this Constraint's
# own required fields (ADR-0060 tier 1: "once we receive the executed
# agreement" -> the agreement field).  Only fields other than the approval
# interpretation itself are link targets — a condition never links to the very
# field whose hedge produced it.  Matched verbatim, one target only.
_FIELD_CONDITION_TERMS = {
    EXECUTED_AGREEMENT_REFERENCE: ("executed agreement", "agreement"),
    AS_BUILT: ("as-built", "as built"),
    ABANDONMENT_DOCUMENTATION: ("abandonment",),
}

_HEDGE = re.compile(
    r"\b(pending|subject to|conditional|conditioned|after|until|once|"
    r"contingent|provided that)\b",
    re.IGNORECASE,
)
_NEGATED_APPROVAL = re.compile(r"\b(not|not yet|never)\s+approved\b", re.IGNORECASE)
_APPROVED = re.compile(r"\bapproved\b", re.IGNORECASE)
_AS_BUILT = re.compile(r"\bas[ -]?built\b", re.IGNORECASE)
_ABANDONMENT = re.compile(r"\babandon(?:ed|ment|ing)?\b", re.IGNORECASE)
_EXECUTED_AGREEMENT = re.compile(
    r"\b(executed|fully signed|signed by all parties)\b.*\bagreement\b"
    r"|\bagreement\b.*\b(executed|fully signed|signed by all parties)\b",
    re.IGNORECASE,
)


class DocumentationConfirmationRefusal(ValueError):
    """The cited source cannot lawfully receive this confirmation."""


@dataclass(frozen=True)
class ChecklistField:
    """One required standard field and the exact source facts behind it."""

    name: str
    label: str
    machine: bool
    complete: bool
    evidence_link_ids: tuple[int, ...]
    candidate_evidence_link_ids: tuple[int, ...] = ()
    candidate_conclusion: str | None = None
    confirmation_ids: tuple[int, ...] = ()
    # Letters whose own sentence hedges the approval.  ADR-0060: these record
    # as conditional automatically at read time — no human act — and keep the
    # field empty until the condition is handled or a person records the
    # optional condition-immaterial override.
    conditional_evidence_link_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class DocumentationChecklist:
    """Read-time field state for one Constraint; no mutable Ready status."""

    dependency_id: int
    fields: tuple[ChecklistField, ...]
    uses_standard_checklist: bool
    legacy_mark_remains_effective: bool
    is_ready: bool
    # ADR-0060 condition entries derived from this Constraint's conditional
    # letters — quoted verbatim, linked where their words name one target, and
    # each carrying whether it is still open (blocking Ready), cleared, or
    # dismissed.  Empty unless a conditional letter is on file.
    conditions: tuple[ConditionEntry, ...] = ()

    def field(self, name: str) -> ChecklistField:
        for field in self.fields:
            if field.name == name:
                return field
        raise KeyError(name)

    @property
    def open_conditions(self) -> tuple[ConditionEntry, ...]:
        return tuple(entry for entry in self.conditions if entry.is_open)


@dataclass(frozen=True)
class _CurrentEvidence:
    link_id: int
    quote: str
    document_type: str
    document_id: int
    document_filename: str
    page_no: int


def required_field_names(dependency: Dependency) -> tuple[str, ...]:
    """The fixed ADR-0052 selector mapping, in user-facing checklist order."""

    required: list[str] = []
    if dependency.resolution_strategy in {"relocate", "remove"}:
        required.extend((AS_BUILT, APPROVAL_INTERPRETATION))
    elif dependency.resolution_strategy == "abandon_in_place":
        required.append(ABANDONMENT_DOCUMENTATION)
    if dependency.cost_responsibility == "reimbursable":
        required.append(EXECUTED_AGREEMENT_REFERENCE)
    return tuple(required)


def read_checklist(
    session: Session,
    dependency_id: int,
    *,
    legacy_ready: bool | None = None,
) -> DocumentationChecklist:
    """Read current standard documentation fields with their cited basis.

    The read decides applicability from the two source-derived selectors.  A
    positive legacy marker continues to be the effective historical answer
    until an append-only structured confirmation exists; absent legacy marks
    are not invented and therefore do not block the standard model.
    """

    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise LookupError(f"no dependency {dependency_id}")
    if legacy_ready is None:
        legacy_ready = _legacy_ready(session, dependency_id)
    confirmations = tuple(
        session.scalars(
            select(DocumentationFieldConfirmation)
            .where(DocumentationFieldConfirmation.dependency_id == dependency_id)
            .order_by(DocumentationFieldConfirmation.id)
        ).all()
    )
    evidence = _current_verified_evidence(session, dependency_id)
    return _build_checklist(
        session,
        dependency,
        legacy_ready=legacy_ready,
        confirmations=confirmations,
        evidence=evidence,
    )


def read_checklists(
    session: Session,
    dependency_ids: Iterable[int],
    *,
    legacy_ready_by_dependency: Mapping[int, bool],
) -> dict[int, DocumentationChecklist]:
    """Read many checklist inputs once for project-wide readers."""
    ids = tuple(dict.fromkeys(dependency_ids))
    if not ids:
        return {}
    if set(legacy_ready_by_dependency) != set(ids):
        raise ValueError(
            "batch checklist reads require complete legacy readiness inputs"
        )
    dependencies = tuple(
        session.scalars(
            select(Dependency)
            .options(undefer(Dependency.cost_responsibility))
            .where(Dependency.id.in_(ids))
            .order_by(Dependency.id)
        ).all()
    )
    if {dependency.id for dependency in dependencies} != set(ids):
        raise LookupError("one or more constraints do not exist")

    confirmations_by_dependency: dict[
        int, list[DocumentationFieldConfirmation]
    ] = {dependency_id: [] for dependency_id in ids}
    for confirmation in session.scalars(
        select(DocumentationFieldConfirmation)
        .where(DocumentationFieldConfirmation.dependency_id.in_(ids))
        .order_by(
            DocumentationFieldConfirmation.dependency_id,
            DocumentationFieldConfirmation.id,
        )
    ):
        confirmations_by_dependency[confirmation.dependency_id].append(confirmation)
    evidence_by_dependency = _current_verified_evidence_many(session, ids)

    return {
        dependency.id: _build_checklist(
            session,
            dependency,
            legacy_ready=legacy_ready_by_dependency.get(dependency.id, False),
            confirmations=tuple(confirmations_by_dependency[dependency.id]),
            evidence=evidence_by_dependency[dependency.id],
        )
        for dependency in dependencies
    }


def _build_checklist(
    session: Session,
    dependency: Dependency,
    *,
    legacy_ready: bool,
    confirmations: tuple[DocumentationFieldConfirmation, ...],
    evidence: tuple[_CurrentEvidence, ...],
) -> DocumentationChecklist:
    """Assemble one checklist from already-frozen current inputs."""
    dependency_id = dependency.id
    required = required_field_names(dependency)
    # Only selector values with an adopted field mapping take over from
    # legacy history.  ``protect_in_place`` is the explicitly complete
    # zero-external-field case; an unmodeled strategy (for example a policy
    # exception) must not accidentally become Ready through vacuous truth.
    has_selector = bool(required) or dependency.resolution_strategy == "protect_in_place"
    uses_standard = has_selector and (not legacy_ready or bool(confirmations))
    fields = tuple(
        _field_state(name, evidence, confirmations) for name in required
    )
    if not uses_standard:
        return DocumentationChecklist(
            dependency_id=dependency_id,
            fields=fields,
            uses_standard_checklist=False,
            legacy_mark_remains_effective=legacy_ready,
            is_ready=legacy_ready,
        )
    conditions = _derive_conditions(session, dependency, fields, evidence)
    # ADR-0060: a hedged letter's approval is met once every condition it
    # raised is *cleared* — by a filled linked field, a completed linked row,
    # a cited/verbal clear, or the gated mechanical clear.  A *dismissed*
    # misdetection is deliberately not a clear, so no misdetection can fill the
    # approval field or produce Ready; the person must still override or
    # confirm the letter if they judge it an approval.
    approval_cleared = bool(conditions) and all(
        entry.state == "cleared" for entry in conditions
    )
    if approval_cleared:
        fields = tuple(
            replace(field, complete=True)
            if field.name == APPROVAL_INTERPRETATION
            else field
            for field in fields
        )
    is_ready = all(field.complete for field in fields) and not any(
        entry.is_open for entry in conditions
    )
    return DocumentationChecklist(
        dependency_id=dependency_id,
        fields=fields,
        uses_standard_checklist=True,
        legacy_mark_remains_effective=False,
        is_ready=is_ready,
        conditions=conditions,
    )


def condition_entries_for(
    session: Session, dependency_id: int
) -> tuple[ConditionEntry, ...]:
    """The derived condition entries for a Constraint (see ADR-0060).

    The checklist is the single place that derives conditions, so callers that
    want just the entries come through here.
    """
    return read_checklist(session, dependency_id).conditions


def run_condition_clearing_admission(
    session: Session, project_id: int
) -> condition_tracking.ConditionClearRun:
    """Auto-clear generic open conditions the mechanical rule proves met.

    This is the one part of #373 that moves a condition *toward* Ready
    automatically, so it expands automatic Record Inclusion and is ADR-0050
    replay-gated: inert until this project's own recorded human clears vouch
    for the rule (a fresh project has none and is therefore inactive — the
    ship-inactive posture of #370/#371).  It writes only exact-and-mechanical,
    sole-passage clears under the machine actor, with a reproducible receipt,
    and is idempotent — a condition already resolved is skipped, so re-running
    clears nothing new.

    It lives here because deriving a condition entry needs the checklist; it
    reuses ``condition_tracking`` for the replay, the mechanical match, and the
    shared append so no logic is duplicated.
    """
    replay = condition_tracking.replay_matches_human_condition_clears(
        session, project_id
    )
    if not replay.passed:
        return condition_tracking.ConditionClearRun(0, replay)
    lock_project(session, project_id)
    dependency_ids = session.scalars(
        select(Dependency.id).where(
            Dependency.project_id == project_id,
            Dependency.dismissed_at.is_(None),
        )
    ).all()
    cleared = 0
    for dependency_id in dependency_ids:
        for entry in condition_entries_for(session, dependency_id):
            if not entry.is_open or entry.target.kind != "generic":
                continue
            matches = condition_tracking.mechanical_matches(
                session, dependency_id, entry.evidence_link_id, entry.condition_text
            )
            if len(matches) != 1:
                continue
            dependency = session.get(Dependency, dependency_id)
            receipt = {
                "policy_version": condition_tracking.CLEARING_POLICY_VERSION,
                "replay_case_count": replay.case_count,
                "condition_evidence_link_id": entry.evidence_link_id,
                "basis_evidence_link_id": matches[0],
                "condition_text": entry.condition_text,
            }
            condition_tracking.append_condition_clear(
                session,
                dependency=dependency,
                entry=entry,
                basis_evidence_link_id=matches[0],
                basis_event_id=None,
                reason=None,
                resolved_by=condition_tracking.MACHINE_ACTOR,
                receipt_json=receipt,
            )
            audit.record(
                session,
                actor=condition_tracking.MACHINE_ACTOR,
                action=audit.CLEAR_CONDITION,
                entity_type=audit.DEPENDENCY,
                entity_id=dependency_id,
                after=receipt,
            )
            cleared += 1
    session.flush()
    return condition_tracking.ConditionClearRun(cleared, replay)


def _derive_conditions(
    session: Session,
    dependency: Dependency,
    fields: tuple[ChecklistField, ...],
    evidence: tuple[_CurrentEvidence, ...],
) -> tuple[ConditionEntry, ...]:
    """Build the conditional-letter links and field candidates, then derive."""
    approval = next(
        (field for field in fields if field.name == APPROVAL_INTERPRETATION), None
    )
    if approval is None or not approval.conditional_evidence_link_ids:
        return ()
    by_id = {item.link_id: item for item in evidence}
    conditional_links = tuple(
        ConditionLink(
            evidence_link_id=item.link_id,
            condition_text=item.quote,
            document_id=item.document_id,
            document_filename=item.document_filename,
            page_no=item.page_no,
        )
        for link_id in approval.conditional_evidence_link_ids
        if (item := by_id.get(link_id)) is not None
    )
    field_candidates = tuple(
        FieldCandidate(field.name, _FIELD_CONDITION_TERMS.get(field.name, ()), field.complete)
        for field in fields
        if field.name != APPROVAL_INTERPRETATION
    )
    return condition_tracking.derive_condition_entries(
        session, dependency, conditional_links, field_candidates
    )


def confirm_interpretation(
    session: Session,
    dependency_id: int,
    evidence_link_id: int,
    *,
    principal: HumanPrincipal,
    condition_immaterial: bool = False,
) -> DocumentationFieldConfirmation:
    """Append one person's confirmation of a cited approval reading.

    The caller supplies only the exact cited source.  The classification is
    recomputed under the project lock and the route cannot substitute a
    conclusion, so a model output or a crafted form never becomes authority.
    A conditional letter records as conditional automatically at read time
    and needs no click to stay not ready (ADR-0060); the only stored acts are
    the clean-letter confirm and the optional ``condition_immaterial``
    override that counts the quoted hedge as immaterial.  Tracking the
    condition itself is #373's boundary.
    """

    principal = require_human_principal(principal)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise DocumentationConfirmationRefusal("no such constraint")
    if APPROVAL_INTERPRETATION not in required_field_names(dependency):
        raise DocumentationConfirmationRefusal("this constraint has no approval field")
    session.flush()
    lock_project(session, dependency.project_id)
    session.expire_all()
    evidence = _current_verified_evidence(session, dependency_id)
    by_id = {item.link_id: item for item in evidence}
    selected = by_id.get(evidence_link_id)
    if selected is None:
        link = session.get(EvidenceLink, evidence_link_id)
        if link is not None and not link.verified:
            raise DocumentationConfirmationRefusal("the cited passage must be verified")
        raise DocumentationConfirmationRefusal("the cited passage is not current support")
    classification = _approval_classification(selected.quote)
    if classification == "conditional" and not condition_immaterial:
        raise DocumentationConfirmationRefusal(
            "a conditional letter already records as conditional and stays "
            "not ready with no click; only the explicit condition-immaterial "
            "override records it as approval"
        )
    if classification == "approved" and condition_immaterial:
        raise DocumentationConfirmationRefusal(
            "the cited passage states a clean approval; there is no "
            "condition to override"
        )
    if classification not in {"approved", "conditional"}:
        raise DocumentationConfirmationRefusal(
            "the cited passage does not state an approval"
        )
    # ADR-0060: the override durably records the exact hedge it counted as
    # immaterial, beside who did it and when (the deposition answer #373 AC6
    # asks for).  A clean-letter confirm carries no overridden hedge.
    overridden = selected.quote if condition_immaterial else None
    confirmation = DocumentationFieldConfirmation(
        dependency_id=dependency_id,
        evidence_link_id=evidence_link_id,
        field_name=APPROVAL_INTERPRETATION,
        classification=classification,
        conclusion="approved",
        confirmed_by=principal.subject,
        condition_immaterial=condition_immaterial,
        overridden_condition_text=overridden,
    )
    session.add(confirmation)
    audit.record(
        session,
        principal=principal,
        action=audit.CONFIRM_DOCUMENTATION_INTERPRETATION,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency_id,
        after={
            "field_name": APPROVAL_INTERPRETATION,
            "classification": classification,
            "conclusion": "approved",
            "condition_immaterial": condition_immaterial,
            "evidence_link_id": evidence_link_id,
        },
    )
    session.flush()
    return confirmation


@dataclass(frozen=True)
class DocumentationClarification:
    """The two Work Decision receipts one Needs clarification request wrote."""

    owner_decision_id: int
    next_action_decision_id: int


class DocumentationClarificationRefusal(ValueError):
    """This requirement is already met, so a clarification is unnecessary."""


def record_documentation_clarification(
    session: Session,
    dependency_id: int,
    *,
    roster_entry_id: int,
    next_action: str,
    due_date: date | None,
    due_date_unknown_reason: str | None,
    principal: HumanPrincipal,
) -> DocumentationClarification:
    """Keep an open Documentation Review open while recording the follow-up.

    A Documentation Review answers whether current documentation meets the
    stated requirement (ADR-0037).  When a person cannot yet answer, they must
    be able to record an Internal Owner and Next Action without being forced to
    confirm the requirement met — the same Needs clarification structured
    follow-up a Source Discrepancy already supports.  This writes only the two
    ordinary Work Decisions; it never confirms an interpretation, moves support,
    or changes the derived readiness (ADR-0035, ADR-0042).
    """

    recorder = require_human_principal(principal)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise LookupError(f"no dependency {dependency_id}")
    checklist = read_checklist(session, dependency_id)
    if not checklist.uses_standard_checklist or all(
        field.complete for field in checklist.fields
    ):
        raise DocumentationClarificationRefusal(
            "this constraint has no open documentation requirement to clarify"
        )
    roster = session.get(ProjectRosterEntry, roster_entry_id)
    if (
        roster is None
        or roster.project_id != dependency.project_id
        or not roster.active
    ):
        raise ValueError(
            "the assignee must be an active member of this project roster"
        )

    with session.begin_nested():
        owner = assign_internal_owner(
            session, dependency_id, roster.display_name, principal=recorder
        )
        action = set_next_action(
            session,
            dependency_id,
            next_action,
            due_date=due_date,
            due_date_unknown_reason=due_date_unknown_reason,
            principal=recorder,
        )
    return DocumentationClarification(
        owner_decision_id=owner.id,
        next_action_decision_id=action.id,
    )


def _legacy_ready(session: Session, dependency_id: int) -> bool:
    """Whether the preserved pre-ADR-0052 mark has current effect."""

    return bool(
        session.scalar(
            select(DependencyEvidenceSufficiency.id)
            .join(
                EvidenceLink,
                DependencyEvidenceSufficiency.evidence_link_id == EvidenceLink.id,
            )
            .join(Document, EvidenceLink.document_id == Document.id)
            .where(
                DependencyEvidenceSufficiency.dependency_id == dependency_id,
                EvidenceLink.verified.is_(True),
                Document.superseded_by.is_(None),
            )
            .limit(1)
        )
    )


def _current_verified_evidence(
    session: Session, dependency_id: int
) -> tuple[_CurrentEvidence, ...]:
    return _current_verified_evidence_many(session, (dependency_id,))[dependency_id]


def _current_verified_evidence_many(
    session: Session, dependency_ids: Iterable[int]
) -> dict[int, tuple[_CurrentEvidence, ...]]:
    ids = tuple(dict.fromkeys(dependency_ids))
    by_dependency: dict[int, list[_CurrentEvidence]] = {
        dependency_id: [] for dependency_id in ids
    }
    if not ids:
        return {}
    for dependency_id, link, document in session.execute(
        select(Dependency.id, EvidenceLink, Document)
        .join(EvidenceLink, EvidenceLink.dependency_id == Dependency.id)
        .join(Document, EvidenceLink.document_id == Document.id)
        .where(
            Dependency.id.in_(ids),
            EvidenceLink.verified.is_(True),
            Document.superseded_by.is_(None),
            Document.project_id == Dependency.project_id,
        )
        .order_by(Dependency.id, EvidenceLink.id)
    ).all():
        by_dependency[dependency_id].append(
            _CurrentEvidence(
                link.id,
                link.quote,
                document.doc_type,
                document.id,
                document.filename,
                link.page_no,
            )
        )
    return {
        dependency_id: tuple(evidence)
        for dependency_id, evidence in by_dependency.items()
    }


def _field_state(
    name: str,
    evidence: tuple[_CurrentEvidence, ...],
    confirmations: tuple[DocumentationFieldConfirmation, ...],
) -> ChecklistField:
    if name == AS_BUILT:
        matches = tuple(item.link_id for item in evidence if _AS_BUILT.search(item.quote))
        return ChecklistField(name, _FIELD_LABELS[name], True, bool(matches), matches)
    if name == ABANDONMENT_DOCUMENTATION:
        matches = tuple(
            item.link_id for item in evidence if _ABANDONMENT.search(item.quote)
        )
        return ChecklistField(name, _FIELD_LABELS[name], True, bool(matches), matches)
    if name == EXECUTED_AGREEMENT_REFERENCE:
        matches = tuple(
            item.link_id
            for item in evidence
            if item.document_type == "agreement" and _EXECUTED_AGREEMENT.search(item.quote)
        )
        return ChecklistField(name, _FIELD_LABELS[name], True, bool(matches), matches)
    if name == APPROVAL_INTERPRETATION:
        classifications = {
            item.link_id: _approval_classification(item.quote) for item in evidence
        }
        candidates = tuple(
            link_id for link_id, classification in classifications.items() if classification == "approved"
        )
        # ADR-0060: hedged letters record as conditional here, at read time,
        # with the quoted sentence as their basis.  No stored row, no click —
        # the field simply stays empty until the condition is handled or a
        # person records the explicit override.
        conditional = tuple(
            link_id
            for link_id, classification in classifications.items()
            if classification == "conditional"
        )
        # A confirmation binds only while its exact cited letter still reads
        # the way the person was shown: a clean-letter confirm to a letter
        # still classified approved, an override to one still conditional.
        confirmed = tuple(
            confirmation
            for confirmation in confirmations
            if confirmation.field_name == APPROVAL_INTERPRETATION
            and confirmation.conclusion == "approved"
            and classifications.get(confirmation.evidence_link_id)
            == confirmation.classification
        )
        confirmation_ids = tuple(item.id for item in confirmed)
        evidence_ids = tuple(item.evidence_link_id for item in confirmed)
        candidate_conclusion = None
        if candidates:
            candidate_conclusion = "approved"
        elif conditional:
            candidate_conclusion = "conditional"
        return ChecklistField(
            name,
            _FIELD_LABELS[name],
            False,
            bool(confirmed),
            evidence_ids,
            candidates,
            candidate_conclusion,
            confirmation_ids,
            conditional,
        )
    raise AssertionError(f"unknown standard checklist field {name!r}")


def _approval_classification(quote: str) -> str | None:
    """Conservative source-text classification shown for the human confirm."""

    if _HEDGE.search(quote):
        return "conditional" if _APPROVED.search(quote) else None
    if _NEGATED_APPROVAL.search(quote):
        return None
    return "approved" if _APPROVED.search(quote) else None
