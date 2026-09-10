"""Bounded, explicitly requested Coordination Summary receipts (#355)."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from corridor.models import (
    Assertion,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    Project,
)
from corridor.principals import HumanPrincipal

from corridor.llm import RequestConfiguration
from model_client_support import FakeModelClient
from access_support import seed_membership


TODAY = date(2026, 8, 30)
ACTOR = HumanPrincipal("local:summary-coordinator")


@pytest.fixture
def project(session):
    value = Project(slug="summary-test", name="Summary Test", is_synthetic=True)
    session.add(value)
    session.flush()
    seed_membership(session, value, ACTOR)
    return value


def stub_client(response):
    """The shared recording double; an exception instance is a failed call."""
    return FakeModelClient(
        response, configuration=RequestConfiguration(model="gpt-5.6-luna")
    )


def _declare(session, project):
    from corridor.coordination_summary import declare_configuration

    return declare_configuration(
        session,
        project_id=project.id,
        principal=ACTOR,
        model="gpt-5.6-luna",
        prompt_version="briefing_v2",
        source_scope="all_sources",
        max_input_tokens=8_000,
        max_output_tokens=500,
        timeout_seconds=30,
        max_requests=1,
        retry_policy="none",
        retention_policy="class_b_30_days",
        observation_context="internal_working_view",
    )


def test_request_refuses_without_a_declared_server_configuration(session, project):
    from corridor.coordination_summary import ConfigurationRequired, request_summary

    client = stub_client({"sentences": []})
    with pytest.raises(ConfigurationRequired):
        request_summary(
            session,
            project_id=project.id,
            principal=ACTOR,
            client_factory=lambda _: client,
            today=TODAY,
        )
    assert client.calls == []


def test_empty_project_keeps_a_truthful_no_draft_receipt_without_model_call(
    session, project
):
    from corridor.coordination_summary import request_summary

    _declare(session, project)
    client = stub_client({"sentences": []})
    receipt = request_summary(
        session,
        project_id=project.id,
        principal=ACTOR,
        client_factory=lambda _: client,
        today=TODAY,
    )

    assert receipt.status == "empty_input"
    assert receipt.summary_markdown is None
    assert client.calls == []


def test_same_frozen_reading_reuses_the_unsuccessful_receipt_without_retrying_paid_work(
    session, project
):
    from corridor.coordination_summary import request_summary

    _declare(session, project)
    session.add(
        Dependency(
            project_id=project.id,
            ref_code="DEP-355",
            dep_type="utility_relocation",
            title="No cited source",
        )
    )
    session.flush()
    client = stub_client(RuntimeError("network unavailable"))

    first = request_summary(
        session,
        project_id=project.id,
        principal=ACTOR,
        client_factory=lambda _: client,
        today=TODAY,
    )
    second = request_summary(
        session,
        project_id=project.id,
        principal=ACTOR,
        client_factory=lambda _: client,
        today=TODAY,
    )

    assert first.id == second.id
    assert first.status == "transport_failure"
    assert len(client.calls) == 1


def test_configuration_is_append_only_and_request_records_reading_identity(
    session, project
):
    from corridor.coordination_summary import declare_configuration, request_summary
    from corridor.models import (
        CoordinationSummaryConfiguration,
        CoordinationSummaryRequest,
    )

    configuration = _declare(session, project)
    with pytest.raises(Exception, match="append-only"):
        with session.begin_nested():
            configuration.model = "another-model"
            session.flush()
    session.refresh(configuration)

    receipt = request_summary(
        session,
        project_id=project.id,
        principal=ACTOR,
        client_factory=lambda _: stub_client({"sentences": []}),
        today=TODAY,
    )
    stored = session.get(CoordinationSummaryRequest, receipt.id)
    assert stored.configuration_id == configuration.id
    assert stored.project_reading_json["project_id"] == project.id
    assert stored.evaluated_on == TODAY
    assert (
        session.scalars(select(CoordinationSummaryConfiguration)).one().created_by
        == ACTOR.subject
    )


def test_http_read_is_access_gated_and_request_requires_an_explicit_action(
    session, project
):
    from fastapi.testclient import TestClient
    from corridor.web.app import app, get_human_principal, get_session

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: ACTOR
    try:
        with TestClient(app) as client:
            page = client.get(f"/internal-report/{project.slug}")
            assert page.status_code == 200
            assert "Unavailable until Corridor Operations declares" in page.text
            assert "Request Coordination Summary" in page.text
            refused = client.post(
                f"/internal-report/{project.slug}/coordination-summary"
            )
            assert refused.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def cited_dependency(session, project):
    """One record with a verified quote, an assertion, and fired rules."""
    document = Document(
        project_id=project.id,
        sha256="c" * 64,
        filename="ucm.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
        doc_date=TODAY - timedelta(days=40),
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text="UC-1 CenterPoint Energy Electric 1149+00 relocation required",
            image_path=None,
            text_source="text_layer",
        )
    )
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="Electric — CenterPoint Energy",
        resolution_strategy="relocate",
        committed_date=TODAY - timedelta(days=10),
        internal_owner=None,
    )
    session.add(dependency)
    session.flush()
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="UC-1 CenterPoint Energy Electric 1149+00",
        verified=True,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dependency.id,
            field_name="external_org",
            asserted_value="CenterPoint Energy",
            evidence_link_id=link.id,
            doc_date=None,
        )
    )
    session.flush()
    return dependency


def _project_floor(session, project):
    """The bucket refs the project reading assigns, read off the same engine."""
    from corridor.dependency_events import published_dependency_statements
    from corridor.exceptions import evaluate_project

    dependency_ids = session.scalars(
        select(Dependency.id).where(
            Dependency.project_id == project.id,
            Dependency.dismissed_at.is_(None),
        )
    ).all()
    publication = published_dependency_statements(
        session, dependency_ids, project_id=project.id
    )
    evaluation = evaluate_project(
        session, project.id, today=TODAY, statement_publication=publication
    )
    return [f"XB{i + 1}" for i in range(len(evaluation.facets()))]


def test_incomplete_configuration_bounds_are_refused(session, project):
    from corridor.coordination_summary import (
        InvalidSummaryConfiguration,
        declare_configuration,
    )

    complete = dict(
        project_id=project.id,
        principal=ACTOR,
        model="gpt-5.6-luna",
        prompt_version="briefing_v2",
        source_scope="all_sources",
        max_input_tokens=8_000,
        max_output_tokens=500,
        timeout_seconds=30,
        max_requests=1,
        retry_policy="none",
        retention_policy="class_b_30_days",
        observation_context="internal_working_view",
    )
    for overrides in (
        {"retry_policy": "exponential"},
        {"max_requests": 2},
        {"max_input_tokens": 0},
        {"timeout_seconds": 0},
        {"prompt_version": "some_other_prompt"},
        {"source_scope": "everything"},
        {"model": "  "},
    ):
        with pytest.raises(InvalidSummaryConfiguration):
            declare_configuration(session, **{**complete, **overrides})


def test_input_over_the_declared_budget_is_refused_without_a_model_call(
    session, project, cited_dependency
):
    from corridor.coordination_summary import declare_configuration, request_summary

    declare_configuration(
        session,
        project_id=project.id,
        principal=ACTOR,
        model="gpt-5.6-luna",
        prompt_version="briefing_v2",
        source_scope="all_sources",
        max_input_tokens=1,
        max_output_tokens=500,
        timeout_seconds=30,
        max_requests=1,
        retry_policy="none",
        retention_policy="class_b_30_days",
        observation_context="internal_working_view",
    )
    client = stub_client({"sentences": []})
    receipt = request_summary(
        session,
        project_id=project.id,
        principal=ACTOR,
        client_factory=lambda _: client,
        today=TODAY,
    )

    assert receipt.status == "budget_exhausted"
    assert receipt.summary_markdown is None
    assert "declared" in receipt.reason
    assert client.calls == []


def test_missing_required_alert_coverage_withholds_the_whole_draft(
    session, project, cited_dependency
):
    from corridor.coordination_summary import request_summary

    _declare(session, project)
    floor = _project_floor(session, project)
    assert floor, "fixture must fire at least one Constraint Alert facet"
    client = stub_client(
        {"sentences": [{"text": "An assertion exists.", "cites": ["A1"]}]}
    )

    receipt = request_summary(
        session,
        project_id=project.id,
        principal=ACTOR,
        client_factory=lambda _: client,
        today=TODAY,
    )

    assert receipt.status == "validation_refused"
    assert receipt.summary_markdown is None
    assert "required alert coverage missing" in receipt.reason
    assert len(client.calls) == 1


def test_completed_draft_covers_the_floor_and_withholds_unsupported_citations(
    session, project, cited_dependency
):
    from corridor.coordination_summary import request_summary

    _declare(session, project)
    floor = _project_floor(session, project)
    assert floor
    sentences = [
        {"text": f"Bucket {ref} is open.", "cites": [ref]} for ref in floor
    ]
    sentences.append({"text": "A made-up fact.", "cites": ["E99"]})
    client = stub_client({"sentences": sentences})

    receipt = request_summary(
        session,
        project_id=project.id,
        principal=ACTOR,
        client_factory=lambda _: client,
        today=TODAY,
    )

    assert receipt.status == "completed"
    assert receipt.reason is None
    assert "A made-up fact." not in receipt.summary_markdown
    assert "withheld" in receipt.summary_markdown
    for ref in floor:
        assert f"Bucket {ref} is open." in receipt.summary_markdown
    assert receipt.project_reading_json["required_alert_floor"] == floor
    assert len(client.calls) == 1
