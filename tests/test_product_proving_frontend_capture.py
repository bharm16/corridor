"""Authoritative observation at the Product Proving frontend boundary."""

from __future__ import annotations

from datetime import date, datetime, timezone
import base64
from hashlib import sha256
from inspect import signature
import json
from types import SimpleNamespace

import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.config import settings
from corridor.db import engine
from corridor.extraction_runs import (
    declare_active_run,
    declare_active_run_by_policy,
    record_extraction_run,
)
from corridor.extractor_lineage import injected_extractor_config, zero_token_usage
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    Document,
    EvidenceInvestigationCandidateReviewStart,
    ExternalReportArtifact,
    ExternalReportRelease,
    Project,
    ReportRun,
    WorkDecision,
)
from corridor.principals import HumanPrincipal
from corridor.product_proving_execution import (
    LiveProductProvingOperationsCapture,
    capture_project_write_set,
    run_bounded_product_proving_operations,
    proving_database_identity,
)
from corridor.product_proving_database import (
    DatabaseFingerprint,
    TableFingerprint,
    observe_database_connection_identity,
)
from corridor.product_proving_run import ExpectedPreflight, ObservedPreflight
from corridor.work_decisions import assign_internal_owner


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


def _extraction_run(session, project, document):
    quote = "Equistar will provide the title package by January 2025."
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "commitment",
                "external_org": "Equistar",
                "stated_party": "Equistar",
                "description": quote,
                "committed_date": {
                    "text": "January 2025",
                    "precision": "month",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                },
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                    "whole_row": False,
                }
            ],
            "confidence": 0.9,
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="minutes_v4",
        model="gpt-test",
        citations_verified=True,
        state="pending",
    )
    config = injected_extractor_config(
        extractor="frontend-capture-test",
        prompt_version="minutes_v4",
        model="gpt-test",
        schema_version="minutes_v4",
        prompt_bytes=b"stable prompt",
        schema={"type": "object", "additionalProperties": False},
        postprocessor_bytes=b"stable postprocessor",
        request_controls={"strict": True},
    )
    return record_extraction_run(
        session,
        document,
        prompt_version="minutes_v4",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="gpt-test",
        schema_version="minutes_v4",
        extractor_config=config,
        token_usage=zero_token_usage(document.id),
    )


def test_frontend_capture_api_has_no_caller_claim_fields():
    """Outcomes and pass flags are observations, never caller testimony."""
    from corridor.product_proving_frontend_capture import (
        FrontendPassMetadata,
        capture_product_proving_frontend_pass,
    )

    capture_parameters = signature(capture_product_proving_frontend_pass).parameters
    assert "residual_candidate_ids" not in capture_parameters
    assert "residual_outcomes" not in capture_parameters
    assert "frontend_kind" not in capture_parameters
    assert "frontend_actions" not in capture_parameters
    assert "workarounds" not in capture_parameters
    assert "report_pdf_sha256" not in capture_parameters
    assert "approved_export_bytes" not in capture_parameters

    metadata_parameters = signature(FrontendPassMetadata).parameters
    assert set(metadata_parameters) == {
        "pass_number",
        "operations_elapsed_seconds",
        "practitioner_elapsed_seconds",
        "non_blocking_friction",
        "screenshot_sha256",
    }

    with pytest.raises(TypeError):
        FrontendPassMetadata(  # type: ignore[call-arg]
            pass_number=1,
            operations_elapsed_seconds=1,
            practitioner_elapsed_seconds=2,
            non_blocking_friction=(),
            screenshot_sha256={"work-list": "a" * 64},
            workarounds=(),
        )


def test_invalid_action_observation_requires_a_server_observed_refusal(session):
    from corridor.product_proving_execution import ProjectWriteSetSnapshot
    from corridor.product_proving_frontend_capture import (
        observe_refused_invalid_action,
    )

    before = ProjectWriteSetSnapshot(
        project_id=7,
        project_slug="bounded-project",
        rows={"work_decisions": ()},
    )
    with pytest.raises(ValueError, match="server-observed|protected Project state"):
        observe_refused_invalid_action(session, before=before, after=before)

    other_project = ProjectWriteSetSnapshot(
        project_id=8,
        project_slug="other-project",
        rows={"work_decisions": ()},
    )
    with pytest.raises(ValueError, match="same Project"):
        observe_refused_invalid_action(session, before=before, after=other_project)


