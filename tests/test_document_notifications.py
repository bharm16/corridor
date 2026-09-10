"""Document-change and lost-support interruption discovery and registration (#353).

These tests drive the domain seams through a rollback-scoped session. An earlier
affirmative Documentation Review that loses applicable current support registers
one occurrence per typed recipient (the current assignee and the original
reviewer, deduplicated); an exact-unchanged support update and a historical
marker without the required identity register nothing; an authentic proven source
transition to a relocation/removal/abandonment Constraint registers a change
interruption, and an ambiguous correspondence is retained as uncertain rather than
proved. Recipients resolve only through typed roster and contact records, missing
mappings stay visible, a rolled-back transition leaves nothing, and reads never
leak across projects. Delivery over the shared runtime is proven in
test_document_notifications_runtime.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor import document_notifications as dn
from corridor.adjudicate import accept_candidate
from corridor.documentation_checklist import confirm_interpretation, read_checklist
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    Candidate,
    CommitmentLineage,
    Dependency,
    DocPage,
    Document,
    DocumentNotification,
    DocumentNotificationDispatch,
    EvidenceLink,
    ExternalOrg,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
)
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import create_revision_comparison
from corridor.supersession import SupersessionDeclaration, register_supersessions
from corridor.work_decisions import (
    CoordinationSubject,
    FollowUpPlanDraft,
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    save_follow_up_plan,
)

REGISTRAR = "runtime:document-notification-test"
REVIEWER = HumanPrincipal("local:doc-notify-reviewer")
ASSIGNEE = HumanPrincipal("local:doc-notify-assignee")
COORDINATOR = HumanPrincipal("local:doc-notify-coordinator")


# --- shared fixtures ------------------------------------------------------


def _roster(session, project, principal, *, display_name, email="person@example.com"):
    entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject=principal.subject,
        display_name=display_name,
        active=True,
        can_coordinate=True,
        can_review_documentation=True,
    )
    session.add(entry)
    session.flush()
    if email is not None and session.scalar(
        select(PersonIdentity).where(PersonIdentity.principal_subject == principal.subject)
    ) is None:
        session.add(
            PersonIdentity(email_normalized=email, principal_subject=principal.subject)
        )
        session.flush()
    return entry


def _document(session, project, *, name, text, doc_type="agreement", registry_id=None):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=(name * 64)[:64],
        filename=f"{name}.pdf",
        doc_type=doc_type,
        parse_status="parsed",
        doc_date=date(2026, 8, 29),
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=text))
    session.flush()
    return document


def _support(session, dependency, document, quote, *, verified=True):
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


def _affirmative_review(session, project, *, ref_code, reviewer=REVIEWER):
    """A relocation Constraint made Ready by a confirmed approval interpretation.

    The as-built (machine) support and the approval letter sit on separate
    documents so superseding only the approval document isolates the loss of the
    affirmative review's own reviewed support.
    """

    dependency = Dependency(
        project_id=project.id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title="Gas crossing",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    as_built_doc = _document(
        session, project, name=f"asbuilt-{ref_code}", text="The as-built package is on file."
    )
    _support(session, dependency, as_built_doc, "The as-built package is on file.")
    approval_doc = _document(
        session,
        project,
        name=f"approval-{ref_code}",
        text="The relocation is approved.",
        registry_id=f"APPROVAL-{ref_code}",
    )
    approval = _support(session, dependency, approval_doc, "The relocation is approved.")
    confirmation = confirm_interpretation(
        session, dependency.id, approval.id, principal=reviewer
    )
    assert read_checklist(session, dependency.id).is_ready is True
    return dependency, confirmation, approval_doc, approval


def _supersede_document(session, project, document, *, name):
    """Register a real supersession so the predecessor becomes non-current."""

    successor = _document(
        session,
        project,
        name=name,
        text="A newer superseding revision.",
        registry_id=f"SUCC-{name}",
    )
    _document(
        session,
        project,
        name=f"idx-{name}",
        text=f"{document.registry_id} superseded by SUCC-{name} on 2026-08-01",
        doc_type="other",
        registry_id=f"IDX-{name}",
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=document.registry_id,
                successor_registry_id=f"SUCC-{name}",
                replacement_date=date(2026, 8, 1),
                source_registry_id=f"IDX-{name}",
                source_page=1,
            )
        ],
        project_id=project.id,
    )
    session.refresh(document)
    return successor


def _assign_constraint(session, dependency, roster, *, principal=COORDINATOR):
    return save_follow_up_plan(
        session,
        FollowUpPlanDraft(
            dependency_id=dependency.id,
            internal_owner_roster_entry_id=roster.id,
            next_action=FOLLOW_UP_NEXT_ACTION_CHOICES[0],
            action_due_date=date(2026, 9, 1),
            action_due_date_unknown_reason=None,
        ),
        principal=principal,
    )


# --- Category A: an affirmative review loses applicable support ------------


def test_lost_support_reaches_current_assignee_and_original_reviewer(session, project):
    dependency, confirmation, approval_doc, approval = _affirmative_review(
        session, project, ref_code="DN-A1"
    )
    assignee_roster = _roster(
        session, project, ASSIGNEE, display_name="Dana Assignee", email="dana@example.com"
    )
    _roster(session, project, REVIEWER, display_name="Rae Reviewer", email="rae@example.com")
    _assign_constraint(session, dependency, assignee_roster)
    successor = _supersede_document(session, project, approval_doc, name="approval-DN-A1-b")

    # Superseding the reviewed approval letter drops the requirement.
    assert read_checklist(session, dependency.id).is_ready is False

    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    assert len(registered) == 2
    by_recipient = {n.recipient_principal_subject: n for n in registered}
    assert set(by_recipient) == {ASSIGNEE.subject, REVIEWER.subject}
    for notification in registered:
        assert notification.category == dn.CATEGORY_DOCUMENTATION_LOSS
        assert notification.review_confirmation_id == confirmation.id
        assert notification.requirement_field == "approval_interpretation"
        assert notification.reviewed_evidence_link_id == approval.id
        assert notification.original_reviewer_subject == REVIEWER.subject
        assert notification.reason_code == dn.REASON_SUPERSEDING_REVISION
        assert notification.successor_document_id == successor.id
    assert by_recipient[ASSIGNEE.subject].recipient_role == dn.ROLE_CURRENT_ASSIGNEE
    assert by_recipient[REVIEWER.subject].recipient_role == dn.ROLE_ORIGINAL_REVIEWER
    # The message preserves the earlier judgment and does not assert physical work.
    summary = dn._subject_summary(session, by_recipient[REVIEWER.subject])
    assert summary["earlier_review"]["author"] == REVIEWER.subject
    assert "physical work" in summary["body"]
    assert "does not reverse" in summary["body"]


def test_coinciding_recipient_is_deduplicated_to_one_occurrence(session, project):
    # The reviewer is also the current assignee: one occurrence, combined role.
    dependency, _confirmation, approval_doc, _approval = _affirmative_review(
        session, project, ref_code="DN-A2", reviewer=ASSIGNEE
    )
    assignee_roster = _roster(
        session, project, ASSIGNEE, display_name="Dana Both", email="dana@example.com"
    )
    _assign_constraint(session, dependency, assignee_roster)
    _supersede_document(session, project, approval_doc, name="approval-DN-A2-b")

    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    assert len(registered) == 1
    assert registered[0].recipient_principal_subject == ASSIGNEE.subject
    assert registered[0].recipient_role == dn.ROLE_ASSIGNEE_AND_REVIEWER


def test_unresolved_assignee_mapping_stays_visible_not_inferred(session, project):
    # A current owner exists but through no typed roster binding: the reviewer is
    # still reached, and the assignee gap is recorded, never name-inferred.
    dependency, _confirmation, approval_doc, _approval = _affirmative_review(
        session, project, ref_code="DN-A3"
    )
    _roster(session, project, REVIEWER, display_name="Rae Reviewer", email="rae@example.com")
    # An owner set outside the guided plan leaves no FollowUpPlanReceipt binding.
    from corridor.work_decisions import assign_internal_owner

    assign_internal_owner(
        session, CoordinationSubject.dependency(dependency.id), "Legacy Owner", principal=COORDINATOR
    )
    _supersede_document(session, project, approval_doc, name="approval-DN-A3-b")

    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    assert [n.recipient_principal_subject for n in registered] == [REVIEWER.subject]
    assert registered[0].source_context_json["current_assignee_mapping"] == "unresolved"


def test_reconfirmed_requirement_is_not_a_loss(session, project):
    # A re-review that re-establishes the requirement on the newer document is
    # not a loss: an exact re-confirmation keeps the constraint Ready.
    dependency, _confirmation, approval_doc, _approval = _affirmative_review(
        session, project, ref_code="DN-A4"
    )
    _roster(session, project, REVIEWER, display_name="Rae Reviewer", email="rae@example.com")
    successor = _supersede_document(session, project, approval_doc, name="approval-DN-A4-b")
    # The reviewer confirms the newer approval letter, restoring the requirement.
    new_approval = _support(
        session, dependency, successor, "The relocation is approved.")
    successor_page = session.scalars(
        select(DocPage).where(DocPage.document_id == successor.id)
    ).one()
    successor_page.text = "The relocation is approved."
    session.flush()
    confirm_interpretation(session, dependency.id, new_approval.id, principal=REVIEWER)
    assert read_checklist(session, dependency.id).is_ready is True

    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    assert registered == ()


def test_legacy_readiness_marker_without_identity_is_not_reinterpreted(session, project):
    # A preserved legacy sufficiency mark has no reviewer identity; its loss is
    # not silently reinterpreted as an affirmative Documentation Review.
    dependency = Dependency(
        project_id=project.id,
        ref_code="DN-A5",
        dep_type="utility_relocation",
        title="Legacy crossing",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document = _document(
        session,
        project,
        name="legacy-DN-A5",
        text="The as-built package is on file.",
        registry_id="LEGACY-DN-A5",
    )
    legacy = _support(session, dependency, document, "The as-built package is on file.")
    mark_satisfies(session, dependency.id, legacy.id, principal=REVIEWER)
    _roster(session, project, ASSIGNEE, display_name="Dana", email="dana@example.com")
    _supersede_document(session, project, document, name="legacy-DN-A5-b")

    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    assert registered == ()


def test_repeated_discovery_converges_on_one_occurrence_per_recipient(session, project):
    dependency, _confirmation, approval_doc, _approval = _affirmative_review(
        session, project, ref_code="DN-A6"
    )
    _roster(session, project, REVIEWER, display_name="Rae", email="rae@example.com")
    _supersede_document(session, project, approval_doc, name="approval-DN-A6-b")

    first = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )
    second = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    assert {n.id for n in first} == {n.id for n in second}
    assert (
        session.scalar(select(func.count()).select_from(DocumentNotification))
        == len(first)
    )


def test_rolled_back_transition_leaves_no_occurrence(session, project):
    _roster(session, project, REVIEWER, display_name="Rae", email="rae@example.com")
    with session.begin_nested() as savepoint:
        dependency, _c, approval_doc, _a = _affirmative_review(
            session, project, ref_code="DN-A7"
        )
        _supersede_document(session, project, approval_doc, name="approval-DN-A7-b")
        savepoint.rollback()

    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    assert registered == ()
    assert session.scalar(select(func.count()).select_from(DocumentNotification)) == 0


# --- Category B: a proven source transition affects a qualifying subject ----


def _change_seed(session, *, successor_rows, prior_satisfying=False, strategy="relocate"):
    """Seed one superseded, admitted Constraint and its successor revision."""

    project = Project(
        slug=f"docnotif-change-{uuid4().hex}", name="Change", is_synthetic=True
    )
    session.add(project)
    session.flush([project])
    if session.scalar(select(ExternalOrg).where(ExternalOrg.name == "AT&T")) is None:
        session.add(ExternalOrg(name="AT&T", aliases=[]))
        session.flush()

    def _fields(baseline=None, **overrides):
        fields = {
            "utility_id": "FOC1-1",
            "external_org": "AT&T",
            "utility_type": "Telecom",
            "station_from": "100+00",
        }
        if baseline is not None:
            fields["baseline"] = baseline
        fields.update(overrides)
        return fields

    def _quote(fields):
        return " ".join(
            str(v)
            for v in (
                fields["utility_id"],
                fields["external_org"],
                fields["utility_type"],
                fields["station_from"],
                fields.get("baseline"),
            )
            if v
        )

    def _document_row(registry_id, sha, filename, page_text):
        document = Document(
            project_id=project.id,
            registry_id=registry_id,
            sha256=sha * 64,
            filename=filename,
            doc_type="other" if registry_id == "INDEX" else "matrix",
            parse_status="parsed",
            pages=1,
        )
        session.add(document)
        session.flush([document])
        session.add(
            DocPage(document_id=document.id, page_no=1, text=page_text, image_path=f"/tmp/{filename}.png")
        )
        session.flush()
        return document

    def _candidate_row(document, fields):
        return Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": dict(fields),
                "citations": [
                    {"document_id": document.id, "page": 1, "quote": _quote(fields), "verified": True}
                ],
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=1.0,
            prompt_version="matrix-v1",
            model="test-model",
            citations_verified=True,
        )

    def _completed(document, *candidates):
        run = record_extraction_run(
            session,
            document,
            prompt_version="matrix-v1",
            candidate_count=len(candidates),
            page_errors=0,
            candidates=candidates,
            model="test-model",
            schema_version="candidate-v1",
            allow_unsealed_legacy=True,
        )
        declare_active_run(session, document.id, run.id, principal=COORDINATOR)
        session.flush()
        return run

    predecessor_fields = _fields()
    predecessor = _document_row("REV-A", "a", "revision-a.pdf", _quote(predecessor_fields))
    successor = _document_row(
        "REV-B", "b", "revision-b.pdf", "FOC1-1 AT&T Telecom 100+00 200+00 IH-69"
    )
    _document_row("INDEX", "c", "index.pdf", "REV-A superseded by REV-B on 2026-08-01")

    predecessor_candidate = _candidate_row(predecessor, predecessor_fields)
    predecessor_run = _completed(predecessor, predecessor_candidate)
    dependency = accept_candidate(session, predecessor_candidate, principal=COORDINATOR)
    dependency.evidence_required = "approved relocation closeout"
    dependency.resolution_strategy = strategy
    session.flush()
    old_evidence = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == dependency.id)
        .order_by(EvidenceLink.id)
    ).one()
    if prior_satisfying:
        mark_satisfies(session, dependency.id, old_evidence.id, principal=COORDINATOR)

    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-A",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    successor_candidates = []
    for overrides in successor_rows:
        fields = _fields(**overrides)
        successor_candidates.append(_candidate_row(successor, fields))
    successor_run = _completed(successor, *successor_candidates)
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=predecessor_run.id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    session.flush()
    return {"project": project, "dependency": dependency, "successor": successor}


def test_proven_change_to_relocation_constraint_registers_to_assignee(session, project):
    scenario = _change_seed(session, successor_rows=({"utility_type": "Gas"},))
    roster = _roster(
        session, scenario["project"], ASSIGNEE, display_name="Dana", email="dana@example.com"
    )
    _assign_constraint(session, scenario["dependency"], roster)

    registered = dn.register_project_document_notifications(
        session, project_id=scenario["project"].id, registered_by=REGISTRAR
    )

    assert len(registered) == 1
    notification = registered[0]
    assert notification.category == dn.CATEGORY_DOCUMENT_CHANGE
    assert notification.subject_kind == "constraint"
    assert notification.dependency_id == scenario["dependency"].id
    assert notification.recipient_principal_subject == ASSIGNEE.subject
    assert notification.comparison_id is not None
    assert notification.successor_document_id == scenario["successor"].id
    assert notification.change_uncertain is False
    summary = dn._subject_summary(session, notification)
    assert summary["uncertain"] is False


def test_ambiguous_correspondence_is_retained_as_uncertain_not_proved(session, project):
    scenario = _change_seed(
        session,
        successor_rows=({"utility_id": "FOC9-9", "station_from": "101+50"},),
    )
    roster = _roster(
        session, scenario["project"], ASSIGNEE, display_name="Dana", email="dana@example.com"
    )
    _assign_constraint(session, scenario["dependency"], roster)

    registered = dn.register_project_document_notifications(
        session, project_id=scenario["project"].id, registered_by=REGISTRAR
    )

    assert len(registered) == 1
    assert registered[0].change_uncertain is True
    summary = dn._subject_summary(session, registered[0])
    assert summary["uncertain"] is True
    assert "uncertain" in summary["body"]
    assert "not treated as a proved change" in summary["body"]


def test_exact_unchanged_support_update_registers_nothing(session, project):
    scenario = _change_seed(session, successor_rows=({},))
    roster = _roster(
        session, scenario["project"], ASSIGNEE, display_name="Dana", email="dana@example.com"
    )
    _assign_constraint(session, scenario["dependency"], roster)

    registered = dn.register_project_document_notifications(
        session, project_id=scenario["project"].id, registered_by=REGISTRAR
    )

    assert registered == ()


def test_non_relocation_constraint_change_is_not_a_category_four(session, project):
    # A document change to a Constraint that is not a relocation/removal/
    # abandonment subject is not a category-4 interruption.
    scenario = _change_seed(
        session, successor_rows=({"utility_type": "Gas"},), strategy="protect_in_place"
    )
    roster = _roster(
        session, scenario["project"], ASSIGNEE, display_name="Dana", email="dana@example.com"
    )
    _assign_constraint(session, scenario["dependency"], roster)

    registered = dn.register_project_document_notifications(
        session, project_id=scenario["project"].id, registered_by=REGISTRAR
    )

    assert registered == ()


def test_readiness_loss_routes_to_category_a_not_document_change(session, project):
    # A changed readiness support on a legacy-satisfied Constraint routes to a
    # Documentation Review consequence; it is not a document-change interruption,
    # and its legacy marker carries no reviewer identity, so nothing registers.
    scenario = _change_seed(
        session, successor_rows=({"utility_type": "Gas"},), prior_satisfying=True
    )
    roster = _roster(
        session, scenario["project"], ASSIGNEE, display_name="Dana", email="dana@example.com"
    )
    _assign_constraint(session, scenario["dependency"], roster)

    registered = dn.register_project_document_notifications(
        session, project_id=scenario["project"].id, registered_by=REGISTRAR
    )

    assert [n.category for n in registered] == []


def test_unassigned_change_registers_nothing_but_stays_on_the_work_list(session, project):
    # No current assignee to interrupt: no occurrence, and the change remains a
    # derived Work List item (never a fabricated recipient).
    scenario = _change_seed(session, successor_rows=({"utility_type": "Gas"},))

    registered = dn.register_project_document_notifications(
        session, project_id=scenario["project"].id, registered_by=REGISTRAR
    )

    assert registered == ()


# --- Category B: a factual successor affects a current Commitment ----------


def _commitment_with_factual_successor(session, project):
    """A commitment whose plan a superseding statement marks for review."""

    from corridor.external_statements import (
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )
    from corridor.work_decisions import assign_internal_owner

    party = ExternalOrg(name=f"Party {uuid4().hex[:8]}")
    session.add(party)
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code=f"DN-B{uuid4().hex[:4]}",
        dep_type="utility_relocation",
        title="Commitment subject",
        external_org_id=party.id,
    )
    session.add(dependency)
    session.flush()
    first = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="verbal",
        event_date=date(2026, 8, 12),
        description="Party will provide the relocation schedule.",
        new_timing=StatementTiming.day("August 20, 2026", date(2026, 8, 20)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
    )
    subject = CoordinationSubject.statement(first.commitment_lineage_id)
    assign_internal_owner(session, subject, "Dana Fields", principal=COORDINATOR)
    successor = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="verbal",
        event_date=date(2026, 8, 13),
        description="Party will provide a revised relocation schedule.",
        new_timing=StatementTiming.day("August 21, 2026", date(2026, 8, 21)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
        commitment_lineage_id=subject.commitment_lineage_id,
    )
    return first.commitment_lineage_id, successor


def _coordinate_commitment(session, project, owner_principal):
    """A guided-coordinated commitment with a typed roster owner binding."""

    from corridor.external_statements import StatementScope, StatementTiming
    from corridor.statement_coordination import (
        StatementCoordinationDraft,
        coordinate_statement,
    )
    from corridor.statement_values import CitedStatementEvidence

    party_name = f"Coord Party {uuid4().hex[:8]}"
    party = ExternalOrg(name=party_name)
    session.add(party)
    session.flush()
    roster = _roster(
        session, project, owner_principal, display_name="Owner Fields", email="owner@example.com"
    )
    quote = f"{party_name} proposes extending the completion to May 16th."
    document = _document(
        session, project, name=f"minutes-{uuid4().hex[:6]}", text=quote, doc_type="minutes"
    )
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {"event_type": "slip", "description": quote},
            "citations": [{"document_id": document.id, "page": 1, "quote": quote, "verified": True}],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="guided-statement-test",
        model="test-model",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version="guided-statement-test",
        candidate_count=1,
        page_errors=0,
        candidates=[candidate],
        model="test-model",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=COORDINATOR)
    result = coordinate_statement(
        session,
        StatementCoordinationDraft(
            candidate_id=candidate.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            event_date=date(2026, 1, 16),
            description=quote,
            new_timing=StatementTiming.day("May 16th", date(2026, 5, 16)),
            previous_timing=None,
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
            scope=StatementScope.unknown(),
            internal_owner_roster_entry_id=roster.id,
            next_action="Confirm the revised completion plan with the party",
            action_due_date=date(2026, 2, 1),
            action_due_date_unknown_reason=None,
            milestone_impact=None,
            milestone_ids=(),
        ),
        principal=COORDINATOR,
    )
    return result, party


def test_commitment_change_reaches_the_coordinated_owner(session, project):
    from corridor.external_statements import (
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    owner = HumanPrincipal("local:doc-notify-commit-owner")
    result, party = _coordinate_commitment(session, project, owner)
    lineage_id = result.event.commitment_lineage_id
    successor = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="verbal",
        event_date=date(2026, 1, 20),
        description="Party now proposes June 1st.",
        new_timing=StatementTiming.day("June 1st", date(2026, 6, 1)),
        scope=StatementScope.unknown(),
        created_by="local:statement-coordinator",
        commitment_lineage_id=lineage_id,
    )
    assert session.get(CommitmentLineage, lineage_id).plan_needs_review is True

    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    statement_occurrences = [n for n in registered if n.subject_kind == "statement"]
    assert len(statement_occurrences) == 1
    occurrence = statement_occurrences[0]
    assert occurrence.category == dn.CATEGORY_DOCUMENT_CHANGE
    assert occurrence.commitment_lineage_id == lineage_id
    assert occurrence.recipient_principal_subject == owner.subject
    assert occurrence.statement_event_id == successor.id
    assert occurrence.reason_code == dn.REASON_FACTUAL_SUCCESSOR


def test_commitment_change_without_typed_owner_binding_is_a_visible_gap(session, project):
    # The plan a factual successor affects is discovered, but a name-only owner
    # (no guided-coordination roster binding) is never turned into a recipient.
    lineage_id, successor = _commitment_with_factual_successor(session, project)
    lineage = session.get(CommitmentLineage, lineage_id)
    assert lineage.plan_needs_review is True
    assert successor.supersedes_event_id is not None

    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    statement_occurrences = [n for n in registered if n.subject_kind == "statement"]
    assert statement_occurrences == []


# --- Reads are scoped and never leak across projects ----------------------


def test_reads_do_not_leak_across_projects(session, project):
    dependency, _c, approval_doc, _a = _affirmative_review(
        session, project, ref_code="DN-R1"
    )
    _roster(session, project, REVIEWER, display_name="Rae", email="rae@example.com")
    assignee_roster = _roster(
        session, project, ASSIGNEE, display_name="Dana", email="dana@example.com"
    )
    _assign_constraint(session, dependency, assignee_roster)
    _supersede_document(session, project, approval_doc, name="approval-DN-R1-b")
    dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )

    # A second project the assignee is not part of.
    other = Project(slug=f"docnotif-other-{uuid4().hex}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()

    inbox = dn.recipient_document_inbox(
        session, project_id=project.id, principal_subject=ASSIGNEE.subject
    )
    assert len(inbox) == 1
    assert inbox[0]["category"] == dn.CATEGORY_DOCUMENTATION_LOSS
    # The one project's operations view sees its deliveries; the other sees none.
    assert (
        dn.operations_document_notifications_view(session, project_id=other.id)["deliveries"]
        == []
    )
    view = dn.operations_document_notifications_view(session, project_id=project.id)
    assert view["delivery_enabled"] is False
    assert len(view["deliveries"]) == 2


def test_registration_refuses_an_empty_registrar(session, project):
    with pytest.raises(dn.DocumentNotificationRefusal):
        dn.register_project_document_notifications(
            session, project_id=project.id, registered_by="  "
        )


def test_registration_queues_a_dispatch_with_resolved_contact(session, project):
    dependency, _c, approval_doc, _a = _affirmative_review(
        session, project, ref_code="DN-D1"
    )
    _roster(session, project, REVIEWER, display_name="Rae", email="rae@example.com")
    _supersede_document(session, project, approval_doc, name="approval-DN-D1-b")
    registered = dn.register_project_document_notifications(
        session, project_id=project.id, registered_by=REGISTRAR
    )
    dispatch = session.scalar(
        select(DocumentNotificationDispatch).where(
            DocumentNotificationDispatch.notification_id == registered[0].id
        )
    )
    assert dispatch.delivery_state == "queued"
    assert dispatch.recipient_contact == "rae@example.com"
    assert dispatch.delivery_limitation is None
