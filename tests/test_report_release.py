"""External Report release is a sealed artifact, not a live Report pointer."""

from hashlib import sha256
from io import BytesIO
import re
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import event, select, text

from corridor.changes import record_run as record_report_run
from corridor.db import Session, engine
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_closure,
    record_external_party_statement,
)
from corridor.models import (
    AuditLog,
    Dependency,
    DependencyEvent,
    DependencyEventTiming,
    DocPage,
    Document,
    ExternalOrg,
    ExternalReportArtifact,
    Project,
    ProjectRosterEntry,
    ReportRun,
)
from corridor.principals import HumanPrincipal
from access_support import seed_membership
from corridor.report import Cell, build_report
from corridor.report_release import (
    ExternalReportRelease,
    ReleasedArtifactIntegrityError,
    ReleaseRefusal,
    RenderedExternalReport,
    external_report_release_history,
    prepare_external_report,
    review_prepared_external_report,
    release_external_report,
    render_and_prepare_external_report,
    render_external_report_pdf,
    retrieve_released_external_report,
)
from corridor.work_decisions import assign_internal_owner
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
        )
    )
    session.flush()
    return project


@pytest.fixture
def client(session, project):
    # HTTP release tests act as an enrolled, release-designated member of the
    # project under test (#331). Seeding lives here rather than in the shared
    # project fixture so the many non-HTTP release-history tests keep exercising
    # the releaser's own roster and display behavior untouched.
    seed_membership(
        session, project, TEST_PRINCIPAL, display_name="Release Coordinator"
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _rendered(session, project, *, today=date(2026, 8, 13), document_only=False):
    report = build_report(session, project.id, today=today, document_only=document_only)
    return RenderedExternalReport(
        artifact_name=f"{project.slug}-{today.isoformat()}.pdf",
        pdf_bytes=PDF_A,
        report=report,
    )


def _rendered_real(session, project, *, today=date(2026, 8, 13), document_only=False):
    return render_external_report_pdf(
        session,
        project.id,
        today=today,
        document_only=document_only,
    )


def _prepare(session, project, *, rendered=None):
    return prepare_external_report(
        session,
        project_id=project.id,
        rendered=rendered or _rendered(session, project),
    )


def _release(session, project, *, artifact=None):
    return release_external_report(
        session,
        project_id=project.id,
        artifact_id=(artifact or _prepare(session, project)).id,
        principal=TEST_PRINCIPAL,
    )


def test_prepare_refuses_a_report_run_from_another_ledger_reading(session, project):
    rendered = _rendered(session, project)
    assert rendered.report.evaluation is not None
    report_run = record_report_run(
        session,
        project.id,
        evaluation=rendered.report.evaluation,
        committed_dates=rendered.report.committed_dates,
        document_only=rendered.report.document_only,
    )
    report_run.snapshot_json = {
        **report_run.snapshot_json,
        "dependencies": {},
    }

    with pytest.raises(ReleaseRefusal, match="does not match the rendered Report"):
        prepare_external_report(
            session,
            project_id=project.id,
            rendered=rendered,
            report_run=report_run,
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


def _record_unknown_scope_statement(session, project):
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    party = session.get(ExternalOrg, dependency.external_org_id)
    assert party is not None
    quote = "Release Test Utility will provide the cable reels in September 2026."
    document = Document(
        project_id=project.id,
        sha256="c" * 64,
        filename="weekly-utility-coordination-minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=7, text=quote))
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
        new_timing=StatementTiming.month("September 2026", 2026, 9),
        scope=StatementScope.unknown(),
        created_by="local:statement-recorder",
        evidence=CitedStatementEvidence(document.id, 7, quote),
    )


def test_release_seals_exact_pdf_bytes_and_the_frozen_report_context(session, project):
    rendered = _rendered(session, project)

    artifact = _prepare(session, project, rendered=rendered)
    release = _release(session, project, artifact=artifact)

    assert release.format == "pdf"
    assert release.artifact_id == artifact.id
    assert release.pdf_bytes == PDF_A
    assert (
        release.pdf_sha256
        == "a2231b868a02ee046abc6015b2ea72648450e70f4690c9c4614369bf45c3badd"
    )
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
    assert release.released_by_display == "Project person (display name not recorded)"
    provenance_names = {
        "Assertion": "Assertion",
        "Derivation": "Derivation",
        "WorkDecision": "Work Decision",
        "Verbal": "Verbal",
    }
    assert release.record_context_json["dependencies"] == [
        {
            "dependency_id": next(iter(rendered.report.committed_dates)),
            "ref_code": "DEP-RELEASE-1",
            "current_statement_event_id": None,
            "published_statement_event_id": None,
        }
    ]
    assert release.record_context_json["document_only"] is rendered.report.document_only
    assert release.record_context_json["party_statements"] == []
    assert release.record_context_json["provenance_classes"] == sorted(
        {
            provenance_names[type(cell.provenance).__name__]
            for cell in rendered.report.cells
            if cell.provenance is not None
        }
    )
    assert len(release.record_context_json["report_cells"]) == len(
        rendered.report.cells
    )


def test_new_release_receipt_references_artifact_without_copying_bytes_or_context(
    session, project
):
    artifact = _prepare(session, project)

    release = _release(session, project, artifact=artifact)
    stored = session.execute(
        text(
            "select pdf_bytes, evaluation_context_json, record_context_json "
            "from external_report_releases where id = :release_id"
        ),
        {"release_id": release.id},
    ).one()

    assert stored.pdf_bytes is None
    assert stored.evaluation_context_json is None
    assert stored.record_context_json is None
    assert release.pdf_bytes == artifact.pdf_bytes
    assert release.evaluation_context_json == artifact.evaluation_context_json
    assert release.record_context_json == artifact.record_context_json


def test_release_context_reads_cannot_mutate_the_artifact_owned_context(
    session, project
):
    artifact = _prepare(session, project)
    release = _release(session, project, artifact=artifact)

    exposed_context = release.record_context_json
    exposed_context["dependencies"].clear()

    stored = retrieve_released_external_report(session, project.id, release.id)
    assert stored.record_context_json == artifact.record_context_json
    assert len(stored.record_context_json["dependencies"]) == 1


def test_prepared_report_review_uses_only_the_fixed_artifact_context(session, project):
    statement = _record_statement_version(session, project)
    artifact = _prepare(session, project)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    dependency.ref_code = "LIVE-REF-CHANGED-AFTER-RENDER"
    session.flush()

    review = review_prepared_external_report(session, project.id, artifact.id)

    assert review.artifact_name == artifact.artifact_name
    assert review.evaluated_on == date(2026, 8, 13)
    assert review.ruleset_version == artifact.ruleset_version
    assert review.provenance_mode == "all-supported-sources"
    assert review.covered_records == ("DEP-RELEASE-1",)
    assert review.covered_statement_versions == (statement.id,)
    assert review.pdf_sha256 == artifact.pdf_sha256


def test_prepared_report_review_names_unknown_scope_statement_and_frozen_source(
    session, project
):
    statement = _record_unknown_scope_statement(session, project)
    artifact = _prepare(session, project, rendered=_rendered_real(session, project))

    review = review_prepared_external_report(session, project.id, artifact.id)

    [covered_statement] = review.covered_party_statements
    assert covered_statement.external_party == "Release Test Utility"
    assert covered_statement.supported_statement == (
        "Release Test Utility will provide the cable reels in September 2026."
    )
    assert covered_statement.source_context == (
        "weekly-utility-coordination-minutes.pdf · page 7 · "
        "“Release Test Utility will provide the cable reels in September 2026.”"
    )
    assert covered_statement.statement_version_ids == (statement.id,)


def test_prepared_report_freezes_unknown_scope_statement_plan_display(session, project):
    from corridor.work_decisions import (
        CoordinationSubject,
        assign_internal_owner,
        set_next_action,
    )

    statement = _record_unknown_scope_statement(session, project)
    subject = CoordinationSubject.statement(statement.commitment_lineage_id)
    assign_internal_owner(session, subject, "Dana Fields", principal=TEST_PRINCIPAL)
    set_next_action(
        session,
        subject,
        "Confirm the cable-reel delivery",
        due_date_unknown_reason="awaiting_external_information",
        principal=TEST_PRINCIPAL,
    )
    rendered = render_external_report_pdf(session, project.id, today=date(2026, 8, 13))
    pdf_text = " ".join(
        "\n".join(
            page.extract_text() for page in PdfReader(BytesIO(rendered.pdf_bytes)).pages
        ).split()
    )
    for expected in (
        "Assigned to Dana Fields",
        "Next action Confirm the cable-reel delivery",
        "Action due date Date not yet known (awaiting external information)",
    ):
        assert expected in pdf_text
    artifact = _prepare(session, project, rendered=rendered)

    [display] = artifact.record_context_json["party_statement_display"]
    assert display["report_fields"] == {
        "external_party": "Release Test Utility",
        "supported_statement": (
            "Release Test Utility will provide the cable reels in September 2026."
        ),
        "timing": "September 2026",
        "timing_precision": "month",
        "statement_type": "Commitment",
        "commitment_scope": "Not yet known",
        "open_status": "Open · not past due",
        "internal_owner": "Dana Fields",
        "next_action": "Confirm the cable-reel delivery",
        "action_due": ("Date not yet known (awaiting external information)"),
        "milestone_impact": "Not applicable",
    }

    assign_internal_owner(
        session, subject, "Changed after prepare", principal=TEST_PRINCIPAL
    )
    set_next_action(
        session,
        subject,
        "Changed after prepare",
        due_date=date(2026, 8, 14),
        principal=TEST_PRINCIPAL,
    )

    assert display["report_fields"]["internal_owner"] == "Dana Fields"
    assert display["report_fields"]["next_action"] == (
        "Confirm the cable-reel delivery"
    )


def test_prepare_refuses_a_pdf_that_omits_frozen_statement_plan_fields(
    session, project
):
    pdf_without_statement = render_external_report_pdf(
        session, project.id, today=date(2026, 8, 13)
    )
    _record_unknown_scope_statement(session, project)
    report_with_statement = build_report(session, project.id, today=date(2026, 8, 13))
    mismatched = RenderedExternalReport(
        artifact_name=pdf_without_statement.artifact_name,
        pdf_bytes=pdf_without_statement.pdf_bytes,
        report=report_with_statement,
    )

    with pytest.raises(
        ReleaseRefusal,
        match="PDF does not contain its frozen External Party statement fields",
    ):
        _prepare(session, project, rendered=mismatched)


def test_prepared_report_excludes_a_closed_unknown_scope_statement(session, project):
    statement = _record_unknown_scope_statement(session, project)
    closure_quote = "Release Test Utility confirms the cable reels were delivered."
    closure_document = Document(
        project_id=project.id,
        sha256="d" * 64,
        filename="cable-reel-closure.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(closure_document)
    session.flush()
    session.add(
        DocPage(
            document_id=closure_document.id,
            page_no=1,
            text=closure_quote,
        )
    )
    session.flush()
    record_external_party_closure(
        session,
        project_id=project.id,
        commitment_lineage_id=statement.commitment_lineage_id,
        source_kind="cited",
        event_date=date(2026, 8, 14),
        description=closure_quote,
        created_by="local:statement-recorder",
        evidence=CitedStatementEvidence(closure_document.id, 1, closure_quote),
    )

    artifact = _prepare(session, project)
    review = review_prepared_external_report(session, project.id, artifact.id)

    assert artifact.record_context_json["party_statements"] == []
    assert "party_statement_display" not in artifact.record_context_json
    assert review.covered_party_statements == ()


def test_legacy_prepared_context_keeps_event_wording_without_inventing_a_citation(
    session, project
):
    statement = _record_unknown_scope_statement(session, project)
    prepared = _prepare(session, project, rendered=_rendered_real(session, project))
    legacy_context = deepcopy(prepared.record_context_json)
    legacy_context.pop("party_statement_display")
    legacy = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="legacy-party-context.pdf",
        format=prepared.format,
        pdf_bytes=bytes(prepared.pdf_bytes),
        pdf_sha256=prepared.pdf_sha256,
        evaluated_on=prepared.evaluated_on,
        ruleset_version=prepared.ruleset_version,
        evaluation_context_json=deepcopy(prepared.evaluation_context_json),
        provenance_mode=prepared.provenance_mode,
        record_context_json=legacy_context,
    )
    session.add(legacy)
    session.flush()

    review = review_prepared_external_report(session, project.id, legacy.id)

    [covered_statement] = review.covered_party_statements
    assert covered_statement.external_party == "Release Test Utility"
    assert covered_statement.supported_statement == statement.description
    assert covered_statement.source_context == (
        "Cited source context not retained in this legacy release context"
    )
    assert "weekly-utility-coordination-minutes.pdf" not in (
        covered_statement.source_context
    )
    assert "page 7" not in covered_statement.source_context


def test_new_and_legacy_verbal_source_context_hide_the_recorder_identity(
    client, session, project
):
    predecessor = _record_unknown_scope_statement(session, project)
    recorder_subject = "local:sensitive-statement-recorder"
    statement = DependencyEvent(
        project_id=project.id,
        commitment_lineage_id=predecessor.commitment_lineage_id,
        supersedes_event_id=predecessor.id,
        affected_external_org_id=predecessor.affected_external_org_id,
        stated_external_org_id=predecessor.stated_external_org_id,
        attribution_state="resolved",
        scope_mode="unknown",
        event_type="committed_date_change",
        source_kind="verbal",
        stated_party=predecessor.stated_party,
        event_date=date(2026, 8, 13),
        description="Release Test Utility confirmed September 2026 by phone.",
        created_by=recorder_subject,
    )
    session.add(statement)
    session.flush()
    session.add(
        DependencyEventTiming(
            event_id=statement.id,
            kind="new",
            text="September 2026",
            precision="month",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
    )
    session.flush()
    prepared = _prepare(session, project, rendered=_rendered_real(session, project))
    legacy_context = deepcopy(prepared.record_context_json)
    legacy_context.pop("party_statement_display")
    legacy = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="legacy-verbal-party-context.pdf",
        format=prepared.format,
        pdf_bytes=bytes(prepared.pdf_bytes),
        pdf_sha256=prepared.pdf_sha256,
        evaluated_on=prepared.evaluated_on,
        ruleset_version=prepared.ruleset_version,
        evaluation_context_json=deepcopy(prepared.evaluation_context_json),
        provenance_mode=prepared.provenance_mode,
        record_context_json=legacy_context,
    )
    session.add(legacy)
    session.flush()

    reviews = (
        review_prepared_external_report(session, project.id, prepared.id),
        review_prepared_external_report(session, project.id, legacy.id),
    )

    for review in reviews:
        [covered_statement] = review.covered_party_statements
        assert covered_statement.source_context == (
            "Recorded verbal statement · conversation 2026-08-13"
        )
        assert recorder_subject not in covered_statement.source_context

    _release(session, project, artifact=prepared)
    history_page = client.get(f"/reports/{project.slug}")
    assert history_page.status_code == 200
    assert "Recorded verbal statement · conversation 2026-08-13" in history_page.text
    assert recorder_subject not in history_page.text


def test_legacy_document_only_context_does_not_publish_current_event_wording(
    session, project
):
    statement = _record_unknown_scope_statement(session, project)
    page = session.scalars(
        select(DocPage)
        .join(Document, Document.id == DocPage.document_id)
        .where(Document.project_id == project.id)
    ).one()
    page.text = "The registered page no longer supports the statement."
    session.flush()
    prepared = _prepare(
        session,
        project,
        rendered=_rendered_real(session, project, document_only=True),
    )
    [recorded_statement] = prepared.record_context_json["party_statements"]
    assert recorded_statement["current_statement_event_id"] == statement.id
    assert recorded_statement["published_statement_event_id"] is None
    legacy_context = deepcopy(prepared.record_context_json)
    legacy_context.pop("party_statement_display")
    legacy = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="legacy-document-only-party-context.pdf",
        format=prepared.format,
        pdf_bytes=bytes(prepared.pdf_bytes),
        pdf_sha256=prepared.pdf_sha256,
        evaluated_on=prepared.evaluated_on,
        ruleset_version=prepared.ruleset_version,
        evaluation_context_json=deepcopy(prepared.evaluation_context_json),
        provenance_mode=prepared.provenance_mode,
        record_context_json=legacy_context,
    )
    session.add(legacy)
    session.flush()

    review = review_prepared_external_report(session, project.id, legacy.id)

    [covered_statement] = review.covered_party_statements
    assert covered_statement.external_party == "Release Test Utility"
    assert covered_statement.supported_statement == (
        "Current statement unsupported in this provenance mode"
    )
    assert covered_statement.source_context == (
        "Not published; source context not retained in this legacy release context"
    )
    assert statement.description not in covered_statement.supported_statement


def test_released_pdf_is_retrievable_and_digest_verified_after_the_ledger_changes(
    session, project
):
    historical_bytes = b"%PDF-1.7\nReadiness: Ready 1, Milestone, Evidence\n%%EOF"
    historical = replace(_rendered(session, project), pdf_bytes=historical_bytes)
    release = _release(session, project, artifact=_prepare(session, project, rendered=historical))
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    assign_internal_owner(
        session, dependency.id, "Changed after release", principal=TEST_PRINCIPAL
    )

    stored = retrieve_released_external_report(session, project.id, release.id)

    assert stored.id == release.id
    assert stored.pdf_bytes == historical_bytes
    assert stored.digest_is_valid is True
    assert stored.record_context_json == release.record_context_json


def test_released_pdf_remains_retrievable_after_render_run_cleanup(session, project):
    rendered, report_run, artifact = render_and_prepare_external_report(
        session, project_id=project.id
    )
    release = _release(session, project, artifact=artifact)

    session.delete(report_run)
    session.flush()
    session.expunge_all()
    stored = retrieve_released_external_report(session, project.id, release.id)

    assert stored.pdf_bytes == rendered.pdf_bytes
    assert stored.pdf_sha256 == artifact.pdf_sha256
    assert stored.digest_is_valid is True


def test_release_refuses_an_artifact_whose_bytes_do_not_match_its_digest(
    session, project
):
    artifact = _prepare(session, project)
    artifact.pdf_bytes = PDF_B

    with pytest.raises(ReleasedArtifactIntegrityError, match="SHA-256 digest"):
        _release(session, project, artifact=artifact)

    with session.no_autoflush:
        assert session.scalars(
            select(ExternalReportRelease).where(
                ExternalReportRelease.project_id == project.id
            )
        ).all() == []


def test_release_uses_the_prepared_artifact_after_the_ledger_changes(session, project):
    artifact = _prepare(session, project)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    assign_internal_owner(
        session, dependency.id, "Changed after preparation", principal=TEST_PRINCIPAL
    )

    release = _release(session, project, artifact=artifact)

    assert release.artifact_id == artifact.id
    assert release.pdf_bytes == artifact.pdf_bytes == PDF_A
    assert release.record_context_json == artifact.record_context_json
    assert release.record_context_json["dependencies"] == [
        {
            "dependency_id": dependency.id,
            "ref_code": "DEP-RELEASE-1",
            "current_statement_event_id": None,
            "published_statement_event_id": None,
        }
    ]


def test_retrying_the_same_artifact_returns_its_original_release(session, project):
    artifact = _prepare(session, project)

    first = _release(session, project, artifact=artifact)
    retried = _release(session, project, artifact=artifact)

    assert retried.id == first.id
    assert retried.released_at == first.released_at
    assert session.scalars(
        select(ExternalReportRelease).where(
            ExternalReportRelease.artifact_id == artifact.id
        )
    ).all() == [first]


def test_changed_bytes_evaluation_mode_population_or_statement_version_must_create_a_new_release(
    session, project
):
    first = _release(session, project)

    changed_bytes = _release(
        session,
        project,
        artifact=_prepare(
            session,
            project,
            rendered=replace(_rendered(session, project), pdf_bytes=PDF_B),
        ),
    )
    changed_evaluation = _release(
        session,
        project,
        artifact=_prepare(
            session,
            project,
            rendered=_rendered(session, project, today=date(2026, 8, 14)),
        ),
    )
    changed_mode = _release(
        session,
        project,
        artifact=_prepare(
            session,
            project,
            rendered=_rendered(session, project, document_only=True),
        ),
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
    assert (
        len(
            {
                first.id,
                changed_bytes.id,
                changed_evaluation.id,
                changed_mode.id,
                changed_population.id,
                changed_statement_version.id,
            }
        )
        == 6
    )
    assert changed_population.record_context_json["dependencies"] == []
    assert (
        changed_statement_version.record_context_json["dependencies"][0][
            "current_statement_event_id"
        ]
        is not None
    )


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
            _prepare(session, project, rendered=candidate)

    assert (
        session.scalars(
            select(ExternalReportRelease).where(
                ExternalReportRelease.project_id == project.id
            )
        ).all()
        == []
    )
    assert (
        session.scalars(
            select(ExternalReportArtifact).where(
                ExternalReportArtifact.project_id == project.id
            )
        ).all()
        == []
    )


def test_release_history_is_project_language_and_keeps_the_audit_digest(
    session, project
):
    release = _release(session, project)

    [entry] = external_report_release_history(session, project.id)

    assert entry.release_id == release.id
    assert entry.artifact_name == "release-test-2026-08-13.pdf"
    assert entry.released_by == TEST_PRINCIPAL.subject
    assert entry.released_by_display == "Project person (display name not recorded)"
    assert entry.evaluated_on == date(2026, 8, 13)
    assert entry.covered_dependency_count == 1
    assert entry.covered_records == ("DEP-RELEASE-1",)
    assert entry.pdf_sha256 == release.pdf_sha256


def test_release_history_uses_roster_name_while_retaining_principal_for_audit(
    client, session, project
):
    # The releaser is already an enrolled member (client fixture); give that
    # membership the projected display name this test is about.
    seed_membership(session, project, TEST_PRINCIPAL, display_name="Dana Fields")
    session.flush()
    release = _release(session, project)

    [entry] = external_report_release_history(session, project.id)
    page = client.get(f"/reports/{project.slug}")

    assert entry.released_by_display == "Dana Fields"
    assert entry.released_by == TEST_PRINCIPAL.subject
    assert page.status_code == 200
    assert "Approved to share by</dt><dd>Dana Fields</dd>" in page.text
    assert "Audit identity" not in page.text
    assert TEST_PRINCIPAL.subject not in page.text
    assert release.released_by == TEST_PRINCIPAL.subject


def test_release_history_keeps_the_release_time_name_after_a_roster_edit(
    session, project
):
    roster_entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject=TEST_PRINCIPAL.subject,
        display_name="Dana Fields",
    )
    session.add(roster_entry)
    session.flush()
    release = _release(session, project)

    roster_entry.display_name = "Dana Fields-Renamed"
    session.flush()

    [entry] = external_report_release_history(session, project.id)
    assert entry.released_by_display == "Dana Fields"
    assert entry.released_by == TEST_PRINCIPAL.subject
    assert release.released_by_display == "Dana Fields"


def test_release_history_keeps_the_release_time_name_after_roster_deletion(
    session, project
):
    roster_entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject=TEST_PRINCIPAL.subject,
        display_name="Dana Fields",
    )
    session.add(roster_entry)
    session.flush()
    release = _release(session, project)

    session.delete(roster_entry)
    session.flush()

    [entry] = external_report_release_history(session, project.id)
    assert entry.released_by_display == "Dana Fields"
    assert entry.released_by == TEST_PRINCIPAL.subject
    assert release.released_by_display == "Dana Fields"


