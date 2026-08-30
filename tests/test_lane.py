"""The lane's judgments, tested where they live.

These rules previously existed only inside HTTP handlers, so the only way
to ask "is this one gesture over one conflict?" was to post a form and
read a status code. They need no database and no request.
"""

import pytest

from corridor.identity import PER_PARTY
from corridor.lane import (
    ConflictUnidentified,
    MalformedSiblingList,
    NotTheSameConflict,
    SiblingsNeedTheEventLane,
    check_sibling_request,
    check_sibling_set,
    conflict_key,
    same_conflict,
)
from corridor.models import Candidate

# Documents absent from the mapping read as project-unique — the declared
# default — so the plain cases pass an empty mapping.
NO_SCHEMES: dict[int, str] = {}


def _candidate(
    candidate_id: int,
    utility_id: str | None,
    *,
    org: str | None = None,
    document_id: int = 1,
) -> Candidate:
    fields = {"utility_id": utility_id} if utility_id is not None else {}
    if org is not None:
        fields["external_org"] = org
    return Candidate(
        id=candidate_id,
        payload_json={"fields": fields},
        source_document_id=document_id,
    )


def test_the_conflict_key_is_the_identity_under_the_declared_scheme():
    assert conflict_key(_candidate(1, "W12"), NO_SCHEMES) == ("", "W12")


@pytest.mark.parametrize("empty", [None, ""])
def test_a_candidate_naming_no_utility_states_no_conflict(empty):
    assert conflict_key(_candidate(1, empty), NO_SCHEMES) is None


def test_the_key_compares_as_text_so_a_number_matches_its_string():
    """The payload is JSON a model wrote; 12 and "12" are the same row."""
    numeric = Candidate(
        id=1, payload_json={"fields": {"utility_id": 12}}, source_document_id=1
    )

    assert same_conflict(numeric, _candidate(2, "12"), NO_SCHEMES)


def test_offering_and_refusing_use_one_predicate():
    """The rule was written twice in one file — once to decide which
    revisions to offer as merges, once to decide which to accept."""
    primary = _candidate(1, "W12")

    assert same_conflict(primary, _candidate(2, "W12"), NO_SCHEMES)
    assert not same_conflict(primary, _candidate(3, "W13"), NO_SCHEMES)


def test_an_unidentified_conflict_matches_nothing_including_itself():
    """Absent is not equal to absent: two rows that each name no utility
    are not thereby revisions of one another."""
    anonymous = _candidate(1, None)

    assert not same_conflict(anonymous, _candidate(2, None), NO_SCHEMES)


def test_under_a_per_party_scheme_the_name_is_the_party_and_the_number():
    """Nine parties each correctly have a conflict 1 (ADR-0030): sharing
    the number does not make two parties' rows one conflict."""
    schemes = {1: PER_PARTY}
    att = _candidate(1, "1", org="AT&T TCA")

    assert conflict_key(att, schemes) == ("at t tca", "1")
    assert same_conflict(att, _candidate(2, "1", org="AT&T TCA"), schemes)
    assert not same_conflict(att, _candidate(3, "1", org="Comcast"), schemes)


def test_a_per_party_row_with_no_party_states_no_conflict():
    schemes = {1: PER_PARTY}
    assert conflict_key(_candidate(1, "1"), schemes) is None


def test_one_party_spelled_two_ways_is_one_conflict():
    """Revisions of one form spell a company differently; keying on the
    raw string made that two records with the same number."""
    schemes = {1: PER_PARTY}
    assert same_conflict(
        _candidate(1, "1", org="AT&T Texas"),
        _candidate(2, "1", org="AT&T  TEXAS"),
        schemes,
    )


def test_a_party_the_document_declined_to_name_states_no_conflict():
    """`N/A` on two rows is not one party twice."""
    schemes = {1: PER_PARTY}
    assert conflict_key(_candidate(1, "1", org="N/A"), schemes) is None


def test_no_siblings_asked_for_is_always_coherent():
    check_sibling_request(
        _candidate(1, None), [], in_event_lane=False, schemes=NO_SCHEMES
    )
    check_sibling_set(_candidate(1, None), [], NO_SCHEMES)


def test_sibling_merges_are_the_event_lanes_affordance():
    with pytest.raises(SiblingsNeedTheEventLane):
        check_sibling_request(
            _candidate(1, "W12"), [2], in_event_lane=False, schemes=NO_SCHEMES
        )


def test_a_gesture_may_not_name_the_candidate_it_accepts():
    with pytest.raises(MalformedSiblingList):
        check_sibling_request(
            _candidate(1, "W12"), [1, 2], in_event_lane=True, schemes=NO_SCHEMES
        )


def test_a_gesture_may_not_name_a_sibling_twice():
    with pytest.raises(MalformedSiblingList):
        check_sibling_request(
            _candidate(1, "W12"), [2, 2], in_event_lane=True, schemes=NO_SCHEMES
        )


def test_a_primary_naming_no_conflict_cannot_take_siblings():
    with pytest.raises(ConflictUnidentified):
        check_sibling_request(
            _candidate(1, None), [2], in_event_lane=True, schemes=NO_SCHEMES
        )


def test_one_gesture_covers_one_conflict():
    primary = _candidate(1, "W12")

    check_sibling_set(
        primary, [_candidate(2, "W12"), _candidate(3, "W12")], NO_SCHEMES
    )

    with pytest.raises(NotTheSameConflict, match="one gesture covers one"):
        check_sibling_set(
            primary, [_candidate(2, "W12"), _candidate(3, "W13")], NO_SCHEMES
        )


def test_the_whole_set_is_judged_before_anything_is_written():
    """A refused sibling refuses the gesture; there is no partial accept."""
    primary = _candidate(1, "W12")

    with pytest.raises(NotTheSameConflict) as refusal:
        check_sibling_set(
            primary, [_candidate(9, "W99"), _candidate(2, "W12")], NO_SCHEMES
        )

    # It names the offending row rather than failing at whichever it reached.
    assert "candidate 9" in str(refusal.value)
