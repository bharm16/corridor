"""Job outputs supply current proof; completed logs supply timing history."""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier

import pytest


SPEC = importlib.util.spec_from_file_location(
    "corridor_ci_feedback", Path(__file__).resolve().parents[1] / "scripts/ci_feedback.py"
)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def test_colored_logs_are_captured_without_printing_terminal_control_bytes(monkeypatch, capsys):
    calls = []
    raw = b"\x1b[36mstep heading\x1b[0m\nCORRIDOR_TEST_RECEIPT {\"sample\":1}\n"
    def run(command, **kwargs):
        calls.append(command[:])
        assert kwargs["capture_output"] is True
        if "--allow-escape-sequences" not in command:
            raise subprocess.CalledProcessError(1, command, stderr=b"response contains terminal escape sequences; pass --allow-escape-sequences")
        return SimpleNamespace(stdout=raw)
    monkeypatch.setattr(ci.subprocess, "run", run)
    result = ci.github("repos/owner/repo/actions/jobs/1/logs", binary=True)
    assert result == raw
    assert ci.logged_json(result, ci.RECEIPT_MARKER) == {"sample": 1}
    assert len(calls) == 2
    assert "--allow-escape-sequences" not in calls[0]
    assert capsys.readouterr().out == ""


def test_old_cli_needs_no_new_flag_and_unrelated_errors_are_not_retried(monkeypatch):
    calls = []
    def success(command, **kwargs):
        calls.append(command[:])
        return SimpleNamespace(stdout=b"plain logs")
    monkeypatch.setattr(ci.subprocess, "run", success)
    assert ci.github("job/logs", binary=True) == b"plain logs"
    assert len(calls) == 1 and "--allow-escape-sequences" not in calls[0]
    def denied(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=b"HTTP 403")
    monkeypatch.setattr(ci.subprocess, "run", denied)
    with pytest.raises(subprocess.CalledProcessError):
        ci.github("job/logs", binary=True)


def _report(run=100, attempt=1):
    expected = {"run_id": str(run), "run_attempt": attempt, "head_sha": "a" * 40,
                "suites": {"pytest": 1, "slow": 1}}
    receipts = [{
        "schema_version": 1, **{key: expected[key] for key in ("run_id", "run_attempt", "head_sha")},
        "suite": suite, "shard": 1, "shards": 1, "elapsed_seconds": 1.0,
        "exit_code": 0, "test_count": 1, "per_file_seconds": {"tests/test_one.py": 0.5},
    } for suite in ("pytest", "slow")]
    return ci.aggregate_receipts(receipts, expected, 2)


def _log(marker, data):
    return ("2026-09-08T10:00:00Z " + marker + " " + json.dumps(data) + "\n").encode()


def _run(run=100, attempt=1):
    return {
        "id": run, "run_attempt": attempt, "conclusion": "failure",
        "repository": {"id": 1}, "head_repository": {"id": 1},
        "created_at": "2026-09-08T01:00:00Z" if attempt > 1 else "2026-09-08T10:00:00Z",
        "run_started_at": "2026-09-08T10:00:00Z", "updated_at": "2026-09-08T10:00:30Z",
    }


def _jobs(start, end, *, attempt=1):
    return [{
        "id": 10 + index, "name": name, "run_attempt": attempt,
        "started_at": start, "completed_at": end, "conclusion": "success",
        "steps": [{"name": "Enforce the measured feedback budget", "conclusion": "failure"}],
    } for index, name in enumerate(("classify", "check", "pytest (1)", "slow (1)", "release-gate"))]


def _history_api(monkeypatch, runs, summaries, logs):
    calls = []

    def github(path, *, binary=False):
        calls.append((path, binary))
        if "/workflows/release-gate.yml/runs?" in path:
            return {"workflow_runs": runs}
        if "/jobs?filter=all" in path:
            run_id = int(path.split("/runs/", 1)[1].split("/", 1)[0])
            return {"jobs": summaries[run_id]}
        if path.endswith("/logs"):
            assert binary is True
            return logs[int(path.rsplit("/", 2)[1])]
        pytest.fail(f"unexpected GitHub endpoint: {path}")

    monkeypatch.setattr(ci, "github", github)
    return calls


def test_logged_json_reads_the_last_machine_record_in_timestamped_logs():
    log = _log(ci.REPORT_MARKER, {"sample": 1})
    log += b"5284 passed, 3 skipped, 23 warnings in 564.85s\n"
    log += (ci.REPORT_MARKER + ' {"sample":2}\n').encode()
    assert ci.logged_json(log, ci.REPORT_MARKER) == {"sample": 2}


def test_human_summaries_and_other_machine_records_are_not_timing_receipts():
    for log in (b"", b"5284 passed, 3 skipped, 23 warnings in 564.85s\n",
                _log(ci.REPORT_MARKER, _report())):
        assert ci.logged_json(log, ci.RECEIPT_MARKER) is None


