"""A Source Fact value materializes from a Source Segment, never from a model (#446).

Hostile model responses — an invented value, a segment from another document,
a segment outside the set the model was shown, an undeclared literal field —
must not reach a Source Segment or a Source Fact, and the 188 historical
vision misreads must fail materialization rather than become values.
"""

import dataclasses
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

from openpyxl import Workbook
import pytest
from sqlalchemy import func, select

from corridor.db import Session, engine
from corridor.extract_sheet import PROMPT_VERSION, SCHEMA_VERSION, extract_document
from corridor.extraction_runs import append_source_facts
from corridor.ingest import ingest_document
from corridor.materializer import (
    FactReplayMismatch,
    MaterializationRefused,
    MaterializedValue,
    materialize_quoted_statement_wording,
    materialize_segment_value,
)
from corridor.models import (
    Candidate,
    Dependency,
    ExtractedProposal,
    ExtractionRun,
    ExternalOrg,
    Fact,
    Project,
    SourceFactAppendReceipt,
    SourceSegment,
)
from corridor.prose_interpretation import interpret_prose_document
from corridor.source_append import SegmentValues, append_fact, append_source_segments
from corridor.typed_output import TypedOutputValidationError

from pdf_fixture_support import PdfFixture


MISREADS = Path(__file__).parent / "fixtures" / "vision-misreads.json"
STATEMENT = "Equistar will submit the signed exhibit by March 2025."
ATTRIBUTION = "Equistar coordination subject."


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
    project = Project(
        slug="fact-materialization", name="Fact Materialization", is_synthetic=True
    )
    session.add(project)
    session.flush()
    return project


def _cells(session, project, texts, *, filename="cells.xlsx"):
    """Append one spreadsheet cell segment per text through the append command."""

    from corridor.models import Document

    document = Document(
        project_id=project.id,
        sha256=sha256(filename.encode()).hexdigest(),
        filename=filename,
        doc_type="matrix",
    )
    session.add(document)
    session.flush()
    return append_source_segments(
        session,
        project_id=project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=tuple(
            SegmentValues(
                kind="spreadsheet_cell",
                exact_text=text,
                content_sha256=sha256(text.encode()).hexdigest(),
                ordinal=index + 1,
                sheet_name="Utility Conflicts",
                cell_range=f"A{index + 1}",
            )
            for index, text in enumerate(texts)
        ),
    )


# --- The type boundary -----------------------------------------------------


def test_a_materialized_value_cannot_be_minted_or_altered_outside_the_materializer(
    session, project
):
    (segment,) = _cells(session, project, ["1140+02"])
    genuine = materialize_segment_value(session, "station_from", segment)

    assert genuine.text_value == "1140+02"
    assert genuine.source_links == (("value_source", segment.id),)
    assert genuine.materializer_version == "fact_materializer_v1"
    with pytest.raises(MaterializationRefused):
        MaterializedValue(
            fact_type="station_from",
            transformation="trim_cell_text_v1",
            source_links=(("value_source", segment.id),),
            text_value="1140+22",
        )
    with pytest.raises(MaterializationRefused):
        MaterializedValue(
            fact_type="station_from",
            transformation="trim_cell_text_v1",
            source_links=(("value_source", segment.id),),
            text_value="1140+22",
            seal=genuine.seal,
        )
    with pytest.raises(MaterializationRefused):
        dataclasses.replace(genuine, text_value="1140+22")
    with pytest.raises(MaterializationRefused):
        dataclasses.replace(genuine, source_links=(("value_source", segment.id + 1),))
    assert dataclasses.replace(genuine) == genuine


def test_a_value_needs_a_stored_segment_whose_digest_proves_its_words(
    session, project
):
    unsaved = SourceSegment(
        project_id=project.id,
        kind="spreadsheet_cell",
        exact_text="1140+02",
        content_sha256=sha256(b"1140+02").hexdigest(),
        ordinal=1,
    )
    with pytest.raises(MaterializationRefused, match="stored Source Segment"):
        materialize_segment_value(session, "station_from", unsaved)

    (segment,) = _cells(session, project, ["1140+02"])
    segment.exact_text = "1140+22"
    with pytest.raises(FactReplayMismatch, match="digest does not match"):
        materialize_segment_value(session, "station_from", segment)


