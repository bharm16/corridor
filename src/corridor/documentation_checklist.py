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

from dataclasses import dataclass
from datetime import date
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
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

_HEDGE = re.compile(
    r"\b(pending|subject to|conditional|conditioned|after|until|provided that)\b",
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

    def field(self, name: str) -> ChecklistField:
        for field in self.fields:
            if field.name == name:
                return field
        raise KeyError(name)


@dataclass(frozen=True)
class _CurrentEvidence:
    link_id: int
    quote: str
    document_type: str


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
    required = required_field_names(dependency)
    # Only selector values with an adopted field mapping take over from
    # legacy history.  ``protect_in_place`` is the explicitly complete
    # zero-external-field case; an unmodeled strategy (for example a policy
    # exception) must not accidentally become Ready through vacuous truth.
    has_selector = bool(required) or dependency.resolution_strategy == "protect_in_place"
    if legacy_ready is None:
        legacy_ready = _legacy_ready(session, dependency_id)
    confirmations = tuple(
        session.scalars(
            select(DocumentationFieldConfirmation)
            .where(DocumentationFieldConfirmation.dependency_id == dependency_id)
            .order_by(DocumentationFieldConfirmation.id)
        ).all()
    )
    uses_standard = has_selector and (not legacy_ready or bool(confirmations))
    evidence = _current_verified_evidence(session, dependency_id)
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
    return DocumentationChecklist(
        dependency_id=dependency_id,
        fields=fields,
        uses_standard_checklist=True,
        legacy_mark_remains_effective=False,
        is_ready=all(field.complete for field in fields),
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
    confirmation = DocumentationFieldConfirmation(
        dependency_id=dependency_id,
        evidence_link_id=evidence_link_id,
        field_name=APPROVAL_INTERPRETATION,
        classification=classification,
        conclusion="approved",
        confirmed_by=principal.subject,
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
    return tuple(
        _CurrentEvidence(link.id, link.quote, document.doc_type)
        for link, document in session.execute(
            select(EvidenceLink, Document)
            .join(Document, EvidenceLink.document_id == Document.id)
            .join(Dependency, EvidenceLink.dependency_id == Dependency.id)
            .where(
                EvidenceLink.dependency_id == dependency_id,
                EvidenceLink.verified.is_(True),
                Document.superseded_by.is_(None),
                Document.project_id == Dependency.project_id,
            )
            .order_by(EvidenceLink.id)
        ).all()
    )


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