def test_malformed_final_record_cannot_fall_back_to_an_earlier_good_record():
    for malformed in ('{', '{"value":NaN}', '{"value":1,"value":2}'):
        log = _log(ci.RECEIPT_MARKER, {"valid": 1})
        log += (ci.RECEIPT_MARKER + " " + malformed).encode()
        with pytest.raises(ValueError):
            ci.logged_json(log, ci.RECEIPT_MARKER)


def test_history_uses_completed_attempt_time_and_validated_job_log_weights(monkeypatch):
    run = _run(attempt=2)
    jobs = _jobs(run["run_started_at"], run["updated_at"], attempt=2)
    older_summary = {**jobs[-1], "id": 9, "run_attempt": 1}
    calls = _history_api(monkeypatch, [run], {100: [older_summary, *jobs]},
                         {14: _log(ci.REPORT_MARKER, _report(attempt=2))})
    reports = ci.previous_reports("owner/repo", "200")
    assert len(reports) == 1
    assert reports[0]["gate_elapsed_seconds"] == 30
    assert reports[0]["suites"]["pytest"]["per_file_seconds"] == {"tests/test_one.py": 0.5}
    assert [path for path, _ in calls if path.endswith("/logs")] == [
        "repos/owner/repo/actions/jobs/14/logs"
    ]


def test_history_ignores_newer_runs_forks_cancelled_runs_and_old_unmeasured_jobs(monkeypatch):
    measured = _run()
    summaries = {100: [{"id": 14, "name": "release-gate", "run_attempt": 1,
                       "steps": [{"name": "Enforce the measured feedback budget", "conclusion": "success"}]}]}
    candidates = [_run(300), _run(200), measured,
                  {**_run(99), "head_repository": {"id": 2}},
                  {**_run(98), "conclusion": "cancelled"}]
    calls = _history_api(monkeypatch, candidates, summaries,
                         {14: b"release gate satisfied\n100 passed, 2 warnings in 9s\n"})
    assert ci.previous_reports("owner/repo", "200") == []
    assert not any(f"/runs/{run_id}/" in path for path, _ in calls for run_id in (300, 200, 99, 98))
    measured["run_attempt"] = 2
    calls.clear()
    assert ci.previous_reports("owner/repo", "200") == []
    assert not any(path.endswith("/logs") for path, _ in calls)


def test_historical_report_must_match_its_github_run_and_attempt(monkeypatch):
    run = _run(attempt=2)
    jobs = _jobs(run["run_started_at"], run["updated_at"], attempt=2)
    for wrong_report in (_report(run=101, attempt=2), _report(attempt=1)):
        _history_api(monkeypatch, [run], {100: jobs},
                     {14: _log(ci.REPORT_MARKER, wrong_report)})
        with pytest.raises(ci.EvidenceError, match="disagrees with its GitHub job"):
            ci.previous_reports("owner/repo", "200")


def test_corrupt_historical_measurement_is_ignored_explicitly(monkeypatch, capsys):
    run = _run()
    jobs = _jobs(run["created_at"], run["updated_at"])
    _history_api(monkeypatch, [run], {100: jobs},
                 {14: (ci.REPORT_MARKER + " {broken}\n").encode()})
    assert ci.previous_reports("owner/repo", "200") == []
    assert "Ignoring unusable historical timing for run 100" in capsys.readouterr().out


def test_unavailable_historical_logs_do_not_block_current_job_outputs(monkeypatch, capsys):
    run = _run()
    def github(path, **kwargs):
        if "/workflows/" in path:
            return {"workflow_runs": [run]}
        raise subprocess.CalledProcessError(1, ["gh", "api"], stderr=b"HTTP 403: log access unavailable")
    monkeypatch.setattr(ci, "github", github)
    assert ci.previous_reports("owner/repo", "200") == []
    assert "current-run budget remains enforced" in capsys.readouterr().out


def test_prepare_emits_one_shared_output_for_measured_and_bootstrap_weights(tmp_path, monkeypatch):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    for history in ([], [_report()]):
        output.write_text("")
        monkeypatch.setattr(ci, "previous_reports", lambda *_args: history)
        ci.prepare("owner/repo", "200")
        lines = output.read_text().splitlines()
        assert len(lines) == 1
        name, encoded = lines[0].split("=", 1)
        assert name == "timing_weights"
        expected = {suite: {"tests/test_one.py": 0.5} for suite in ("pytest", "slow")} if history else {}
        assert json.loads(encoded) == expected


def test_retry_measures_its_own_attempt_instead_of_hours_since_creation():
    run = {"run_attempt": 2, "created_at": "2026-09-08T01:00:00Z", "run_started_at": "2026-09-08T10:00:00Z"}
    assert ci._elapsed(ci._attempt_start(run), "2026-09-08T10:00:30Z") == 30


