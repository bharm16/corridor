"""The one comparison every Propose Delta producer runs, on plain mappings.

These tests hold the comparison itself, with no workbook, no export, no model
and no database: the accepted record is a mapping and what a source states is a
mapping, which is exactly the pair the five producers used to compare in five
copies of the same branch.
"""

from __future__ import annotations

import pytest

from corridor.proposed_delta_comparison import (
    AMBIGUITY_BEFORE_SEAL,
    ENTIRE_SUBJECT,
    ComparisonRefused,
    StatedSubject,
    SEAL_BEFORE_ROW_FAILURE,
    compare_stated_subjects,
    removal_disposition,
    revision_label,
)
from corridor.proposed_deltas import ExistingSubjectTarget, ProposedSubjectTarget


RULE = "test-comparison-v1"
LABEL = "revision:7"


def compare(accepted, stated, **kwargs):
    return compare_stated_subjects(
        accepted=accepted,
        stated=stated,
        comparison_rule_version=RULE,
        accepted_baseline_revision=LABEL,
        **kwargs,
    )


def test_a_stated_value_the_record_already_holds_proposes_nothing():
    result = compare(
        {("UC-1", "size"): "12 in"},
        (StatedSubject("UC-1", (("size", "12 in"),)),),
    )

    assert result.deltas == ()
    assert result.values_agreed == 1
    assert result.restated_rows == frozenset()


def test_a_different_stated_value_is_one_modify_carrying_both_sides():
    result = compare(
        {("UC-1", "size"): "12 in"},
        (StatedSubject("UC-1", (("size", "18 in"),)),),
    )

    (delta,) = result.deltas
    assert delta.change_type == "modify"
    assert delta.target == ExistingSubjectTarget("UC-1", "size")
    assert delta.accepted_value == "12 in"
    assert delta.proposed_value == "18 in"
    assert delta.comparison_rule_version == RULE
    assert delta.accepted_baseline_revision == LABEL
    assert result.values_agreed == 0


def test_a_field_the_accepted_subject_does_not_hold_is_an_add():
    result = compare(
        {("UC-1", "size"): "12 in"},
        (StatedSubject("UC-1", (("material", "HDPE"),)),),
    )

    (delta,) = result.deltas
    assert delta.change_type == "add"
    assert delta.target == ExistingSubjectTarget("UC-1", "material")
    assert delta.accepted_value is None
    assert delta.proposed_value == "HDPE"


def test_an_unpaired_subject_is_one_delta_carrying_its_initial_fields():
    """ADR-0082: a new subject is one atomic change, not one delta per field."""

    result = compare(
        {("UC-1", "size"): "12 in"},
        (
            StatedSubject(
                "UC-9",
                (("size", "4 in"), ("material", "PVC")),
                paired=False,
            ),
        ),
    )

    (delta,) = result.deltas
    assert delta.change_type == "add"
    assert delta.target == ProposedSubjectTarget("UC-9", ("size", "material"))
    assert delta.accepted_value is None
    assert delta.proposed_value == {"size": "4 in", "material": "PVC"}


def test_the_deltas_of_one_comparison_keep_the_order_they_were_stated_in():
    result = compare(
        {("UC-1", "size"): "12 in", ("UC-2", "size"): "6 in"},
        (
            StatedSubject("UC-1", (("size", "18 in"),)),
            StatedSubject("UC-9", (("size", "4 in"),), paired=False),
            StatedSubject("UC-2", (("size", "8 in"),)),
        ),
        removals=("UC-3",),
        sealed=True,
    )

    assert [
        (delta.change_type, delta.target.subject_identity) for delta in result.deltas
    ] == [
        ("modify", "UC-1"),
        ("add", "UC-9"),
        ("modify", "UC-2"),
        ("apparent_removal", "UC-3"),
    ]


def test_a_sealed_complete_source_proposes_the_absent_subject_for_removal():
    result = compare(
        {
            ("UC-3", "utility_id"): "UC-3",
            ("UC-3", "size"): "6 in",
            ("UC-1", "size"): "12 in",
        },
        (),
        removals=("UC-3",),
        sealed=True,
    )

    (delta,) = result.deltas
    assert delta.change_type == "apparent_removal"
    assert delta.target == ExistingSubjectTarget("UC-3", ENTIRE_SUBJECT)
    # The whole accepted subject, and only that subject, is what would go.
    assert delta.accepted_value == {"size": "6 in", "utility_id": "UC-3"}
    assert delta.proposed_value is None


def test_a_removal_without_the_seal_is_refused_rather_than_proposed():
    with pytest.raises(ComparisonRefused, match="complete"):
        compare(
            {("UC-3", "size"): "6 in"},
            (),
            removals=("UC-3",),
        )


