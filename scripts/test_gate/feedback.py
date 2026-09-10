"""Aggregate required-CI shard receipts and assess the feedback targets.

The previous duration files needed extra local suite runs. These receipts
come from required CI itself; missing evidence fails closed. Target assessment
retains the same numbers and strict diagnostic CLI verdict. ADR-0097 makes
elapsed-time breaches advisory in the merge gate, whose correctness verdict
still requires complete successful proof. Each receipt is validated by the
type that wrote it (`receipt.ShardReceipt`); this module asks the questions
only the whole set can answer: identity, partition cover and gate time.
"""

from __future__ import annotations

import math
import statistics

from .evidence import (
    EvidenceError, require_identity, require_integer, require_object, require_seconds,
)
from .receipt import ShardReceipt


def validate_expected(expected: dict) -> None:
    require_object(expected, {"run_id", "run_attempt", "head_sha", "suites"}, "expected")
    require_identity(expected)
    suites = expected["suites"]
    if not isinstance(suites, dict) or not {"pytest", "slow"} <= set(suites):
        raise EvidenceError("a behavior report requires both pytest and slow suites")
    if set(suites) - {"pytest", "slow", "migration", "check"}:
        raise EvidenceError("expected contains an unknown suite")
    for name, shards in suites.items():
        require_integer(shards, f"{name} shard count", 1)


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
    gate_seconds = require_seconds(gate_seconds, "gate elapsed")
    if not isinstance(receipts, list):
        raise EvidenceError("receipts must be a list")
    breakdown = {} if breakdown is None else breakdown
    if not isinstance(breakdown, dict) or set(breakdown) - {
        "queue_seconds", "setup_seconds"
    }:
        raise EvidenceError("breakdown permits only queue_seconds and setup_seconds")
    for name, value in breakdown.items():
        if require_seconds(value, name) > gate_seconds:
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
    for raw in receipts:
        receipt = ShardReceipt.validate(raw)
        for field in ("run_id", "head_sha"):
            if getattr(receipt, field) != expected[field]:
                raise EvidenceError(f"receipt {field} disagrees with expected identity")
        if receipt.run_attempt > expected["run_attempt"]:
            raise EvidenceError("receipt run_attempt is newer than the expected attempt")
        if receipt.suite not in suites:
            raise EvidenceError("unexpected receipt suite")
        key = (receipt.suite, receipt.shard)
        if key not in required or receipt.shards != expected["suites"][receipt.suite]:
            raise EvidenceError("receipt shard disagrees with expected partition")
        if key in seen:
            raise EvidenceError(f"duplicate receipt: {receipt.suite}/{receipt.shard}")
        seen.add(key)
        if receipt.elapsed_seconds > gate_seconds:
            raise EvidenceError("receipt elapsed exceeds gate elapsed")
        target = suites[receipt.suite]
        target["elapsed_seconds"] = max(target["elapsed_seconds"], receipt.elapsed_seconds)
        target["test_count"] += receipt.test_count
        for name, seconds in receipt.per_file_seconds.items():
            # Files are partitioned across runners. A duplicate indicates
            # inconsistent partition inputs even when every shard arrived.
            if name in target["per_file_seconds"]:
                raise EvidenceError(f"test file appears in multiple {receipt.suite} shards: {name}")
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
    require_object(report, {"schema_version", "expected", "gate_elapsed_seconds",
                            "breakdown", "suites", "receipts"}, "report")
    if require_integer(report["schema_version"], "report schema", 1) != 1:
        raise EvidenceError("unsupported report schema")
    rebuilt = aggregate_receipts(
        report["receipts"], report["expected"], report["gate_elapsed_seconds"],
        report["breakdown"],
    )
    if not isinstance(report["suites"], dict):
        raise EvidenceError("report suites must be an object")
    for suite in report["suites"].values():
        require_object(suite, {"shards", "elapsed_seconds", "test_count", "per_file_seconds"},
                       "suite summary")
        require_integer(suite["shards"], "summary shards", 1)
        require_integer(suite["test_count"], "summary test count")
        require_seconds(suite["elapsed_seconds"], "summary elapsed")
        if not isinstance(suite["per_file_seconds"], dict):
            raise EvidenceError("summary per_file_seconds must be an object")
        for seconds in suite["per_file_seconds"].values():
            require_seconds(seconds, "summary per-file elapsed")
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
    require_object(policy, POLICY_KEYS, "policy")
    if require_integer(policy["schema_version"], "policy schema", 1) != 1:
        raise EvidenceError("unsupported policy schema")
    window = require_integer(policy["window"], "window", 3)
    minimum = require_integer(policy["minimum_samples"], "minimum_samples", 3)
    if minimum > window:
        raise EvidenceError("minimum_samples exceeds the rolling window")
    for name in ("median_seconds", "p90_seconds", "migration_seconds"):
        if require_seconds(policy[name], name) == 0:
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
    # Missing historical access must still bound actual test work. During
    # bootstrap, constrain the slowest test command rather than treating one
    # queued workflow as an end-to-end median. The five-minute ceiling remains.
    command_seconds = max(
        current["suites"][suite]["elapsed_seconds"] for suite in ("pytest", "slow")
    )
    if now >= policy["p90_seconds"]:
        failures.append(f"current gate {now:.1f}s reaches the {policy['p90_seconds']:g}s ceiling")
    if not enough and command_seconds >= policy["median_seconds"]:
        failures.append(
            f"slowest test command {command_seconds:.1f}s reaches the "
            f"{policy['median_seconds']:g}s budget while history is insufficient"
        )
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
        "current_test_command_seconds": command_seconds,
        "median_within_budget": median < policy["median_seconds"],
        "p90_within_budget": p90 < policy["p90_seconds"],
        "sample_runs": [report["expected"] for report in samples],
    }
