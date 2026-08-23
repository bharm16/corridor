import json
from pathlib import Path

import pytest

from corridor.verify import (
    MIN_TOKEN_CHARS,
    THRESHOLD,
    literal_quote_on_page,
    match_ratio,
    quote_appears_on,
    tokens,
    unverified_fields,
    value_appears_on,
)

PAGE = """NHHIP Segment 3C-2 Utility Conflict Matrix

FOC1-1    AT&T Texas (SWBT)    Telecom    FOC    UG    IH 69    Crossing
          IH 69    Schwartz Street    Eastex Freeway FR SB    1149+00    303
"""


def verified(quote, page=PAGE):
    return quote_appears_on(quote, page)


def test_an_exact_quote_verifies():
    assert verified("AT&T Texas (SWBT)")
    assert match_ratio("AT&T Texas (SWBT)", PAGE) == 1.0


def test_whitespace_differences_do_not_break_a_citation():
    """PDF text extraction inserts newlines and runs of spaces at will."""
    assert verified("AT&T Texas (SWBT)    Telecom")
    assert verified("AT&T Texas (SWBT) Telecom")
    assert verified("AT&T Texas (SWBT)\n Telecom")


def test_case_differences_do_not_break_a_citation():
    assert verified("at&t texas (swbt)")


def test_typographic_quotes_and_dashes_are_normalized():
    page = "the Utility shall complete relocation — see “Exhibit D”"
    assert quote_appears_on('relocation - see "Exhibit D"', page)


def test_hyphenation_across_a_line_break_is_repaired():
    """A quote spanning a hyphenated line break is still the same words."""
    page = "the utility must complete reloca-\ntion before construction"
    assert quote_appears_on("complete relocation before construction", page)


def test_literal_quote_on_page_recovers_the_exact_page_substring():
    page = "the Utility shall complete reloca-\ntion — see “Exhibit D”"

    assert literal_quote_on_page(
        'complete relocation - see "Exhibit D"', page
    ) == 'complete reloca-\ntion — see “Exhibit D”'


def test_literal_quote_on_page_rejects_a_normalized_near_miss():
    page = "Kinder Morgan will finish in July 2026."

    assert literal_quote_on_page(
        "Kinder Morgan will finish in June 2026.", page
    ) is None


def test_a_quote_from_a_different_page_does_not_verify():
    assert not verified("CenterPoint Energy Gas at STA 2414+50")


def test_an_empty_quote_never_verifies():
    """Otherwise a citation with no content would pass, asserting nothing."""
    assert not verified("")
    assert not verified("   \n  ")
    assert match_ratio("", PAGE) == 0.0


def test_a_near_miss_below_threshold_fails():
    # Same shape, wrong owner and wrong station: a plausible hallucination.
    assert not verified("AT&T Texas (SWBT) Telecom FOC UG IH 69 Parallel 9999+00 817")


def test_small_ocr_style_noise_still_verifies():
    assert verified("AT&T Texas (SWBT)  Telecorn")


@pytest.mark.parametrize("quote", ["Schwartz Street", "Eastex Freeway FR SB", "1149+00"])
def test_short_field_values_verify(quote):
    assert verified(quote)


def test_threshold_is_the_documented_one():
    """v0-build-spec.md 7 fixes this at 0.9; drifting it silently changes
    every recall and citation-validity number ever recorded."""
    assert THRESHOLD == 0.9


# ------------------------------------------------------- field-token checking


def test_a_field_value_that_is_on_the_page_passes():
    assert value_appears_on("AT&T Texas (SWBT)", PAGE)
    assert value_appears_on("Schwartz Street", PAGE)
    assert value_appears_on("1149+00", PAGE)


def test_a_value_with_an_invented_token_fails():
    """The defect a fuzzy row-level quote match can never catch.

    A transcribed `1140+00` sits inside a perfectly valid row quote and
    clears 0.9 comfortably, so citation verification passes it. This is
    the only mechanical check on field *values*.
    """
    assert not value_appears_on("1140+00", PAGE)
    assert not value_appears_on("Comcast of Houston", PAGE)


def test_a_clipped_value_fails():
    """`Rothwell Street` arrived from PyMuPDF as `othwell Street` (#44).

    Cutting on punctuation rather than matching substrings is what keeps
    this catchable: `othwell` is still not `schwartz`, and it is still not
    a token of anything on the page.
    """
    assert not value_appears_on("chwartz Street", PAGE)


def test_a_combined_station_and_offset_cell_passes_both_halves():
    """Some layouts carry stationing and offset in one column, and the
    page's own tokenization keeps them together. Splitting the cell into
    two real fields is correct, and both halves are on the page, so the
    comma must not read as invented text."""
    page = "135+58.68, 236.85' LT  CENTURY LINK  Buried fiber"
    assert value_appears_on("135+58.68", page)
    assert value_appears_on("236.85", page)
    assert value_appears_on("135+58.68, 236.85'", page)


def test_short_tokens_are_not_checked():
    """`R`, `Y`, `UG` are enum-ish and match anywhere, so requiring them
    proves nothing and rejecting them would fail every row."""
    assert value_appears_on("UG", PAGE)
    assert value_appears_on("R", PAGE)


def test_an_empty_value_is_not_a_claim():
    assert value_appears_on("", PAGE)
    assert value_appears_on(None, PAGE)