def test_the_append_command_takes_no_value_literal(session, project):
    (segment,) = _cells(session, project, ["1140+02"])
    common = dict(
        project_id=project.id,
        document_id=segment.document_id,
        extraction_run_id=None,
        subject_kind="source_row",
        subject_key="Utility Conflicts!1",
        recorded_by="local:test",
        content_sha256=sha256(b"literal").hexdigest(),
    )

    with pytest.raises(TypeError):
        append_fact(session, value="1140+22", **common)
    with pytest.raises(TypeError):
        append_fact(
            session,
            value=materialize_segment_value(session, "station_from", segment),
            text_value="1140+22",
            **common,
        )
    assert session.scalar(select(func.count()).select_from(Fact)) == 0


def test_a_quoted_statement_must_lie_inside_its_segment(session, project):
    (segment,) = append_source_segments(
        session,
        project_id=project.id,
        document_id=_cells(session, project, ["x"])[0].document_id,
        recorded_verbal_origin_id=None,
        segments=(
            SegmentValues(
                kind="prose_span",
                exact_text=STATEMENT,
                content_sha256=sha256(STATEMENT.encode()).hexdigest(),
                ordinal=2,
                page_no=1,
                start_offset=0,
                end_offset=len(STATEMENT),
            ),
        ),
    )

    assert (
        materialize_quoted_statement_wording(segment, "submit the signed exhibit")
        .text_value
        == "submit the signed exhibit"
    )
    with pytest.raises(ValueError, match="must appear in its cited passage"):
        materialize_quoted_statement_wording(segment, "Equistar will pay a penalty.")


# --- Hostile model responses ------------------------------------------------


class StubClient:
    model = "gpt-5.6-luna"
    effort = "none"
    flex = False
    base_url = "https://provider.example/v1"

    def __init__(self, output):
        self.output = output

    def complete(self, *, system, user, schema):
        return self.output


def _minutes(session, project, tmp_path, name, lines):
    path = tmp_path / name
    fixture = PdfFixture()
    fixture.add_page().text(
        (72, 72),
        ("Registered coordination context.\n" * 8)
        + "\n".join(lines)
        + "\nMeeting Notes",
    )
    fixture.save(path)
    document = ingest_document(
        session,
        project_id=project.id,
        path=path,
        filename=f"Meeting Notes/Equistar/{name}",
        doc_type="minutes",
        doc_date=date(2025, 2, 12),
        images_dir=tmp_path / "images",
    )
    segments = tuple(
        session.scalars(
            select(SourceSegment)
            .where(SourceSegment.document_id == document.id)
            .order_by(SourceSegment.ordinal)
        ).all()
    )
    return document, path, segments


@pytest.fixture
def prose(session, project, tmp_path):
    equistar = ExternalOrg(name="Equistar", aliases=["Equistar Chemicals"])
    session.add(equistar)
    session.flush()
    session.add(
        Dependency(
            project_id=project.id,
            external_org_id=equistar.id,
            ref_code="DEP-PI-1",
            dep_type="utility_relocation",
            title="Equistar pipeline",
        )
    )
    document, path, segments = _minutes(
        session,
        project,
        tmp_path,
        "notes.pdf",
        [ATTRIBUTION, "Action Items:", f"1. {STATEMENT}"],
    )
    other_document, _other_path, other_segments = _minutes(
        session,
        project,
        tmp_path,
        "other-notes.pdf",
        ["Kinder Morgan coordination subject.", "Action Items:", "1. KM will pay."],
    )
    return {
        "document": document,
        "path": path,
        "segments": segments,
        "value": next(s for s in segments if s.exact_text == STATEMENT),
        "attribution": next(s for s in segments if s.exact_text == ATTRIBUTION),
        "foreign": next(s for s in other_segments if s.exact_text == "KM will pay."),
        "equistar": equistar,
    }


def _response(prose, *, value_segment_id=None, proposal_extra=None):
    proposal = {
        "fact_type": "statement_wording",
        "sources": [
            {
                "segment_id": value_segment_id or prose["value"].id,
                "role": "value_source",
            },
            {"segment_id": prose["attribution"].id, "role": "attribution_source"},
        ],
        "subject_candidates": [
            {"subject_type": "external_org", "subject_id": prose["equistar"].id}
        ],
    }
    proposal.update(proposal_extra or {})
    return {
        "read_segment_ids": [segment.id for segment in prose["segments"]],
        "proposals": [proposal],
    }


HOSTILE = {
    "invented value": lambda prose: _response(
        prose, proposal_extra={"value": "Equistar will pay a $1M penalty."}
    ),
    "segment from another document": lambda prose: _response(
        prose, value_segment_id=prose["foreign"].id
    ),
    "segment outside the shown set": lambda prose: _response(
        prose, value_segment_id=9_999_999
    ),
    "extra literal field": lambda prose: _response(
        prose, proposal_extra={"stated_party": "Equistar", "committed_date": "2025-03-01"}
    ),
}


