"""The lane's judgments, tested where they live.

These rules previously existed only inside HTTP handlers, so the only way
to ask "is this one gesture over one conflict?" was to post a form and
read a status code. They need no database and no request.
"""

import pytest

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


def _candidate(candidate_id: int, utility_id: str | None) -> Candidate:
    fields = {"utility_id": utility_id} if utility_id is not None else {}
    return Candidate(id=candidate_id, payload_json={"fields": fields})


def test_the_conflict_key_is_the_utility_id():
    assert conflict_key(_candidate(1, "W12")) == "W12"


@pytest.mark.parametrize("empty", [None, ""])
def test_a_candidate_naming_no_utility_states_no_conflict(empty):
    assert conflict_key(_candidate(1, empty)) is None


def test_the_key_compares_as_text_so_a_number_matches_its_string():
    """The payload is JSON a model wrote; 12 and "12" are the same row."""
    numeric = Candidate(id=1, payload_json={"fields": {"utility_id": 12}})

    assert same_conflict(numeric, _candidate(2, "12"))


def test_offering_and_refusing_use_one_predicate():
    """The rule was written twice in one file — once to decide which
    revisions to offer as merges, once to decide which to accept."""
    primary = _candidate(1, "W12")

    assert same_conflict(primary, _candidate(2, "W12"))
    assert not same_conflict(primary, _candidate(3, "W13"))


def test_an_unidentified_conflict_matches_nothing_including_itself():
    """Absent is not equal to absent: two rows that each name no utility
    are not thereby revisions of one another."""
    anonymous = _candidate(1, None)

    assert not same_conflict(anonymous, _candidate(2, None))


def test_no_siblings_asked_for_is_always_coherent():
    check_sibling_request(_candidate(1, None), [], in_event_lane=False)
    check_sibling_set(_candidate(1, None), [])


def test_sibling_merges_are_the_event_lanes_affordance():
    with pytest.raises(SiblingsNeedTheEventLane):
        check_sibling_request(_candidate(1, "W12"), [2], in_event_lane=False)


def test_a_gesture_may_not_name_the_candidate_it_accepts():
    with pytest.raises(MalformedSiblingList):
        check_sibling_request(_candidate(1, "W12"), [1, 2], in_event_lane=True)


def test_a_gesture_may_not_name_a_sibling_twice():
    with pytest.raises(MalformedSiblingList):
        check_sibling_request(_candidate(1, "W12"), [2, 2], in_event_lane=True)


def test_a_primary_naming_no_conflict_cannot_take_siblings():
    with pytest.raises(ConflictUnidentified):
        check_sibling_request(_candidate(1, None), [2], in_event_lane=True)


def test_one_gesture_covers_one_conflict():
    primary = _candidate(1, "W12")

    check_sibling_set(primary, [_candidate(2, "W12"), _candidate(3, "W12")])

    with pytest.raises(NotTheSameConflict, match="one gesture covers one"):
        check_sibling_set(primary, [_candidate(2, "W12"), _candidate(3, "W13")])


def test_the_whole_set_is_judged_before_anything_is_written():
    """A refused sibling refuses the gesture; there is no partial accept."""
    primary = _candidate(1, "W12")

    with pytest.raises(NotTheSameConflict) as refusal:
        check_sibling_set(primary, [_candidate(9, "W99"), _candidate(2, "W12")])

    # It names the offending row rather than failing at whichever it reached.
    assert "candidate 9" in str(refusal.value)
