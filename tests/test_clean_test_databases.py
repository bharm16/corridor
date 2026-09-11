"""The scratch-database sweep keeps what it does not recognise.

A cleanup command that guesses is worse than no cleanup command: the cost of
keeping one stale copy is a few megabytes, and the cost of dropping the wrong
one is a developer's working state.  These prove the four exclusions hold, and
that an unrecognised name is kept rather than swept.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

from corridor.m8_acceptance_database import disposable_database_name
from scripts.clean_test_databases import is_scratch_name, owner_pid, sweepable
from source_scan_support import python_files, read_python, source_scan_cache  # noqa: F401

_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PREFIX = "corridor_pytest_"
_LABEL_SHAPE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


PROTECTED = frozenset({"corridor"})


def NOTHING_RUNNING(pid: int) -> bool:
    """The process table a name-recognition test means: the minting run is gone.

    Without it these read the machine's real process table, and a name whose
    embedded pid happens to belong to some live process passes or fails by
    accident -- which is how this file first failed after the owner rule landed.
    """

    return False


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


# --- The minted namespace ------------------------------------------------
#
# Twenty-two hand-written patterns could not keep up with eight modules that
# each invented a prefix: `corridor_sh99_real_admission_acceptance_*`,
# `corridor_pipeline_shadow_*`, `corridor_native_matrix_measurement_*` and
# `corridor_migrated_template_*` were never listed at all, and
# `corridor_proving_restore_._.+` matched a single-digit pid only. The sweeper
# now asks the minting module, so these prove the two ends meet: every label
# the source declares mints a name the sweep collects, and the pytest harness's
# own scheme is still collected by the historical patterns that own it.


_NAMING_FUNCTIONS = frozenset({
    "disposable_database_name",
    "disposable_database_prefix",
    "provision_disposable_postgres",
    "reclaim_abandoned_database_copies",
    "upgrade_provisioned_postgres",
})


def _declared_labels() -> set[str]:
    """Every disposable-database label the source declares, read from the source.

    A registry inside the provisioner would list its own callers. Reading the
    labels back out of the call sites means a new workflow cannot be added
    without this test seeing it.
    """

    labels: set[str] = set()
    for path in (*python_files(_ROOT / "src"), *python_files(_ROOT / "tests")):
        source = read_python(path)
        constants = {
            target.id: node.value.value
            for node in source.tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in source.nodes:
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.attr if isinstance(function, ast.Attribute)
                else function.id if isinstance(function, ast.Name)
                else ""
            )
            if name not in _NAMING_FUNCTIONS:
                continue
            arguments = [
                keyword.value for keyword in node.keywords if keyword.arg == "label"
            ]
            if name in {"disposable_database_name", "disposable_database_prefix"}:
                arguments.extend(node.args[:1])
            for argument in arguments:
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ):
                    labels.add(argument.value)
                elif isinstance(argument, ast.Name) and argument.id in constants:
                    labels.add(constants[argument.id])
    return {label for label in labels if _LABEL_SHAPE.fullmatch(label)}


def test_every_declared_label_mints_a_name_the_sweep_collects():
    labels = _declared_labels()

    assert len(labels) >= 12, sorted(labels)
    for label in sorted(labels):
        name = disposable_database_name(label)

        assert len(name.encode()) <= 63, name
        assert is_scratch_name(name), name
        assert sweepable({name: 0}, protected=PROTECTED, live=NOTHING_RUNNING) == [
            name
        ]
        assert sweepable({name: 1}, protected=PROTECTED, live=NOTHING_RUNNING) == []


def test_the_pytest_harness_databases_are_still_collected():
    """The harness keeps its own scheme; the historical patterns still own it."""

    for name in (
        f"{HARNESS_PREFIX}20260909a_120000_gw0",
        f"{HARNESS_PREFIX}20260909a_120000_tmpl",
    ):
        assert is_scratch_name(name), name
        assert sweepable({name: 0}, protected=PROTECTED, live=NOTHING_RUNNING) == [
            name
        ]


def _running(*pids: int):
    """A process table naming exactly which minting runs are still alive."""

    return lambda pid: pid in pids


def test_a_live_runs_template_is_kept_even_with_no_backend_open():
    """The defect this rule exists for: a template is cloned from, never held open.

    A dry run on 2026-09-11 selected two concurrently running lanes' templates
    because nothing was connected to either at the instant it looked. Since #856
    the per-run template is what every isolated database clones, so dropping one
    mid-run costs that run every clone it has not taken yet, not one database.
    """

    template = f"{HARNESS_PREFIX}44670_7c6aa182_tmpl"

    assert sweepable({template: 0}, protected=PROTECTED, live=_running(44670)) == []
    assert sweepable({template: 0}, protected=PROTECTED, live=_running(1)) == [template]


def test_a_dead_owner_leaves_its_databases_eligible():
    """The sweep still does its job; abandoned runs are what it collects."""

    names = {
        f"{HARNESS_PREFIX}44670_7c6aa182_tmpl": 0,
        f"{HARNESS_PREFIX}44670_7c6aa182_gw0": 0,
        disposable_database_name("baseline_activation"): 0,
    }

    assert sorted(sweepable(names, protected=PROTECTED, live=_running())) == sorted(
        names
    )


def test_an_owner_this_process_cannot_ask_about_is_kept():
    """Unverifiable ownership is kept, and the asymmetry is the point.

    Keeping a dead run's database costs one stale database until the next sweep.
    Dropping a live run's template costs that run the rest of its session.
    """

    def refuses(pid: int) -> bool:
        raise PermissionError(pid)

    template = f"{HARNESS_PREFIX}44670_7c6aa182_tmpl"

    assert sweepable({template: 0}, protected=PROTECTED, live=refuses) == []


def test_a_name_carrying_no_minting_process_falls_back_to_the_other_rules():
    """A hand-made verification copy has no minting module, and is still swept."""

    # A scratch name from before the minting namespace existed: recognised by the
    # historical patterns, carrying no pid for the owner rule to read.
    hand_made = "corridor_m8_acceptance_0e5b0adcb1d54d45a164e63baa8f3841"

    assert owner_pid(hand_made) is None
    assert sweepable({hand_made: 0}, protected=PROTECTED, live=_running()) == [hand_made]
    # And an unrecognised name is still kept, by the rule that already did that.
    assert sweepable(
        {"corridor_pre_baseline_95da88f": 0}, protected=PROTECTED, live=_running()
    ) == []


def test_the_owner_is_read_from_the_name_by_whichever_module_minted_it():
    """One pid reader per family, neither of them written twice."""

    assert owner_pid(f"{HARNESS_PREFIX}44670_7c6aa182_tmpl") == 44670
    assert owner_pid(f"{HARNESS_PREFIX}44670_7c6aa182") == 44670
    assert owner_pid("corridor_disposable_baseline_activation_18413_7c6aa1826a05") == 18413
    assert owner_pid("corridor") is None