def test_release_history_metadata_query_does_not_load_retained_pdf_bytes(
    session, project
):
    _release(session, project)
    statements = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement)

    connection = session.get_bind()
    event.listen(connection, "before_cursor_execute", capture_sql)
    try:
        external_report_release_history(session, project.id)
    finally:
        event.remove(connection, "before_cursor_execute", capture_sql)

    [history_sql] = [
        statement for statement in statements if "external_report_releases" in statement
    ]
    assert "pdf_bytes" not in history_sql


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
    rendered = _rendered_real(session, project)

    release = _release(
        session,
        project,
        artifact=_prepare(session, project, rendered=rendered),
    )

    assert release.id is not None
    commitments = next(
        section
        for section in rendered.report.sections
        if section.title == "Organization commitments"
    )
    assert commitments.rows[0][5].value == "Not yet known"
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

    assert (
        session.scalars(
            select(ExternalReportRelease).where(
                ExternalReportRelease.project_id == project.id
            )
        ).all()
        == []
    )


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

    assert retrieve_released_external_report(
        session, project.id, release.id
    ).artifact_name == ("release-test-2026-08-13.pdf")


def test_database_refuses_truncating_sealed_releases(session, project):
    release = _release(session, project)

    with pytest.raises(Exception, match="immutable"):
        with session.begin_nested():
            session.execute(text("truncate external_report_releases"))

    assert (
        retrieve_released_external_report(session, project.id, release.id).id
        == release.id
    )


