"""The turnkey, exact-Document Product Proving extraction adapter."""

from __future__ import annotations

from hashlib import sha256

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import product_proving_extraction
from corridor.db import engine
from corridor.extraction_runs import record_extraction_run
from corridor.extractor_lineage import deployed_extractor_config
from corridor.llm import Usage
from corridor.models import DocPage, Document, ExtractionRun, Project
from corridor.product_proving_extraction import (
    ProductProvingExtractionError,
    extract_product_proving_document,
)


class StubClient:
    model = "gpt-5.6-luna"
    effort = "none"
    flex = False
    base_url = "https://api.openai.com/v1"
    max_workers = 1

    def __init__(self, *, events=()):
        self.events = list(events)
        self.usage = Usage()
        self.calls: list[dict] = []
        self.closed = False

    def complete(self, **request):
        self.calls.append(request)
        return {"events": list(self.events)}

    def close(self):
        self.closed = True


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


@pytest.fixture
def project(session):
    value = Project(
        slug="product-proving-extraction",
        name="Product Proving Extraction",
        is_synthetic=True,
    )
    session.add(value)
    session.flush([value])
    return value


def _document(session, project, *, doc_type="minutes", name="source.pdf"):
    document = Document(
        project_id=project.id,
        sha256=sha256(f"{project.id}:{name}".encode()).hexdigest(),
        filename=name,
        doc_type=doc_type,
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    return document


def _record(
    session,
    document,
    client,
    *,
    extractor=None,
    outcome="completed",
    sealed=True,
):
    extractor_name = extractor or (
        "matrix" if document.doc_type == "matrix" else "minutes"
    )
    if not sealed:
        return record_extraction_run(
            session,
            document,
            prompt_version="legacy_fixture",
            candidate_count=0,
            page_errors=0,
            allow_unsealed_legacy=True,
        )
    config = deployed_extractor_config(extractor_name, client=client)
    return record_extraction_run(
        session,
        document,
        prompt_version=config.prompt_version,
        candidate_count=0,
        page_errors=0 if outcome == "completed" else 1,
        outcome=outcome,
        model=config.model,
        schema_version=config.schema_version,
        extractor_config=config,
        token_usage={
            "scope": "run",
            "document_ids": [document.id],
            "measurement": "exact",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
        },
    )


@pytest.mark.parametrize(
    ("doc_type", "implementation_name", "extractor_name"),
    (
        ("matrix", "_extract_matrix_document", "matrix"),
        ("minutes", "_extract_minutes_document", "minutes"),
    ),
)
def test_it_derives_one_new_current_sealed_run_for_the_exact_document(
    session,
    project,
    monkeypatch,
    doc_type,
    implementation_name,
    extractor_name,
):
    document = _document(session, project, doc_type=doc_type, name=f"{doc_type}.pdf")
    client = StubClient()
    calls = []

    def operation(db, target, shared_client):
        calls.append((db, target, shared_client))
        _record(db, target, shared_client, extractor=extractor_name)
        return 999_999  # the adapter derives identity; it never trusts this

    monkeypatch.setattr(product_proving_extraction, implementation_name, operation)

    run_id = extract_product_proving_document(session, document, client=client)

    [call] = calls
    assert call == (session, document, client)
    run = session.get(ExtractionRun, run_id)
    assert run.document_id == document.id
    assert run.outcome == "completed"
    assert run.extractor_config_json["extractor"] == extractor_name
    assert run.token_usage_json["document_ids"] == [document.id]
    assert client.closed is False


def test_minutes_path_uses_v5_page_wiring_without_committing_or_closing(
    session, project, monkeypatch
):
    document = _document(session, project, name="meeting-minutes.pdf")
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text="Registered meeting minutes with no attributable statement. " * 8,
            text_source="text_layer",
        )
    )
    session.flush()
    client = StubClient()

    def refuse_commit():
        raise AssertionError("bounded extraction must not commit")

    monkeypatch.setattr(session, "commit", refuse_commit)

    run_id = extract_product_proving_document(session, document, client=client)

    run = session.get(ExtractionRun, run_id)
    assert run.prompt_version == "minutes_v5"
    assert run.schema_version == "minutes_v5"
    assert run.extractor_config_json["extractor"] == "minutes"
    assert run.candidate_count == 0
    assert len(client.calls) == 1
    assert client.closed is False


