"""The retirement rule, at its single home (ADR-0012).

Direct tests, because both extractor tiers and the sheet path share this
predicate and a drift in its semantics would break zero of their tests
while changing what enters the Ledger.
"""

from corridor.gold import RETIREMENT_PHRASES as GOLD_PHRASES
from corridor.vocabulary import (
    RETIREMENT_PHRASES,
    ROW_FIELDS,
    UCM_CONFLICT_LIST_HEADINGS,
    dedupe_hint,
    is_retired_row,
    normalize_header,
)


def test_the_ucm_conflict_list_form_maps_only_to_real_canonical_fields():
    """The second published TxDOT form's columns map to existing fields, never
    a phantom one (#365). A typo here would file a value under a heading no
    reader downstream knows, which is the drift ROW_FIELDS exists to close."""
    assert set(UCM_CONFLICT_LIST_HEADINGS.values()) <= set(ROW_FIELDS)


def test_a_numbered_retired_row_is_retired():
    assert is_retired_row({"utility_id": "72", "notes": "Not Used"}) is True


def test_a_phrase_only_row_is_retired():
    """4 of 9424's 101 Retired Rows carry no number at all."""
    assert is_retired_row({"notes": "Not used"}) is True


def test_a_populated_row_carrying_the_phrase_is_not_retired():
    """Row 210: the phrase beside real content describes the facility."""
    assert (
        is_retired_row(
            {"utility_id": "210", "external_org": "PSE", "notes": "Not used"}
        )
        is False
    )


def test_a_superstring_of_the_phrase_does_not_retire():
    """Whole-cell equality, not containment: `Not used for potable supply`
    is a note about a facility, and retiring it would hide a conflict
    behind two words of its own prose. The report may *flag* substrings —
    over-reporting to a human is safe — but the extractor may not act on
    them (ADR-0012)."""
    assert is_retired_row({"notes": "Not used for potable supply"}) is False


def test_an_identifier_alone_is_not_retired():
    """An empty slot (ids 163, 256) is a different thing: no phrase, no
    claim — the row guards handle it, not this rule."""
    assert is_retired_row({"utility_id": "163"}) is False


def test_an_empty_row_is_not_retired():
    assert is_retired_row({}) is False


def test_the_report_and_the_extractors_share_one_phrase_list():
    """The day the list grows in one place and not the other, the
    reviewer's diff and the Ledger disagree about what retires a row —
    the exact drift ADR-0012 forbids. Identity, not equality: an equal
    copy would pass today and drift tomorrow."""
    assert GOLD_PHRASES is RETIREMENT_PHRASES


def test_dedupe_hint_is_org_type_and_station_range():
    fields = {"external_org": "AT&T Texas (SWBT)", "utility_type": "Telecom",
              "station_from": "1149+00", "station_to": "1153+17"}
    assert dedupe_hint(fields) == "AT&T Texas (SWBT)|Telecom|1149+00-1153+17"


def test_a_printed_heading_normalizes_to_one_spaced_uppercase_form():
    """A heading arrives wrapped across lines and cased however it was typed."""
    assert normalize_header("Dependent\n  Activity ") == "DEPENDENT ACTIVITY"
    assert normalize_header(None) == ""