def test_report_chronology_uses_work_decision_recorded_at(session):
    from corridor.product_proving_frontend_capture import (
        _require_practitioner_work_precedes_report,
    )

    project = Project(slug="chronology-recorded-at", name="Chronology Recorded At")
    session.add(project)
    session.flush([project])
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-CHRONOLOGY-1",
        dep_type="utility_relocation",
        title="Chronology dependency",
    )
    session.add(dependency)
    session.flush([dependency])

    start = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    decision = WorkDecision(
        dependency_id=dependency.id,
        commitment_lineage_id=None,
        decision_type="assign_internal_owner",
        field="internal_owner",
        before_value=None,
        after_value="Test Lead",
        recorded_by="local:simulated-practitioner",
        recorded_at=start,
        predecessor_decision_id=None,
    )
    prerequisite = AuditLog(
        actor="local:simulated-practitioner",
        human_principal="local:simulated-practitioner",
        action="chronology_prerequisite",
        entity_type="dependency",
        entity_id=dependency.id,
        ts=start,
    )
    session.add_all((decision, prerequisite))
    session.flush()

    report_run = ReportRun(
        project_id=project.id,
        ts=start.replace(minute=1),
        ruleset_version="v-test",
        snapshot_json={},
        document_only=False,
    )
    session.add(report_run)
    session.flush([report_run])
    pdf_bytes = b"%PDF-1.7\nchronology\n%%EOF"
    digest = sha256(pdf_bytes).hexdigest()
    artifact = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="chronology.pdf",
        format="pdf",
        pdf_bytes=pdf_bytes,
        pdf_sha256=digest,
        rendered_at=start.replace(minute=2),
        evaluated_on=start.date(),
        ruleset_version="v-test",
        evaluation_context_json={},
        provenance_mode="all-supported-sources",
        record_context_json={},
    )
    session.add(artifact)
    session.flush([artifact])

    route_rows = []
    for minute, route_name in enumerate(
        ("render_report", "review_report", "preview_prepared_report"), start=3
    ):
        row = AuditLog(
            actor="local:simulated-practitioner",
            human_principal="local:simulated-practitioner",
            action=audit.PRODUCT_PROVING_FRONTEND_REQUEST,
            entity_type="project",
            entity_id=project.id,
            ts=start.replace(minute=minute),
        )
        session.add(row)
        route_rows.append((route_name, row))
    session.flush()
    release = ExternalReportRelease(
        project_id=project.id,
        artifact_id=artifact.id,
        artifact_name=artifact.artifact_name,
        format="pdf",
        pdf_sha256=digest,
        released_at=start.replace(minute=6),
        evaluated_on=artifact.evaluated_on,
        ruleset_version=artifact.ruleset_version,
        provenance_mode=artifact.provenance_mode,
        released_by="local:simulated-practitioner",
        released_by_display="Simulated practitioner",
    )
    release_request = AuditLog(
        actor="local:simulated-practitioner",
        human_principal="local:simulated-practitioner",
        action=audit.PRODUCT_PROVING_FRONTEND_REQUEST,
        entity_type="project",
        entity_id=project.id,
        ts=start.replace(minute=7),
    )
    session.add_all((release, release_request))
    session.flush()
    route_rows.append(("release_prepared_report", release_request))

    _require_practitioner_work_precedes_report(
        session,
        residuals=(),
        decision_changes=(
            SimpleNamespace(
                audit_log_id=prerequisite.id,
                successor_decision_id=decision.id,
            ),
        ),
        correction=SimpleNamespace(audit_log_id=None),
        invalid_action=SimpleNamespace(
            frontend_request_audit_id=prerequisite.id
        ),
        frontend_requests=tuple(
            SimpleNamespace(route_name=route_name, audit_log_id=row.id)
            for route_name, row in route_rows
        ),
        report_release=SimpleNamespace(
            report_run_id=report_run.id,
            artifact_id=artifact.id,
            release_id=release.id,
        ),
    )


