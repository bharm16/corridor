"""The required runner preserves pytest outcomes and measured file ownership."""

import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "corridor_run_test_gate", Path(__file__).resolve().parents[1] / "scripts/run_test_gate.py"
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_measurement_keeps_zeroes_and_ignores_skipped_work(tmp_path):
    report = tmp_path / "suite.xml"
    report.write_text('<testsuites><testsuite><testcase classname="tests.test_a" time="2"/><testcase classname="tests.test_a" time="5"><skipped/></testcase></testsuite></testsuites>')
    assert gate.measured_cases(report, ["tests/test_a.py", "tests/test_b.py"]) == (1, {"tests/test_a.py": 2.0, "tests/test_b.py": 0.0})


@pytest.mark.parametrize("case", ['classname="tests.test_other" time="1"', 'classname="tests.test_a" time="nan"', 'classname="tests.test_a" time="-1"'])
def test_untrustworthy_junit_cannot_produce_weights(tmp_path, case):
    report = tmp_path / "suite.xml"
    report.write_text(f"<testsuites><testcase {case}/></testsuites>")
    with pytest.raises(ValueError):
        gate.measured_cases(report, ["tests/test_a.py"])


def test_migration_parallelizes_only_the_migration_file(tmp_path):
    files = gate.partition("migration", 1, 1, tmp_path)
    assert files == ["tests/test_migration_baseline.py"]
    command = gate.pytest_command("migration", files, 2, tmp_path / "migration.xml")
    assert command[command.index("-n") + 1] == "2"
    assert command[command.index("--dist") + 1] == "worksteal"
    pytest_arguments = command[3:]
    assert pytest_arguments[pytest_arguments.index("-m") + 1] == "migration"
    assert command[-1] == files[0]


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
