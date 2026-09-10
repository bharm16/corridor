"""One shard receipt that writes and validates itself.

`run_test_gate.py` wrote this record as a literal dictionary and
`test_feedback.py` validated it against a separate key set, so a writer change
was caught by the validator only once CI ran it. The on-disk `schema_version`
and field names are unchanged: receipts already retained in job outputs and
logs still validate.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, ClassVar

from .contract import RECEIPT_MARKER, output_slot
from .evidence import (
    EvidenceError, loads, require_identity, require_integer, require_object, require_seconds,
)


@dataclass(frozen=True)
class ShardReceipt:
    run_id: str
    run_attempt: int
    head_sha: str
    suite: str
    shard: int
    shards: int
    elapsed_seconds: float
    exit_code: int
    test_count: int
    per_file_seconds: dict[str, float]

    SCHEMA_VERSION: ClassVar[int] = 1
    # The exact on-disk shape. Field order is the order the gate has always
    # written, so an encoded line is byte-identical to the previous writer's.
    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version", "run_id", "run_attempt", "head_sha", "suite", "shard",
        "shards", "elapsed_seconds", "exit_code", "test_count", "per_file_seconds",
    )

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": self.SCHEMA_VERSION, **{
            name: getattr(self, name) for name in self.FIELDS if name != "schema_version"
        }}

    def encoded(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"), allow_nan=False)

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n")

    def output_line(self) -> str:
        """The `GITHUB_OUTPUT` line that publishes this receipt to the summary job."""
        return f"{output_slot(self.suite, self.shard)}={self.encoded()}\n"

    def marker_line(self) -> str:
        return RECEIPT_MARKER + " " + self.encoded()

    @classmethod
    def validate(cls, value: Any) -> "ShardReceipt":
        """Accept only a complete receipt that proves a successful test command.

        Agreement with the expected run, partition and gate time is the
        aggregation's question; this is what any single receipt must satisfy.
        """
        require_object(value, set(cls.FIELDS), "receipt")
        if require_integer(value["schema_version"], "receipt schema", 1) != cls.SCHEMA_VERSION:
            raise EvidenceError("unsupported receipt schema")
        require_identity(value)
        if not isinstance(value["suite"], str):
            raise EvidenceError("unexpected receipt suite")
        shard = require_integer(value["shard"], "shard", 1)
        shards = require_integer(value["shards"], "shards", 1)
        elapsed = require_seconds(value["elapsed_seconds"], "receipt elapsed")
        count = require_integer(value["test_count"], "executed test count")
        status = require_integer(value["exit_code"], "pytest exit code")
        if status != 0 and not (status == 5 and count == 0):
            raise EvidenceError("receipt does not prove successful tests")
        times = value["per_file_seconds"]
        if not isinstance(times, dict) or (count > 0 and not times):
            raise EvidenceError("executed tests require per-file timings")
        measured = {}
        for name, seconds in times.items():
            if not isinstance(name, str) or not re.fullmatch(r"tests/test_[^/]+\.py", name):
                raise EvidenceError("timing path is not a top-level test file")
            seconds = require_seconds(seconds, f"{name} elapsed")
            if count == 0 and seconds != 0:
                raise EvidenceError("an empty shard cannot report executed test work")
            measured[name] = seconds
        return cls(
            run_id=value["run_id"], run_attempt=value["run_attempt"], head_sha=value["head_sha"],
            suite=value["suite"], shard=shard, shards=shards, elapsed_seconds=elapsed,
            exit_code=status, test_count=count, per_file_seconds=measured,
        )

    @staticmethod
    def from_output(encoded: str, suite: str, shard: int) -> dict:
        """Read one published job output back, bound to the slot it was read from."""
        receipt = loads(encoded, f"{output_slot(suite, shard)} job output")
        if not isinstance(receipt, dict) or receipt.get("suite") != suite or receipt.get("shard") != shard:
            raise EvidenceError("receipt is bound to the wrong output slot")
        return receipt
