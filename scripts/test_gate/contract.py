"""The strings the release-gate workflow and its scripts agree on.

`.github/workflows/release-gate.yml` names its matrix jobs `pytest (N)` and
`slow (N)`, publishes each shard's receipt in a step output named `pytest_N`,
and the summary job reads those outputs back. The same names were built by
hand in two places in `ci_feedback.py`, and the receipt marker line the gate
prints was matched by the adapter that later reads job logs.
`tests/test_ci_policy.py` asserts the workflow side of this contract.
"""

from __future__ import annotations

# The final machine record a job prints; log readers take the last one.
RECEIPT_MARKER = "CORRIDOR_TEST_RECEIPT"
REPORT_MARKER = "CORRIDOR_TEST_FEEDBACK"

# Suites that run as a matrix; their GitHub job names carry the shard number.
MATRIX_SUITES = ("pytest", "slow")


def job_name(suite: str, shard: int) -> str:
    return f"{suite} ({shard})" if suite in MATRIX_SUITES else suite


def output_slot(suite: str, shard: int) -> str:
    return f"{suite}_{shard}"


def latest_jobs(jobs: list[dict]) -> dict[str, dict]:
    """The newest attempt of every job name in a run's job inventory."""
    latest: dict[str, dict] = {}
    for job in jobs:
        if job["name"] not in latest or job["run_attempt"] > latest[job["name"]]["run_attempt"]:
            latest[job["name"]] = job
    return latest
