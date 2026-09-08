"""Reuse completed CI measurements and enforce feedback on the required run.

Only artifacts from this repository's completed release-gate runs supply
weights. They are inert, validated JSON, never executable configuration or
a test-selection list. A missing historical measurement uses the checked-in
starting weights; unavailable or corrupt current proof fails the gate.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

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


def report_from_zip(data: bytes) -> dict:
    if len(data) > 10_000_000:
        raise EvidenceError("timing artifact exceeds its size bound")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != "report.json" or entries[0].file_size > 10_000_000:
            raise EvidenceError("timing artifact must contain only a bounded report.json")
        return validate_report(json.loads(archive.read(entries[0])))


def prepare(directory: Path, repository: str, run_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    history = directory / "history"
    history.mkdir(exist_ok=True)
    # The same job may be retried. Its previous partial download is not proof.
    for path in history.glob("*.json"):
        path.unlink()
    for suite in ("pytest", "slow"):
        (directory / f"{suite}.json").unlink(missing_ok=True)
    artifacts = github(f"repos/{repository}/actions/artifacts?name=test-feedback&per_page=30")["artifacts"]
    candidates = [item for item in artifacts if (
        not item["expired"] and item["name"] == "test-feedback"
        and int(item["workflow_run"]["id"]) < int(run_id)
        and item["workflow_run"]["repository_id"] == item["workflow_run"]["head_repository_id"]
    )][:10]

    def load(item: dict) -> dict | None:
        metadata = item["workflow_run"]
        run = github(f"repos/{repository}/actions/runs/{metadata['id']}")
        if run["path"].split("@")[0] != WORKFLOW or run["event"] != "pull_request" or run["status"] != "completed" or run["conclusion"] not in ("success", "failure"):
            return None
        try:
            report = report_from_zip(github(f"repos/{repository}/actions/artifacts/{item['id']}/zip", binary=True))
        except (EvidenceError, ValueError, zipfile.BadZipFile) as error:
            print(f"Ignoring unusable historical timing artifact {item['id']}: {error}")
            return None
        if report["expected"]["run_id"] != str(run["id"]):
            raise EvidenceError("historical artifact disagrees with its GitHub run")
        if report["expected"]["run_attempt"] != run["run_attempt"]:
            print(f"Ignoring superseded attempt timing for run {run['id']}")
            return None
        # Once completed, GitHub can supply the last few seconds the original
        # summary could not yet observe (artifact upload and job teardown).
        return aggregate_receipts(report["receipts"], report["expected"],
                                  max(report["gate_elapsed_seconds"], _elapsed(_attempt_start(run), run["updated_at"])), report["breakdown"])

    with ThreadPoolExecutor(max_workers=4) as executor:
        reports = [report for report in executor.map(load, candidates) if report is not None]
    reports.sort(key=lambda item: (int(item["expected"]["run_id"]), item["expected"]["run_attempt"]), reverse=True)
    for report in reports:
        (history / f"{report['expected']['run_id']}.json").write_text(json.dumps(report, sort_keys=True) + "\n")
    if reports:
        for suite in ("pytest", "slow"):
            (directory / f"{suite}.json").write_text(json.dumps(reports[0]["suites"][suite]["per_file_seconds"], sort_keys=True) + "\n")
        print(f"CI partition weights from run {reports[0]['expected']['run_id']}; {len(reports)} completed measurements")
    else:
        print("No completed feedback history yet; using checked-in starting weights")
    # Keep the initial artifact nonempty, and make bootstrap visible.
    (directory / "basis.json").write_text(json.dumps({"history_runs": [report["expected"]["run_id"] for report in reports]}) + "\n")


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


def finish(inputs: Path, receipts_dir: Path, output: Path, repository: str, run_id: str) -> int:
    output.unlink(missing_ok=True)
    basis = read_json(inputs / "basis.json")
    if not isinstance(basis, dict) or set(basis) != {"history_runs"} or not isinstance(basis["history_runs"], list):
        raise EvidenceError("the timing input basis is malformed")
    suites = {"pytest": int(os.environ["PYTEST_SHARDS"]), "slow": int(os.environ["SLOW_SHARDS"])}
    if os.environ["MIGRATION_REQUIRED"] == "true":
        suites["migration"] = 1
    elif os.environ["MIGRATION_REQUIRED"] != "false":
        raise EvidenceError("migration requirement must be true or false")
    expected = {"run_id": run_id, "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]), "head_sha": os.environ["GITHUB_SHA"], "suites": suites}
    run = github(f"repos/{repository}/actions/runs/{run_id}")
    if run["status"] != "in_progress" or run["run_attempt"] != expected["run_attempt"]:
        raise EvidenceError("feedback is not observing the current running attempt")
    receipts = [read_json(path) for path in sorted(receipts_dir.rglob("receipt-*.json"))]
    jobs = []
    for page in range(1, 6):
        batch = github(f"repos/{repository}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}")["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise EvidenceError("workflow attempt history exceeds the bounded job inventory")
    now = datetime.now(timezone.utc).isoformat()
    report = aggregate_receipts(receipts, expected, verified_gate_seconds(receipts, run, jobs, now))
    required_files = set(test_files())
    for suite in ("pytest", "slow"):
        if set(report["suites"][suite]["per_file_seconds"]) != required_files:
            raise EvidenceError(f"{suite} receipts do not cover every current test file")
    history = [read_json(path) for path in sorted((inputs / "history").glob("*.json"))]
    if sorted(item["expected"]["run_id"] for item in history) != sorted(basis["history_runs"]):
        raise EvidenceError("the prepared timing history is incomplete")
    result = assess(report, history, read_json(ROOT / "tests/feedback-budget.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    summary = ["### Test feedback", "", f"Current gate: **{report['gate_elapsed_seconds']:.1f}s** (through this decision).", "", "```json", json.dumps(result, indent=2, sort_keys=True), "```", "", "| Suite | Executed tests | Slowest test command |", "|---|---:|---:|"]
    for name, measured in report["suites"].items():
        summary.append(f"| {name} | {measured['test_count']} | {measured['elapsed_seconds']:.1f}s |")
    summary += ["", "Timings are reused by the next required run; no separate local timing run is required."]
    rendered = "\n".join(summary) + "\n"
    print(rendered)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(rendered)
    return 0 if result["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--directory", type=Path, required=True)
    finishing = commands.add_parser("finish")
    finishing.add_argument("--input", type=Path, required=True)
    finishing.add_argument("--receipts", type=Path, required=True)
    finishing.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        repository, run_id = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"]
        if args.command == "prepare":
            prepare(args.directory, repository, run_id)
            return 0
        return finish(args.input, args.receipts, args.output, repository, run_id)
    except (EvidenceError, ValueError, KeyError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print(f"::error::test feedback failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
