from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.adjudicate import accept_candidate, merge_candidate
from corridor.db import Session, engine
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.exceptions import evaluate_project
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import (
    NoSuchEvidence,
    UnverifiedEvidence,
    browse,
    load_dependency,
    mark_satisfies,
    primary_evidence,
)
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventTiming,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.operative_support import (
    designate_publication_support,
    resolve_operative_support,
)
from corridor.principals import HumanPrincipal
from corridor.supersession import SupersessionDeclaration, register_supersessions
from corridor.web.app import app, get_human_principal, get_session

TEST_PRINCIPAL = HumanPrincipal("local:ledger-reviewer")

FIELDS = {
    "utility_id": "FOC1-1",
    "external_org": "LT AT&T Texas",
    "utility_type": "Telecom",
    "station_from": "1149+00",
    "station_to": "1153+17",
}


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
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    p = Project(slug="ledger-test", name="Ledger Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def document(session, project):
    d = Document(
        project_id=project.id,
        sha256="f" * 64,
        filename="nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=28,
        doc_date=date(2026, 2, 13),
    )
    session.add(d)
    session.flush()
    session.add(
        DocPage(
            document_id=d.id,
            page_no=1,
            text="FOC1-1 LT AT&T Texas Telecom",
            image_path="/tmp/corridor-missing-page.png",
        )
    )
    session.flush()
    return d


def make_candidate(
    session, project, document, *, fields=None, verified=True, citations=None
):
    c = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields or FIELDS,
            "citations": citations
            or [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "FOC1-1 LT AT&T Texas Telecom",
                    "verified": verified,
                }
            ],
            "confidence": 1.0,
            "dedupe_hint": "x",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=verified,
    )
    session.add(c)
    session.flush()
    run = record_extraction_run(
        session,
        document,
        prompt_version=c.prompt_version,
        candidate_count=1,
        page_errors=0,
        candidates=(c,),
        model=c.model,
    )
    declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)
    session.flush()
    return c


@pytest.fixture
def dependency(session, project, document):
    return accept_candidate(
        session,
        make_candidate(session, project, document),
        principal=TEST_PRINCIPAL,
    )


# ------------------------------------------------------------------- browse


def _browse(session, project, **filters):
    """Browse at a reading taken now.

    `browse` requires its evaluation rather than defaulting to a fresh one,
    so a caller that already holds one cannot silently pay for a second
    against a second clock. These tests hold none, and say so here once.
    """
    return browse(
        session,
        project.id,
        evaluation=evaluate_project(session, project.id),
        **filters,
    )


def test_browse_refuses_an_evaluation_from_another_project(session, project):
    foreign = Project(slug="ledger-foreign", name="Foreign", is_synthetic=True)
    session.add(foreign)
    session.flush()

    with pytest.raises(ValueError, match="evaluation belongs to another project"):
        browse(session, project.id, evaluation=evaluate_project(session, foreign.id))


def test_browse_lists_dependencies_with_their_backing(session, project, dependency):
    [row] = _browse(session, project)
    assert row.dependency.id == dependency.id
    assert row.org_name == "LT AT&T Texas"
    assert row.assertion_count == len(FIELDS)
    assert row.evidence_count == 1
    assert row.verified_evidence_count == 1
    assert row.is_ready is False
    assert row.contradicted is False


def test_a_link_that_does_not_hold_is_still_backing_but_not_evidence(
    session, project, dependency
):
    """Two facts the Backing column and the report each want a different one of.

    The ledger page's "1e" means "one link behind this record", which is
    what a reviewer chasing a bad citation needs to see. A published
    figure saying "evidence" means the ones that hold.
    """
    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    link.verified = False
    session.flush()

    [row] = _browse(session, project)

    assert row.evidence_count == 1
    assert row.verified_evidence_count == 0


def test_browse_filters_by_status(session, project, dependency):
    assert len(_browse(session, project, status="identified")) == 1
    assert _browse(session, project, status="closed") == []


