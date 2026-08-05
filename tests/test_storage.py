from pathlib import Path

import pytest

from corridor.config import settings
from corridor.models import Document
from corridor.storage import stored_file, stored_pdf


def make_document(*, sha256: str) -> Document:
    return Document(
        project_id=1,
        sha256=sha256,
        filename="source.pdf",
        doc_type="matrix",
    )


def test_stored_file_resolves_a_document_from_the_content_addressed_store(
    tmp_path, monkeypatch
):
    sha = "ab" * 32
    stored = tmp_path / sha[:2] / f"{sha}.xlsx"
    stored.parent.mkdir(parents=True)
    stored.write_text("xlsx bytes live elsewhere; the suffix is the point")
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path))

    path = stored_file(make_document(sha256=sha))

    assert path == stored


@pytest.mark.parametrize("document", [None, make_document(sha256="")])
def test_stored_file_is_none_without_a_hash(document, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path))

    assert stored_file(document) is None


def test_stored_pdf_refuses_a_non_pdf_even_when_the_document_exists(
    tmp_path, monkeypatch
):
    sha = "cd" * 32
    stored = tmp_path / sha[:2] / f"{sha}.xlsx"
    stored.parent.mkdir(parents=True)
    stored.write_text("workbook")
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path))

    assert stored_pdf(make_document(sha256=sha)) is None


def test_stored_pdf_returns_the_pdf_when_the_stored_document_is_a_pdf(
    tmp_path, monkeypatch
):
    sha = "ef" * 32
    stored = tmp_path / sha[:2] / f"{sha}.pdf"
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path))

    assert stored_pdf(make_document(sha256=sha)) == stored


def test_docs_can_import_the_pipeline_entrypoint_at_module_scope():
    from corridor.docs import ingest_manifest as docs_ingest_manifest
    from corridor.pipeline import ingest_manifest

    assert docs_ingest_manifest is ingest_manifest
