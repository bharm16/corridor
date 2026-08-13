"""External Report release is a sealed artifact, not a live Report pointer."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

from corridor.db import Session, engine
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import Dependency, DocPage, Document, ExternalOrg, Project
from corridor.principals import HumanPrincipal
from corridor.report import Cell, build_report
from corridor.report_release import (
    ExternalReportRelease,
    ReleaseRefusal,
    RenderedExternalReport,
    external_report_release_history,
    release_external_report,
    render_external_report_pdf,
    retrieve_released_external_report,
)
from corridor.web.app import app, get_human_principal, get_session


TEST_PRINCIPAL = HumanPrincipal("local:report-releaser")
PDF_A = b"%PDF-1.7\nsealed report A\n%%EOF"
PDF_B = b"%PDF-1.7\nsealed report B\n%%EOF"


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
    project = Project(slug="release-test", name="Release Test", is_synthetic=True)
    party = ExternalOrg(name="Release Test Utility")
    session.add_all((project, party))
    session.flush()
    session.add(
        Dependency(
            project_id=project.id,
            external_org_id=party.id,
            ref_code="DEP-RELEASE-1",
            dep_type="utility_relocation",
            title="Release test telecom relocation",
            status="identified",
        )
    )
    session.flush()
    return project


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _rendered(session, project, *, today=date(2026, 8, 13), document_only=False):
    report = build_report(
        session, project.id, today=today, document_only=document_only
    )
    return RenderedExternalReport(
        artifact_name=f"{project.slug}-{today.isoformat()}.pdf",
        pdf_bytes=PDF_A,
        report=report,
    )


def _release(session, project, **kwargs):
    return release_external_report(
        session,
        project_id=project.id,
        rendered=_rendered(session, project),
        principal=TEST_PRINCIPAL,
        **kwargs,
    )


def _record_statement_version(session, project):
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    party = session.get(ExternalOrg, dependency.external_org_id)
    assert party is not None
    quote = "Release Test Utility will complete the relocation on August 20, 2026."
    document = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="release-statement.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    session.flush()
    return record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2026, 8, 13),
        description=quote,
        new_timing=StatementTiming.day("August 20, 2026", date(2026, 8, 20)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-recorder",
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )


def test_release_seals_exact_pdf_bytes_and_the_frozen_report_context(session, project):
    rendered = _rendered(session, project)

    release = release_external_report(
        session,
        project_id=project.id,
        rendered=rendered,
        principal=TEST_PRINCIPAL,
    )

    assert release.format == "pdf"
    assert release.pdf_bytes == PDF_A
    assert release.pdf_sha256 == "a2231b868a02ee046abc6015b2ea72648450e70f4690c9c4614369bf45c3badd"
    assert release.evaluated_on == date(2026, 8, 13)
    assert release.ruleset_version == rendered.report.ruleset_version
    assert release.evaluation_context_json == {
        "evaluated_on": "2026-08-13",
        "ruleset_version": rendered.report.ruleset_version,
        "thresholds": {
            "action_due_soon_days": 7,
            "due_soon_days": 30,
            "stale_days": 14,
        },
    }
    assert release.provenance_mode == "all-supported-sources"
    assert release.released_by == TEST_PRINCIPAL.subject
    assert release.record_context_json == {
        "dependencies": [
            {
                "dependency_id": next(iter(rendered.report.committed_dates)),
                "ref_code": "DEP-RELEASE-1",
                "current_statement_event_id": None,
                "published_statement_event_id": None,
            }
        ],
        "party_statements": [],
    }


def test_released_pdf_is_retrievable_and_digest_verified_after_the_ledger_changes(
    session, project
):
    release = _release(session, project)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    dependency.status = "closed"
    session.flush()

    stored = retrieve_released_external_report(session, project.id, release.id)

    assert stored.id == release.id
    assert stored.pdf_bytes == PDF_A
    assert stored.digest_is_valid is True
    assert stored.record_context_json == release.record_context_json


def test_changed_bytes_evaluation_mode_population_or_statement_version_must_create_a_new_release(
    session, project
):
    first = _release(session, project)

    changed_bytes = release_external_report(
        session,
        project_id=project.id,
        rendered=replace(_rendered(session, project), pdf_bytes=PDF_B),
        principal=TEST_PRINCIPAL,
    )
    changed_evaluation = release_external_report(
        session,
        project_id=project.id,
        rendered=_rendered(session, project, today=date(2026, 8, 14)),
        principal=TEST_PRINCIPAL,
    )
    changed_mode = release_external_report(
        session,
        project_id=project.id,
        rendered=_rendered(session, project, document_only=True),
        principal=TEST_PRINCIPAL,
    )

    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    dependency.dismissed_at = rendered_at = datetime(2026, 8, 13, tzinfo=timezone.utc)
    changed_population = _release(session, project)
    dependency.dismissed_at = None
    _record_statement_version(session, project)
    changed_statement_version = _release(session, project)

    assert rendered_at.date() == date(2026, 8, 13)
    assert len(
        {
            first.id,
            changed_bytes.id,
            changed_evaluation.id,
            changed_mode.id,
            changed_population.id,
            changed_statement_version.id,
        }
    ) == 6
    assert changed_population.record_context_json["dependencies"] == []
    assert changed_statement_version.record_context_json["dependencies"][0][
        "current_statement_event_id"
    ] is not None


def test_release_refuses_non_pdf_bytes_bare_or_inconsistent_content_without_a_receipt(
    session, project
):
    rendered = _rendered(session, project)
    no_pdf = replace(rendered, pdf_bytes=b"<html>not a PDF</html>")
    bare = replace(rendered, report=replace(rendered.report))
    bare.report.summary.append(Cell("Unsafe", "value"))
    inconsistent = replace(
        rendered,
        report=replace(rendered.report, document_only=True),
    )

    for candidate in (no_pdf, bare, inconsistent):
        with pytest.raises(ReleaseRefusal):
            release_external_report(
                session,
                project_id=project.id,
                rendered=candidate,
                principal=TEST_PRINCIPAL,
            )

    assert session.scalars(
        select(ExternalReportRelease).where(
            ExternalReportRelease.project_id == project.id
        )
    ).all() == []


def test_release_history_is_project_language_and_keeps_the_audit_digest(session, project):
    release = _release(session, project)

    [entry] = external_report_release_history(session, project.id)

    assert entry.release_id == release.id
    assert entry.artifact_name == "release-test-2026-08-13.pdf"
    assert entry.released_by == TEST_PRINCIPAL.subject
    assert entry.evaluated_on == date(2026, 8, 13)
    assert entry.covered_dependency_count == 1
    assert entry.covered_records == ("DEP-RELEASE-1",)
    assert entry.pdf_sha256 == release.pdf_sha256


def test_honestly_adverse_content_does_not_block_release(session, project):
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    party = session.get(ExternalOrg, dependency.external_org_id)
    assert party is not None
    quote = "Release Test Utility will provide the unscoped materials in July 2026."
    document = Document(
        project_id=project.id,
        sha256="b" * 64,
        filename="adverse-release-statement.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    session.flush()
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2026, 7, 1),
        description=quote,
        new_timing=StatementTiming.month("July 2026", 2026, 7),
        scope=StatementScope.unknown(),
        created_by="local:statement-recorder",
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )
    rendered = _rendered(session, project)

    release = release_external_report(
        session,
        project_id=project.id,
        rendered=rendered,
        principal=TEST_PRINCIPAL,
    )

    assert release.id is not None
    commitments = next(
        section for section in rendered.report.sections if section.title == "External Party commitments"
    )
    assert commitments.rows[0][5].value == "Scope not yet known"
    assert commitments.rows[0][6].value == "Open · past due"
    assert commitments.rows[0][7].value == "—"


def test_storage_failure_rolls_back_without_a_successful_release_receipt(
    session, project
):
    def fail_storage(*_args, **_kwargs):
        raise OSError("sealed artifact storage unavailable")

    event.listen(ExternalReportRelease, "before_insert", fail_storage)
    try:
        with pytest.raises(OSError, match="storage unavailable"):
            _release(session, project)
    finally:
        event.remove(ExternalReportRelease, "before_insert", fail_storage)

    assert session.scalars(
        select(ExternalReportRelease).where(
            ExternalReportRelease.project_id == project.id
        )
    ).all() == []


def test_database_refuses_edits_to_a_sealed_release(session, project):
    release = _release(session, project)

    with pytest.raises(Exception, match="immutable"):
        with session.begin_nested():
            session.execute(
                text(
                    "update external_report_releases "
                    "set artifact_name = 'rewritten.pdf' where id = :release_id"
                ),
                {"release_id": release.id},
            )

    assert retrieve_released_external_report(session, project.id, release.id).artifact_name == (
        "release-test-2026-08-13.pdf"
    )


def test_database_refuses_truncating_sealed_releases(session, project):
    release = _release(session, project)

    with pytest.raises(Exception, match="immutable"):
        with session.begin_nested():
            session.execute(text("truncate external_report_releases"))

    assert retrieve_released_external_report(session, project.id, release.id).id == release.id


def test_real_renderer_bytes_are_the_bytes_the_release_service_seals(session, project):
    rendered = render_external_report_pdf(session, project.id, today=date(2026, 8, 13))

    release = release_external_report(
        session,
        project_id=project.id,
        rendered=rendered,
        principal=TEST_PRINCIPAL,
    )

    assert rendered.pdf_bytes.startswith(b"%PDF-")
    assert release.pdf_bytes == rendered.pdf_bytes
    assert release.digest_is_valid is True


def test_renderer_failure_cannot_create_a_release_receipt(session, project, monkeypatch):
    import corridor.report_release as report_release

    monkeypatch.setattr(
        report_release,
        "to_pdf_bytes",
        lambda _html: (_ for _ in ()).throw(OSError("PDF renderer unavailable")),
    )

    with pytest.raises(OSError, match="renderer unavailable"):
        report_release.render_external_report_pdf(session, project.id)

    assert session.scalars(
        select(ExternalReportRelease).where(
            ExternalReportRelease.project_id == project.id
        )
    ).all() == []


def test_release_control_delegates_to_the_public_service_and_returns_the_sealed_identity(
    client, session, project, monkeypatch
):
    import corridor.web.app as web_app

    rendered = _rendered(session, project)
    sealed = _release(session, project)
    calls = []

    monkeypatch.setattr(
        web_app,
        "render_external_report_pdf",
        lambda _session, project_id: (
            calls.append(("render", project_id)) or rendered
        ),
    )
    monkeypatch.setattr(
        web_app,
        "release_external_report",
        lambda _session, *, project_id, rendered, principal: (
            calls.append(("release", project_id, rendered, principal)) or sealed
        ),
    )

    response = client.post(f"/reports/{project.slug}/release")

    assert response.status_code == 201
    assert response.json() == {
        "release_id": sealed.id,
        "artifact_name": sealed.artifact_name,
        "pdf_sha256": sealed.pdf_sha256,
    }
    assert calls[0] == ("render", project.id)
    assert calls[1] == ("release", project.id, rendered, TEST_PRINCIPAL)