@pytest.mark.parametrize("doc_type", ("agreement", "email", "other"))
def test_it_refuses_unsupported_document_types(session, project, doc_type):
    document = _document(session, project, doc_type=doc_type, name=f"{doc_type}.pdf")
    before = set(session.scalars(select(ExtractionRun.id)).all())

    with pytest.raises(ProductProvingExtractionError, match="only matrix and minutes"):
        extract_product_proving_document(session, document, client=StubClient())

    assert set(session.scalars(select(ExtractionRun.id)).all()) == before


def test_it_refuses_a_missing_or_extra_extraction_run(session, project, monkeypatch):
    missing = _document(session, project, name="missing.pdf")
    monkeypatch.setattr(
        product_proving_extraction,
        "_extract_minutes_document",
        lambda *_: None,
    )

    with pytest.raises(ProductProvingExtractionError, match="observed 0"):
        extract_product_proving_document(session, missing, client=StubClient())

    extra = _document(session, project, name="extra.pdf")
    client = StubClient()

    def create_two(db, document, shared_client):
        _record(db, document, shared_client)
        _record(db, document, shared_client)

    monkeypatch.setattr(
        product_proving_extraction,
        "_extract_minutes_document",
        create_two,
    )

    with pytest.raises(ProductProvingExtractionError, match="observed 2"):
        extract_product_proving_document(session, extra, client=client)


@pytest.mark.parametrize(
    ("sealed", "outcome", "message"),
    (
        (False, "completed", "unsealed legacy"),
        (True, "failed", "completed zero-error"),
    ),
)
def test_it_refuses_unsealed_and_failed_runs(
    session, project, monkeypatch, sealed, outcome, message
):
    document = _document(
        session,
        project,
        name=f"{sealed}-{outcome}.pdf",
    )

    def invalid_run(db, target, client):
        _record(db, target, client, sealed=sealed, outcome=outcome)

    monkeypatch.setattr(
        product_proving_extraction,
        "_extract_minutes_document",
        invalid_run,
    )

    with pytest.raises(ProductProvingExtractionError, match=message):
        extract_product_proving_document(session, document, client=StubClient())


@pytest.mark.parametrize("different_project", (False, True))
def test_it_refuses_a_run_for_the_wrong_document_or_project(
    session, project, monkeypatch, different_project
):
    expected = _document(session, project, name="expected.pdf")
    target_project = project
    if different_project:
        target_project = Project(
            slug="other-product-proving-project",
            name="Other Product Proving Project",
            is_synthetic=True,
        )
        session.add(target_project)
        session.flush([target_project])
    wrong = _document(session, target_project, name="wrong.pdf")

    def wrong_run(db, _document, client):
        _record(db, wrong, client)

    monkeypatch.setattr(
        product_proving_extraction,
        "_extract_minutes_document",
        wrong_run,
    )

    with pytest.raises(ProductProvingExtractionError, match="wrong Document/Project"):
        extract_product_proving_document(session, expected, client=StubClient())


def test_it_refuses_a_sealed_but_stale_or_wrong_extractor(
    session, project, monkeypatch
):
    document = _document(session, project, doc_type="matrix", name="matrix.pdf")

    def minutes_run(db, target, client):
        _record(db, target, client, extractor="minutes")

    monkeypatch.setattr(
        product_proving_extraction,
        "_extract_matrix_document",
        minutes_run,
    )

    with pytest.raises(ProductProvingExtractionError, match="deployed extractor seal"):
        extract_product_proving_document(session, document, client=StubClient())
