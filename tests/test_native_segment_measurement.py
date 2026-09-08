"""The paired scorer sees actual typed segments, including their omissions (#736)."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from corridor import native_segment_measurement as driver
from corridor.reader_segments import native_segment_values
from corridor_pdf_reader import measurement, provenance
from corridor_pdf_reader.bootstrap import corpus
from corridor_pdf_reader.bootstrap.score import score_pair
from corridor_pdf_reader.execution import PdfiumExecutor
from corridor_pdf_reader.replacement.pages import slim_page
from pdf_fixture_support import PdfFixture


def _pdf(path: Path, *, outside_and_clipped: bool = False) -> Path:
    fixture = PdfFixture()
    page = fixture.add_page(width=440, height=340)
    for x in (20, 170, 320):
        page.line((x, 50), (x, 150))
    for y in (50, 100, 150):
        page.line((20, y), (320, y))
    page.text((30, 75), "Owner")
    page.text((180, 75), "Station")
    page.text((30, 125), "Exact Utilities")
    page.text((180, 125), "1149+00")
    if outside_and_clipped:
        page.text((30, 235), "OUTSIDE")
        page._content.append("q 0 0 8 8 re W n\n")
        page.text((50, 260), "HIDDEN")
        page._content.append("Q\n")
        fixture.add_page().text((40, 80), "Standalone outside wording.")
    return fixture.save(path)


@pytest.fixture(scope="module")
def reading(tmp_path_factory):
    path = _pdf(tmp_path_factory.mktemp("native-handoff") / "table.pdf")
    return driver.read_native_pdf(path, source_sha256=sha256(path.read_bytes()).hexdigest())


def _reference():
    return {"key": "fixture", "sheets": [{
        "name": "S", "cells": [
            [1, 1, "Owner", "text"], [1, 2, "Station", "text"],
            [2, 1, "Exact Utilities", "text"], [2, 2, "1149+00", "text"],
        ],
        "merged": [], "print_area": None, "outside_text": [], "uncached": [],
        "print_titles": None, "header_footer": [],
    }]}


def _cells(result):
    return [cell for page in result["pages"] for table in page["tables"] for cell in table["cells"]]


def test_configuration_selects_the_actual_handoff_and_keeps_existing_drivers():
    frozen = measurement.CONFIGURATIONS["frozen-reader"]
    native = measurement.CONFIGURATIONS["native-segments-v1"]

    assert frozen.driver == "bootstrap.read"
    assert frozen.driver_module == "corridor_pdf_reader.bootstrap.read"
    assert measurement.CONFIGURATIONS["drawn-grid"].driver_module == frozen.driver_module
    assert native.driver_module == "corridor.native_segment_measurement"
    assert (native.engine, native.dpi) == (frozen.engine, frozen.dpi) == ("tagged", 36)
    identity = native.identity(4)
    adapter = identity["source_segment_adapter"]
    assert adapter["driver"] == native.driver_module
    assert adapter["segment_scheme"] == driver.PDF_SEGMENT_SCHEME
    assert adapter["native_adapter_version"] == driver.READER_NATIVE_ADAPTER_VERSION
    assert len(adapter["identity_sha256"]) == 64
    assert all(len(digest) == 64 for digest in adapter["implementation_files"].values())
    assert "source_segment_adapter" not in frozen.identity(4)
    assert provenance.verify() == []


def test_the_unchanged_scorer_reads_the_actual_typed_cell_values(reading, monkeypatch):
    calls = []

    def observed(native_reading):
        calls.append(native_reading.reading_sha256)
        return native_segment_values(native_reading)

    monkeypatch.setattr(driver, "native_segment_values", observed)

    result = driver.segment_reading(reading, key="fixture", pdf="table.pdf")
    score = score_pair(_reference(), result)

    assert calls == [reading.reading_sha256]
    assert score["pass"] is True and score["exact_cells"] == 4
    values = {value.exact_text: value for value in native_segment_values(reading) if value.kind == "pdf_cell"}
    for cell in _cells(result):
        if cell["text"]:
            assert cell["source_segment"]["content_sha256"] == values[cell["text"]].content_sha256
            assert cell["source_segment"]["source_address"]["reading_sha256"] == reading.reading_sha256
            assert "id" not in cell["source_segment"]
    handoff = result["source_segment_handoff"]
    assert handoff["reader_identity"] == reading.identity
    assert handoff["metrics"]["materialized_nonempty_cells"] == 4
    assert handoff["metrics"]["omitted_nonempty_cells"] == 0
    assert "no database persistence" in handoff["scope"]


def test_a_missing_segment_is_scored_as_an_omission_not_filled_from_the_reader(reading, monkeypatch):
    generated = native_segment_values(reading)
    monkeypatch.setattr(driver, "native_segment_values", lambda _: tuple(
        value for value in generated if not (value.kind == "pdf_cell" and value.exact_text == "1149+00")
    ))

    result = driver.segment_reading(reading, key="fixture", pdf="table.pdf")
    score = score_pair(_reference(), result)

    assert score["pass"] is False and score["exact_cells"] == 3
    assert "1149+00" not in [cell["text"] for cell in _cells(result)]
    assert result["source_segment_handoff"]["metrics"]["missing_cell_segments"] == 1
    assert result["source_segment_handoff"]["metrics"]["omitted_nonempty_cells"] == 1


def test_duplicate_cell_segment_candidates_are_an_actual_omission(reading, monkeypatch):
    generated = native_segment_values(reading)
    duplicate = next(value for value in generated if value.kind == "pdf_cell" and value.exact_text == "1149+00")
    monkeypatch.setattr(driver, "native_segment_values", lambda _: (*generated, duplicate))

    result = driver.segment_reading(reading, key="fixture", pdf="table.pdf")

    assert score_pair(_reference(), result)["pass"] is False
    assert result["source_segment_handoff"]["metrics"]["ambiguous_cell_segments"] == 1
    assert result["source_segment_handoff"]["metrics"]["cell_segment_records_not_used"] == 2
    assert "1149+00" not in [cell["text"] for cell in _cells(result)]


@pytest.mark.parametrize(("field", "value"), [
    ("rendition_sha256", "0" * 64), ("reading_sha256", "0" * 64),
    ("reader_identity", {"scheme": "other"}), ("content_sha256", "0" * 64),
    ("page_no", 2), ("table_index", 9), ("cell_row", 99), ("cell_column", 99),
    ("location_json", {"glyphs": [], "display_box": [1, 2, 3, 4]}),
])
def test_wrong_identity_address_digest_or_glyph_membership_cannot_supply_text(
    reading, monkeypatch, field, value
):
    generated = native_segment_values(reading)
    monkeypatch.setattr(driver, "native_segment_values", lambda _: tuple(
        replace(segment, **{field: value})
        if segment.kind == "pdf_cell" and segment.exact_text == "1149+00" else segment
        for segment in generated
    ))

    result = driver.segment_reading(reading, key="fixture", pdf="table.pdf")

    assert score_pair(_reference(), result)["pass"] is False
    assert result["source_segment_handoff"]["metrics"]["omitted_nonempty_cells"] == 1
    assert "1149+00" not in [cell["text"] for cell in _cells(result)]


def test_a_wrong_typed_value_reaches_the_scorer_without_being_repaired(reading, monkeypatch):
    generated = native_segment_values(reading)
    wrong = "wrong typed value"
    monkeypatch.setattr(driver, "native_segment_values", lambda _: tuple(
        replace(value, exact_text=wrong, content_sha256=sha256(wrong.encode()).hexdigest())
        if value.kind == "pdf_cell" and value.exact_text == "1149+00" else value
        for value in generated
    ))

    result = driver.segment_reading(reading, key="fixture", pdf="table.pdf")

    assert wrong in [cell["text"] for cell in _cells(result)]
    assert "1149+00" not in [cell["text"] for cell in _cells(result)]
    assert score_pair(_reference(), result)["pass"] is False


def test_actual_ambiguous_glyph_refusal_is_visible_to_the_measurement(tmp_path):
    path = _pdf(tmp_path / "ambiguous.pdf")

    class SharedGlyphExecutor:
        def read_document(self, source, **kwargs):
            result = PdfiumExecutor().read_document(source, **kwargs)
            nonempty = [cell for cell in result["pages"][0]["tables"]["value"][0]["structured_cells"] if cell["text"]]
            nonempty[1]["source_indices"].append(nonempty[0]["source_indices"][0])
            return result

    ambiguous = driver.read_native_pdf(
        path, source_sha256=sha256(path.read_bytes()).hexdigest(), executor=SharedGlyphExecutor()
    )

    result = driver.segment_reading(ambiguous, key="fixture", pdf="table.pdf")

    assert result["source_segment_handoff"]["metrics"]["missing_cell_segments"] == 2
    assert result["source_segment_handoff"]["metrics"]["ambiguously_assigned_source_characters"] == 1
    assert score_pair(_reference(), result)["pass"] is False


def test_outside_metadata_and_actual_clipped_spans_remain_distinct(tmp_path):
    path = _pdf(tmp_path / "clipped.pdf", outside_and_clipped=True)
    reading = driver.read_native_pdf(path, source_sha256=sha256(path.read_bytes()).hexdigest())

    result = driver.segment_reading(reading, key="fixture", pdf=path.name)

    assert result["pages"][0]["outside"] == slim_page(reading.pages[0])["outside"]
    assert result["pages"][1]["outside"] == slim_page(reading.pages[1])["outside"]
    assert result["pages"][0]["clipped"] == slim_page(reading.pages[0])["clipped"]
    handoff = result["source_segment_handoff"]
    (clipped,) = [span for span in handoff["source_spans"] if span["span_stream"] == "clipped"]
    glyphs = reading.pages[0]["clipped"]["value"]
    # The authored glyph sequence is independent of platform substitute-font
    # metrics. Projection spacing is a property of this exact reading, not a
    # cross-platform literal that may silently replace its stored words.
    assert [glyph["text"] for glyph in glyphs] == list("HIDDEN")
    assert clipped["exact_text"] == reading.page_text(1, stream="clipped")
    assert clipped["exact_text"]
    assert clipped["content_sha256"] == sha256(clipped["exact_text"].encode()).hexdigest()
    assert clipped["source_indices"] == [glyph["source_index"] for glyph in glyphs]
    assert handoff["metrics"]["clipped_characters_in_source_spans"] == 6
    assert handoff["metrics"]["generated_clipped_spans"] == 1
    assert any(span["outside_source_indices"] for span in handoff["source_spans"] if span["span_stream"] == "page")


def test_the_driver_writes_scorer_reads_and_aggregate_handoff_metrics(tmp_path, monkeypatch):
    path = _pdf(tmp_path / "table.pdf")
    pair = corpus.Pair(
        key="fixture", folder=".", pdf=path.name, spreadsheet="never-opened.xlsx",
        tier="A", reading="fixture", pages=1, visible_cells=4,
        printed_sheets=("S",), unprinted_sheets=(),
        pdf_sha256=sha256(path.read_bytes()).hexdigest(), book_sha256="0" * 64,
    )
    monkeypatch.setattr(corpus, "TRUE_PAIRS", tmp_path)
    monkeypatch.setattr(driver, "load_pairs", lambda: [pair])
    output = tmp_path / "measurement"

    assert driver.main(["--output", str(output), "--jobs", "2", "--keys", "fixture"]) == 0

    read = json.loads((output / "reads/fixture.json").read_text())
    receipts = json.loads((output / "read-receipts.json").read_text())
    assert score_pair(_reference(), read)["pass"] is True
    assert receipts["source_segment_metrics"] == read["source_segment_handoff"]["metrics"]
    assert receipts["source_segment_metrics"]["materialized_nonempty_cells"] == 4
    assert len(receipts["receipts"]) == 1 and "error" not in receipts["receipts"][0]
    assert not (tmp_path / "never-opened.xlsx").exists()
