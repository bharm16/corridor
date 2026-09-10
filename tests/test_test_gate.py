"""The test gate's shared definitions behave as one interface.

The receipt the gate writes is the receipt the summary validates and reads
back from a job output; the JUnit reader is the one rule every tool measures
by; the test-file enumeration is what both partitions cover. These are the
properties that used to hold only by three scripts agreeing by hand.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import test_timing
from scripts.test_gate import feedback
from scripts.test_gate.contract import RECEIPT_MARKER, job_name, latest_jobs, output_slot
from scripts.test_gate.evidence import EvidenceError, read_json
from scripts.test_gate.junit import case_counts, measured_cases
# The enumeration is reached through its module: a bare `test_files` name in
# a test module would itself be collected as a test.
from scripts.test_gate import partition
from scripts.test_gate.partition import SLOW_MINIMUM_FILE_SECONDS, recorded_seconds, shard
from scripts.test_gate.receipt import ShardReceipt


ROOT = Path(__file__).resolve().parents[1]


def _receipt(**overrides) -> ShardReceipt:
    values = {
        "run_id": "100", "run_attempt": 1, "head_sha": "a" * 40,
        "suite": "pytest", "shard": 1, "shards": 2, "elapsed_seconds": 12.5,
        "exit_code": 0, "test_count": 3,
        "per_file_seconds": {"tests/test_one.py": 7.5, "tests/test_two.py": 0.0},
    }
    values.update(overrides)
    return ShardReceipt(**values)


def test_receipt_round_trips_from_writer_through_validation_to_job_output(tmp_path):
    receipt = _receipt()
    path = tmp_path / "receipt-pytest-1.json"
    receipt.write(path)

    assert ShardReceipt.validate(read_json(path)) == receipt
    slot, encoded = receipt.output_line().rstrip("\n").split("=", 1)
    assert slot == output_slot("pytest", 1) == "pytest_1"
    assert ShardReceipt.from_output(encoded, "pytest", 1) == receipt.as_dict()
    assert receipt.marker_line() == RECEIPT_MARKER + " " + encoded
    # The on-disk shape retained in job outputs and logs is unchanged.
    assert list(receipt.as_dict()) == [
        "schema_version", "run_id", "run_attempt", "head_sha", "suite", "shard",
        "shards", "elapsed_seconds", "exit_code", "test_count", "per_file_seconds",
    ]
    assert receipt.as_dict()["schema_version"] == 1
    assert json.loads(encoded) == json.loads(path.read_text())


def test_written_receipts_are_what_aggregation_accepts():
    expected = {"run_id": "100", "run_attempt": 1, "head_sha": "a" * 40,
                "suites": {"pytest": 2, "slow": 1}}
    receipts = [
        _receipt().as_dict(),
        _receipt(shard=2, per_file_seconds={"tests/test_three.py": 1.0}).as_dict(),
        _receipt(suite="slow", shards=1, per_file_seconds={"tests/test_one.py": 2.0}).as_dict(),
    ]
    report = feedback.aggregate_receipts(receipts, expected, 100)
    assert report["suites"]["pytest"]["per_file_seconds"] == {
        "tests/test_one.py": 7.5, "tests/test_two.py": 0.0, "tests/test_three.py": 1.0,
    }
    assert report["suites"]["pytest"]["test_count"] == 6


@pytest.mark.parametrize("change", [
    lambda value: value.pop("test_count"),
    lambda value: value.update(schema_version=2),
    lambda value: value.update(run_id="0"),
    lambda value: value.update(head_sha="abc"),
    lambda value: value.update(shard=0),
    lambda value: value.update(exit_code=1),
    lambda value: value.update(exit_code=5),
    lambda value: value.update(test_count=0),
    lambda value: value.update(per_file_seconds={}),
    lambda value: value.update(per_file_seconds={"../test_bad.py": 1}),
    lambda value: value.update(per_file_seconds={"tests/test_one.py": float("nan")}),
    lambda value: value.update(elapsed_seconds=-1),
])
def test_receipt_validation_refuses_incomplete_or_unsuccessful_proof(change):
    value = _receipt().as_dict()
    change(value)
    with pytest.raises(EvidenceError):
        ShardReceipt.validate(value)


def test_empty_shard_receipt_is_valid_only_with_no_measured_work():
    empty = _receipt(exit_code=5, test_count=0, per_file_seconds={"tests/test_one.py": 0.0})
    assert ShardReceipt.validate(empty.as_dict()) == empty
    with pytest.raises(EvidenceError, match="empty shard"):
        ShardReceipt.validate(_receipt(exit_code=5, test_count=0).as_dict())


@pytest.mark.parametrize("encoded, suite, shard", [
    (_receipt().encoded(), "pytest", 2),
    (_receipt().encoded(), "slow", 1),
    ("[1]", "pytest", 1),
    ('{"suite":"pytest","shard":1,"suite":"slow"}', "pytest", 1),
    ("{broken", "pytest", 1),
])
def test_job_output_must_be_a_receipt_bound_to_its_own_slot(encoded, suite, shard):
    with pytest.raises(ValueError):
        ShardReceipt.from_output(encoded, suite, shard)


def test_junit_reader_measures_executed_work_and_keeps_zero_for_unexercised_files(tmp_path):
    report = tmp_path / "suite.xml"
    report.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_a" time="2"/>'
        '<testcase classname="tests.test_a.TestGroup" time="1"/>'
        '<testcase classname="tests.test_a" time="5"><skipped/></testcase>'
        '</testsuite></testsuites>'
    )
    assert measured_cases(report, ["tests/test_a.py", "tests/test_b.py"]) == (
        2, {"tests/test_a.py": 3.0, "tests/test_b.py": 0.0}
    )


@pytest.mark.parametrize("case", [
    'classname="tests.test_other" time="1"',
    'classname="" name="tests.test_a" time="1"',
    'classname="tests.test_a" time="nan"',
    'classname="tests.test_a" time="-1"',
])
def test_junit_reader_refuses_out_of_partition_and_untrustworthy_cases(tmp_path, case):
    report = tmp_path / "suite.xml"
    report.write_text(f"<testsuites><testcase {case}/></testsuites>")
    with pytest.raises(ValueError):
        measured_cases(report, ["tests/test_a.py"])


def test_case_counts_sum_every_recorded_suite(tmp_path):
    report = tmp_path / "suite.xml"
    report.write_text(
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1"/>'
        '<testsuite tests="2" errors="2"/></testsuites>'
    )
    assert case_counts(report) == {"tests": 5, "failures": 1, "errors": 2, "skipped": 1}


def test_test_files_names_every_top_level_test_module_once():
    expected = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")
    )
    files = partition.test_files()
    assert files == expected
    assert len(files) == len(set(files))
    assert "tests/test_test_gate.py" in files
    assert not any("/" in name.removeprefix("tests/") for name in files)


def test_job_names_output_slots_and_latest_attempts_follow_the_workflow():
    assert job_name("pytest", 3) == "pytest (3)"
    assert job_name("slow", 1) == "slow (1)"
    assert job_name("migration", 1) == "migration"
    assert output_slot("slow", 2) == "slow_2"
    jobs = [
        {"name": "pytest (1)", "run_attempt": 1, "id": 1},
        {"name": "pytest (1)", "run_attempt": 2, "id": 2},
        {"name": "check", "run_attempt": 1, "id": 3},
    ]
    assert latest_jobs(jobs) == {"pytest (1)": jobs[1], "check": jobs[2]}


def test_timing_tool_writes_bootstrap_weights_the_partition_reads_back(tmp_path, capsys):
    """Producer and consumer of `tests/durations*.json` share one shape.

    The weights come through the same JUnit rule as the CI receipt, so a
    skipped case adds nothing and a file the suite does not contain is refused
    rather than recorded.
    """
    report = tmp_path / "suite.xml"
    report.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_test_gate" time="1.26"/>'
        '<testcase classname="tests.test_test_gate" time="0.5"><skipped/></testcase>'
        '<testcase classname="tests.test_feedback" time="0.04"/>'
        '</testsuite></testsuites>'
    )
    destination = tmp_path / "durations.json"
    assert test_timing.main([str(report), "--write", str(destination)]) == 0
    recorded = recorded_seconds(destination)
    assert set(recorded) == set(partition.test_files())
    assert recorded["tests/test_test_gate.py"] == 1.3
    assert recorded["tests/test_feedback.py"] == 0.0
    assert all(seconds == 0.0 for name, seconds in recorded.items()
               if name not in ("tests/test_test_gate.py", "tests/test_feedback.py"))
    buckets = shard(partition.test_files(), recorded, 3, minimum_file_seconds=SLOW_MINIMUM_FILE_SECONDS)
    assert sorted(name for bucket in buckets for name in bucket) == partition.test_files()
    assert "tests/test_test_gate.py" in capsys.readouterr().out

    report.write_text('<testsuites><testcase classname="tests.test_absent" time="1"/></testsuites>')
    assert test_timing.main([str(report), "--write", str(destination)]) == 2
    assert recorded_seconds(destination) == recorded


def test_the_strict_reader_refuses_ambiguous_or_non_finite_evidence(tmp_path):
    for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
        path = tmp_path / "receipt.json"
        path.write_text(raw)
        with pytest.raises(EvidenceError):
            read_json(path)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(deepcopy({"a": 1})))
    assert read_json(path) == {"a": 1}