def test_browse_filters_by_readiness(session, project, dependency):
    """Readiness is computed, so this filter cannot be a WHERE clause."""
    assert _browse(session, project, ready=True) == []
    assert len(_browse(session, project, ready=False)) == 1

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    mark_satisfies(session, dependency.id, link.id, principal=TEST_PRINCIPAL)

    assert len(_browse(session, project, ready=True)) == 1
    assert _browse(session, project, ready=False) == []


def test_browse_flags_contradicted_records(session, project, document, dependency):
    merge_candidate(
        session,
        make_candidate(
            session, project, document, fields={**FIELDS, "station_from": "1160+00"}
        ),
        dependency,
        principal=TEST_PRINCIPAL,
    )
    [row] = _browse(session, project)
    assert row.contradicted is True


def test_browse_and_detail_read_the_current_statement_projection(
    session, project, dependency
):
    """A current event stays visible even before its scalar refresh runs."""
    event = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=dependency.external_org_id,
        stated_external_org_id=dependency.external_org_id,
        scope_mode="selected",
        event_type="commitment",
        source_kind="verbal",
        stated_party="LT AT&T Texas",
        event_date=date(2026, 3, 4),
        description="AT&T said relocation will finish in August.",
        created_by="local:ledger-reviewer",
    )
    session.add(event)
    session.flush()
    session.add_all(
        (
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text="2026-08-15",
                precision="day",
                start_date=date(2026, 8, 15),
                end_date=date(2026, 8, 15),
            ),
            DependencyEventScope(event_id=event.id, dependency_id=dependency.id),
        )
    )
    session.flush()
    assert dependency.committed_date is None

    [row] = _browse(session, project)
    detail = load_dependency(session, dependency.id)

    assert row.committed_date == date(2026, 8, 15)
    assert row.committed_event is event
    assert detail.current_statement.event is event


def test_browse_and_internal_web_withhold_an_unsupported_cited_statement_date(
    session, client, project, dependency
):
    """The internal Ledger shares the Evaluation's published reading.

    This event is deliberately shaped like old imported data: it is current
    and exact-day, but has no verified event-owned Evidence.  Raw statement
    traversal sees the date; neither the Ledger row nor its rendered page
    may publish it or derive an overdue exception from it.
    """
    event = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=dependency.external_org_id,
        stated_external_org_id=dependency.external_org_id,
        scope_mode="selected",
        event_type="commitment",
        source_kind="cited",
        stated_party="LT AT&T Texas",
        event_date=date(2026, 3, 4),
        description="AT&T stated an unsupported completion date.",
        created_by="local:ledger-reviewer",
    )
    session.add(event)
    session.flush()
    session.add_all(
        (
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text="2026-08-15",
                precision="day",
                start_date=date(2026, 8, 15),
                end_date=date(2026, 8, 15),
            ),
            DependencyEventScope(event_id=event.id, dependency_id=dependency.id),
        )
    )
    session.flush()

    evaluation = evaluate_project(session, project.id, today=date(2026, 9, 30))
    [row] = browse(session, project.id, evaluation=evaluation)
    page = client.get(f"/ledger/{project.slug}")

    assert row.committed_date is None
    assert row.committed_event is None
    assert not {"DUE_SOON", "OVERDUE"}.intersection(
        exception.rule for exception in row.exceptions
    )
    assert page.status_code == 200
    assert "2026-08-15" not in page.text


def test_an_unverified_disagreement_is_not_a_contradiction(
    session, project, document, dependency
):
    """A bad citation is a bad citation, not evidence that sources disagree."""
    merge_candidate(
        session,
        make_candidate(
            session,
            project,
            document,
            fields={**FIELDS, "station_from": "9999+00"},
            verified=False,
        ),
        dependency,
        principal=TEST_PRINCIPAL,
    )
    [row] = _browse(session, project)
    assert row.contradicted is False


# ------------------------------------------------------------------- detail


