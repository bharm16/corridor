import json
from copy import deepcopy
from datetime import date
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor.adjudicate import (
    InvalidCandidateScope,
    accept_candidate,
    edit_candidate,
    merge_candidate,
    reject_candidate,
)
from corridor.admission import load_project
from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DependencyEvidenceSufficiency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventTiming,
    DocPage,
    Document,
    EvidenceLink,
    EvidenceInvestigationCandidateReviewStart,
    ExternalReportArtifact,
    ExternalOrg,
    OrganizationIdentityReceipt,
    Project,
    ProjectRosterEntry,
    ReportRun,
    WorkDecision,
)
from corridor.operative_support import designate_publication_support
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import (
    create_revision_comparison,
    read_revision_comparison,
)
from corridor.supersession import SupersessionDeclaration, register_supersessions
from corridor.supersession_review import build_reviewer_worklist
from corridor.web.app import app, get_human_principal, get_session
from corridor.web.queue import build_view, next_candidate, pending_counts
from corridor.work_decisions import (
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    current_internal_owner_decision,
    current_next_action_decision,
)
from corridor.models import ProjectRosterEntry
from access_support import seed_membership

TEST_PRINCIPAL = HumanPrincipal("local:test-reviewer")
PLAN_ACTION = FOLLOW_UP_NEXT_ACTION_CHOICES[0]


def _plan_assignee(session, project, name="Dana Fields"):
    """One active roster identity behind the displayed assignee name."""
    entry = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.project_id == project.id,
            ProjectRosterEntry.display_name == name,
        )
    ).first()
    if entry is None:
        entry = ProjectRosterEntry(
            project_id=project.id,
            principal_subject=f"local:{name.lower().replace(' ', '-')}",
            display_name=name,
        )
        session.add(entry)
        session.flush()
    return entry


def _save_plan(
    client,
    session,
    project,
    dependency_id,
    *,
    owner="Dana Fields",
    next_action=PLAN_ACTION,
    due_date="",
    due_date_unknown_reason="",
    redirect_to="",
):
    """Save the whole roster-backed Follow-up Plan the way the form does."""
    entry = _plan_assignee(session, project, owner)
    owner_tail = current_internal_owner_decision(session, dependency_id)
    action_tail = current_next_action_decision(session, dependency_id)
    return client.post(
        f"/dependencies/{dependency_id}/plan",
        data={
            "slug": project.slug,
            "internal_owner_roster_entry_id": str(entry.id),
            "next_action": next_action,
            "action_due_date": due_date,
            "action_due_date_unknown_reason": due_date_unknown_reason,
            "expected_internal_owner_decision_id": (
                str(owner_tail.id) if owner_tail else ""
            ),
            "expected_next_action_decision_id": (
                str(action_tail.id) if action_tail else ""
            ),
            "redirect_to": redirect_to,
        },
        follow_redirects=False,
    )


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def client(session):
    """The app shares the test's transaction, so nothing is committed."""
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_without_session(session):
    """A client with no signed-in session, exercising the real fail-closed gate.

    Nothing overrides the identity dependency, so a request carries no session
    cookie and the real ``get_human_principal`` refuses it (#331).
    """
    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    p = Project(slug="web-test", name="Web Test", is_synthetic=True)
    session.add(p)
    session.flush()
    # The web fixtures exercise queue behavior after the registry has an
    # established organization.  ADR-0051 moves first-time organization
    # confirmation to its own explicit command; normal candidate acceptance
    # may no longer create this row as a side effect.
    session.add_all(
        [
            ExternalOrg(name="AT&T Texas (SWBT)", aliases=[]),
            ExternalOrg(name="City of Houston", aliases=[]),
            ExternalOrg(name="Web queue Pipeline Co", aliases=[]),
            ExternalOrg(name="Web Pipeline Co", aliases=[]),
        ]
    )
    session.flush()
    seed_membership(session, p, TEST_PRINCIPAL)
    return p


def _document_sha(project_id: int, value: str) -> str:
    return sha256(f"{project_id}:{value}".encode()).hexdigest()


@pytest.fixture
def document(session, project):
    d = Document(
        project_id=project.id,
        sha256=_document_sha(
            project.id, "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"
        ),
        filename="nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=28,
    )
    session.add(d)
    session.flush()
    session.add(
        DocPage(
            document_id=d.id,
            page_no=1,
            text="FOC1-1 GOOD-1 BAD-1 AT&T Texas (SWBT) 1149+00",
            image_path="/tmp/corridor-missing-page.png",
        )
    )
    session.flush()
    return d


def make_candidate(
    session,
    project,
    document,
    *,
    verified=True,
    uid="FOC1-1",
    station_from="1149+00",
    whole_row=True,
    unverified_fields=(),
    low_confidence_tokens=(),
    kind="dependency",
    prompt_version="txdot_ucm_v1",
    auto_active_run=True,
    model=None,
):
    c = Candidate(
        project_id=project.id,
        kind=kind,
        payload_json={
            "kind": kind,
            "fields": {
                "utility_id": uid,
                "external_org": "AT&T Texas (SWBT)",
                "station_from": station_from,
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": f"{uid} AT&T Texas (SWBT)",
                    "verified": verified,
                    "whole_row": whole_row,
                }
            ],
            "confidence": 1.0,
            "unverified_fields": list(unverified_fields),
            "low_confidence_tokens": list(low_confidence_tokens),
            "dedupe_hint": "x",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=prompt_version,
        citations_verified=(
            verified and not unverified_fields and not low_confidence_tokens
        ),
        model=model,
    )
    session.add(c)
    session.flush()

    if auto_active_run:
        run = record_extraction_run(
            session,
            document,
            prompt_version=prompt_version,
            candidate_count=1,
            page_errors=0,
            candidates=(c,),
            model=model,
            allow_unsealed_legacy=True,
        )
        declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)
    return c


def test_queue_shows_the_next_pending_candidate(client, session, project, document):
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "AT&amp;T Texas (SWBT)" in r.text
    assert "1149+00" in r.text


def test_confirming_an_organization_is_project_gated_and_records_the_source_choice(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    candidate.payload_json = {
        **candidate.payload_json,
        "fields": {
            **candidate.payload_json["fields"],
            "external_org": "AT&T Texas",
        },
    }
    session.flush()
    registered = session.scalar(
        select(ExternalOrg).where(ExternalOrg.name == "AT&T Texas (SWBT)")
    )

    response = client.post(
        f"/candidates/{candidate.id}/confirm-organization",
        data={
            "slug": project.slug,
            "external_org_id": str(registered.id),
            "facility_classes": ["Telecom"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    receipt = session.scalar(
        select(OrganizationIdentityReceipt).where(
            OrganizationIdentityReceipt.candidate_id == candidate.id
        )
    )
    assert receipt.external_org_id == registered.id
    assert receipt.recorded_by == TEST_PRINCIPAL.subject
    assert receipt.facility_classes_json == ["Telecom"]


def test_confirming_an_organization_requires_an_explicit_existing_or_new_choice(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    response = client.post(
        f"/candidates/{candidate.id}/confirm-organization",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_unresolved_identity_shows_one_card_with_evidence_and_no_preselection(
    client, session, project
):
    """ADR-0051: the abstained spelling gets its card — source wording, the
    evidence already considered, and explicit existing-or-new choices."""
    from corridor.dependency_admission import run_dependency_admission
    from corridor.extraction_runs import (
        declare_single_run_documents,
        record_extraction_run,
    )

    matrix = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, "identity-card.pdf"),
        filename="identity-card.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(matrix)
    session.flush()
    fields = {
        "utility_id": "PL9",
        "external_org": "Fresh Spelling Gas Partners",
        "utility_type": "Petroleum and Gaseous Materials",
        "station_from": "1102+20",
        "station_to": "1102+80",
    }
    quote = " | ".join(fields.values())
    session.add(DocPage(document_id=matrix.id, page_no=1, text=quote))
    row = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields,
            "citations": [
                {
                    "document_id": matrix.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": quote,
            "text_source": "text_layer",
        },
        source_document_id=matrix.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="matrix_v1",
        model="gpt-test",
        citations_verified=True,
    )
    session.add(row)
    record_extraction_run(
        session,
        matrix,
        prompt_version="matrix_v1",
        candidate_count=1,
        page_errors=0,
        candidates=(row,),
        model="gpt-test",
        schema_version="matrix_candidate_shape_v1",
        allow_unsealed_legacy=True,
    )
    session.flush()
    declare_single_run_documents(session, project.id, principal=TEST_PRINCIPAL)
    result = run_dependency_admission(session, project.id)
    assert {item.reason for item in result.abstentions} == {
        "external_org_identity_unresolved"
    }

    page = client.get(f"/queue/{project.slug}").text

    assert "External Organization identity not established." in page
    assert "Fresh Spelling Gas Partners" in page
    assert "Evidence already considered" in page
    # The empty choice is the only preselected option; no organization is.
    assert '<option value="" selected>' in page
    assert page.count(" selected>") == 1
    assert 'name="create_organization"' in page
    assert f'action="/candidates/{row.id}/confirm-organization"' in page


def test_queue_rejects_an_unknown_review_lane(client, project):
    r = client.get(f"/queue/{project.slug}?lane=not-a-review-lane")

    assert r.status_code == 422


def test_queue_exposes_two_counted_exclusive_review_lanes(client, project):
    r = client.get(f"/queue/{project.slug}")

    assert r.status_code == 200
    assert f'href="/queue/{project.slug}?lane=candidate" aria-current="page"' in r.text
    assert f'href="/queue/{project.slug}?lane=reconfirmation"' in r.text
    assert "Proposed constraints (0)" in r.text
    assert "Newer documents needing attention (0)" in r.text


def test_the_review_screen_states_what_is_left_and_reaches_the_record(
    client, session, project, document
):
    """The header is one line: what is left, and the way to the record.

    The lane bar is gone — a permanent row of counts for lanes the
    operator is not in was three labels competing with the one thing on
    the screen (#209).
    """
    make_candidate(session, project, document)

    r = client.get(f"/queue/{project.slug}?lane=candidate")

    assert r.status_code == 200
    assert "1 waiting for you" in r.text
    assert f'href="/ledger/{project.slug}"' in r.text
    assert "Proposed constraints (" not in r.text


def test_proposed_constraint_uses_current_labels_without_changing_source_fields(
    client, session, project, document
):
    page = session.scalar(
        select(DocPage).where(DocPage.document_id == document.id, DocPage.page_no == 1)
    )
    page.text = "Ready-Milestone-Evidence AT&T Texas (SWBT) 1149+00"
    candidate = make_candidate(
        session, project, document, uid="Ready-Milestone-Evidence"
    )

    page = client.get(f"/queue/{project.slug}")

    assert page.status_code == 200
    assert "Add constraint" in page.text
    assert "Do not add" in page.text
    assert "<dt>Organization</dt>" in page.text
    assert "Ready-Milestone-Evidence AT&amp;T Texas (SWBT)" in page.text
    assert 'name="field_utility_id" value="Ready-Milestone-Evidence"' in page.text
    assert f'action="/candidates/{candidate.id}/accept"' in page.text
    assert f'action="/candidates/{candidate.id}/reject"' in page.text


def _seed_waiting_supersession_review(session, project, predecessor):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-WAITING-REVISION",
        dep_type="utility_relocation",
        title="Telecom — waiting for current revision",
    )
    successor = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, "successor-awaiting-extraction.pdf"),
        filename="successor-awaiting-extraction.pdf",
        doc_type="matrix",
        parse_status="pending",
        pages=1,
        registry_id=f"web-successor-{predecessor.id}",
    )
    session.add_all([dependency, successor])
    session.flush()

    publication = EvidenceLink(
        dependency_id=dependency.id,
        document_id=predecessor.id,
        page_no=1,
        quote="FOC1-1 AT&T Texas (SWBT)",
        verified=True,
    )
    session.add(publication)
    session.flush()
    designate_publication_support(
        session,
        dependency.id,
        publication.id,
        principal=TEST_PRINCIPAL,
    )

    predecessor.registry_id = f"web-predecessor-{predecessor.id}"
    session.flush()
    # The successor states what it replaces, so it carries the cited page.
    session.add(
        DocPage(document_id=successor.id, page_no=1, text="Replaces the prior revision")
    )
    session.flush()
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=date.today(),
                source_registry_id=successor.registry_id,
                source_page=1,
            )
        ],
        project_id=project.id,
    )
    return dependency, successor


def test_candidate_lane_surfaces_dependency_work_while_extraction_is_pending(
    client, session, project, document
):
    dependency, successor = _seed_waiting_supersession_review(
        session, project, document
    )

    r = client.get(f"/queue/{project.slug}?lane=candidate")

    assert r.status_code == 200
    assert "Queue empty" not in r.text
    assert "Proposed constraints (1)" in r.text
    assert dependency.ref_code in r.text
    assert document.filename in r.text
    assert successor.filename in r.text
    assert "Awaiting extraction" in r.text
    assert "No successor extraction attempt exists" in r.text
    assert "Current supporting documents are unavailable" in r.text
    assert ">Reconfirm<" not in r.text


def test_candidate_lane_explains_a_durable_successor_extraction_failure(
    client, session, project, document
):
    dependency, successor = _seed_waiting_supersession_review(
        session, project, document
    )
    record_extraction_run(
        session,
        successor,
        prompt_version="txdot_ucm_v1",
        candidate_count=0,
        page_errors=1,
        outcome="failed",
        error_detail="page could not be read",
        allow_unsealed_legacy=True,
    )
    session.flush()

    r = client.get(f"/queue/{project.slug}?lane=candidate")

    assert r.status_code == 200
    assert dependency.ref_code in r.text
    assert "Extraction failed" in r.text
    assert "completed current document reading is required" in r.text
    assert "Current supporting documents are unavailable" in r.text
    assert ">Reconfirm<" not in r.text


def test_dependency_only_work_stays_visible_beside_an_ordinary_candidate(
    client, session, project, document
):
    dependency, _successor = _seed_waiting_supersession_review(
        session, project, document
    )
    current = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, "current-independent-matrix.pdf"),
        filename="current-independent-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(current)
    session.flush()
    session.add(
        DocPage(
            document_id=current.id,
            page_no=1,
            text="ORD-1 AT&T Texas (SWBT) 1149+00",
        )
    )
    session.flush()
    make_candidate(session, project, current, uid="ORD-1")

    r = client.get(f"/queue/{project.slug}?lane=candidate")

    assert r.status_code == 200
    assert "ORD-1" in r.text
    assert dependency.ref_code in r.text
    assert "Awaiting extraction" in r.text


def test_empty_reconfirmation_lane_points_to_remaining_candidate_work(
    client, session, project, document
):
    make_candidate(session, project, document)

    r = client.get(f"/queue/{project.slug}?lane=reconfirmation")

    assert r.status_code == 200
    assert "Queue empty" in r.text
    assert "Proposed constraints (1)" in r.text
    assert (
        f'href="/queue/{project.slug}?lane=reconfirmation" '
        'aria-current="page"' in r.text
    )
    assert "1 proposed constraint item(s) remain" in r.text
    assert "AT&amp;T Texas (SWBT)" not in r.text


def test_unverified_candidates_sink_but_are_never_hidden(
    client, session, project, document
):
    """A quote that could not be found is a signal, not noise."""
    bad = make_candidate(
        session,
        project,
        document,
        verified=False,
        uid="BAD-1",
        auto_active_run=False,
    )
    good = make_candidate(
        session,
        project,
        document,
        verified=True,
        uid="GOOD-1",
        auto_active_run=False,
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version=bad.prompt_version,
        candidate_count=2,
        page_errors=0,
        candidates=(bad, good),
        model=bad.model,
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)

    assert next_candidate(session, project.id).id == good.id

    good.state = "accepted"
    session.flush()
    assert next_candidate(session, project.id).id == bad.id

    r = client.get(f"/queue/{project.slug}")
    assert "Source passage check failed" in r.text


def test_editing_a_whole_row_candidate_updates_queue_counts_and_order(
    session, project, document
):
    page = session.scalars(
        select(DocPage).where(DocPage.document_id == document.id, DocPage.page_no == 1)
    ).one()
    page.text = "BAD-1 GOOD-1 AT&T Texas (SWBT) 1149+00"
    session.flush()

    bad = make_candidate(
        session,
        project,
        document,
        uid="BAD-1",
        station_from="1092+00",
        unverified_fields=["station_from"],
        low_confidence_tokens=["1092"],
        auto_active_run=False,
    )
    good = make_candidate(
        session,
        project,
        document,
        uid="GOOD-1",
        auto_active_run=False,
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version=bad.prompt_version,
        candidate_count=2,
        page_errors=0,
        candidates=(bad, good),
        model=bad.model,
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)

    assert pending_counts(session, project.id) == (2, 1)
    assert next_candidate(session, project.id).id == good.id

    edit_candidate(
        session,
        bad,
        {
            "utility_id": "BAD-1",
            "external_org": "AT&T Texas (SWBT)",
            "station_from": "1149+00",
        },
        principal=TEST_PRINCIPAL,
    )

    assert pending_counts(session, project.id) == (2, 2)
    assert next_candidate(session, project.id).id == bad.id

    view = build_view(session, bad)
    assert view.citations_verified is True
    assert view.unverified_fields == []
    assert view.low_confidence_tokens == []


