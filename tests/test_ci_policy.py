"""Repository test gates run in the smallest CI scope that proves the change."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> dict:
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(),
        Loader=yaml.BaseLoader,
    )


def _run_commands(workflow: dict) -> tuple[str, ...]:
    return tuple(
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "run" in step
    )


def test_normal_pr_ci_runs_non_overlapping_behavior_gates_once():
    workflow = _workflow("test.yml")

    assert set(workflow["on"]) == {"pull_request"}
    assert set(workflow["jobs"]) == {"pytest", "slow"}
    commands = _run_commands(workflow)
    # Each behavior gate runs exactly once across the workflow, and the
    # standalone check workflow owns `make check`, so neither job repeats it.
    assert "make check" not in commands
    # The non-slow suite is sharded across runners, so it appears once as a
    # matrix step rather than as one whole-suite command (#548).
    assert commands.count("make test") == 0
    assert sum("make test-shard" in command for command in commands) == 1
    assert commands.count("make test-slow") == 0
    assert sum("make test-slow-shard" in command for command in commands) == 1
    assert "make test-full" not in commands
    assert "make test-migrations" not in commands


def test_behavior_gates_skip_documentation_only_revisions():
    ignored = set(_workflow("test.yml")["on"]["pull_request"]["paths-ignore"])

    assert {"**.md", "docs/**"} <= ignored


def test_check_runs_on_every_revision_including_documentation_only():
    workflow = _workflow("check.yml")

    # Not path-scoped: `make check` owns the ADR lifecycle, amendment-graph,
    # and INDEX.md freshness tests, which a documentation-only change breaks.
    assert set(workflow["on"]) == {"pull_request"}
    assert not workflow["on"]["pull_request"], "no paths / paths-ignore scoping"
    assert set(workflow["jobs"]) == {"check"}
    assert _run_commands(workflow).count("make check") == 1


def test_every_test_file_lands_in_exactly_one_shard():
    """Sharding must cover the suite, or the gate silently proves less.

    The risk of splitting a suite across runners is a file that falls in no
    shard: the gate stays green while nothing runs it.
    """

    import subprocess
    import sys

    shards = _shard_count()
    expected = {
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")
    }

    # Each gate balances on its own recorded seconds, so each has its own
    # partition; both must cover the suite exactly.
    for profile in ([], ["--slow"]):
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

        label = profile[0] if profile else "--fast"
        assert len(assigned) == len(set(assigned)), f"{label}: a file landed twice"
        assert set(assigned) == expected, (
            f"{label}: shards do not cover the suite: "
            f"{sorted(expected.symmetric_difference(assigned))}"
        )


def _shard_count() -> int:
    workflow = _workflow("test.yml")
    return len(workflow["jobs"]["pytest"]["strategy"]["matrix"]["shard"])


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


def test_migration_ci_is_path_scoped_and_full_history_is_scheduled():
    workflow = _workflow("migration-test.yml")

    assert set(workflow["on"]) == {
        "pull_request",
        "schedule",
        "workflow_dispatch",
    }
    migration_paths = set(workflow["on"]["pull_request"]["paths"])
    assert {
        "src/corridor/migrations/**",
        "src/corridor/models.py",
        "tests/test_*migration*.py",
        "src/corridor/m8_acceptance_database.py",
        "tests/conftest.py",
    } <= migration_paths
    commands = _run_commands(workflow)
    assert "make test-migrations" in commands
    assert "make test-full" in commands


def test_pr_workflows_cancel_obsolete_revisions():
    for name in ("check.yml", "test.yml", "migration-test.yml"):
        concurrency = _workflow(name)["concurrency"]
        assert concurrency["cancel-in-progress"] == "true"
        assert "github.event.pull_request.number" in concurrency["group"]
