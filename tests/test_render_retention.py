"""Renders are deletable Class B intermediaries; citations never depend on them.

ADR-0068 verifies a citation by replaying its source segment against the
document's pinned, content-addressed bytes — never against a render. ADR-0072
classifies every render as a regenerable Class B intermediary. Together that
means the TTL job may delete every render for a document and both must still
hold: the citation still verifies, and any profile's render rebuilds from the
pinned bytes with the digest the manifest already recorded. These tests prove
the retention wiring keeps that guarantee, and that the persistence seam
classifies every render and raw-OCR intermediary by construction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

from sqlalchemy import select

from corridor.ingest import ingest_document
from corridor.models import PageProcessingFailure, PageRenderDerivative, ProcessingArtifact, RetentionReference, SourceSegment
from corridor.principals import HumanPrincipal
from corridor.render_profiles import regenerate_render_derivative
from corridor.retention import CLASS_B_DAYS, execute_retention, plan_retention
from corridor.source_segments import dereference_source_segment

from pdf_fixture_support import PdfFixture, scan_image


ACTOR = HumanPrincipal("local:retention-operator")


def _minutes_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "coordination-minutes.pdf"
    fixture = PdfFixture()
    fixture.add_page().text(
        (72, 72),
        "Meeting notes and attendance.\n"
        "Action Items:\n"
        "1. Equistar will submit the signed exhibit by March 2025.\n"
        "Meeting Notes",
    )
    return fixture.save(path)


def _scanned_pdf(tmp_path: Path) -> Path:
    """An image-only page, so routing chooses OCR and renders drive extraction."""

    scan = scan_image(
        595,
        842,
        dpi=300,
        lines=(
            ((72, 120), "UTILITY RELOCATION AGREEMENT", 22),
            ((72, 170), "CENTERPOINT ENERGY", 22),
        ),
    )
    fixture = PdfFixture()
    fixture.add_page().image((0, 0, 595, 842), scan)
    return fixture.save(tmp_path / "scanned.pdf")


def test_deleting_every_render_leaves_citations_verifiable_and_regenerable(
    session, project, tmp_path
):
    pdf = _minutes_pdf(tmp_path)
    document = ingest_document(
        session,
        project_id=project.id,
        path=pdf,
        doc_type="minutes",
        images_dir=tmp_path / "images",
    )
    segments = session.scalars(
        select(SourceSegment).where(SourceSegment.document_id == document.id)
    ).all()
    assert segments, "minutes ingest must append replayable prose spans"

    # Baseline: citations verify, and every render is Class B by construction.
    assert [dereference_source_segment(document, s, pdf) for s in segments] == [
        s.exact_text for s in segments
    ]
    derivatives = session.scalars(
        select(PageRenderDerivative).where(
            PageRenderDerivative.document_id == document.id
        )
    ).all()
    assert {d.profile_name for d in derivatives} == {"review", "ocr_layout", "table_cv"}
    render_artifacts = session.scalars(
        select(ProcessingArtifact).where(
            ProcessingArtifact.project_id == project.id,
            ProcessingArtifact.kind == "page_render",
        )
    ).all()
    assert {a.storage_path for a in render_artifacts} == {
        d.artifact_path for d in derivatives
    }

    # Run the real TTL path far enough ahead to delete every Class B render.
    future = datetime.now(timezone.utc) + timedelta(days=CLASS_B_DAYS + 1)
    manifest = plan_retention(session, as_of=future, principal=ACTOR)
    execute_retention(
        session,
        manifest_id=manifest.id,
        expected_sha256=manifest.content_sha256,
        executed_at=future,
    )

    # Every render file is gone and its artifact is marked deleted.
    for derivative in derivatives:
        assert not Path(derivative.artifact_path).is_file()
    assert all(
        a.deleted_at is not None
        for a in session.scalars(
            select(ProcessingArtifact).where(
                ProcessingArtifact.project_id == project.id,
                ProcessingArtifact.kind == "page_render",
            )
        ).all()
    )

    # 1. Citations still verify against the pinned original bytes.
    assert [dereference_source_segment(document, s, pdf) for s in segments] == [
        s.exact_text for s in segments
    ]
    # 2. Any profile's render regenerates from the pinned bytes; 3. its digest
    #    was retained in the manifest and matches the rebuild.
    for derivative in session.scalars(
        select(PageRenderDerivative).where(
            PageRenderDerivative.document_id == document.id
        )
    ).all():
        assert derivative.manifest_json["artifact_sha256"] == derivative.artifact_sha256
        regenerated = regenerate_render_derivative(
            derivative,
            pdf_path=pdf,
            output_dir=tmp_path / "regenerated" / derivative.profile_name,
        )
        assert regenerated.artifact_sha256 == derivative.artifact_sha256


def test_every_render_and_raw_ocr_intermediary_is_class_b_by_construction(
    session, project, tmp_path
):
    pdf = _scanned_pdf(tmp_path)
    document = ingest_document(
        session,
        project_id=project.id,
        path=pdf,
        doc_type="agreement",
        images_dir=tmp_path / "images",
    )
    derivatives = session.scalars(
        select(PageRenderDerivative).where(
            PageRenderDerivative.document_id == document.id
        )
    ).all()
    assert {d.profile_name for d in derivatives} == {"review", "ocr_layout", "table_cv"}

    # Every render — whichever profile — is classified, with the manifest digest.
    for derivative in derivatives:
        artifact = session.scalar(
            select(ProcessingArtifact).where(
                ProcessingArtifact.storage_path == derivative.artifact_path
            )
        )
        assert artifact is not None
        assert artifact.kind == "page_render"
        assert artifact.retention_class == "class_b"
        assert artifact.content_sha256 == derivative.artifact_sha256

    # The raw OCR output is its own registered Class B intermediary.
    raw_ocr = session.scalars(
        select(ProcessingArtifact).where(
            ProcessingArtifact.project_id == project.id,
            ProcessingArtifact.kind == "raw_ocr",
        )
    ).all()
    assert raw_ocr, "each OCR attempt must register a raw-OCR receipt"
    assert all(Path(a.storage_path).is_file() for a in raw_ocr)


def test_an_open_processing_failure_keeps_its_render_and_raw_ocr_reachable(
    session, project, tmp_path
):
    # No customer authorization exists, so the OCR-routed page is refused at
    # the outbound boundary and the refusal is the open Processing Failure
    # this test needs (#732, #741).
    pdf = _scanned_pdf(tmp_path)
    document = ingest_document(
        session,
        project_id=project.id,
        path=pdf,
        doc_type="agreement",
        images_dir=tmp_path / "images",
    )
    [failure] = session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == document.id
        )
    ).all()
    references = session.scalars(
        select(RetentionReference).where(
            RetentionReference.project_id == project.id,
            RetentionReference.kind == "processing_failure",
        )
    ).all()
    assert references, "an open failure must hold its intermediaries reachable"
    assert all(r.family == "processing_artifact" for r in references)
    assert all(
        r.referenced_by == f"page_processing_failure:{failure.id}" for r in references
    )
    assert all(r.closed_at is None for r in references)

    referenced_ids = {r.source_row_id for r in references}
    raw_ocr = session.scalar(
        select(ProcessingArtifact).where(
            ProcessingArtifact.project_id == project.id,
            ProcessingArtifact.kind == "raw_ocr",
        )
    )
    ocr_layout = session.scalar(
        select(PageRenderDerivative).where(
            PageRenderDerivative.document_id == document.id,
            PageRenderDerivative.profile_name == "ocr_layout",
        )
    )
    ocr_layout_artifact = session.scalar(
        select(ProcessingArtifact).where(
            ProcessingArtifact.storage_path == ocr_layout.artifact_path
        )
    )
    assert raw_ocr.id in referenced_ids
    assert ocr_layout_artifact.id in referenced_ids