def test_only_an_unverified_citation_is_announced(client, session, project, document):
    """Verified is the default, so it is not news (#209).

    A green banner on every row spends the reader's attention on the
    absence of news. The failure keeps its banner, because that is the
    one a reviewer must act on.
    """
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}?mode=review")
    assert "Citation verified" not in r.text
    assert "Source passage check failed" not in r.text

    unverified = make_candidate(
        session, project, document, uid="FOC2-2", verified=False
    )
    r = client.get(f"/queue/{project.slug}?mode=review&candidate_id={unverified.id}")
    assert "Source passage check failed" in r.text


def test_merge_is_unavailable_when_there_is_nothing_to_merge_into(
    client, session, project, document
):
    """No existing dependency for this party means no merge, not a bad one."""
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}?mode=review")
    assert "disabled" in r.text
    assert "Nothing to merge into" in r.text


def test_merge_suggestions_appear_once_a_dependency_exists(
    client, session, project, document
):
    """The same facility, seen again in a later revision.

    This staged both candidates on one document until #46. A matrix lists
    each facility once, so that pair could only ever have been two
    facilities — the merge case needs a second revision to be real.
    """
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    later = Document(
        project_id=project.id,
        sha256=_document_sha(
            project.id, "nhhip-seg3c2-utilities-inventory-4-30-2026.pdf"
        ),
        filename="nhhip-seg3c2-utilities-inventory-4-30-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=28,
    )
    session.add(later)
    session.flush()
    make_candidate(session, project, later, uid="FOC1-1")

    r = client.get(f"/queue/{project.slug}?mode=review")
    assert "DEP-00001" in r.text
    # The reason is visible, not just the ranking.
    assert "station" in r.text and "text" in r.text


def _document_with_registry_id(session, project, *, registry_id, filename, doc_date):
    document = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, f"{registry_id}:{filename}"),
        filename=filename,
        doc_type="matrix",
        parse_status="parsed",
        doc_date=doc_date,
        pages=3,
        registry_id=str(registry_id),
    )
    session.add(document)
    session.flush()
    return document


def _supersession_module():
    return __import__("corridor.supersession", fromlist=["*"])


def _seed_supersession_chain(
    session,
    project,
    *,
    predecessor_registry_id="RID-100",
    successor_registry_id="RID-101",
    predecessor_uid="PRE-ONLY",
    successor_uid="SUCC-ONLY",
    include_successor_candidate=True,
    successor_failed=False,
    register=True,
):
    supersession = _supersession_module()
    Declaration = supersession.SupersessionDeclaration

    source_pointer = _document_with_registry_id(
        session,
        project,
        registry_id="RID-INDEX",
        filename="rid-index.xlsx",
        doc_date=date(2010, 1, 1),
    )
    session.add_all(
        [
            DocPage(document_id=source_pointer.id, page_no=1, text="RID index page 1"),
            DocPage(document_id=source_pointer.id, page_no=4, text="RID index page 4"),
        ]
    )

    predecessor = _document_with_registry_id(
        session,
        project,
        registry_id=predecessor_registry_id,
        filename="rev-01.pdf",
        doc_date=date(2025, 10, 1),
    )
    successor = _document_with_registry_id(
        session,
        project,
        registry_id=successor_registry_id,
        filename="rev-02.pdf",
        doc_date=date(2026, 1, 1),
    )
    session.add_all(
        [
            DocPage(
                document_id=predecessor.id,
                page_no=1,
                text=f"{predecessor_uid} AT&T Texas (SWBT)",
            ),
            DocPage(
                document_id=successor.id,
                page_no=1,
                text=f"{successor_uid} AT&T Texas (SWBT)",
            ),
        ]
    )
    session.flush()

    predecessor_candidate = make_candidate(
        session,
        project,
        predecessor,
        uid=predecessor_uid,
        model="gpt-4o-mini",
        auto_active_run=False,
    )
    predecessor_run = record_extraction_run(
        session,
        predecessor,
        prompt_version="txdot_ucm_v1",
        candidate_count=1,
        page_errors=0,
        candidates=(predecessor_candidate,),
        model="gpt-4o-mini",
        allow_unsealed_legacy=True,
    )
    declare_active_run(
        session, predecessor.id, predecessor_run.id, principal=TEST_PRINCIPAL
    )

    successor_candidate = None
    if successor_failed:
        assert not include_successor_candidate
        successor_run = record_extraction_run(
            session,
            successor,
            prompt_version="txdot_ucm_v1",
            candidate_count=0,
            page_errors=1,
            outcome="failed",
            model="gpt-4o-mini",
            error_detail="page extraction failed",
            allow_unsealed_legacy=True,
        )
    elif include_successor_candidate:
        successor_candidate = make_candidate(
            session,
            project,
            successor,
            uid=successor_uid,
            model="gpt-4o-mini",
            auto_active_run=False,
        )
        successor_run = record_extraction_run(
            session,
            successor,
            prompt_version="txdot_ucm_v1",
            candidate_count=1,
            page_errors=0,
            candidates=(successor_candidate,),
            model="gpt-4o-mini",
            allow_unsealed_legacy=True,
        )
    else:
        successor_run = record_extraction_run(
            session,
            successor,
            prompt_version="txdot_ucm_v1",
            candidate_count=0,
            page_errors=0,
            model="gpt-4o-mini",
            allow_unsealed_legacy=True,
        )

    declaration = Declaration(
        predecessor_registry_id=predecessor.registry_id,
        successor_registry_id=successor.registry_id,
        replacement_date=date(2026, 2, 13),
        source_registry_id=source_pointer.registry_id,
        source_page=4,
    )

    def register_supersession():
        return supersession.register_supersessions(session, [declaration])

    if register:
        register_supersession()

    return {
        "predecessor": predecessor,
        "successor": successor,
        "predecessor_candidate": predecessor_candidate,
        "successor_candidate": successor_candidate
        if include_successor_candidate
        else None,
        "successor_run": successor_run,
        "register_supersession": register_supersession,
        "activate_successor": lambda: declare_active_run(
            session, successor.id, successor_run.id, principal=TEST_PRINCIPAL
        ),
    }


def _seed_reconfirmation_ready_chain(session, project):
    chain = _seed_supersession_chain(
        session,
        project,
        predecessor_uid="FOC1-1",
        successor_uid="FOC1-1",
    )
    accept_candidate(
        session,
        chain["predecessor_candidate"],
        principal=TEST_PRINCIPAL,
        historical_document_id=chain["predecessor"].id,
    )
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    publication = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    designate_publication_support(
        session,
        dependency.id,
        publication.id,
        principal=TEST_PRINCIPAL,
    )
    mark_satisfies(
        session,
        dependency.id,
        publication.id,
        principal=TEST_PRINCIPAL,
    )
    chain["activate_successor"]()
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=chain["predecessor_candidate"].extraction_run_id,
        successor_extraction_run_id=chain["successor_run"].id,
        matcher_version="revision-correspondence-v2",
    )
    [finding] = read_revision_comparison(session, comparison.id).findings
    [review] = build_reviewer_worklist(session, project.id).reconfirmation
    chain["dependency"] = dependency
    chain["comparison"] = comparison
    chain["finding"] = finding
    chain["review"] = review
    return chain


def test_queue_selection_changes_immediately_when_successor_is_registered(
    client, session, project
):
    chain = _seed_supersession_chain(session, project, register=False)

    before = client.get(f"/queue/{project.slug}")
    assert before.status_code == 200
    assert "PRE-ONLY" in before.text

    chain["register_supersession"]()

    after = client.get(f"/queue/{project.slug}")
    assert after.status_code == 200
    assert "Queue empty" in after.text
    assert "PRE-ONLY" not in after.text


def test_queue_hides_predecessor_until_a_successor_active_run_is_declared(
    client, session, project
):
    _seed_supersession_chain(session, project)
    r = client.get(f"/queue/{project.slug}?mode=review")
    assert r.status_code == 200
    assert "Queue empty" in r.text


def test_queue_stays_empty_when_successor_extraction_failed(client, session, project):
    _seed_supersession_chain(
        session,
        project,
        include_successor_candidate=False,
        successor_failed=True,
    )

    r = client.get(f"/queue/{project.slug}?mode=review")
    assert r.status_code == 200
    assert "Queue empty" in r.text
    assert "PRE-ONLY" not in r.text
    assert "Every candidate" not in r.text


def test_queue_selects_successor_when_active_run_is_declared(client, session, project):
    chain = _seed_supersession_chain(session, project)
    chain["activate_successor"]()
    r = client.get(f"/queue/{project.slug}?mode=review")
    assert r.status_code == 200
    assert "SUCC-ONLY" in r.text
    assert "PRE-ONLY" not in r.text


def test_safe_unchanged_successor_moves_out_of_candidate_lane(client, session, project):
    chain = _seed_reconfirmation_ready_chain(session, project)

    candidate_lane = client.get(f"/queue/{project.slug}?lane=candidate")
    reconfirm_lane = client.get(f"/queue/{project.slug}?lane=reconfirmation")

    assert candidate_lane.status_code == 200
    assert "FOC1-1" not in candidate_lane.text
    assert "Queue empty" in candidate_lane.text
    assert "Proposed constraints (0)" in candidate_lane.text
    assert "Newer documents needing attention (1)" in candidate_lane.text
    assert reconfirm_lane.status_code == 200
    body = reconfirm_lane.text
    assert chain["dependency"].ref_code in body
    assert chain["predecessor"].filename in body
    assert chain["successor"].filename in body
    assert "The source wording is unchanged" in body
    assert "<kbd>c</kbd> Confirm replacement supporting document" in body
    assert f'action="/supersession-review/{chain["dependency"].id}/reconfirm"' in body
    assert f'name="predecessor_document_id" value="{chain["predecessor"].id}"' in body
    assert (
        f'name="successor_candidate_id" value="{chain["successor_candidate"].id}"'
        in body
    )
    assert f'name="comparison_id" value="{chain["comparison"].id}"' in body
    assert 'name="finding_id" value="' in body
    assert 'name="scope_fingerprint" value=' in body
    assert ">accept<" not in body
    assert ">reject<" not in body


def test_queue_keeps_automatic_carry_forward_in_the_audit_layer(
    client, session, project
):
    _seed_reconfirmation_ready_chain(session, project)
    response = client.get(f"/queue/{project.slug}?lane=reconfirmation")

    assert response.status_code == 200
    assert "Automatic Carry-Forward" not in response.text
    assert "automatic-carry-forward-v2" not in response.text
    assert "automatic-carry-forward-abstentions-v1" not in response.text


def test_queue_hides_versioned_automatic_abstention_counts(client, project):
    response = client.get(f"/queue/{project.slug}")

    assert response.status_code == 200
    assert "3 comparison changed" not in response.text
    assert "4 carried" not in response.text
    assert "automatic-carry-forward-abstentions-v1" not in response.text


