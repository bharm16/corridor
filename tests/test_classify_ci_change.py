"""The CI classifier decides which gates a pull request must run.

Public-seam cases use a throwaway git repository and invoke the script exactly
as `release-gate.yml` does. They prove that a rename reaches the classifier
under both names, so moving an executable input into documentation cannot make
its original path vanish (#697). Additional path-boundary cases call classify
directly; they add coverage without repeating Git setup for every filename.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from scripts import classify_ci_change


ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = ROOT / "scripts" / "classify_ci_change.py"


def _module():
    return classify_ci_change


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _write(repository: Path, path: str, body: str) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository whose base commit carries one file of every shape."""

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "gate@example.invalid")
    _git(root, "config", "user.name", "Release Gate")
    for path in (
        "docs/only.md",
        "README.md",
        "roadmap.md",
        "prompts/agreement_v3.md",
        "src/corridor/ledger.py",
        "src/corridor/models/spine.py",
        "src/corridor/migrations/baseline_versions/0001_base.py",
        ".github/workflows/release-gate.yml",
        "Makefile",
    ):
        _write(root, path, f"base contents of {path}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _commit(repository: Path, message: str = "change") -> tuple[str, str]:
    base = _git(repository, "rev-parse", "HEAD").strip()
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "-m", message)
    head = _git(repository, "rev-parse", "HEAD").strip()
    return base, head


def _classify(repository: Path, base: str, head: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(CLASSIFIER),
            "--base",
            base,
            "--head",
            head,
            "--repository",
            str(repository),
        ],
        capture_output=True,
        text=True,
    )


def _answers(completed: subprocess.CompletedProcess) -> dict[str, str]:
    assert completed.returncode == 0, completed.stderr
    return dict(
        line.split("=", 1) for line in completed.stdout.split() if "=" in line
    )


def _classified(repository: Path, message: str = "change") -> dict[str, str]:
    base, head = _commit(repository, message)
    return _answers(_classify(repository, base, head))


def test_a_documentation_only_change_runs_check_alone(repository: Path):
    _write(repository, "docs/only.md", "revised\n")

    assert _classified(repository) == {
        "behavior_required": "false",
        "migration_required": "false",
        "infrastructure_required": "false",
    }


def test_a_readme_change_runs_check_alone(repository: Path):
    _write(repository, "README.md", "revised\n")

    assert _classified(repository) == {
        "behavior_required": "false",
        "migration_required": "false",
        "infrastructure_required": "false",
    }


def test_a_runtime_prompt_change_runs_the_behavior_suites(repository: Path):
    """The coverage gap this classifier exists to close.

    `prompts/agreement_v3.md` is executable: src/corridor/extract_agreement.py
    reads it at runtime. The predecessor `paths-ignore: ["**.md"]` filter
    skipped every behavior suite for a change to it.
    """

    _write(repository, "prompts/agreement_v3.md", "a different instruction\n")

    assert _classified(repository) == {
        "behavior_required": "true",
        "migration_required": "false",
        "infrastructure_required": "false",
    }


def test_an_ordinary_source_change_runs_the_behavior_suites(repository: Path):
    _write(repository, "src/corridor/ledger.py", "print('revised')\n")

    assert _classified(repository) == {
        "behavior_required": "true",
        "migration_required": "false",
        "infrastructure_required": "false",
    }


def test_a_migration_change_runs_the_behavior_and_migration_gates(repository: Path):
    _write(
        repository,
        "src/corridor/migrations/baseline_versions/0001_base.py",
        "revision = '0002'\n",
    )

    assert _classified(repository) == {
        "behavior_required": "true",
        "migration_required": "true",
        "infrastructure_required": "false",
    }


def test_a_workflow_change_runs_the_behavior_and_migration_gates(repository: Path):
    """`.github/workflows/**` was migration-adjacent before, and stays so."""

    _write(repository, ".github/workflows/release-gate.yml", "name: revised\n")

    assert _classified(repository) == {
        "behavior_required": "true",
        "migration_required": "true",
        "infrastructure_required": "true",
    }


def test_documentation_beside_code_runs_the_behavior_suites(repository: Path):
    """One non-documentation path is enough; docs-only means *every* path."""

    _write(repository, "docs/only.md", "revised\n")
    _write(repository, "src/corridor/ledger.py", "print('revised')\n")

    assert _classified(repository) == {
        "behavior_required": "true",
        "migration_required": "false",
        "infrastructure_required": "false",
    }


def test_a_runtime_prompt_renamed_into_documentation_still_runs_behavior(
    repository: Path,
):
    """Both names of a rename are classified, or the gate can be moved away.

    Without this, `git mv prompts/agreement_v3.md docs/` would present the
    classifier with a single documentation path and skip every behavior
    suite for a change that moved an executable prompt out from under the
    code that reads it.
    """

    _git(repository, "mv", "prompts/agreement_v3.md", "docs/agreement_v3.md")
    base, head = _commit(repository, "move the prompt into docs")

    # Arm the precondition: this fixture only exercises the rename branch if
    # git actually reports one record naming both paths.
    record = _git(repository, "diff", "--name-status", "-M", f"{base}...{head}")
    assert record.startswith("R"), f"git reported no rename, only: {record!r}"
    assert "prompts/agreement_v3.md" in record and "docs/agreement_v3.md" in record

    completed = _classify(repository, base, head)

    assert _answers(completed)["behavior_required"] == "true"
    assert "prompts/agreement_v3.md" in completed.stderr


