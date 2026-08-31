"""Storage-duplication baseline and frozen semantic-output contracts."""

from __future__ import annotations

from datetime import date
from hashlib import sha256

import pymupdf
import pytest

from corridor.db import Session, engine
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


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def _seed_representative_state(session):
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
    pdf = b"%PDF-1.7\nbaseline release\n%%EOF"
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
    release = ExternalReportRelease(
        project_id=project.id,
        artifact_id=artifact.id,
        artifact_name=artifact.artifact_name,
        format="pdf",
        pdf_bytes=pdf,
        pdf_sha256=digest,
        evaluated_on=artifact.evaluated_on,
        ruleset_version=artifact.ruleset_version,
        evaluation_context_json=artifact.evaluation_context_json,
        provenance_mode=artifact.provenance_mode,
        record_context_json=artifact.record_context_json,
        released_by="local:baseline-reviewer",
        released_by_display="Baseline Reviewer",
    )
    session.add(release)
    session.flush()
    return report_run, release, run


def test_baseline_measures_known_copy_families_by_table_and_column(session):
    report_run, release, run = _seed_representative_state(session)

    baseline = build_storage_baseline(
        session,
        selection=BaselineSelection(
            report_run_id=report_run.id,
            release_id=release.id,
            extraction_run_id=run.id,
        ),
    )

    families = {family["family"]: family for family in baseline["families"]}
    assert set(families) == {
        "accepted_field_map_copies",
        "cited_quote_copies",
        "released_pdf_copies",
        "report_snapshot_copies",
        "run_payload_snapshots",
    }
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
    assert sum(member["bytes"] for member in pdf_members) >= len(release.pdf_bytes) * 2
    assert all(
        {"table", "column", "path", "rows", "bytes", "present"} <= set(member)
        for family in families.values()
        for member in family["members"]
    )


def test_baseline_freezes_representative_semantics_and_numeric_target(session):
    report_run, release, run = _seed_representative_state(session)
    selection = BaselineSelection(
        report_run_id=report_run.id,
        release_id=release.id,
        extraction_run_id=run.id,
    )

    first = build_storage_baseline(session, selection=selection)
    second = build_storage_baseline(session, selection=selection)

    assert first == second
    frozen = first["representative_outputs"]
    assert frozen["coordination_report"]["source_id"] == report_run.id
    assert frozen["release"]["source_id"] == release.id
    assert frozen["extraction_run"]["source_id"] == run.id
    assert all(len(item["sha256"]) == 64 for item in frozen.values())
    assert first["target"] == {
        "metric": "known_duplication_bytes",
        "minimum_reduction_percent": 50,
        "baseline_bytes": first["known_duplication_bytes"],
        "maximum_cutover_bytes": first["known_duplication_bytes"] // 2,
    }
    assert len(first["sha256"]) == 64


def test_baseline_can_pin_already_sealed_outputs_when_rows_are_not_in_dev_database(
    session, tmp_path
):
    _, _, run = _seed_representative_state(session)
    report_path = tmp_path / "coordination-report.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Representative coordination report")
        report_path.write_bytes(pdf.tobytes())
    release_path = tmp_path / "released-report.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Representative released report")
        release_path.write_bytes(pdf.tobytes())

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