def test_direct_post_cannot_admit_a_reconfirmation_only_candidate(
    client, session, project
):
    chain = _seed_reconfirmation_ready_chain(session, project)

    response = client.post(
        f"/candidates/{chain['successor_candidate'].id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert chain["successor_candidate"].state == "pending"


def test_reconfirmation_post_moves_support_and_redirects_back_to_that_lane(
    client, session, project
):
    chain = _seed_reconfirmation_ready_chain(session, project)
    before = client.get(f"/ledger/{project.slug}/{chain['dependency'].id}")
    assert "Supporting document replaced" in before.text

    response = client.post(
        f"/supersession-review/{chain['dependency'].id}/reconfirm",
        data={
            "slug": project.slug,
            "predecessor_document_id": str(chain["predecessor"].id),
            "successor_candidate_id": str(chain["successor_candidate"].id),
            "comparison_id": str(chain["comparison"].id),
            "finding_id": str(chain["finding"].id),
            "scope_fingerprint": json.dumps(
                [list(item) for item in chain["review"].scope_fingerprint]
            ),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/queue/{project.slug}?lane=reconfirmation"
    refreshed = client.get(f"/queue/{project.slug}?lane=reconfirmation")
    assert "Queue empty" in refreshed.text
    ordinary_after = client.get(f"/queue/{project.slug}?lane=candidate")
    assert "Queue empty" in ordinary_after.text
    assert "Proposed constraints (0)" in ordinary_after.text
    assert "FOC1-1" not in ordinary_after.text
    detail = client.get(f"/ledger/{project.slug}/{chain['dependency'].id}")
    assert "Supporting document replaced" not in detail.text
    assert chain["predecessor"].filename in detail.text
    assert chain["successor"].filename in detail.text
    assert "reconfirm_operative_support" in detail.text


def test_stale_reconfirmation_form_is_409_and_leaves_review_work_open(
    client, session, project
):
    chain = _seed_reconfirmation_ready_chain(session, project)

    response = client.post(
        f"/supersession-review/{chain['dependency'].id}/reconfirm",
        data={
            "slug": project.slug,
            "predecessor_document_id": str(chain["predecessor"].id),
            "successor_candidate_id": str(chain["successor_candidate"].id),
            "comparison_id": str(chain["comparison"].id),
            "finding_id": str(chain["finding"].id + 1_000_000),
            "scope_fingerprint": json.dumps(
                [list(item) for item in chain["review"].scope_fingerprint]
            ),
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "stale or no longer safe" in response.json()["detail"]
    review = client.get(f"/queue/{project.slug}?lane=reconfirmation")
    assert "Confirm replacement supporting document" in review.text
    detail = client.get(f"/ledger/{project.slug}/{chain['dependency'].id}")
    assert "Supporting document replaced" in detail.text
    assert chain["predecessor"].filename in detail.text
    assert chain["successor"].filename not in detail.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("predecessor_document_id", "not-an-id"),
        ("successor_candidate_id", ""),
        ("comparison_id", "0"),
        ("finding_id", "-1"),
    ],
)
def test_malformed_reconfirmation_identity_is_400(
    client, session, project, field, value
):
    chain = _seed_reconfirmation_ready_chain(session, project)
    data = {
        "slug": project.slug,
        "predecessor_document_id": str(chain["predecessor"].id),
        "successor_candidate_id": str(chain["successor_candidate"].id),
        "comparison_id": str(chain["comparison"].id),
        "finding_id": str(chain["finding"].id),
        "scope_fingerprint": json.dumps(
            [list(item) for item in chain["review"].scope_fingerprint]
        ),
    }
    data[field] = value

    response = client.post(
        f"/supersession-review/{chain['dependency'].id}/reconfirm",
        data=data,
        follow_redirects=False,
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("raw_scope_fingerprint", "expected_detail"),
    [
        ("", "scope_fingerprint must be present"),
        ("not-json", "scope_fingerprint must be valid JSON"),
    ],
)
def test_missing_or_malformed_scope_fingerprint_is_400(
    client, session, project, raw_scope_fingerprint, expected_detail
):
    chain = _seed_reconfirmation_ready_chain(session, project)

    response = client.post(
        f"/supersession-review/{chain['dependency'].id}/reconfirm",
        data={
            "slug": project.slug,
            "predecessor_document_id": str(chain["predecessor"].id),
            "successor_candidate_id": str(chain["successor_candidate"].id),
            "comparison_id": str(chain["comparison"].id),
            "finding_id": str(chain["finding"].id),
            "scope_fingerprint": raw_scope_fingerprint,
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == expected_detail
    assert chain["successor_candidate"].state == "pending"


def test_queue_uses_the_declared_successor_run_not_a_newer_experiment(
    client, session, project
):
    chain = _seed_supersession_chain(session, project)
    experimental = make_candidate(
        session,
        project,
        chain["successor"],
        uid="EXPERIMENTAL",
        prompt_version="txdot_ucm_experiment",
        model="gpt-4o-mini",
        auto_active_run=False,
    )
    record_extraction_run(
        session,
        chain["successor"],
        prompt_version="txdot_ucm_experiment",
        candidate_count=1,
        page_errors=0,
        candidates=(experimental,),
        model="gpt-4o-mini",
        allow_unsealed_legacy=True,
    )
    chain["activate_successor"]()

    r = client.get(f"/queue/{project.slug}?mode=review")
    assert r.status_code == 200
    assert "SUCC-ONLY" in r.text
    assert "EXPERIMENTAL" not in r.text


def test_queue_prefers_historical_document_when_override_is_set(
    client, session, project
):
    chain = _seed_supersession_chain(session, project)
    chain["activate_successor"]()
    active = client.get(f"/queue/{project.slug}?mode=review")
    assert "SUCC-ONLY" in active.text
    assert "PRE-ONLY" not in active.text

    r = client.get(
        f"/queue/{project.slug}?historical_document_id={chain['predecessor'].id}"
    )
    assert "PRE-ONLY" in r.text
    assert "Queue empty" not in r.text
    assert 'name="historical_document_id"' in r.text
    assert f'value="{chain["predecessor"].id}"' in r.text


@pytest.mark.parametrize(
    ("action", "extra_form"),
    [
        ("accept", {}),
        ("edit-accept", {}),
        ("merge", {"dependency_id": "1"}),
        ("reject", {"reason": "wrong"}),
    ],
)
def test_direct_post_cannot_mutate_a_historical_candidate_by_default(
    client, session, project, action, extra_form
):
    chain = _seed_supersession_chain(session, project)

    response = client.post(
        f"/candidates/{chain['predecessor_candidate'].id}/{action}",
        data={"slug": project.slug, **extra_form},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert chain["predecessor_candidate"].state == "pending"


def test_authoritative_mutation_apis_reject_historical_candidate_bypass(
    session, project
):
    chain = _seed_supersession_chain(session, project)
    candidate = chain["predecessor_candidate"]
    target = Dependency(
        project_id=project.id,
        ref_code="DEP-SCOPE",
        dep_type="utility_relocation",
        title="Scope target",
    )
    session.add(target)
    session.flush()

    mutations = (
        lambda: accept_candidate(session, candidate, principal=TEST_PRINCIPAL),
        lambda: edit_candidate(
            session,
            candidate,
            {"utility_id": "PRE-EDIT"},
            principal=TEST_PRINCIPAL,
        ),
        lambda: merge_candidate(
            session,
            candidate,
            target,
            principal=TEST_PRINCIPAL,
        ),
        lambda: reject_candidate(
            session,
            candidate,
            "wrong",
            principal=TEST_PRINCIPAL,
        ),
    )

    for mutate in mutations:
        with pytest.raises(InvalidCandidateScope):
            mutate()
        assert candidate.state == "pending"


def test_authoritative_mutation_api_rejects_an_inactive_successor_run(session, project):
    chain = _seed_supersession_chain(session, project)

    with pytest.raises(InvalidCandidateScope):
        accept_candidate(
            session,
            chain["successor_candidate"],
            principal=TEST_PRINCIPAL,
        )


def test_explicit_historical_override_makes_that_exact_candidate_actionable(
    client, session, project
):
    chain = _seed_supersession_chain(session, project)

    response = client.post(
        f"/candidates/{chain['predecessor_candidate'].id}/accept",
        data={
            "slug": project.slug,
            "historical_document_id": str(chain["predecessor"].id),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert chain["predecessor_candidate"].state == "accepted"
    assert (
        f"historical_document_id={chain['predecessor'].id}"
        in response.headers["location"]
    )


def test_zero_row_successor_run_does_not_fall_back_to_predecessor_by_default(
    client, session, project
):
    chain = _seed_supersession_chain(
        session, project, include_successor_candidate=False
    )
    chain["activate_successor"]()

    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "Queue empty" in r.text
    assert "PRE-ONLY" not in r.text


def test_a_sibling_row_of_the_same_matrix_is_not_suggested(
    client, session, project, document
):
    """#46: 94 of 96 AT&T rows drew a suggestion against their own siblings."""
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    make_candidate(session, project, document, uid="FOC1-2")

    r = client.get(f"/queue/{project.slug}")
    assert "DEP-00001" not in r.text
    assert "Nothing to merge into" in r.text


def test_merging_from_the_queue_adds_to_the_existing_dependency(
    client, session, project, document
):
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    target = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()

    second = make_candidate(session, project, document, uid="FOC1-2")
    r = client.post(
        f"/candidates/{second.id}/merge",
        data={"slug": project.slug, "dependency_id": target.id},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert second.state == "merged"
    assert second.merged_into == target.id
    # Still one dependency: merging must not create a second.
    assert (
        len(
            session.scalars(
                select(Dependency).where(Dependency.project_id == project.id)
            ).all()
        )
        == 1
    )


def test_merging_into_another_projects_dependency_is_refused(
    client, session, project, document
):
    """Two ledgers that were never the same thing must not be joined."""
    other = Project(slug="web-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray = Dependency(
        project_id=other.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="unrelated",
    )
    session.add(stray)
    session.flush()

    candidate = make_candidate(session, project, document)
    r = client.post(
        f"/candidates/{candidate.id}/merge",
        data={"slug": project.slug, "dependency_id": stray.id},
        follow_redirects=False,
    )
    assert r.status_code == 404
    assert candidate.state == "pending"


def test_accepting_malformed_citations_returns_400_without_writes(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    candidate.payload_json = {**candidate.payload_json, "citations": {"page": 1}}
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_non_mapping_payload_returns_400_without_writes(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    candidate.payload_json = []
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_non_mapping_fields_returns_400_without_writes(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    candidate.payload_json = {**candidate.payload_json, "fields": []}
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_merging_malformed_citations_returns_400_without_writes(
    client, session, project, document
):
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    target = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    candidate = make_candidate(session, project, document, uid="FOC1-2")
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                key: value
                for key, value in candidate.payload_json["citations"][0].items()
                if key != "quote"
            }
        ],
    }
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/merge",
        data={"slug": project.slug, "dependency_id": target.id},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


@pytest.mark.parametrize(
    "payload_fields",
    [{}, {"utility_id": "FOC1-1", "external_org": "AT&T Texas (SWBT)"}],
    ids=["no-fields", "fields"],
)
def test_accepting_a_candidate_that_cites_nothing_returns_400_without_writes(
    client, session, project, document, payload_fields
):
    """The queue's 303 back to the queue was the whole of the feedback.

    A reviewer pressing accept on a citation-less card was redirected as
    though it had worked, and an evidence-free Dependency was committed
    behind them. With fields it was a 500 instead — the route has to
    answer 400 to both.
    """
    candidate = make_candidate(session, project, document)
    candidate.payload_json = {
        **candidate.payload_json,
        "fields": payload_fields,
        "citations": [],
    }
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


@pytest.mark.parametrize(
    "payload_fields",
    [{}, {"utility_id": "FOC1-2", "external_org": "AT&T Texas (SWBT)"}],
    ids=["no-fields", "fields"],
)
def test_merging_a_candidate_that_cites_nothing_returns_400_without_writes(
    client, session, project, document, payload_fields
):
    """And the Candidate stays in the queue instead of leaving it merged."""
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    target = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    candidate = make_candidate(session, project, document, uid="FOC1-2")
    candidate.payload_json = {
        **candidate.payload_json,
        "fields": payload_fields,
        "citations": [],
    }
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/merge",
        data={"slug": project.slug, "dependency_id": target.id},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_merging_non_integer_citation_document_id_returns_400_without_writes(
    client, session, project, document
):
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    target = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    candidate = make_candidate(session, project, document, uid="FOC1-2")
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                **candidate.payload_json["citations"][0],
                "document_id": {"id": document.id},
            }
        ],
    }
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/merge",
        data={"slug": project.slug, "dependency_id": target.id},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_merging_non_integer_cited_page_returns_400_without_writes(
    client, session, project, document
):
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    target = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    candidate = make_candidate(session, project, document, uid="FOC1-2")
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                **candidate.payload_json["citations"][0],
                "page": {"page": 1},
            }
        ],
    }
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/merge",
        data={"slug": project.slug, "dependency_id": target.id},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_another_projects_candidate_is_hidden_and_refused(
    client, session, project, document
):
    other = Project(slug="web-accept-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray_doc = Document(
        project_id=other.id,
        sha256="f" * 64,
        filename="other-accept.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(stray_doc)
    session.flush()
    candidate = make_candidate(session, other, stray_doc)
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert candidate.state == "pending"
    assert (
        session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        ).all()
        == []
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_rejecting_another_projects_candidate_is_hidden_and_refused(
    client, session, project, document
):
    other = Project(slug="web-reject-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray_doc = Document(
        project_id=other.id,
        sha256="g" * 64,
        filename="other-reject.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(stray_doc)
    session.flush()
    candidate = make_candidate(session, other, stray_doc)
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/reject",
        data={"slug": project.slug, "reason": "duplicate"},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert candidate.state == "pending"
    assert (
        session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        ).all()
        == []
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_edit_accepting_another_projects_candidate_is_hidden_and_refused(
    client, session, project, document
):
    other = Project(slug="web-edit-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray_doc = Document(
        project_id=other.id,
        sha256="h" * 64,
        filename="other-edit.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(stray_doc)
    session.flush()
    candidate = make_candidate(session, other, stray_doc)
    original = dict(candidate.payload_json["fields"])
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client.post(
        f"/candidates/{candidate.id}/edit-accept",
        data={
            "slug": project.slug,
            "field_utility_id": "FOC9-9",
            "field_external_org": "AT&T Texas (SWBT)",
            "field_station_from": "1150+00",
        },
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert candidate.state == "pending"
    assert candidate.payload_json["fields"] == original
    assert (
        session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        ).all()
        == []
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_marking_evidence_on_another_projects_dependency_is_hidden_and_refused(
    client, session, project, document
):
    other = Project(slug="web-evidence-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray_doc = Document(
        project_id=other.id,
        sha256="i" * 64,
        filename="other-evidence.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(stray_doc)
    session.flush()
    session.add(
        DocPage(
            document_id=stray_doc.id,
            page_no=1,
            text="FOC1-1 AT&T Texas (SWBT) 1149+00",
        )
    )
    session.flush()
    candidate = make_candidate(session, other, stray_doc)
    # The acting principal is not a member of `other`, so it could never build
    # this through the app; create the dependency and evidence directly so the
    # test can prove the member-slug route still refuses to reach across.
    accept_candidate(session, candidate, principal=TEST_PRINCIPAL)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == other.id)
    ).one()
    evidence = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()

    before_audit = session.scalar(select(func.count()).select_from(AuditLog))
    mark = client.post(
        f"/dependencies/{dependency.id}/evidence/{evidence.id}/satisfies",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert mark.status_code == 404
    assert (
        session.scalar(
            select(func.count())
            .select_from(DependencyEvidenceSufficiency)
            .where(
                DependencyEvidenceSufficiency.dependency_id == dependency.id,
                DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
                DependencyEvidenceSufficiency.scope_link_id.is_(None),
            )
        )
        == 0
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_creates_a_dependency_and_advances(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    r = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/work/{project.slug}"
    assert candidate.state == "accepted"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all()


@pytest.mark.parametrize("action", ["accept", "edit-accept", "merge", "reject"])
def test_mutation_routes_refuse_without_a_signed_in_session_and_write_nothing(
    client_without_session, session, project, document, action
):
    candidate = make_candidate(session, project, document)
    original_payload = deepcopy(candidate.payload_json)
    data = {"slug": project.slug}
    if action == "edit-accept":
        data["field_station_from"] = "1150+00"
    elif action == "merge":
        target = Dependency(
            project_id=project.id,
            ref_code="DEP-00001",
            dep_type="utility_relocation",
            title="Existing dependency",
        )
        session.add(target)
        session.flush()
        data["dependency_id"] = target.id
    elif action == "reject":
        data["reason"] = "duplicate"

    before = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in (Dependency, Assertion, EvidenceLink, AuditLog)
    }

    response = client_without_session.post(
        f"/candidates/{candidate.id}/{action}",
        data=data,
        follow_redirects=False,
    )

    assert response.status_code == 401
    session.refresh(candidate)
    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert candidate.payload_json == original_payload
    assert {
        model: session.scalar(select(func.count()).select_from(model))
        for model in before
    } == before


def test_mark_satisfies_route_refuses_without_a_signed_in_session(
    client_without_session, session, project, document
):
    candidate = make_candidate(session, project, document)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).first()
    assert dependency is None

    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    try:
        accepted = client_without_session.post(
            f"/candidates/{candidate.id}/accept",
            data={"slug": project.slug},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_human_principal, None)
    assert accepted.status_code == 303

    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    evidence = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    response = client_without_session.post(
        f"/dependencies/{dependency.id}/evidence/{evidence.id}/satisfies",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert (
        session.scalar(
            select(func.count())
            .select_from(DependencyEvidenceSufficiency)
            .where(
                DependencyEvidenceSufficiency.dependency_id == dependency.id,
                DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
                DependencyEvidenceSufficiency.scope_link_id.is_(None),
            )
        )
        == 0
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_edit_then_accept_records_the_edited_values(client, session, project, document):
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/edit-accept",
        data={
            "slug": project.slug,
            "field_utility_id": "FOC1-1",
            "field_external_org": "AT&T Texas (SWBT)",
            "field_station_from": "1150+00",
        },
        follow_redirects=False,
    )
    dep = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    station = session.scalars(
        select(Assertion).where(
            Assertion.dependency_id == dep.id, Assertion.field_name == "station_from"
        )
    ).one()
    assert station.asserted_value == "1150+00"


@pytest.mark.parametrize(
    "form_fields",
    [
        {},
        {"field_utility_id": "", "field_external_org": "   "},
    ],
    ids=["boxes-absent", "boxes-blanked"],
)
def test_edit_accepting_with_every_field_box_cleared_is_refused(
    client, session, project, document, form_fields
):
    """The live path, and the only one of these defects a reviewer can reach.

    `edit_accept` keeps only `field_*` inputs that still hold a value, so
    clearing the boxes sends `fields = {}` — and acceptance committed a
    Dependency with a real verified EvidenceLink, zero Assertions, and
    `_title` calling it "Utility" because there was no utility_type left
    to name it by. Evidence for nothing, at 303 back to the queue.
    """
    candidate = make_candidate(session, project, document)
    before_dependencies = session.scalar(select(func.count()).select_from(Dependency))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))

    response = client.post(
        f"/candidates/{candidate.id}/edit-accept",
        data={"slug": project.slug, **form_fields},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    # On the Ledger, not on the payload. `edit_accept` is one act: the edit
    # is written and flushed before acceptance refuses it, and the route's
    # session never commits on that path, so production takes the whole
    # thing back. These tests share the caller's session, which cannot
    # tell a flush-then-rollback from a never-write — so they assert the
    # thing both harnesses agree on.
    assert (
        session.scalar(select(func.count()).select_from(Dependency))
        == before_dependencies
    )
    assert (
        session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    )
    assert (
        session.scalar(select(func.count()).select_from(EvidenceLink))
        == before_evidence
    )


def test_an_edit_is_audited_against_the_original_extraction(
    client, session, project, document
):
    """What the extractor said must survive the reviewer changing it."""
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/edit-accept",
        data={
            "slug": project.slug,
            "field_utility_id": "FOC1-1",
            "field_external_org": "AT&T Texas (SWBT)",
            "field_station_from": "1150+00",
        },
        follow_redirects=False,
    )
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "edit_candidate", AuditLog.entity_id == candidate.id
        )
    ).one()
    assert entry.before_json["fields"]["station_from"] == "1149+00"
    assert entry.after_json["fields"]["station_from"] == "1150+00"


def test_rejecting_records_a_reason_and_creates_no_dependency(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/reject",
        data={"slug": project.slug, "reason": "duplicate"},
        follow_redirects=False,
    )
    assert candidate.state == "rejected"
    assert not session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all()
    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "reject_candidate")
    ).one()
    assert entry.after_json["reason"] == "duplicate"


def test_an_unknown_reject_reason_is_refused(client, session, project, document):
    candidate = make_candidate(session, project, document)
    r = client.post(
        f"/candidates/{candidate.id}/reject",
        data={"slug": project.slug, "reason": "because"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert candidate.state == "pending"


def test_adjudicating_twice_is_refused(client, session, project, document):
    """Two tabs or a double submit would create two Dependencies from one row."""
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    again = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert again.status_code == 409
    assert (
        len(
            session.scalars(
                select(Dependency).where(Dependency.project_id == project.id)
            ).all()
        )
        == 1
    )


def test_an_empty_queue_says_so(client, session, project):
    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "Queue empty" in r.text


def test_a_missing_page_image_is_a_404_not_a_crash(client, session, document):
    r = client.get(f"/page-image/{document.id}/1")
    assert r.status_code == 404


# ------------------------------ what the queue surfaces about a row (#71)


def rich_candidate(session, project, document, **payload):
    c = make_candidate(session, project, document)
    c.payload_json = {**c.payload_json, **payload}
    session.flush()
    return c


def test_the_queue_shows_which_tier_read_the_row(session, project, document):
    """A transcribed row deserves different weight from one read off the
    text layer, the same way `text_source: ocr` already does — and today a
    reviewer cannot tell without opening the payload."""
    rich_candidate(session, project, document, tier="transcribe", text_source="ocr")

    view = build_view(session, next_candidate(session, project.id))

    assert view.tier == "transcribe"
    assert view.text_source == "ocr"


def test_the_queue_lists_headers_the_vocabulary_could_not_place(
    session, project, document
):
    """The queue telling a human "the document says something the Ledger
    has no field for" — the trigger for a deliberate vocabulary extension,
    which is not an extractor's decision to make. Reconstructing these from
    the payload by hand is how #85 and #97 were investigated."""
    rich_candidate(
        session,
        project,
        document,
        unmapped_columns=["Retain and Protect", "Abandon / Deactivate"],
    )

    view = build_view(session, next_candidate(session, project.id))

    assert view.unmapped_columns == ["Retain and Protect", "Abandon / Deactivate"]


def test_an_unverified_row_names_the_field_that_failed(session, project, document):
    """ "Unverified" that names the suspect value instead of only sinking
    the row. A reviewer who cannot see *which* field is unsupported has to
    re-verify all of them."""
    rich_candidate(
        session,
        project,
        document,
        unverified_fields=["station_from"],
        low_confidence_tokens=["1149"],
    )

    view = build_view(session, next_candidate(session, project.id))

    assert view.unverified_fields == ["station_from"]
    assert view.low_confidence_tokens == ["1149"]


def test_a_clean_row_surfaces_nothing_extra(session, project, document):
    """The common case stays quiet. A queue that flags every row flags
    nothing."""
    make_candidate(session, project, document)

    view = build_view(session, next_candidate(session, project.id))

    assert view.unmapped_columns == []
    assert view.unverified_fields == []
    assert view.low_confidence_tokens == []
    assert view.tier is None


def test_the_queue_page_renders_what_it_surfaces(client, session, project, document):
    rich_candidate(
        session,
        project,
        document,
        tier="transcribe",
        unmapped_columns=["Retain and Protect"],
        unverified_fields=["station_from"],
    )
    session.commit()

    body = client.get(f"/queue/{project.slug}").text

    assert "transcribe" in body
    assert "Retain and Protect" in body
    assert "station_from" in body


# ------------- evidence for a source with no page image (ADR-0005, #60)


def sheet_document(session, project):
    """A workbook: sheets instead of pages, cells instead of a layout."""
    d = Document(
        project_id=project.id,
        sha256="e" * 64,
        filename="I-35-NEX-South-UCM.xlsx",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(d)
    session.flush()
    session.add(
        DocPage(
            document_id=d.id,
            page_no=1,
            text=(
                "Utility Conflict ID Utility Owner Start Station\n"
                "UC-1 CenterPoint Energy 1149+00\n"
                "UC-2 AT&T Texas 1151+00"
            ),
            image_path=None,
            text_source="cells",
        )
    )
    session.flush()
    return d


def test_a_sheet_candidate_shows_its_cells_instead_of_a_page_image(
    client, session, project
):
    """#60's third scope item: a sheet has no page and still needs a
    rendering a reviewer can check a quote against.

    The generated text *is* that rendering — it was made from the same
    cells the values came from — and it is already stored. Showing an
    `<img>` that 404s and nothing else leaves the reviewer with a quote and
    no way to check it, which is the one thing the evidence pane exists
    for.
    """
    document = sheet_document(session, project)
    make_candidate(session, project, document, uid="UC-1")

    body = client.get(f"/queue/{project.slug}").text

    assert "UC-1 CenterPoint Energy 1149+00" in body
    assert f"/page-image/{document.id}/1" not in body


def test_a_pdf_candidate_still_shows_its_page_image(client, session, project, document):
    """The common case is untouched — a printout has a rendering and the
    quote is shown against it."""
    make_candidate(session, project, document)

    body = client.get(f"/queue/{project.slug}").text

    assert f"/page-image/{document.id}/1" in body


def test_a_sheet_candidate_is_labelled_as_read_from_cells(client, session, project):
    """`OCR` already earns a label because it is materially less reliable.
    Cells are materially *more* so, and a reviewer weighing a citation
    should see which they are looking at."""
    document = sheet_document(session, project)
    make_candidate(session, project, document, uid="UC-1")

    body = client.get(f"/queue/{project.slug}").text

    # The label beside the filename, where `OCR` already appears — not the
    # word "cells" anywhere on the page, which the highlight hint has said
    # since long before spreadsheets were readable.
    assert "· <span" in body and "read from cells" in body


# ----------------- exception pills read as facts (#117, ADR-0010)


def test_documentation_review_labels_preserve_source_wording_and_current_mark(
    client, session, project, document
):
    """A review mark describes documents, without rewriting the source's terms."""
    source_wording = "Ready Dependency: Milestone Evidence required for closure."
    document.filename = "Ready-Milestone-Evidence.pdf"
    page = session.scalar(
        select(DocPage).where(DocPage.document_id == document.id, DocPage.page_no == 1)
    )
    page.text = source_wording
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-DOCUMENT-REVIEW",
        dep_type="utility_relocation",
        title=source_wording,
        evidence_required="Evidence Required: original completion record",
    )
    session.add(dependency)
    session.flush()
    evidence = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=source_wording,
        verified=True,
    )
    session.add(evidence)
    session.flush()
    detail_url = f"/ledger/{project.slug}/{dependency.id}"

    before = client.get(detail_url)
    assert before.status_code == 200
    assert "Documents required for this condition" in before.text
    assert "Source passage check" in before.text
    assert "Not confirmed" in before.text
    assert "Mark documents sufficient" in before.text

    marked = client.post(
        f"/dependencies/{dependency.id}/evidence/{evidence.id}/satisfies",
        data={"slug": project.slug},
    )
    assert marked.status_code == 200
    assert "Documents marked sufficient" in marked.text
    assert "Remove sufficiency mark" in marked.text
    assert "Mark documents sufficient" not in marked.text

    for response in (before, marked):
        assert source_wording in unescape(response.text)
        assert "Ready-Milestone-Evidence.pdf" in response.text
        assert "Evidence Required: original completion record" in response.text
        assert "Relocation complete" not in response.text
        assert "mark as closing" not in response.text

    listing = client.get(f"/ledger/{project.slug}?ready=yes")
    assert "Constraints" in listing.text
    assert "DEP-DOCUMENT-REVIEW" in listing.text
    assert "Documents marked sufficient" in listing.text
    assert 'name="ready"' in listing.text


def test_standard_documentation_checklist_allows_only_a_cited_approval_confirmation(
    client, session, project, document
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-STANDARD-DOCUMENTATION",
        dep_type="utility_relocation",
        title="Standard documentation",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    document.filename = "relocation-approval.pdf"
    page = session.scalar(
        select(DocPage).where(DocPage.document_id == document.id, DocPage.page_no == 1)
    )
    page.text = "The as-built package is on file. The relocation is approved."
    as_built = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="The as-built package is on file.",
        verified=True,
    )
    approval = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="The relocation is approved.",
        verified=True,
    )
    session.add_all((as_built, approval))
    session.flush()
    detail = f"/ledger/{project.slug}/{dependency.id}"

    before = client.get(detail)
    assert before.status_code == 200
    assert "Standard documentation checklist" in before.text
    assert "Confirm: this letter approves the relocation" in before.text
    assert "Mark documents sufficient" not in before.text

    bypass = client.post(
        f"/dependencies/{dependency.id}/evidence/{as_built.id}/satisfies",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert bypass.status_code == 409

    confirmed = client.post(
        f"/dependencies/{dependency.id}/documentation/confirm-approval",
        data={"slug": project.slug, "evidence_link_id": approval.id},
    )
    assert confirmed.status_code == 200
    assert "complete" in confirmed.text
    assert "Documentation fields complete" in confirmed.text


def test_a_conditional_letter_shows_its_quoted_condition_and_only_the_override_click(
    client, session, project, document
):
    """ADR-0060: conditional records itself; the only click is the override."""

    condition = "The relocation is approved pending final inspection of segment B."
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-CONDITIONAL-LETTER",
        dep_type="utility_relocation",
        title="Conditional letter",
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    page = session.scalar(
        select(DocPage).where(DocPage.document_id == document.id, DocPage.page_no == 1)
    )
    page.text = f"The as-built package is on file. {condition}"
    as_built = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="The as-built package is on file.",
        verified=True,
    )
    hedged = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=condition,
        verified=True,
    )
    session.add_all((as_built, hedged))
    session.flush()

    before = client.get(f"/ledger/{project.slug}/{dependency.id}")
    assert before.status_code == 200
    text = unescape(before.text)
    assert "recorded as conditional" in text
    assert "waiting on:" in text
    assert condition in text
    assert "Record as approval — the quoted condition is immaterial" in text
    assert f"/page-image/{document.id}/1" in before.text
    assert "Documentation fields not complete" in before.text

    plain_confirm = client.post(
        f"/dependencies/{dependency.id}/documentation/confirm-approval",
        data={"slug": project.slug, "evidence_link_id": hedged.id},
        follow_redirects=False,
    )
    assert plain_confirm.status_code == 409

    overridden = client.post(
        f"/dependencies/{dependency.id}/documentation/confirm-approval",
        data={
            "slug": project.slug,
            "evidence_link_id": hedged.id,
            "condition_immaterial": "true",
        },
    )
    assert overridden.status_code == 200
    assert "Documentation fields complete" in overridden.text


def two_overdue(session, project):
    """Two overdue records; one critical, one whose document said nothing."""
    from datetime import date, timedelta

    document = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, "overdue-statements"),
        filename="overdue-statements.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=2,
    )
    session.add(document)
    session.flush()
    deps = []
    for ref, strategy in (("DEP-CRIT", "relocate"), ("DEP-PLAIN", None)):
        committed_date = date.today() - timedelta(days=40)
        party = ExternalOrg(name=f"Overdue test party {len(deps) + 1}")
        session.add(party)
        session.flush()
        dep = Dependency(
            project_id=project.id,
            ref_code=ref,
            dep_type="utility_relocation",
            title=f"Telecom — {ref}",
            resolution_strategy=strategy,
            external_org_id=party.id,
            internal_owner="Bryce",
        )
        session.add(dep)
        deps.append(dep)
    session.flush()
    for page_no, dep in enumerate(deps, start=1):
        party = session.get(ExternalOrg, dep.external_org_id)
        quote = "The external party stated its completion date."
        session.add(
            DocPage(
                document_id=document.id,
                page_no=page_no,
                text=quote,
                image_path=None,
            )
        )
        session.flush()
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=date.today() - timedelta(days=60),
            description=quote,
            new_timing=StatementTiming.day(committed_date.isoformat(), committed_date),
            scope=StatementScope.selected((dep.id,)),
            created_by="corridor:event-admission",
            evidence=CitedStatementEvidence(document.id, page_no, quote),
        )
    return deps


