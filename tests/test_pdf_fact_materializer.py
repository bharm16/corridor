"""Native PDF transformations preserve exact evidence and never accept a literal.

These fixtures append the output of an already-scoped native reading. Physical
reader/locator replay is covered by test_reader_segments and the supported
migration proof; this seam tests scalar rules, paired support and SQL storage.
"""

from datetime import date
from hashlib import sha256

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from corridor.fact_types import FACT_TYPE_CONTRACTS, STRUCTURED_CELL_FACT_TYPES
from corridor.materializer import (
    PDF_MARKED_RESOLUTION_TRANSFORMATION,
    PDF_TEXT_TRANSFORMATION,
    FactReplayMismatch,
    FactValidationError,
    MaterializationRefused,
    clean_pdf_source_text,
    materialize_pdf_marked_resolution,
    materialize_pdf_segment_value,
    materialize_segment_value,
    replay_pdf_materialized_value,
)
from corridor.models import (
    Document,
    ExtractionRun,
    ExternalOrg,
    FactDecision,
    FactSource,
    Project,
    ProjectRecordRevision,
)
from corridor.source_append import SegmentValues, append_fact, append_source_segments


@pytest.fixture
def document(session):
    project = Project(slug="pdf-fact-materializer", name="PDF facts", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id, filename="matrix.pdf", doc_type="matrix",
        sha256=sha256(b"pdf-fact-materializer-fixture").hexdigest(),
    )
    session.add(document)
    session.flush()
    return document


def _cells(session, document, *specs):
    values = []
    for index, spec in enumerate(specs):
        values.append(SegmentValues(
            kind="pdf_cell",
            exact_text=spec.get("words", "X"),
            content_sha256=sha256(spec.get("words", "X").encode()).hexdigest(),
            ordinal=index + 1,
            rendition_sha256=document.sha256,
            reading_sha256=spec.get("reading", "a" * 64),
            reader_identity={
                "scheme": "corridor.pdf-segments.v1",
                "native_layer": {"engine": "corridor-pdf-reader"},
            },
            location_json={"glyphs": []},
            page_no=spec.get("page", 1),
            table_index=spec.get("table", 0),
            cell_row=spec.get("row", 1),
            cell_column=spec.get("column", index),
            row_span=spec.get("row_span", 1),
            column_span=spec.get("column_span", 1),
        ))
    return append_source_segments(
        session, project_id=document.project_id, document_id=document.id,
        recorded_verbal_origin_id=None, segments=values,
    )


def _capture(session, document, value):
    run = ExtractionRun(document_id=document.id, prompt_version="fixture", candidate_count=0)
    session.add(run)
    session.flush()
    return append_fact(
        session, project_id=document.project_id, document_id=document.id,
        extraction_run_id=run.id, subject_kind="source_row", subject_key="p1:t0:r1",
        recorded_by="local:test", content_sha256=sha256(repr(value).encode()).hexdigest(),
        value=value,
    )


def test_pdf_whitespace_rule_is_named_and_preserves_nonwhitespace_source_characters(
    session, document,
):
    words = " \t 6-inch\n Water\u00a0main\r\n  soft\u00adhyphen "
    (segment,) = _cells(session, document, {"words": words})
    value = materialize_pdf_segment_value(session, "conflict_description", segment)
    assert value.text_value == "6-inch Water main soft\u00adhyphen"
    assert clean_pdf_source_text(words) == value.text_value
    assert value.transformation == PDF_TEXT_TRANSFORMATION
    assert value.source_links == (("value_source", segment.id),)
    assert segment.exact_text == words
    assert materialize_segment_value(session, "conflict_description", segment) == value
    fact = _capture(session, document, value)
    assert (fact.text_value, fact.transformation) == (value.text_value, PDF_TEXT_TRANSFORMATION)
    assert session.scalar(select(func.count()).select_from(FactDecision)) == 0
    assert session.scalar(select(func.count()).select_from(ProjectRecordRevision)) == 0


@pytest.mark.parametrize("words", ["Summer 2020", "Spring 2019", "Apr-20", "N/A", "2020-02-30"])
def test_pdf_date_refusal_never_invents_calendar_precision(session, document, words):
    (segment,) = _cells(session, document, {"words": words})
    with pytest.raises(FactValidationError, match="not an ISO calendar date"):
        materialize_pdf_segment_value(session, "need_date", segment)
    assert segment.exact_text == words


