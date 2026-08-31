"""Typed stationing facts through completed extraction and replay seams."""

import os
from pathlib import Path

from openpyxl import Workbook
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.db import Session, engine
from corridor.config import settings
from corridor.extract_sheet import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    extract_document,
)
from corridor.extraction_runs import record_extraction_run
from corridor.facts import FactReplayMismatch, replay_fact
from corridor.ingest import ingest_document
from corridor.models import (
    Fact,
    FactSource,
    Project,
    SourceSegment,
)


HEADINGS = [
    "Utility Conflict ID",
    "Utility Owner",
    "Utility Type",
    "Start Station",
    "End Station",
    "Utility Conflict Description",
]
REAL_WORKBOOK_SHA256 = (
    "3cd94fea058a3e61ac95ab1efd566e684e146f64ce6f25048d93cf9db55f83ba"
)
CORPUS_STORE = Path(
    os.environ.get("CORRIDOR_TEST_CORPUS_STORE", settings.corpus_store)
)
REAL_WORKBOOK = (
    CORPUS_STORE / REAL_WORKBOOK_SHA256[:2] / f"{REAL_WORKBOOK_SHA256}.xlsx"
)
needs_corpus = pytest.mark.skipif(
    not REAL_WORKBOOK.exists(),
    reason="run `make corpus` to fetch the I-35 NEX South workbook",
)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(slug="fact-test", name="Fact Test", is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _workbook(tmp_path, name, rows):
    path = tmp_path / name
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) - Utility Conflicts"])
    sheet.append(HEADINGS)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


def _ingest(session, project, path, tmp_path):
    document = ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type="matrix",
        images_dir=tmp_path / "images",
    )
    document._stored_path = str(path)
    return document


def _complete_extraction(session, document, monkeypatch):
    import corridor.extract_sheet as extract_sheet

    monkeypatch.setattr(
        extract_sheet,
        "stored_file",
        lambda value: getattr(value, "_stored_path", None),
    )
    candidates = extract_document(session, document)
    run = record_extraction_run(
        session,
        document,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model=None,
        row_accounting_json=candidates.row_accounting,
        allow_unsealed_legacy=True,
    )
    return run, candidates


def test_completed_native_extraction_appends_typed_stationing_facts(
    session, project, tmp_path, monkeypatch
):
    path = _workbook(
        tmp_path,
        "stationing.xlsx",
        [
            ["UC-1", "CenterPoint", "Electric", " 1149+00 ", "1150+00", "Pole"],
            ["UC-2", "AT&T", "Telecom", "1151+00", "1152+00", "Duct"],
        ],
    )
    document = _ingest(session, project, path, tmp_path)

    run, _candidates = _complete_extraction(session, document, monkeypatch)

    facts = session.scalars(select(Fact).order_by(Fact.id)).all()
    assert [
        (
            fact.fact_type,
            fact.subject_kind,
            fact.subject_key,
            fact.text_value,
            fact.transformation,
            fact.document_id,
            fact.extraction_run_id,
        )
        for fact in facts
    ] == [
        (
            "station_from",
            "source_row",
            "Utility Conflicts!3",
            "1149+00",
            "trim_cell_text_v1",
            document.id,
            run.id,
        ),
        (
            "station_to",
            "source_row",
            "Utility Conflicts!3",
            "1150+00",
            "trim_cell_text_v1",
            document.id,
            run.id,
        ),
        (
            "station_from",
            "source_row",
            "Utility Conflicts!4",
            "1151+00",
            "trim_cell_text_v1",
            document.id,
            run.id,
        ),
        (
            "station_to",
            "source_row",
            "Utility Conflicts!4",
            "1152+00",
            "trim_cell_text_v1",
            document.id,
            run.id,
        ),
    ]
    sources = session.scalars(select(FactSource).order_by(FactSource.fact_id)).all()
    assert [(row.role, row.ordinal) for row in sources] == [
        ("value_source", 1),
        ("value_source", 1),
        ("value_source", 1),
        ("value_source", 1),
    ]
    assert all(not hasattr(row, "exact_text") for row in sources)