def test_ledger_pills_carry_the_rules_own_days(client, session, project):
    """ "Committed Date passed 40d" is a fact a reviewer can check against the record;
    the score it replaces was not."""
    two_overdue(session, project)

    body = client.get(f"/ledger/{project.slug}").text

    assert "Promised timing passed 40d" in body


def test_no_severity_markup_survives_anywhere(client, session, project):
    """The colour that fired at an arbitrary constant died with the
    constant (ADR-0010)."""
    deps = two_overdue(session, project)

    ledger = client.get(f"/ledger/{project.slug}").text
    detail = client.get(f"/ledger/{project.slug}/{deps[0].id}").text

    for body in (ledger, detail):
        assert "sev-high" not in body
        assert "severity" not in body.lower()


def test_overdue_criticals_is_one_query(client, session, project):
    """The slice ADR-0010 promised: criticality filters, the rule filters,
    and together they answer "show me the overdue relocations"."""
    two_overdue(session, project)

    body = client.get(
        f"/ledger/{project.slug}?resolution_strategy=critical&rule=OVERDUE"
    ).text

    assert "DEP-CRIT" in body
    assert "DEP-PLAIN" not in body


def test_the_dependency_view_states_days_beside_each_rule(client, session, project):
    deps = two_overdue(session, project)

    body = client.get(f"/ledger/{project.slug}/{deps[0].id}").text

    assert "Promised timing passed" in body
    assert "40d" in body


def test_a_zero_day_quantity_still_renders(client, session, project):
    """ "Needed in 0 days" is due today, and 0 is not None: a truthiness
    check would have swallowed exactly the row a reviewer most needs."""
    from datetime import date

    dep = Dependency(
        project_id=project.id,
        ref_code="DEP-TODAY",
        dep_type="utility_relocation",
        title="Telecom — DEP-TODAY",
        need_date=date.today(),
        committed_date=date.today(),
        internal_owner="Bryce",
    )
    session.add(dep)
    session.flush()

    body = client.get(f"/ledger/{project.slug}").text

    assert "Required by date is near 0d" in body


def test_superseded_citation_is_visible_as_reconfirmation_work(
    client, session, project, document
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-PROVENANCE-REVIEW",
        dep_type="utility_relocation",
        title="Telecom — provenance review",
    )
    successor = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, "successor-awaiting-extraction.pdf"),
        filename="successor-awaiting-extraction.pdf",
        doc_type="matrix",
        parse_status="pending",
        pages=1,
    )
    session.add_all([dependency, successor])
    session.flush()
    publication = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="FOC1-1 AT&T Texas (SWBT)",
        verified=True,
    )
    session.add(publication)
    session.flush()
    designate_publication_support(
        session,
        dependency.id,
        publication.id,
        principal=TEST_PRINCIPAL,
    )
    document.registry_id = f"web-predecessor-{document.id}"
    successor.registry_id = f"web-successor-{successor.id}"
    session.flush()
    # The successor states what it replaces, so it carries the cited page.
    session.add(
        DocPage(document_id=successor.id, page_no=1, text="Replaces the prior revision")
    )
    session.flush()
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=document.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=date.today(),
                source_registry_id=successor.registry_id,
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    ledger = client.get(f"/ledger/{project.slug}").text
    detail = client.get(f"/ledger/{project.slug}/{dependency.id}").text

    for body in (ledger, detail):
        assert "Supporting document replaced 0d" in body
        assert "needs human re-confirmation against the current revision" in body


def _event_candidate(session, project, document):
    c = make_candidate(
        session,
        project,
        document,
        uid="EVT-1",
        station_from="",
        whole_row=False,
        kind="event",
        prompt_version="minutes_v1",
        auto_active_run=False,
    )
    c.payload_json = {
        **c.payload_json,
        "kind": "event",
        "fields": {"description": "AT&T confirmed relocation NTP in August"},
        "citations": [
            {
                "document_id": document.id,
                "page": 1,
                "quote": "AT&T confirmed relocation NTP in August",
                "verified": True,
            }
        ],
    }
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes_v1",
        candidate_count=1,
        page_errors=0,
        candidates=(c,),
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)
    return c


def test_the_queue_never_offers_an_event_at_all(session, project, document):
    """Stronger than a disabled button: the row is not served.

    `make minutes` writes events into the same table, and the queue used
    to serve them — 1,629 of them stood in front of SH 99's 1,401
    dependency rows, each drawn as a conflict row of dashes, each
    offering an accept that Adjudication must refuse (#209).
    """
    event = _event_candidate(session, project, document)
    dependency = make_candidate(session, project, document, uid="FOC9-9")

    served = next_candidate(session, project.id)

    assert served is not None
    assert served.id == dependency.id
    assert served.id != event.id


def test_posting_accept_for_an_event_is_refused_not_a_server_error(
    session, project, document, client
):
    """The button is disabled; a form post can still reach the route."""
    candidate = _event_candidate(session, project, document)
    before = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in (Dependency, Assertion, EvidenceLink, AuditLog)
    }

    response = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert candidate.state == "pending"
    assert {
        model: session.scalar(select(func.count()).select_from(model))
        for model in before
    } == before


# --- The first Work Decision surface: assign an Internal Owner (#169) -------


def test_the_record_view_assigns_an_owner_as_a_visible_project_decision(
    session, client, project
):
    dep = Dependency(
        project_id=project.id,
        ref_code="WD-WEB-1",
        dep_type="utility_relocation",
        title="Water main at 1102+20",
    )
    session.add(dep)
    session.flush()

    page = client.get(f"/ledger/{project.slug}/{dep.id}").text
    assert "unassigned" in page

    response = _save_plan(client, session, project, dep.id, due_date="2026-09-01")
    assert response.status_code == 303

    page = client.get(f"/ledger/{project.slug}/{dep.id}").text
    assert "Dana Fields" in page
    assert "a project decision by" in page
    assert TEST_PRINCIPAL.subject in page


