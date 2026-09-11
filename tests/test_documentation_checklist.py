"""Public standard-documentation checklist behavior (#347, ADR-0052)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from corridor.documentation_checklist import (
    DocumentationClarificationRefusal,
    DocumentationConfirmationRefusal,
    confirm_interpretation,
    read_checklist,
    read_checklists,
    record_documentation_clarification,
)
from corridor.ledger import mark_satisfies
from corridor.models import (
    AuditLog,
    Dependency,
    DocumentationFieldConfirmation,
    DocPage,
    Document,
    EvidenceLink,
    Project,
    ProjectRosterEntry,
)
from corridor.presentation import (
    documentation_review_label,
    documentation_state_label,
)
from corridor.principals import HumanPrincipal
from corridor.work_decisions import (
    current_internal_owner_decision,
    current_next_action_decision,
)


REVIEWER = HumanPrincipal("local:checklist-reviewer")


@pytest.fixture
def project(session):
    project = Project(slug="checklist", name="Checklist", is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _document(session, project, *, name: str, text: str) -> Document:
    document = Document(
        project_id=project.id,
        sha256=(name * 64)[:64],
        filename=f"{name}.pdf",
        doc_type="agreement",
        parse_status="parsed",
        doc_date=date(2026, 8, 29),
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=text))
    session.flush()
    return document


def _support(session, dependency, document, quote: str, *, verified: bool = True):
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=quote,
        verified=verified,
    )
    session.add(link)
    session.flush()
    return link


def test_batch_checklist_requires_complete_legacy_readiness_inputs(session, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-batch-input",
        dep_type="utility_relocation",
        title="Legacy readiness input",
    )
    session.add(dependency)
    session.flush()

    with pytest.raises(ValueError, match="complete legacy readiness"):
        read_checklists(
            session,
            (dependency.id,),
            legacy_ready_by_dependency={},
        )


def test_relocation_checklist_derives_machine_field_and_requires_cited_confirmation(
    session, project
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-1",
        dep_type="utility_relocation",
        title="Gas crossing",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="approval",
        text="The as-built package is on file. The relocation is approved.",
    )
    as_built = _support(
        session, dependency, document, "The as-built package is on file.")
    approval = _support(session, dependency, document, "The relocation is approved.")

    before = read_checklist(session, dependency.id)

    assert before.uses_standard_checklist is True
    assert before.is_ready is False
    assert before.field("as_built").complete is True
    assert before.field("as_built").evidence_link_ids == (as_built.id,)
    interpretation = before.field("approval_interpretation")
    assert interpretation.complete is False
    assert interpretation.candidate_evidence_link_ids == (approval.id,)
    assert interpretation.candidate_conclusion == "approved"

    confirmation = confirm_interpretation(
        session,
        dependency.id,
        approval.id,
        principal=REVIEWER,
    )

    after = read_checklist(session, dependency.id)
    assert confirmation.field_name == "approval_interpretation"
    assert after.is_ready is True
    assert after.field("approval_interpretation").complete is True
    persisted = session.scalars(select(DocumentationFieldConfirmation)).one()
    assert (persisted.confirmed_by, persisted.conclusion, persisted.evidence_link_id) == (
        REVIEWER.subject,
        "approved",
        approval.id,
    )
    audit = session.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert audit is not None
    assert audit.action == "confirm_documentation_interpretation"
    assert audit.actor == REVIEWER.subject
    assert audit.after_json["evidence_link_id"] == approval.id


def test_the_two_documentation_states_are_named_by_their_own_words():
    """Filled standard fields and a retained legacy mark are different facts.

    Both sentences used to be composed inside `dependency.html`, the
    standard-checklist pair as Jinja literals that appeared nowhere in `src/`
    at all — so one Project Record question was answered by an owned label on
    the legacy path and by the screen itself on the other. These are the exact
    words the screen has always rendered; changing either is a terminology
    decision (`docs/agents/domain.md`), not an edit to this test.
    """

    assert (
        documentation_state_label(True, uses_standard_checklist=True)
        == "Documentation fields complete"
    )
    assert (
        documentation_state_label(False, uses_standard_checklist=True)
        == "Documentation fields not complete"
    )
    # The legacy mark keeps its own adopted label rather than a second copy of
    # it: `documentation_review_label` is still the one owner of those words.
    assert documentation_state_label(
        True, uses_standard_checklist=False
    ) == documentation_review_label(True)
    assert documentation_state_label(
        False, uses_standard_checklist=False
    ) == documentation_review_label(False)
    # Neither path may describe the other's fact.
    assert documentation_state_label(
        True, uses_standard_checklist=False
    ) != documentation_state_label(True, uses_standard_checklist=True)


def test_legacy_sufficiency_stays_effective_until_a_structured_confirmation_replaces_it(
    session, project
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-2",
        dep_type="utility_relocation",
        title="Legacy Gas crossing",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="legacy",
        text="The as-built package is on file. The relocation is approved.",
    )
    legacy = _support(session, dependency, document, "The as-built package is on file.")
    approval = _support(session, dependency, document, "The relocation is approved.")
    mark_satisfies(session, dependency.id, legacy.id, principal=REVIEWER)

    before = read_checklist(session, dependency.id)
    assert before.uses_standard_checklist is False
    assert before.legacy_mark_remains_effective is True
    assert before.is_ready is True

    confirm_interpretation(session, dependency.id, approval.id, principal=REVIEWER)
    after = read_checklist(session, dependency.id)
    assert after.uses_standard_checklist is True
    assert after.legacy_mark_remains_effective is False
    assert after.is_ready is True


def test_reimbursable_work_requires_an_executed_agreement_reference(session, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-3",
        dep_type="utility_relocation",
        title="Reimbursable relocation",
        cost_responsibility="reimbursable",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="agreement",
        text="The utility agreement is fully executed by all parties.",
    )
    evidence = _support(
        session,
        dependency,
        document,
        "The utility agreement is fully executed by all parties.",
    )

    checklist = read_checklist(session, dependency.id)

    assert checklist.is_ready is True
    field = checklist.field("executed_agreement_reference")
    assert field.complete is True
    assert field.evidence_link_ids == (evidence.id,)


def test_unverified_or_conditional_letters_cannot_be_confirmed_as_approval(session, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-4",
        dep_type="utility_relocation",
        title="Conditional approval",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="conditional",
        text="The relocation is approved pending final inspection.",
    )
    conditional = _support(
        session,
        dependency,
        document,
        "The relocation is approved pending final inspection.",
    )
    unverified = _support(
        session,
        dependency,
        document,
        "The relocation is approved.",
        verified=False,
    )

    with pytest.raises(DocumentationConfirmationRefusal, match="conditional"):
        confirm_interpretation(session, dependency.id, conditional.id, principal=REVIEWER)
    with pytest.raises(DocumentationConfirmationRefusal, match="verified"):
        confirm_interpretation(session, dependency.id, unverified.id, principal=REVIEWER)
    assert read_checklist(session, dependency.id).is_ready is False


def test_a_conditional_letter_records_as_conditional_with_no_human_act(
    session, project
):
    """ADR-0060: the fail-closed conditional answer derives itself at read."""

    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-6",
        dep_type="utility_relocation",
        title="Hedged approval",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="hedged",
        text="The as-built package is on file. "
        "The relocation is approved pending final inspection of segment B.",
    )
    _support(session, dependency, document, "The as-built package is on file.")
    hedged = _support(
        session,
        dependency,
        document,
        "The relocation is approved pending final inspection of segment B.",
    )

    checklist = read_checklist(session, dependency.id)

    field = checklist.field("approval_interpretation")
    assert checklist.is_ready is False
    assert field.complete is False
    assert field.candidate_conclusion == "conditional"
    assert field.conditional_evidence_link_ids == (hedged.id,)
    # No stored answer exists: the conditional reading is a read-time
    # predicate over the quoted sentence, never a written row.
    assert session.scalars(select(DocumentationFieldConfirmation)).all() == []


def test_the_optional_override_records_a_hedged_letter_as_approval(session, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-7",
        dep_type="utility_relocation",
        title="Immaterial hedge",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="immaterial",
        text="The as-built package is on file. "
        "The relocation is approved subject to updating our records.",
    )
    _support(session, dependency, document, "The as-built package is on file.")
    hedged = _support(
        session,
        dependency,
        document,
        "The relocation is approved subject to updating our records.",
    )

    confirmation = confirm_interpretation(
        session,
        dependency.id,
        hedged.id,
        principal=REVIEWER,
        condition_immaterial=True,
    )

    assert (confirmation.classification, confirmation.conclusion) == (
        "conditional",
        "approved",
    )
    after = read_checklist(session, dependency.id)
    assert after.is_ready is True
    assert after.field("approval_interpretation").complete is True
    audit = session.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert audit is not None
    assert audit.after_json["condition_immaterial"] is True
    assert audit.after_json["classification"] == "conditional"


def test_the_override_refuses_a_letter_without_a_condition(session, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-8",
        dep_type="utility_relocation",
        title="Clean approval",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session, project, name="clean", text="The relocation is approved."
    )
    clean = _support(session, dependency, document, "The relocation is approved.")

    with pytest.raises(DocumentationConfirmationRefusal, match="no\\s+condition"):
        confirm_interpretation(
            session,
            dependency.id,
            clean.id,
            principal=REVIEWER,
            condition_immaterial=True,
        )
    assert session.scalars(select(DocumentationFieldConfirmation)).all() == []


def test_confirmation_stops_binding_when_its_exact_document_is_superseded(
    session, project
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-5",
        dep_type="utility_relocation",
        title="Superseded approval",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    approved = _document(
        session,
        project,
        name="approved",
        text="The as-built package is on file. The relocation is approved.",
    )
    _support(session, dependency, approved, "The as-built package is on file.")
    approval = _support(session, dependency, approved, "The relocation is approved.")
    confirm_interpretation(session, dependency.id, approval.id, principal=REVIEWER)
    assert read_checklist(session, dependency.id).is_ready is True

    successor = _document(
        session,
        project,
        name="approval-revision",
        text="Replacement approval package.",
    )
    index = _document(
        session,
        project,
        name="approval-index",
        text="APPROVED-1 is superseded by APPROVED-2 on 2026-08-30.",
    )
    approved.registry_id = "APPROVED-1"
    successor.registry_id = "APPROVED-2"
    index.registry_id = "APPROVAL-INDEX"
    session.flush()
    approved.superseded_by = successor.id
    approved.superseded_on = date(2026, 8, 30)
    approved.supersession_source_document_id = index.id
    approved.supersession_source_page = 1
    session.flush()

    current = read_checklist(session, dependency.id)
    assert current.is_ready is False
    assert current.field("approval_interpretation").complete is False


def _roster(session, project, *, display_name="Dana Reviewer", active=True):
    entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject=f"local:{display_name.lower().replace(' ', '-')}",
        display_name=display_name,
        active=active,
    )
    session.add(entry)
    session.flush()
    return entry


def test_documentation_needs_clarification_records_follow_up_without_confirming(
    session, project
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-CLARIFY",
        dep_type="utility_relocation",
        title="Gas crossing",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="hedged",
        text="The as-built package is on file. Approval is pending final sign-off.",
    )
    _support(session, dependency, document, "The as-built package is on file.")
    _support(session, dependency, document, "Approval is pending final sign-off.")
    roster = _roster(session, project)

    before = read_checklist(session, dependency.id)
    assert before.uses_standard_checklist is True
    assert before.is_ready is False

    clarification = record_documentation_clarification(
        session,
        dependency.id,
        roster_entry_id=roster.id,
        next_action="Ask the utility for a clean approval letter",
        due_date=date(2026, 9, 15),
        due_date_unknown_reason=None,
        principal=REVIEWER,
    )

    # The follow-up is recorded, and the requirement stays not met — no
    # conclusion was forced simply to clear the work.
    owner = current_internal_owner_decision(session, dependency.id)
    action = current_next_action_decision(session, dependency.id)
    assert owner is not None and owner.id == clarification.owner_decision_id
    assert action is not None and action.id == clarification.next_action_decision_id
    assert owner.after_value == "Dana Reviewer"
    after = read_checklist(session, dependency.id)
    assert after.is_ready is False
    assert after.field("approval_interpretation").complete is False
    assert session.scalars(select(DocumentationFieldConfirmation)).all() == []


def test_documentation_clarification_refused_when_requirement_already_met(
    session, project
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-DONE",
        dep_type="utility_relocation",
        title="Gas crossing",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="clean",
        text="The as-built package is on file. The relocation is approved.",
    )
    _support(session, dependency, document, "The as-built package is on file.")
    approval = _support(session, dependency, document, "The relocation is approved.")
    confirm_interpretation(session, dependency.id, approval.id, principal=REVIEWER)
    roster = _roster(session, project)

    assert read_checklist(session, dependency.id).is_ready is True

    with pytest.raises(DocumentationClarificationRefusal):
        record_documentation_clarification(
            session,
            dependency.id,
            roster_entry_id=roster.id,
            next_action="anything",
            due_date=None,
            due_date_unknown_reason="waiting",
            principal=REVIEWER,
        )


def test_documentation_clarification_rejects_a_foreign_roster_member(session, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-FOREIGN",
        dep_type="utility_relocation",
        title="Gas crossing",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(session, project, name="hedged2", text="approval pending")
    _support(session, dependency, document, "approval pending")
    other_project = Project(slug="other", name="Other", is_synthetic=True)
    session.add(other_project)
    session.flush()
    foreign = _roster(session, other_project, display_name="Outsider")

    with pytest.raises(ValueError):
        record_documentation_clarification(
            session,
            dependency.id,
            roster_entry_id=foreign.id,
            next_action="anything",
            due_date=None,
            due_date_unknown_reason="waiting",
            principal=REVIEWER,
        )
