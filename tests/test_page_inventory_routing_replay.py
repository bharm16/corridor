"""The Stage 1 routing replay refuses to spend a holdout by accident (#734).

The replay itself is an experiment run outside pytest, against registered
bytes: it is not a test and does not run in CI. What runs here is the one
thing about it that must never be discovered by trying it — that a gold set
containing the spent holdout family is refused unless the run names an actor
and a reason, and that the access it records says which document was read
(ADR-0008).
"""

from __future__ import annotations

import json
from pathlib import Path

from corridor import holdout_ledger
from scripts import page_inventory_routing_replay as _MODULE

REPO_ROOT = Path(__file__).resolve().parents[1]

HOLDOUT = "5b9c39bf570447c23e4e4ad2e02629576005697d39b8e52f1083b5fe10e2c1be"


def test_the_frozen_gold_set_still_contains_the_spent_holdout_family():
    """The refusal below is worth nothing if the corpus stopped including it."""

    gold = json.loads(_MODULE.DEFAULT_GOLD.read_text())
    wanted = {case["document_sha256"] for case in gold["cases"]}

    assert _MODULE.holdout_digests(_MODULE.DEFAULT_DATASET, wanted) == [HOLDOUT]


def test_a_holdout_run_without_an_actor_and_a_reason_is_refused(tmp_path):
    ledger = tmp_path / "holdout-access.jsonl"

    exit_code = _MODULE.main(
        [
            "--output-dir",
            str(tmp_path / "out"),
            "--holdout-access-log",
            str(ledger),
        ]
    )

    assert exit_code == 2
    # Refused before anything was read, so nothing was written either.
    assert not ledger.exists()
    assert not (tmp_path / "out").exists()


def test_a_recorded_access_names_the_document_the_actor_and_the_reason(tmp_path):
    ledger = tmp_path / "holdout-access.jsonl"

    entry = _MODULE.record_holdout_access(
        ledger, "2026-08-31.2", [HOLDOUT], "an actor", "a reason"
    )

    assert json.loads(ledger.read_text().strip()) == entry
    assert holdout_ledger.read(ledger) == [entry]
    assert entry["holdout"]["document_sha256s"] == [HOLDOUT]
    assert entry["actor"] == "an actor"
    assert entry["reason"] == "a reason"
    assert entry["dataset"]["dataset_version"] == "2026-08-31.2"
    assert entry["configuration"] == _MODULE.page_inventory_identity()
    assert entry["schema_version"] == holdout_ledger.SCHEMA
