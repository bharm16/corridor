"""Artifact plumbing cannot invent history or accept incomplete current proof."""

from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest


SPEC = importlib.util.spec_from_file_location(
    "corridor_ci_feedback", Path(__file__).resolve().parents[1] / "scripts/ci_feedback.py"
)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def _report(run=100):
    expected = {"run_id": str(run), "run_attempt": 1, "head_sha": "a" * 40, "suites": {"pytest": 1, "slow": 1}}
    receipts = [{
        "schema_version": 1, **{k: expected[k] for k in ("run_id", "run_attempt", "head_sha")},
        "suite": suite, "shard": 1, "shards": 1, "elapsed_seconds": 1.0,
        "exit_code": 0, "test_count": 1, "per_file_seconds": {"tests/test_one.py": 0.5},
    } for suite in ("pytest", "slow")]
    return ci.aggregate_receipts(receipts, expected, 2)


def _zip(report, name="report.json"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, json.dumps(report))
    return stream.getvalue()


def _jobs(start, end, *, attempt=1):
    return [{"name": name, "run_attempt": attempt, "started_at": start, "completed_at": end, "conclusion": "success"} for name in ("classify", "check", "pytest (1)", "slow (1)", "release-gate")]


def test_artifact_requires_the_actual_validated_single_report():
    assert ci.report_from_zip(_zip(_report())) == _report()
    with pytest.raises(ci.EvidenceError):
        ci.report_from_zip(_zip(_report(), "../report.json"))
    invalid = _report()
    invalid["suites"]["pytest"]["test_count"] = 1000
    with pytest.raises(ci.EvidenceError):
        ci.report_from_zip(_zip(invalid))


def test_bootstrap_is_explicit_and_removes_stale_local_weights(tmp_path, monkeypatch):
    (tmp_path / "pytest.json").write_text('{"old": 1}')
    monkeypatch.setattr(ci, "github", lambda *args, **kwargs: {"artifacts": []})
    ci.prepare(tmp_path, "owner/repo", "200")
    assert json.loads((tmp_path / "basis.json").read_text()) == {"history_runs": []}
    assert not (tmp_path / "pytest.json").exists()


def test_history_uses_completed_workflow_time_and_actual_ci_weights(tmp_path, monkeypatch):
    def github(path, **kwargs):
        if "?name=" in path:
            return {"artifacts": [{"expired": False, "name": "test-feedback", "id": 7, "workflow_run": {"id": 100, "repository_id": 1, "head_repository_id": 1}}]}
        if path.endswith("/zip"):
            return _zip(_report())
        return {"id": 100, "path": ci.WORKFLOW, "event": "pull_request", "status": "completed", "conclusion": "failure", "run_attempt": 1, "created_at": "2026-09-08T10:00:00Z", "updated_at": "2026-09-08T10:00:30Z"}
    monkeypatch.setattr(ci, "github", github)
    ci.prepare(tmp_path, "owner/repo", "200")
    retained = ci.read_json(tmp_path / "history/100.json")
    assert retained["gate_elapsed_seconds"] == 30
    assert ci.read_json(tmp_path / "pytest.json") == {"tests/test_one.py": 0.5}


def test_retry_measures_its_own_attempt_instead_of_hours_since_creation():
    run = {"run_attempt": 2, "created_at": "2026-09-08T01:00:00Z", "run_started_at": "2026-09-08T10:00:00Z"}
    assert ci._elapsed(ci._attempt_start(run), "2026-09-08T10:00:30Z") == 30


def test_retry_ignores_newer_runs_and_superseded_attempt_artifacts(tmp_path, monkeypatch):
    called = []
    def github(path, **kwargs):
        called.append(path)
        if "?name=" in path:
            return {"artifacts": [{"expired": False, "name": "test-feedback", "id": run, "workflow_run": {"id": run, "repository_id": 1, "head_repository_id": 1}} for run in (300, 100)]}
        if path.endswith("/zip"):
            return _zip(_report())
        return {"id": 100, "path": ci.WORKFLOW, "event": "pull_request", "status": "completed", "conclusion": "failure", "run_attempt": 2}
    monkeypatch.setattr(ci, "github", github)
    ci.prepare(tmp_path, "owner/repo", "200")
    assert not any("/runs/300" in path for path in called)
    assert ci.read_json(tmp_path / "basis.json") == {"history_runs": []}


@pytest.mark.parametrize("defect", [None, "missing receipt", "missing history", "missing file"])
def test_finish_requires_complete_current_and_prepared_history(tmp_path, monkeypatch, defect):
    inputs, receipts, output = tmp_path / "inputs", tmp_path / "receipts", tmp_path / "report.json"
    inputs.mkdir()
    receipts.mkdir()
    (inputs / "basis.json").write_text(json.dumps({"history_runs": ["99"] if defect == "missing history" else []}))
    report = _report()
    for receipt in report["receipts"]:
        if defect == "missing receipt" and receipt["suite"] == "slow":
            continue
        (receipts / f"receipt-{receipt['suite']}-1.json").write_text(json.dumps(receipt))
    for name, value in {"PYTEST_SHARDS": "1", "SLOW_SHARDS": "1", "MIGRATION_REQUIRED": "false", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(ci, "test_files", lambda: ["tests/test_one.py"] + (["tests/test_missing.py"] if defect == "missing file" else []))
    start = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    end = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(ci, "github", lambda path: {"jobs": _jobs(start, end)} if "/jobs?" in path else {"status": "in_progress", "run_attempt": 1, "created_at": start})
    output.write_text("stale green")
    if defect:
        with pytest.raises(ci.EvidenceError):
            ci.finish(inputs, receipts, output, "owner/repo", "100")
        assert not output.exists()
    else:
        assert ci.finish(inputs, receipts, output, "owner/repo", "100") == 0
        assert ci.read_json(output)["expected"]["run_id"] == "100"


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
