"""The one versioned semantic source-class contract both paths interpret (#951).

The limited onboarding grant and the activated source authorization each carry a
set of source classes they permit.  #951's amendment makes both effective, and
makes them read the *same* interpretation of what a class is: a signed workbook
is a ``ucm_revision`` whether it arrived on the onboarding path or the activated
one, and a class the contract does not recognize is unrecognized to both.  What
differs between the paths is the permitted *set*, never the interpretation.

These are pure-Python facts, so nothing here touches the database.
"""

from __future__ import annotations

import pytest

from corridor import source_class_contract as contract


def test_the_contract_states_a_version_and_a_closed_set_of_classes():
    """A grant records the version it was interpreted under, so it is stated."""

    assert contract.CONTRACT_VERSION
    # The classes already named across the source-authorization corpus, and no
    # open-ended acceptance of whatever string a caller submits.
    for known in ("ucm_revision", "email", "minutes", "schedule_export", "matrix"):
        assert contract.is_recognized(known), known
    assert not contract.is_recognized("a-filename.xlsx")
    assert not contract.is_recognized("application/vnd.ms-excel")
    assert not contract.is_recognized("")


def test_the_interpretation_keeps_the_facets_separate():
    """Class, format, revision role, channel and processing policy are distinct.

    A filename, extension or MIME type names at most the format, and the
    contract never lets the format stand in for the class -- that is the whole
    point of interpreting the class rather than sniffing the bytes.
    """

    interpretation = contract.interpret("ucm_revision")
    assert interpretation is not None
    facets = {
        interpretation.source_format,
        interpretation.revision_role,
        interpretation.channel_suitability,
        interpretation.processing_policy,
    }
    # Four facets, each its own value -- not one label reused for the class.
    assert len(facets) == 4
    assert interpretation.source_class == "ucm_revision"
    assert contract.interpret("a-filename.xlsx") is None


def test_evaluate_shares_interpretation_and_separates_permission():
    """One function, one interpretation, and the permitted set is the argument.

    The onboarding path passes the grant's permitted classes; the activated
    path passes the binding's.  The recognition is identical; the yes/no is the
    caller's set.
    """

    permitted = ("email", "ucm_revision")

    ok = contract.evaluate(permitted, "ucm_revision")
    assert (ok.permitted, ok.recognized, ok.reason) == (True, True, "")

    prohibited = contract.evaluate(permitted, "matrix")
    assert prohibited.permitted is False
    assert prohibited.recognized is True
    assert prohibited.reason == contract.SOURCE_CLASS_NOT_PERMITTED

    unknown = contract.evaluate(permitted, "mystery")
    assert unknown.permitted is False
    assert unknown.recognized is False
    assert unknown.reason == contract.SOURCE_CLASS_UNRECOGNIZED

    undeclared = contract.evaluate(permitted, "")
    assert undeclared.permitted is False
    assert undeclared.reason == contract.SOURCE_CLASS_UNDECLARED


def test_an_empty_permitted_set_permits_no_class():
    """Fail closed: a binding that names no classes admits no semantic work."""

    decision = contract.evaluate((), "ucm_revision")
    assert decision.permitted is False
    assert decision.recognized is True
    assert decision.reason == contract.SOURCE_CLASS_NOT_PERMITTED


def test_the_decision_never_infers_a_class_from_a_filename_or_mime():
    """A format token is not a class, so it is unrecognized, not permitted."""

    for spelled in ("ucm.xlsx", "text/csv", ".xlsx", "UCM_REVISION"):
        decision = contract.evaluate(("ucm_revision",), spelled)
        assert decision.permitted is False, spelled
