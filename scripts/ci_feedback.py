"""Reuse completed CI measurements and enforce feedback on the required run.

Only completed release-gate job logs from this repository supply weights. They are inert, validated JSON, never executable configuration or
a test-selection list. A missing historical measurement uses the checked-in
starting weights; unavailable or corrupt current proof fails the gate.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.test_feedback import EvidenceError, aggregate_receipts, assess, read_json, validate_report
from scripts.test_shard import test_files


WORKFLOW = ".github/workflows/release-gate.yml"


def github(path: str, *, binary: bool = False):
    result = subprocess.run(["gh", "api", "--method", "GET", path], capture_output=True, check=True, timeout=30)
    return result.stdout if binary else json.loads(result.stdout)


def _elapsed(start: str, end: str) -> float:
    return (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def _attempt_start(run: dict) -> str:
    # created_at belongs to the original run, even when an attempt is retried
    # hours later. A retry must measure its own execution, not that idle gap.
    return run["created_at"] if run["run_attempt"] == 1 else run["run_started_at"]


RECEIPT_MARKER = "CORRIDOR_TEST_RECEIPT"
REPORT_MARKER = "CORRIDOR_TEST_FEEDBACK"


def logged_json(log: bytes, marker: str) -> dict | None:
    """Read the final machine record, never pytest's human summary text."""
    if len(log) > 20_000_000:
        raise EvidenceError("job log exceeds its bounded size")
    payload = None
    for line in log.decode("utf-8-sig").splitlines():
        if line.startswith(marker + " "):
            payload = line[len(marker) + 1:]
        elif " " + marker + " " in line:
            payload = line.split(" " + marker + " ", 1)[1]
    if payload is None:
        return None
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise EvidenceError("duplicate field in logged timing record")
            value[key] = item
        return value
    def invalid(_value):
        raise EvidenceError("non-finite logged timing value")
    return json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid)


