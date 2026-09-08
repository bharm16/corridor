"""Small child processes prove local test completion without nested pytest."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "corridor_local_test_runner", ROOT / "scripts" / "run_local_tests.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _run(tmp_path, source, timeout=2):
    receipt = tmp_path / "test.json"
    code = runner._run_child(
        [sys.executable, "-c", source], suite="test", receipt_path=receipt,
        timeout_seconds=timeout, heartbeat_seconds=0.05,
    )
    return code, json.loads(receipt.read_text())


def test_warning_summary_finishes_and_all_output_reaches_the_terminal(tmp_path, capfd):
    code, receipt = _run(tmp_path, """
import sys
print('working: 50%')
print('5284 passed, 3 skipped, 23 warnings in 564.85s')
print('a diagnostic on stderr', file=sys.stderr)
""")
    captured = capfd.readouterr()
    assert "working: 50%" in captured.out
    assert "23 warnings in" in captured.out
    assert "diagnostic on stderr" in captured.err
    assert code == receipt["exit_code"] == receipt["child_exit_code"] == 0
    assert receipt["status"] == "completed"
    assert receipt["outcome"] == "exited"
    assert receipt["pid"] != os.getpid()
    assert receipt["elapsed_seconds"] < 2


@pytest.mark.parametrize("exit_code", [1, 2, 5])
def test_pytest_exit_status_is_preserved_without_reading_output(tmp_path, exit_code):
    code, receipt = _run(tmp_path, f"raise SystemExit({exit_code})")
    assert code == receipt["exit_code"] == receipt["child_exit_code"] == exit_code
    assert receipt["status"] == "completed"


def test_running_receipt_replaces_stale_success_before_the_child_starts(tmp_path):
    path = tmp_path / "test.json"
    path.write_text('{"status":"completed","exit_code":0}')
    source = f"""
import json
from pathlib import Path
receipt = json.loads(Path({str(path)!r}).read_text())
assert receipt['status'] == 'running'
assert receipt['exit_code'] is None
"""
    code, receipt = _run(tmp_path, source)
    assert code == 0
    assert receipt["status"] == "completed"


def test_timeout_stops_its_child_and_writes_a_failure_receipt(tmp_path):
    code, receipt = _run(tmp_path, "import time; time.sleep(30)", timeout=0.1)
    assert code == receipt["exit_code"] == 124
    assert receipt["status"] == "completed"
    assert receipt["outcome"] == "timed_out"
    with pytest.raises(ProcessLookupError):
        os.kill(receipt["pid"], 0)


def test_timeout_kills_an_owned_worker_even_when_it_ignores_termination(tmp_path):
    marker = tmp_path / "worker-survived"
    ready = tmp_path / "worker-ready"
    source = f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, '-c',
    "import signal,time; from pathlib import Path; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "Path({str(ready)!r}).touch(); "
    "time.sleep(0.7); Path({str(marker)!r}).write_text('orphan')"])
time.sleep(30)
"""
    code, _receipt = _run(tmp_path, source, timeout=0.5)
    assert code == 124
    assert ready.exists()
    time.sleep(0.4)
    assert not marker.exists()


def test_timeout_leaves_an_independent_sibling_process_running(tmp_path):
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        code, _receipt = _run(tmp_path, "import time; time.sleep(30)", timeout=0.1)
        assert code == 124
        assert sibling.poll() is None
    finally:
        sibling.terminate()
        sibling.wait(timeout=2)


def test_signal_exit_is_returned_using_shell_exit_status_conventions(tmp_path):
    code, receipt = _run(tmp_path, "import os,signal; os.kill(os.getpid(), signal.SIGTERM)")
    assert code == receipt["exit_code"] == 128 + signal.SIGTERM
    assert receipt["child_exit_code"] == -signal.SIGTERM
    assert receipt["outcome"] == "signalled"


def test_interrupt_stops_owned_child_and_records_the_interrupt(tmp_path):
    source = f"""
import os, signal, time
os.kill({os.getpid()}, signal.SIGTERM)
time.sleep(30)
"""
    code, receipt = _run(tmp_path, source)
    assert code == receipt["exit_code"] == 128 + signal.SIGTERM
    assert receipt["outcome"] == "interrupted"
    with pytest.raises(ProcessLookupError):
        os.kill(receipt["pid"], 0)


