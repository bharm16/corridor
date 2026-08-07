import json
from copy import deepcopy
from datetime import date
from hashlib import sha256

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
from corridor.automatic_carry_forward import authorize_automatic_carry_forward
from corridor.config import settings
from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    Project,
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

TEST_PRINCIPAL = HumanPrincipal("local:test-reviewer")


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
def client_without_principal(session, monkeypatch):
    """Exercise the real fail-closed deployment identity dependency."""
    monkeypatch.setattr(settings, "human_principal", "")
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
        )
        declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)
    return c


def test_queue_shows_the_next_pending_candidate(client, session, project, document):
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "AT&amp;T Texas (SWBT)" in r.text
    assert "1149+00" in r.text


def test_queue_rejects_an_unknown_review_lane(client, project):
    r = client.get(f"/queue/{project.slug}?lane=not-a-review-lane")

    assert r.status_code == 422


def test_queue_exposes_two_counted_exclusive_review_lanes(client, project):
    r = client.get(f"/queue/{project.slug}")

    assert r.status_code == 200
    assert (
        f'href="/queue/{project.slug}?lane=candidate" aria-current="page"'
        in r.text
    )
    assert f'href="/queue/{project.slug}?lane=reconfirmation"' in r.text
    assert "Candidate Adjudication (0)" in r.text
    assert "Reconfirmation (0)" in r.text


def test_candidate_review_keeps_lane_navigation_visible(
    client, session, project, document
):
    make_candidate(session, project, document)

    r = client.get(f"/queue/{project.slug}?lane=candidate")

    assert r.status_code == 200
    assert "Candidate Adjudication (1)" in r.text
    assert "Reconfirmation (0)" in r.text
    assert (
        f'href="/queue/{project.slug}?lane=candidate" '
        'aria-current="page"' in r.text
    )


def _seed_waiting_supersession_review(session, project, predecessor):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-WAITING-REVISION",
        dep_type="utility_relocation",
        title="Telecom — waiting for current revision",
        status="identified",
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
    assert "Candidate Adjudication (1)" in r.text
    assert "Reconfirmation (0)" in r.text
    assert dependency.ref_code in r.text
    assert document.filename in r.text
    assert successor.filename in r.text
    assert "Awaiting extraction" in r.text
    assert "No successor extraction attempt exists" in r.text
    assert "Reconfirmation unavailable" in r.text
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
    )
    session.flush()

    r = client.get(f"/queue/{project.slug}?lane=candidate")

    assert r.status_code == 200
    assert dependency.ref_code in r.text
    assert "Extraction failed" in r.text
    assert "completed Active Run is required" in r.text
    assert "Reconfirmation unavailable" in r.text
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
    assert "Candidate Adjudication (2)" in r.text
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
    assert "Candidate Adjudication (1)" in r.text
    assert "Reconfirmation (0)" in r.text
    assert (
        f'href="/queue/{project.slug}?lane=reconfirmation" '
        'aria-current="page"' in r.text
    )
    assert "1 item(s) remain in Candidate Adjudication" in r.text
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
    )
    declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)

    assert next_candidate(session, project.id).id == good.id

    good.state = "accepted"
    session.flush()
    assert next_candidate(session, project.id).id == bad.id

    r = client.get(f"/queue/{project.slug}")
    assert "Citation unverified" in r.text


def test_editing_a_whole_row_candidate_updates_queue_counts_and_order(
    session, project, document
):
    page = session.scalars(
        select(DocPage).where(
            DocPage.document_id == document.id, DocPage.page_no == 1
        )
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


def test_a_verified_candidate_is_labelled_verified(client, session, project, document):
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}")
    assert "Citation verified" in r.text
    assert "Citation unverified" not in r.text