def test_the_detail_view_carries_events_and_audit(session, project, dependency):
    event = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=dependency.external_org_id,
        stated_external_org_id=dependency.external_org_id,
        scope_mode="selected",
        event_type="committed_date_change",
        event_date=date(2026, 3, 4),
        description="AT&T moved relocation from June to August",
        created_by="local:detail-reader",
    )
    session.add(event)
    session.flush()
    session.add(DependencyEventScope(event_id=event.id, dependency_id=dependency.id))
    session.flush()

    view = load_dependency(session, dependency.id)
    assert [e.event_type for e in view.events] == ["committed_date_change"]
    # Acceptance already wrote one audit entry.
    assert any(a.action == "accept_candidate" for a in view.audit)


def test_the_detail_view_states_the_clock_it_was_read_against(
    session, project, dependency
):
    """The page prints "40d overdue"; it must be able to say against what.

    The view took its own `date.today()` inside, so nothing it published
    could be checked later and no test could state a date.
    """
    view = load_dependency(session, dependency.id)

    assert view.evaluation is not None
    assert view.evaluation.today == date.today()
    assert view.evaluation.ruleset_version


def test_a_caller_may_read_the_record_against_a_stated_evaluation(
    session, project, document, dependency
):
    """The clock is a parameter of the read, not a default inside it."""
    organization = session.get(ExternalOrg, dependency.external_org_id)
    assert organization is not None
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=organization.id,
        stated_external_org_id=organization.id,
        stated_party=organization.name,
        source_kind="cited",
        event_date=date(2026, 1, 1),
        description="AT&T stated that relocation would complete on January 1.",
        new_timing=StatementTiming.day("January 1", date(2026, 1, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id,
            1,
            "FOC1-1 LT AT&T Texas Telecom",
        ),
    )

    stated = evaluate_project(session, project.id, today=date(2026, 2, 10))
    view = load_dependency(session, dependency.id, evaluation=stated)

    assert view.evaluation is stated
    overdue = [e for e in view.exceptions if e.rule == "OVERDUE"]
    assert overdue, "a committed date 40 days past the stated clock is overdue"
    assert overdue[0].quantity_days == 40


# ---------------------------------------------------------------------- web


def test_the_ledger_page_renders_rows(client, project, dependency):
    r = client.get(f"/ledger/{project.slug}")
    assert r.status_code == 200
    assert dependency.ref_code in r.text
    assert "LT AT&amp;T Texas" in r.text


def test_the_detail_page_shows_each_claim_with_its_source(
    client, project, dependency
):
    r = client.get(f"/ledger/{project.slug}/{dependency.id}")
    assert r.status_code == 200
    assert "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf" in r.text
    assert "1149+00" in r.text
    assert "not ready" in r.text


