"""Run one required test partition and retain its actual timing evidence.

The required test run is also the timing run. Previously a separate full
local run maintained the weights, and its corpus and hardware did not match
CI. Receipts preserve pytest's failure, the exact partition and tested SHA;
the summary job checks their completeness before using them for feedback.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.test_shard import DURATIONS, SLOW_DURATIONS, recorded_seconds, shard, test_files


CHECK_OWNED_FILES = (
    "tests/test_architecture.py",
    "tests/test_source_scan_support.py",
)


def partition(suite: str, shards: int, number: int, inputs: Path) -> list[str]:
    if not 1 <= number <= shards:
        raise ValueError("shard must be within 1..shards")
    if suite == "migration":
        if (shards, number) != (1, 1):
            raise ValueError("migration has one partition")
        return ["tests/test_migration_baseline.py"]
    shared = os.environ.get("CORRIDOR_CI_WEIGHTS")
    if os.environ.get("GITHUB_ACTIONS") == "true" and not shared:
        raise ValueError("CI did not provide its shared timing snapshot")
    incoming = json.loads(shared) if shared else {}
    if not isinstance(incoming, dict) or set(incoming) - {"pytest", "slow"}:
        raise ValueError("shared timing snapshot is malformed")
    weights = incoming.get(suite, recorded_seconds(DURATIONS if suite == "pytest" else SLOW_DURATIONS))
    if not isinstance(weights, dict) or any(
        not isinstance(name, str) or type(value) not in (float, int)
        or not math.isfinite(value) or value < 0 for name, value in weights.items()
    ):
        raise ValueError("partition weights must be finite nonnegative seconds")
    if suite == "pytest":
        weights.update({name: 0.0 for name in CHECK_OWNED_FILES})
    return shard(test_files(), weights, shards)[number - 1]


def pytest_command(suite: str, files: list[str], workers: int, junit: Path) -> list[str]:
    if workers < 1:
        raise ValueError("workers must be positive")
    command = [sys.executable, "-m", "pytest"]
    if suite != "migration":
        command += ["-n", str(workers), "--dist", "worksteal" if suite == "pytest" else "loadfile"]
    else:
        # Each migration case owns a distinct disposable database, so fresh
        # installation and supported-upgrade proofs can run independently.
        command += ["-n", str(workers), "--dist", "worksteal"]
    if suite == "slow":
        # partition() already puts the measured expensive files first.
        # xdist's default scope sort uses case counts instead, postponing a
        # costly one-case replay behind cheap many-case files.
        command.append("--no-loadscope-reorder")
    command += ["-m", {"pytest": "not slow", "slow": "slow and not migration", "migration": "migration"}[suite]]
    return command + ["--durations=25", "--durations-min=1.0", f"--junitxml={junit}", *files]


def measured_cases(junit: Path, assigned: list[str]) -> tuple[int, dict[str, float]]:
    totals = dict.fromkeys(assigned, 0.0)
    count = 0
    for case in ElementTree.parse(junit).iter("testcase"):
        name = case.get("file")
        if name is None:
            match = re.match(r"tests\.(test_[^.]+)(?:\.|$)", case.get("classname", ""))
            if match is None:
                raise ValueError("JUnit case does not name a top-level test file")
            name = f"tests/{match[1]}.py"
        if name not in totals:
            raise ValueError(f"JUnit case is outside its assigned partition: {name}")
        if case.find("skipped") is not None:
            continue
        seconds = float(case.get("time", "0"))
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("JUnit duration must be finite and nonnegative")
        totals[name] += seconds
        count += 1
    return count, totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("pytest", "slow", "migration"), required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--input", type=Path, default=ROOT / "out/test-feedback-input")
    parser.add_argument("--output", type=Path, default=ROOT / "out/test-feedback")
    args = parser.parse_args(argv)
    assigned = partition(args.suite, args.shards, args.shard, args.input)
    files = [name for name in assigned if args.suite != "pytest" or name not in CHECK_OWNED_FILES]
    args.output.mkdir(parents=True, exist_ok=True)
    junit = args.output / f"{args.suite}-{args.shard}.xml"
    # An interrupted earlier invocation must never provide this run's proof.
    junit.unlink(missing_ok=True)
    receipt_path = args.output / f"receipt-{args.suite}-{args.shard}.json"
    receipt_path.unlink(missing_ok=True)
    child_environment = os.environ.copy()
    # Only this parent publishes the authoritative receipt. Tests must not
    # accidentally write fake fixture outputs into the current Actions step.
    for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_ENV", "GITHUB_PATH"):
        child_environment.pop(name, None)
    started = time.monotonic()
    status = subprocess.run(pytest_command(args.suite, files, args.workers, junit), cwd=ROOT, env=child_environment).returncode if files else 5
    elapsed = time.monotonic() - started
    if junit.exists():
        count, totals = measured_cases(junit, assigned)
    elif status == 5 and not files:
        count, totals = 0, dict.fromkeys(assigned, 0.0)
    else:
        print("pytest produced no timing receipt; required evidence is missing", file=sys.stderr)
        return status or 1
    tested_sha = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    receipt = {
        "schema_version": 1,
        "run_id": os.environ.get("GITHUB_RUN_ID", "1"),
        "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
        "head_sha": tested_sha,
        "suite": args.suite, "shard": args.shard, "shards": args.shards,
        "elapsed_seconds": elapsed, "exit_code": status,
        "test_count": count, "per_file_seconds": totals,
    }
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    encoded = json.dumps(receipt, separators=(",", ":"), allow_nan=False)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"{args.suite}_{args.shard}={encoded}\n")
    print("CORRIDOR_TEST_RECEIPT " + encoded, flush=True)
    return 0 if status == 5 and count == 0 else status


if __name__ == "__main__":
    raise SystemExit(main())