def _finish_api(monkeypatch, *, defect=None):
    report = _report()
    start = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    end = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    jobs = _jobs(start, end)
    jobs[-1].update(completed_at=None, conclusion=None)
    jobs[-1]["started_at"] = end
    outputs = {receipt["suite"]: {f"{receipt['suite']}_1": json.dumps(receipt)} for receipt in report["receipts"]}
    if defect == "missing receipt":
        outputs["slow"] = {}
    elif defect == "malformed receipt":
        outputs["slow"]["slow_1"] = "{broken}"
    elif defect == "wrong slot":
        changed = dict(report["receipts"][1], shard=2)
        outputs["slow"]["slow_1"] = json.dumps(changed)
    elif defect == "failed job":
        jobs[3]["conclusion"] = "failure"
    calls = []

    def github(path, *, binary=False):
        calls.append(path)
        if path.endswith("/runs/100"):
            return {"status": "in_progress", "run_attempt": 1, "created_at": start}
        if "/jobs?filter=all" in path:
            return {"jobs": jobs}
        pytest.fail(f"unexpected GitHub endpoint: {path}")

    monkeypatch.setattr(ci, "github", github)
    monkeypatch.setattr(ci, "previous_reports", lambda *_args: [])
    monkeypatch.setattr(ci, "test_files", lambda: ["tests/test_one.py"] + (
        ["tests/test_missing.py"] if defect == "missing file" else []
    ))
    for key, value in {"PYTEST_SHARDS": "1", "SLOW_SHARDS": "1", "MIGRATION_REQUIRED": "false",
                       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40}.items():
        monkeypatch.setenv(key, value)
    for suite, output in outputs.items():
        monkeypatch.setenv(f"CORRIDOR_{suite.upper()}_RECEIPTS", json.dumps(output))
    return calls


def test_finish_uses_job_outputs_without_fetching_unpublished_current_logs(tmp_path, monkeypatch, capsys):
    calls = _finish_api(monkeypatch)
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert ci.finish("owner/repo", "100") == 0
    output = capsys.readouterr().out
    assert len([line for line in output.splitlines() if line.startswith(ci.REPORT_MARKER + " ")]) == 1
    report = ci.logged_json(output.encode(), ci.REPORT_MARKER)
    assert ci.validate_report(report)["expected"]["run_id"] == "100"
    assert "Current gate:" in summary.read_text()
    assert not any(path.endswith("/logs") for path in calls)
    assert not any("artifact" in path for path in calls)


def test_finish_fetches_independent_metadata_together_before_validating(monkeypatch, capsys):
    """Run status, job inventory and completed history are independent reads."""
    _finish_api(monkeypatch)
    github, run_jobs = ci.github, ci.run_jobs
    started = Barrier(3, timeout=1)

    def current_run(path, **kwargs):
        if path.endswith("/runs/100"):
            started.wait()
        return github(path, **kwargs)

    def current_jobs(*args):
        started.wait()
        return run_jobs(*args)

    def history(*_args):
        started.wait()
        return []

    monkeypatch.setattr(ci, "github", current_run)
    monkeypatch.setattr(ci, "run_jobs", current_jobs)
    monkeypatch.setattr(ci, "previous_reports", history)
    assert ci.finish("owner/repo", "100") == 0
    output = capsys.readouterr().out
    assert ci.validate_report(ci.logged_json(output.encode(), ci.REPORT_MARKER))


def test_finish_refuses_missing_malformed_failed_or_incomplete_current_proof(monkeypatch, capsys):
    for defect in ("missing receipt", "malformed receipt", "wrong slot", "failed job", "missing file"):
        _finish_api(monkeypatch, defect=defect)
        with pytest.raises(ValueError):
            ci.finish("owner/repo", "100")
        assert ci.REPORT_MARKER not in capsys.readouterr().out


def test_partial_retry_keeps_the_successful_jobs_cost_and_verifies_their_attempts():
    report = _report()
    jobs = _jobs("2026-09-08T10:00:00Z", "2026-09-08T10:04:00Z")
    jobs[-1] = {"name": "release-gate", "run_attempt": 2, "started_at": "2026-09-08T11:00:00Z", "completed_at": None, "conclusion": None}
    run = {"run_attempt": 2, "created_at": "2026-09-08T10:00:00Z", "run_started_at": "2026-09-08T11:00:00Z"}
    assert ci.verified_gate_seconds(report["receipts"], run, jobs, "2026-09-08T11:00:10Z") == 490
    jobs[2]["run_attempt"] = 2
    with pytest.raises(ci.EvidenceError, match="successful GitHub job"):
        ci.verified_gate_seconds(report["receipts"], run, jobs, "2026-09-08T11:00:10Z")


def test_independent_check_path_does_not_pay_classifier_time_twice():
    jobs = _jobs("2026-09-08T10:00:00Z", "2026-09-08T10:01:00Z")
    jobs[1]["completed_at"] = "2026-09-08T10:02:30Z"
    jobs[-1] = {"name": "release-gate", "run_attempt": 2, "started_at": "2026-09-08T11:00:00Z", "completed_at": None, "conclusion": None}
    run = {"run_attempt": 2, "created_at": "2026-09-08T10:00:00Z", "run_started_at": "2026-09-08T11:00:00Z"}
    assert ci.verified_gate_seconds(_report()["receipts"], run, jobs, "2026-09-08T11:00:10Z") == 160