def test_start_failure_replaces_stale_success_with_a_known_failure(tmp_path):
    receipt = tmp_path / "test.json"
    receipt.write_text('{"status":"completed","exit_code":0}')
    with pytest.raises(FileNotFoundError):
        runner._run_child(
            [str(tmp_path / "missing")], suite="test", receipt_path=receipt,
            timeout_seconds=2,
        )
    data = json.loads(receipt.read_text())
    assert data["status"] == "completed"
    assert data["exit_code"] == 127
    assert data["outcome"] == "runner_error"


def test_cli_builds_exactly_one_pytest_child_with_the_same_interpreter(monkeypatch):
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))
        return 5

    monkeypatch.setattr(runner, "_run_child", record)
    assert runner.main(["--suite", "focused", "--timeoutseconds", "30", "--",
                        "-n", "1", "tests/test_example.py"]) == 5
    assert calls == [(
        [sys.executable, "-m", "pytest", "-x", "-n", "1", "tests/test_example.py"],
        {"suite": "focused", "timeout_seconds": 30,
         "diagnostic_reason": None,
         "receipt_path": ROOT / "out" / "test-results" / "focused.json"},
    )]


def test_an_explicit_diagnostic_failure_limit_is_preserved(monkeypatch):
    commands = []
    monkeypatch.setattr(runner, "_run_child", lambda command, **kwargs: commands.append(command) or 0)
    assert runner.main(["--suite", "test", "--diagnostic-reason", "failure-reproduction",
                        "--", "--maxfail=0"]) == 0
    assert commands == [[sys.executable, "-m", "pytest", "--maxfail=0"]]


@pytest.mark.parametrize("suite", ["test", "slow", "full"])
@pytest.mark.parametrize("ci_value", [None, "false", "1", "TRUE"])
def test_broad_local_cli_refuses_before_launch_without_a_diagnostic_reason(
    monkeypatch, capsys, suite, ci_value
):
    if ci_value is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", ci_value)
    # An ambient authorization does not replace the explicit CLI choice.
    monkeypatch.setenv(runner.DIAGNOSTIC_ENV, "failure-reproduction")
    monkeypatch.setattr(runner, "_run_child", lambda *_args, **_kwargs: pytest.fail("child launched"))
    assert runner.main(["--suite", suite, "--", "-n", "2"]) == 2
    assert "make test-focused" in capsys.readouterr().err


@pytest.mark.parametrize("reason", runner.DIAGNOSTIC_REASONS)
def test_explicit_diagnostic_cli_launches_once_with_the_chosen_reason(monkeypatch, reason):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    calls = []
    monkeypatch.setattr(runner, "_run_child", lambda command, **kwargs: calls.append(kwargs) or 0)
    assert runner.main(["--suite", "full", "--diagnostic-reason", reason]) == 0
    assert len(calls) == 1
    assert calls[0]["diagnostic_reason"] == reason


def test_ci_broad_cli_launches_once_without_a_local_diagnostic_reason(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    calls = []
    monkeypatch.setattr(runner, "_run_child", lambda command, **kwargs: calls.append(kwargs) or 0)
    assert runner.main(["--suite", "test"]) == 0
    assert len(calls) == 1
    assert calls[0]["diagnostic_reason"] is None


def test_diagnostic_reason_reaches_only_the_owned_child(monkeypatch, tmp_path):
    monkeypatch.setenv(runner.DIAGNOSTIC_ENV, "parent-value")
    receipt = tmp_path / "test.json"
    code = runner._run_child(
        [sys.executable, "-c", "import os; assert os.environ.get('CORRIDOR_LOCAL_BROAD_REASON') == 'performance-investigation'"],
        suite="test", receipt_path=receipt, timeout_seconds=2,
        diagnostic_reason="performance-investigation",
    )
    assert code == 0
    assert os.environ[runner.DIAGNOSTIC_ENV] == "parent-value"
    assert json.loads(receipt.read_text())["diagnostic_reason"] == "performance-investigation"


def test_child_does_not_inherit_an_unrequested_broad_diagnostic_reason(monkeypatch, tmp_path):
    monkeypatch.setenv(runner.DIAGNOSTIC_ENV, "failure-reproduction")
    code, _receipt = _run(
        tmp_path, "import os; assert 'CORRIDOR_LOCAL_BROAD_REASON' not in os.environ"
    )
    assert code == 0
    assert os.environ[runner.DIAGNOSTIC_ENV] == "failure-reproduction"
