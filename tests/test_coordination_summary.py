"""Bounded, explicitly requested Coordination Summary receipts (#355)."""

from datetime import date

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.models import Dependency, Project
from corridor.principals import HumanPrincipal
from access_support import seed_membership


TODAY = date(2026, 8, 30)
ACTOR = HumanPrincipal("local:summary-coordinator")


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    value = Project(slug="summary-test", name="Summary Test", is_synthetic=True)
    session.add(value)
    session.flush()
    seed_membership(session, value, ACTOR)
    return value


class StubClient:
    model = "gpt-5.6-luna"

    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, *, system, user, schema, images=(), logprobs=False):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


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
        retention_policy="retained_indefinitely",
        observation_context="internal_working_view",
    )


def test_request_refuses_without_a_declared_server_configuration(session, project):
    from corridor.coordination_summary import ConfigurationRequired, request_summary

    client = StubClient({"sentences": []})
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
    client = StubClient({"sentences": []})
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
    client = StubClient(RuntimeError("network unavailable"))

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
        client_factory=lambda _: StubClient({"sentences": []}),
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