def test_a_blank_assignee_refuses_at_the_form_boundary(session, client, project):
    dep = Dependency(
        project_id=project.id,
        ref_code="WD-WEB-2",
        dep_type="utility_relocation",
        title="Duct bank at 1117+00",
    )
    session.add(dep)
    session.flush()

    response = client.post(
        f"/dependencies/{dep.id}/plan",
        data={
            "slug": project.slug,
            "internal_owner_roster_entry_id": "",
            "next_action": PLAN_ACTION,
            "action_due_date": "2026-09-01",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    session.refresh(dep)
    assert dep.internal_owner is None


def test_the_action_lifecycle_runs_from_the_record_view(session, client, project):
    dep = Dependency(
        project_id=project.id,
        ref_code="WD-WEB-3",
        dep_type="utility_relocation",
        title="Duct bank at 1117+00",
    )
    session.add(dep)
    session.flush()

    set_response = _save_plan(
        client, session, project, dep.id, due_date="2026-09-01"
    )
    assert set_response.status_code == 303
    page = client.get(f"/ledger/{project.slug}/{dep.id}").text
    assert PLAN_ACTION in page
    assert "due 2026-09-01" in page

    done = client.post(
        f"/dependencies/{dep.id}/action/complete",
        data={
            "slug": project.slug,
            "no_follow_up_reason": "return_condition_recorded",
        },
        follow_redirects=False,
    )
    assert done.status_code == 303
    page = client.get(f"/ledger/{project.slug}/{dep.id}").text
    assert "none recorded" in page

    again = client.post(
        f"/dependencies/{dep.id}/action/complete",
        data={
            "slug": project.slug,
            "no_follow_up_reason": "return_condition_recorded",
        },
        follow_redirects=False,
    )
    assert again.status_code == 400


# --- The rehearsal queue lane (#175) ----------------------------------------


def _rehearsal_receipt(
    session,
    project,
    *,
    added_rows=(("W4", "1110+00"),),
):
    """A receipt derived through the real service: W4 in, W1 out."""
    import hashlib as _hashlib
    from datetime import date as _date

    from corridor.cohort import derive_cohort_receipt
    from corridor.revision_comparison import create_revision_comparison
    from corridor.supersession import (
        SupersessionDeclaration,
        register_supersessions,
    )

    def doc(registry_id, filename):
        document = Document(
            project_id=project.id,
            registry_id=registry_id,
            sha256=_hashlib.sha256(registry_id.encode()).hexdigest(),
            filename=filename,
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text="rehearsal rows"))
        session.flush()
        return document

    def run(document, rows, prompt_version):
        made = []
        for utility_id, station in rows:
            candidate = Candidate(
                project_id=project.id,
                kind="dependency",
                payload_json={
                    "kind": "dependency",
                    "fields": {
                        "utility_id": utility_id,
                        "external_org": "City of Houston",
                        "utility_type": "WW",
                        "baseline": "SR-BL",
                        "potential_conflict": "Y",
                        "station_from": station,
                        "station_to": station,
                    },
                    "citations": [
                        {
                            "document_id": document.id,
                            "page": 1,
                            "quote": f"{utility_id} City of Houston",
                            "verified": True,
                            "whole_row": True,
                        }
                    ],
                    "unverified_fields": [],
                    "unmapped_columns": [],
                    "low_confidence_tokens": [],
                    "tier": "structure",
                    "dedupe_hint": f"{utility_id}|{station}",
                    "text_source": "text_layer",
                },
                source_document_id=document.id,
                source_pages=[1],
                confidence=0.99,
                prompt_version=prompt_version,
                model="gpt-test",
                citations_verified=True,
            )
            session.add(candidate)
            made.append(candidate)
        recorded = record_extraction_run(
            session,
            document,
            prompt_version=prompt_version,
            candidate_count=len(made),
            page_errors=0,
            candidates=tuple(made),
            model="gpt-test",
            schema_version="matrix_candidate_shape_v1",
            allow_unsealed_legacy=True,
        )
        session.flush()
        return recorded, made

    december = doc("rehearsal-dec", "dec.pdf")
    february = doc("rehearsal-feb", "feb.pdf")
    index = doc("rehearsal-rid", "rid.pdf")
    predecessor_run, _ = run(december, [("W1", "1102+20")], "rehearsal_v1.dec")
    successor_run, candidates = run(
        february,
        [("W1", "1102+20"), *added_rows],
        "rehearsal_v1.feb",
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="rehearsal-dec",
                successor_registry_id="rehearsal-feb",
                replacement_date=_date(2026, 2, 13),
                source_registry_id="rehearsal-rid",
                source_page=1,
            )
        ],
        project_id=project.id,
    )
    declare_active_run(
        session, december.id, predecessor_run.id, principal=TEST_PRINCIPAL
    )
    declare_active_run(session, february.id, successor_run.id, principal=TEST_PRINCIPAL)
    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    receipt = derive_cohort_receipt(
        session, comparison.id, external_org="City of Houston"
    )
    return receipt, candidates


def test_the_rehearsal_lane_reads_exactly_the_receipt(session, client, project):
    receipt, _ = _rehearsal_receipt(session, project)

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    ).text
    assert f"Test input manifest #{receipt.id}" in page
    assert "0 of 1 decided" in page
    assert "W4" in page
    assert 'name="cohort_receipt_id"' in page
    # The redesign's contracts: the machinery banner stays off this lane,
    # the reason-for-membership is stated, and the rail groups by it.
    assert "Automatic Carry-Forward" not in page
    assert "Newly added (1)" in page
    assert "newly added" in page


def test_the_boundary_refuses_a_non_member_mutation(session, client, project):
    receipt, candidates = _rehearsal_receipt(session, project)
    non_member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W1"
    )

    response = client.post(
        f"/candidates/{non_member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    session.refresh(non_member)
    assert non_member.state == "pending"

    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    assert accepted.status_code == 303


def test_the_boundary_refuses_a_receipt_from_another_project(session, client, project):
    """The read path already refused this; every write path allowed it.

    Rendering the lane checked that the named receipt belonged to the
    project in the URL. The mutation routes checked only that the receipt
    existed and that the row was one of its members — so a receipt id from
    another project, posted against this project's slug, was honoured.
    """
    from corridor.models import Project

    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )

    stranger = Project(slug="a-different-project", name="A different project")
    session.add(stranger)
    session.flush()
    # Make the caller a genuine member of the stranger project, so this test
    # exercises the receipt/candidate cross-project boundary (409) rather than
    # stopping earlier at the membership gate (#331).
    seed_membership(session, stranger, TEST_PRINCIPAL)

    # The member and the receipt are genuinely paired; only the project
    # the request names is wrong.
    response = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": stranger.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "another project" in response.text
    session.refresh(member)
    assert member.state == "pending"


def test_accept_flows_into_the_coordination_strip_and_back(session, client, project):
    """One pass: admit, assign, set the action, land on the next row."""
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"

    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    location = accepted.headers["location"]
    assert location.startswith(lane)
    assert "coordinate=" in location

    page = client.get(location).text
    assert "Recorded" in page
    assert "assigned to" in page

    dependency_id = int(location.rsplit("coordinate=", 1)[1])
    saved = _save_plan(
        client,
        session,
        project,
        dependency_id,
        due_date="2026-09-01",
        redirect_to=lane,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == lane

    dep = session.get(Dependency, dependency_id)
    session.refresh(dep)
    assert dep.internal_owner == "Dana Fields"
    assert dep.next_action == PLAN_ACTION


def test_decided_rehearsal_cohort_resumes_admitted_dependency_coordination(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"

    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])

    page = client.get(lane).text

    assert "Proposed constraint review complete" in page
    assert "1 recorded Constraint needs coordination" in page
    assert "Recorded" in page
    assert "assigned to" in page
    assert "Continue coordination" in page
    assert f"coordinate={dependency_id}" in page
    assert "Queue empty" not in page

    continued = client.get(f"{lane}&coordinate={dependency_id}").text
    assert "Recorded" in continued
    assert "assigned to" in continued
    assert 'name="internal_owner_roster_entry_id"' in continued
    assert 'name="next_action"' in continued


def test_decided_rehearsal_cohort_keeps_coordinated_dependency_openable(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])
    _save_plan(
        client,
        session,
        project,
        dependency_id,
        due_date_unknown_reason="awaiting_schedule_information",
    )

    page = client.get(lane).text

    assert "Proposed constraint review complete" in page
    assert "Every recorded Constraint currently has a Follow-up plan" in page
    assert "Review coordination" in page
    detail_url = _link_href(page, "Review coordination")
    assert detail_url.startswith(f"/ledger/{project.slug}/{dependency_id}?return_to=")
    assert 'name="owner"' not in page
    assert "Queue empty" not in page

    detail = client.get(detail_url).text
    assert "Assigned to" in detail
    assert "completed" in detail
    assert "cancel action" in detail


def test_rehearsal_cohort_summary_flag_clears_default_coordinate_focus(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])

    page = client.get(f"{lane}&summary=1").text

    assert "Proposed constraint review complete" in page
    assert "Continue coordination" in page
    assert f"coordinate={dependency_id}" in page
    assert 'name="owner"' not in page


def test_decided_rehearsal_cohort_does_not_hide_a_mixed_unresolved_member(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(
        session,
        project,
        added_rows=(("W4", "1110+00"), ("W5", "1112+00")),
    )
    w4 = next(c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4")
    w5 = next(c for c in candidates if c.payload_json["fields"]["utility_id"] == "W5")
    accepted = client.post(
        f"/candidates/{w4.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])
    _save_plan(
        client,
        session,
        project,
        dependency_id,
        due_date_unknown_reason="awaiting_schedule_information",
    )
    w5.state = "accepted"
    session.flush([w5])

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}&summary=1"
    ).text

    assert "Every recorded Constraint currently has a Follow-up plan" not in page
    assert "1 test case has no trustworthy recorded Constraint link" in page
    assert "Recorded Constraint unavailable" in page


def test_decided_rehearsal_cohort_orders_incomplete_before_coordinated(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(
        session,
        project,
        added_rows=(("W4", "1110+00"), ("W5", "1112+00")),
    )
    by_utility_id = {
        candidate.payload_json["fields"]["utility_id"]: candidate
        for candidate in candidates
    }
    admitted = {}
    for utility_id in ("W4", "W5"):
        response = client.post(
            f"/candidates/{by_utility_id[utility_id].id}/accept",
            data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
            follow_redirects=False,
        )
        admitted[utility_id] = int(
            response.headers["location"].rsplit("coordinate=", 1)[1]
        )
    _save_plan(
        client,
        session,
        project,
        admitted["W4"],
        due_date_unknown_reason="awaiting_schedule_information",
    )

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}&summary=1"
    ).text

    assert page.index("W5") < page.index("W4")
    assert "1 recorded Constraint needs coordination" in page


def test_rehearsal_cohort_focuses_the_first_missing_coordination_field(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    int(accepted.headers["location"].rsplit("coordinate=", 1)[1])

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    ).text

    # One roster-backed plan form, one Save; the free-text inputs are gone
    # and the unassigned record focuses the assignee choice first.
    assert 'name="owner"' not in page
    assert 'name="action"' not in page
    owner_select = page.split('name="internal_owner_roster_entry_id"', 1)[1].split(
        ">", 1
    )[0]
    assert "autofocus" in owner_select


def _link_href(page: str, label: str) -> str:
    before_link_end = page.split(f">{label}</a>", 1)[0]
    return unescape(before_link_end.rsplit('href="', 1)[1].split('"', 1)[0])


class _RenderedForm(HTMLParser):
    """Read submitted input values from one form the user can see."""

    def __init__(self, action: str):
        super().__init__()
        self.action = action
        self.in_form = False
        self.found = False
        self.data: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.in_form = attributes.get("action") == self.action
            self.found = self.found or self.in_form
        elif self.in_form and tag == "input" and attributes.get("name"):
            self.data[attributes["name"]] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.in_form:
            self.in_form = False


def _rendered_form_data(page: str, action: str) -> dict[str, str]:
    form = _RenderedForm(action)
    form.feed(page)
    assert form.found, f"no rendered form posts to {action}"
    return form.data


def test_cohort_carried_coordination_forms_return_to_the_exact_cohort(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    record_url = _link_href(
        client.get(lane).text,
        "View record and supporting documents",
    )
    detail = client.get(record_url).text
    return_url = _link_href(detail, "Back to test coordination")
    action = f"/dependencies/{dependency_id}/plan"
    form = _rendered_form_data(detail, action)

    assert form["redirect_to"] == return_url
    entry = _plan_assignee(session, project)
    form.update(
        {
            "internal_owner_roster_entry_id": str(entry.id),
            "next_action": PLAN_ACTION,
            "action_due_date": "",
            "action_due_date_unknown_reason": "awaiting_schedule_information",
        }
    )
    response = client.post(action, data=form, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == return_url
    dependency = session.get(Dependency, dependency_id)
    session.refresh(dependency)
    assert dependency.internal_owner == "Dana Fields"
    next_action = session.scalar(
        select(WorkDecision).where(
            WorkDecision.dependency_id == dependency_id,
            WorkDecision.decision_type == "set_next_action",
        )
    )
    assert next_action is not None
    assert json.loads(next_action.after_value) == {
        "action": PLAN_ACTION,
        "due_date": None,
    }
    assert next_action.action_due_date_reason == "awaiting_schedule_information"

    record_url = _link_href(
        client.get(f"{lane}&summary=1").text,
        "Review coordination",
    )
    detail = client.get(record_url).text
    return_url = _link_href(detail, "Back to test coordination")
    assert return_url == f"{lane}&summary=1"
    action = f"/dependencies/{dependency_id}/action/complete"
    form = _rendered_form_data(detail, action)
    assert form["redirect_to"] == return_url
    form["no_follow_up_reason"] = "no_immediate_follow_up"
    response = client.post(action, data=form, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == return_url
    completion = session.scalar(
        select(WorkDecision).where(
            WorkDecision.dependency_id == dependency_id,
            WorkDecision.decision_type == "complete_next_action",
        )
    )
    assert completion is not None
    assert completion.predecessor_decision_id == next_action.id
    assert completion.after_value is None
    assert completion.no_follow_up_reason == "no_immediate_follow_up"

    detail = client.get(record_url).text
    action = f"/dependencies/{dependency_id}/plan"
    form = _rendered_form_data(detail, action)
    assert form["redirect_to"] == return_url
    form.update(
        {
            "internal_owner_roster_entry_id": str(entry.id),
            "next_action": FOLLOW_UP_NEXT_ACTION_CHOICES[1],
            "action_due_date": "",
            "action_due_date_unknown_reason": "awaiting_external_information",
        }
    )
    response = client.post(action, data=form, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == return_url
    successor = session.scalar(
        select(WorkDecision)
        .where(
            WorkDecision.dependency_id == dependency_id,
            WorkDecision.decision_type == "set_next_action",
        )
        .order_by(WorkDecision.id.desc())
    )
    assert successor is not None
    assert successor.id != next_action.id
    assert successor.predecessor_decision_id == completion.id
    assert json.loads(successor.after_value) == {
        "action": FOLLOW_UP_NEXT_ACTION_CHOICES[1],
        "due_date": None,
    }
    assert successor.action_due_date_reason == "awaiting_external_information"

    detail = client.get(record_url).text
    action = f"/dependencies/{dependency_id}/action/cancel"
    form = _rendered_form_data(detail, action)
    assert form["redirect_to"] == return_url
    form.update(
        {
            "no_follow_up_reason": "return_condition_recorded",
            "cancellation_reason": "superseded",
        }
    )
    response = client.post(action, data=form, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == return_url
    cancellation = session.scalar(
        select(WorkDecision).where(
            WorkDecision.dependency_id == dependency_id,
            WorkDecision.decision_type == "cancel_next_action",
        )
    )
    assert cancellation is not None
    assert cancellation.predecessor_decision_id == successor.id
    assert cancellation.after_value is None
    assert cancellation.no_follow_up_reason == "return_condition_recorded"
    assert cancellation.cancellation_reason == "superseded"


def test_rehearsal_cohort_exposes_record_evidence_and_exact_return(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))
    before_work_decisions = session.scalar(
        select(func.count()).select_from(WorkDecision)
    )
    before_reports = session.scalar(select(func.count()).select_from(ReportRun))
    before_candidate_state = member.state

    coordination = client.get(lane).text
    record_url = _link_href(coordination, "View record and supporting documents")

    assert record_url.startswith(f"/ledger/{project.slug}/{dependency_id}?return_to=")
    detail = client.get(record_url)
    assert detail.status_code == 200
    assert "Back to test coordination" in detail.text
    assert "feb.pdf" in detail.text
    return_url = _link_href(detail.text, "Back to test coordination")
    assert return_url == f"{lane}&coordinate={dependency_id}"

    returned = client.get(return_url).text
    assert "Recorded" in returned
    assert 'name="internal_owner_roster_entry_id"' in returned
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit
    assert (
        session.scalar(select(func.count()).select_from(WorkDecision))
        == before_work_decisions
    )
    assert session.scalar(select(func.count()).select_from(ReportRun)) == before_reports
    session.refresh(member)
    assert member.state == before_candidate_state


def test_dependency_detail_refuses_an_unsafe_cohort_return(session, client, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-RETURN-SAFE",
        dep_type="utility_relocation",
        title="Safe return test",
    )
    session.add(dependency)
    session.flush()

    for unsafe in (
        "https://example.com/",
        "//example.com/cohort",
        "/queue/other-project?lane=rehearsal&cohort_receipt_id=1",
        f"/queue/{project.slug}?lane=candidate",
    ):
        response = client.get(
            f"/ledger/{project.slug}/{dependency.id}",
            params={"return_to": unsafe},
        )
        assert response.status_code == 400
        assert "example.com" not in response.text


def test_coordinated_cohort_record_returns_to_the_cohort_summary(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])
    _save_plan(
        client,
        session,
        project,
        dependency_id,
        due_date_unknown_reason="awaiting_schedule_information",
    )
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"

    summary = client.get(f"{lane}&summary=1").text
    detail_url = _link_href(summary, "Review coordination")
    detail = client.get(detail_url).text

    assert "Back to test coordination" in detail
    assert _link_href(detail, "Back to test coordination") == f"{lane}&summary=1"


def test_dependency_detail_without_cohort_context_returns_to_ledger(
    session, client, project
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-LEDGER-BACK",
        dep_type="utility_relocation",
        title="Ordinary detail",
    )
    session.add(dependency)
    session.flush()

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text

    assert "Back to test coordination" not in page
    assert f'href="/ledger/{project.slug}">← constraints</a>' in page


def _anchor_before_label(page: str, label: str) -> str:
    return page.split(f">{label}</a>", 1)[0].rsplit("<a ", 1)[1]


def test_rehearsal_coordination_opens_the_existing_report_workspace_in_a_new_tab(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    before_artifacts = session.scalar(
        select(func.count()).select_from(ExternalReportArtifact)
    )
    before_reports = session.scalar(select(func.count()).select_from(ReportRun))
    before_decisions = session.scalar(select(func.count()).select_from(WorkDecision))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))
    before_state = member.state

    page = client.get(lane).text
    anchor = _anchor_before_label(page, "Prepare coordination report (opens in new tab)")

    assert f'href="/reports/{project.slug}"' in anchor
    assert 'target="_blank"' in anchor
    assert 'rel="noopener"' in anchor
    workspace = client.get(f"/reports/{project.slug}")
    assert workspace.status_code == 200
    assert "Prepare fixed PDF for review" in workspace.text
    assert (
        session.scalar(select(func.count()).select_from(ExternalReportArtifact))
        == before_artifacts
    )
    assert session.scalar(select(func.count()).select_from(ReportRun)) == before_reports
    assert (
        session.scalar(select(func.count()).select_from(WorkDecision))
        == before_decisions
    )
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit + 1
    request_receipt = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == "product_proving_frontend_request",
            AuditLog.entity_type == "project",
            AuditLog.entity_id == project.id,
        )
        .order_by(AuditLog.id.desc())
    )
    assert request_receipt is not None
    assert request_receipt.after_json["route_name"] == "reports"
    assert request_receipt.after_json["method"] == "GET"
    assert request_receipt.after_json["status"] == 200
    session.refresh(member)
    assert member.state == before_state


