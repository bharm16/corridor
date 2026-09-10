"""Storage-duplication baseline and frozen semantic-output contracts."""

from __future__ import annotations

from datetime import date
from hashlib import sha256

from sqlalchemy import text

from corridor.models import (
    Candidate,
    Dependency,
    Document,
    EvidenceLink,
    ExternalReportArtifact,
    ExternalReportRelease,
    ExtractionRun,
    Project,
    ReportRun,
)
from corridor.storage_baseline import BaselineSelection, build_storage_baseline

from pdf_fixture_support import PdfFixture


def _pdf_bytes(text: str) -> bytes:
    fixture = PdfFixture()
    fixture.add_page().text((72, 72), text)
    return fixture.tobytes()


def _seed_representative_state(session, tmp_path):
    project = Project(
        slug="storage-baseline-test",
        name="Storage Baseline Test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        sha256="4" * 64,
        filename="baseline-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    payload = {
        "fields": {"utility_id": "U-17", "station_from": "10+00"},
        "citations": [
            {
                "page": 1,
                "quote": "Utility U-17 begins at Station 10+00.",
                "verified": True,
            }
        ],
    }
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        source_document_id=document.id,
        payload_json=payload,
        source_pages=[1],
        confidence=0.99,
        prompt_version="baseline-v1",
        model="test-model",
        citations_verified=True,
        state="accepted",
    )
    session.add(candidate)
    session.flush()
    run = ExtractionRun(
        document_id=document.id,
        prompt_version="baseline-v1",
        outcome="completed",
        candidate_count=1,
        page_errors=0,
        model="test-model",
        schema_version="baseline-shape-v1",
        candidate_inputs_json=[
            {
                "candidate_id": candidate.id,
                "project_id": project.id,
                "kind": "dependency",
                "source_document_id": document.id,
                "payload_json": payload,
                "source_pages": [1],
                "confidence": 0.99,
                "prompt_version": "baseline-v1",
                "model": "test-model",
                "citations_verified": True,
                "state": "pending",
            }
        ],
    )
    session.add(run)
    session.flush()
    candidate.extraction_run_id = run.id
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-BASELINE-1",
        dep_type="utility_relocation",
        title="Storage baseline utility",
        station_from="10+00",
    )
    session.add(dependency)
    session.flush()
    session.add(
        EvidenceLink(
            dependency_id=dependency.id,
            document_id=document.id,
            page_no=1,
            quote="Utility U-17 begins at Station 10+00.",
            verified=True,
        )
    )
    report_run = ReportRun(
        project_id=project.id,
        ruleset_version="baseline-rules-v1",
        snapshot_json={
            "ruleset_version": "baseline-rules-v1",
            "thresholds": {"stale_days": 14},
            "dependencies": {
                dependency.ref_code: {
                    "id": dependency.id,
                    "committed_date": None,
                    "ready": False,
                }
            },
        },
        output_path="out/baseline-report.html",
        document_only=False,
    )
    session.add(report_run)
    session.flush()
    pdf = _pdf_bytes("Baseline release")
    digest = sha256(pdf).hexdigest()
    artifact = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="baseline-report.pdf",
        format="pdf",
        pdf_bytes=pdf,
        pdf_sha256=digest,
        evaluated_on=date(2026, 8, 31),
        ruleset_version="baseline-rules-v1",
        evaluation_context_json={"today": "2026-08-31"},
        provenance_mode="all-supported-sources",
        record_context_json={"dependency_ids": [dependency.id]},
    )
    session.add(artifact)
    session.flush()
    release_path = tmp_path / "baseline-released-report.pdf"
    release_path.write_bytes(pdf)
    has_single_store_release = bool(
        session.scalar(
            text(
                """
                select exists (
                    select 1 from information_schema.columns
                    where table_schema = 'public'
                      and table_name = 'external_report_releases'
                      and column_name = 'content_storage'
                )
                """
            )
        )
    )
    release = None
    if has_single_store_release:
        # #430's release receipt references artifact-owned bytes and context.
        # This branch executes in the combined compatibility gate; pre-#430
        # main uses the already-sealed file fallback until that schema lands.
        release = ExternalReportRelease(
            project_id=project.id,
            artifact_id=artifact.id,
            artifact_name=artifact.artifact_name,
            format="pdf",
            pdf_sha256=artifact.pdf_sha256,
            evaluated_on=artifact.evaluated_on,
            ruleset_version=artifact.ruleset_version,
            provenance_mode=artifact.provenance_mode,
            released_by="local:baseline-reviewer",
            released_by_display="Baseline Reviewer",
        )
        session.add(release)
        session.flush()
    return report_run, artifact, release, run, release_path


def _single_store_selection(release, release_path):
    return {
        "release_id": release.id if release is not None else 9_999_999_990,
        "release_path": None if release is not None else release_path,
    }


