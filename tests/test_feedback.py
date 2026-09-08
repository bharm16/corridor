"""The CI feedback guard rejects incomplete proof and sustained regressions.

Synthetic receipts cover the decision without running a database or waiting
for a slow suite. The actual workflow produces the same public CLI inputs.
"""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "corridor_test_feedback", ROOT / "scripts" / "test_feedback.py"
)
feedback = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feedback)
POLICY = json.loads((ROOT / "tests" / "feedback-budget.json").read_text())


def _expected(run=100, *, sha=None, migration=False, shards=1):
    return {
        "run_id": str(run), "run_attempt": 1,
        "head_sha": sha or f"{run:040x}",
        "suites": {"pytest": shards, "slow": shards,
                   **({"migration": 1} if migration else {})},
    }


def _receipts(expected, *, elapsed=10):
    return [
        {
            "schema_version": 1,
            **{name: expected[name] for name in ("run_id", "run_attempt", "head_sha")},
            "suite": suite, "shard": shard, "shards": shards,
            "elapsed_seconds": elapsed, "exit_code": 0, "test_count": 2,
            "per_file_seconds": {f"tests/test_part_{shard}.py": 5.0},
        }
        for suite, shards in expected["suites"].items()
        for shard in range(1, shards + 1)
    ]


def _report(seconds=100, run=100, *, sha=None, migration_seconds=None):
    expected = _expected(run, sha=sha, migration=migration_seconds is not None)
    receipts = _receipts(expected)
    if migration_seconds is not None:
        next(item for item in receipts if item["suite"] == "migration")[
            "elapsed_seconds"
        ] = migration_seconds
    return feedback.aggregate_receipts(receipts, expected, seconds)


def test_policy_keeps_the_existing_budget_numbers():
    assert POLICY["median_seconds"] == 180
    assert POLICY["p90_seconds"] == 300
    assert POLICY["migration_seconds"] == 45
    assert POLICY["minimum_samples"] == 5
    assert POLICY["window"] == 10


def test_aggregate_preserves_wall_time_and_work_time_as_different_numbers():
    expected = _expected(shards=2)
    receipts = _receipts(expected)
    receipts[0]["elapsed_seconds"] = 20
    receipts[0]["per_file_seconds"]["tests/test_part_1.py"] = 25
    report = feedback.aggregate_receipts(
        receipts, expected, 50, {"queue_seconds": 2, "setup_seconds": 5}
    )
    assert report["suites"]["pytest"] == {
        "shards": 2, "elapsed_seconds": 20.0, "test_count": 4,
        "per_file_seconds": {"tests/test_part_1.py": 25.0, "tests/test_part_2.py": 5.0},
    }
    assert report["gate_elapsed_seconds"] == 50
    assert report["breakdown"] == {"queue_seconds": 2, "setup_seconds": 5}
    assert feedback.validate_report(report) == report


@pytest.mark.parametrize("change", [
    lambda rows: rows.pop(),
    lambda rows: rows.append(deepcopy(rows[0])),
    lambda rows: rows[0].update(run_id="99"),
    lambda rows: rows[0].update(run_attempt=2),
    lambda rows: rows[0].update(head_sha="0" * 40),
    lambda rows: rows[0].update(suite="other"),
    lambda rows: rows[0].update(shard=2),
    lambda rows: rows[0].update(shards=2),
    lambda rows: rows[0].update(schema_version=2),
    lambda rows: rows[0].update(exit_code=1),
    lambda rows: rows[0].update(exit_code=5),
    lambda rows: rows[0].update(test_count=0, per_file_seconds={}),
    lambda rows: rows[0].update(per_file_seconds={}),
    lambda rows: rows[0].update(per_file_seconds={"../test_bad.py": 1}),
])
def test_aggregate_rejects_missing_duplicate_mismatched_and_unsuccessful_proof(change):
    expected = _expected()
    receipts = _receipts(expected)
    change(receipts)
    with pytest.raises(feedback.EvidenceError):
        feedback.aggregate_receipts(receipts, expected, 100)


def test_partial_rerun_keeps_successful_receipt_attempts_and_full_cost():
    expected = _expected()
    expected["run_attempt"] = 2
    receipts = _receipts(expected)
    receipts[0].update(run_attempt=1, elapsed_seconds=140)
    original = deepcopy(receipts)

    report = feedback.aggregate_receipts(receipts, expected, 170)

    assert receipts == original
    assert [item["run_attempt"] for item in report["receipts"]] == [1, 2]
    assert report["expected"]["run_attempt"] == 2
    assert feedback.validate_report(report) == report
    assert feedback.assess(report, [], POLICY)["passed"] is True