def test_database_refuses_edits_or_truncation_of_rendered_artifacts(session, project):
    artifact = _prepare(session, project)

    with pytest.raises(Exception, match="immutable"):
        with session.begin_nested():
            session.execute(
                text(
                    "update external_report_artifacts "
                    "set artifact_name = 'rewritten.pdf' where id = :artifact_id"
                ),
                {"artifact_id": artifact.id},
            )
    with pytest.raises(Exception, match="immutable"):
        with session.begin_nested():
            session.execute(text("truncate external_report_artifacts cascade"))

    assert session.get(ExternalReportArtifact, artifact.id).digest_is_valid is True


def test_real_renderer_bytes_are_the_bytes_the_release_service_seals(session, project):
    rendered = render_external_report_pdf(session, project.id, today=date(2026, 8, 13))

    release = _release(
        session,
        project,
        artifact=_prepare(session, project, rendered=rendered),
    )

    assert rendered.pdf_bytes.startswith(b"%PDF-")
    assert release.pdf_bytes == rendered.pdf_bytes
    assert release.digest_is_valid is True


def test_renderer_failure_cannot_create_a_release_receipt(
    session, project, monkeypatch
):
    import corridor.report_release as report_release

    monkeypatch.setattr(
        report_release,
        "to_pdf_bytes",
        lambda _html: (_ for _ in ()).throw(OSError("PDF renderer unavailable")),
    )

    with pytest.raises(OSError, match="renderer unavailable"):
        report_release.render_external_report_pdf(session, project.id)

    assert (
        session.scalars(
            select(ExternalReportRelease).where(
                ExternalReportRelease.project_id == project.id
            )
        ).all()
        == []
    )


