"""Repository test gates run in the smallest CI scope that proves the change.

The gate is one workflow, `.github/workflows/release-gate.yml`, because a
required status check has to report on every pull request or it blocks that
pull request forever. A workflow skipped by a path filter on its *trigger*
never reports; a job skipped by an `if` reports `skipped`, which the
`release-gate` summary job reads through `needs.<job>.result` and matches
against what `scripts/classify_ci_change.py` asked for (ADR-0093, #697).
"""

import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
import yaml


ROOT = Path(__file__).resolve().parents[1]
GATE = "release-gate.yml"
# Every gate job that installs packages. The account's measured ceiling is
# concurrent package downloads, not runners (#548, #595).
DOWNLOADING_JOB_COUNT = 11


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


def _job(name: str, workflow: str = GATE) -> dict:
    return _workflow(workflow)["jobs"][name]


def _shard_count(job: str = "pytest") -> int:
    return len(_job(job)["strategy"]["matrix"]["shard"])


def test_the_repository_carries_exactly_the_gate_and_the_scheduled_suite():
    """A third pull-request workflow is how the pending-check problem returns.

    Any workflow whose `pull_request` trigger is path-filtered leaves a check
    pending on the pull requests it skips, so it can never be required and
    the gate it holds can never be proven.
    """

    assert _workflow_names() == ("full-suite.yml", GATE)


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
    assert set(classify["outputs"]) == {"behavior_required", "migration_required"}


def test_the_added_jobs_download_no_packages():
    """Job count is the account's measured ceiling, so it may not creep.

    `classify` and `release-gate` are new, but they run alone at either end
    of the gate and install nothing, so the number of jobs competing for
    package downloads is the eleven the three predecessor workflows already
    ran (#548, #595).
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

    The wall clock is the slowest of nine test jobs, so each run samples the
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


def test_the_gate_migrates_the_database_its_shared_state_tests_read():
    """The workflow's `alembic upgrade head` is not redundant (#595).

    tests/conftest.py migrates a per-run template and clones it per worker,
    which makes the workflow's own upgrade look like duplicated work. It is
    not: `shared_source_database_url` hands tests the *configured* database
    rather than a worker clone, and three tests read it —
    tests/test_briefing.py, tests/test_sh99_admission_acceptance.py and
    tests/test_sh99_shared_admission_seal.py. Each is written to skip when
    the shared corpus is absent, which is the outcome CI wants. Measured on
    an empty database all three raise UndefinedTable instead, and all three
    skip again once it is migrated.
    """

    setup = (ROOT / "scripts" / "ci_environment.sh").read_text()

    assert "alembic upgrade head" in setup


def test_the_shared_state_fixture_reaches_a_migrated_database(
    shared_source_database_url,
):
    """The executable half of the guard above.

    This fails on a CI job whose setup stopped migrating the configured
    database, and it fails there instead of in three unrelated test files.
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
        "the configured database carries no schema, so the shared-state "
        "tests will raise UndefinedTable instead of skipping"
    )


def test_every_test_file_lands_in_exactly_one_shard():
    """Sharding must cover the suite, or the gate silently proves less.

    The risk of splitting a suite across runners is a file that falls in no
    shard: the gate stays green while nothing runs it.
    """

    expected = {
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")
    }

    # Each gate balances on its own recorded seconds and carries its own
    # runner count, so each has its own partition; both must cover the suite
    # exactly.
    for job, profile in (("pytest", []), ("slow", ["--slow"])):
        shards = _shard_count(job)
        assigned: list[str] = []
        for shard in range(1, shards + 1):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "test_shard.py"),
                    "--shards",
                    str(shards),
                    "--shard",
                    str(shard),
                    *profile,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assigned.extend(completed.stdout.split())

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


def test_the_parallel_gates_rebalance_instead_of_pinning_a_file_to_one_worker():
    """`--dist loadfile` made one file the suite's floor (#548).

    tests/test_facts was 42.7% of the non-slow suite, and loadfile puts a
    whole file on one worker, so that file alone set a ~306s wall clock no
    worker count could beat. Rebalancing cut the same suite from 351.9s to
    134.9s. The focused and migration gates stay on loadfile: they run on one
    worker, where the mode is irrelevant.
    """

    makefile = (ROOT / "Makefile").read_text()
    parallel = [
        line
        for line in makefile.splitlines()
        if "pytest -n $(TEST_WORKERS)" in line
    ]

    assert parallel, "expected the parallel gates to be defined in the Makefile"
    assert all("--dist worksteal" in line for line in parallel), (
        "a parallel gate reverted to loadfile: " + "; ".join(parallel)
    )


def test_pr_workflows_cancel_obsolete_revisions():
    concurrency = _workflow(GATE)["concurrency"]

    assert concurrency["cancel-in-progress"] == "true"
    assert "github.event.pull_request.number" in concurrency["group"]


# --- the summary job, executed ------------------------------------------------
#
# The summary is the required context, so its decision is proven by running
# the exact bytes the workflow ships rather than by reading them.


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


def _summary_script() -> str:
    body = _summary_step()["run"]
    assert "${{" not in body, (
        "the summary body interpolates a workflow expression, so what runs "
        "in CI is not what this test executes"
    )
    return body


def _summarize(**overrides: str) -> subprocess.CompletedProcess:
    environment = {"PATH": os.environ["PATH"]}
    environment.update(overrides)
    return subprocess.run(
        ["bash", "-c", _summary_script()],
        env=environment,
        capture_output=True,
        text=True,
    )


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


@pytest.mark.parametrize(
    "shape, results",
    [
        ("behavior", BEHAVIOR_PULL_REQUEST),
        ("documentation", DOCUMENTATION_PULL_REQUEST),
        ("migration", MIGRATION_PULL_REQUEST),
    ],
)
def test_the_summary_passes_when_every_job_matches_the_classifier(shape, results):
    completed = _summarize(**results)

    assert completed.returncode == 0, f"{shape}: {completed.stdout}{completed.stderr}"
    assert "release gate satisfied" in completed.stdout


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
    completed = _summarize(**results)

    assert completed.returncode != 0, f"{reason}: the gate reported green"
    assert expected_error in completed.stdout, (
        f"{reason}: {completed.stdout}{completed.stderr}"
    )
    assert "release gate failed closed" in completed.stdout


def test_release_gate_proof_deliberate_failure():
    """Deliberate failure proving the gate reports red (#697). Reverted."""
    assert False, "deliberate release-gate proof failure"