@pytest.mark.parametrize("hostile", sorted(HOSTILE))
def test_a_hostile_model_response_enters_no_segment_and_no_fact(
    session, project, prose, hostile
):
    segments_before = session.scalar(
        select(func.count()).select_from(SourceSegment)
    )
    segment_digests_before = set(
        session.scalars(select(SourceSegment.content_sha256)).all()
    )

    with pytest.raises(TypedOutputValidationError):
        interpret_prose_document(
            session,
            prose["document"],
            client=StubClient(HOSTILE[hostile](prose)),
            source_path=prose["path"],
            idempotency_key=f"hostile:{hostile}",
        )

    assert session.scalar(select(func.count()).select_from(Fact)) == 0
    assert session.scalar(select(func.count()).select_from(Candidate)) == 0
    assert session.scalar(select(func.count()).select_from(ExtractionRun)) == 0
    assert session.scalar(select(func.count()).select_from(ExtractedProposal)) == 0
    assert (
        session.scalar(select(func.count()).select_from(SourceFactAppendReceipt))
        == 0
    )
    assert (
        session.scalar(select(func.count()).select_from(SourceSegment))
        == segments_before
    )
    assert (
        set(session.scalars(select(SourceSegment.content_sha256)).all())
        == segment_digests_before
    )


def test_an_honest_response_writes_the_segment_words_not_the_response(
    session, project, prose
):
    result = interpret_prose_document(
        session,
        prose["document"],
        client=StubClient(_response(prose)),
        source_path=prose["path"],
        idempotency_key="honest",
    )

    [fact] = result.append.facts
    assert fact.text_value == prose["value"].exact_text == STATEMENT


# --- The historical vision misreads -----------------------------------------


@pytest.fixture(scope="module")
def corpus():
    return json.loads(MISREADS.read_text())


def test_every_recorded_vision_misread_fails_materialization(
    session, project, corpus
):
    """The cell a misread was cited to materializes the page's words, never
    the transcribed literal.  ``nearby_on_page`` is what the page carried
    where the model read its value; where the corpus recorded no neighbour,
    the page's own tokens stand in."""

    entries = corpus["misreads"]
    cell_texts = [
        " ".join(entry["nearby_on_page"]) or " ".join(corpus["pages"][entry["page"]])
        for entry in entries
    ]
    segments = _cells(session, project, cell_texts, filename="misreads.xlsx")
    assert len(segments) == len(entries) == corpus["counts"]["flagged_field_values"]

    survivors = []
    for entry, segment, cell_text in zip(entries, segments, cell_texts):
        materialized = materialize_segment_value(session, entry["field"], segment)
        if materialized.scalar != cell_text.strip():
            survivors.append((entry["field"], "not the cell", materialized.scalar))
        if materialized.scalar == entry["value"]:
            survivors.append((entry["field"], "misread became a value", entry["value"]))

    assert survivors == []


def test_a_misread_carried_by_an_extracted_proposal_cannot_become_a_fact(
    session, project, tmp_path, monkeypatch, corpus
):
    """The native path: the cell reads 1140+02, the proposal claims 1140+22."""

    import corridor.extract_sheet as extract_sheet

    misread = next(
        entry
        for entry in corpus["misreads"]
        if entry["field"] == "station_from" and entry["nearby_on_page"]
    )
    real_cell = misread["nearby_on_page"][1]
    path = tmp_path / "misread.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) - Utility Conflicts"])
    sheet.append(["Utility Conflict ID", "Utility Owner", "Start Station"])
    sheet.append(["UC-1", "CenterPoint", real_cell])
    book.save(path)
    document = ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type="matrix",
        images_dir=tmp_path / "images",
    )
    monkeypatch.setattr(extract_sheet, "stored_file", lambda value: str(path))
    candidates = extract_document(session, document)
    [candidate] = candidates
    assert candidate.payload_json["fields"]["station_from"] == real_cell
    candidate.payload_json["fields"]["station_from"] = misread["value"]

    with pytest.raises(FactReplayMismatch, match="does not reproduce"):
        append_source_facts(
            session,
            document,
            idempotency_key="misread:native",
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            candidate_count=1,
            page_errors=0,
            candidates=candidates,
            model=None,
            row_accounting_json=candidates.row_accounting,
            allow_unsealed_legacy=True,
            source_path=path,
        )

    assert session.scalar(select(func.count()).select_from(Fact)) == 0
    assert session.scalar(select(func.count()).select_from(ExtractionRun)) == 0