def test_marking_evidence_as_closing_makes_it_ready(
    client, session, project, dependency
):
    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    r = client.post(
        f"/dependencies/{dependency.id}/evidence/{link.id}/satisfies",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert load_dependency(session, dependency.id).is_ready is True


def test_unverified_evidence_cannot_be_marked_as_closing(
    client, session, project, document
):
    """ADR-0002: readiness cannot rest on a quote that is not on the page."""
    dep = accept_candidate(
        session,
        make_candidate(session, project, document, verified=False),
        principal=TEST_PRINCIPAL,
    )
    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dep.id)
    ).one()
    r = client.post(
        f"/dependencies/{dep.id}/evidence/{link.id}/satisfies",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert load_dependency(session, dep.id).is_ready is False


def test_ledger_rows_carry_their_exceptions(session, project, dependency):
    [row] = _browse(session, project)
    rules = {e.rule for e in row.exceptions}
    # No milestone linked and no committed date on a fresh matrix record.
    assert "ORPHAN" in rules
    assert "MISSING_DATE" in rules


def test_the_ledger_can_be_filtered_to_one_rule(session, project, dependency):
    assert len(_browse(session, project, rule="ORPHAN")) == 1
    assert _browse(session, project, rule="OVERDUE") == []


def test_the_ledger_page_shows_exception_pills(client, project, dependency):
    r = client.get(f"/ledger/{project.slug}")
    assert "ORPHAN" in r.text
    assert "any exception" in r.text


def test_the_detail_page_explains_each_exception(client, project, dependency):
    r = client.get(f"/ledger/{project.slug}/{dependency.id}")
    assert "not linked to any milestone" in r.text


def test_a_dependency_from_another_project_is_not_reachable(
    client, session, project, dependency
):
    other = Project(slug="ledger-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    r = client.get(f"/ledger/{other.slug}/{dependency.id}")
    assert r.status_code == 404


# --------------------------------------------------------- primary evidence


def _manual_dependency_with_links(session, project, document, *, ref_code, quotes):
    dependency = Dependency(
        project_id=project.id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title="Telecom — Manual support fixture",
        status="identified",
    )
    session.add(dependency)
    session.flush()
    links = [
        EvidenceLink(
            dependency_id=dependency.id,
            document_id=document.id,
            page_no=1,
            quote=quote,
            verified=True,
        )
        for quote in quotes
    ]
    session.add_all(links)
    session.flush()
    return dependency, links


def test_primary_evidence_prefers_designated_publication_support(
    session, project, document
):
    """Publication is a human designation, never the lowest link id."""
    dependency, links = _manual_dependency_with_links(
        session,
        project,
        document,
        ref_code="DEP-09001",
        quotes=["FOC1-1 completion complete", "FOC1-1 utility claim"],
    )
    designate_publication_support(
        session,
        dependency.id,
        links[1].id,
        principal=TEST_PRINCIPAL,
    )

    found = primary_evidence(session, [dependency.id])
    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    evidence = found[dependency.id]
    assert resolved.publication.evidence_link_id == links[1].id
    assert evidence.document_id == document.id
    assert evidence.filename == document.filename
    assert evidence.page_no == 1
    assert evidence.quote == "FOC1-1 utility claim"


def test_primary_evidence_does_not_fall_back_without_publication_designation(
    session, project, document
):
    dependency, _ = _manual_dependency_with_links(
        session,
        project,
        document,
        ref_code="DEP-09002",
        quotes=["first verified quote", "second verified quote"],
    )

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]
    assert resolved.publication is None
    assert primary_evidence(session, [dependency.id]) == {}


def test_primary_evidence_skips_an_unverified_link(session, project, document):
    """An unverified quote is a bad citation, not a citable one."""
    unverified = accept_candidate(
        session,
        make_candidate(session, project, document, verified=False),
        principal=TEST_PRINCIPAL,
    )

    assert primary_evidence(session, [unverified.id]) == {}


def test_primary_evidence_answers_for_many_dependencies_at_once(
    session, project, document, dependency
):
    """Batched: the export used to ask once per exported row."""
    other = accept_candidate(
        session,
        make_candidate(
            session, project, document, fields={**FIELDS, "utility_id": "FOC1-2"}
        ),
        principal=TEST_PRINCIPAL,
    )

    found = primary_evidence(session, [dependency.id, other.id])

    assert set(found) == {dependency.id, other.id}
    assert primary_evidence(session, []) == {}


# ------------------------------------------------------------- readiness


def _link_of(session, dependency):
    return session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()


def test_marking_evidence_is_the_ledger_s_act_not_a_route_s(
    session, project, dependency
):
    """The ownership check, the verified precondition and the audit entry
    are the Ledger's rules. They lived inside a FastAPI handler, so the
    only way to prove readiness was to POST a form."""
    link = _link_of(session, dependency)

    assert (
        mark_satisfies(
            session, dependency.id, link.id, principal=TEST_PRINCIPAL
        )
        is True
    )
    assert load_dependency(session, dependency.id).is_ready is True

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == dependency.id,
            AuditLog.action == "mark_satisfies_requirement",
        )
    ).one()
    assert entry.actor == TEST_PRINCIPAL.subject
    assert entry.human_principal == TEST_PRINCIPAL.subject
    assert entry.before_json["satisfies"] is False
    assert entry.after_json["satisfies"] is True


