"""The scratch-database sweep keeps what it does not recognise.

A cleanup command that guesses is worse than no cleanup command: the cost of
keeping one stale copy is a few megabytes, and the cost of dropping the wrong
one is a developer's working state.  These prove the four exclusions hold, and
that an unrecognised name is kept rather than swept.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# `scripts/` is a directory of commands, not an importable package, so the
# module is loaded by path rather than made one for a test's convenience.
_SPEC = importlib.util.spec_from_file_location(
    "clean_test_databases",
    Path(__file__).resolve().parents[1] / "scripts" / "clean_test_databases.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

is_scratch_name = _MODULE.is_scratch_name
sweepable = _MODULE.sweepable


PROTECTED = frozenset({"corridor"})


def test_a_recognised_scratch_database_with_no_backend_is_swept():
    swept = sweepable(
        {"corridor_m8_acceptance_0e5b0adcb1d54d45a164e63baa8f3841": 0},
        protected=PROTECTED,
    )

    assert swept == ["corridor_m8_acceptance_0e5b0adcb1d54d45a164e63baa8f3841"]


def test_the_configured_development_database_is_never_swept():
    """Whatever it is named: the protection is the configured name, not a literal."""

    assert sweepable({"corridor": 0}, protected=PROTECTED) == []
    assert sweepable({"corridor_dev": 0}, protected=frozenset({"corridor_dev"})) == []


def test_the_configured_database_is_protected_even_when_it_looks_like_scratch():
    """The case the allowlist cannot cover, and the one that actually happens.

    A verification run points `DATABASE_URL` at a freshly migrated copy — this
    repository did exactly that on 2026-09-03 with `corridor_timing` and
    `corridor_verify605`, both of which match a scratch pattern. While that is
    the configured database, the name protection is the only thing standing
    between the sweep and the database in use.
    """

    assert sweepable(
        {"corridor_timing": 0}, protected=frozenset({"corridor_timing"})
    ) == []
    assert sweepable({"corridor_timing": 0}, protected=PROTECTED) == [
        "corridor_timing"
    ]


def test_a_database_with_an_open_backend_is_never_swept():
    """An open connection means something is using it, including another lane."""

    assert sweepable({"corridor_issue147": 1}, protected=PROTECTED) == []


def test_postgres_and_the_templates_are_never_swept():
    candidates = {"postgres": 0, "template0": 0, "template1": 0}

    assert sweepable(candidates, protected=PROTECTED) == []


def test_an_unrecognised_name_is_kept_rather_than_guessed_at():
    """The sweep is an allowlist. A name nobody recognises is somebody's work."""

    candidates = {
        "corridor_pre_baseline_95da88f": 0,
        "customer_pilot_export": 0,
        "corridor_ui_verify_20260823": 0,
    }

    assert sweepable(candidates, protected=PROTECTED) == []


def test_the_patterns_match_the_harness_names_that_actually_occur():
    """Each of these was a real database on the development server."""

    for name in (
        "corridor_pytest_12345_ab12cd34_tmpl",
        "corridor_due_work_test_9081_0a1b2c3d4e5f",
        "corridor_event_admission_race_4b222685a5f14636a3f5f962cc3f6140",
        "corridor_issue249_check_6",
        "corridor_pr265_20260814d_113104",
        "corridor_tdd_251_1786679876069",
        "corridor_lane4_441",
        "corridor_consol_final",
        "corridor_224_full",
        "a222_statement_40c8f167f4bf4718984699e81d44f449",
        "issue339_predecessor_4609b62b1fc0498192a84ad0978ff713",
    ):
        assert is_scratch_name(name), name


def test_a_bare_prefix_is_not_a_scratch_name():
    """`corridor_lane` alone could be someone's deliberate copy; only a suffixed one is swept."""

    assert not is_scratch_name("corridor_consol")
    assert not is_scratch_name("corridor_baseline")