def test_ordinary_report_flow_renders_fixed_pdf_for_review_without_asking_for_an_identity(
    client, session, project
):
    before_runs = tuple(
        session.scalars(
            select(ReportRun).where(ReportRun.project_id == project.id)
        ).all()
    )
    coordinator_home = client.get(f"/work/{project.slug}")
    assert coordinator_home.status_code == 200
    assert f'href="/reports/{project.slug}"' in coordinator_home.text
    assert "Prepare coordination report" in coordinator_home.text

    workspace = client.get(f"/reports/{project.slug}")

    assert workspace.status_code == 200
    assert "Prepare fixed PDF for review" in workspace.text

    response = client.post(f"/reports/{project.slug}/render", data={"ordinary": "1"})

    assert response.status_code == 201
    assert "Review this fixed PDF" in response.text
    assert "Checks as of" in response.text
    assert "All supported sources" in response.text
    assert "DEP-RELEASE-1" in response.text
    assert "Covered statement versions" in response.text
    assert "SHA-256 digest" in response.text
    assert "Fixed coordination report PDF preview" in response.text
    assert "Download this exact PDF" in response.text
    assert "Approve this exact PDF to share" in response.text
    assert "Artifact ID" not in response.text
    assert 'name="artifact_id"' not in response.text
    assert re.search(
        rf'action="/reports/{project.slug}/prepared/\d+/release"', response.text
    )
    assert re.search(
        rf'data="/reports/{project.slug}/prepared/\d+/preview"', response.text
    )
    after_runs = tuple(
        session.scalars(
            select(ReportRun).where(ReportRun.project_id == project.id)
        ).all()
    )
    assert len(after_runs) == len(before_runs) + 1
    assert (
        after_runs[-1].snapshot_json["ruleset_version"]
        == after_runs[-1].ruleset_version
    )
    render_receipt = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == "product_proving_frontend_request",
            AuditLog.entity_type == "project",
            AuditLog.entity_id == project.id,
            AuditLog.after_json["route_name"].astext == "render_report",
        )
        .order_by(AuditLog.id.desc())
    )
    assert render_receipt is not None
    assert (render_receipt.after_json or {})["subject"]["report_run_id"] == (
        after_runs[-1].id
    )