def test_rehearsal_cohort_summary_keeps_report_navigation_available(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    client.post(
        f"/candidates/{member.id}/reject",
        data={
            "slug": project.slug,
            "cohort_receipt_id": str(receipt.id),
            "reason": "duplicate",
        },
        follow_redirects=False,
    )

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}&summary=1"
    ).text
    anchor = _anchor_before_label(page, "Prepare coordination report (opens in new tab)")

    assert f'href="/reports/{project.slug}"' in anchor
    assert 'target="_blank"' in anchor
    assert 'rel="noopener"' in anchor


def test_ordinary_candidate_lane_does_not_gain_cohort_report_navigation(
    client, session, project, document
):
    make_candidate(session, project, document)

    page = client.get(f"/queue/{project.slug}?lane=candidate").text

    assert "Prepare coordination report (opens in new tab)" not in page


def test_decided_rehearsal_cohort_names_when_nothing_was_admitted(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    rejected = client.post(
        f"/candidates/{member.id}/reject",
        data={
            "slug": project.slug,
            "cohort_receipt_id": str(receipt.id),
            "reason": "duplicate",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 303

    page = client.get(lane).text

    assert "Proposed constraint review complete" in page
    assert "No Constraints were recorded from these test cases" in page
    assert "W4" in page
    assert "rejected" in page
    assert "Queue empty" not in page


def test_decided_rehearsal_cohort_fails_closed_on_missing_admission_link(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    member.state = "accepted"
    session.flush([member])

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    ).text

    assert "Proposed constraint review complete" in page
    assert "W4" in page
    assert "Recorded Constraint unavailable" in page
    assert f'href="/ledger/{project.slug}/' not in page
    assert "Queue empty" not in page


def test_decided_rehearsal_cohort_links_a_merged_member_to_its_exact_dependency(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    target = Dependency(
        project_id=project.id,
        ref_code="DEP-MERGED-W4",
        dep_type="utility_relocation",
        title="City water crossing",
    )
    session.add(target)
    session.flush()
    merged = client.post(
        f"/candidates/{member.id}/merge",
        data={
            "slug": project.slug,
            "cohort_receipt_id": str(receipt.id),
            "dependency_id": str(target.id),
        },
        follow_redirects=False,
    )
    assert merged.status_code == 303

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    ).text

    assert "W4" in page
    assert "merged" in page
    assert f"coordinate={target.id}" in page
    assert "Continue coordination" in page


def test_decided_rehearsal_cohort_preserves_a_dismissed_admission_outcome(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])
    dismissed = client.post(
        f"/ledger/{project.slug}/{dependency_id}/dismiss",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )
    assert dismissed.status_code == 303

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    ).text

    assert "W4" in page
    assert "No recorded Constraints remain open for coordination" in page
    assert "Removed from the active log by a recorded decision" in page
    assert f'href="/ledger/{project.slug}/{dependency_id}"' not in page
    assert "Continue coordination" not in page


def test_rehearsal_cohort_does_not_open_a_dismissed_dependency(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    lane = f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id={receipt.id}"
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={"slug": project.slug, "cohort_receipt_id": str(receipt.id)},
        follow_redirects=False,
    )
    dependency_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])
    client.post(
        f"/ledger/{project.slug}/{dependency_id}/dismiss",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )

    page = client.get(f"{lane}&coordinate={dependency_id}").text

    assert "Removed from the active log by a recorded decision" in page
    assert "Recorded" not in page
    assert 'name="owner"' not in page


def test_rehearsal_cohort_does_not_open_a_dependency_outside_its_receipt(
    session, client, project
):
    receipt, candidates = _rehearsal_receipt(session, project)
    member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W4"
    )
    accepted = client.post(
        f"/candidates/{member.id}/accept",
        data={
            "slug": project.slug,
            "cohort_receipt_id": str(receipt.id),
        },
        follow_redirects=False,
    )
    admitted_id = int(accepted.headers["location"].rsplit("coordinate=", 1)[1])
    outsider = Dependency(
        project_id=project.id,
        ref_code="DEP-OUTSIDE-COHORT",
        dep_type="utility_relocation",
        title="Outside the pinned receipt",
    )
    session.add(outsider)
    session.flush()

    page = client.get(
        f"/queue/{project.slug}?lane=rehearsal"
        f"&cohort_receipt_id={receipt.id}&coordinate={outsider.id}"
    ).text

    assert "1 recorded Constraint needs coordination" in page
    assert f"coordinate={admitted_id}" in page
    assert "DEP-OUTSIDE-COHORT" not in page
    assert "Outside the pinned receipt" not in page
    assert 'name="owner"' not in page


_COORDINATION_FORM_SUBMISSIONS = (
    (
        "plan",
        {
            "next_action": PLAN_ACTION,
            "action_due_date": "",
            "action_due_date_unknown_reason": "date_not_yet_known",
        },
    ),
    (
        "action/complete",
        {"no_follow_up_reason": "no_immediate_follow_up"},
    ),
    (
        "action/cancel",
        {
            "no_follow_up_reason": "return_condition_recorded",
            "cancellation_reason": "no_longer_needed",
        },
    ),
)


