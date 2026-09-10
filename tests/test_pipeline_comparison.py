"""Real source/Facts remain comparable across storage identities and locations."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from corridor.extractor_lineage import DEPLOYED_NATIVE_MATRIX_REQUEST
from corridor.models import Document, Project
from corridor.native_matrix import extract_native_matrix, render_native_matrix_context
from corridor.pipeline_comparison import canonical_native_output, compare_pipeline_outputs
from corridor.reader_segments import read_native_pdf
from corridor.source_segment_errors import SourceSegmentLocatorMismatch
from corridor_pdf_reader.replacement import semantics
from pdf_fixture_support import PdfFixture


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    directory = tmp_path_factory.mktemp("pipeline-comparison")
    fixture = PdfFixture()
    page = fixture.add_page(width=760, height=260)
    edges = (20, 100, 360, 520, 620, 740)
    rows = (
        ("ID", "Owner", "Date", "Protect", "Relocate"),
        ("UC-1", "Repeated Owner", "Summer 2020", "x", "x"),
        ("UC-2", "Repeated Owner", "2026-10-12", "", "x"),
    )
    for x in edges:
        page.line((x, 50), (x, 185))
    for index in range(4):
        page.line((20, 50 + 45 * index), (740, 50 + 45 * index))
    for row, cells in enumerate(rows):
        for column, value in enumerate(cells):
            if value:
                page.text((edges[column] + 7, 73 + row * 45), value, fontsize=10)
    path = fixture.save(directory / "matrix.pdf")
    reading = read_native_pdf(path, source_sha256=sha256(path.read_bytes()).hexdigest())
    images = render_native_matrix_context(path, directory / "images", [1])
    return path, reading, images


class AuthoredStructureClient:
    """One authored structure answer, under the deployed configuration it
    stands for. It states that configuration instead of carrying four
    attributes describing a provider this test never reaches."""

    image_detail = "original"

    def __init__(self):
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0

    def configuration(self):
        return DEPLOYED_NATIVE_MATRIX_REQUEST

    def complete(self, *, system, user, schema, images=(), logprobs=False):
        assert system == semantics.PROMPT_PATH.read_text()
        assert schema == semantics.STRUCTURE_SCHEMA
        assert "Repeated Owner" in user
        assert len(images) == 1 and Path(images[0]).is_file()
        assert logprobs is False
        self.calls += 1
        return {
            "is_utility_matrix": True, "matrix_table": 0, "header_row": 0,
            "columns": [
                {"index": column, "canonical_field": name}
                for column, name in enumerate(
                    ("utility_id", "external_org", "committed_date", "resolution_strategy", "resolution_strategy"),
                    start=1,
                )
            ],
            "page_attributes": {"external_org": None}, "mapping_confidence": 0.93,
        }


def persisted(session, source, slug="comparison"):
    path, reading, images = source
    project = Project(slug=slug, name="Synthetic comparison", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(project_id=project.id, sha256=reading.rendition_sha256,
                        filename=path.name, doc_type="matrix", pages=1,
                        parse_status="parsed", numbering_scheme="project-unique")
    session.add(document)
    session.flush()
    extraction = extract_native_matrix(
        session, document, reading=reading, source_path=path,
        client=AuthoredStructureClient(), images=images, document_label=path.name,
    )
    return document, extraction


@pytest.fixture
def output(session, source):
    document, extraction = persisted(session, source)
    return canonical_native_output(session, document, extraction, source_path=source[0])


def field(row, name):
    return next(item for item in row["fields"] if item["name"] == name)


def all_keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(item) for item in value))
    return set()


def test_independent_database_and_run_ids_do_not_change_canonical_output(session, source):
    first_document, first = persisted(session, source, "first")
    second_document, second = persisted(session, source, "second")
    assert first_document.id != second_document.id and first.run.id != second.run.id
    assert first.mapping.identity != second.mapping.identity
    assert {fact.content_sha256 for fact in first.facts}.isdisjoint(
        fact.content_sha256 for fact in second.facts
    )
    expected = canonical_native_output(session, first_document, first, source_path=source[0])
    actual = canonical_native_output(session, second_document, second, source_path=source[0])
    result = compare_pipeline_outputs(expected, actual)
    assert result["equal"] and result["passed"]
    assert result["expected_rows"] == result["actual_rows"] == result["matched_rows"] == 2
    assert result["expected_facts"] == result["actual_facts"] > 0
    assert not ({"id", "project_id", "document_id", "extraction_run_id", "segment_id",
                 "scoped_id", "native_row_id", "candidate_id", "reading_sha256", "subject_key",
                 "source_index", "object_id"} & all_keys(actual))


def test_storage_order_is_ignored_without_mutating_the_caller(output):
    actual = deepcopy(output)
    actual["rows"].reverse()
    actual["facts"].reverse()
    before = deepcopy(actual)
    assert compare_pipeline_outputs(output, actual)["passed"]
    assert actual == before


def test_reordered_identical_labels_are_not_the_same_physical_occurrence(output):
    actual = deepcopy(output)
    first, second = [field(row, "external_org") for row in actual["rows"]]
    assert first["text"] == second["text"] == "Repeated Owner"
    assert first["sources"]["value_source"][0]["location"] != second["sources"]["value_source"][0]["location"]
    first["sources"], second["sources"] = second["sources"], first["sources"]
    result = compare_pipeline_outputs(output, actual)
    assert not result["equal"] and not result["passed"]
    assert result["matched_rows"] == 0
    assert any("sources" in item["path"] for item in result["differences"])


def test_many_occurrences_becoming_one_and_duplicate_keys_preserve_denominator(output):
    actual = deepcopy(output)
    actual["rows"] = actual["rows"][:1]
    result = compare_pipeline_outputs(output, actual)
    assert not result["passed"] and result["expected_rows"] == 2 and result["actual_rows"] == 1
    expected = deepcopy(output)
    expected["rows"].append(deepcopy(expected["rows"][0]))
    repeated = compare_pipeline_outputs(expected, output)
    assert not repeated["passed"]
    assert repeated["expected_rows"] == 3 and repeated["matched_rows"] == 2


@pytest.mark.parametrize("change", ["value_order", "context_order", "role"])
def test_ordered_fact_roles_and_locations_cannot_be_replaced_by_equal_text(output, change):
    actual = deepcopy(output)
    fact = next(item for item in actual["facts"] if item["field"] == "resolution_strategy"
                and sum(link["role"] == "value_source" for link in item["sources"]) == 2)
    if change == "role":
        fact["sources"][0]["role"] = "context"
    else:
        role = "value_source" if change == "value_order" else "context"
        selected = [link for link in fact["sources"] if link["role"] == role]
        assert len(selected) == 2
        selected[0]["segment"], selected[1]["segment"] = selected[1]["segment"], selected[0]["segment"]
    result = compare_pipeline_outputs(output, actual)
    assert not result["passed"]
    assert any(item["path"].startswith("$.facts") for item in result["differences"])


def test_non_iso_phrase_remains_exact_and_its_fact_refusal_is_not_erased(output):
    row = next(row for row in output["rows"] if field(row, "committed_date")["text"] == "Summer 2020")
    date_field = field(row, "committed_date")
    assert date_field["materialization"] == {"status": "refused", "reason": "non_iso_date"}
    assert row["proposal"]["payload"]["fields"]["committed_date"] == "Summer 2020"
    assert not any(fact["field"] == "committed_date" and fact["location"] == row["location"] for fact in output["facts"])
    actual = deepcopy(output)
    actual_row = next(item for item in actual["rows"] if item["location"] == row["location"])
    field(actual_row, "committed_date")["materialization"]["reason"] = "omitted"
    assert not compare_pipeline_outputs(output, actual)["passed"]


@pytest.mark.parametrize("key", ["refused", "unmapped"])
def test_mapping_diagnostics_remain_part_of_the_comparison(output, key):
    actual = deepcopy(output)
    reading = actual["pages"][0]["reading"]
    target = reading if key == "refused" else reading["mapping"]
    target[key] = [*target[key], "FRANCHISE", "FRANCHISE"]
    assert not compare_pipeline_outputs(output, actual)["passed"]


def test_empty_output_can_repeat_but_cannot_pass_quality(output):
    empty = {**output, "rows": [], "facts": []}
    result = compare_pipeline_outputs(empty, empty)
    assert result["equal"] and not result["passed"]
    assert result["coverage_refusal"] == "empty_row_denominator"
    refused = {**empty, "pages": []}
    unobserved = compare_pipeline_outputs(refused, refused)
    assert unobserved["equal"] and not unobserved["passed"]


def test_wrong_source_document_or_incomplete_fact_population_refuses(session, source, tmp_path):
    document, extraction = persisted(session, source)
    other, _ = persisted(session, source, "other")
    with pytest.raises(SourceSegmentLocatorMismatch):
        canonical_native_output(session, other, extraction, source_path=source[0])
    changed = tmp_path / "changed.pdf"
    changed.write_bytes(source[0].read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source bytes"):
        canonical_native_output(session, document, extraction, source_path=changed)
    for facts in (extraction.facts[:-1], (*extraction.facts, extraction.facts[0])):
        with pytest.raises(ValueError, match="complete persisted Fact population"):
            canonical_native_output(session, document, replace(extraction, facts=facts), source_path=source[0])


def test_invalid_schema_and_nonfinite_values_refuse(output):
    with pytest.raises(ValueError, match="canonical schema"):
        compare_pipeline_outputs({}, {})
    invalid = deepcopy(output)
    invalid["rows"][0]["confidence"] = float("nan")
    with pytest.raises(ValueError):
        compare_pipeline_outputs(output, invalid)


@pytest.mark.parametrize("missing", ["row", "glyph_anchors", "glyph_record", "cell_record", "cell_box", "table_box", "diagnostics", "page"])
def test_shaped_but_unlocated_records_cannot_create_a_quality_denominator(output, missing):
    invalid = deepcopy(output)
    if missing == "row":
        invalid["rows"] = [{}]
    elif missing == "glyph_anchors":
        field(invalid["rows"][0], "external_org")["sources"]["value_source"][0]["location"]["glyphs"] = []
    elif missing == "glyph_record":
        field(invalid["rows"][0], "external_org")["sources"]["value_source"][0]["location"]["glyphs"] = [{}]
    elif missing == "cell_record":
        invalid["rows"][0]["geometry"]["cells"] = [{}]
    elif missing == "cell_box":
        invalid["rows"][0]["geometry"]["cells"][0]["box"] = []
    elif missing == "table_box":
        invalid["rows"][0]["geometry"]["table_box"] = []
    elif missing == "diagnostics":
        invalid["rows"][0].pop("disposition")
    else:
        invalid["pages"] = [{"page": 1}]
    with pytest.raises(ValueError):
        compare_pipeline_outputs(invalid, invalid)


def test_review_placeholder_cannot_manufacture_a_positive_denominator(output):
    placeholder = {
        "schema_version": output["schema_version"], "source_sha256": "a" * 64,
        "pages": [{"page": 1}], "rows": [{
            "location": {"page": 1, "table": 0, "row": 0},
            "geometry": {"table_box": [], "cells": [{}]}, "fields": [],
        }], "facts": [],
    }
    with pytest.raises(ValueError):
        compare_pipeline_outputs(placeholder, placeholder)


def test_known_blank_rows_keep_their_real_geometry_and_disposition(output):
    blank = deepcopy(output)
    blank["rows"] = [blank["rows"][0]]
    blank["rows"][0].update(disposition="blank", reason="blank_source_row", fields=[], proposal=None)
    blank["facts"] = []
    assert compare_pipeline_outputs(blank, blank)["passed"]