def test_ordinary_release_click_keeps_the_reviewed_bytes_retrievable_in_history(
    client, session, project
):
    statement = _record_statement_version(session, project)
    artifact = _prepare(session, project)

    review = client.get(f"/reports/{project.slug}/prepared/{artifact.id}")
    assert review.status_code == 200
    assert artifact.artifact_name in review.text
    assert "1 exact statement version retained in the audit receipt." in review.text
    assert f">{statement.id}<" not in review.text

    prepared_preview = client.get(
        f"/reports/{project.slug}/prepared/{artifact.id}/preview"
    )
    assert prepared_preview.status_code == 200
    assert prepared_preview.headers["content-disposition"].startswith("inline;")
    assert prepared_preview.content == PDF_A

    prepared_pdf = client.get(
        f"/reports/{project.slug}/prepared/{artifact.id}/download"
    )
    assert prepared_pdf.status_code == 200
    assert prepared_pdf.headers["content-disposition"].startswith("attachment;")
    assert prepared_pdf.content == PDF_A

    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    assign_internal_owner(
        session, dependency.id, "Changed before release", principal=TEST_PRINCIPAL
    )

    released = client.post(f"/reports/{project.slug}/prepared/{artifact.id}/release")

    assert released.status_code == 201
    assert "This exact PDF is approved to share and retained." in released.text
    assert artifact.artifact_name in released.text
    # The signed-in releaser is an enrolled member, so the history shows their
    # roster display name, never the raw principal subject (#331).
    assert "Release Coordinator" in released.text
    assert TEST_PRINCIPAL.subject not in released.text
    assert "DEP-RELEASE-1" in released.text
    assert artifact.pdf_sha256 in released.text
    assert (
        "Approval does not send the PDF by email or document control." in released.text
    )
    download = re.search(
        rf'href="(/reports/{project.slug}/releases/\d+/download)"', released.text
    )
    assert download is not None
    frontend_routes = {
        (entry.after_json or {}).get("route_name")
        for entry in session.scalars(
            select(AuditLog).where(
                AuditLog.action == "product_proving_frontend_request",
                AuditLog.entity_type == "project",
                AuditLog.entity_id == project.id,
            )
        ).all()
    }
    assert {
        "review_report",
        "preview_prepared_report",
        "download_prepared_report",
        "release_prepared_report",
    } <= frontend_routes

    released_pdf = client.get(download.group(1))

    assert released_pdf.status_code == 200
    assert released_pdf.headers["content-type"] == "application/pdf"
    assert released_pdf.content == prepared_pdf.content == PDF_A

    history = client.get(f"/reports/{project.slug}")
    assert history.status_code == 200
    assert artifact.artifact_name in history.text
    assert TEST_PRINCIPAL.subject not in history.text
    assert "DEP-RELEASE-1" in history.text
    assert artifact.pdf_sha256 in history.text


