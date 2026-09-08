"""The clean and degraded twin receipts, held to the retained lanes they came from (#739).

ADR-0094 measured Textract on the ten development pairs in lanes that must
never be collapsed into one number, and two of them are the scanned route this
ticket integrates: the clean 300 dpi twins and the degraded ones. Corridor
records those two beside the baseline lanes in `gold/pdf-pairs/v1`, as two
separate receipts, because the whole point of the pair is that a degraded scan
is not a clean one and neither is the native baseline.

The receipts were derived from the retained per-pair score files rather than
typed, and this recomputes the same derivation so they cannot drift from the
evidence. Nothing here calls a provider: the responses are retained, the
per-pair scores are committed, and the corpus twins are outside this repository
and are never read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corridor_pdf_reader import registry

REPO_ROOT = Path(__file__).resolve().parents[1]
LANES = REPO_ROOT / "src" / "corridor_pdf_reader" / "receipts" / "textract"
RECEIPTS = REPO_ROOT / "gold" / "pdf-pairs" / "v1" / "receipts"

# The two receipts, the retained lane each is recorded from, and the numbers
# #739 names. Written out here on purpose: the derivation below must reproduce
# them from the evidence, so a silent change to either side is a failure.
TWINS = {
    "clean": {
        "run": "739-textract-clean-twins",
        "lane": "textract-B",
        "pairs": [0, 10],
        "pages": [7, 53],
        "cells_exact": [13450, 14641],
    },
    "degraded": {
        "run": "739-textract-degraded-twins",
        "lane": "textract-C",
        "pairs": [0, 10],
        "pages": [6, 53],
        "cells_exact": [12427, 14641],
    },
}


def _receipt(twin: str) -> dict:
    return json.loads((RECEIPTS / TWINS[twin]["run"] / "receipt.json").read_text(encoding="utf-8"))


def _derive(lane: str) -> dict:
    """Pairs, pages and cells recomputed from one lane's retained per-pair scores."""
    scores = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((LANES / lane / "scores").glob("*.json"))
    ]
    return {
        "pairs": [sum(1 for score in scores if score["pass"]), len(scores)],
        "pages": [
            sum(int(score["pages_pass"]) for score in scores),
            sum(len(score["pages"]) for score in scores),
        ],
        "cells_exact": [
            sum(int(score["exact_cells"]) for score in scores),
            sum(int(score["reference_cells"]) for score in scores),
        ],
    }


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_the_receipt_reproduces_from_the_retained_per_pair_scores(twin):
    expected = TWINS[twin]
    derived = _derive(expected["lane"])
    recorded = _receipt(twin)["measures"]

    assert derived == {key: expected[key] for key in ("pairs", "pages", "cells_exact")}
    assert [recorded["pairs"], recorded["pages"], recorded["cells_exact"]] == [
        derived["pairs"],
        derived["pages"],
        derived["cells_exact"],
    ]


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_the_receipt_matches_the_lane_summary_it_was_recorded_from(twin):
    """The second source of the same numbers, so the derivation is checked twice."""
    summary = json.loads(
        (LANES / TWINS[twin]["lane"] / "summary.json").read_text(encoding="utf-8")
    )
    recorded = _receipt(twin)["measures"]

    assert recorded["pairs"] == [summary["pairs_pass"], summary["pairs"]]
    assert recorded["pages"] == [summary["pages_pass"], summary["pages"]]
    assert recorded["cells_exact"] == [summary["exact_cells"], summary["reference_cells"]]


def test_clean_and_degraded_are_two_receipts_and_two_index_entries():
    """Kept apart: a degraded scan is not a clean one, and neither is the baseline."""
    index = {entry["run"]: entry for entry in registry.load_receipt_index()["receipts"]}

    for twin, expected in TWINS.items():
        entry = index[expected["run"]]
        assert entry["role"] == "scanned-twin-measurement"
        assert entry["twin"] == twin
        assert entry["path"] == f"gold/pdf-pairs/v1/receipts/{expected['run']}"
    assert index["739-textract-clean-twins"]["measures"] != index["739-textract-degraded-twins"]["measures"]
    # Beside the baseline lanes, not merged into them.
    assert index["frozen-reader-2026-09-06"]["role"] == "baseline"
    assert {index[run]["path"] for run in ("739-textract-clean-twins", "739-textract-degraded-twins")} <= {
        entry["path"] for entry in registry.load_receipt_index()["receipts"]
    }


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_the_three_measures_stay_three_numbers(twin):
    """No receipt offers a single score, and each measure keeps its own denominator."""
    measures = _receipt(twin)["measures"]

    assert set(measures) == {"pairs", "pages", "cells_exact", "note", "by_pair"}
    assert len({tuple(measures[name]) for name in ("pairs", "pages", "cells_exact")}) == 3
    assert "never be collapsed into one number" in measures["note"]
    assert sum(pair["pages"] for pair in measures["by_pair"]) == measures["pages"][1]
    assert sum(pair["exact_cells"] for pair in measures["by_pair"]) == measures["cells_exact"][0]


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_the_receipt_reports_cost_per_document_and_claims_no_qualification(twin):
    receipt = _receipt(twin)

    per_document = {row["pdf"]: row for row in receipt["cost"]["per_document"]}
    assert len(per_document) == 10
    assert all(row["list_charge_usd"] == round(row["pages"] * 0.015, 3) for row in per_document.values())
    assert "not a qualification of it and not a selection of it" in receipt["not_a_claim"]
    assert receipt["limits"] == registry.LIMITS


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_the_retained_evidence_is_still_the_bytes_the_receipt_recorded(twin):
    """A lane receipt edited after the fact would make the recording a different claim."""
    receipt = _receipt(twin)
    lane_root = REPO_ROOT / receipt["evidence"]["retained_lane"]

    assert registry.digest_files(lane_root, sorted(receipt["evidence"]["files"])) == receipt["evidence"]["files"]
