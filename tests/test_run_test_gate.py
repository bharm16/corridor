"""The required runner preserves pytest outcomes and measured file ownership."""

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import run_test_gate as gate
from scripts.test_gate.partition import SLOW_MINIMUM_FILE_SECONDS, shard


def test_migration_parallelizes_only_the_migration_file(tmp_path):
    files = gate.partition("migration", 1, 1, tmp_path)
    assert files == ["tests/test_migration_baseline.py"]
    command = gate.pytest_command("migration", files, 2, tmp_path / "migration.xml")
    assert command[command.index("-n") + 1] == "2"
    assert command[command.index("--dist") + 1] == "worksteal"
    pytest_arguments = command[3:]
    assert pytest_arguments[pytest_arguments.index("-m") + 1] == "migration"
    assert command[-1] == files[0]


def test_slow_runner_preserves_measured_order_when_case_counts_differ(tmp_path):
    """A one-case expensive file must start before a later many-case file.

    The shard already orders files by measured work. Exercise the actual
    installed xdist scheduler: its default case-count sort discards that
    priority even though the command line has the right order.
    """
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = slow: synthetic scheduler case\n")
    (tmp_path / "conftest.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def pytest_runtest_setup(item):\n"
        "    first = Path('first-' + os.environ['PYTEST_XDIST_WORKER'])\n"
        "    if not first.exists():\n"
        "        first.write_text(item.path.name)\n"
    )
    priority = ["test_priority_a.py", "test_priority_b.py"]
    for name in priority:
        (tmp_path / name).write_text(
            "import pytest\npytestmark = pytest.mark.slow\n"
            "def test_one():\n    assert True\n"
        )
    (tmp_path / "test_many.py").write_text(
        "import pytest\npytestmark = pytest.mark.slow\n"
        "@pytest.mark.parametrize('value', range(10))\n"
        "def test_many(value):\n    assert value >= 0\n"
    )
    files = [*priority, "test_many.py"]
    command = gate.pytest_command("slow", files, 2, tmp_path / "scheduler.xml")
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        command, cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.read_text() for path in tmp_path.glob("first-gw*")} == set(priority)


def test_required_runner_preserves_failure_and_does_not_repeat_source_checks(tmp_path, monkeypatch):
    assigned = [*gate.CHECK_OWNED_FILES, "tests/test_example.py"]
    monkeypatch.setattr(gate, "partition", lambda *args: assigned)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    step_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(step_output))
    def run(command, **kwargs):
        assert not set(gate.CHECK_OWNED_FILES).intersection(command)
        assert "GITHUB_OUTPUT" not in kwargs["env"]
        junit = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        junit.write_text('<testsuites><testcase classname="tests.test_example" time="1"><failure/></testcase></testsuites>')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.main(["--suite", "pytest", "--shards", "1", "--shard", "1", "--workers", "2", "--output", str(tmp_path)]) == 1
    receipt = json.loads((tmp_path / "receipt-pytest-1.json").read_text())
    assert receipt["exit_code"] == 1
    assert receipt["test_count"] == 1
    assert receipt["per_file_seconds"] == dict.fromkeys(gate.CHECK_OWNED_FILES, 0.0) | {"tests/test_example.py": 1.0}
    key, encoded = step_output.read_text().strip().split("=", 1)
    assert key == "pytest_1"
    assert json.loads(encoded) == receipt


def test_missing_report_cannot_reuse_an_earlier_green_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "partition", lambda *args: ["tests/test_example.py"])
    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    receipt = tmp_path / "receipt-pytest-1.json"
    receipt.write_text('{"exit_code":0}')
    (tmp_path / "pytest-1.xml").write_text("old")
    assert gate.main(["--suite", "pytest", "--shards", "1", "--shard", "1", "--workers", "2", "--output", str(tmp_path)]) == 1
    assert not receipt.exists()


def test_slow_partition_spreads_zero_time_imports_without_changing_coverage(monkeypatch, tmp_path):
    """Zero-time reports mean no measured cases, not free module collection.

    Six large files nearly fill three work buckets. At the old 0.05s cost all
    278 zero-time imports fit in the lightest bucket, even though both workers
    must import them. The slow-only floor spreads that load without pruning a
    single file or moving the expensive modules in this regression population.
    """
    heavy = {f"tests/test_heavy_{i}.py": value for i, value in enumerate((100, 98, 90, 85, 74, 55))}
    durations = {**heavy, **{f"tests/test_zero_{i:03}.py": 0.0 for i in range(278)}}
    files = sorted(durations)
    monkeypatch.setattr(gate, "test_files", lambda: files)
    monkeypatch.setenv("CORRIDOR_CI_WEIGHTS", json.dumps({"slow": durations, "pytest": durations}))
    previous = shard(files, durations, 3)
    slow = [gate.partition("slow", 3, index, tmp_path) for index in range(1, 4)]
    ordinary = [gate.partition("pytest", 3, index, tmp_path) for index in range(1, 4)]
    assert ordinary == previous
    assert slow == shard(files, durations, 3, minimum_file_seconds=SLOW_MINIMUM_FILE_SECONDS)
    assert max(map(len, previous)) > 250
    assert max(map(len, slow)) < 125
    assert max(map(len, slow)) - min(map(len, slow)) < 45
    assert sorted(name for bucket in slow for name in bucket) == files
    assert [{name for name in bucket if name in heavy} for bucket in slow] == [
        {name for name in bucket if name in heavy} for bucket in previous]


@pytest.mark.parametrize("floor", [-1, float("inf"), float("nan")])
def test_nonfinite_or_negative_collection_floors_cannot_create_a_partition(floor):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        shard(["tests/test_one.py"], {}, 1, minimum_file_seconds=floor)