def test_pdf_iso_date_keeps_the_existing_date_contract(session, document):
    (segment,) = _cells(session, document, {"words": " 2020-04-20 "})
    value = materialize_pdf_segment_value(session, "need_date", segment)
    assert value.date_value == date(2020, 4, 20)
    assert value.text_value is None
    assert value.transformation == "iso_date_cell_v1"
    assert replay_pdf_materialized_value(
        session, "need_date", "iso_date_cell_v1", [segment], [],
    ) == value
    assert _capture(session, document, value).date_value == date(2020, 4, 20)


def test_only_external_organization_accepts_an_outside_pdf_span(session, document):
    organization = ExternalOrg(name="Exact Utilities", aliases=[])
    session.add(organization)
    session.flush()
    words = " Exact\nUtilities "
    (segment,) = append_source_segments(
        session, project_id=document.project_id, document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=[SegmentValues(
            kind="pdf_span", exact_text=words, content_sha256=sha256(words.encode()).hexdigest(),
            ordinal=1, rendition_sha256=document.sha256, reading_sha256="a" * 64,
            reader_identity={"scheme": "corridor.pdf-segments.v1", "native_layer": {"engine": "corridor-pdf-reader"}},
            location_json={"glyphs": []}, page_no=1, start_offset=0,
            end_offset=len(words), span_stream="page",
        )],
    )
    value = materialize_pdf_segment_value(session, "external_org", segment)
    assert (value.text_value, value.external_org_value_id) == ("Exact Utilities", organization.id)
    assert _capture(session, document, value).external_org_value_id == organization.id
    for fact_type in ("station_from", "need_date"):
        with pytest.raises(FactValidationError, match="does not accept pdf_span"):
            materialize_pdf_segment_value(session, fact_type, segment)
    # A statement Fact's value source is a `pdf_span` too since #741, so the
    # kind no longer separates the contracts and the refusal names the reason:
    # statement wording is the exact prose span, with an attribution source.
    with pytest.raises(
        FactValidationError, match="does not materialize as a PDF cell reading"
    ):
        materialize_pdf_segment_value(session, "statement_wording", segment)


def test_native_fact_support_does_not_extend_automatic_record_policies():
    for fact_type in STRUCTURED_CELL_FACT_TYPES:
        assert FACT_TYPE_CONTRACTS[fact_type].automatic_segment_kinds == {"spreadsheet_cell"}
    assert FACT_TYPE_CONTRACTS["resolution_strategy"].transformation == "trim_cell_text_v1"


@pytest.mark.parametrize("page", [1, 3])
def test_marked_resolution_captures_ordered_header_and_mark_sources_and_replays(
    session, document, page,
):
    header1, header2, mark1, mark2 = _cells(
        session, document,
        {"words": " Relocate\nfacility ", "row": 0, "column": 2},
        {"words": "Protect\tin place", "row": 0, "column": 5},
        {"words": "X", "row": 1, "column": 2, "page": page},
        {"words": "check", "row": 1, "column": 5, "page": page},
    )
    headers, marks = [header1, header2], [mark1, mark2]
    value = materialize_pdf_marked_resolution(
        headers, marks, preceding_header_page_no=1 if page != 1 else None,
    )
    assert value.text_value == "Relocate facility; Protect in place"
    assert value.source_links == (
        ("value_source", header1.id), ("value_source", header2.id),
        ("context", mark1.id), ("context", mark2.id),
    )
    fact = _capture(session, document, value)
    assert fact.transformation == PDF_MARKED_RESOLUTION_TRANSFORMATION
    links = session.scalars(select(FactSource).where(FactSource.fact_id == fact.id).order_by(FactSource.id)).all()
    assert [(link.role, link.ordinal, link.source_segment_id) for link in links] == [
        ("value_source", 1, header1.id), ("value_source", 2, header2.id),
        ("context", 1, mark1.id), ("context", 2, mark2.id),
    ]
    assert replay_pdf_materialized_value(
        session, fact.fact_type, fact.transformation, headers, marks,
    ) == value
    with pytest.raises(TypeError):
        materialize_pdf_marked_resolution(headers, marks, text_value="invented")
    assert session.scalar(select(func.count()).select_from(FactDecision)) == 0


def test_spanning_header_is_retained_once_for_each_marked_column(session, document):
    header, mark1, mark2 = _cells(
        session, document,
        {"words": "Adjust", "row": 0, "column": 2, "column_span": 2},
        {"row": 1, "column": 2}, {"row": 1, "column": 3},
    )
    value = materialize_pdf_marked_resolution([header], [mark1, mark2])
    assert value.text_value == "Adjust; Adjust"
    assert value.source_links == (("value_source", header.id), ("context", mark1.id), ("context", mark2.id))