def test_capture_refuses_durable_rows_without_server_observed_frontend(session):
    from corridor.product_proving_frontend_capture import (
        FrontendPassMetadata,
        capture_product_proving_frontend_pass,
    )

    reviewer = HumanPrincipal("local:simulated-practitioner")
    project = Project(slug="frontend-capture-pass", name="Frontend Capture Pass")
    session.add(project)
    session.flush([project])
    document = Document(
        project_id=project.id,
        sha256=sha256(b"bounded-minutes").hexdigest(),
        filename="bounded-minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-FRONTEND-1",
        dep_type="utility_relocation",
        title="Existing bounded dependency",
    )
    session.add_all((document, dependency))
    session.flush()
    assign_internal_owner(session, dependency.id, "Initial owner", principal=reviewer)
    baseline = _extraction_run(session, project, document)
    session.flush()
    declare_active_run_by_policy(session, document.id, baseline.id)
    session.flush()

    operations = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=lambda db, doc: _extraction_run(db, project, doc),
        active_run_operation=lambda db, document_id, run_id: declare_active_run(
            db, document_id, run_id, principal=reviewer
        ),
        admission_operation=lambda _db, _project_id: "admitted",
        _commit_for_test=lambda db: db.flush(),
    )
    [candidate_id] = operations.residual_candidate_ids
    empty_state_sha256 = sha256(
        json.dumps(
            {"tables": [], "sequences": [], "schema_objects": []},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    expected = ExpectedPreflight(
        source_revision="a" * 40,
        origin_main_revision="b" * 40,
        migration_head="test-head",
        policy_digests={"event-admission": "c" * 64},
        documents={document.id: document.sha256},
        baseline_runs={document.id: baseline.id},
        milestone_sources={},
        baseline_fingerprint=empty_state_sha256,
    )
    observed = ObservedPreflight(
        source_revision=expected.source_revision,
        origin_main_revision=expected.origin_main_revision,
        clean_worktree=True,
        migration_head=expected.migration_head,
        policy_digests=expected.policy_digests,
        documents=expected.documents,
        baseline_runs=expected.baseline_runs,
        milestone_sources=expected.milestone_sources,
        baseline_fingerprint=expected.baseline_fingerprint,
    )
    operations_capture = LiveProductProvingOperationsCapture(
        expected=expected,
        observed=observed,
        operations=operations,
        database_baseline_manifest_sha256="1" * 64,
        database_baseline_dump_sha256="2" * 64,
        database_baseline_state_sha256=expected.baseline_fingerprint,
        database_baseline_fingerprint=DatabaseFingerprint(
            (), (), expected.baseline_fingerprint
        ),
        database_source_identity=proving_database_identity(settings.database_url),
        database_source_connection_identity=observe_database_connection_identity(
            settings.database_url
        ).as_dict(),
        pass_number=1,
        execution_id="11111111-1111-4111-8111-111111111111",
        started_at="2026-08-26T12:00:00+00:00",
        prior_restore_operation_id=None,
        prior_restore_bundle_manifest_sha256=None,
        prior_restore_bundle_canonical_sha256=None,
    )

    session.add(
        EvidenceInvestigationCandidateReviewStart(
            project_id=project.id,
            candidate_id=candidate_id,
            principal=reviewer.subject,
            observed_at=datetime.now(timezone.utc),
        )
    )
    audit.record(
        session,
        principal=reviewer,
        action=audit.KEEP_CANDIDATE_UNRESOLVED,
        entity_type=audit.CANDIDATE,
        entity_id=candidate_id,
        before={"candidate_state": "pending"},
        after={
            "candidate_state": "pending",
            "authority_gap": "timing_not_established",
        },
    )
    assign_internal_owner(session, dependency.id, "Changed owner", principal=reviewer)

    invalid_before = capture_project_write_set(session, project.slug)
    with pytest.raises(ValueError, match="named person"):
        assign_internal_owner(session, dependency.id, " ", principal=reviewer)
    invalid_after = capture_project_write_set(session, project.slug)

    ruleset = "readiness-v-test"
    report_run = ReportRun(
        project_id=project.id,
        ruleset_version=ruleset,
        snapshot_json={
            "dependencies": {dependency.ref_code: {"id": dependency.id, "ready": False}}
        },
        document_only=False,
    )
    session.add(report_run)
    session.flush([report_run])
    pdf_bytes = b"%PDF-1.7\nfixed frontend report\n%%EOF"
    digest = sha256(pdf_bytes).hexdigest()
    record_context = {
        "dependencies": [
            {
                "dependency_id": dependency.id,
                "ref_code": dependency.ref_code,
                "current_statement_event_id": None,
                "published_statement_event_id": None,
            }
        ],
        "party_statements": [],
        "provenance_classes": ["Assertion", "Derivation", "Work Decision"],
    }
    evaluation_context = {
        "evaluated_on": "2026-08-24",
        "ruleset_version": ruleset,
        "thresholds": {},
    }
    artifact = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="bounded-report.pdf",
        format="pdf",
        pdf_bytes=pdf_bytes,
        pdf_sha256=digest,
        evaluated_on=date(2026, 8, 24),
        ruleset_version=ruleset,
        evaluation_context_json=evaluation_context,
        provenance_mode="all-supported-sources",
        record_context_json=record_context,
    )
    session.add(artifact)
    session.flush([artifact])
    release = ExternalReportRelease(
        project_id=project.id,
        artifact_id=artifact.id,
        artifact_name=artifact.artifact_name,
        format="pdf",
        pdf_sha256=digest,
        evaluated_on=artifact.evaluated_on,
        ruleset_version=ruleset,
        provenance_mode=artifact.provenance_mode,
        released_by=reviewer.subject,
        released_by_display="Simulated practitioner",
    )
    session.add(release)
    session.flush([release])

    screenshot_evidence = {
        "work-list": b"exact work list screenshot bytes",
        "release": b"exact release screenshot bytes",
    }
    screenshot_sha256 = {
        label: sha256(value).hexdigest() for label, value in screenshot_evidence.items()
    }
    terminal_table = TableFingerprint(
        name="frontend_test_write",
        row_count=1,
        rows_sha256="e" * 64,
    )
    terminal_state_sha256 = sha256(
        json.dumps(
            {
                "tables": [terminal_table.as_dict()],
                "sequences": [],
                "schema_objects": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="server-observed|frontend"):
        capture_product_proving_frontend_pass(
            session,
            operations_capture=operations_capture,
            metadata=FrontendPassMetadata(
                pass_number=1,
                operations_elapsed_seconds=2,
                practitioner_elapsed_seconds=3,
                non_blocking_friction=("dense candidate copy",),
                screenshot_sha256=screenshot_sha256,
            ),
            invalid_action_before=invalid_before,
            invalid_action_after=invalid_after,
            database_url=settings.database_url,
            _fingerprint_for_test=lambda _url: DatabaseFingerprint(
                (terminal_table,), (), terminal_state_sha256
            ),
        )


def test_capture_refuses_a_caller_looking_residual_outcome_without_route_receipts(
    session,
):
    """A Candidate state alone cannot be promoted into a proving claim."""
    # The full public test above establishes the accepted receipt shape. This
    # focused diagnosis pins the exact missing receipt without duplicating its
    # complete extraction setup.
    from corridor.product_proving_frontend_capture import _observe_residual_candidates
    from corridor.product_proving_execution import ProjectWriteSetDiff

    project = Project(slug="frontend-capture-fake", name="Fake Caller")
    document = Document(
        project_id=0,
        sha256=sha256(b"fake").hexdigest(),
        filename="fake.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(project)
    session.flush([project])
    document.project_id = project.id
    session.add(document)
    session.flush([document])
    run = _extraction_run(session, project, document)
    session.flush([run])
    [candidate] = session.scalars(
        select(Candidate).where(Candidate.extraction_run_id == run.id)
    ).all()
    candidate.state = "accepted"
    session.flush([candidate])
    empty = ProjectWriteSetDiff(
        project_id=project.id,
        project_slug=project.slug,
        created={},
        deleted={},
        updated={},
    )

    with pytest.raises(ValueError, match="open every exact residual Candidate"):
        _observe_residual_candidates(
            session,
            (candidate.id,),
            {document.id: run.id},
            empty,
        )


def test_screenshot_evidence_requires_a_decodable_png():
    from corridor.product_proving_frontend_capture import _sensible_png_dimensions

    tiny = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 900, 600), False)
    pixmap.clear_with(255)
    browser_sized = pixmap.tobytes("png")

    assert _sensible_png_dimensions(browser_sized) == (900, 600)
    assert _sensible_png_dimensions(tiny) is None
    assert _sensible_png_dimensions(b"not an image") is None


def test_committed_date_change_candidate_does_not_force_a_false_correction(session):
    from corridor.product_proving_execution import ProjectWriteSetDiff
    from corridor.product_proving_frontend_capture import _observe_factual_correction

    project = Project(slug="frontend-no-false-correct", name="No False Correct")
    session.add(project)
    session.flush([project])
    document = Document(
        project_id=project.id,
        sha256=sha256(b"date-change").hexdigest(),
        filename="date-change.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush([document])
    run = _extraction_run(session, project, document)
    session.flush([run])
    candidate = session.scalar(
        select(Candidate).where(Candidate.extraction_run_id == run.id)
    )
    candidate.payload_json = {
        **candidate.payload_json,
        "fields": {
            **candidate.payload_json["fields"],
            "event_type": "committed_date_change",
            "previous_timing": {
                "text": "December 2024",
                "precision": "month",
                "start_date": "2024-12-01",
                "end_date": "2024-12-31",
            },
        },
    }
    session.flush([candidate])
    empty = ProjectWriteSetDiff(
        project_id=project.id,
        project_slug=project.slug,
        created={},
        deleted={},
        updated={},
    )

    observed = _observe_factual_correction(
        session,
        project.id,
        {document.id: run.id},
        empty,
    )

    assert observed.outcome == "no_structured_correction_observed"
    assert observed.supporting_candidate_ids == ()


def test_report_context_binding_compares_exact_statement_versions_and_scope():
    from corridor.product_proving_frontend_capture import (
        _validate_report_context_binding,
    )

    snapshot = {
        "dependencies": {},
        "external_party_commitments": {
            "42": {
                "current_event_id": 100,
                "published_event_id": 99,
                "scope_decision_id": 77,
                "unsupported_current": False,
            }
        },
    }
    report_run = SimpleNamespace(id=5, snapshot_json=snapshot, document_only=False)
    snapshot_sha = sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifact = SimpleNamespace(
        record_context_json={
            "dependencies": [],
            "party_statements": [
                {
                    "commitment_lineage_id": 42,
                    "current_statement_event_id": 101,
                    "published_statement_event_id": 99,
                    "scope_decision_id": 77,
                    "unsupported_current": False,
                }
            ],
            "report_run": {"id": 5, "snapshot_sha256": snapshot_sha},
            "document_only": False,
        }
    )

    with pytest.raises(ValueError, match="statement coverage"):
        _validate_report_context_binding(report_run, artifact)


def test_execution_and_route_receipt_identities_fail_closed():
    from corridor.product_proving_frontend_capture import (
        FrontendRequestObservation,
        _frontend_route_receipt_is_valid,
        _required_screenshot_bindings,
        _validated_serialized_execution_identity,
    )

    def request(audit_id, route, subject, method="GET", status=200):
        return FrontendRequestObservation(
            audit_log_id=audit_id,
            route_name=route,
            route_template=f"/{route}",
            method=method,
            status=status,
            principal="local:simulated-practitioner",
            request_fields_sha256="a" * 64,
            subject=subject,
        )

    first = {
        "pass_number": 1,
        "execution_id": "11111111-1111-4111-8111-111111111111",
        "started_at": "2026-08-26T12:00:00+00:00",
        "prior_restore_operation_id": None,
        "prior_restore_bundle_manifest_sha256": None,
        "prior_restore_bundle_canonical_sha256": None,
    }
    assert _validated_serialized_execution_identity(first) == first
    with pytest.raises(ValueError, match="pass 1 cannot name"):
        _validated_serialized_execution_identity(
            {**first, "prior_restore_operation_id": first["execution_id"]}
        )

    assert _frontend_route_receipt_is_valid(
        route_name="preview_prepared_report",
        route_template="/reports/{slug}/prepared/{artifact_id}/preview",
        method="GET",
        status=200,
    )
    assert not _frontend_route_receipt_is_valid(
        route_name="preview_prepared_report",
        route_template="/reports/{slug}/prepared/{artifact_id}/download",
        method="GET",
        status=200,
    )

    requests = (
        request(1, "coordinator_home", {"project_id": 9}),
        request(2, "queue", {"project_id": 9, "candidate_id": 11}),
        request(
            3,
            "correct_statement_scope_from_screen",
            {"project_id": 9, "candidate_id": 12},
            method="POST",
            status=400,
        ),
        request(
            4,
            "preview_prepared_report",
            {"project_id": 9, "artifact_id": 21},
        ),
        request(
            5,
            "release_prepared_report",
            {"project_id": 9, "artifact_id": 21, "release_id": 22},
            method="POST",
            status=201,
        ),
    )
    bindings = _required_screenshot_bindings(
        frontend_requests=requests,
        residuals=(SimpleNamespace(candidate_id=11, candidate_kind="dependency"),),
        invalid_action=SimpleNamespace(frontend_request_audit_id=3),
        report_release=SimpleNamespace(artifact_id=21, release_id=22),
    )

    assert set(bindings) == {
        "work-list",
        "candidate-11-review",
        "invalid-action-refusal",
        "report-preview",
        "approved-export-release",
    }
    assert bindings["report-preview"]["subject"] == {
        "project_id": 9,
        "artifact_id": 21,
    }
    with pytest.raises(ValueError, match="preview_prepared_report"):
        _required_screenshot_bindings(
            frontend_requests=tuple(
                item
                for item in requests
                if item.route_name != "preview_prepared_report"
            ),
            residuals=(SimpleNamespace(candidate_id=11, candidate_kind="dependency"),),
            invalid_action=SimpleNamespace(frontend_request_audit_id=3),
            report_release=SimpleNamespace(artifact_id=21, release_id=22),
        )
