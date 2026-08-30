"""The narrowed-set card only re-renders the matcher's recorded abstention.

The matcher (#370, ADR-0054) owns statement-to-Constraint matching.  These
tests pin the consumer contract: the card reader recognizes the matcher's
abstention card shape wherever the receipt nests it, refuses every malformed
or stale shape, and never manufactures or selects a scope.
"""

from corridor.statement_scope_match import (
    ScopeMatchCandidate,
    read_statement_scope_match_card,
    scope_match_card_data,
)


def _card(**overrides):
    card = {
        "candidate_dependency_ids": [41, 52],
        "matched_details": ["12-inch gas main", "Station 6608+70"],
        "evidence_applied": {"matched_terms": ["12-inch gas main"]},
        "choice_modes": ["each", "both_all_listed"],
    }
    card.update(overrides)
    return card


def _candidates():
    return (
        ScopeMatchCandidate(dependency_id=41, ref_code="PL41", title="12-inch gas main"),
        ScopeMatchCandidate(dependency_id=52, ref_code="PL52", title="12-inch gas main"),
    )


def test_finds_the_card_wherever_the_abstention_receipt_nests_it():
    receipt = {"input": {"candidate_id": 7}, "verdict": "ambiguous", "card": _card()}
    assert scope_match_card_data(receipt) == _card()
    assert scope_match_card_data({"outcomes": [{"detail": {"card": _card()}}]}) == _card()


def test_recognizes_nothing_without_the_full_matcher_shape():
    assert scope_match_card_data(None) is None
    assert scope_match_card_data({"verdict": "no_conflict_reference"}) is None
    # One survivor is the exact tier's business, never a narrowed-set card.
    assert scope_match_card_data({"card": _card(candidate_dependency_ids=[41])}) is None
    assert (
        scope_match_card_data({"card": _card(candidate_dependency_ids=[41, 41])})
        is None
    )
    assert scope_match_card_data({"card": _card(candidate_dependency_ids=[41, 0])}) is None
    assert scope_match_card_data({"card": _card(choice_modes=["each"])}) is None
    assert scope_match_card_data({"card": _card(evidence_applied="not-a-mapping")}) is None
    assert scope_match_card_data({"card": _card(matched_details=[41])}) is None


def test_binds_the_card_to_exactly_the_surviving_rows():
    card = read_statement_scope_match_card(_card(), _candidates())
    assert card is not None
    assert card.candidate_dependency_ids == (41, 52)
    assert tuple(item.ref_code for item in card.candidates) == ("PL41", "PL52")
    assert card.matched_details == ("12-inch gas main", "Station 6608+70")
    assert card.evidence_applied["matched_terms"] == ["12-inch gas main"]


def test_a_stale_card_renders_nothing_instead_of_guessing():
    # A surviving row that is no longer among the screen's active constraints
    # of this organization means the record moved on after the abstention.
    only_one_row = (_candidates()[0],)
    assert read_statement_scope_match_card(_card(), only_one_row) is None
    duplicated_rows = (_candidates()[0], _candidates()[0])
    assert read_statement_scope_match_card(_card(), duplicated_rows) is None
    assert read_statement_scope_match_card(_card(choice_modes=[]), _candidates()) is None