@pytest.mark.parametrize(("mark_spec", "declaration", "message"), [
    ({"column": 3}, None, "matching header column span"),
    ({"column_span": 2}, None, "matching header column span"),
    ({"row": 0, "table": 1}, None, "header must precede"),
    ({"table": 1}, None, "header must precede"),
    ({"page": 2}, None, "declared preceding header page"),
    ({"page": 2}, 2, "declared preceding header page"),
    ({"reading": "b" * 64}, None, "crosses its reading scope"),
    ({"words": " \n "}, None, "marks cannot be empty"),
])
def test_marked_resolution_refuses_unpaired_or_unscoped_sources(
    session, document, mark_spec, declaration, message,
):
    header, mark = _cells(
        session, document,
        {"words": "Relocate", "row": 0, "column": 2},
        {"row": 1, "column": 2, **mark_spec},
    )
    with pytest.raises(FactValidationError, match=message):
        materialize_pdf_marked_resolution([header], [mark], preceding_header_page_no=declaration)


def test_marked_resolution_rejects_reordered_duplicate_and_extra_support(session, document):
    h1, h2, m1, m2 = _cells(
        session, document,
        {"words": "Relocate", "row": 0, "column": 2},
        {"words": "Protect", "row": 0, "column": 3},
        {"column": 2}, {"column": 3},
    )
    for headers, marks in (([h2, h1], [m1, m2]), ([h1, h2], [m2, m1]), ([h1, h1], [m1])):
        with pytest.raises(FactValidationError, match="unique ordered columns"):
            materialize_pdf_marked_resolution(headers, marks)
    with pytest.raises(FactValidationError, match="no corresponding selected mark"):
        materialize_pdf_marked_resolution([h1, h2], [m1])
    with pytest.raises(FactValidationError, match="exactly one value source"):
        replay_pdf_materialized_value(session, "notes", PDF_TEXT_TRANSFORMATION, [m1, m2], [])
    with pytest.raises(FactValidationError, match="wrong Fact type"):
        replay_pdf_materialized_value(session, "notes", PDF_MARKED_RESOLUTION_TRANSFORMATION, [h1], [m1])
    with pytest.raises(FactValidationError, match="does not match its contract"):
        replay_pdf_materialized_value(session, "notes", "trim_cell_text_v1", [m1], [])


@pytest.mark.parametrize("scope_field", ["project_id", "document_id", "rendition_sha256", "reader_identity"])
def test_marked_resolution_refuses_support_from_another_scope(session, document, scope_field):
    header, mark = _cells(
        session, document,
        {"words": "Relocate", "row": 0, "column": 2}, {"column": 2},
    )
    changed = {
        "project_id": document.project_id + 1, "document_id": document.id + 1,
        "rendition_sha256": "b" * 64, "reader_identity": {"scheme": "another-reading"},
    }
    setattr(mark, scope_field, changed[scope_field])
    with pytest.raises(FactValidationError, match="crosses its reading scope"):
        materialize_pdf_marked_resolution([header], [mark])


def test_resolution_header_cannot_overlap_body_or_come_from_a_future_page(session, document):
    header, mark = _cells(
        session, document,
        {"words": "Relocate", "row": 0, "row_span": 2, "column": 2}, {"column": 2},
    )
    with pytest.raises(FactValidationError, match="header must precede"):
        materialize_pdf_marked_resolution([header], [mark])
    header.page_no = 2
    with pytest.raises(FactValidationError, match="declared preceding header page"):
        materialize_pdf_marked_resolution([header], [mark], preceding_header_page_no=2)


@pytest.mark.parametrize("damage", ["words", "unstored", "reading"])
def test_pdf_sealing_refuses_changed_words_unstored_or_unscoped_cells(session, document, damage):
    (segment,) = _cells(session, document, {"words": "1149+00"})
    if damage == "words":
        segment.exact_text = "1148+00"
        expected = FactReplayMismatch
    elif damage == "unstored":
        segment.id = None
        expected = MaterializationRefused
    else:
        segment.reading_sha256 = None
        expected = FactValidationError
    with pytest.raises(expected):
        materialize_pdf_segment_value(session, "station_from", segment)