def test_report_response_render_failure_rolls_back_artifact_and_frontend_receipt(
    session, client, project, monkeypatch
):
    import importlib

    web_app = importlib.import_module("corridor.web.app")
    real_template_response = web_app.TEMPLATES.TemplateResponse
    before_run_ids = tuple(
        session.scalars(
            select(ReportRun.id).where(ReportRun.project_id == project.id)
        ).all()
    )
    before_artifact_ids = tuple(
        session.scalars(
            select(ExternalReportArtifact.id).where(
                ExternalReportArtifact.project_id == project.id
            )
        ).all()
    )
    before_receipt_ids = tuple(
        session.scalars(
            select(AuditLog.id).where(
                AuditLog.entity_type == "project",
                AuditLog.entity_id == project.id,
                AuditLog.action == "product_proving_frontend_request",
            )
        ).all()
    )

    def fail_report_review(request, name, *args, **kwargs):
        if name == "report_review.html":
            raise RuntimeError("synthetic report response failure")
        return real_template_response(request, name, *args, **kwargs)

    monkeypatch.setattr(web_app.TEMPLATES, "TemplateResponse", fail_report_review)

    with pytest.raises(RuntimeError, match="synthetic report response failure"):
        client.post(f"/reports/{project.slug}/render", data={"ordinary": "1"})

    session.expire_all()
    assert tuple(
        session.scalars(
            select(ReportRun.id).where(ReportRun.project_id == project.id)
        ).all()
    ) == before_run_ids
    assert tuple(
        session.scalars(
            select(ExternalReportArtifact.id).where(
                ExternalReportArtifact.project_id == project.id
            )
        ).all()
    ) == before_artifact_ids
    assert tuple(
        session.scalars(
            select(AuditLog.id).where(
                AuditLog.entity_type == "project",
                AuditLog.entity_id == project.id,
                AuditLog.action == "product_proving_frontend_request",
            )
        ).all()
    ) == before_receipt_ids