def test_baseline_measures_known_copy_families_by_table_and_column(
    session, tmp_path
):
    report_run, artifact, release, run, release_path = _seed_representative_state(
        session, tmp_path
    )

    baseline = build_storage_baseline(
        session,
        selection=BaselineSelection(
            report_run_id=report_run.id,
            extraction_run_id=run.id,
            **_single_store_selection(release, release_path),
        ),
    )

    families = {family["family"]: family for family in baseline["families"]}
    assert set(families) == {
        "accepted_field_map_copies",
        "cited_quote_copies",
        "released_pdf_copies",
        "report_snapshot_copies",
        "run_payload_snapshots",
        "run_extractor_config_copies",
    }
    config_members = {
        (member["table"], member["column"]): member
        for member in families["run_extractor_config_copies"]["members"]
    }
    # The registry is the single owner the per-run copies move to, so it is
    # measured and deliberately outside the removable target.
    assert config_members[("extraction_runs", "extractor_config_json")][
        "target_included"
    ] is True
    assert config_members[("extractor_configurations", "config_json")][
        "target_included"
    ] is False
    quote_members = {
        (member["table"], member["column"]): member
        for member in families["cited_quote_copies"]["members"]
    }
    assert quote_members[("candidates", "payload_json")]["rows"] >= 1
    assert quote_members[("candidates", "payload_json")]["bytes"] > 0
    assert quote_members[("evidence_links", "quote")]["rows"] >= 1
    assert quote_members[("evidence_links", "quote")]["bytes"] > 0
    field_members = families["accepted_field_map_copies"]["members"]
    dependency_columns = {
        member["column"]
        for member in field_members
        if member["table"] == "dependencies"
    }
    assert "typed current-value columns" not in dependency_columns
    assert {"source_ref", "external_org_id", "station_from", "notes"} <= (
        dependency_columns
    )
    pdf_members = families["released_pdf_copies"]["members"]
    assert {member["column"] for member in pdf_members} == {"pdf_bytes"}
    pdf_by_table = {member["table"]: member for member in pdf_members}
    assert pdf_by_table["external_report_artifacts"]["bytes"] >= len(
        artifact.pdf_bytes
    )
    assert pdf_by_table["external_report_releases"]["bytes"] == 0
    assert pdf_by_table["external_report_artifacts"]["target_included"] is False
    assert pdf_by_table["external_report_releases"]["target_included"] is True
    assert families["released_pdf_copies"]["target_bytes"] == 0
    if release is not None:
        stored = session.execute(
            text(
                "select artifact_id, pdf_bytes, evaluation_context_json, "
                "record_context_json from external_report_releases where id = :id"
            ),
            {"id": release.id},
        ).one()
        assert stored.artifact_id == artifact.id
        assert stored.pdf_bytes is None
        assert stored.evaluation_context_json is None
        assert stored.record_context_json is None
    assert all(
        {"table", "column", "path", "rows", "bytes", "present"} <= set(member)
        for family in families.values()
        for member in family["members"]
    )


def test_baseline_freezes_representative_semantics_and_numeric_target(
    session, tmp_path
):
    report_run, artifact, release, run, release_path = _seed_representative_state(
        session, tmp_path
    )
    selection = BaselineSelection(
        report_run_id=report_run.id,
        extraction_run_id=run.id,
        **_single_store_selection(release, release_path),
    )

    first = build_storage_baseline(session, selection=selection)
    second = build_storage_baseline(session, selection=selection)

    assert first == second
    frozen = first["representative_outputs"]
    assert frozen["coordination_report"]["source_id"] == report_run.id
    if release is None:
        assert frozen["release"]["source_path"] == str(release_path)
    else:
        assert frozen["release"]["source_id"] == release.id
        assert frozen["release"]["content"]["artifact_id"] == artifact.id
        assert frozen["release"]["content"]["pdf_sha256"] == artifact.pdf_sha256
        assert frozen["release"]["content"]["evaluation_context"] == (
            artifact.evaluation_context_json
        )
        assert frozen["release"]["content"]["record_context"] == (
            artifact.record_context_json
        )
    assert frozen["extraction_run"]["source_id"] == run.id
    assert all(len(item["sha256"]) == 64 for item in frozen.values())
    assert first["target"] == {
        "metric": "known_duplication_bytes",
        "minimum_reduction_percent": 50,
        "baseline_bytes": first["known_duplication_bytes"],
        "maximum_cutover_bytes": first["known_duplication_bytes"] // 2,
    }
    assert "artifact-owned PDF bytes" in first["metric_definition"]
    assert len(first["sha256"]) == 64


def test_baseline_can_pin_already_sealed_outputs_when_rows_are_not_in_dev_database(
    session, tmp_path
):
    _, _, _, run, _ = _seed_representative_state(session, tmp_path)
    report_path = tmp_path / "coordination-report.pdf"
    report_path.write_bytes(_pdf_bytes("Representative coordination report"))
    release_path = tmp_path / "released-report.pdf"
    release_path.write_bytes(_pdf_bytes("Representative released report"))

    baseline = build_storage_baseline(
        session,
        selection=BaselineSelection(
            report_run_id=9_999_999_991,
            release_id=9_999_999_992,
            extraction_run_id=run.id,
            coordination_report_path=report_path,
            release_path=release_path,
        ),
    )

    report = baseline["representative_outputs"]["coordination_report"]
    release = baseline["representative_outputs"]["release"]
    assert report["source_path"] == str(report_path)
    assert report["artifact"]["sha256"] == sha256(report_path.read_bytes()).hexdigest()
    assert report["content"]["pages"][0]["text"].strip() == (
        "Representative coordination report"
    )
    assert release["source_path"] == str(release_path)
    assert release["artifact"]["sha256"] == sha256(release_path.read_bytes()).hexdigest()
    assert release["content"]["pages"][0]["text"].strip() == (
        "Representative released report"
    )