@pytest.mark.parametrize(("fact_type", "transformation"), [
    ("station_from", PDF_MARKED_RESOLUTION_TRANSFORMATION),
    ("need_date", PDF_TEXT_TRANSFORMATION),
    ("station_from", "pdf_unreleased_text_v1"),
])
def test_sql_typed_value_check_keeps_pdf_transformations_in_their_fact_types(
    session, document, fact_type, transformation,
):
    run = ExtractionRun(document_id=document.id, prompt_version="fixture", candidate_count=0)
    session.add(run)
    session.flush()
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(text("set local role corridor_source_append"))
        session.execute(text(
            "insert into facts (project_id, document_id, extraction_run_id, fact_type, "
            "subject_kind, subject_key, text_value, transformation, recorded_by, content_sha256) "
            "values (:project, :document, :run, :type, 'source_row', 'p1:t0:r1', 'value', "
            ":transformation, 'local:test', :digest)"
        ), {"project": document.project_id, "document": document.id, "run": run.id,
            "type": fact_type, "transformation": transformation,
            "digest": sha256(f"{fact_type}:{transformation}".encode()).hexdigest()})
    assert caught.value.orig.diag.constraint_name == "ck_facts_typed_value"


def _native_accounting_receipt():
    return {
        "schema_version": "native-matrix-row-accounting-v1",
        "reader_version": "matrix_structure_ids_v1",
        "reader_path": "native_matrix_cells",
        "detected_row_count": 1, "accounted_row_count": 1,
        "extracted_row_count": 1, "blank_row_count": 0, "skipped_row_count": 0,
        "unaccounted_rows": [],
        "rows": [{"row_id": "scoped:r0", "page": 1, "row_number": 0,
                  "disposition": "extracted", "reason": "extracted"}],
        "native_mapping": {
            "identity": "a" * 64, "reading_sha256": "b" * 64,
            "pages": [{
                "number": 1, "structure": {}, "listing": "t0r0c0: 1149+00",
                "image_sha256": "c" * 64,
                "reading": {"rows": [{"row_id": "t0r0", "fields": {
                    "station_from": {"text": "1149+00", "cells": ["t0r0c0"]},
                }}]},
            }],
        },
        "field_materialization": [{
            "row_id": "scoped:r0", "local_row_id": "t0r0", "page": 1,
            "field": "station_from", "status": "materialized", "reason": "materialized",
            "value_source_ids": [1], "context_source_ids": [],
        }],
    }


def test_sql_accepts_complete_native_accounting_with_zero_based_rows(session, document):
    receipt = _native_accounting_receipt()
    run = ExtractionRun(
        document_id=document.id, prompt_version="matrix_structure_ids_v1",
        candidate_count=1, row_accounting_json=receipt,
    )
    session.add(run)
    session.flush()
    session.refresh(run)
    assert run.row_accounting_json == receipt
    assert session.scalar(select(func.count()).select_from(ProjectRecordRevision)) == 0


@pytest.mark.parametrize("damage", [
    "omitted", "old_schema", "missing_version", "missing_mapping", "null_identity",
    "missing_field_outcomes", "unreported_field", "invalid_status", "missing_status",
    "disposition_sum", "disposition_miscount", "candidate_count", "unaccounted",
])
def test_sql_refuses_omitted_or_incomplete_native_accounting(session, document, damage):
    receipt = _native_accounting_receipt()
    count = 1
    if damage == "omitted":
        receipt = None
    elif damage == "old_schema":
        receipt["schema_version"] = "matrix-row-accounting-v1"
    elif damage == "missing_version":
        receipt.pop("reader_version")
    elif damage == "missing_mapping":
        receipt.pop("native_mapping")
    elif damage == "null_identity":
        receipt["native_mapping"]["identity"] = None
    elif damage == "missing_field_outcomes":
        receipt.pop("field_materialization")
    elif damage == "unreported_field":
        receipt["field_materialization"] = []
    elif damage == "invalid_status":
        receipt["field_materialization"][0]["status"] = "accepted"
    elif damage == "missing_status":
        receipt["field_materialization"][0].pop("status")
    elif damage == "disposition_sum":
        receipt["skipped_row_count"] = 1
    elif damage == "disposition_miscount":
        receipt["rows"][0]["disposition"] = "skipped"
    elif damage == "candidate_count":
        count = 0
    else:
        receipt["accounted_row_count"] = 0
        receipt["extracted_row_count"] = 0
        receipt["rows"][0]["disposition"] = None
        receipt["unaccounted_rows"] = ["scoped:r0"]
        count = 0
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.add(ExtractionRun(
            document_id=document.id, prompt_version="matrix_structure_ids_v1",
            candidate_count=count, row_accounting_json=receipt,
        ))
        session.flush()
    assert caught.value.orig.diag.constraint_name in {
        "ck_extraction_runs_row_accounting_shape",
        "ck_extraction_runs_completed_row_accounting",
    }
