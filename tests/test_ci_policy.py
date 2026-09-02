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
    assert set(workflow["jobs"]) == {"check", "pytest", "slow"}
    commands = _run_commands(workflow)
    # Each behavior gate runs exactly once across the workflow: the standalone
    # check job owns `make check`, so the pytest job must not repeat it.
    assert commands.count("make check") == 1
    assert commands.count("make test") == 1
    assert commands.count("make test-slow") == 1
    assert "make test-full" not in commands
    assert "make test-migrations" not in commands
    assert "make check" not in _run_commands({"jobs": {"pytest": workflow["jobs"]["pytest"]}})


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
    for name in ("test.yml", "migration-test.yml"):
        concurrency = _workflow(name)["concurrency"]
        assert concurrency["cancel-in-progress"] == "true"
        assert "github.event.pull_request.number" in concurrency["group"]