def test_an_ambiguous_row_restating_a_whole_accepted_subject_says_nothing_new():
    """Two accepted subjects share one conflict number and cannot be told apart."""

    result = compare(
        {
            ("Sheet!3", "utility_id"): "UC-1",
            ("Sheet!3", "size"): "2 in",
            ("Sheet!4", "utility_id"): "UC-1",
            ("Sheet!4", "size"): "12 in",
        },
        (
            StatedSubject(
                "UC-1#2",
                (("utility_id", "UC-1"), ("size", "12 in")),
                paired=False,
                row_key="Sheet!6",
                unresolved_accepted_subjects=("Sheet!3", "Sheet!4"),
            ),
        ),
    )

    assert result.deltas == ()
    assert result.values_agreed == 2
    assert result.restated_rows == frozenset({"Sheet!6"})


def test_an_ambiguous_row_that_differs_reaches_its_own_focused_item():
    result = compare(
        {
            ("Sheet!3", "utility_id"): "UC-1",
            ("Sheet!3", "size"): "2 in",
            ("Sheet!4", "utility_id"): "UC-1",
            ("Sheet!4", "size"): "12 in",
        },
        (
            StatedSubject(
                "UC-1#2",
                (("utility_id", "UC-1"), ("size", "3 in")),
                paired=False,
                row_key="Sheet!6",
                unresolved_accepted_subjects=("Sheet!3", "Sheet!4"),
            ),
        ),
    )

    (delta,) = result.deltas
    assert delta.target == ProposedSubjectTarget("UC-1#2", ("utility_id", "size"))
    assert result.values_agreed == 0
    assert result.restated_rows == frozenset()


def test_a_partial_row_stating_only_some_accepted_values_is_not_a_restatement():
    """A subset is not the same row: `stands accepted` is asked of the whole row."""

    result = compare(
        {("Sheet!3", "utility_id"): "UC-1", ("Sheet!3", "size"): "2 in"},
        (
            StatedSubject(
                "UC-1#2",
                (("utility_id", "UC-1"),),
                paired=False,
                row_key="Sheet!6",
                unresolved_accepted_subjects=("Sheet!3",),
            ),
        ),
    )

    assert [delta.change_type for delta in result.deltas] == ["add"]
    assert result.restated_rows == frozenset()


def test_an_unsealed_source_withholds_every_removal_it_is_asked_about():
    proposed, withheld = removal_disposition(
        sealed=False,
        unsealed_reason="not_sealed",
        blocking_reason=None,
        precedence=SEAL_BEFORE_ROW_FAILURE,
    )

    assert (proposed, withheld) == (False, "not_sealed")


def test_a_sealed_source_with_nothing_blocking_proposes_the_removal():
    for precedence in (SEAL_BEFORE_ROW_FAILURE, AMBIGUITY_BEFORE_SEAL):
        assert removal_disposition(
            sealed=True,
            unsealed_reason="not_sealed",
            blocking_reason=None,
            precedence=precedence,
        ) == (True, None)


def test_the_two_producers_report_a_withholding_in_their_own_precedence():
    """The one place the two readers genuinely differ, named rather than copied.

    An ambiguous conflict number is a property of the accepted record itself:
    nobody can say *which* of two identically numbered facilities went, sealed
    or not, so `later_revision` reports the ambiguity even for a partial
    revision.  A row that failed to process is a property of this one delivery,
    and `key_date_table` reports the unsealed export first, because a filtered
    export withholds every removal for one reason before any row is consulted.
    """

    assert removal_disposition(
        sealed=False,
        unsealed_reason="unsealed",
        blocking_reason="ambiguous",
        precedence=AMBIGUITY_BEFORE_SEAL,
    ) == (False, "ambiguous")
    assert removal_disposition(
        sealed=False,
        unsealed_reason="unsealed",
        blocking_reason="row_failed",
        precedence=SEAL_BEFORE_ROW_FAILURE,
    ) == (False, "unsealed")
    assert removal_disposition(
        sealed=True,
        unsealed_reason="unsealed",
        blocking_reason="row_failed",
        precedence=SEAL_BEFORE_ROW_FAILURE,
    ) == (False, "row_failed")


def test_an_unknown_precedence_is_refused_rather_than_defaulted():
    with pytest.raises(ComparisonRefused):
        removal_disposition(
            sealed=False,
            unsealed_reason="unsealed",
            blocking_reason="ambiguous",
            precedence="whichever_reads_better",
        )


def test_the_accepted_baseline_a_comparison_ran_against_is_named_or_absent():
    assert revision_label(7) == "revision:7"
    assert revision_label(None) is None