def test_readiness_is_refused_without_a_verified_quote(session, project, document):
    """ADR-0002: readiness cannot rest on a quote that is not on the page."""
    dep = accept_candidate(
        session,
        make_candidate(session, project, document, verified=False),
        principal=TEST_PRINCIPAL,
    )

    with pytest.raises(UnverifiedEvidence):
        mark_satisfies(
            session, dep.id, _link_of(session, dep).id, principal=TEST_PRINCIPAL
        )

    assert load_dependency(session, dep.id).is_ready is False


def test_evidence_belonging_to_another_dependency_is_refused(
    session, project, document, dependency
):
    """Marking is scoped to the record it is claimed against."""
    other = accept_candidate(
        session,
        make_candidate(
            session, project, document, fields={**FIELDS, "utility_id": "FOC1-9"}
        ),
        principal=TEST_PRINCIPAL,
    )

    with pytest.raises(NoSuchEvidence):
        mark_satisfies(
            session,
            dependency.id,
            _link_of(session, other).id,
            principal=TEST_PRINCIPAL,
        )

    assert load_dependency(session, dependency.id).is_ready is False


def test_marking_twice_returns_the_record_to_not_ready(
    session, project, dependency
):
    """It is a toggle. Nothing in the domain names un-readying, and this
    pins the behaviour that exists rather than endorsing it."""
    link = _link_of(session, dependency)

    assert (
        mark_satisfies(
            session, dependency.id, link.id, principal=TEST_PRINCIPAL
        )
        is True
    )
    assert (
        mark_satisfies(
            session, dependency.id, link.id, principal=TEST_PRINCIPAL
        )
        is False
    )
    assert load_dependency(session, dependency.id).is_ready is False


def test_ledger_readers_use_the_same_current_support_resolution(
    session, project, document, dependency
):
    successor = Document(
        project_id=project.id,
        sha256="a1" * 32,
        filename="successor-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(successor)
    session.flush()
    link = _link_of(session, dependency)
    mark_satisfies(session, dependency.id, link.id, principal=TEST_PRINCIPAL)
    document.registry_id = "ledger-matrix-old"
    successor.registry_id = "ledger-matrix-new"
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
                replacement_date=date(2026, 8, 5),
                source_registry_id=successor.registry_id,
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]
    detail = load_dependency(session, dependency.id)
    [row] = _browse(session, project)

    assert resolved.is_ready is False
    assert set(resolved.superseded_roles) == {"publication", "readiness"}
    assert detail.is_ready is resolved.is_ready
    assert row.is_ready is resolved.is_ready


# --------------------------------------------------------- contradiction


def _assert_value(session, dependency, field_name, value):
    from corridor.models import Assertion

    session.add(
        Assertion(
            dependency_id=dependency.id,
            field_name=field_name,
            asserted_value=value,
            evidence_link_id=_link_of(session, dependency).id,
            doc_date=None,
        )
    )
    session.flush()


def test_a_blank_asserted_value_is_not_a_source_disagreeing(
    session, project, dependency
):
    """Three views of contradiction gave two answers.

    The list page and the exception engine filtered on the value being
    non-null; the detail page filtered on it being truthy. A blank value
    competing with a real one therefore contradicted in two places and
    not in the third, on one record.
    """
    _assert_value(session, dependency, "external_org", "")

    view = load_dependency(session, dependency.id)
    field = next(f for f in view.fields if f.name == "external_org")
    [row] = _browse(session, project)

    assert field.contradicted is False
    assert row.contradicted is False
    assert not any(e.rule == "CONTRADICTION" for e in view.exceptions)


def test_two_real_values_still_contradict_everywhere(
    session, project, dependency
):
    """The detection this metric exists for is untouched."""
    _assert_value(session, dependency, "external_org", "CenterPoint Energy")

    view = load_dependency(session, dependency.id)
    field = next(f for f in view.fields if f.name == "external_org")
    [row] = _browse(session, project)

    assert field.contradicted is True
    assert row.contradicted is True
    assert any(e.rule == "CONTRADICTION" for e in view.exceptions)