def _seed_next_action_from_detail(client, session, project, detail_url, dependency_id):
    action_path = f"/dependencies/{dependency_id}/plan"
    form = _rendered_form_data(client.get(detail_url).text, action_path)
    form.update(
        {
            "internal_owner_roster_entry_id": str(
                _plan_assignee(session, project).id
            ),
            "next_action": PLAN_ACTION,
            "action_due_date": "",
            "action_due_date_unknown_reason": "awaiting_schedule_information",
        }
    )
    response = client.post(action_path, data=form, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == detail_url


@pytest.mark.parametrize(
    ("path_suffix", "submitted"),
    _COORDINATION_FORM_SUBMISSIONS,
)
def test_direct_coordination_forms_fall_back_to_dependency_detail(
    session, client, project, path_suffix, submitted
):
    dep = Dependency(
        project_id=project.id,
        ref_code="WD-WEB-DIRECT",
        dep_type="utility_relocation",
        title="Direct detail fallback",
    )
    session.add(dep)
    session.flush()
    detail_url = f"/ledger/{project.slug}/{dep.id}"

    if path_suffix in {"action/complete", "action/cancel"}:
        _seed_next_action_from_detail(client, session, project, detail_url, dep.id)

    action_path = f"/dependencies/{dep.id}/{path_suffix}"
    form = _rendered_form_data(client.get(detail_url).text, action_path)
    assert "redirect_to" not in form
    form.update(submitted)
    if path_suffix == "plan":
        form["internal_owner_roster_entry_id"] = str(
            _plan_assignee(session, project).id
        )
        form["next_action"] = FOLLOW_UP_NEXT_ACTION_CHOICES[1]

    response = client.post(action_path, data=form, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == detail_url


@pytest.mark.parametrize(
    ("path_suffix", "submitted"),
    _COORDINATION_FORM_SUBMISSIONS,
)
@pytest.mark.parametrize(
    "unsafe_return",
    ("https://example.com/escape", "//example.com/escape"),
)
def test_unsafe_coordination_form_return_is_rejected_before_any_write(
    session, client, project, path_suffix, submitted, unsafe_return
):
    dep = Dependency(
        project_id=project.id,
        ref_code="WD-WEB-RD",
        dep_type="utility_relocation",
        title="Redirect test",
    )
    session.add(dep)
    session.flush()

    detail_url = f"/ledger/{project.slug}/{dep.id}"
    if path_suffix in {"action/complete", "action/cancel"}:
        _seed_next_action_from_detail(client, session, project, detail_url, dep.id)

    action_path = f"/dependencies/{dep.id}/{path_suffix}"
    form = _rendered_form_data(client.get(detail_url).text, action_path)
    form.update(submitted)
    if path_suffix == "plan":
        form["internal_owner_roster_entry_id"] = str(
            _plan_assignee(session, project).id
        )
        form["next_action"] = FOLLOW_UP_NEXT_ACTION_CHOICES[1]
    form["redirect_to"] = unsafe_return
    before_decisions = session.scalar(select(func.count()).select_from(WorkDecision))
    before_projection = (
        dep.internal_owner,
        dep.next_action,
        dep.action_due_date,
        dep.action_due_date_reason,
    )

    response = client.post(action_path, data=form, follow_redirects=False)

    assert response.status_code == 400
    assert "example.com" not in response.text
    assert (
        session.scalar(select(func.count()).select_from(WorkDecision))
        == before_decisions
    )
    session.refresh(dep)
    assert (
        dep.internal_owner,
        dep.next_action,
        dep.action_due_date,
        dep.action_due_date_reason,
    ) == before_projection


# ── The event lane (#199): explicit revision choice, one gesture ─────────


def _event_cohort_lane(session, project):
    """A receipt derived through the real service: PL7 in (two matrix
    revisions, one dated commitment), PL8 out (no dated event)."""
    from corridor.cohort import derive_event_cohort_receipt
    from corridor.extraction_runs import (
        declare_single_run_documents,
        record_extraction_run,
    )

    def doc(filename, doc_type, doc_date):
        document = Document(
            project_id=project.id,
            sha256=_document_sha(project.id, filename),
            filename=filename,
            doc_type=doc_type,
            parse_status="parsed",
            pages=1,
            doc_date=doc_date,
        )
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text="event lane rows"))
        session.flush()
        return document

    def run(document, candidates):
        for c in candidates:
            session.add(c)
        made = record_extraction_run(
            session,
            document,
            prompt_version="minutes_v1",
            candidate_count=len(candidates),
            page_errors=0,
            candidates=candidates,
            model="gpt-test",
            schema_version="matrix_candidate_shape_v1",
            allow_unsealed_legacy=True,
        )
        session.flush()
        return made

    def dep(document, uid):
        return Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": {
                    "utility_id": uid,
                    "external_org": "Web queue Pipeline Co",
                    "utility_type": "Petroleum and Gaseous Materials",
                    "baseline": "SR-BL",
                    "station_from": "1102+20",
                    "station_to": "1102+80",
                },
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": "event lane rows",
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "dedupe_hint": f"{uid}|{document.id}",
                "text_source": "text_layer",
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version="minutes_v1",
            model="gpt-test",
            citations_verified=True,
        )

    def event(document, ref):
        return Candidate(
            project_id=project.id,
            kind="event",
            payload_json={
                "kind": "event",
                "fields": {
                    "event_type": "commitment",
                    "description": f"Web queue Pipeline committed on {ref}",
                    "external_org": "Web queue Pipeline Co",
                    "stated_party": "Web queue Pipeline Co",
                    "event_date": "2025-01-16",
                    "committed_date": "2025-06-01",
                    "conflict_ref": ref,
                },
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": "event lane rows",
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "dedupe_hint": f"event|{ref}",
                "text_source": "text_layer",
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version="minutes_v1",
            model="gpt-test",
            citations_verified=True,
        )

    from datetime import date as _date

    rev_a = doc("ucm-feb.pdf", "matrix", _date(2025, 2, 23))
    rev_b = doc("ucm-may.pdf", "matrix", _date(2025, 5, 5))
    minutes = doc("minutes-jan.pdf", "minutes", _date(2025, 1, 16))
    pl7_a = dep(rev_a, "PL7")
    pl7_b = dep(rev_b, "PL7")
    pl8_b = dep(rev_b, "PL8")
    run(rev_a, [pl7_a])
    run(rev_b, [pl7_b, pl8_b])
    run(minutes, [event(minutes, "PL7")])
    declare_single_run_documents(session, project.id, principal=TEST_PRINCIPAL)
    receipt = derive_event_cohort_receipt(session, project.id)
    return receipt, {"pl7_a": pl7_a, "pl7_b": pl7_b, "pl8_b": pl8_b}


def test_the_event_lane_reads_exactly_the_receipt(session, client, project):
    receipt, c = _event_cohort_lane(session, project)

    page = client.get(
        f"/queue/{project.slug}?lane=events"
        f"&event_cohort_receipt_id={receipt.id}"
        f"&candidate_id={c['pl7_b'].id}"
    ).text
    assert f"Statement test input manifest #{receipt.id}" in page
    assert "PL7" in page
    # The other revision of the same conflict is offered as a pre-checked
    # merge inside the accept gesture, named by its document.
    assert 'name="merge_sibling_ids"' in page
    assert "ucm-feb.pdf" in page
    assert 'name="event_cohort_receipt_id"' in page


def test_event_lane_accept_still_opens_the_admitted_dependency(
    session, client, project
):
    receipt, candidates = _event_cohort_lane(session, project)
    accepted = client.post(
        f"/candidates/{candidates['pl7_b'].id}/accept",
        data={
            "slug": project.slug,
            "event_cohort_receipt_id": str(receipt.id),
            "merge_sibling_ids": [str(candidates["pl7_a"].id)],
        },
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert "coordinate=" in accepted.headers["location"]
    page = client.get(accepted.headers["location"]).text
    assert "Recorded" in page
    assert 'name="internal_owner_roster_entry_id"' in page
    assert 'name="next_action"' in page
    assert "Prepare coordination report (opens in new tab)" not in page


def test_the_event_lane_boundary_refuses_a_non_member_mutation(
    session, client, project
):
    receipt, c = _event_cohort_lane(session, project)

    refused = client.post(
        f"/candidates/{c['pl8_b'].id}/accept",
        data={
            "slug": project.slug,
            "event_cohort_receipt_id": str(receipt.id),
        },
        follow_redirects=False,
    )
    assert refused.status_code == 409
    session.refresh(c["pl8_b"])
    assert c["pl8_b"].state == "pending"

    accepted = client.post(
        f"/candidates/{c['pl7_b'].id}/accept",
        data={
            "slug": project.slug,
            "event_cohort_receipt_id": str(receipt.id),
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert (
        f"lane=events&event_cohort_receipt_id={receipt.id}"
        in accepted.headers["location"]
    )


def test_accept_merges_checked_siblings_in_one_gesture(session, client, project):
    receipt, c = _event_cohort_lane(session, project)

    response = client.post(
        f"/candidates/{c['pl7_b'].id}/accept",
        data={
            "slug": project.slug,
            "event_cohort_receipt_id": str(receipt.id),
            "merge_sibling_ids": [str(c["pl7_a"].id)],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    session.refresh(c["pl7_b"])
    session.refresh(c["pl7_a"])
    assert c["pl7_b"].state == "accepted"
    assert c["pl7_a"].state == "merged"
    dependency_id = int(response.headers["location"].rsplit("coordinate=", 1)[1])
    assert c["pl7_a"].merged_into == dependency_id


def test_a_sibling_outside_the_conflict_refuses_the_whole_gesture(
    session, client, project
):
    receipt, c = _event_cohort_lane(session, project)

    response = client.post(
        f"/candidates/{c['pl7_b'].id}/accept",
        data={
            "slug": project.slug,
            "event_cohort_receipt_id": str(receipt.id),
            "merge_sibling_ids": [str(c["pl8_b"].id)],
        },
        follow_redirects=False,
    )
    assert response.status_code == 409
    session.refresh(c["pl7_b"])
    session.refresh(c["pl8_b"])
    assert c["pl7_b"].state == "pending"
    assert c["pl8_b"].state == "pending"


def test_every_mutation_route_holds_the_event_lane_boundary(session, client, project):
    """edit-accept, merge, and reject refuse a non-member exactly as
    accept does — the keyboard shortcuts are not a side door."""
    receipt, c = _event_cohort_lane(session, project)
    non_member = c["pl8_b"].id
    scope = {
        "slug": project.slug,
        "event_cohort_receipt_id": str(receipt.id),
    }

    edit = client.post(
        f"/candidates/{non_member}/edit-accept",
        data={**scope, "field_utility_id": "PL8"},
        follow_redirects=False,
    )
    assert edit.status_code == 409

    merge = client.post(
        f"/candidates/{non_member}/merge",
        data={**scope, "dependency_id": "1"},
        follow_redirects=False,
    )
    assert merge.status_code == 409

    reject = client.post(
        f"/candidates/{non_member}/reject",
        data={**scope, "reason": "duplicate"},
        follow_redirects=False,
    )
    assert reject.status_code == 409

    session.refresh(c["pl8_b"])
    assert c["pl8_b"].state == "pending"


def test_reject_holds_the_rehearsal_boundary_too(session, client, project):
    """The pre-existing gap: reject carried the receipt id and ignored it."""
    receipt, candidates = _rehearsal_receipt(session, project)
    non_member = next(
        c for c in candidates if c.payload_json["fields"]["utility_id"] == "W1"
    )
    response = client.post(
        f"/candidates/{non_member.id}/reject",
        data={
            "slug": project.slug,
            "cohort_receipt_id": str(receipt.id),
            "reason": "duplicate",
        },
        follow_redirects=False,
    )
    assert response.status_code == 409
    session.refresh(non_member)
    assert non_member.state == "pending"


def test_a_self_or_duplicate_sibling_refuses_cleanly(session, client, project):
    receipt, c = _event_cohort_lane(session, project)
    response = client.post(
        f"/candidates/{c['pl7_b'].id}/accept",
        data={
            "slug": project.slug,
            "event_cohort_receipt_id": str(receipt.id),
            "merge_sibling_ids": [str(c["pl7_b"].id), str(c["pl7_a"].id)],
        },
        follow_redirects=False,
    )
    assert response.status_code == 409
    session.refresh(c["pl7_b"])
    assert c["pl7_b"].state == "pending"


# ── The list builds itself (ADR-0029) ───────────────────────────────────


def test_the_queue_asks_for_no_signature_before_showing_the_list(
    session, client, project
):
    """The load screen is gone. Opening a project shows work, not a
    checklist and not "not loaded yet"."""
    _event_cohort_lane(session, project)
    load_project(session, project.id)

    page = client.get(f"/queue/{project.slug}").text
    assert "Load this project" not in page
    assert "not loaded yet" not in page
    assert f"/projects/{project.slug}/admission/dependencies" not in page
    assert f"/projects/{project.slug}/admission/events" not in page


def test_landing_documents_put_their_conflicts_on_the_record(session, client, project):
    """PL7 is stated identically by both revisions; PL8 by one. Both are
    conflicts, and one matrix is enough to be one."""
    _event_cohort_lane(session, project)

    result = load_project(session, project.id)
    assert result.dependencies.admitted_count == 2

    assert {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"PL7", "PL8"}

    # Both are readable on the record without anyone having judged them.
    record = client.get(f"/ledger/{project.slug}").text
    assert "PL7" in record and "PL8" in record


def test_a_statement_attaches_to_its_conflict_in_the_same_pass(
    session, client, project
):

    _event_cohort_lane(session, project)
    result = load_project(session, project.id)
    assert result.events.admitted_count == 1

    dependency = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project.id,
            Dependency.source_ref == "PL7",
        )
    ).one()
    [event] = session.scalars(
        select(DependencyEvent)
        .join(DependencyEventScope, DependencyEventScope.event_id == DependencyEvent.id)
        .where(DependencyEventScope.dependency_id == dependency.id)
    ).all()
    assert event.event_type == "commitment"


def test_a_document_with_two_readings_waits_rather_than_being_guessed(
    session, client, project
):
    """Choosing between completed runs stays a human act; the rest of the
    project loads meanwhile rather than going dark behind it."""
    from corridor.extraction_runs import record_extraction_run

    _event_cohort_lane(session, project)
    stray = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, "ucm-draft.pdf"),
        filename="ucm-draft.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(stray)
    session.flush()
    session.add(DocPage(document_id=stray.id, page_no=1, text="rows"))
    session.flush()
    for _ in range(2):
        record_extraction_run(
            session,
            stray,
            prompt_version="matrix_v1",
            candidate_count=0,
            page_errors=0,
            model="gpt-test",
            schema_version="matrix_candidate_shape_v1",
            allow_unsealed_legacy=True,
        )
    session.flush()

    result = load_project(session, project.id)
    assert result.ambiguous_documents == ["ucm-draft.pdf"]
    assert result.dependencies.admitted_count == 2


# ── The reason-led review card (#209) ────────────────────────────────────


def _disagreeing_project(session, project):
    """Two revisions that disagree about one conflict, policy-run."""
    from corridor.dependency_admission import run_dependency_admission
    from corridor.extraction_runs import (
        declare_single_run_documents,
        record_extraction_run,
    )

    if session.scalars(
        select(ExternalOrg).where(ExternalOrg.name == "Web Pipeline Co")
    ).first() is None:
        session.add(ExternalOrg(name="Web Pipeline Co", aliases=[]))
        session.flush()

    def doc(filename, doc_date):
        from datetime import date as _date

        d = Document(
            project_id=project.id,
            sha256=_document_sha(project.id, filename),
            filename=filename,
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
            doc_date=doc_date,
        )
        session.add(d)
        session.flush()
        session.add(DocPage(document_id=d.id, page_no=1, text="rows"))
        session.flush()
        return d

    def row(document, station):
        fields = {
            "utility_id": "PL7",
            "external_org": "Web Pipeline Co",
            "utility_type": "Petroleum and Gaseous Materials",
            "station_from": station,
            "station_to": station,
        }
        quote = " | ".join(fields.values())
        return Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": fields,
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": quote,
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "dedupe_hint": quote,
                "text_source": "text_layer",
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version="matrix_v1",
            model="gpt-test",
            citations_verified=True,
        )

    from datetime import date as _date

    # The generic human-settlement fixture must remain genuinely contested
    # after ADR-0061: equal document dates have no usable ordering, unlike
    # the separately covered stale-history path.
    feb = doc("ucm-feb.pdf", _date(2025, 5, 5))
    may = doc("ucm-may.pdf", _date(2025, 5, 5))
    for document, station in ((feb, "1102+20"), (may, "1105+00")):
        candidate = row(document, station)
        session.add(candidate)
        record_extraction_run(
            session,
            document,
            prompt_version="matrix_v1",
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model="gpt-test",
            schema_version="matrix_candidate_shape_v1",
            allow_unsealed_legacy=True,
        )
        session.flush()
    declare_single_run_documents(session, project.id, principal=TEST_PRINCIPAL)
    result = run_dependency_admission(session, project.id)
    # The row lands with the Dispute on it (ADR-0031), not withheld.
    assert result.admitted_count == 1
    return feb, may


def _disputed_record(session, project):
    _disagreeing_project(session, project)
    return session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()


def test_a_disagreement_lands_on_the_record_rather_than_waiting(
    session, client, project
):
    """A Dispute rides on the row (ADR-0031): the conflict is on the
    record and workable, not held back in a queue."""
    _disputed_record(session, project)

    assert "sources disagree" in client.get(f"/ledger/{project.slug}").text


def test_the_record_shows_the_disagreement_itself(session, client, project):
    dependency = _disputed_record(session, project)

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text

    # The differing field, both values — the decision, not a hint of it.
    assert "station_from" in page
    assert "1102+20" in page
    assert "1105+00" in page


def test_a_contested_disagreement_is_a_summary_with_a_bounded_timeline(
    session, client, project
):
    dependency = _disputed_record(session, project)

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text

    assert "Source discrepancy summary" in page
    assert "Why this remains contested" in page
    assert "Timeline — machine reading, not a conclusion" in page
    assert "Page image unavailable; cited text shown." in page
    assert "Record follow-up without settling" in page


def _direct_claim(
    session,
    dependency,
    *,
    filename,
    doc_date,
    value,
    doc_type="matrix",
    field_name="station_from",
    image_path=None,
    quote=None,
):
    document = Document(
        project_id=dependency.project_id,
        sha256=_document_sha(dependency.project_id, filename),
        filename=filename,
        doc_type=doc_type,
        doc_date=doc_date,
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    quote = quote or f"Recorded value: {value}"
    session.add(
        DocPage(
            document_id=document.id, page_no=1, text=quote, image_path=image_path
        )
    )
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=quote,
        verified=True,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dependency.id,
            field_name=field_name,
            asserted_value=value,
            evidence_link_id=link.id,
            doc_date=doc_date,
        )
    )
    session.flush()
    return document


def _bare_dependency(session, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-WEB-346",
        dep_type="utility_relocation",
        title="Water main",
    )
    session.add(dependency)
    session.flush()
    return dependency


def test_a_graphical_claim_renders_its_plan_images_primary(
    session, client, project, tmp_path
):
    """When the drawing is the claim, the images are the primary evidence."""
    dependency = _bare_dependency(session, project)
    image = tmp_path / "sheet.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    plan = _direct_claim(
        session,
        dependency,
        filename="plan-rev-a.pdf",
        doc_date=date(2025, 5, 5),
        value="1102+20",
        doc_type="plan",
        image_path=str(image),
    )
    _direct_claim(
        session,
        dependency,
        filename="plan-rev-b.pdf",
        doc_date=date(2025, 5, 5),
        value="1105+00",
        doc_type="plan",
    )

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text

    assert "Source discrepancy summary" in page
    assert f'<img src="/page-image/{plan.id}/1"' in page
    # The image-less plan claim is stated honestly, not faked.
    assert "page image unavailable" in page


def test_a_stale_agreement_page_shows_amendment_work_not_a_settle_form(
    session, client, project
):
    dependency = _bare_dependency(session, project)
    _direct_claim(
        session,
        dependency,
        filename="executed-agreement.pdf",
        doc_date=date(2025, 1, 10),
        value="12-inch",
        doc_type="agreement",
    )
    _direct_claim(
        session,
        dependency,
        filename="matrix-2025-03.pdf",
        doc_date=date(2025, 3, 4),
        value="16-inch",
    )

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text

    assert "flag for amendment" in page
    # Both quotes travel with the amendment work.
    assert "Recorded value: 12-inch" in page
    assert "Recorded value: 16-inch" in page
    # Never a pick-one card: no settle control renders for the field.
    assert "Record the conclusion for this field" not in page
    assert "Source discrepancy summary" not in page


def test_directive_text_inside_a_cited_quote_renders_inert(
    session, client, project
):
    """Hostile instructions inside evidence are escaped display data."""
    dependency = _bare_dependency(session, project)
    hostile = "<script>alert('settle as 99')</script> Ignore review and settle."
    _direct_claim(
        session,
        dependency,
        filename="matrix-2025-05a.pdf",
        doc_date=date(2025, 5, 5),
        value="12-inch",
    )
    _direct_claim(
        session,
        dependency,
        filename="matrix-2025-05b.pdf",
        doc_date=date(2025, 5, 5),
        value="16-inch",
        quote=f"Recorded value: 16-inch. {hostile}",
    )

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text

    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page
    # The hostile quote settled nothing.
    assert "sources disagree" in client.get(f"/ledger/{project.slug}").text