def test_unverified_fields_names_every_field_that_failed():
    """The caller needs to know *which* value is suspect, not just that one is."""
    fields = {
        "external_org": "AT&T Texas (SWBT)",
        "station_from": "1140+00",
        "location_start": "Nowhere Boulevard",
    }
    assert unverified_fields(fields, PAGE) == {"station_from", "location_start"}
    assert unverified_fields({"external_org": "AT&T Texas (SWBT)"}, PAGE) == set()


# ------------------------------------------- the recorded misread corpus


MISREADS = Path(__file__).parent / "fixtures" / "vision-misreads.json"


@pytest.fixture(scope="module")
def corpus():
    return json.loads(MISREADS.read_text())


def _page(corpus, entry):
    return " ".join(corpus["pages"][entry["page"]])


def _survivors(corpus, check):
    return [m for m in corpus["misreads"] if check(m["value"], _page(corpus, m))]


def test_every_recorded_misread_is_still_caught(corpus):
    """188 real transcription failures, from a real model, on real pages.

    Recorded before the Candidates carrying them were deleted, and not
    reproducible: re-extracting produces different wrong rows. They are the
    empirical basis of ADR-0006, and this is what makes them useful
    afterwards.
    """
    survived = _survivors(corpus, value_appears_on)

    assert not survived, (
        f"{len(survived)} recorded misreads are no longer flagged, "
        f"e.g. {[(s['field'], s['value']) for s in survived[:5]]}"
    )


def test_the_corpus_notices_a_loosened_checker(corpus):
    """The test above only proves today's checker flags them. This proves
    the corpus can still tell when a change stops it.

    The loosening modelled here is the tempting one — count a token as
    present if any page token contains it, which would "fix" the 29
    span-concatenation entries and silently swallow every misread the
    model produced by splitting a word.

    Not hypothetical. An earlier version of this fixture stored only each
    value's own present tokens, which stripped out the glued page tokens
    a loosened checker matches on. Every assertion still passed and the
    corpus detected nothing.
    """

    def loosened(value, page_text):
        page = tokens(page_text)
        return all(
            any(token in page_token for page_token in page)
            for token in tokens(value)
            if len(token) >= MIN_TOKEN_CHARS
        )

    assert _survivors(corpus, loosened), (
        "a substring-matching checker flags every recorded misread, so this "
        "corpus can no longer detect that loosening"
    )


def test_the_corpus_records_what_it_cannot_decide(corpus):
    """Not all 188 are the model's fault, and the fixture must not pretend.

    Where the page's own text stream glued a correct value to its
    neighbour, the checker cannot see the value and flags a row that is
    right. Telling that apart from the model splitting a word was
    attempted and does not work — `1148+60` inside `freeway1148+60` and
    `phonosc` inside `phonoscope` are the same shape to a substring test.
    So the evidence is recorded and the verdict is not.
    """
    assert "what_is_not_claimed" in corpus
    ambiguous = [
        m for m in corpus["misreads"] if m["page_tokens_containing_an_absent_one"]
    ]
    assert len(ambiguous) == corpus["counts"]["with_a_containing_page_token"]
    assert 0 < len(ambiguous) < len(corpus["misreads"])
    # No count may read as a number of model errors, which is the claim the
    # separation above cannot support.
    assert not any("error" in key for key in corpus["counts"])


def test_the_corpus_is_internally_consistent(corpus):
    """A fixture that drifted from its own counts would fail silently."""
    counts = corpus["counts"]
    assert counts["flagged_field_values"] == len(corpus["misreads"])
    assert counts["distinct_page_field_value"] == len(
        {(m["page"], m["field"], m["value"]) for m in corpus["misreads"]}
    )
    assert counts["distinct_wrong_values"] == len(
        {m["value"] for m in corpus["misreads"]}
    )
    assert counts["pages"] == len(corpus["pages"])

    for entry in corpus["misreads"]:
        assert entry["absent_tokens"], f"{entry['value']!r} flags nothing"
        page = set(corpus["pages"][entry["page"]])
        for token in entry["absent_tokens"]:
            assert token not in page, f"{entry['value']!r}: {token!r}"


# ---------------- a source with no print damage (ADR-0005, #60)


def test_a_citation_against_cells_must_match_exactly():
    """The 0.9 threshold exists for damage that cannot happen here.

    It is there because a printout loses separators between text spans and
    clips cells at their boundaries, so a true quote can come back slightly
    wrong. A spreadsheet's page text is generated from the same cells the
    values came from — nothing is recovered, so nothing is approximate, and
    a near-miss is a real disagreement rather than print damage.
    """
    from corridor.verify import threshold_for

    assert threshold_for("cells") == 1.0
    assert threshold_for("text_layer") == THRESHOLD
    assert threshold_for("ocr") == THRESHOLD


def test_a_near_miss_passes_on_a_printout_and_fails_on_a_sheet():
    """The threshold difference, as the behaviour it buys."""
    from corridor.verify import quote_appears_on, threshold_for

    page = "UC-1 CenterPoint Energy Electric 1149+00"
    # One digit out, and not a prefix of the page — a clipped cell on a
    # printout, a wrong number anywhere else.
    nearly = "UC-1 CenterPoint Energy Electric 1149+01"

    assert quote_appears_on(nearly, page, threshold_for("text_layer")) is True
    assert quote_appears_on(nearly, page, threshold_for("cells")) is False


def test_an_exact_quote_still_passes_against_cells():
    """The strict threshold must not make a true citation fail — every
    quote this reader writes is a row of the text it is checked against."""
    from corridor.verify import quote_appears_on, threshold_for

    page = "UC-1 CenterPoint Energy Electric 1149+00"

    assert quote_appears_on(page, page, threshold_for("cells")) is True
