"""Measured native matrix mapping reaches the source spine without record authority."""

from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from math import ceil
from pathlib import Path

from PIL import Image
import pytest
from sqlalchemy import select

from corridor.config import Settings
from corridor.admission import load_project
from corridor.current_record import read_current_project_record
from corridor.db import Session, engine
from corridor.extraction_runs import (
    SourceFactAppendConflict,
    current_active_run_declaration,
    declare_active_run,
    declare_active_run_by_policy,
    declare_single_run_documents,
    declare_single_run_documents_by_policy,
    is_completed_run,
    record_extraction_run,
)
from corridor.facts import proposal_input_snapshots, replay_fact
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    ExtractionRun,
    Fact,
    FactDisposition,
    FactSource,
    Project,
    SourceFactAppendReceipt,
    SourceSegment,
)
from corridor.native_matrix import (
    extract_native_matrix,
    map_native_matrix,
    render_native_matrix_context,
)
from corridor.native_matrix_bindings import NativeMatrixRefused, bind_native_matrix
from corridor.principals import HumanPrincipal
from corridor.reader_segments import (
    NativeCellIndex,
    append_native_segments,
    pdf_cell_id,
    read_native_pdf,
)
from corridor.source_segment_errors import (
    SourceDocumentDigestMismatch,
    SourceSegmentLocatorMismatch,
)
from corridor.token_layers import NativePdfReading
from corridor_pdf_reader.replacement import semantics
from corridor_pdf_reader.replacement.pages import slim_page
from pdf_fixture_support import PdfFixture


