"""Repository test gates run in the smallest CI scope that proves the change.

The gate is one workflow, `.github/workflows/release-gate.yml`, because a
required status check has to report on every pull request or it blocks that
pull request forever. A workflow skipped by a path filter on its *trigger*
never reports; a job skipped by an `if` reports `skipped`, which the
`release-gate` summary job reads through `needs.<job>.result` and matches
against what `scripts/classify_ci_change.py` asked for (ADR-0093, #697).
"""

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
import yaml

from corridor.render_profiles import DEFAULT_WORKER_PROJECT
from makefile_support import recipe as make_recipe, targets as make_targets
from scripts import gate_results, run_test_gate
from scripts.test_gate.broad_run import DIAGNOSTIC_ENV
from scripts.test_gate.partition import CHECK_OWNED_FILES


ROOT = Path(__file__).resolve().parents[1]
GATE = "release-gate.yml"
# Eleven test runners plus check and migration. Keep the capacity trial explicit;
# the required timing receipts assess it against the unchanged ADR-0096 budget.
DOWNLOADING_JOB_COUNT = 13


def _workflow(name: str) -> dict:
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(),
        Loader=yaml.BaseLoader,
    )


def _workflow_names() -> tuple[str, ...]:
    return tuple(
        sorted(path.name for path in (ROOT / ".github" / "workflows").glob("*.yml"))
    )


def _run_commands(workflow: dict) -> tuple[str, ...]:
    return tuple(
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "run" in step
    )


def _shell_statements(script: Path) -> tuple[str, ...]:
    """Every executable line of a shell script, whole-line comments removed."""
    return tuple(
        stripped
        for line in script.read_text().splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    )


def _job(name: str, workflow: str = GATE) -> dict:
    return _workflow(workflow)["jobs"][name]


def _shard_count(job: str = "pytest") -> int:
    return len(_job(job)["strategy"]["matrix"]["shard"])


# The two steps that apply the fail-closed rule, the job each lives in, and
# the environment `scripts/gate_results.py` reads for it.
MATCHERS = {
    "summary": (
        "release-gate",
        "Match every job result against the classifier",
        gate_results.SUMMARY_ENVIRONMENT,
    ),
    "infrastructure": (
        "check",
        "Match infrastructure results against the classifier",
        gate_results.INFRASTRUCTURE_ENVIRONMENT,
    ),
}


def _matcher_step(surface: str) -> dict:
    job, name, _ = MATCHERS[surface]
    return next(step for step in _job(job)["steps"] if step.get("name") == name)


def test_only_the_gate_reports_on_a_pull_request():
    """A second pull-request workflow is how the pending-check problem returns.

    Any workflow whose `pull_request` trigger is path-filtered leaves a check
    pending on the pull requests it skips, so it can never be required and
    the gate it holds can never be proven.

    The rule is about *pull-request* workflows, which is what the failure mode
    needs. A workflow that only answers `workflow_dispatch` or `schedule`
    never reports on a pull request and cannot leave one pending, so manual
    deployment workflows are allowed alongside the two -- and are held to
    carrying no `pull_request` trigger at all, which is stricter than being
    merely unfiltered.
    """

    assert GATE in _workflow_names()
    assert "full-suite.yml" in _workflow_names()

    reporting = tuple(
        name for name in _workflow_names() if "pull_request" in _workflow(name)["on"]
    )
    assert reporting == (GATE,), (
        f"{reporting} report on a pull request; only {GATE} may"
    )


def test_the_required_gate_is_triggered_on_every_pull_request():
    workflow = _workflow(GATE)

    assert set(workflow["on"]) == {"pull_request"}
    assert not workflow["on"]["pull_request"], (
        "the required workflow carries trigger-level path scoping; a run it "
        "skips leaves `release-gate` pending and blocks the pull request"
    )


def test_no_workflow_filters_a_pull_request_trigger_by_path():
    for name in _workflow_names():
        trigger = _workflow(name)["on"].get("pull_request")
        if trigger is None:
            continue
        assert not trigger, f"{name} scopes its pull_request trigger: {trigger}"


def test_the_scheduled_suite_never_reports_on_a_pull_request():
    workflow = _workflow("full-suite.yml")

    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert "make test-full" in _run_commands(workflow)


def test_the_gate_holds_every_job_the_summary_needs():
    assert set(_workflow(GATE)["jobs"]) == {
        "classify",
        "check",
        "pytest",
        "slow",
        "migration",
        "release-gate",
    }


def test_each_behavior_gate_runs_exactly_once_across_the_gate():
    commands = _run_commands(_workflow(GATE))

    # `check` owns `make check`; no behavior job repeats it.
    assert commands.count("make check") == 1
    # The suites are sharded across runners, so each appears once as a matrix
    # step rather than as one whole-suite command (#548).
    assert commands.count("make test") == 0
    assert sum("make test-shard" in command for command in commands) == 1
    assert commands.count("make test-slow") == 0
    assert sum("make test-slow-shard" in command for command in commands) == 1
    assert commands.count("make test-migrations") == 1
    # The complete suite is the scheduled gate, never the pull-request one.
    assert "make test-full" not in commands


