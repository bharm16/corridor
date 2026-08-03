import pytest

from corridor.verify import (
    THRESHOLD,
    match_ratio,
    quote_appears_on,
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
