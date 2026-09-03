"""Ingest persists both token layers as Class B manifests; geometry reads them.

Native and OCR token layers persist with coordinates, origin, and full engine
pinning (AC1), the storage manifests live in PostgreSQL and their artifacts are
Class B (AC4), geometry-consuming extraction gates on the native token layer
rather than the page-text verdict (AC3), and deleting an expired token layer
through the real TTL leaves every promoted source segment verifiable (AC5).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extract_matrix import _read_geometry
from corridor.ingest import ingest_document
from corridor.models import (
    Document,
    ProcessingArtifact,
    Project,
    SourceSegment,
    TokenLayerManifest,
)
from corridor.object_storage import store_bytes
from corridor.principals import HumanPrincipal
from corridor.retention import CLASS_B_DAYS, execute_retention, plan_retention
from corridor.source_segments import dereference_source_segment
from corridor.token_layers import load_token_layer


ACTOR = HumanPrincipal("local:retention-operator")


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    value = Session(bind=connection)
    yield value
    value.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    row = Project(slug="token-ingest", name="Token Ingest", is_synthetic=True)
    session.add(row)
    session.flush()
    return row


def _minutes_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "minutes.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text(
            (72, 72),
            "Meeting notes and attendance.\n"
            "Action Items:\n"
            "1. Equistar will submit the signed exhibit by March 2025.\n"
            "Meeting Notes",
        )
        pdf.save(path)
    return path


def test_native_token_layer_persists_with_pinning_and_is_class_b(
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
    manifests = session.scalars(
        select(TokenLayerManifest).where(
            TokenLayerManifest.document_id == document.id
        )
    ).all()
    native = [m for m in manifests if m.origin == "native"]
    assert native, "a text page persists a native token layer manifest"
    manifest = native[0]
    assert manifest.token_count > 0
    assert manifest.engine_json["engine"] == "pymupdf"
    assert manifest.engine_json["adapter_version"]
    assert manifest.source_sha256 == document.sha256

    # The manifest's artifact is a Class B ProcessingArtifact (AC4), and the
    # artifact rebuilds the positioned tokens.
    artifact = session.scalar(
        select(ProcessingArtifact).where(
            ProcessingArtifact.storage_path == manifest.artifact_path
        )
    )
    assert artifact is not None
    assert artifact.kind == "token_layer"
    assert artifact.retention_class == "class_b"
    layer = load_token_layer(manifest)
    assert layer.tokens[0].polygon_pdf.x1 > layer.tokens[0].polygon_pdf.x0
    assert layer.tokens[0].confidence is None  # native readings are not estimates


def test_geometry_gates_on_the_native_token_layer_not_the_page_verdict(
    session, project, tmp_path
):
    # A page carrying native tokens but tagged text_source="ocr" (a mixed page)
    # is still eligible for geometry — the old text_layer gate discarded it.
    pdf = tmp_path / "table.pdf"
    with pymupdf.open() as document_pdf:
        page = document_pdf.new_page(width=420, height=320)
        page.insert_text((60, 80), "Owner")
        page.draw_rect(pymupdf.Rect(55, 100, 340, 240))
        page.draw_line((190, 100), (190, 240))
        page.insert_text((75, 140), "AT&T")
        document_pdf.save(pdf)
    import hashlib

    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    # Register the document in the content store so stored_pdf resolves it.
    stored = store_bytes(pdf.read_bytes(), sha256=digest, suffix=".pdf")
    try:
        document = Document(
            project_id=project.id,
            sha256=digest,
            filename="table.pdf",
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        session.add(document)
        session.flush()
        from corridor.models import DocPage

        page_row = DocPage(
            document_id=document.id,
            page_no=1,
            text="",
            text_source="ocr",  # a mixed page whose OCR reading "won"
            image_path=None,
        )
        session.add(page_row)
        session.add(
            TokenLayerManifest(
                document_id=document.id,
                page_no=1,
                origin="native",
                layer_key="k" * 64,
                source_sha256=digest,
                engine_json={"engine": "pymupdf"},
                token_count=5,
                quality_json={},
                artifact_path=str(tmp_path / "tokens.json"),
                artifact_sha256="b" * 64,
                artifact_bytes=10,
            )
        )
        session.flush()
        grids = _read_geometry(session, document, [page_row])
        assert 1 in grids, "geometry runs on a native-token page despite ocr verdict"
    finally:
        stored.unlink(missing_ok=True)


def test_deleting_expired_token_layers_leaves_segments_verifiable(
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
    assert segments
    manifests = session.scalars(
        select(TokenLayerManifest).where(
            TokenLayerManifest.document_id == document.id
        )
    ).all()
    assert manifests

    future = datetime.now(timezone.utc) + timedelta(days=CLASS_B_DAYS + 1)
    manifest = plan_retention(session, as_of=future, principal=ACTOR)
    execute_retention(
        session,
        manifest_id=manifest.id,
        expected_sha256=manifest.content_sha256,
        executed_at=future,
    )

    # Every token-layer artifact is gone, but the promoted citations verify.
    for layer in manifests:
        assert not Path(layer.artifact_path).is_file()
    assert [dereference_source_segment(document, s, pdf) for s in segments] == [
        s.exact_text for s in segments
    ]