def test_check_runs_unconditionally_and_without_waiting_for_the_classifier():
    """`make check` owns the only tests that read documentation.

    It also keeps the job name `check`, which is the context the ruleset
    requires today; renaming it would leave that required context pending on
    every pull request during the transition (ADR-0093).
    """

    check = _job("check")

    assert "if" not in check, "the check job became conditional"
    assert "needs" not in check, "check waits for the classifier for no reason"
    assert "make check" in tuple(
        step["run"] for step in check["steps"] if "run" in step
    )


def test_cdk_runs_only_for_infrastructure_inputs_inside_the_independent_check_job():
    definition = _job("check")
    assert "if" not in definition and "needs" not in definition
    steps = definition["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == "0"
    classify = next(step for step in steps if step.get("id") == "infrastructure")
    assert classify["run"] == (
        'python3 scripts/classify_ci_change.py --base "$BASE_SHA" --head "$HEAD_SHA"'
    )
    assert classify["env"] == {
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    infra_ids = {"infra_node", "infra_install", "infra_assertions", "infra_synth"}
    infra_steps = [step for step in steps if step.get("id") in infra_ids]
    assert {step["id"] for step in infra_steps} == infra_ids
    assert all(
        step["if"] == "${{ steps.infrastructure.outputs.infrastructure_required == 'true' }}"
        and "continue-on-error" not in step
        for step in infra_steps
    )
    assert all(steps.index(classify) < steps.index(step) for step in infra_steps)
    assertions = next(step for step in infra_steps if step["id"] == "infra_assertions")
    assert assertions["run"] == (
        "uv run python -m pytest tests --ignore=tests/test_workflow_ordering.py -q"
    )
    # The runbook and workflow assertions stay in the unconditional source
    # check, and are excluded from the optional CDK project to run once.
    assert "infra/tests/test_workflow_ordering.py" in make_recipe("check")
    for step in steps:
        if step.get("working-directory") == "infra" or "setup-node" in step.get("uses", ""):
            assert step.get("id") in infra_ids


def test_the_infrastructure_matcher_judges_every_outcome_the_cdk_steps_produce():
    """`always()` is what lets a failed or skipped CDK step reach the matcher.

    The rule it then applies is proven once, for both matcher surfaces, in
    the fail-closed section at the end of this file.
    """

    step = _matcher_step("infrastructure")
    assert step["if"] == "${{ always() }}"
    assert step["env"] == {
        "INFRASTRUCTURE_REQUIRED": "${{ steps.infrastructure.outputs.infrastructure_required }}",
        "NODE_RESULT": "${{ steps.infra_node.outcome }}",
        "INSTALL_RESULT": "${{ steps.infra_install.outcome }}",
        "ASSERTIONS_RESULT": "${{ steps.infra_assertions.outcome }}",
        "SYNTH_RESULT": "${{ steps.infra_synth.outcome }}",
    }
    # Every conditional step the job runs is one the matcher reads a result
    # for, so a CDK step added to the job cannot land outside its judgement.
    conditional = {
        step["id"]
        for step in _job("check")["steps"]
        if step.get("if") == (
            "${{ steps.infrastructure.outputs.infrastructure_required == 'true' }}"
        )
    }
    assert {label for label, _ in gate_results.INFRASTRUCTURE_STEPS} == conditional


def test_the_expensive_jobs_are_skipped_by_a_condition_not_by_a_path_filter():
    """The difference the whole design rests on.

    An `if` that is false yields `skipped`, a result the summary inspects. A
    path filter on the trigger yields no check run at all.
    """

    for job in ("pytest", "slow"):
        assert (
            _job(job)["if"]
            == "${{ needs.classify.outputs.behavior_required == 'true' }}"
        ), job
        assert _job(job)["needs"] == "classify"

    assert (
        _job("migration")["if"]
        == "${{ needs.classify.outputs.migration_required == 'true' }}"
    )
    assert _job("migration")["needs"] == "classify"


def test_the_classifier_is_the_in_repository_script_reading_full_history():
    classify = _job("classify")
    checkout = next(
        step for step in classify["steps"] if step.get("uses", "").startswith(
            "actions/checkout"
        )
    )

    # base...head needs the merge base in the clone.
    assert checkout["with"]["fetch-depth"] == "0"
    step = next(step for step in classify["steps"] if "run" in step)
    assert "scripts/classify_ci_change.py" in step["run"]
    # The SHAs arrive through env, never interpolated into the shell body.
    assert "${{" not in step["run"]
    assert set(step["env"]) == {"BASE_SHA", "HEAD_SHA"}
    assert set(classify["outputs"]) == {
        "behavior_required", "migration_required", "timing_weights"
    }


def test_the_added_jobs_download_no_packages():
    """Package concurrency is explicit, so it may not grow unnoticed.

    `classify` and `release-gate` are new, but they run alone at either end
    of the gate and install nothing. The 2026-09-09 capacity trial adds one
    ordinary runner to the prior twelve downloading jobs (ADR-0096).
    """

    jobs = _workflow(GATE)["jobs"]
    downloading = 0
    for name, definition in jobs.items():
        steps = definition["steps"]
        installs = any(
            "setup-uv" in step.get("uses", "")
            or "ci_environment" in step.get("run", "")
            or step.get("run", "").startswith("uv ")
            for step in steps
        )
        if name in {"classify", "release-gate"}:
            assert not installs, f"{name} adds a concurrent package download"
            continue
        if installs:
            matrix = definition.get("strategy", {}).get("matrix", {})
            downloading += len(matrix.get("shard", ["1"]))

    assert downloading == DOWNLOADING_JOB_COUNT


def test_every_gate_job_runs_the_one_concurrent_setup_step():
    """Per-job setup is one step because the gate takes a max, not a mean.

    The wall clock is the slowest required job, so each run samples the
    worst setup draw taken in it. Over the twelve pull-request runs after the
    five-way split, the job that decided the wall clock spent a median of 60s
    outside its test command against a fleet-wide per-job median of 38s — it
    was the job whose downloads were slow, not the one holding the most
    tests. Serial setup steps add their draws; scripts/ci_environment.sh runs
    the independent ones concurrently so only the largest counts (#595).
    """

    database_jobs = {
        GATE: ("pytest", "slow", "migration"),
        "full-suite.yml": ("full-suite",),
    }
    for name, jobs in database_jobs.items():
        workflow = _workflow(name)
        for job in jobs:
            definition = workflow["jobs"][job]
            commands = tuple(
                step["run"] for step in definition["steps"] if "run" in step
            )
            assert "scripts/ci_environment.sh" in commands, (
                f"{name}:{job} does not run the shared setup script"
            )
            # A second setup step would be a serial draw again.
            assert sum(
                command.startswith(("uv sync", "sudo apt-get", "uv run alembic"))
                for command in commands
            ) == 0, f"{name}:{job} sets up outside the concurrent step"
            # A `services:` container is pulled and health-checked before the
            # first step, so none of its cost can overlap with anything.
            assert "services" not in definition, (
                f"{name}:{job} pays for a service container before it starts"
            )


def test_the_gate_defers_empty_shared_state_to_the_coordinated_harness():
    """The executable fixture guard below still prevents #595 UndefinedTable.

    A marked empty CI source uses a clone of the already migrated template;
    another full schema in each job's setup is no longer necessary.
    """
    setup = (ROOT / "scripts" / "ci_environment.sh").read_text()
    assert "alembic upgrade head" not in setup
    assert "CORRIDOR_CI_EMPTY_SHARED_SOURCE=1" in setup
    assert setup.index('wait "$postgres_job"') < setup.index("CORRIDOR_CI_EMPTY_SHARED_SOURCE=1")


def test_the_setup_script_provisions_exactly_what_a_test_job_may_assume():
    """Nothing may download packages inside a test, so the script must say so.

    `scripts/ci_environment.sh` is the interface every test job depends on:
    what it leaves provisioned is what a test may assume already exists.
    `workers/render` is a separate uv project whose opencv-python-headless is
    never in the root lock, so before #548 the first page render built that
    environment over the network, inside pytest, in four racing xdist workers,
    with its output captured. Deleting that line again costs a slow job rather
    than a red one, so the ordered statement list is asserted here the way
    `tests/test_ci_cache_warming.py` asserts the warmer's step list: a
    provisioning step may not appear, move or disappear unnoticed.
    """

    statements = _shell_statements(ROOT / "scripts" / "ci_environment.sh")
    assert statements == (
        "set -euo pipefail",
        'cd "$(dirname "${BASH_SOURCE[0]}")/.."',
        'logs="${RUNNER_TEMP:-/tmp}"',
        'scripts/ci_postgres.sh >"$logs/postgres.log" 2>&1 &',
        "postgres_job=$!",
        "uv sync --locked",
        "uv sync --project workers/render --frozen",
        'wait "$postgres_job" || { cat "$logs/postgres.log" >&2; exit 1; }',
        'cat "$logs/postgres.log"',
        'if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then',
        "printf 'CORRIDOR_CI_EMPTY_SHARED_SOURCE=1\\n' >> \"$GITHUB_ENV\"",
        "fi",
    )
    # The provisioned project is the one the runtime caller actually runs, so
    # moving the worker cannot leave the sync pointing at the old path.
    project = DEFAULT_WORKER_PROJECT.relative_to(ROOT).as_posix()
    assert f"uv sync --project {project} --frozen" in statements


def test_the_shared_state_fixture_reaches_a_migrated_database(
    shared_source_database_url,
):
    """The shared fixture must supply a migrated schema before corpus queries.

    Preserve #595's executable proof without rebuilding the template schema
    in the configured empty CI database as well.
    """

    engine = create_engine(shared_source_database_url)
    try:
        with engine.connect() as connection:
            present = connection.execute(
                text("select to_regclass('public.projects')")
            ).scalar()
    finally:
        engine.dispose()

    assert present is not None, (
        "the shared-state fixture carries no schema, so the corpus "
        "tests will raise UndefinedTable instead of skipping"
    )


def test_every_test_file_lands_in_exactly_one_shard(monkeypatch, tmp_path):
    """Sharding must cover the suite, less what check owns, or the gate proves less.

    The risk of splitting a suite across runners is a file that falls in no
    shard: the gate stays green while nothing runs it. The two check-owned
    files are the one deliberate omission, because the `check` job runs them
    and each required proof runs once.
    """

    expected = {
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")
    } - set(CHECK_OWNED_FILES)

    # Partition through the gate's own command, on the recorded bootstrap
    # weights, so the cover is the one CI computes rather than one this test
    # rebuilds. Each gate balances on its own recorded seconds and carries its
    # own runner count, so each has its own partition.
    monkeypatch.delenv("CORRIDOR_CI_WEIGHTS", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    for job in ("pytest", "slow"):
        count = _shard_count(job)
        assigned = [
            name
            for number in range(1, count + 1)
            for name in run_test_gate.partition(job, count, number, tmp_path)
        ]

        assert len(assigned) == len(set(assigned)), f"{job}: a file landed twice"
        assert set(assigned) == expected, (
            f"{job}: shards do not cover the suite: "
            f"{sorted(expected.symmetric_difference(assigned))}"
        )


def test_each_gate_asks_for_the_shard_count_its_matrix_runs():
    """A `SHARDS=` that disagrees with the matrix drops or repeats files.

    The two gates carry different runner counts — the non-slow work divides
    across 171 files, the slow gate's floor is one file — so the count is read
    from each job rather than assumed equal (#548).
    """

    for job, target in (("pytest", "test-shard"), ("slow", "test-slow-shard")):
        shards = _shard_count(job)
        command = next(
            step["run"]
            for step in _job(job)["steps"]
            if "run" in step and f"make {target} " in step["run"]
        )
        assert f"SHARDS={shards}" in command, (
            f"{job}: matrix runs {shards} shards but the command says {command!r}"
        )


def test_each_gate_uses_the_scheduler_appropriate_to_its_fixture_cost():
    """Ordinary tests rebalance; slow module fixtures are built only once."""
    for target in ("test", "test-full", "test-timing"):
        assert "--dist worksteal" in make_recipe(target)
    for target in ("test-slow", "test-slow-timing"):
        assert "--dist loadfile" in make_recipe(target)
    for target, suite in (("test-shard", "pytest"), ("test-slow-shard", "slow")):
        recipe = make_recipe(target)
        assert "scripts/run_test_gate.py" in recipe
        assert f"--suite {suite}" in recipe
        assert "--shards $(SHARDS) --shard $(SHARD) --workers $(TEST_WORKERS)" in recipe
    assert (
        "scripts/run_test_gate.py --suite migration --shards 1 --shard 1 --workers 2"
        in make_recipe("test-migrations")
    )


def test_no_make_target_writes_its_own_broad_local_authorization():
    """A target that exports the reason runs the suite outside every control.

    `make test-timing` used to set the diagnostic environment variable itself
    and call pytest directly over the whole non-slow suite, so the collector's
    guard was pre-satisfied and the run had no timeout, no process-group
    cleanup and no `out/test-results` receipt. The authorization is the
    wrapper's `--diagnostic-reason`, which is also what writes that receipt.
    """
    for target in make_targets().values():
        assert DIAGNOSTIC_ENV not in target.recipe_text, (
            f"make {target.name} authorizes its own broad local run"
        )
    for target in ("test-timing", "test-slow-timing"):
        recipe = make_recipe(target)
        assert "scripts/run_local_tests.py" in recipe, f"make {target} skips the wrapper"
        assert "--diagnostic-reason performance-investigation" in recipe
        # The wrapper prepends -x unless a failure limit is stated, and a
        # timing pass that stops at the first failure measures a fraction of
        # the suite it was asked to measure.
        assert "--maxfail=0" in recipe


def test_ci_worker_count_matches_the_private_runner_capacity():
    workflow = _workflow(GATE)
    assert workflow["env"]["TEST_WORKERS"] == "2"
    for name in ("pytest", "slow", "migration"):
        assert "TEST_WORKERS" not in _job(name).get("env", {})
    assert _job("full-suite", "full-suite.yml")["env"]["TEST_WORKERS"] == "2"


def test_check_owns_its_source_checks_once_in_the_required_gate():
    """The same checks must not execute in check and a behavior shard."""
    expected = {"tests/test_architecture.py", "tests/test_source_scan_support.py"}
    assert set(CHECK_OWNED_FILES) == expected
    recipe = make_recipe("check")
    commands = [line.strip() for line in recipe.splitlines() if "pytest " in line]
    assert len(commands) == 1
    assert set(commands[0].split()[3:-1]) == expected | {
        "infra/tests/test_workflow_ordering.py"
    }
    assert commands[0].endswith(" -q")


def test_every_test_job_consumes_the_same_timing_output_and_ends_with_its_test_command():
    """Metadata transport must not fail a completed test job or alter its partition."""
    commands = {
        "pytest": "make test-shard SHARDS=8 SHARD=${{ matrix.shard }}",
        "slow": "make test-slow-shard SHARDS=3 SHARD=${{ matrix.shard }}",
        "migration": "make test-migrations",
    }
    for suite, command in commands.items():
        job = _job(suite)
        assert job["env"]["CORRIDOR_CI_WEIGHTS"] == (
            "${{ needs.classify.outputs.timing_weights }}"
        )
        assert job["steps"][-1] == {"id": "measure", "run": command}
    for job in _workflow(GATE)["jobs"].values():
        assert all(
            "actions/upload-artifact" not in step.get("uses", "")
            and "actions/download-artifact" not in step.get("uses", "")
            for step in job["steps"]
        ), "artifact transport can fail after tests already passed"


def test_every_matrix_shard_has_a_unique_receipt_output_read_by_the_summary():
    """Each matrix child writes its own key; the summary receives all keys."""
    summary = _job("release-gate")["steps"][-1]
    seen = set()
    for suite in ("pytest", "slow", "migration"):
        job = _job(suite)
        count = 1 if suite == "migration" else _shard_count(suite)
        slots = {f"{suite}_{number}" for number in range(1, count + 1)}
        assert not seen.intersection(slots)
        seen.update(slots)
        assert job["outputs"] == {
            slot: "${{ steps.measure.outputs." + slot + " }}" for slot in slots
        }
        assert len([step for step in job["steps"] if step.get("id") == "measure"]) == 1
        assert summary["env"][f"CORRIDOR_{suite.upper()}_RECEIPTS"] == (
            "${{ toJSON(needs." + suite + ".outputs) }}"
        )


def test_the_classifier_shares_one_validated_timing_output():
    job = _job("classify")
    steps = job["steps"]
    prepare = next(step for step in steps if step.get("id") == "timing")
    assert prepare["if"] == "${{ steps.classify.outputs.behavior_required == 'true' }}"
    assert prepare["run"] == "python3 scripts/ci_feedback.py prepare"
    assert prepare["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert job["outputs"]["timing_weights"] == "${{ steps.timing.outputs.timing_weights }}"
    classifier = next(step for step in steps if step.get("id") == "classify")
    assert steps.index(classifier) < steps.index(prepare)
    assert _workflow(GATE)["permissions"] == {"actions": "read", "contents": "read"}


def test_current_evidence_validation_extends_the_fail_closed_summary():
    """The summary's own step order, which the matcher's move constrains.

    The matcher is an in-repository module now, so the checkout runs first
    and unconditionally -- the repository has to be on disk before the
    required rule executes. Evidence validation still runs last, and only
    when the matcher has already passed on a behavior pull request.
    """

    steps = _job("release-gate")["steps"]
    assert len(steps) == 3
    checkout, matcher, finish = steps
    assert checkout == {"uses": "actions/checkout@v4"}
    assert matcher == _summary_step()
    assert "if" not in matcher, "the required matcher became conditional"
    assert finish["if"] == (
        "${{ success() && needs.classify.outputs.behavior_required == 'true' }}"
    )
    assert finish["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "CORRIDOR_PYTEST_RECEIPTS": "${{ toJSON(needs.pytest.outputs) }}",
        "CORRIDOR_SLOW_RECEIPTS": "${{ toJSON(needs.slow.outputs) }}",
        "CORRIDOR_MIGRATION_RECEIPTS": "${{ toJSON(needs.migration.outputs) }}",
        "PYTEST_SHARDS": str(_shard_count("pytest")),
        "SLOW_SHARDS": str(_shard_count("slow")),
        "MIGRATION_REQUIRED": "${{ needs.classify.outputs.migration_required }}",
    }
    assert finish["run"] == "python3 scripts/ci_feedback.py finish"
    assert finish["name"] == "Verify test evidence and report timing targets"
    assert "continue-on-error" not in finish


def test_pr_workflows_cancel_obsolete_revisions():
    concurrency = _workflow(GATE)["concurrency"]

    assert concurrency["cancel-in-progress"] == "true"
    assert "github.event.pull_request.number" in concurrency["group"]


# --- the fail-closed matcher --------------------------------------------------
#
# One rule decides the required status: a job the classifier said was required
# must report `success`, a job it said was not required must report `skipped`,
# and an answer that is neither `true` nor `false` fails the gate rather than
# picking a default. It used to be two hand-written bash blocks in the
# workflow, each re-executed here under `bash -c`; `scripts/gate_results.py`
# now holds it once and both matcher steps call it, so these cases exercise
# the function CI decides with.


def _summary_step() -> dict:
    return next(
        step for step in _job("release-gate")["steps"] if "run" in step
    )


def test_the_summary_always_runs_and_needs_every_gate_job():
    definition = _job("release-gate")

    assert definition["if"] == "${{ always() }}"
    assert set(definition["needs"]) == {
        "classify",
        "check",
        "pytest",
        "slow",
        "migration",
    }


def test_the_summary_reads_every_result_through_needs():
    """`needs.<job>.result` is unambiguous; polling the API is not."""

    assert _summary_step()["env"] == {
        "BEHAVIOR_REQUIRED": "${{ needs.classify.outputs.behavior_required }}",
        "MIGRATION_REQUIRED": "${{ needs.classify.outputs.migration_required }}",
        "CLASSIFY_RESULT": "${{ needs.classify.result }}",
        "CHECK_RESULT": "${{ needs.check.result }}",
        "PYTEST_RESULT": "${{ needs.pytest.result }}",
        "SLOW_RESULT": "${{ needs.slow.result }}",
        "MIGRATION_RESULT": "${{ needs.migration.result }}",
    }


@pytest.mark.parametrize("surface", sorted(MATCHERS))
def test_each_matcher_step_runs_the_module_these_tests_call(surface):
    """What replaced `"${{" not in run`, and proves strictly more.

    While the rule was a bash string, the only way to know the executed text
    was the shipped text was to assert the body carried no workflow-expression
    interpolation. Both harnesses had separately invented that guard. The
    rule is a module now, so the same property is asserted about the module:
    the step runs this file, at this path, and reads exactly the names the
    step sets. Interpolation is still refused, because a `${{` in the command
    would mean CI ran something this test did not.
    """

    step = _matcher_step(surface)
    _, _, environment = MATCHERS[surface]

    assert step["run"] == f"python3 scripts/gate_results.py {surface}"
    assert "${{" not in step["run"]
    assert (ROOT / "scripts" / "gate_results.py") == Path(
        gate_results.__file__
    ).resolve()
    # Both directions: a name the step stops setting, or one the adapter
    # stops reading, fails here rather than in the gate.
    assert set(step["env"]) == set(environment)
    assert surface in gate_results.SURFACES


def _expectations(surface: str, required: str, result: str) -> list:
    """Every result the surface judges set to `result`, under answer `required`."""

    if surface == "summary":
        return gate_results.summary_expectations({
            "BEHAVIOR_REQUIRED": required,
            "MIGRATION_REQUIRED": required,
            # The two unconditional jobs are held green so the parametrized
            # case is about the classified ones.
            "CLASSIFY_RESULT": "success",
            "CHECK_RESULT": "success",
            "PYTEST_RESULT": result,
            "SLOW_RESULT": result,
            "MIGRATION_RESULT": result,
        })
    return gate_results.infrastructure_expectations({
        "INFRASTRUCTURE_REQUIRED": required,
        **{name: result for _, name in gate_results.INFRASTRUCTURE_STEPS},
    })


@pytest.mark.parametrize("surface", sorted(MATCHERS))
@pytest.mark.parametrize("required, result", [("true", "success"), ("false", "skipped")])
def test_every_matcher_accepts_only_the_result_the_classifier_asked_for(
    surface, required, result
):
    assert gate_results.match(_expectations(surface, required, result)) == []


@pytest.mark.parametrize("surface", sorted(MATCHERS))
@pytest.mark.parametrize(
    "required, result",
    [
        # The failure the whole design exists to prevent: work the
        # classifier required that a broken condition skipped.
        ("true", "skipped"),
        ("true", "failure"),
        ("true", "cancelled"),
        ("true", ""),
        ("true", "neutral"),
        # The inconsistent pair: it ran although the classifier said it was
        # not needed, so one of the two is wrong and neither may be trusted.
        ("false", "success"),
        ("false", "failure"),
        ("false", "cancelled"),
        ("false", ""),
        # An answer the matcher cannot read is a classifier it cannot trust.
        ("", "skipped"),
        ("", "success"),
        ("maybe", "skipped"),
        ("maybe", "success"),
        ("True", "success"),
        ("TRUE", "success"),
        ("1", "success"),
        ("yes", "success"),
    ],
)
def test_every_matcher_fails_closed(surface, required, result):
    """Both surfaces inherit every case, because both call the same rule."""

    assert gate_results.match(_expectations(surface, required, result)), (
        f"{surface}: answer {required!r} with result {result!r} reported green"
    )


def test_the_summary_judges_exactly_the_jobs_the_gate_runs():
    """A job the adapter stops listing is a job nothing judges.

    The summary `needs` every other job in the gate, so the set of labels it
    matches has to be that same set. A job added to the workflow and not to
    the adapter would otherwise contribute no result at all, which is the
    silent green this whole design exists to prevent.
    """

    judged = {pair.label for pair in gate_results.summary_expectations({})}

    assert judged == set(_job("release-gate")["needs"])
    assert judged == set(_workflow(GATE)["jobs"]) - {"release-gate"}


@pytest.mark.parametrize("surface", sorted(MATCHERS))
@pytest.mark.parametrize("required", ["true", "false", "maybe"])
def test_every_result_reaches_the_matchers_judgement(surface, required):
    """A row the rule skips is work nothing judged -- a gate going quiet.

    `report` is the whole log the step prints, so every result the surface
    was handed has to appear in it exactly once, whatever the classifier
    answered.
    """

    pairs = _expectations(surface, required, "success")
    lines = gate_results.report(pairs)

    for pair in pairs:
        judged = sum(
            f"{pair.label} is " in line or f"{pair.label} = " in line
            for line in lines
        )
        assert judged == 1, f"{surface}/{required}: {pair.label} in {lines}"


BEHAVIOR_PULL_REQUEST = {
    "BEHAVIOR_REQUIRED": "true",
    "MIGRATION_REQUIRED": "false",
    "CLASSIFY_RESULT": "success",
    "CHECK_RESULT": "success",
    "PYTEST_RESULT": "success",
    "SLOW_RESULT": "success",
    "MIGRATION_RESULT": "skipped",
}
DOCUMENTATION_PULL_REQUEST = {
    **BEHAVIOR_PULL_REQUEST,
    "BEHAVIOR_REQUIRED": "false",
    "PYTEST_RESULT": "skipped",
    "SLOW_RESULT": "skipped",
}
MIGRATION_PULL_REQUEST = {
    **BEHAVIOR_PULL_REQUEST,
    "MIGRATION_REQUIRED": "true",
    "MIGRATION_RESULT": "success",
}


def _summarize(results: dict) -> list[str]:
    return gate_results.match(gate_results.summary_expectations(results))


@pytest.mark.parametrize(
    "shape, results",
    [
        ("behavior", BEHAVIOR_PULL_REQUEST),
        ("documentation", DOCUMENTATION_PULL_REQUEST),
        ("migration", MIGRATION_PULL_REQUEST),
    ],
)
def test_the_summary_passes_when_every_job_matches_the_classifier(shape, results):
    assert _summarize(results) == [], shape


@pytest.mark.parametrize(
    "reason, results, expected_error",
    [
        # The failure this ticket exists to prevent: a behavior job that a
        # broken `if` condition skipped must never read as green.
        (
            "pytest skipped when the classifier required it",
            {**BEHAVIOR_PULL_REQUEST, "PYTEST_RESULT": "skipped"},
            "pytest is skipped",
        ),
        (
            "slow skipped when the classifier required it",
            {**BEHAVIOR_PULL_REQUEST, "SLOW_RESULT": "skipped"},
            "slow is skipped",
        ),
        (
            "pytest failed",
            {**BEHAVIOR_PULL_REQUEST, "PYTEST_RESULT": "failure"},
            "pytest is failure",
        ),
        (
            "slow cancelled",
            {**BEHAVIOR_PULL_REQUEST, "SLOW_RESULT": "cancelled"},
            "slow is cancelled",
        ),
        (
            "check failed",
            {**BEHAVIOR_PULL_REQUEST, "CHECK_RESULT": "failure"},
            "check is failure",
        ),
        # A dead classifier leaves both outputs empty and every dependent
        # job skipped. Answering docs-only there would skip the whole suite.
        (
            "the classifier itself failed",
            {
                "BEHAVIOR_REQUIRED": "",
                "MIGRATION_REQUIRED": "",
                "CLASSIFY_RESULT": "failure",
                "CHECK_RESULT": "success",
                "PYTEST_RESULT": "skipped",
                "SLOW_RESULT": "skipped",
                "MIGRATION_RESULT": "skipped",
            },
            "classify is failure",
        ),
        (
            "the classifier answered something that is not a boolean",
            {**BEHAVIOR_PULL_REQUEST, "BEHAVIOR_REQUIRED": "maybe"},
            "behavior_required is maybe",
        ),
        # The inconsistent pair: the behavior jobs ran although the
        # classifier said the change was documentation-only, so one of the
        # two is wrong and neither may be trusted.
        (
            "behavior ran on a documentation-only classification",
            {**DOCUMENTATION_PULL_REQUEST, "PYTEST_RESULT": "success"},
            "pytest is success",
        ),
        (
            "migration skipped when the classifier required it",
            {**MIGRATION_PULL_REQUEST, "MIGRATION_RESULT": "skipped"},
            "migration is skipped",
        ),
        (
            "migration ran when the classifier did not require it",
            {**BEHAVIOR_PULL_REQUEST, "MIGRATION_RESULT": "success"},
            "migration is success",
        ),
    ],
)
def test_the_summary_fails_closed(reason, results, expected_error):
    failures = _summarize(results)

    assert failures, f"{reason}: the gate reported green"
    assert any(expected_error in line for line in failures), f"{reason}: {failures}"


def _run_matcher(surface: str, results: dict) -> subprocess.CompletedProcess:
    """Run the command the workflow ships, from the repository root."""

    return subprocess.run(
        ["bash", "-c", _matcher_step(surface)["run"]],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], **results},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "shape, results",
    [
        ("behavior", BEHAVIOR_PULL_REQUEST),
        ("documentation", DOCUMENTATION_PULL_REQUEST),
        ("migration", MIGRATION_PULL_REQUEST),
    ],
)
def test_the_shipped_summary_command_exits_zero_on_a_matching_run(shape, results):
    completed = _run_matcher("summary", results)

    assert completed.returncode == 0, f"{shape}: {completed.stdout}{completed.stderr}"
    assert "release gate satisfied" in completed.stdout


@pytest.mark.parametrize(
    "reason, results",
    [
        (
            "pytest skipped when the classifier required it",
            {**BEHAVIOR_PULL_REQUEST, "PYTEST_RESULT": "skipped"},
        ),
        (
            "the classifier answered something that is not a boolean",
            {**BEHAVIOR_PULL_REQUEST, "BEHAVIOR_REQUIRED": "maybe"},
        ),
    ],
)
def test_the_shipped_summary_command_exits_non_zero_and_says_so(reason, results):
    """The exit status is what GitHub reads, so it is proven by running it."""

    completed = _run_matcher("summary", results)

    assert completed.returncode != 0, f"{reason}: {completed.stdout}"
    assert "release gate failed closed" in completed.stdout


@pytest.mark.parametrize(
    "required, result, exits_zero",
    [("true", "success", True), ("false", "skipped", True),
     ("true", "skipped", False), ("false", "success", False), ("maybe", "success", False)],
)
def test_the_shipped_infrastructure_command_matches_the_rule(required, result, exits_zero):
    completed = _run_matcher("infrastructure", {
        "INFRASTRUCTURE_REQUIRED": required,
        **{name: result for _, name in gate_results.INFRASTRUCTURE_STEPS},
    })

    assert (completed.returncode == 0) is exits_zero, (
        f"{required}/{result}: {completed.stdout}{completed.stderr}"
    )


def test_a_matcher_run_without_its_environment_fails_closed():
    """An `env:` line deleted from the workflow may not read as agreement."""

    for surface in sorted(MATCHERS):
        completed = _run_matcher(surface, {})
        assert completed.returncode != 0, surface
        assert "release gate failed closed" in completed.stdout, surface


def test_uv_caches_retain_wheels_and_separate_the_complete_dependency_populations():
    """A metadata-only cache forced all runners to redownload wheels (#781).

    GitHub caches are immutable: the root-only check must not win the cache
    that promises both root and render dependencies. Every behavior/full job
    prepares the same locked population and can safely share its wheel cache.
    setup-uv includes the pruning mode and dependency hashes in the cache key.
    """
    gate = _workflow(GATE)
    suffixes = {}
    for name in ("check", "pytest", "slow", "migration"):
        steps = [s for s in gate["jobs"][name]["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
        assert len(steps) == 1
        inputs = steps[0]["with"]
        assert inputs["enable-cache"] == "true"
        assert inputs["prune-cache"] == "false"
        suffixes[name] = inputs["cache-suffix"]
        # Omission keeps setup-uv's full dependency-glob default. Disabling it
        # would leave a cache identity unrelated to the locks it must follow.
        assert inputs.get("cache-dependency-glob", "default")
    assert suffixes["check"] == "root-check-wheels-v1"
    assert {suffixes[name] for name in ("pytest", "slow", "migration")} == {"root-render-wheels-v1"}
    for job in _workflow("full-suite.yml")["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                assert step["with"]["prune-cache"] == "false"
                assert step["with"]["cache-suffix"] == "root-render-wheels-v1"


BARE_PYTHON_SCRIPTS = (
    "scripts/classify_ci_change.py",
    "scripts/ci_feedback.py",
    "scripts/gate_results.py",
)


def _imported_top_level_names(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_bare_python_jobs_scripts_import_only_the_standard_library_and_scripts():
    """The classify and summary jobs run `python3 scripts/...` with no `uv sync`.

    `scripts/__init__.py` states that nothing in the package may import outside
    the standard library; this is the rule as a test rather than a sentence.
    Relative imports inside `scripts/test_gate` resolve within the package.
    """

    checked = [ROOT / name for name in BARE_PYTHON_SCRIPTS]
    checked += sorted((ROOT / "scripts" / "test_gate").glob("*.py"))
    checked.append(ROOT / "scripts" / "__init__.py")
    allowed = set(sys.stdlib_module_names) | {"scripts", "__future__"}
    offenders = {
        str(path.relative_to(ROOT)): sorted(_imported_top_level_names(path) - allowed)
        for path in checked
    }
    assert {name: extra for name, extra in offenders.items() if extra} == {}
