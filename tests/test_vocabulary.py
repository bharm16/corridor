"""The retirement rule, at its single home (ADR-0012).

Direct tests, because both extractor tiers and the sheet path share this
predicate and a drift in its semantics would break zero of their tests
while changing what enters the Ledger.
"""

from corridor.gold import RETIREMENT_PHRASES as GOLD_PHRASES
from corridor.vocabulary import RETIREMENT_PHRASES, is_retired_row


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