@pytest.mark.parametrize("changes", [
    {"run_id": "99"},
    {"head_sha": "0" * 40},
    {"run_attempt": 3},
    {"run_attempt": 0},
    {"run_attempt": True},
    {"exit_code": 1},
])
def test_partial_rerun_cannot_reuse_foreign_future_or_unsuccessful_proof(changes):
    expected = _expected()
    expected["run_attempt"] = 2
    receipts = _receipts(expected)
    receipts[0]["run_attempt"] = 1
    receipts[0].update(changes)

    with pytest.raises(feedback.EvidenceError):
        feedback.aggregate_receipts(receipts, expected, 100)


@pytest.mark.parametrize("command_seconds,gate_seconds,history", [
    (280, 310, []),
    (185, 200, [_report(250, run) for run in range(90, 94)]),
])
def test_summary_only_retry_cannot_erase_carried_proof_cost(
    command_seconds, gate_seconds, history,
):
    expected = _expected()
    expected["run_attempt"] = 2
    receipts = _receipts(expected, elapsed=command_seconds)
    for receipt in receipts:
        receipt["run_attempt"] = 1

    # There need not be a newly executed test job in a summary-only retry,
    # but the reconstructed duration still contains the previous test cost.
    report = feedback.aggregate_receipts(receipts, expected, gate_seconds)
    assert feedback.assess(report, history, POLICY)["passed"] is False
    with pytest.raises(feedback.EvidenceError, match="exceeds gate"):
        feedback.aggregate_receipts(receipts, expected, 5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 10 ** 400, -1, True, "10", None])
def test_non_finite_negative_and_non_numeric_seconds_cannot_pass(bad):
    expected = _expected()
    receipts = _receipts(expected)
    with pytest.raises(feedback.EvidenceError):
        feedback.aggregate_receipts(receipts, expected, bad)
    receipts[0]["elapsed_seconds"] = bad
    with pytest.raises(feedback.EvidenceError):
        feedback.aggregate_receipts(receipts, expected, 100)
    receipts = _receipts(expected)
    receipts[0]["per_file_seconds"]["tests/test_part_1.py"] = bad
    with pytest.raises(feedback.EvidenceError):
        feedback.aggregate_receipts(receipts, expected, 100)


@pytest.mark.parametrize("bad", [True, 1.2, -1, "2", None])
def test_executed_test_count_is_a_real_nonnegative_integer(bad):
    expected = _expected()
    receipts = _receipts(expected)
    receipts[0]["test_count"] = bad
    with pytest.raises(feedback.EvidenceError):
        feedback.aggregate_receipts(receipts, expected, 100)


def test_empty_shard_is_accepted_only_with_other_executed_tests_in_its_suite():
    expected = _expected(shards=2)
    receipts = _receipts(expected)
    receipts[0].update(exit_code=5, test_count=0, per_file_seconds={})
    report = feedback.aggregate_receipts(receipts, expected, 100)
    assert report["suites"]["pytest"]["test_count"] == 2
    receipts[1].update(exit_code=5, test_count=0, per_file_seconds={})
    with pytest.raises(feedback.EvidenceError, match="execute at least one"):
        feedback.aggregate_receipts(receipts, expected, 100)


def test_duplicate_file_detects_different_partition_inputs_across_runners():
    expected = _expected(shards=2)
    receipts = _receipts(expected)
    receipts[1]["per_file_seconds"] = {"tests/test_part_1.py": 5}
    with pytest.raises(feedback.EvidenceError, match="multiple pytest shards"):
        feedback.aggregate_receipts(receipts, expected, 100)


def test_summary_cannot_claim_less_time_than_its_receipts():
    report = _report()
    report["suites"]["pytest"]["elapsed_seconds"] = 1
    with pytest.raises(feedback.EvidenceError, match="summary disagrees"):
        feedback.validate_report(report)
    with pytest.raises(feedback.EvidenceError, match="exceeds gate"):
        feedback.aggregate_receipts(_receipts(_expected()), _expected(), 9)


def test_summary_booleans_cannot_impersonate_numeric_measurements():
    report = _report()
    report["suites"]["pytest"]["shards"] = True
    with pytest.raises(feedback.EvidenceError):
        feedback.validate_report(report)


