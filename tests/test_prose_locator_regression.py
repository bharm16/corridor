"""Corpus text matches must not masquerade as physical citation rebinding.

The generated PDFs put repeated labels in deliberately different places and
orders. Their authored locations and unique neighboring labels establish
which physical occurrence each reader reaches, independently of string
equality. The audit has no occurrence-bound coordinate mapping, so it reports
location unknown even when an old offset still returns exactly the same text.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from corridor.source_segments import ProseSegment
from corridor.token_layers import page_text_projection, read_native_token_layers

from pdf_fixture_support import PdfFixture


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prose_locator_regression", ROOT / "scripts/prose_locator_regression.py"
)
regression = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = regression
SPEC.loader.exec_module(regression)


def _segment(text: str, start: int = 0) -> ProseSegment:
    return ProseSegment(
        ordinal=1,
        page_no=1,
        start_offset=start,
        end_offset=start + len(text),
        exact_text=text,
        content_sha256=sha256(text.encode("utf-8")).hexdigest(),
    )


@pytest.mark.parametrize(
    ("historical", "replacement", "start", "outcome", "positions"),
    [
        ("Owner", "Owner", 0, "exact_text_at_recorded_offsets", (0,)),
        ("Owner", "New\nOwner", 0, "exact_text_unique_candidate", (4,)),
        (
            "Owner\nOwner",
            "New\nOwner\nOwner",
            0,
            "exact_text_equal_count_ordinal_candidate",
            (4, 10),
        ),
        (
            "Owner",
            "New\nOwner\nOwner",
            0,
            "exact_text_multiple_candidates",
            (4, 10),
        ),
        ("Owner", "Status", 0, "exact_text_absent", ()),
        ("Owner", None, 0, "replacement_page_missing", ()),
        (None, "Owner", 0, "historical_locator_invalid", ()),
    ],
)
def test_text_outcomes_never_supply_a_replacement_locator(
    historical, replacement, start, outcome, positions
):
    comparison = regression.classify(_segment("Owner", start), historical, replacement)

    assert comparison.text_outcome == outcome
    assert comparison.candidate_start_offsets == positions
    assert comparison.source_location_outcome == "unknown"
    assert comparison.source_location_reason == "independent_occurrence_coordinates_not_collected"
    assert comparison.rebound_start_offset is None


def test_reversed_physical_duplicates_refuse_same_offset_rebinding(tmp_path):
    fixture = PdfFixture()
    page = fixture.add_page(width=340, height=250)
    lower, _ = page.text((170, 180), "REVIEW\nLOWER")
    upper, _ = page.text((35, 65), "REVIEW\nUPPER")
    pdf = fixture.save(tmp_path / "reversed-duplicates.pdf")
    historical = regression.historical_page_texts(pdf)[1]
    (layer,) = read_native_token_layers(pdf, source_sha256=sha256(pdf.read_bytes()).hexdigest())
    replacement = page_text_projection(layer)

    # Content-stream order starts at LOWER; geometric reading starts at UPPER.
    # The adjacent unique labels make this a different physical occurrence,
    # despite the identical text, offset, occurrence count and ordinal.
    assert historical == "REVIEW\nLOWER\nREVIEW\nUPPER\n"
    assert replacement == "REVIEW\nUPPER\nREVIEW\nLOWER"
    positioned = [token for token in layer.tokens if token.raw_text == "REVIEW"]
    assert len(positioned) == 2
    assert positioned[0].polygon_pdf.x0 == upper.fixed_point_box()[0]
    assert positioned[1].polygon_pdf.x0 == lower.fixed_point_box()[0]
    assert positioned[0].polygon_pdf.y1 < positioned[1].polygon_pdf.y0

    comparison = regression.classify(_segment("REVIEW"), historical, replacement)

    assert comparison.text_outcome == "exact_text_at_recorded_offsets"
    assert comparison.candidate_start_offsets == (0, 13)
    assert comparison.source_location_outcome == "unknown"
    assert comparison.rebound_start_offset is None


def test_reordered_columns_keep_duplicate_candidates_unbound(tmp_path):
    fixture = PdfFixture()
    page = fixture.add_page(width=340, height=200)
    right, _ = page.text((220, 70), "STATUS\nRIGHT")
    left, _ = page.text((35, 70), "STATUS\nLEFT")
    pdf = fixture.save(tmp_path / "reordered-columns.pdf")
    historical = regression.historical_page_texts(pdf)[1]
    (layer,) = read_native_token_layers(pdf, source_sha256=sha256(pdf.read_bytes()).hexdigest())
    replacement = page_text_projection(layer)

    assert historical == "STATUS\nRIGHT\nSTATUS\nLEFT\n"
    assert replacement == "STATUSSTATUS\nLEFT RIGHT"
    (joined,) = [token for token in layer.tokens if token.raw_text == "STATUSSTATUS"]
    # This reading coalesces the labels into one word box spanning both
    # columns. That real box cannot identify either substring by itself.
    assert joined.polygon_pdf.x0 == left.fixed_point_box()[0]
    assert joined.polygon_pdf.x1 > right.fixed_point_box()[0]
    old_left = _segment("STATUS", historical.index("STATUS", 1))

    comparison = regression.classify(old_left, historical, replacement)

    assert comparison.text_outcome == "exact_text_equal_count_ordinal_candidate"
    assert comparison.candidate_start_offsets == (0, 6)
    assert comparison.source_location_outcome == "unknown"
    assert comparison.rebound_start_offset is None


@pytest.mark.parametrize("survivor", ["REVIEW\nUPPER", "UPPER\nREVIEW"])
def test_one_surviving_duplicate_does_not_identify_the_original_occurrence(survivor):
    historical = "REVIEW\nLOWER\nREVIEW\nUPPER"
    lower = _segment("REVIEW")

    comparison = regression.classify(lower, historical, survivor)

    assert comparison.text_outcome in {
        "exact_text_at_recorded_offsets", "exact_text_unique_candidate"
    }
    assert len(comparison.candidate_start_offsets) == 1
    assert comparison.source_location_outcome == "unknown"
    assert comparison.rebound_start_offset is None


@pytest.mark.parametrize(
    ("historical", "replacement"),
    [
        ("Obtain owner\napproval", "Obtain owner approval"),
        ("Re\u00adlocate utility", "Re-locate utility"),
        ("Owner: AT&T", "Owner: AT"),
    ],
    ids=["multiline", "soft-hyphen", "clipped-text"],
)
def test_normalization_or_clipping_cannot_turn_missing_exact_text_into_recovery(
    historical, replacement
):
    comparison = regression.classify(_segment(historical), historical, replacement)

    assert comparison.text_outcome == "exact_text_absent"
    assert comparison.candidate_start_offsets == ()
    assert comparison.source_location_outcome == "unknown"
    assert comparison.rebound_start_offset is None


@pytest.mark.parametrize(
    "segment",
    [
        replace(_segment("Owner"), content_sha256="0" * 64),
        replace(_segment("Owner"), start_offset=-5),
        replace(_segment("Owner"), end_offset=6),
        _segment(""),
    ],
    ids=["bad-digest", "negative-offset", "past-page-end", "empty-span"],
)
def test_a_broken_historical_control_is_never_a_replacement_reader_failure(segment):
    comparison = regression.classify(segment, "Owner", "Owner")

    assert comparison.text_outcome == "historical_locator_invalid"
    assert comparison.candidate_start_offsets == ()
    assert comparison.rebound_start_offset is None


def test_corpus_measurement_reports_text_and_unknown_locations_separately(tmp_path):
    fixture = PdfFixture()
    fixture.add_page().text((50, 60), "Owner\nOwner")
    pdf = fixture.save(tmp_path / "measured.pdf")

    counts, document = regression.measure_document(
        pdf, regression.PdfiumExecutor(), [], sha256(pdf.read_bytes()).hexdigest()
    )
    summary = counts.as_json()

    assert document == summary
    assert summary["segments"] == 2
    assert summary["exact_text_recovered"] == 2
    assert summary["exact_text_not_recovered"] == 0
    assert summary["historical_locator_invalid"] == 0
    assert summary["by_source_location_outcome"] == {"unknown": 2}
    assert summary["replacement_locators_proved"] == 0


def test_receipts_are_explicit_about_population_and_never_overwrite_history(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(regression, "registered_pdfs", lambda: [])
    output = tmp_path / "new-receipt.json"

    assert regression.main(["--output", str(output)]) == 0
    receipt_bytes = output.read_bytes()
    receipt = json.loads(receipt_bytes)

    assert receipt["schema_version"] == "corridor.prose-locator-regression.v2"
    assert "never queries stored Source Segments" in receipt["what_was_compared"]
    assert "no count is a count of broken customer citations" in receipt["what_was_compared"]
    assert "Every location is unknown" in receipt["source_location_evidence"]
    assert receipt["summary"]["all_pdf_population"]["replacement_locators_proved"] == 0
    assert receipt["clarifies_frozen_receipt"]["sha256"] == sha256(
        regression.FROZEN_RECEIPT.read_bytes()
    ).hexdigest()

    with pytest.raises(SystemExit) as error:
        regression.main(["--output", str(output)])

    assert error.value.code == 2
    assert "receipt already exists" in capsys.readouterr().err
    assert output.read_bytes() == receipt_bytes