def test_merge_is_unavailable_when_there_is_nothing_to_merge_into(
    client, session, project, document
):
    """No existing dependency for this party means no merge, not a bad one."""
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}")
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

    r = client.get(f"/queue/{project.slug}")
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
    )
    declare_active_run(session, predecessor.id, predecessor_run.id, principal=TEST_PRINCIPAL)

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
        )
    else:
        successor_run = record_extraction_run(
            session,
            successor,
            prompt_version="txdot_ucm_v1",
            candidate_count=0,
            page_errors=0,
            model="gpt-4o-mini",
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
        "activate_successor": lambda: declare_active_run(session, successor.id, successor_run.id, principal=TEST_PRINCIPAL),
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
    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "Queue empty" in r.text


def test_queue_stays_empty_when_successor_extraction_failed(client, session, project):
    _seed_supersession_chain(
        session,
        project,
        include_successor_candidate=False,
        successor_failed=True,
    )

    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "Queue empty" in r.text
    assert "PRE-ONLY" not in r.text
    assert "Every candidate" not in r.text


def test_queue_selects_successor_when_active_run_is_declared(client, session, project):
    chain = _seed_supersession_chain(session, project)
    chain["activate_successor"]()
    r = client.get(f"/queue/{project.slug}")
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
    assert "Candidate Adjudication (0)" in candidate_lane.text
    assert "Reconfirmation (1)" in candidate_lane.text
    assert reconfirm_lane.status_code == 200
    body = reconfirm_lane.text
    assert chain["dependency"].ref_code in body
    assert chain["predecessor"].filename in body
    assert chain["successor"].filename in body
    assert "Mechanically verified unchanged" in body
    assert "<kbd>c</kbd> Reconfirm operative support" in body
    assert (
        f'action="/supersession-review/{chain["dependency"].id}/reconfirm"'
        in body
    )
    assert (
        f'name="predecessor_document_id" value="{chain["predecessor"].id}"'
        in body
    )
    assert (
        f'name="successor_candidate_id" value="{chain["successor_candidate"].id}"'
        in body
    )
    assert (
        f'name="comparison_id" value="{chain["comparison"].id}"' in body
    )
    assert 'name="finding_id" value="' in body
    assert 'name="scope_fingerprint" value=' in body
    assert ">accept<" not in body
    assert ">reject<" not in body


def test_queue_shows_read_only_automatic_carry_forward_policy_status(
    client, session, project
):
    _seed_reconfirmation_ready_chain(session, project)
    authorize_automatic_carry_forward(
        session,
        project.id,
        principal=TEST_PRINCIPAL,
    )

    response = client.get(f"/queue/{project.slug}?lane=reconfirmation")

    assert response.status_code == 200
    assert "Automatic Carry-Forward" in response.text
    assert "automatic-carry-forward-v1" in response.text
    assert TEST_PRINCIPAL.subject in response.text
    assert "1 eligible" in response.text
    assert "0 carried" in response.text
    assert "automatic-carry-forward-abstentions-v1" in response.text


def test_queue_shows_versioned_automatic_abstention_counts(
    client, project, monkeypatch
):
    class Status:
        enabled = True
        policy_current = True
        policy_approval_id = 12
        policy_version = "automatic-carry-forward-v1"
        policy_sha256 = "ab" * 32
        approved_by = TEST_PRINCIPAL.subject
        carried_count = 4
        eligible_count = 0
        abstention_reason_version = (
            "automatic-carry-forward-abstentions-v1"
        )
        abstention_counts = {"comparison_changed": 3}

    monkeypatch.setattr(
        "corridor.web.app.automatic_carry_forward_status",
        lambda _session, _project_id, **_kwargs: Status(),
    )

    response = client.get(f"/queue/{project.slug}")

    assert response.status_code == 200
    assert "3 comparison changed" in response.text
    assert "4 carried" in response.text
    assert "automatic-carry-forward-abstentions-v1" in response.text


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
    before = client.get(
        f"/ledger/{project.slug}/{chain['dependency'].id}"
    )
    assert "SUPERSEDED_CITATION" in before.text

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
    assert "Candidate Adjudication (0)" in ordinary_after.text
    assert "FOC1-1" not in ordinary_after.text
    detail = client.get(f"/ledger/{project.slug}/{chain['dependency'].id}")
    assert "SUPERSEDED_CITATION" not in detail.text
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
    assert "Reconfirm operative support" in review.text
    detail = client.get(f"/ledger/{project.slug}/{chain['dependency'].id}")
    assert "SUPERSEDED_CITATION" in detail.text
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
    )
    chain["activate_successor"]()

    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "SUCC-ONLY" in r.text
    assert "EXPERIMENTAL" not in r.text


def test_queue_prefers_historical_document_when_override_is_set(
    client, session, project
):
    chain = _seed_supersession_chain(session, project)
    chain["activate_successor"]()
    active = client.get(f"/queue/{project.slug}")
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
        status="identified",
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


