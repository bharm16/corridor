"""The Report Reading payload's version, digest and governed names (#633).

These are pure over the payload shape: a reader of a ten-month-old retained
reading must not need a database, a report, or an evaluation to make sense of
it, and these tests are written the way such a reader would be. The
database-backed half — that a sealed payload survives a JSONB round trip with
its digest intact, and that a report writes the governed names — lives beside
the diff baseline in ``test_report_diff_reference.py``.
"""

import pytest

from corridor.report_reading import (
    DIGEST_KEY,
    LEGACY_PAYLOAD_SCHEMA_VERSION,
    LEGACY_TO_GOVERNED,
    PAYLOAD_SCHEMA_VERSION,
    PROMISED_FOR_PROJECTION_RULE_VERSION,
    SCHEMA_VERSION_KEY,
    content_digest,
    digest_is_intact,
    payload_schema_version,
    read_entries,
    read_entry,
    seal,
)


V1_PAYLOAD = {
    "ruleset_version": "v0.4",
    "thresholds": {"stale_days": 14},
    "dependencies": {
        "DEP-1": {
            "id": 7,
            "resolution_strategy": "relocate",
            "committed_date": "2026-06-03",
            "need_date": "2026-07-01",
            "ready": False,
            "exceptions": ["ORPHAN", "STALE"],
        }
    },
}


def test_a_payload_written_before_the_version_key_is_version_one():
    """Absence is an answer, not a defect: those payloads are evidence."""

    assert payload_schema_version(V1_PAYLOAD) == LEGACY_PAYLOAD_SCHEMA_VERSION
    assert payload_schema_version({}) == LEGACY_PAYLOAD_SCHEMA_VERSION
    assert payload_schema_version(None) == LEGACY_PAYLOAD_SCHEMA_VERSION
    assert payload_schema_version(seal(V1_PAYLOAD)) == PAYLOAD_SCHEMA_VERSION


def test_sealing_stamps_the_version_and_a_digest_over_the_rest():
    sealed = seal(V1_PAYLOAD)

    assert sealed[SCHEMA_VERSION_KEY] == PAYLOAD_SCHEMA_VERSION
    assert sealed[DIGEST_KEY] == content_digest(sealed)
    assert digest_is_intact(sealed) is True
    # The original is not mutated: a caller's dict is its own.
    assert DIGEST_KEY not in V1_PAYLOAD


def test_the_digest_ignores_key_order_but_not_content():
    """It has to survive a JSONB round trip, which does not preserve order."""

    sealed = seal(V1_PAYLOAD)
    reordered = dict(reversed(list(sealed.items())))

    assert digest_is_intact(reordered) is True

    tampered = {**sealed, "ruleset_version": "v0.5"}
    assert digest_is_intact(tampered) is False


def test_a_payload_with_no_digest_is_uncheckable_not_wrong():
    """"Not checkable" and "checked and wrong" are different answers.

    Every version 1 payload is in the first case, and reading it as the second
    would report an intact historical reading as corrupt.
    """

    assert digest_is_intact(V1_PAYLOAD) is None
    assert digest_is_intact({}) is None
    assert digest_is_intact(None) is None


def test_a_version_one_entry_reads_under_the_governed_names():
    [entry] = read_entries(V1_PAYLOAD).values()

    assert entry["documentation_requirement_met"] is False
    assert entry["constraint_alerts"] == ["ORPHAN", "STALE"]
    assert entry["published_promised_for"] == "2026-06-03"
    # Translated, never duplicated: one key, one quantity.
    assert set(entry).isdisjoint(LEGACY_TO_GOVERNED)
    # Record-owned fields are carried through untouched.
    assert entry["resolution_strategy"] == "relocate"
    assert entry["need_date"] == "2026-07-01"
    assert entry["id"] == 7


def test_translation_never_invents_a_key_the_payload_did_not_have():
    """A payload written before #96 recorded no ``resolution_strategy``.

    The diff reads that absence as unknown, never as "was not critical"
    (ADR-0044). A translator that filled the gap would report an escalation
    for every record on the first report after a migration.
    """

    entry = read_entry({"id": 7, "ready": True})

    assert entry == {"id": 7, "documentation_requirement_met": True}
    assert "constraint_alerts" not in entry
    assert "published_promised_for" not in entry
    assert "resolution_strategy" not in entry


def test_a_governed_key_already_present_is_never_overwritten_by_a_legacy_one():
    """A payload holding both is malformed; the governed name still wins."""

    entry = read_entry({"ready": False, "documentation_requirement_met": True})

    assert entry == {"documentation_requirement_met": True}


def test_the_projected_promised_for_is_not_the_committed_date_fact():
    """The rename is the point, so the two names must not collapse again.

    ``committed_date`` on the spine is the source cell. The reading's value is
    the date projected from the external party's current statement, which is
    ``None`` where no statement was recorded whatever the cell says. One key
    carrying both is the defect ADR-0092 closes.
    """

    assert LEGACY_TO_GOVERNED["committed_date"] == "published_promised_for"
    assert "committed_date" not in read_entry(
        V1_PAYLOAD["dependencies"]["DEP-1"]
    )
    assert PROMISED_FOR_PROJECTION_RULE_VERSION


@pytest.mark.parametrize("payload", [None, {}, {"dependencies": None}])
def test_a_payload_with_no_population_reads_as_an_empty_one(payload):
    assert read_entries(payload) == {}
