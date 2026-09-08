"""Validate required-CI timing receipts and enforce the feedback budget.

The previous duration files needed extra local suite runs and recorded no
enforceable runtime limit. These receipts come from the required CI runs
themselves. Missing evidence fails closed; old slow runs cannot prevent a
current under-budget repair from passing. No database or third-party package
is needed by the summary job.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any


class EvidenceError(ValueError):
    """A timing measurement cannot support a gate decision."""


def _object(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise EvidenceError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise EvidenceError(f"{label} must be an integer >= {minimum}")
    return value


def _seconds(value: Any, label: str) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise EvidenceError(f"{label} must be finite nonnegative seconds")
    return float(value)


def _identity(value: dict) -> None:
    if not isinstance(value["run_id"], str) or not re.fullmatch(
        r"[1-9][0-9]*", value["run_id"]
    ):
        raise EvidenceError("run_id must be a positive decimal string")
    _integer(value["run_attempt"], "run_attempt", 1)
    if not isinstance(value["head_sha"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", value["head_sha"]
    ):
        raise EvidenceError("head_sha must be a full lowercase commit SHA")


def validate_expected(expected: dict) -> None:
    _object(expected, {"run_id", "run_attempt", "head_sha", "suites"}, "expected")
    _identity(expected)
    suites = expected["suites"]
    if not isinstance(suites, dict) or not {"pytest", "slow"} <= set(suites):
        raise EvidenceError("a behavior report requires both pytest and slow suites")
    if set(suites) - {"pytest", "slow", "migration", "check"}:
        raise EvidenceError("expected contains an unknown suite")
    for name, shards in suites.items():
        _integer(shards, f"{name} shard count", 1)


RECEIPT_KEYS = {
    "schema_version", "run_id", "run_attempt", "head_sha", "suite", "shard",
    "shards", "elapsed_seconds", "exit_code", "test_count", "per_file_seconds",
}


def aggregate_receipts(
    receipts: list[dict], expected: dict, gate_seconds: float,
    breakdown: dict | None = None,
) -> dict:
    """Require one successful receipt for every expected suite and shard.

    A partial rerun can retain a successful earlier attempt of the same run
    and tested SHA. The CI adapter verifies each receipt's actual job attempt
    and reconstructs a gate duration that includes reused work; aggregation
    preserves the original receipt identity and refuses a smaller time basis
    than any included test command.
    """
    validate_expected(expected)
    gate_seconds = _seconds(gate_seconds, "gate elapsed")
    if not isinstance(receipts, list):
        raise EvidenceError("receipts must be a list")
    breakdown = {} if breakdown is None else breakdown
    if not isinstance(breakdown, dict) or set(breakdown) - {
        "queue_seconds", "setup_seconds"
    }:
        raise EvidenceError("breakdown permits only queue_seconds and setup_seconds")
    for name, value in breakdown.items():
        if _seconds(value, name) > gate_seconds:
            raise EvidenceError(f"{name} exceeds the gate elapsed time")

    required = {
        (suite, shard)
        for suite, shards in expected["suites"].items()
        for shard in range(1, shards + 1)
    }
    seen: set[tuple[str, int]] = set()
    suites = {
        name: {"shards": count, "elapsed_seconds": 0.0, "test_count": 0,
               "per_file_seconds": {}}
        for name, count in expected["suites"].items()
    }
    for receipt in receipts:
        _object(receipt, RECEIPT_KEYS, "receipt")
        if _integer(receipt["schema_version"], "receipt schema", 1) != 1:
            raise EvidenceError("unsupported receipt schema")
        _identity(receipt)
        for field in ("run_id", "head_sha"):
            if receipt[field] != expected[field]:
                raise EvidenceError(f"receipt {field} disagrees with expected identity")
        if receipt["run_attempt"] > expected["run_attempt"]:
            raise EvidenceError("receipt run_attempt is newer than the expected attempt")
        suite = receipt["suite"]
        if not isinstance(suite, str) or suite not in suites:
            raise EvidenceError("unexpected receipt suite")
        shard = _integer(receipt["shard"], "shard", 1)
        shards = _integer(receipt["shards"], "shards", 1)
        key = (suite, shard)
        if key not in required or shards != expected["suites"][suite]:
            raise EvidenceError("receipt shard disagrees with expected partition")
        if key in seen:
            raise EvidenceError(f"duplicate receipt: {suite}/{shard}")
        seen.add(key)
        elapsed = _seconds(receipt["elapsed_seconds"], "receipt elapsed")
        if elapsed > gate_seconds:
            raise EvidenceError("receipt elapsed exceeds gate elapsed")
        count = _integer(receipt["test_count"], "executed test count")
        status = _integer(receipt["exit_code"], "pytest exit code")
        if status != 0 and not (status == 5 and count == 0):
            raise EvidenceError("receipt does not prove successful tests")
        times = receipt["per_file_seconds"]
        if not isinstance(times, dict) or (count > 0 and not times):
            raise EvidenceError("executed tests require per-file timings")
        target = suites[suite]
        target["elapsed_seconds"] = max(target["elapsed_seconds"], elapsed)
        target["test_count"] += count
        for name, seconds in times.items():
            if not isinstance(name, str) or not re.fullmatch(r"tests/test_[^/]+\.py", name):
                raise EvidenceError("timing path is not a top-level test file")
            seconds = _seconds(seconds, f"{name} elapsed")
            if count == 0 and seconds != 0:
                raise EvidenceError("an empty shard cannot report executed test work")
            # Files are partitioned across runners. A duplicate indicates
            # inconsistent partition inputs even when every shard arrived.
            if name in target["per_file_seconds"]:
                raise EvidenceError(f"test file appears in multiple {suite} shards: {name}")
            target["per_file_seconds"][name] = seconds
    if seen != required:
        raise EvidenceError(f"missing receipts: {sorted(required - seen)}")
    if any(summary["test_count"] == 0 for summary in suites.values()):
        raise EvidenceError("every required suite must execute at least one test")
    return {
        "schema_version": 1, "expected": expected,
        "gate_elapsed_seconds": gate_seconds, "breakdown": breakdown,
        "suites": suites,
        "receipts": sorted(receipts, key=lambda item: (item["suite"], item["shard"])),
    }


def validate_report(report: dict) -> dict:
    _object(report, {"schema_version", "expected", "gate_elapsed_seconds",
                     "breakdown", "suites", "receipts"}, "report")
    if _integer(report["schema_version"], "report schema", 1) != 1:
        raise EvidenceError("unsupported report schema")
    rebuilt = aggregate_receipts(
        report["receipts"], report["expected"], report["gate_elapsed_seconds"],
        report["breakdown"],
    )
    if not isinstance(report["suites"], dict):
        raise EvidenceError("report suites must be an object")
    for suite in report["suites"].values():
        _object(suite, {"shards", "elapsed_seconds", "test_count", "per_file_seconds"},
                "suite summary")
        _integer(suite["shards"], "summary shards", 1)
        _integer(suite["test_count"], "summary test count")
        _seconds(suite["elapsed_seconds"], "summary elapsed")
        if not isinstance(suite["per_file_seconds"], dict):
            raise EvidenceError("summary per_file_seconds must be an object")
        for seconds in suite["per_file_seconds"].values():
            _seconds(seconds, "summary per-file elapsed")
    if rebuilt != report:
        raise EvidenceError("report summary disagrees with its receipts")
    return rebuilt


POLICY_KEYS = {
    "schema_version", "window", "minimum_samples", "median_seconds",
    "p90_seconds", "migration_seconds",
}


def assess(current: dict, history: list[dict], policy: dict) -> dict:
    """Enforce today's ceiling and a sustained median across distinct heads.

    All complete behavior evidence is eligible, including runs rejected only
    by this performance policy. Keeping only green performance results would
    make the very regressions being measured disappear from the history.
    """
    current = validate_report(current)
    _object(policy, POLICY_KEYS, "policy")
    if _integer(policy["schema_version"], "policy schema", 1) != 1:
        raise EvidenceError("unsupported policy schema")
    window = _integer(policy["window"], "window", 3)
    minimum = _integer(policy["minimum_samples"], "minimum_samples", 3)
    if minimum > window:
        raise EvidenceError("minimum_samples exceeds the rolling window")
    for name in ("median_seconds", "p90_seconds", "migration_seconds"):
        if _seconds(policy[name], name) == 0:
            raise EvidenceError(f"{name} must be positive")
    if policy["median_seconds"] > policy["p90_seconds"]:
        raise EvidenceError("median budget exceeds p90 budget")

    identity = current["expected"]
    current_key = (int(identity["run_id"]), identity["run_attempt"])
    unique: dict[str, dict] = {identity["head_sha"]: current}
    identities: set[tuple[int, int]] = {current_key}
    for raw in history:
        report = validate_report(raw)
        recorded = report["expected"]
        key = (int(recorded["run_id"]), recorded["run_attempt"])
        if key in identities:
            raise EvidenceError("duplicate run identity in timing history")
        identities.add(key)
        if key >= current_key:
            raise EvidenceError("history contains a run newer than the current run")
        sha = recorded["head_sha"]
        previous = unique.get(sha)
        if previous is None or key > (
            int(previous["expected"]["run_id"]), previous["expected"]["run_attempt"]
        ):
            unique[sha] = report
    samples = sorted(unique.values(), key=lambda report: (
        int(report["expected"]["run_id"]), report["expected"]["run_attempt"]
    ), reverse=True)[:window]
    times = sorted(report["gate_elapsed_seconds"] for report in samples)
    median = statistics.median(times)
    p90 = times[math.ceil(0.9 * len(times)) - 1]
    enough = len(times) >= minimum
    now = current["gate_elapsed_seconds"]
    failures = []
    # The five-minute ceiling also applies while the median is warming up;
    # a new history must not license twenty-minute checks.
    if now >= policy["p90_seconds"]:
        failures.append(f"current gate {now:.1f}s reaches the {policy['p90_seconds']:g}s ceiling")
    if enough and median >= policy["median_seconds"] and now >= policy["median_seconds"]:
        failures.append(
            f"rolling median {median:.1f}s and current {now:.1f}s reach the "
            f"{policy['median_seconds']:g}s budget"
        )
    migration = current["suites"].get("migration")
    if migration and migration["elapsed_seconds"] >= policy["migration_seconds"]:
        failures.append(
            f"migration {migration['elapsed_seconds']:.1f}s reaches the "
            f"{policy['migration_seconds']:g}s budget"
        )
    return {
        "passed": not failures, "failures": failures,
        "sample_count": len(times), "minimum_samples": minimum,
        "history_status": "sufficient" if enough else "insufficient_history",
        "current_seconds": now, "median_seconds": median, "p90_seconds": p90,
        "median_within_budget": median < policy["median_seconds"],
        "p90_within_budget": p90 < policy["p90_seconds"],
        "sample_runs": [report["expected"] for report in samples],
    }


def read_json(path: Path) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvidenceError(f"duplicate JSON field {key} in {path}")
            result[key] = value
        return result

    def constant(value):
        raise EvidenceError(f"non-finite JSON number {value} in {path}")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=constant)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--receipts", type=Path, required=True)
    aggregate.add_argument("--expected", type=Path, required=True)
    aggregate.add_argument("--gate-seconds", type=float, required=True)
    aggregate.add_argument("--queue-seconds", type=float)
    aggregate.add_argument("--setup-seconds", type=float)
    aggregate.add_argument("--output", type=Path, required=True)
    assessment = commands.add_parser("assess")
    assessment.add_argument("--current", type=Path, required=True)
    assessment.add_argument("--history", type=Path, required=True)
    assessment.add_argument("--policy", type=Path, required=True)
    assessment.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        # A failed retry must not leave an older successful report available
        # to an always-running artifact upload step.
        if arguments.output is not None:
            arguments.output.unlink(missing_ok=True)
        if arguments.command == "aggregate":
            if not arguments.receipts.is_dir():
                raise EvidenceError("receipt directory is absent")
            result = aggregate_receipts(
                [read_json(path) for path in sorted(arguments.receipts.rglob("*.json"))],
                read_json(arguments.expected), arguments.gate_seconds,
                {name: getattr(arguments, name) for name in ("queue_seconds", "setup_seconds")
                 if getattr(arguments, name) is not None},
            )
        else:
            if not arguments.history.is_dir():
                raise EvidenceError("history directory is absent; create it explicitly for bootstrap")
            result = assess(
                read_json(arguments.current),
                [read_json(path) for path in sorted(arguments.history.rglob("*.json"))],
                read_json(arguments.policy),
            )
        rendered = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if arguments.output is not None:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if result.get("passed", True) else 1
    except (EvidenceError, OSError, ValueError, TypeError, KeyError) as error:
        print(f"invalid timing evidence: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