def test_ordinary_review_and_history_name_party_statements_without_raw_event_ids(
    client, session, project
):
    statement = _record_unknown_scope_statement(session, project)
    artifact = _prepare(session, project, rendered=_rendered_real(session, project))

    review = client.get(f"/reports/{project.slug}/prepared/{artifact.id}")

    assert review.status_code == 200
    assert "Release Test Utility" in review.text
    assert (
        "Release Test Utility will provide the cable reels in September 2026."
        in review.text
    )
    assert "weekly-utility-coordination-minutes.pdf" in review.text
    assert "page 7" in review.text
    assert f">{statement.id}<" not in review.text

    released = client.post(f"/reports/{project.slug}/prepared/{artifact.id}/release")

    assert released.status_code == 201
    assert "Release Test Utility" in released.text
    assert (
        "Release Test Utility will provide the cable reels in September 2026."
        in released.text
    )
    assert "weekly-utility-coordination-minutes.pdf" in released.text
    assert "page 7" in released.text
    assert f">{statement.id}<" not in released.text


def test_render_control_prepares_a_distinct_artifact(
    client, session, project, monkeypatch
):
    import corridor.web.app as web_app

    rendered = _rendered(session, project)
    artifact = _prepare(session, project, rendered=rendered)
    report_run = record_report_run(
        session,
        project.id,
        evaluation=rendered.report.evaluation,
        committed_dates=rendered.report.committed_dates,
        document_only=rendered.report.document_only,
    )
    calls = []

    monkeypatch.setattr(
        web_app,
        "render_and_prepare_external_report",
        lambda _session, *, project_id: (
            calls.append(("render_and_prepare", project_id))
            or (rendered, report_run, artifact)
        ),
    )

    response = client.post(f"/reports/{project.slug}/render")

    assert response.status_code == 201
    assert response.json() == {
        "artifact_id": artifact.id,
        "artifact_name": artifact.artifact_name,
        "pdf_sha256": artifact.pdf_sha256,
    }
    assert calls == [("render_and_prepare", project.id)]