def test_an_empty_diff_fails_the_classifier_rather_than_answering_docs_only(
    repository: Path,
):
    head = _git(repository, "rev-parse", "HEAD").strip()

    completed = _classify(repository, head, head)

    assert completed.returncode != 0
    assert "no changed paths" in completed.stderr
    assert "behavior_required" not in completed.stdout


def test_an_unreadable_diff_fails_the_classifier(repository: Path):
    head = _git(repository, "rev-parse", "HEAD").strip()

    completed = _classify(repository, "0" * 40, head)

    assert completed.returncode != 0
    assert "could not read the diff" in completed.stderr
    assert "behavior_required" not in completed.stdout


def test_the_documentation_allowlist_is_narrow_and_names_no_wildcard():
    """Docs-only is an allowlist. A `**.md` entry would reopen the gap."""

    module = _module()

    assert set(module.DOCUMENTATION_PATHS) == {
        "docs/**",
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "CONTEXT.md",
        "CONTEXT-MAP.md",
        "roadmap.md",
    }
    for executable in (
        "prompts/agreement_v3.md",
        "prompts/minutes_v5.md",
        "src/corridor/ledger.py",
        "tests/test_ci_policy.py",
        "scripts/test_shard.py",
        "workers/render/pyproject.toml",
        ".github/workflows/release-gate.yml",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
    ):
        assert not module.is_documentation(executable), executable


def test_the_migration_path_set_covers_schema_and_the_database_harness():
    module = _module()

    assert set(module.MIGRATION_PATHS) == {
        "src/corridor/migrations/**",
        "src/corridor/models/**",
        "src/corridor/shadow_schema.py",
        "src/corridor/legacy_history_inventory.py",
        "src/corridor/db.py",
        "src/corridor/m8_acceptance_database.py",
        "tests/conftest.py",
        "tests/test_*migration*.py",
        "tests/test_m8_acceptance_integrity.py",
        "alembic.ini",
        "pyproject.toml",
        "Makefile",
        ".github/workflows/**",
    }
    for adjacent in (
        "src/corridor/migrations/baseline_versions/0001_base.py",
        "src/corridor/shadow_schema.py",
        "src/corridor/legacy_history_inventory.py",
        "tests/test_database_migration.py",
        "alembic.ini",
        ".github/workflows/full-suite.yml",
    ):
        assert module.is_migration_adjacent(adjacent), adjacent
    for unrelated in ("src/corridor/ledger.py", "docs/only.md", "prompts/minutes_v5.md"):
        assert not module.is_migration_adjacent(unrelated), unrelated


def test_a_star_does_not_cross_a_path_separator():
    """`tests/test_*migration*.py` must not swallow the whole tree."""

    module = _module()

    assert not module.is_migration_adjacent("tests/nested/test_migration_x.py")
    assert module.is_migration_adjacent("tests/test_migration_x.py")


@pytest.mark.parametrize("path", [
    "infra/corridor_infra/application_stack.py",
    "infra/tests/test_stacks.py",
    "infra/cdk.context.json",
    "infra/uv.lock",
    "infra/package-lock.json",
    "src/corridor/config.py",
    "src/corridor/deployment_bootstrap.py",
    "scripts/container_entrypoint.py",
    "scripts/register_ecs_release_task_definitions.py",
    "scripts/verify_ecs_release.py",
    ".github/scripts/validate-deployment-config.sh",
    "Dockerfile",
    ".dockerignore",
    "docker-compose.yml",
    "pyproject.toml",
    "uv.lock",
    "workers/render/pyproject.toml",
    "workers/render/uv.lock",
    "alembic.ini",
    "Makefile",
])
def test_infrastructure_and_its_external_inputs_run_cdk_checks(path: str):
    classification = _module().classify([path])

    assert classification.infrastructure_required
    assert classification.behavior_required


@pytest.mark.parametrize("path", [
    "docs/deployment/nonproduction-aws.md",
    "docs/adr/0088-example.md",
    "src/corridor/facts.py",
    "tests/test_facts.py",
    "prompts/agreement_v3.md",
])
def test_unrelated_changes_skip_cdk_without_narrowing_behavior(path: str):
    classification = _module().classify([path])

    assert not classification.infrastructure_required
    assert classification.behavior_required == (not path.startswith("docs/"))


def test_an_infrastructure_input_renamed_into_docs_still_runs_cdk(repository: Path):
    _write(repository, "infra/app.py", "infrastructure source\n")
    _commit(repository, "add infrastructure")
    _git(repository, "mv", "infra/app.py", "docs/old-app.py")

    assert _classified(repository)["infrastructure_required"] == "true"