HEADINGS = ("ID", "Owner", "Station", "Committed date", "Notes", "Protect", "Relocate")
COLUMN_FIELDS = (
    "utility_id", "external_org", "station_from", "committed_date", "notes",
    "resolution_strategy", "resolution_strategy",
)
COLUMN_EDGES = (20, 100, 270, 380, 500, 690, 780, 870)
BODY_ROWS = (
    ("UC-1", "Exact Utilities", " 1149+00 ", "Summer 2020", "Pole\nclearance", "x", "x"),
    ("UC-2", "not used", "", "", "", "", ""),
    ("UC-3", "", "", "", "", "", ""),
    ("UC-4", "", "1152+00", "", "", "", ""),
    ("", "", "", "", "", "", ""),
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
    value = Project(slug="native-matrix", name="Native matrix", is_synthetic=True)
    session.add(value)
    session.flush()
    return value


def _answer(*, header_row=0, owner=None):
    return {
        "is_utility_matrix": True,
        "matrix_table": 0,
        "header_row": header_row,
        "columns": [
            {"index": column, "canonical_field": field}
            for column, field in enumerate(COLUMN_FIELDS, start=1)
        ],
        "page_attributes": {"external_org": owner},
        "mapping_confidence": 0.93,
    }


def _draw_page(fixture, rows, *, header=True, owner=None):
    page = fixture.add_page(width=900, height=480)
    if owner:
        # A visible repeated-page heading can be marked as a PDF Artifact.
        # Tagged reconstruction then leaves it outside the table while native
        # page text and exact visible span provenance still include it.
        page._content.append("/Artifact BMC\n")
        page.text((30, 30), owner)
        page._content.append("EMC\n")
    values = (HEADINGS, *rows) if header else tuple(rows)
    top, row_height = 70, 45
    bottom = top + row_height * len(values)
    for x in COLUMN_EDGES:
        page.line((x, top), (x, bottom))
    for row in range(len(values) + 1):
        y = top + row_height * row
        page.line((COLUMN_EDGES[0], y), (COLUMN_EDGES[-1], y))
    for row, fields in enumerate(values):
        for column, wording in enumerate(fields):
            if wording:
                page.text(
                    (COLUMN_EDGES[column] + 8, top + row * row_height + 23),
                    wording,
                    fontsize=10,
                )


@dataclass(frozen=True)
class NativeFixture:
    path: Path
    reading: NativePdfReading
    images: dict[int, Path]
    answers: tuple[dict, ...]


def _finish_fixture(fixture, directory, answers):
    path = fixture.save(directory / "matrix.pdf")
    reading = read_native_pdf(path, source_sha256=sha256(path.read_bytes()).hexdigest())
    images = render_native_matrix_context(
        path, directory / "context", [page["number"] for page in reading.pages]
    )
    return NativeFixture(path, reading, images, tuple(answers))


@pytest.fixture(scope="module")
def matrix_source(tmp_path_factory):
    fixture = PdfFixture()
    _draw_page(fixture, BODY_ROWS)
    return _finish_fixture(fixture, tmp_path_factory.mktemp("native-matrix"), [_answer()])


@pytest.fixture(scope="module")
def continuation_source(tmp_path_factory):
    fixture = PdfFixture()
    _draw_page(fixture, [("UC-1", "Exact Utilities", "1149+00", "", "", "x", "")])
    _draw_page(
        fixture,
        [("UC-2", "Exact Utilities", "1152+00", "", "", "x", "x")],
        header=False,
    )
    return _finish_fixture(
        fixture, tmp_path_factory.mktemp("native-continuation"),
        [_answer(), _answer(header_row=None)],
    )


@pytest.fixture(scope="module")
def outside_owner_source(tmp_path_factory):
    fixture = PdfFixture()
    _draw_page(
        fixture, [("UC-1", "", "1149+00", "", "Pole", "", "")],
        owner="Outside Utilities",
    )
    return _finish_fixture(
        fixture, tmp_path_factory.mktemp("native-outside-owner"), [_answer(owner="o0")]
    )


def _document(session, project, source, **overrides):
    values = {
        "project_id": project.id,
        "sha256": source.reading.rendition_sha256,
        "filename": source.path.name,
        "doc_type": "matrix",
        "numbering_scheme": "project-unique",
        "pages": len(source.reading.pages),
        "parse_status": "parsed",
    }
    values.update(overrides)
    value = Document(**values)
    session.add(value)
    session.flush()
    return value


class RecordedStructureClient:
    """Return authored IDs after verifying the actual measured request contract."""

    model = "gpt-5.6-luna"
    effort = "none"
    image_detail = "original"
    flex = False
    base_url = "https://api.openai.com/v1"

    def __init__(self, source, *, answers=None):
        self.source = source
        self.answers = deepcopy(source.answers if answers is None else answers)
        self.requests = []
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0

    def complete(self, *, system, user, schema, images=()):
        page = self.source.reading.pages[self.calls]
        number = page["number"]
        assert system == semantics.PROMPT_PATH.read_text()
        assert schema == semantics.STRUCTURE_SCHEMA
        assert user == semantics.listing(slim_page(page), self.source.path.name)
        assert list(map(Path, images)) == [self.source.images[number]]
        width, height = page["geometry"]["width"], page["geometry"]["height"]
        if page["geometry"]["rotation"] % 180:
            width, height = height, width
        with Image.open(images[0]) as image:
            assert image.size == (ceil(width * 110 / 72), ceil(height * 110 / 72))
        self.requests.append((system, user, deepcopy(schema), tuple(images)))
        response = deepcopy(self.answers[self.calls])
        self.calls += 1
        self.prompt_tokens += 100
        self.completion_tokens += 20
        return response


def _map(document, source, client=None):
    return map_native_matrix(
        document, reading=source.reading, client=client or RecordedStructureClient(source),
        images=source.images, document_label=source.path.name,
    )


def _extract(session, document, source, **overrides):
    values = {
        "reading": source.reading,
        "source_path": source.path,
        "client": RecordedStructureClient(source),
        "images": source.images,
        "document_label": source.path.name,
        "idempotency_key": "native-matrix-test",
    }
    values.update(overrides)
    return extract_native_matrix(session, document, **values)


def _spine_ids(session):
    return {
        model.__tablename__: tuple(session.scalars(select(model.id).order_by(model.id)))
        for model in (
            SourceSegment, ExtractionRun, Fact, FactSource, Candidate,
            ExtractedProposal, ExtractedProposalFact, SourceFactAppendReceipt,
        )
    }


def _fact_sources(session, fact):
    return tuple(
        (link.role, link.ordinal, session.get(SourceSegment, link.source_segment_id))
        for link in session.scalars(
            select(FactSource).where(FactSource.fact_id == fact.id)
            .order_by(FactSource.role, FactSource.ordinal)
        )
    )


def test_measured_request_and_scoped_values_account_for_every_detected_body_row(
    session, project, matrix_source
):
    document = _document(session, project, matrix_source)
    client = RecordedStructureClient(matrix_source)

    mapping = _map(document, matrix_source, client)

    assert client.calls == 1
    assert matrix_source.reading.identity["native_layer"]["dpi"] == 36
    config = mapping.config_record
    assert config["model"] == "gpt-5.6-luna"
    assert config["prompt_version"] == "matrix_structure_ids_v1"
    assert config["prompt_sha256"] == sha256(semantics.PROMPT_PATH.read_bytes()).hexdigest()
    controls = config["config_json"]["request_controls"]
    assert (controls["reasoning_effort"], controls["image_detail"],
            controls["model_image_dpi"], controls["native_reader_engine"],
            controls["native_reader_dpi"], controls["selection"]) == (
        "none", "original", 110, "tagged", 36, "explicit_selection_required",
    )
    page = mapping.pages[0]
    assert page["structure"] == _answer()
    assert page["image_sha256"] == sha256(matrix_source.images[1].read_bytes()).hexdigest()
    assert page["reading"]["mapping_confidence"] == 0.93
    rows = page["reading"]["rows"]
    assert [(row["row"], row["disposition"], row["reason"]) for row in rows] == [
        (1, "extracted", "candidate_recorded"),
        (2, "skipped", "retired_row"),
        (3, "skipped", "insufficient_mapped_fields"),
        (4, "skipped", "missing_required_fields"),
    ], page["listing"]
    # The frozen tagged reader emits glyph-backed rows only. The final blank
    # ruled row in this authored page is absent from its measured input; this
    # adapter accounts for each detected row without inventing another one.
    assert rows[0]["fields"] == {
        "utility_id": {"text": "UC-1", "cells": ["t0r1c1"]},
        "external_org": {"text": "Exact Utilities", "cells": ["t0r1c2"]},
        "station_from": {"text": "1149+00", "cells": ["t0r1c3"]},
        "committed_date": {"text": "Summer 2020", "cells": ["t0r1c4"]},
        "notes": {"text": "Pole clearance", "cells": ["t0r1c5"]},
        "resolution_strategy": {"text": "Protect; Relocate", "cells": ["t0r1c6", "t0r1c7"]},
    }
    bound = {field.name: field for field in mapping.rows[0].fields}
    station = bound["station_from"].value_sources[0]
    assert station.model_id == "t0r1c3"
    assert station.scoped_id == pdf_cell_id(
        document, matrix_source.reading.reading_sha256, 1, 0, 1, 3
    )
    assert (station.expected.page_no, station.expected.table_index,
            station.expected.cell_row, station.expected.cell_column) == (1, 0, 1, 3)
    receipt = mapping.row_accounting
    assert (receipt["detected_row_count"], receipt["accounted_row_count"],
            receipt["extracted_row_count"], receipt["blank_row_count"],
            receipt["skipped_row_count"]) == (4, 4, 1, 0, 3)
    assert len({row["row_id"] for row in receipt["rows"]}) == 4
    assert receipt["unaccounted_rows"] == []
    assert not any(_spine_ids(session).values())


def test_rotated_page_uses_the_measured_display_image_dimensions(
    session, project, tmp_path
):
    fixture = PdfFixture()
    fixture.add_page(width=900, height=480, rotation=90).text(
        (40, 80), "Rotated source context"
    )
    answer = _answer(header_row=None)
    answer.update(is_utility_matrix=False, matrix_table=None, columns=[])
    source = _finish_fixture(fixture, tmp_path, [answer])
    document = _document(session, project, source)
    client = RecordedStructureClient(source)

    mapping = _map(document, source, client)

    assert client.calls == 1
    with Image.open(source.images[1]) as image:
        assert image.size == (ceil(480 * 110 / 72), ceil(900 * 110 / 72))
    assert mapping.pages[0]["reading"]["is_utility_matrix"] is False
    assert mapping.rows == ()


def test_native_extraction_seals_source_facts_and_proposals_without_accepting_them(
    session, project, matrix_source
):
    document = _document(session, project, matrix_source)
    assert read_current_project_record(session, project.id) == ()

    result = _extract(session, document, matrix_source)

    assert result.created is True
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    expected = {
        "utility_id": "UC-1", "external_org": "Exact Utilities",
        "station_from": "1149+00", "committed_date": "Summer 2020",
        "notes": "Pole clearance", "resolution_strategy": "Protect; Relocate",
    }
    assert candidate.payload_json["fields"] == expected
    assert candidate.extraction_run_id == result.run.id
    assert candidate.state == "pending"
    facts = {fact.fact_type: fact for fact in result.facts}
    assert {name: fact.text_value for name, fact in facts.items()} == {
        name: value for name, value in expected.items() if name != "committed_date"
    }
    assert all(fact.document_id == document.id for fact in facts.values())
    assert all(fact.extraction_run_id == result.run.id for fact in facts.values())
    for name in ("utility_id", "external_org", "station_from", "notes"):
        assert facts[name].transformation == "collapse_pdf_whitespace_v1"
        sources = _fact_sources(session, facts[name])
        assert [(role, ordinal) for role, ordinal, _segment in sources] == [("value_source", 1)]
        segment = sources[0][2]
        assert (segment.kind, segment.page_no, segment.table_index, segment.cell_row) == (
            "pdf_cell", 1, 0, 1,
        )
        assert segment.reading_sha256 == matrix_source.reading.reading_sha256
        assert replay_fact(
            session, document, facts[name], matrix_source.path,
            native_reading=matrix_source.reading,
        ) == expected[name]
    notes_segment = _fact_sources(session, facts["notes"])[0][2]
    assert notes_segment.exact_text == "Pole\nclearance"
    assert notes_segment.content_sha256 == sha256(b"Pole\nclearance").hexdigest()
    resolution = facts["resolution_strategy"]
    assert resolution.transformation == "pdf_marked_resolution_v1"
    assert [
        (role, ordinal, segment.page_no, segment.cell_row, segment.cell_column, segment.exact_text)
        for role, ordinal, segment in _fact_sources(session, resolution)
    ] == [
        ("context", 1, 1, 1, 6, "x"), ("context", 2, 1, 1, 7, "x"),
        ("value_source", 1, 1, 0, 6, "Protect"),
        ("value_source", 2, 1, 0, 7, "Relocate"),
    ]
    assert replay_fact(
        session, document, resolution, matrix_source.path, native_reading=matrix_source.reading
    ) == "Protect; Relocate"
    proposal = session.scalar(select(ExtractedProposal))
    assert proposal.candidate_id == candidate.id
    assert proposal.subject_key == result.mapping.rows[0].row_id
    assert len(session.scalars(select(ExtractedProposalFact)).all()) == len(facts)
    assert proposal_input_snapshots(session, result.run)[0]["payload_json"]["fields"] == expected
    assert read_current_project_record(session, project.id) == ()
    for model in (Dependency, FactDisposition, ActiveExtractionRun):
        assert session.scalars(select(model)).all() == []


def test_non_iso_date_remains_source_bound_with_a_separate_fact_refusal(
    session, project, matrix_source
):
    document = _document(session, project, matrix_source)

    result = _extract(session, document, matrix_source)

    outcomes = [outcome for outcome in result.field_outcomes if outcome["field"] == "committed_date"]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert (outcome["status"], outcome["reason"]) == ("refused", "non_iso_date")
    assert len(outcome["value_source_ids"]) == 1 and outcome["context_source_ids"] == []
    segment = session.get(SourceSegment, outcome["value_source_ids"][0])
    assert (segment.kind, segment.page_no, segment.cell_row, segment.cell_column,
            segment.exact_text) == ("pdf_cell", 1, 1, 4, "Summer 2020")
    candidate = result.candidates[0]
    assert candidate.payload_json["fields"]["committed_date"] == "Summer 2020"
    references = candidate.payload_json["field_sources"]["committed_date"]
    assert references == {
        "value_source": [{
            "segment_id": segment.id,
            "scoped_id": pdf_cell_id(document, matrix_source.reading.reading_sha256, 1, 0, 1, 4),
            "model_id": "t0r1c4",
        }],
        "context": [],
    }
    assert not any(fact.fact_type in {"committed_date", "statement_wording", "statement_timing"}
                   for fact in result.facts)
    assert result.run.row_accounting_json["extracted_row_count"] == 1
    assert sum(outcome["status"] == "materialized" for outcome in result.field_outcomes) == 5
    assert {outcome["status"] for outcome in result.field_outcomes} == {
        "materialized", "refused", "not_extracted",
    }
    altered = deepcopy(candidate.payload_json)
    altered["fields"]["committed_date"] = "2020-06-01"
    candidate.payload_json = altered
    session.flush()
    preserved = proposal_input_snapshots(session, result.run)[0]["payload_json"]
    assert preserved["fields"]["committed_date"] == "Summer 2020"
    assert preserved["field_sources"]["committed_date"] == references


def test_retry_returns_original_source_spine_rows(session, project, matrix_source):
    document = _document(session, project, matrix_source)
    first = _extract(session, document, matrix_source)
    original = _spine_ids(session)

    replayed = _extract(session, document, matrix_source)

    assert first.created is True and replayed.created is False
    assert replayed.run.id == first.run.id
    assert [fact.id for fact in replayed.facts] == [fact.id for fact in first.facts]
    assert [candidate.id for candidate in replayed.candidates] == [candidate.id for candidate in first.candidates]
    assert replayed.field_outcomes == first.field_outcomes
    assert _spine_ids(session) == original
    changed = _answer()
    changed["mapping_confidence"] = 0.75
    client = RecordedStructureClient(matrix_source, answers=[changed])
    with pytest.raises(SourceFactAppendConflict, match="another mapping"):
        _extract(session, document, matrix_source, client=client)
    assert _spine_ids(session) == original


def test_new_mapping_owns_new_facts_while_exact_mapping_retries_keep_their_run(
    session, project, matrix_source,
):
    document = _document(session, project, matrix_source)
    first = _extract(session, document, matrix_source, idempotency_key=None)
    first_facts = {fact.id for fact in first.facts}
    first_snapshots = proposal_input_snapshots(session, first.run)
    first_links = {
        (link.proposal_id, link.fact_id)
        for link in session.scalars(select(ExtractedProposalFact)).all()
    }

    retry = _extract(session, document, matrix_source, idempotency_key=None)
    fresh_key = _extract(session, document, matrix_source, idempotency_key="another-exact-mapping-request")
    for same in (retry, fresh_key):
        assert same.created is False
        assert same.run.id == first.run.id
        assert {fact.id for fact in same.facts} == first_facts

    changed = _answer()
    changed["mapping_confidence"] = 0.75
    second = _extract(
        session, document, matrix_source, idempotency_key=None,
        client=RecordedStructureClient(matrix_source, answers=[changed]),
    )

    assert second.created is True and second.run.id != first.run.id
    assert second.mapping.identity != first.mapping.identity
    second_facts = {fact.id for fact in second.facts}
    assert second_facts and first_facts.isdisjoint(second_facts)
    assert all(fact.extraction_run_id == first.run.id for fact in first.facts)
    assert all(fact.extraction_run_id == second.run.id for fact in second.facts)
    for proposal in session.scalars(select(ExtractedProposal)).all():
        ids = set(session.scalars(select(ExtractedProposalFact.fact_id).where(
            ExtractedProposalFact.proposal_id == proposal.id,
        )).all())
        assert ids == (first_facts if proposal.extraction_run_id == first.run.id else second_facts)
    assert first_links <= {
        (link.proposal_id, link.fact_id)
        for link in session.scalars(select(ExtractedProposalFact)).all()
    }
    assert proposal_input_snapshots(session, first.run) == first_snapshots
    assert read_current_project_record(session, project.id) == ()


@pytest.mark.parametrize("native_runs", [1, 2])
def test_ordinary_load_keeps_completed_challengers_out_of_selection_and_ambiguity(
    session, project, matrix_source, native_runs,
):
    document = _document(session, project, matrix_source)
    captured = _extract(session, document, matrix_source, idempotency_key=None)
    if native_runs == 2:
        changed = _answer()
        changed["mapping_confidence"] = 0.75
        _extract(session, document, matrix_source, idempotency_key=None,
                 client=RecordedStructureClient(matrix_source, answers=[changed]))

    loaded = load_project(session, project.id)

    assert is_completed_run(captured.run)
    assert loaded.declared_documents == 0 and loaded.ambiguous_documents == []
    assert loaded.admitted_count == 0
    assert session.get(ActiveExtractionRun, document.id) is None
    assert current_active_run_declaration(session, document.id) is None
    assert declare_single_run_documents_by_policy(session, project.id).declared == []
    assert declare_single_run_documents(
        session, project.id, principal=HumanPrincipal("local:native-test-operator"),
    ) == []
    assert read_current_project_record(session, project.id) == ()


@pytest.mark.parametrize("already_active", [False, True])
def test_challenger_does_not_obstruct_or_replace_an_eligible_incumbent(
    session, project, matrix_source, already_active,
):
    document = _document(session, project, matrix_source)
    incumbent = record_extraction_run(
        session, document, prompt_version="legacy_matrix_fixture", candidate_count=0,
        page_errors=0, candidates=(), allow_unsealed_legacy=True,
    )
    if already_active:
        declare_active_run(session, document.id, incumbent.id,
                           principal=HumanPrincipal("local:native-test-operator"))
        session.flush()
    captured = _extract(session, document, matrix_source, idempotency_key=None)

    loaded = load_project(session, project.id)
    session.flush()

    assert loaded.declared_documents == (0 if already_active else 1)
    assert loaded.ambiguous_documents == []
    assert session.get(ActiveExtractionRun, document.id).extraction_run_id == incumbent.id
    assert current_active_run_declaration(session, document.id).extraction_run_id == incumbent.id
    assert is_completed_run(captured.run)
    assert read_current_project_record(session, project.id) == ()


@pytest.mark.parametrize("actor", ["policy", "human"])
def test_shared_production_declaration_refuses_challenger_only_configuration(
    session, project, matrix_source, actor,
):
    document = _document(session, project, matrix_source)
    captured = _extract(session, document, matrix_source)

    with pytest.raises(ValueError, match="challenger-only"):
        if actor == "policy":
            declare_active_run_by_policy(session, document.id, captured.run.id)
        else:
            declare_active_run(session, document.id, captured.run.id,
                               principal=HumanPrincipal("local:native-test-operator"))

    assert is_completed_run(captured.run)
    assert session.get(ActiveExtractionRun, document.id) is None
    assert current_active_run_declaration(session, document.id) is None
    assert read_current_project_record(session, project.id) == ()


def test_explicit_reading_preserves_and_ignores_unrelated_historical_segments(
    session, project, matrix_source, monkeypatch
):
    document = _document(session, project, matrix_source)
    older_reading = read_native_pdf(
        matrix_source.path, source_sha256=document.sha256, dpi=37
    )
    older = append_native_segments(session, document, older_reading)
    original = {
        segment.id: (segment.exact_text, segment.content_sha256, segment.reading_sha256)
        for segment in older
    }
    from corridor_pdf_reader.execution import PdfiumExecutor

    read_document = PdfiumExecutor.read_document
    calls = []

    def counted(self, *args, **kwargs):
        assert kwargs.get("dpi", 36) == 36
        calls.append(1)
        return read_document(self, *args, **kwargs)

    monkeypatch.setattr(PdfiumExecutor, "read_document", counted)

    result = _extract(session, document, matrix_source)

    assert len(calls) <= 1
    session.expire_all()
    assert {
        identifier: (
            session.get(SourceSegment, identifier).exact_text,
            session.get(SourceSegment, identifier).content_sha256,
            session.get(SourceSegment, identifier).reading_sha256,
        )
        for identifier in original
    } == original
    supports = session.scalars(
        select(FactSource.source_segment_id).where(
            FactSource.fact_id.in_([fact.id for fact in result.facts])
        )
    ).all()
    assert supports and not (set(supports) & set(original))


@pytest.mark.parametrize("stage", ["segments", "run", "facts", "proposals", "receipt"])
def test_failure_at_each_append_stage_rolls_back_facts_and_compatibility_proposals(
    session, project, matrix_source, stage
):
    document = _document(session, project, matrix_source)
    before = _spine_ids(session)

    with pytest.raises(RuntimeError, match=f"failure after {stage}"):
        _extract(session, document, matrix_source, fail_after_stage=stage)

    assert _spine_ids(session) == before
    assert read_current_project_record(session, project.id) == ()


def test_continuation_replays_resolution_from_the_retained_preceding_header(
    session, project, continuation_source
):
    source = continuation_source
    document = _document(session, project, source)

    result = _extract(session, document, source)

    assert len(result.candidates) == 2
    second = result.mapping.rows[1]
    assert (second.page_no, second.row, second.disposition) == (2, 0, "extracted")
    fact = next(fact for fact in result.facts
                if fact.subject_key == second.row_id and fact.fact_type == "resolution_strategy")
    assert [
        (role, ordinal, segment.page_no, segment.cell_row, segment.cell_column, segment.exact_text)
        for role, ordinal, segment in _fact_sources(session, fact)
    ] == [
        ("context", 1, 2, 0, 6, "x"), ("context", 2, 2, 0, 7, "x"),
        ("value_source", 1, 1, 0, 6, "Protect"),
        ("value_source", 2, 1, 0, 7, "Relocate"),
    ]
    assert replay_fact(session, document, fact, source.path, native_reading=source.reading) == "Protect; Relocate"
    assert result.run.row_accounting_json["detected_row_count"] == 2


def test_outside_owner_attribute_uses_an_exact_visible_pdf_span(
    session, project, outside_owner_source
):
    source = outside_owner_source
    document = _document(session, project, source)

    result = _extract(session, document, source)

    owner = next(fact for fact in result.facts if fact.fact_type == "external_org")
    sources = _fact_sources(session, owner)
    assert len(sources) == 1
    role, ordinal, segment = sources[0]
    assert (role, ordinal, segment.kind, segment.span_stream, segment.exact_text) == (
        "value_source", 1, "pdf_span", "page", "Outside Utilities",
    )
    assert source.reading.page_text(1)[segment.start_offset:segment.end_offset] == segment.exact_text
    assert replay_fact(session, document, owner, source.path, native_reading=source.reading) == "Outside Utilities"
    assert result.candidates[0].payload_json["field_sources"]["external_org"]["value_source"][0]["model_id"] == "o0"


def test_outside_attribute_without_an_exact_span_is_refused_without_ocr(
    session, project, tmp_path, monkeypatch
):
    fixture = PdfFixture()
    # One outside text object spans two sentence-level Source Segments. The
    # adapter cannot relabel either sentence as the whole outside object.
    _draw_page(
        fixture, [("UC-1", "", "1149+00", "", "Pole", "", "")],
        owner="Outside. Utilities",
    )
    source = _finish_fixture(fixture, tmp_path, [_answer(owner="o0")])
    document = _document(session, project, source)
    client = RecordedStructureClient(source)

    def forbidden(*args, **kwargs):
        pytest.fail("an unavailable native span attempted OCR")

    monkeypatch.setattr("corridor.scanned_reading.read_routed_page", forbidden)
    before = _spine_ids(session)

    with pytest.raises(NativeMatrixRefused, match="unique exact native span"):
        _map(document, source, client)

    assert client.calls == 1
    assert _spine_ids(session) == before


def test_mapping_inspection_is_a_copy_and_literal_replacement_breaks_the_seal(
    session, project, matrix_source
):
    document = _document(session, project, matrix_source)
    mapping = _map(document, matrix_source)
    identity = mapping.identity
    edited = mapping.pages
    edited[0]["reading"]["rows"][0]["fields"]["utility_id"]["text"] = "forged"
    assert mapping.pages[0]["reading"]["rows"][0]["fields"]["utility_id"]["text"] == "UC-1"
    assert mapping.identity == identity
    row = mapping.rows[0]
    field = next(value for value in row.fields if value.name == "utility_id")
    forged = replace(field, text="forged")
    fields = tuple(forged if value is field else value for value in row.fields)

    with pytest.raises(NativeMatrixRefused, match="not bound"):
        replace(mapping, rows=(replace(row, fields=fields), *mapping.rows[1:]))

    with pytest.raises(NativeMatrixRefused, match="not bound"):
        replace(mapping, pages_json=json.dumps(edited))


@pytest.mark.parametrize("corruption", ["literal", "other_row", "missing_row", "duplicate_row", "other_page", "other_table"])
def test_rebinding_rejects_forged_values_and_row_or_page_scope(
    session, project, matrix_source, corruption
):
    document = _document(session, project, matrix_source)
    mapping = _map(document, matrix_source)
    pages = mapping.pages
    rows = pages[0]["reading"]["rows"]
    if corruption == "literal":
        rows[0]["fields"]["utility_id"]["text"] = "invented value"
    elif corruption == "other_row":
        rows[0]["fields"]["utility_id"] = {"text": "UC-4", "cells": ["t0r4c1"]}
    elif corruption == "missing_row":
        rows.pop()
    elif corruption == "duplicate_row":
        rows.append(deepcopy(rows[-1]))
    elif corruption == "other_page":
        pages[0]["number"] = 2
    else:
        rows[0]["fields"]["utility_id"]["cells"] = ["t1r1c1"]

    with pytest.raises((NativeMatrixRefused, SourceSegmentLocatorMismatch)):
        bind_native_matrix(document, matrix_source.reading, pages, mapping.config_record)

    assert not any(_spine_ids(session).values())


def test_another_document_is_refused_before_request_or_spine_append(
    session, project, matrix_source
):
    document = _document(session, project, matrix_source, sha256="0" * 64)
    client = RecordedStructureClient(matrix_source)

    with pytest.raises((NativeMatrixRefused, SourceDocumentDigestMismatch, SourceSegmentLocatorMismatch)):
        _extract(session, document, matrix_source, client=client)

    assert client.calls == 0
    assert not any(_spine_ids(session).values())


def test_changed_source_bytes_are_refused_before_any_model_request(
    session, project, matrix_source, tmp_path
):
    document = _document(session, project, matrix_source)
    wrong_path = tmp_path / "changed.pdf"
    wrong_path.write_bytes(matrix_source.path.read_bytes() + b"\n% changed source\n")
    client = RecordedStructureClient(matrix_source)

    with pytest.raises((NativeMatrixRefused, SourceDocumentDigestMismatch, SourceSegmentLocatorMismatch)):
        _extract(session, document, matrix_source, source_path=wrong_path, client=client)

    assert client.calls == 0
    assert not any(_spine_ids(session).values())


def test_bound_reference_rejects_an_index_for_another_document(
    session, project, matrix_source
):
    document = _document(session, project, matrix_source)
    mapping = _map(document, matrix_source)
    other = Document(
        id=document.id + 10000, project_id=document.project_id,
        sha256=document.sha256, filename="another-document.pdf", doc_type="matrix",
    )
    index = NativeCellIndex(other, matrix_source.reading)
    reference = mapping.rows[0].fields[0].value_sources[0]

    with pytest.raises(SourceSegmentLocatorMismatch, match="another Document"):
        reference.select(session, document, index)


@pytest.mark.parametrize("case", ["semantic_refusal", "literal_attribute", "malformed_shape"])
def test_semantic_or_malformed_mapping_never_enters_an_ocr_fallback(
    session, project, matrix_source, monkeypatch, case
):
    def forbidden(*args, **kwargs):
        pytest.fail("native semantic refusal attempted an incumbent or OCR fallback")

    monkeypatch.setattr("corridor.scanned_reading.read_routed_page", forbidden)
    document = _document(session, project, matrix_source)
    answer = _answer()
    if case == "semantic_refusal":
        answer["header_row"] = None
    elif case == "literal_attribute":
        answer["page_attributes"]["external_org"] = "Invented Utilities"
    else:
        answer["invented_literal"] = "Invented Utilities"
    client = RecordedStructureClient(matrix_source, answers=[answer])
    before = _spine_ids(session)

    if case == "literal_attribute":
        result = _extract(session, document, matrix_source, client=client)
        reading = result.mapping.pages[0]["reading"]
        assert reading["page_attributes"] == {}
        assert any("Invented Utilities" in reason for reason in reading["refused"])
        assert result.candidates[0].payload_json["fields"]["external_org"] == "Exact Utilities"
        assert not any(fact.text_value == "Invented Utilities" for fact in result.facts)
    else:
        with pytest.raises(NativeMatrixRefused) as refused:
            _extract(session, document, matrix_source, client=client)
        record = refused.value.record()
        assert record["kind"] == "native_matrix_refusal"
        assert record["pages"][0]["structure"] == answer
        assert record["pages"][0]["listing"] == client.requests[0][1]
        assert record["pages"][0]["image_sha256"] == sha256(
            matrix_source.images[1].read_bytes()
        ).hexdigest()
        if case == "semantic_refusal":
            assert record["pages"][0]["reading"]["refused"] == [
                "no header row and no mapping carried from an earlier page"
            ]
        record["pages"][0]["structure"]["mapping_confidence"] = -1
        assert refused.value.record()["pages"][0]["structure"] == answer
        assert _spine_ids(session) == before

    assert client.calls == 1


@pytest.mark.parametrize("case", ["missing_image", "wrong_resolution", "model", "effort", "native_dpi"])
def test_unmeasured_context_or_model_configuration_is_refused_before_transmission(
    session, project, matrix_source, tmp_path, case
):
    document = _document(session, project, matrix_source)
    client = RecordedStructureClient(matrix_source)
    images = dict(matrix_source.images)
    reading = matrix_source.reading
    if case == "missing_image":
        images = {}
    elif case == "wrong_resolution":
        wrong = tmp_path / "review.png"
        with Image.open(images[1]) as image:
            image.resize((900, 480)).save(wrong)
        images[1] = wrong
    elif case == "model":
        client.model = "unmeasured-model"
    elif case == "effort":
        client.effort = "high"
    else:
        reading = read_native_pdf(matrix_source.path, source_sha256=document.sha256, dpi=37)

    with pytest.raises(ValueError):
        _extract(session, document, matrix_source, client=client, images=images, reading=reading)

    assert client.calls == 0
    assert not any(_spine_ids(session).values())


def test_the_reader_is_the_only_configuration_the_product_can_run():
    assert "native_reader_token_layer" not in Settings.model_fields
    assert "reader_page_inventory" not in Settings.model_fields
    assert semantics.PROMPT_VERSION == "matrix_structure_ids_v1"