def test_authoritative_mutation_api_rejects_an_inactive_successor_run(
    session, project
):
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
        status="identified",
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
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_non_mapping_payload_returns_400_without_writes(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    candidate.payload_json = []
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_non_mapping_fields_returns_400_without_writes(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    candidate.payload_json = {**candidate.payload_json, "fields": []}
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
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
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
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
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
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
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
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
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
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
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
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
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all() == []
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
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all() == []
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
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all() == []
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
    response = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": other.slug},
        follow_redirects=False,
    )
    assert response.status_code == 303
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
    session.refresh(evidence)
    assert evidence.satisfies_requirement is False
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
    assert candidate.state == "accepted"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all()


@pytest.mark.parametrize("action", ["accept", "edit-accept", "merge", "reject"])
def test_mutation_routes_refuse_without_a_configured_human_principal_and_write_nothing(
    client_without_principal, session, project, document, action
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
            status="identified",
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

    response = client_without_principal.post(
        f"/candidates/{candidate.id}/{action}",
        data=data,
        follow_redirects=False,
    )

    assert response.status_code == 503
    session.refresh(candidate)
    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert candidate.payload_json == original_payload
    assert {
        model: session.scalar(select(func.count()).select_from(model))
        for model in before
    } == before


def test_mark_satisfies_route_refuses_without_a_configured_human_principal(
    client_without_principal, session, project, document
):
    candidate = make_candidate(session, project, document)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).first()
    assert dependency is None

    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    try:
        accepted = client_without_principal.post(
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

    response = client_without_principal.post(
        f"/dependencies/{dependency.id}/evidence/{evidence.id}/satisfies",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    assert response.status_code == 503
    session.refresh(evidence)
    assert evidence.satisfies_requirement is False
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_edit_then_accept_records_the_edited_values(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/edit-accept",
        data={
            "slug": project.slug,
            "field_utility_id": "FOC1-1",
            "field_external_org": "AT&T Texas",
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
    before_dependencies = session.scalar(
        select(func.count()).select_from(Dependency)
    )
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
    assert session.scalar(select(func.count()).select_from(Dependency)) == before_dependencies
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence


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
    rich_candidate(session, project, document, tier="transcribe",
                   text_source="ocr")

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
        session, project, document,
        unmapped_columns=["Retain and Protect", "Abandon / Deactivate"],
    )

    view = build_view(session, next_candidate(session, project.id))

    assert view.unmapped_columns == ["Retain and Protect", "Abandon / Deactivate"]


def test_an_unverified_row_names_the_field_that_failed(
    session, project, document
):
    """"Unverified" that names the suspect value instead of only sinking
    the row. A reviewer who cannot see *which* field is unsupported has to
    re-verify all of them."""
    rich_candidate(
        session, project, document,
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
        session, project, document,
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
    assert f'/page-image/{document.id}/1' not in body


def test_a_pdf_candidate_still_shows_its_page_image(
    client, session, project, document
):
    """The common case is untouched — a printout has a rendering and the
    quote is shown against it."""
    make_candidate(session, project, document)

    body = client.get(f"/queue/{project.slug}").text

    assert f'/page-image/{document.id}/1' in body


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


def two_overdue(session, project):
    """Two overdue records; one critical, one whose document said nothing."""
    from datetime import date, timedelta

    deps = []
    for ref, strategy in (("DEP-CRIT", "relocate"), ("DEP-PLAIN", None)):
        dep = Dependency(
            project_id=project.id,
            ref_code=ref,
            dep_type="utility_relocation",
            title=f"Telecom — {ref}",
            status="committed",
            resolution_strategy=strategy,
            committed_date=date.today() - timedelta(days=40),
            internal_owner="Bryce",
        )
        session.add(dep)
        deps.append(dep)
    session.flush()
    return deps


def test_ledger_pills_carry_the_rules_own_days(client, session, project):
    """"OVERDUE 40d" is a fact a reviewer can check against the record;
    the score it replaces was not."""
    two_overdue(session, project)

    body = client.get(f"/ledger/{project.slug}").text

    assert "OVERDUE 40d" in body


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


def test_the_dependency_view_states_days_beside_each_rule(
    client, session, project
):
    deps = two_overdue(session, project)

    body = client.get(f"/ledger/{project.slug}/{deps[0].id}").text

    assert "OVERDUE" in body
    assert "40d" in body


def test_a_zero_day_quantity_still_renders(client, session, project):
    """"Needed in 0 days" is due today, and 0 is not None: a truthiness
    check would have swallowed exactly the row a reviewer most needs."""
    from datetime import date

    dep = Dependency(
        project_id=project.id,
        ref_code="DEP-TODAY",
        dep_type="utility_relocation",
        title="Telecom — DEP-TODAY",
        status="committed",
        need_date=date.today(),
        committed_date=date.today(),
        internal_owner="Bryce",
    )
    session.add(dep)
    session.flush()

    body = client.get(f"/ledger/{project.slug}").text

    assert "DUE_SOON 0d" in body


def test_superseded_citation_is_visible_as_reconfirmation_work(
    client, session, project, document
):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-PROVENANCE-REVIEW",
        dep_type="utility_relocation",
        title="Telecom — provenance review",
        status="identified",
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
        assert "SUPERSEDED_CITATION 0d · re-confirmation" in body
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
    )
    declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)
    return c


def test_the_queue_refuses_to_offer_accept_on_an_event(session, project, document):
    """The same treatment merge gets when there is nothing to merge into.

    `make minutes` writes events into this queue; accepting one built a
    Dependency out of fields it does not have.
    """
    _event_candidate(session, project, document)

    view = build_view(session, next_candidate(session, project.id))

    assert "not a dependency" in view.accept_refused
    assert "merging" in view.accept_refused


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