def test_every_stationing_fact_replays_from_its_exact_cell(
    session, project, tmp_path, monkeypatch
):
    path = _workbook(
        tmp_path,
        "replay.xlsx",
        [["UC-1", "CenterPoint", "Electric", " 1149+00 ", "1150+00", "Pole"]],
    )
    document = _ingest(session, project, path, tmp_path)
    _complete_extraction(session, document, monkeypatch)
    facts = session.scalars(select(Fact).order_by(Fact.id)).all()

    assert [replay_fact(session, document, fact, path) for fact in facts] == [
        "1149+00",
        "1150+00",
    ]


@needs_corpus
def test_real_dev_corpus_matrix_yields_queryable_replayable_stationing_facts(
    session, project, tmp_path, monkeypatch
):
    document = _ingest(session, project, REAL_WORKBOOK, tmp_path)
    run, candidates = _complete_extraction(session, document, monkeypatch)
    facts = session.scalars(select(Fact).order_by(Fact.id)).all()

    assert len(candidates) == 68
    assert len(facts) == 136
    assert {fact.extraction_run_id for fact in facts} == {run.id}
    assert (
        facts[0].fact_type,
        facts[0].subject_key,
        facts[0].text_value,
        replay_fact(session, document, facts[0], REAL_WORKBOOK),
    ) == ("station_from", "UCM-Conflict List!9", "329312.79", "329312.79")
    assert (
        facts[-1].fact_type,
        facts[-1].subject_key,
        facts[-1].text_value,
        replay_fact(session, document, facts[-1], REAL_WORKBOOK),
    ) == ("station_to", "UCM-Conflict List!76", "-", "-")


def test_fact_replay_fails_closed_when_materialized_value_does_not_reproduce(
    session, project, tmp_path, monkeypatch
):
    path = _workbook(
        tmp_path,
        "mismatch.xlsx",
        [["UC-1", "CenterPoint", "Electric", "1149+00", "1150+00", "Pole"]],
    )
    document = _ingest(session, project, path, tmp_path)
    _complete_extraction(session, document, monkeypatch)
    fact = session.scalars(select(Fact).order_by(Fact.id)).first()
    fact.text_value = "9999+99"

    with pytest.raises(FactReplayMismatch, match="does not reproduce"):
        replay_fact(session, document, fact, path)


def test_database_rejects_fact_source_from_another_rendition(
    session, project, tmp_path, monkeypatch
):
    first_path = _workbook(
        tmp_path,
        "first.xlsx",
        [["UC-1", "CenterPoint", "Electric", "1149+00", "1150+00", "Pole"]],
    )
    first = _ingest(session, project, first_path, tmp_path)
    _complete_extraction(session, first, monkeypatch)
    fact = session.scalars(select(Fact).order_by(Fact.id)).first()

    second_path = _workbook(
        tmp_path,
        "second.xlsx",
        [["UC-2", "AT&T", "Telecom", "210+00", "220+00", "Duct"]],
    )
    second = _ingest(session, project, second_path, tmp_path)
    foreign = session.scalars(
        select(SourceSegment).where(SourceSegment.document_id == second.id)
    ).first()
    session.add(
        FactSource(
            project_id=project.id,
            document_id=first.id,
            fact_id=fact.id,
            source_segment_id=foreign.id,
            role="context",
            ordinal=2,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_stationing_fact_type_requires_only_its_typed_text_value(
    session, project, tmp_path
):
    path = _workbook(tmp_path, "typed.xlsx", [])
    document = _ingest(session, project, path, tmp_path)
    fact = Fact(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=None,
        fact_type="station_from",
        subject_kind="source_row",
        subject_key="Utility Conflicts!3",
        text_value=None,
        date_value=None,
        date_range_start=None,
        date_range_end=None,
        external_org_value_id=None,
        document_value_id=None,
        transformation="trim_cell_text_v1",
        recorded_by="extractor:sheet_native_v2",
    )
    session.add(fact)

    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_facts_have_no_update_or_delete_path(
    session, project, tmp_path, monkeypatch, operation
):
    path = _workbook(
        tmp_path,
        "immutable.xlsx",
        [["UC-1", "CenterPoint", "Electric", "1149+00", "1150+00", "Pole"]],
    )
    document = _ingest(session, project, path, tmp_path)
    _complete_extraction(session, document, monkeypatch)
    fact = session.scalars(select(Fact).order_by(Fact.id)).first()
    if operation == "update":
        fact.text_value = "rewritten"
    else:
        session.delete(fact)

    with pytest.raises(DBAPIError, match="facts are append-only"):
        session.flush()
