"""Subprocess-contained incumbent bake-off runner and compact reporter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contract import ContractError, repeatability_digest, validate_result
from evaluators import compare, validate_capability_claims
from fixtures import generate
from registry import REAL_ENGINES

HERE = Path(__file__).parent
ISOLATED_PYTHON = HERE / ".venv/bin/python"


def run_subprocess(request: dict[str, Any], *, timeout: float = 60,
                   worker: Path = HERE / "worker.py", repetition: int = 1,
                   run_order: int = 1) -> dict[str, Any]:
    """Return a receipt or a contained crash/timeout/protocol failure."""
    if not ISOLATED_PYTHON.is_file() or not os.access(ISOLATED_PYTHON, os.X_OK):
        raise RuntimeError(
            f"isolated Python interpreter is missing or not executable: {ISOLATED_PYTHON}; "
            "create it with `uv sync --project experiments/pdf-engine-bakeoff --frozen --no-dev`"
        )
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(ISOLATED_PYTHON), str(worker)], input=json.dumps(request), text=True,
            capture_output=True, timeout=timeout, cwd=HERE, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"containment_status": "timeout", "error_class": "timeout", "message": "adapter exceeded deadline"}
    elapsed = (time.perf_counter() - started) * 1000
    if completed.returncode:
        return {"containment_status": "crash", "error_class": "native_crash", "returncode": completed.returncode, "message": "adapter process exited without a result"}
    try:
        envelope = json.loads(completed.stdout)
        deterministic = envelope["deterministic_output"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return {"containment_status": "malformed_stdout", "error_class": "protocol", "message": "adapter stdout was not one result"}
    receipt = {
        "schema_version": "corridor.pdf-engine-result.v1",
        "deterministic_output": deterministic,
        "repeatability_sha256": repeatability_digest(deterministic),
        "run_observation": {
            "host": _host(), "cgroup": _cgroup(), "run_order": run_order,
            "mode": "process-cold", "thread_mode": "single", "repetition": repetition,
            "started_at": started_at,
            "process_start_ms": round(max(0, elapsed - envelope["import_ms"] - sum(envelope["operation_ms"].values())), 6),
            "import_ms": envelope["import_ms"],
            "operation_latency_ms": round(sum(envelope["operation_ms"].values()), 6),
            "peak_rss_bytes": max(0, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024),
        },
        "operation_observation": {"latency_ms": envelope["operation_ms"], "output_bytes": envelope["output_bytes"]},
    }
    # The operation observation belongs beside, not inside, the strict receipt.
    strict = {key: value for key, value in receipt.items() if key != "operation_observation"}
    validate_result(strict)
    return receipt


def _host() -> dict[str, Any]:
    import pymupdf
    memory_limit = _read_int("/sys/fs/cgroup/memory.max") or 2**63 - 1
    return {"hostname": platform.node() or "unknown", "os": platform.platform(),
            "architecture": platform.machine(), "cpu": platform.processor() or "unknown",
            "cpu_quota": _read("/sys/fs/cgroup/cpu.max") or "unknown",
            "memory_limit_bytes": memory_limit, "python_version": platform.python_version(),
            "package_versions": {"PyMuPDF": pymupdf.version[0], "MuPDF": pymupdf.version[1]}}


def _read(path: str) -> str | None:
    try: return Path(path).read_text().strip()
    except OSError: return None


def _read_int(path: str) -> int | None:
    value = _read(path)
    return int(value) if value and value.isdigit() else None


def _cgroup() -> dict[str, Any]:
    return {name: _read(path) for name, path in (("cpu.max", "/sys/fs/cgroup/cpu.max"), ("memory.max", "/sys/fs/cgroup/memory.max"))}


def run(output: Path) -> int:
    fixtures_dir = output / "fixtures"; definitions = generate(fixtures_dir)
    receipts_dir = output / "receipts"; receipts_dir.mkdir(parents=True, exist_ok=True)
    comparisons, observations = [], []
    for order, (name, options) in enumerate(sorted(definitions.items()), 1):
        source = fixtures_dir / name; digest = hashlib.sha256(source.read_bytes()).hexdigest()
        request = {"engine": "pymupdf", "source": str(source), "source_sha256": digest,
                   "password": options.get("password"), "clip": options.get("clip")}
        pair = [run_subprocess(request, repetition=i, run_order=order * 2 + i) for i in (1, 2)]
        for index, receipt in enumerate(pair, 1):
            (receipts_dir / f"{source.stem}-{index}.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            if "operation_observation" in receipt: observations.append({"fixture": name, "repetition": index, **receipt["operation_observation"], "run": receipt["run_observation"]})
        if all(item.get("schema_version") for item in pair):
            comparisons.append({"fixture": name, **compare(pair[0], pair[1]), "capability_errors": validate_capability_claims(pair[0]) + validate_capability_claims(pair[1])})
        else:
            comparisons.append({"fixture": name, "equal": False, "contained_failures": pair})
    summary = {"schema_version": "corridor.pdf-engine-bakeoff.v1", "real_engines": list(REAL_ENGINES), "comparisons": comparisons, "observations": observations}
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    passed = all(item["equal"] and not item.get("capability_errors") for item in comparisons)
    lines = ["# PDF engine bake-off — PyMuPDF self-comparison", "", f"Result: **{'PASS' if passed else 'FAIL'}**", "", "Performance is observational and excluded from deterministic digests.", "", "| Fixture | Equal |", "|---|---:|"]
    lines.extend(f"| `{item['fixture']}` | {'yes' if item['equal'] else 'no'} |" for item in comparisons)
    (output / "report.md").write_text("\n".join(lines) + "\n")
    print(output / "summary.json"); print(output / "report.md")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, default=Path("out/pdf-engine-bakeoff")); args = parser.parse_args(argv)
    return run(args.output)


if __name__ == "__main__": raise SystemExit(main())