def test_release_control_authorizes_a_prepared_artifact_without_rerendering(
    client, session, project, monkeypatch
):
    import corridor.web.app as web_app

    artifact = _prepare(session, project)
    sealed = _release(session, project, artifact=artifact)
    calls = []

    monkeypatch.setattr(
        web_app,
        "render_and_prepare_external_report",
        lambda *_args, **_kwargs: pytest.fail("release must not rerender a Report"),
    )
    monkeypatch.setattr(
        web_app,
        "release_external_report",
        lambda _session, *, project_id, artifact_id, principal: (
            calls.append(("release", project_id, artifact_id, principal)) or sealed
        ),
    )

    response = client.post(
        f"/reports/{project.slug}/release", data={"artifact_id": artifact.id}
    )

    assert response.status_code == 201
    assert response.json() == {
        "release_id": sealed.id,
        "artifact_name": sealed.artifact_name,
        "pdf_sha256": sealed.pdf_sha256,
    }
    assert calls == [("release", project.id, artifact.id, TEST_PRINCIPAL)]


def test_ordinary_release_control_delegates_its_bound_artifact_without_rerendering(
    client, session, project, monkeypatch
):
    import corridor.web.app as web_app

    artifact = _prepare(session, project)
    sealed = _release(session, project, artifact=artifact)
    calls = []

    monkeypatch.setattr(
        web_app,
        "render_and_prepare_external_report",
        lambda *_args, **_kwargs: pytest.fail("release must not rerender a Report"),
    )
    monkeypatch.setattr(
        web_app,
        "release_external_report",
        lambda _session, *, project_id, artifact_id, principal: (
            calls.append(("release", project_id, artifact_id, principal)) or sealed
        ),
    )

    response = client.post(f"/reports/{project.slug}/prepared/{artifact.id}/release")

    assert response.status_code == 201
    assert "This exact PDF is approved to share and retained." in response.text
    assert calls == [("release", project.id, artifact.id, TEST_PRINCIPAL)]


def test_ordinary_artifact_routes_refuse_another_project(client, session, project):
    other_project = Project(
        slug="other-release-test",
        name="Other Release Test",
        is_synthetic=True,
    )
    session.add(other_project)
    session.flush()
    artifact = _prepare(session, project)

    # The releaser is not a member of the other project, so the membership gate
    # refuses every artifact surface with an indistinguishable 404 before the
    # cross-project artifact mismatch is ever reached (#331).
    assert (
        client.get(f"/reports/{other_project.slug}/prepared/{artifact.id}").status_code
        == 404
    )
    assert (
        client.get(
            f"/reports/{other_project.slug}/prepared/{artifact.id}/preview"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/reports/{other_project.slug}/prepared/{artifact.id}/download"
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/reports/{other_project.slug}/prepared/{artifact.id}/release"
        ).status_code
        == 404
    )

    release = _release(session, project, artifact=artifact)
    assert (
        client.get(
            f"/reports/{other_project.slug}/releases/{release.id}/download"
        ).status_code
        == 404
    )


def _adopted_project(session, tmp_path, slug: str) -> Project:
    """A project whose accepted record came from its own adopted workbook.

    Its own project, not the shared fixture's: Adopt Baseline refuses to write
    over an existing legacy accepted record, which is the ADR-0081 boundary
    working rather than a fixture detail to route around.
    """

    from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes

    project = Project(slug=slug, name="Adopted Release", is_synthetic=True)
    session.add(project)
    session.flush()
    seed_membership(session, project, TEST_PRINCIPAL, display_name="Coordinator")
    body = workbook_bytes(tmp_path / f"{slug}.xlsx", BASELINE_ROWS)
    adopt(session, project, body, tmp_path)
    return project


def test_an_adopted_project_has_no_separate_report_release_act(session, tmp_path):
    """One release act per project: the authorized package, not a second PDF.

    ADR-0086 makes one authorized release package the external issue unit, and
    ADR-0091 makes its membership configuration. The PDF this module seals is
    rendered from the legacy ledger and binds no accepted Project Record
    revision, so for an adopted project it would be a second external release,
    in a second byte format, able to disagree with the package about what the
    record said. The rule lives in the module both acts pass through rather
    than on the routes, so neither door is left open.
    """

    project = _adopted_project(session, tmp_path, "adopted-release-act")

    with pytest.raises(ReleaseRefusal, match="authorized release package"):
        render_and_prepare_external_report(session, project_id=project.id)

    # An artifact the legacy path already retained is refused by the release act
    # too, not only by the act that would prepare a new one.
    artifact = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="pre-adoption.pdf",
        format="pdf",
        pdf_bytes=PDF_A,
        pdf_sha256=sha256(PDF_A).hexdigest(),
        evaluated_on=date(2026, 3, 1),
        ruleset_version="v0.4",
        evaluation_context_json={},
        provenance_mode="all-supported-sources",
        record_context_json={"dependencies": [], "party_statements": []},
    )
    session.add(artifact)
    session.flush()
    with pytest.raises(ReleaseRefusal, match="authorized release package"):
        release_external_report(
            session,
            project_id=project.id,
            artifact_id=artifact.id,
            principal=TEST_PRINCIPAL,
        )


def test_the_release_page_offers_the_issue_instead_for_an_adopted_project(
    session, client, tmp_path
):
    """The page keeps retained history and drops both controls (ADR-0086)."""

    project = _adopted_project(session, tmp_path, "adopted-release-page")

    page = client.get(f"/reports/{project.slug}")

    assert page.status_code == 200
    assert f"/reports/{project.slug}/render" not in page.text
    assert f"/work/{project.slug}" in page.text
    assert "approved for sharing as part of one" in page.text