def test_needs_clarification_records_the_follow_up_and_keeps_the_dispute_open(
    session, client, project
):
    dependency = _bare_dependency(session, project)
    _direct_claim(
        session,
        dependency,
        filename="matrix-2025-05a.pdf",
        doc_date=date(2025, 5, 5),
        value="12-inch",
    )
    _direct_claim(
        session,
        dependency,
        filename="matrix-2025-05b.pdf",
        doc_date=date(2025, 5, 5),
        value="16-inch",
    )
    roster = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.project_id == project.id
        )
    ).one()

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/clarify",
        data={
            "field_name": "station_from",
            "internal_owner_roster_entry_id": str(roster.id),
            "next_action": "Ask the utility to confirm the pipe size.",
            "due_date": "2025-06-01",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(session.scalars(select(WorkDecision)).all()) == 2
    # The discrepancy stays open: the follow-up is not a settlement.
    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text
    assert "Source discrepancy summary" in page


def test_a_rejected_clarification_writes_nothing(session, client, project):
    dependency = _bare_dependency(session, project)
    _direct_claim(
        session,
        dependency,
        filename="matrix-2025-05a.pdf",
        doc_date=date(2025, 5, 5),
        value="12-inch",
    )
    _direct_claim(
        session,
        dependency,
        filename="matrix-2025-05b.pdf",
        doc_date=date(2025, 5, 5),
        value="16-inch",
    )
    other = Project(slug="other-project", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    foreign = ProjectRosterEntry(
        project_id=other.id,
        principal_subject="local:foreign",
        display_name="Foreign Member",
        can_coordinate=True,
    )
    session.add(foreign)
    session.flush()

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/clarify",
        data={
            "field_name": "station_from",
            "internal_owner_roster_entry_id": str(foreign.id),
            "next_action": "Ask the utility to confirm the pipe size.",
            "due_date": "2025-06-01",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert session.scalars(select(WorkDecision)).all() == []


def test_a_disagreement_shows_both_pages(session, client, project):
    """One pane cannot hold a comparison; a toggle makes the reviewer
    hold one value in their head while looking at the other."""
    dependency = _disputed_record(session, project)

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text

    assert "ucm-feb.pdf" in page
    assert "ucm-may.pdf" in page


def test_settling_a_dispute_closes_it_and_is_attributable(session, client, project):
    dependency = _disputed_record(session, project)

    # Both stations differ between the revisions, so both are disputed:
    # settling one leaves the other standing, which is the point.
    first = client.post(
        f"/ledger/{project.slug}/{dependency.id}/settle",
        data={"field_name": "station_from", "value": "1105+00"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert (
        "sources disagree on station_to"
        in client.get(f"/ledger/{project.slug}/{dependency.id}").text
    )

    client.post(
        f"/ledger/{project.slug}/{dependency.id}/settle",
        data={"field_name": "station_to", "value": "1105+00"},
        follow_redirects=False,
    )

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text
    assert "sources disagree" not in page
    assert "Conclusion recorded" in page
    # Both claims survive the settlement: nothing is erased.
    assert "1102+20" in page and "1105+00" in page

    entry = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == dependency.id,
            AuditLog.action == "settle_dispute",
        )
        .order_by(AuditLog.id.desc())
    ).first()
    assert entry is not None
    assert entry.actor == TEST_PRINCIPAL.subject


def test_a_later_revision_disagreeing_again_reopens_the_dispute(
    session, client, project
):
    """A reviewer settled the disagreement in front of them, not every
    disagreement the field will ever have."""
    from corridor.disputes import settle_dispute
    from corridor.models import Assertion, EvidenceLink

    dependency = _disputed_record(session, project)
    for name in ("station_from", "station_to"):
        settle_dispute(
            session,
            dependency.id,
            name,
            value="1105+00",
            principal=TEST_PRINCIPAL,
        )
    assert (
        "sources disagree"
        not in client.get(f"/ledger/{project.slug}/{dependency.id}").text
    )

    # A third revision states something else, cited and verified.
    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).first()
    session.add(
        Assertion(
            dependency_id=dependency.id,
            field_name="station_from",
            asserted_value="1108+40",
            evidence_link_id=link.id,
        )
    )
    session.flush()

    assert (
        "sources disagree" in client.get(f"/ledger/{project.slug}/{dependency.id}").text
    )


def test_settling_a_field_nobody_disputes_refuses(session, client, project):
    dependency = _disputed_record(session, project)

    refused = client.post(
        f"/ledger/{project.slug}/{dependency.id}/settle",
        data={"field_name": "utility_type", "value": "anything"},
        follow_redirects=False,
    )
    assert refused.status_code == 409


# ── Misread rows ride the same list, flagged and last (#212) ─────────────


def test_an_unverifiable_row_is_offered_rather_than_withheld(
    session, client, project, document
):
    """A quote that could not be found on its page is a signal, not
    noise: the row reaches a human instead of disappearing."""
    make_candidate(session, project, document, uid="BAD-1", verified=False)
    load_project(session, project.id)

    page = client.get(f"/queue/{project.slug}").text
    assert "BAD-1" in page
    assert "Source passage check failed" in page


def test_a_row_with_no_identifier_is_offered_with_its_reason(
    session, client, project, document
):
    make_candidate(session, project, document, uid="", verified=True)
    load_project(session, project.id)

    page = client.get(f"/queue/{project.slug}").text
    # The machine's vocabulary is `no_utility_id`; the reviewer is told
    # what they are deciding instead.
    assert "This row has no identifier." in page
    assert "no_utility_id" not in page


def test_flagged_rows_sink_below_clean_work(session, client, project, document):
    """A reviewer meets the rows they can act on first."""
    # Both unverifiable, so both stay for a human; the one whose quote
    # holds is served before the one whose does not.
    make_candidate(
        session,
        project,
        document,
        uid="BAD-1",
        verified=False,
        auto_active_run=False,
    )
    make_candidate(
        session, project, document, uid="GOOD-1", unverified_fields=["notes"]
    )

    served = next_candidate(session, project.id)
    assert served.payload_json["fields"]["utility_id"] == "GOOD-1"


def test_correcting_a_flagged_row_against_its_page_clears_the_flag(
    session, client, project, document
):
    """The page says GOOD-1; the extractor read BAD-1. Correcting it
    re-runs verification rather than taking the reviewer's word."""
    candidate = make_candidate(session, project, document, uid="BAD-1", verified=False)
    assert candidate.citations_verified is False

    response = client.post(
        f"/candidates/{candidate.id}/edit-accept",
        data={
            "slug": project.slug,
            "field_utility_id": "GOOD-1",
            "field_external_org": "AT&T Texas (SWBT)",
            "field_station_from": "1149+00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    session.refresh(candidate)
    assert candidate.citations_verified is True
    assert candidate.state == "accepted"

    entry = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "candidate",
            AuditLog.entity_id == candidate.id,
            AuditLog.action == "edit_candidate",
        )
        .order_by(AuditLog.id.desc())
    ).first()
    assert entry is not None and entry.actor == TEST_PRINCIPAL.subject


def test_no_separate_lane_holds_the_unverified(session, client, project, document):
    """One list. A flagged row is reachable from the ordinary queue with
    no filter, tab, or mode to find first."""
    make_candidate(session, project, document, uid="BAD-1", verified=False)
    load_project(session, project.id)

    assert "BAD-1" in client.get(f"/queue/{project.slug}").text


# ── The pile of statements the machine could not place (#213) ────────────


def _unplaced_statement(session, project):
    """One conflict on the record and one statement naming another."""
    from corridor.extraction_runs import record_extraction_run

    if session.scalars(
        select(ExternalOrg).where(ExternalOrg.name == "Web Pipeline Co")
    ).first() is None:
        session.add(ExternalOrg(name="Web Pipeline Co", aliases=[]))
        session.flush()

    matrix = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, "ucm-statements.pdf"),
        filename="ucm-statements.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    minutes = Document(
        project_id=project.id,
        sha256=_document_sha(project.id, "minutes-statements.pdf"),
        filename="minutes-statements.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    for document in (matrix, minutes):
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text="rows"))
    session.flush()

    def candidate(document, kind, fields, prompt):
        c = Candidate(
            project_id=project.id,
            kind=kind,
            payload_json={
                "kind": kind,
                "fields": fields,
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": "rows",
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "dedupe_hint": f"{kind}|{fields.get('utility_id') or fields.get('conflict_ref')}",
                "text_source": "text_layer",
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version=prompt,
            model="gpt-test",
            citations_verified=True,
        )
        session.add(c)
        return c

    conflict = candidate(
        matrix,
        "dependency",
        {
            "utility_id": "PL1",
            "external_org": "Web Pipeline Co",
            "utility_type": "Petroleum and Gaseous Materials",
            "station_from": "1102+20",
            "station_to": "1102+80",
        },
        "matrix_v1",
    )
    statement = candidate(
        minutes,
        "event",
        {
            "event_type": "commitment",
            "description": "Web Pipeline committed on the crossing",
            "external_org": "Web Pipeline Co",
            "stated_party": "Web Pipeline Co",
            "event_date": "2025-01-16",
            "committed_date": "2025-06-01",
            "conflict_ref": "PL99",
        },
        "minutes_v1",
    )
    session.flush()
    for document, cs, prompt in (
        (matrix, [conflict], "matrix_v1"),
        (minutes, [statement], "minutes_v1"),
    ):
        record_extraction_run(
            session,
            document,
            prompt_version=prompt,
            candidate_count=len(cs),
            page_errors=0,
            candidates=cs,
            model="gpt-test",
            schema_version="matrix_candidate_shape_v1",
            allow_unsealed_legacy=True,
        )
    session.flush()
    load_project(session, project.id)
    return statement


def test_the_pile_names_what_each_statement_needs(session, client, project):
    _unplaced_statement(session, project)

    page = client.get(f"/statements/{project.slug}").text
    assert "This statement names a conflict the record does not have." in page
    # The reviewer sees what was actually said, not a candidate id.
    assert "Web Pipeline committed on the crossing" in page
    assert "2025-01-16" in page


def test_ordinary_statement_review_records_pre_shadow_start_without_exposing_packet(
    session, client, project
):
    candidate = _unplaced_statement(session, project)

    response = client.get(f"/statements/{project.slug}/{candidate.id}/coordinate")

    assert response.status_code == 200
    start = session.scalar(
        select(EvidenceInvestigationCandidateReviewStart).where(
            EvidenceInvestigationCandidateReviewStart.candidate_id == candidate.id
        )
    )
    assert start is not None
    assert start.principal == TEST_PRINCIPAL.subject
    assert "Evidence Investigator" not in response.text
    assert "dependency_options" not in response.text


def test_the_queue_points_at_the_pile_without_becoming_it(session, client, project):
    _unplaced_statement(session, project)

    page = client.get(f"/queue/{project.slug}").text
    assert f"/statements/{project.slug}" in page
    assert "1 statement to review" in page


def test_retired_attach_from_the_pile_hands_off_without_mutating(session, client, project):

    _unplaced_statement(session, project)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    candidate = session.scalars(
        select(Candidate).where(
            Candidate.project_id == project.id,
            Candidate.kind == "event",
        )
    ).one()

    before_events = session.scalars(select(DependencyEvent)).all()
    response = client.post(
        f"/projects/{project.slug}/statements/{candidate.id}/attach",
        data={"dependency_ref": dependency.ref_code, "dependency_id": str(dependency.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/statements/{project.slug}/{candidate.id}/coordinate"
    )
    assert session.scalars(select(DependencyEvent)).all() == before_events
    session.refresh(candidate)
    assert candidate.state == "pending"


def test_retired_attach_ignores_a_forged_reference(session, client, project):
    _unplaced_statement(session, project)
    candidate = session.scalars(
        select(Candidate).where(
            Candidate.project_id == project.id, Candidate.kind == "event"
        )
    ).one()

    before_events = session.scalars(select(DependencyEvent)).all()
    refused = client.post(
        f"/projects/{project.slug}/statements/{candidate.id}/attach",
        data={"dependency_ref": "DEP-99999"},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert refused.headers["location"] == (
        f"/statements/{project.slug}/{candidate.id}/coordinate"
    )
    session.refresh(candidate)
    assert candidate.state == "pending"
    assert session.scalars(select(DependencyEvent)).all() == before_events


def test_tossing_a_statement_returns_to_the_pile(session, client, project):
    _unplaced_statement(session, project)
    candidate = session.scalars(
        select(Candidate).where(
            Candidate.project_id == project.id, Candidate.kind == "event"
        )
    ).one()

    response = client.post(
        f"/candidates/{candidate.id}/reject",
        data={
            "slug": project.slug,
            "reason": "irrelevant",
            "redirect_to": f"/statements/{project.slug}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/statements/{project.slug}"

    session.refresh(candidate)
    assert candidate.state == "rejected"
    assert "Nothing waiting" in client.get(f"/statements/{project.slug}").text


# ── Dismissing junk, kept in history (#214) ──────────────────────────────


def _record_on_the_list(session, project, document):
    candidate = make_candidate(session, project, document, uid="FOC1-1")
    load_project(session, project.id)
    return session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()


def test_dismissing_takes_a_record_off_the_working_list(
    session, client, project, document
):
    dependency = _record_on_the_list(session, project, document)
    assert dependency.ref_code in client.get(f"/ledger/{project.slug}").text

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert dependency.ref_code not in client.get(f"/ledger/{project.slug}").text


def test_a_dismissed_record_stops_raising_exceptions(
    session, client, project, document
):
    """A record nobody is working raises no exceptions about nobody
    working it."""
    from corridor.exceptions import evaluate

    dependency = _record_on_the_list(session, project, document)
    assert evaluate(session, project.id) != []

    client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "not-a-conflict"},
        follow_redirects=False,
    )
    assert evaluate(session, project.id) == []


def test_dismissing_preserves_the_row_its_evidence_and_its_history(
    session, client, project, document
):
    from corridor.models import DependencyDismissal

    dependency = _record_on_the_list(session, project, document)
    before = len(
        session.scalars(
            select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
        ).all()
    )

    client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "wrong"},
        follow_redirects=False,
    )

    session.refresh(dependency)
    assert dependency.dismissed_at is not None
    assert (
        len(
            session.scalars(
                select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
            ).all()
        )
        == before
    )
    # And the record still says why it left, with a name on it.
    [dismissal] = session.scalars(
        select(DependencyDismissal).where(
            DependencyDismissal.dependency_id == dependency.id
        )
    ).all()
    assert dismissal.reason == "wrong"
    assert dismissal.dismissed_by == TEST_PRINCIPAL.subject
    # The detail page is still reachable for anyone asking why.
    assert client.get(f"/ledger/{project.slug}/{dependency.id}").status_code == 200


def test_an_unknown_dismiss_reason_refuses(session, client, project, document):
    dependency = _record_on_the_list(session, project, document)

    refused = client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "because-i-said-so"},
        follow_redirects=False,
    )
    assert refused.status_code == 400
    session.refresh(dependency)
    assert dependency.dismissed_at is None


def test_dismissing_twice_refuses(session, client, project, document):
    dependency = _record_on_the_list(session, project, document)
    client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )

    again = client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )
    assert again.status_code == 409


# ── The list answers "who owns it, and what's next" ──────────────────────


def _coordinated_record(session, client, project, document, *, owner, action):
    dependency = _record_on_the_list(session, project, document)
    _save_plan(
        client,
        session,
        project,
        dependency.id,
        owner=owner,
        next_action=action,
        due_date="2026-01-05",
    )
    session.refresh(dependency)
    return dependency


def test_the_list_shows_who_owns_it_and_what_is_next(
    session, client, project, document
):
    """The coordinator's first question, answerable without opening a
    single row."""
    _coordinated_record(
        session,
        client,
        project,
        document,
        owner="Dana Reyes",
        action=PLAN_ACTION,
    )

    page = client.get(f"/ledger/{project.slug}").text
    assert "Dana Reyes" in page
    assert PLAN_ACTION in page
    assert "by 2026-01-05" in page


def test_the_list_can_be_narrowed_to_one_persons_work(
    session, client, project, document
):
    _coordinated_record(
        session, client, project, document, owner="Dana Reyes", action=PLAN_ACTION
    )

    mine = client.get(f"/ledger/{project.slug}?owner=Dana+Reyes").text
    assert "Dana Reyes" in mine
    assert "1 shown" in mine

    someone_else = client.get(f"/ledger/{project.slug}?owner=Sam+Okafor").text
    assert "0 shown" in someone_else


def test_the_list_can_be_narrowed_to_what_nobody_owns(
    session, client, project, document
):
    """What has nobody is the question a coordinator asks first."""
    _record_on_the_list(session, project, document)

    unassigned = client.get(f"/ledger/{project.slug}?owner=unassigned").text
    assert "1 shown" in unassigned


def test_an_assigned_record_leaves_the_unassigned_list(
    session, client, project, document
):
    _coordinated_record(
        session, client, project, document, owner="Dana Reyes", action=PLAN_ACTION
    )

    unassigned = client.get(f"/ledger/{project.slug}?owner=unassigned").text
    assert "0 shown" in unassigned


def test_the_owner_filter_offers_only_people_this_project_assigned(
    session, client, project, document
):
    _coordinated_record(
        session, client, project, document, owner="Dana Reyes", action=PLAN_ACTION
    )

    page = client.get(f"/ledger/{project.slug}").text
    assert '<option value="Dana Reyes"' in page
    assert '<option value="unassigned"' in page


def test_a_dismissed_record_is_not_offered_as_someones_work(
    session, client, project, document
):
    dependency = _coordinated_record(
        session, client, project, document, owner="Dana Reyes", action=PLAN_ACTION
    )
    client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )

    page = client.get(f"/ledger/{project.slug}?owner=Dana+Reyes").text
    assert "0 shown" in page


# ── Dismissal holds at every door (re-review round) ──────────────────────


def test_a_dismissed_record_refuses_a_merge(session, client, project, document):
    """The offer list already excluded dismissed targets; the mutation
    must too, or a stale form files a claim where nothing looks again."""
    dependency = _record_on_the_list(session, project, document)
    client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )
    candidate = make_candidate(
        session, project, document, uid="FOC1-2", station_from="1150+00"
    )

    refused = client.post(
        f"/candidates/{candidate.id}/merge",
        data={"slug": project.slug, "dependency_id": str(dependency.id)},
        follow_redirects=False,
    )
    assert refused.status_code == 409
    session.refresh(candidate)
    assert candidate.state == "pending"


def test_a_dismissed_record_refuses_work_decisions(session, client, project, document):
    dependency = _record_on_the_list(session, project, document)
    client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "not-a-conflict"},
        follow_redirects=False,
    )

    refused = _save_plan(client, session, project, dependency.id, owner="Dana Reyes")
    assert refused.status_code == 400
    session.refresh(dependency)
    assert dependency.internal_owner is None


def test_a_dismissed_record_leaves_the_reviewer_worklist(
    session, client, project, document
):
    """A worklist must not offer work on a record that was thrown out."""
    from corridor.supersession_review import build_reviewer_worklist

    dependency = _record_on_the_list(session, project, document)
    client.post(
        f"/ledger/{project.slug}/{dependency.id}/dismiss",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )

    worklist = build_reviewer_worklist(session, project.id)
    named = {
        review.dependency_id
        for review in (*worklist.ordinary, *worklist.reconfirmation)
        if getattr(review, "dependency_id", None)
    }
    assert dependency.id not in named


def test_same_document_replay_residual_has_human_review_copy():
    from corridor.web.app import REVIEW_REASONS

    headline, guidance = REVIEW_REASONS["same_document_replay_unproven"]
    assert headline == (
        "This source row was previously handled, but safe replay is not proven."
    )
    assert "current Constraint association" in guidance
    assert "review the cited passages" in guidance


def test_frontend_request_receipt_uses_response_status_and_type_tagged_hashes(
    session, project
):
    from starlette.responses import Response

    from corridor.frontend_request_receipts import (
        FrontendRequestSubject,
        record_frontend_request,
        request_fields_sha256,
    )

    assert request_fields_sha256({"value": None}) != request_fields_sha256(
        {"value": ""}
    )
    empty_bytes_tag = (
        "bytes-sha256:"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert request_fields_sha256({"value": b""}) != request_fields_sha256(
        {"value": empty_bytes_tag}
    )
    assert request_fields_sha256([("b", "2"), ("a", "1")]) == (
        request_fields_sha256([("a", "1"), ("b", "2")])
    )
    assert request_fields_sha256({"member": ["2", "1"]}) == (
        request_fields_sha256({"member": ["1", "2"]})
    )

    secret = "project-person private free text"
    entry = record_frontend_request(
        session,
        principal=TEST_PRINCIPAL,
        route_name="correct_statement_scope_from_screen",
        route_template="/statements/{slug}/{candidate_id}/correct/scope",
        method="POST",
        response=Response(status_code=409),
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields={"private": secret},
    )

    assert entry.after_json["status"] == 409
    assert entry.after_json["schema_version"].endswith(".v2")
    assert secret not in json.dumps(entry.after_json)

    with pytest.raises(ValueError, match="registered route"):
        record_frontend_request(
            session,
            principal=TEST_PRINCIPAL,
            route_name="correct_statement_scope_from_screen",
            route_template="/wrong-template",
            method="POST",
            response=Response(status_code=409),
            subject=FrontendRequestSubject(project_id=project.id),
        )


@pytest.mark.parametrize("invalid_id", [True, 1.5, "1", 0, -1])
def test_frontend_request_subject_refuses_non_positive_integer_ids(invalid_id):
    from corridor.frontend_request_receipts import FrontendRequestSubject

    with pytest.raises((TypeError, ValueError)):
        FrontendRequestSubject(project_id=invalid_id).as_json()  # type: ignore[arg-type]
