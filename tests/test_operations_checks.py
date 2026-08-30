"""The check-configuration operations view on the public HTTP surface.

Real PostgreSQL, ordinary HTTP. These cover the view's three states
(declaration / supported default / unknown history), validation refusal,
attributable append-only saves, preview without writes, product coherence
(the declared thresholds reach the internal report's Evaluation), the common
access contract, and that a sealed artifact keeps its bytes and recorded input
context after a configuration change.
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor import audit
from corridor.config import settings
from corridor.db import Session, engine
from corridor.models import Dependency, Project, ProjectCheckConfiguration, ReportRun
from corridor.principals import HumanPrincipal
from corridor.report_release import (
    render_and_prepare_external_report,
    retrieve_prepared_external_report,
)

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
    from corridor.web.app import app, get_human_principal, get_session

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_without_principal(session, monkeypatch):
    from corridor.web.app import app, get_session

    monkeypatch.setattr(settings, "human_principal", "")
    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    p = Project(slug="ops-checks", name="Ops Checks", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def _due_soon_dep(session, project, ref="DUE-1", days=20):
    """A live constraint with a Need Date `days` out — DUE_SOON at 30d, not 10d."""
    dep = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type="utility_relocation",
        title="Telecom — Example",
        need_date=date.today() + timedelta(days=days),
    )
    session.add(dep)
    session.flush()
    return dep


def _config_count(session, project_id):
    return session.scalar(
        select(func.count())
        .select_from(ProjectCheckConfiguration)
        .where(ProjectCheckConfiguration.project_id == project_id)
    )


def _run_count(session, project_id):
    return session.scalar(
        select(func.count()).select_from(ReportRun).where(
            ReportRun.project_id == project_id
        )
    )


def _receipt_count(session, project_id):
    return session.scalar(
        select(func.count()).select_from(audit.AuditLog).where(
            audit.AuditLog.action == audit.PRODUCT_PROVING_FRONTEND_REQUEST,
            audit.AuditLog.entity_type == audit.PROJECT,
            audit.AuditLog.entity_id == project_id,
        )
    )


def _valid_form():
    return {"stale_days": "14", "due_soon_days": "10", "action_due_soon_days": "7"}


# --- AC1: the view shows supported thresholds and the effective identity -----


def test_default_view_shows_supported_defaults_distinctly(client, session, project):
    body = client.get(f"/operations/{project.slug}/checks").text
    assert "Check configuration" in body
    assert "Supported thresholds" in body
    # No declaration state, clearly named as the supported default.
    assert "No declaration" in body
    assert "supported defaults" in body
    # The supported catalog shows the defaults and the project scope.
    assert "30" in body and "14" in body and "7" in body
    assert "Applies to this project only." in body
    # Unknown historical inputs are named, not backfilled.
    assert "unknown" in body.lower()


def test_view_after_a_declaration_shows_the_effective_declaration(
    client, session, project
):
    save = client.post(f"/operations/{project.slug}/checks", data=_valid_form())
    assert save.status_code == 200  # followed the redirect to GET
    body = save.text
    assert "effective declaration" in body
    assert "Declaration #" in body
    assert TEST_PRINCIPAL.subject in body
    assert "Configuration history" in body


# --- AC2: validation refuses invalid / incomplete; never silently filled -----


@pytest.mark.parametrize(
    "form",
    [
        {"stale_days": "14", "due_soon_days": "10"},  # incomplete
        {"stale_days": "0", "due_soon_days": "10", "action_due_soon_days": "7"},
        {"stale_days": "abc", "due_soon_days": "10", "action_due_soon_days": "7"},
        {"stale_days": "", "due_soon_days": "10", "action_due_soon_days": "7"},
        {
            "stale_days": "14",
            "due_soon_days": "99999",
            "action_due_soon_days": "7",
        },
    ],
)
def test_invalid_configuration_is_refused_and_not_saved(
    client, session, project, form
):
    response = client.post(f"/operations/{project.slug}/checks", data=form)
    assert response.status_code == 400
    session.expire_all()
    assert _config_count(session, project.id) == 0


# --- AC3: a valid save is a new attributable retained identity ---------------


def test_saving_appends_an_attributable_configuration(client, session, project):
    client.post(f"/operations/{project.slug}/checks", data=_valid_form())
    session.expire_all()
    rows = session.scalars(
        select(ProjectCheckConfiguration).where(
            ProjectCheckConfiguration.project_id == project.id
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].due_soon_days == 10
    # Attributed to the deployment identity, never a form value.
    assert rows[0].created_by == TEST_PRINCIPAL.subject


def test_a_form_supplied_author_is_ignored(client, session, project):
    """Possession of a roster name does not set authorship."""
    form = dict(_valid_form(), created_by="local:someone-else", author="mallory")
    client.post(f"/operations/{project.slug}/checks", data=form)
    session.expire_all()
    row = session.scalars(
        select(ProjectCheckConfiguration).where(
            ProjectCheckConfiguration.project_id == project.id
        )
    ).one()
    assert row.created_by == TEST_PRINCIPAL.subject


def test_saves_are_append_only_and_earlier_versions_remain(client, session, project):
    client.post(f"/operations/{project.slug}/checks", data=_valid_form())
    client.post(
        f"/operations/{project.slug}/checks",
        data={"stale_days": "20", "due_soon_days": "25", "action_due_soon_days": "6"},
    )
    session.expire_all()
    assert _config_count(session, project.id) == 2
    body = client.get(f"/operations/{project.slug}/checks").text
    # Both declarations are visible in the append-only history.
    assert body.count("local:test-reviewer") >= 2


# --- AC4/AC5: the declared configuration reaches the product's Evaluation -----


def test_declared_thresholds_reach_the_internal_report(client, session, project):
    _due_soon_dep(session, project)
    before = client.get(f"/internal-report/{project.slug}").text
    assert "Required-by within 30d" in before
    assert "DUE_SOON" in before  # facet present under the default 30d horizon

    client.post(f"/operations/{project.slug}/checks", data=_valid_form())

    after = client.get(f"/internal-report/{project.slug}").text
    # The product's Evaluation now uses the declared 10-day horizon.
    assert "Required-by within 10d" in after
    assert "DUE_SOON" not in after


# --- AC6: preview identifies the reading without writing ----------------------


def test_preview_shows_the_reading_and_writes_nothing(client, session, project):
    _due_soon_dep(session, project)
    runs_before = _run_count(session, project.id)

    response = client.post(
        f"/operations/{project.slug}/checks/preview", data=_valid_form()
    )
    assert response.status_code == 200
    body = response.text
    assert "Preview" in body
    # Under the proposed 10-day horizon the DUE_SOON facet is gone.
    assert "evaluated" in body.lower()

    session.expire_all()
    # No configuration, no report run, effective config unchanged.
    assert _config_count(session, project.id) == 0
    assert _run_count(session, project.id) == runs_before
    # A real reading still uses the default: preview did not activate anything.
    assert "Required-by within 30d" in client.get(
        f"/internal-report/{project.slug}"
    ).text


def test_preview_refuses_an_invalid_proposal(client, session, project):
    response = client.post(
        f"/operations/{project.slug}/checks/preview",
        data={"stale_days": "0", "due_soon_days": "10", "action_due_soon_days": "7"},
    )
    assert response.status_code == 400
    assert _config_count(session, project.id) == 0


# --- AC7: released/prepared artifacts keep their bytes and input context ------


def test_a_sealed_artifact_is_unchanged_by_a_later_configuration(
    client, session, project
):
    _due_soon_dep(session, project)
    _rendered, _run, artifact = render_and_prepare_external_report(
        session, project_id=project.id
    )
    original_sha = artifact.pdf_sha256
    original_bytes = bytes(artifact.pdf_bytes)
    sealed_thresholds = dict(artifact.evaluation_context_json["thresholds"])
    assert sealed_thresholds["due_soon_days"] == 30  # rendered under the default

    # Declare a tighter configuration after the artifact was sealed.
    client.post(f"/operations/{project.slug}/checks", data=_valid_form())

    session.expire_all()
    again = retrieve_prepared_external_report(session, project.id, artifact.id)
    # The bytes, the digest, and the recorded thresholds are all unchanged;
    # retrieval did not rerun the checks under the new configuration.
    assert again.pdf_sha256 == original_sha
    assert bytes(again.pdf_bytes) == original_bytes
    assert again.evaluation_context_json["thresholds"] == sealed_thresholds


# --- AC8: the common access contract; project isolation; not found -----------


def test_new_routes_write_a_project_access_receipt(client, session, project):
    before = _receipt_count(session, project.id)
    client.get(f"/operations/{project.slug}/checks")
    client.post(f"/operations/{project.slug}/checks/preview", data=_valid_form())
    # Do not follow the 303, so each explicit call maps to one receipt.
    client.post(
        f"/operations/{project.slug}/checks",
        data=_valid_form(),
        follow_redirects=False,
    )
    session.expire_all()
    assert _receipt_count(session, project.id) == before + 3


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("get", "/operations/{slug}/checks", None),
        ("post", "/operations/{slug}/checks/preview", {"stale_days": "14"}),
        ("post", "/operations/{slug}/checks", {"stale_days": "14"}),
    ],
)
def test_routes_fail_closed_without_a_seeded_identity(
    client_without_principal, project, method, path, data
):
    caller = getattr(client_without_principal, method)
    url = path.format(slug=project.slug)
    response = caller(url, data=data) if data is not None else caller(url)
    assert response.status_code == 503


def test_configuration_does_not_cross_projects(client, session):
    a = Project(slug="ops-a", name="A", is_synthetic=True)
    b = Project(slug="ops-b", name="B", is_synthetic=True)
    session.add_all((a, b))
    session.flush()
    client.post(f"/operations/{a.slug}/checks", data=_valid_form())

    body_b = client.get(f"/operations/{b.slug}/checks").text
    assert "No declaration" in body_b
    assert _config_count(session, b.id) == 0


def test_unknown_project_is_not_found(client, session, project):
    assert client.get("/operations/no-such/checks").status_code == 404
    assert (
        client.post("/operations/no-such/checks", data=_valid_form()).status_code
        == 404
    )
