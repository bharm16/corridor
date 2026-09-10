"""Class B TTL manifests, reachability, and legal-hold behavior."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from corridor.coordination_summary import current_configuration
from corridor.models import (
    CoordinationSummaryConfiguration,
    CoordinationSummaryRequest,
    ExternalReportArtifact,
    ExternalReportRelease,
    Project,
    RetentionManifestItem,
    SpendAuthorization,
)
from corridor.principals import HumanPrincipal
from corridor.retention import (
    FAMILY_SPECS,
    RetentionRefused,
    close_reference,
    execute_retention,
    delete_rebuildable_page_data,
    lift_hold,
    open_reference,
    place_hold,
    plan_retention,
    register_processing_artifact,
)


ACTOR = HumanPrincipal("local:retention-operator")
AS_OF = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _configuration(session, project, *, retention_policy):
    authorization = SpendAuthorization(
        project_id=project.id,
        operation="coordination_summary",
        model="test-model",
        max_input_tokens=100,
        max_output_tokens=100,
        timeout_seconds=10,
        max_requests=1,
        retry_policy="none",
        retention_policy=retention_policy,
        observation_context="internal_working_view",
        declared_by=ACTOR.subject,
    )
    session.add(authorization)
    session.flush()
    config = CoordinationSummaryConfiguration(
        project_id=project.id,
        authorization_id=authorization.id,
        source_scope="all_sources",
        prompt_version="coordination_summary_v1",
    )
    session.add(config)
    session.flush()
    return config


def _request(session, *, completed_at=AS_OF - timedelta(days=31)):
    project = Project(slug=f"retention-{completed_at.timestamp()}", name="Retention", is_synthetic=True)
    session.add(project)
    session.flush()
    config = _configuration(session, project, retention_policy="class_b_30_days")
    request = CoordinationSummaryRequest(
        public_id=f"receipt-{project.id}",
        project_id=project.id,
        configuration_id=config.id,
        requested_by=ACTOR.subject,
        reading_sha256="a" * 64,
        project_reading_json={"project_id": project.id, "facts": ["copied"]},
        evaluated_on=AS_OF.date(),
        ruleset_version="v1",
        statement_publication_fingerprint="b" * 64,
        provenance_mode="all-supported-sources",
        status="completed",
        summary_markdown="Working draft",
        completed_at=completed_at,
    )
    session.add(request)
    session.flush()
    return project, request


def test_manifest_precedes_deletion_and_digest_remainder_survives(session):
    _, request = _request(session)

    manifest = plan_retention(session, as_of=AS_OF, principal=ACTOR)
    item = session.scalars(
        select(RetentionManifestItem).where(
            RetentionManifestItem.manifest_id == manifest.id
        )
    ).one()

    assert manifest.status == "dry_run"
    assert item.family == "coordination_summary"
    assert len(item.content_sha256) == 64
    assert request.project_reading_json["facts"] == ["copied"]

    execute_retention(
        session,
        manifest_id=manifest.id,
        expected_sha256=manifest.content_sha256,
        executed_at=AS_OF,
    )
    session.refresh(request)
    assert request.project_reading_json is None
    assert request.summary_markdown is None
    assert request.retention_content_sha256 == item.content_sha256
    assert request.retention_deleted_at == AS_OF


def test_open_reference_extends_then_refuses_reachable_content(session):
    project, request = _request(session)
    reference = open_reference(
        session,
        project_id=project.id,
        family="coordination_summary",
        source_row_id=request.id,
        kind="review",
        referenced_by="review:17",
    )

    early = plan_retention(session, as_of=AS_OF, principal=ACTOR)
    assert session.scalars(
        select(RetentionManifestItem).where(RetentionManifestItem.manifest_id == early.id)
    ).all() == []
    with pytest.raises(RetentionRefused, match="reachable"):
        plan_retention(session, as_of=AS_OF + timedelta(days=60), principal=ACTOR)

    close_reference(session, reference.id, closed_at=AS_OF + timedelta(days=60))
    resumed = plan_retention(
        session, as_of=AS_OF + timedelta(days=60), principal=ACTOR
    )
    assert len(
        session.scalars(
            select(RetentionManifestItem).where(
                RetentionManifestItem.manifest_id == resumed.id
            )
        ).all()
    ) == 1


def test_segment_decision_and_release_references_refuse_immediately(session):
    project, request = _request(session)
    open_reference(
        session,
        project_id=project.id,
        family="coordination_summary",
        source_row_id=request.id,
        kind="release",
        referenced_by="release:12",
    )

    with pytest.raises(RetentionRefused, match="durably referenced"):
        plan_retention(session, as_of=AS_OF, principal=ACTOR)


def test_hold_stops_ttl_and_cascade_delete_until_attributable_lift(session):
    project, request = _request(session)
    _, unheld_request = _request(
        session, completed_at=AS_OF - timedelta(days=32)
    )
    hold = place_hold(
        session,
        project_id=project.id,
        reason="open-records request",
        principal=ACTOR,
    )

    held_manifest = plan_retention(session, as_of=AS_OF, principal=ACTOR)
    held_items = session.scalars(
        select(RetentionManifestItem).where(
            RetentionManifestItem.manifest_id == held_manifest.id
        )
    ).all()
    assert [(item.family, item.source_row_id) for item in held_items] == [
        ("coordination_summary", unheld_request.id)
    ]
    with pytest.raises(DBAPIError, match="retention hold"):
        with session.begin_nested():
            session.execute(
                delete(CoordinationSummaryRequest).where(
                    CoordinationSummaryRequest.id == request.id
                )
            )

    lift_hold(session, hold_id=hold.id, principal=ACTOR, lifted_at=AS_OF)
    assert hold.lifted_by == ACTOR.subject
    assert plan_retention(session, as_of=AS_OF, principal=ACTOR).status == "dry_run"


def test_legacy_indefinite_configuration_cannot_authorize_new_requests(session):
    project = Project(slug="legacy-retention-config", name="Legacy", is_synthetic=True)
    session.add(project)
    session.flush()
    # The database still carries the pre-#355 class; the Python declaration
    # refuses it, so the row is written as the baseline would have held it.
    _configuration(session, project, retention_policy="retained_indefinitely")

    assert current_configuration(session, project.id) is None


def test_class_a_tables_are_absent_from_the_ttl_catalog(session):
    tables = {spec.table for spec in FAMILY_SPECS}
    assert "facts" not in tables
    assert "source_segments" not in tables
    assert "external_report_releases" not in tables
    with pytest.raises(RetentionRefused, match="only Class B"):
        open_reference(
            session,
            project_id=1,
            family="facts",
            source_row_id=1,
            kind="review",
            referenced_by="forbidden",
        )


def test_page_render_and_raw_ocr_artifacts_delete_after_manifest(session, tmp_path):
    project, _ = _request(session)
    render = tmp_path / "page-1.png"
    render.write_bytes(b"rendered page")
    ocr = tmp_path / "page-1-ocr.json"
    ocr.write_bytes(b'{"text":"working OCR"}')
    first = register_processing_artifact(
        session,
        project_id=project.id,
        kind="page_render",
        path=render,
        terminal_at=AS_OF - timedelta(days=31),
    )
    second = register_processing_artifact(
        session,
        project_id=project.id,
        kind="raw_ocr",
        path=ocr,
        terminal_at=AS_OF - timedelta(days=31),
    )

    manifest = plan_retention(session, as_of=AS_OF, principal=ACTOR)
    families = session.scalars(
        select(RetentionManifestItem.family).where(
            RetentionManifestItem.manifest_id == manifest.id
        )
    ).all()
    assert families.count("processing_artifact") == 2
    assert render.exists() and ocr.exists()

    execute_retention(
        session,
        manifest_id=manifest.id,
        expected_sha256=manifest.content_sha256,
        executed_at=AS_OF,
    )
    assert not render.exists() and not ocr.exists()
    assert first.deleted_at == AS_OF
    assert second.deleted_at == AS_OF


def test_purging_all_class_b_and_c_keeps_release_verifiable(session):
    project, _ = _request(session)
    pdf = b"%PDF-1.7\nretained release\n%%EOF"
    digest = sha256(pdf).hexdigest()
    artifact = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="retained.pdf",
        format="pdf",
        pdf_bytes=pdf,
        pdf_sha256=digest,
        evaluated_on=AS_OF.date(),
        ruleset_version="v1",
        evaluation_context_json={},
        provenance_mode="all-supported-sources",
        record_context_json={"dependencies": [], "party_statements": []},
    )
    session.add(artifact)
    session.flush()
    release = ExternalReportRelease(
        project_id=project.id,
        artifact_id=artifact.id,
        artifact_name=artifact.artifact_name,
        format="pdf",
        pdf_sha256=digest,
        evaluated_on=AS_OF.date(),
        ruleset_version="v1",
        provenance_mode="all-supported-sources",
        released_by=ACTOR.subject,
        released_by_display="Retention Operator",
    )
    session.add(release)
    session.flush()

    manifest = plan_retention(session, as_of=AS_OF, principal=ACTOR)
    execute_retention(
        session,
        manifest_id=manifest.id,
        expected_sha256=manifest.content_sha256,
        executed_at=AS_OF,
    )
    delete_rebuildable_page_data(session)
    session.expire_all()

    retained = session.get(ExternalReportRelease, release.id)
    assert retained is not None
    assert retained.pdf_bytes == pdf
    assert retained.digest_is_valid is True