def test_sustained_slow_median_fails_but_a_current_repair_clears_inherited_debt():
    history = [_report(250, run) for run in range(90, 94)]
    failed = feedback.assess(_report(220), history, POLICY)
    assert failed["passed"] is False
    assert failed["history_status"] == "sufficient"
    assert failed["median_seconds"] == 250
    repaired = feedback.assess(_report(170), history, POLICY)
    assert repaired["passed"] is True
    assert repaired["median_within_budget"] is False


def test_one_noisy_sample_under_ceiling_does_not_fail_a_healthy_window():
    history = [_report(120, run) for run in range(90, 99)]
    result = feedback.assess(_report(290), history, POLICY)
    assert result["passed"] is True
    assert result["median_seconds"] == 120
    assert result["p90_seconds"] == 120


@pytest.mark.parametrize("seconds,passed", [(179.9, True), (180, False)])
def test_median_budget_is_strictly_under_180(seconds, passed):
    history = [_report(180, run) for run in range(90, 94)]
    assert feedback.assess(_report(seconds), history, POLICY)["passed"] is passed


@pytest.mark.parametrize("seconds,passed", [
    (179.9, True), (180, False), (299.9, False), (300, False), (1200, False),
])
def test_missing_history_still_requires_the_current_gate_under_three_minutes(seconds, passed):
    result = feedback.assess(_report(seconds), [], POLICY)
    assert result["passed"] is passed
    assert result["history_status"] == "insufficient_history"


def test_too_few_healthy_samples_cannot_waive_the_current_three_minute_budget():
    history = [_report(120, run) for run in range(90, 93)]
    result = feedback.assess(_report(180), history, POLICY)
    assert result["median_seconds"] == 120
    assert result["sample_count"] == 4
    assert result["passed"] is False
    assert any("history is insufficient" in reason for reason in result["failures"])


@pytest.mark.parametrize("seconds,passed", [(44.9, True), (45, False)])
def test_migration_budget_is_enforced_when_required(seconds, passed):
    assert feedback.assess(
        _report(migration_seconds=seconds), [], POLICY
    )["passed"] is passed


def test_latest_distinct_heads_fill_the_window_not_repeated_attempts():
    history = [_report(260, run, sha="e" * 40) for run in range(70, 99)]
    result = feedback.assess(_report(220), history, POLICY)
    assert result["sample_count"] == 2
    assert result["sample_runs"][1]["run_id"] == "98"
    assert result["history_status"] == "insufficient_history"
    assert result["passed"] is False


def test_window_keeps_only_the_ten_latest_heads():
    history = [_report(260 if run < 90 else 120, run) for run in range(70, 100)]
    result = feedback.assess(_report(140), history, POLICY)
    assert result["sample_count"] == 10
    assert result["median_seconds"] == 120
    assert result["sample_runs"][-1]["run_id"] == "91"


def test_duplicate_future_and_malformed_history_fail_closed():
    for history in ([_report(run=100)], [_report(run=101)],
                    [_report(run=99), _report(run=99)], [{"schema_version": 2}]):
        with pytest.raises(feedback.EvidenceError):
            feedback.assess(_report(), history, POLICY)


def test_failing_performance_results_remain_valid_history():
    old = _report(400, 99)
    assert feedback.assess(old, [], POLICY)["passed"] is False
    result = feedback.assess(_report(120), [old], POLICY)
    assert result["passed"] is True
    assert result["p90_within_budget"] is False
    assert result["sample_count"] == 2


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_json_reader_refuses_ambiguous_or_non_finite_evidence(tmp_path, raw):
    path = tmp_path / "receipt.json"
    path.write_text(raw)
    with pytest.raises(feedback.EvidenceError):
        feedback.read_json(path)


def test_cli_aggregates_and_assesses_the_same_artifacts_ci_uses(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    expected = _expected()
    for index, receipt in enumerate(_receipts(expected)):
        (receipts / f"{index}.json").write_text(json.dumps(receipt))
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(json.dumps(expected))
    output = tmp_path / "report.json"
    assert feedback.main([
        "aggregate", "--receipts", str(receipts), "--expected", str(expected_path),
        "--gate-seconds", "100", "--output", str(output),
    ]) == 0
    history = tmp_path / "history"
    history.mkdir()
    assert feedback.main([
        "assess", "--current", str(output), "--history", str(history),
        "--policy", str(ROOT / "tests" / "feedback-budget.json"),
    ]) == 0
    (receipts / "0.json").unlink()
    assert feedback.main([
        "aggregate", "--receipts", str(receipts), "--expected", str(expected_path),
        "--gate-seconds", "100", "--output", str(output),
    ]) == 2
    assert not output.exists()