def run_jobs(repository: str, run_id: str) -> list[dict]:
    jobs = []
    for page in range(1, 6):
        batch = github(f"repos/{repository}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}")["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            return jobs
    raise EvidenceError("workflow attempt history exceeds the bounded job inventory")


def previous_reports(repository: str, run_id: str) -> list[dict]:
    runs = github(f"repos/{repository}/actions/workflows/release-gate.yml/runs?event=pull_request&status=completed&per_page=10")["workflow_runs"]
    candidates = [run for run in runs if (
        int(run["id"]) < int(run_id)
        and run["conclusion"] in ("success", "failure")
        and run.get("head_repository", {}).get("id") == run["repository"]["id"]
    )]
    def load(run):
        summaries = [job for job in run_jobs(repository, str(run["id"])) if job["name"] == "release-gate" and job["run_attempt"] == run["run_attempt"]]
        if len(summaries) != 1 or not any(step["name"] == "Enforce the measured feedback budget" and step["conclusion"] in ("success", "failure") for step in summaries[0].get("steps", [])):
            return None
        log = github(f"repos/{repository}/actions/jobs/{summaries[0]['id']}/logs", binary=True)
        try:
            data = logged_json(log, REPORT_MARKER)
            if data is None:
                return None
            report = validate_report(data)
        except (EvidenceError, ValueError) as error:
            print(f"Ignoring unusable historical timing for run {run['id']}: {error}")
            return None
        if report["expected"]["run_id"] != str(run["id"]) or report["expected"]["run_attempt"] != run["run_attempt"]:
            raise EvidenceError("historical timing disagrees with its GitHub job")
        return aggregate_receipts(report["receipts"], report["expected"], max(report["gate_elapsed_seconds"], _elapsed(_attempt_start(run), run["updated_at"])), report["breakdown"])
    with ThreadPoolExecutor(max_workers=4) as executor:
        reports = [report for report in executor.map(load, candidates) if report is not None]
    return sorted(reports, key=lambda report: (int(report["expected"]["run_id"]), report["expected"]["run_attempt"]), reverse=True)


def prepare(repository: str, run_id: str) -> None:
    reports = previous_reports(repository, run_id)
    weights = {suite: reports[0]["suites"][suite]["per_file_seconds"] for suite in ("pytest", "slow")} if reports else {}
    encoded = json.dumps(weights, separators=(",", ":"), allow_nan=False)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("timing_weights=" + encoded + "\n")
    print(f"Prepared one shared timing snapshot from {len(reports)} completed runs; " + ("using measured CI weights" if reports else "using checked-in bootstrap weights"))


def verified_gate_seconds(receipts: list[dict], run: dict, jobs: list[dict], now: str) -> float:
    """Reusing successful jobs must preserve their measured cost and identity.

    GitHub can rerun only failed jobs. Their siblings' successful receipts
    remain valid on the same tested SHA, but a summary-only retry must not
    turn a slow suite into a ten-second measurement. Reconstruct its required
    critical path from GitHub's job timings as a floor on the current attempt.
    """
    latest = {}
    for job in jobs:
        if job["name"] not in latest or job["run_attempt"] > latest[job["name"]]["run_attempt"]:
            latest[job["name"]] = job
    required = ["check"]
    for receipt in receipts:
        name = f"{receipt['suite']} ({receipt['shard']})" if receipt["suite"] in ("pytest", "slow") else receipt["suite"]
        job = latest.get(name)
        if job is None or job["conclusion"] != "success" or job["run_attempt"] != receipt["run_attempt"]:
            raise EvidenceError(f"receipt does not match the successful GitHub job: {name}")
        if receipt["elapsed_seconds"] > _elapsed(job["started_at"], job["completed_at"]) + 1:
            raise EvidenceError(f"receipt duration exceeds its GitHub job: {name}")
        required.append(name)
    if any(name not in latest or latest[name]["conclusion"] != "success" for name in ["classify", *required]):
        raise EvidenceError("required GitHub job proof is incomplete")
    summary = latest.get("release-gate")
    if summary is None or summary["run_attempt"] != run["run_attempt"]:
        raise EvidenceError("current summary job is absent")
    classify_seconds = _elapsed(latest["classify"]["started_at"], latest["classify"]["completed_at"])
    check_seconds = _elapsed(latest["check"]["started_at"], latest["check"]["completed_at"])
    behavior_seconds = max(_elapsed(latest[name]["started_at"], latest[name]["completed_at"]) for name in required if name != "check")
    cost_floor = max(check_seconds, classify_seconds + behavior_seconds) + _elapsed(summary["started_at"], now)
    return max(_elapsed(_attempt_start(run), now), cost_floor)


def finish(repository: str, run_id: str) -> int:
    suites = {"pytest": int(os.environ["PYTEST_SHARDS"]), "slow": int(os.environ["SLOW_SHARDS"])}
    if os.environ["MIGRATION_REQUIRED"] == "true":
        suites["migration"] = 1
    elif os.environ["MIGRATION_REQUIRED"] != "false":
        raise EvidenceError("migration requirement must be true or false")
    expected = {"run_id": run_id, "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]), "head_sha": os.environ["GITHUB_SHA"], "suites": suites}
    run = github(f"repos/{repository}/actions/runs/{run_id}")
    if run["status"] != "in_progress" or run["run_attempt"] != expected["run_attempt"]:
        raise EvidenceError("feedback is not observing the current running attempt")
    jobs = run_jobs(repository, run_id)
    latest = {}
    for job in jobs:
        if job["name"] not in latest or job["run_attempt"] > latest[job["name"]]["run_attempt"]:
            latest[job["name"]] = job
    names = [f"{suite} ({number})" if suite in ("pytest", "slow") else suite for suite, shards in suites.items() for number in range(1, shards + 1)]
    if any(name not in latest or latest[name]["conclusion"] != "success" for name in names):
        raise EvidenceError("required test jobs did not all succeed")
    def receipt(name):
        log = github(f"repos/{repository}/actions/jobs/{latest[name]['id']}/logs", binary=True)
        value = logged_json(log, RECEIPT_MARKER)
        if value is None:
            raise EvidenceError(f"successful job has no timing receipt: {name}")
        return value
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(receipt, names))
    history = previous_reports(repository, run_id)
    now = datetime.now(timezone.utc).isoformat()
    report = aggregate_receipts(receipts, expected, verified_gate_seconds(receipts, run, jobs, now))
    required_files = set(test_files())
    for suite in ("pytest", "slow"):
        if set(report["suites"][suite]["per_file_seconds"]) != required_files:
            raise EvidenceError(f"{suite} receipts do not cover every current test file")
    result = assess(report, history, read_json(ROOT / "tests/feedback-budget.json"))
    # Logs already belong to this workflow. No subsequent upload can turn a
    # successful test job into a failure and force its tests to run again.
    print(REPORT_MARKER + " " + json.dumps(report, separators=(",", ":"), allow_nan=False), flush=True)
    summary = ["### Test feedback", "", f"Current gate: **{report['gate_elapsed_seconds']:.1f}s** (through this decision).", "", "```json", json.dumps(result, indent=2, sort_keys=True), "```", "", "| Suite | Executed tests | Slowest test command |", "|---|---:|---:|"]
    for name, measured in report["suites"].items():
        summary.append(f"| {name} | {measured['test_count']} | {measured['elapsed_seconds']:.1f}s |")
    rendered = "\n".join(summary) + "\n"
    print(rendered)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(rendered)
    return 0 if result["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "finish"))
    args = parser.parse_args(argv)
    try:
        repository, run_id = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"]
        if args.command == "prepare":
            prepare(repository, run_id)
            return 0
        return finish(repository, run_id)
    except (EvidenceError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"::error::test feedback failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
