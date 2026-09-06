"""The paired-rendition Reference Dataset registry is machine-readable and honest (#731).

`gold/pdf-pairs/v1` registers the 333-pair dataset, its holdout-access
history, the scorer's taxonomy and the baseline receipts. These tests hold
the files to their own claims without the corpus: the registration's pairs
carry the identities `bootstrap.corpus` derives from the digests and the
split file it names; the taxonomy names exactly the classes and subclasses
`bootstrap/score.py` emits, and its failing flags are the scorer's own rule;
the ledger holds the three accesses that predate this harness and every
retained holdout run; and every indexed receipt exists at its path with the
digests recorded for it, its measures three numbers apart.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from corridor_pdf_reader import registry
from corridor_pdf_reader.bootstrap import score, tally

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "gold" / "pdf-pairs" / "v1"
SCORE_SOURCE = (REPO_ROOT / "src" / "corridor_pdf_reader" / "bootstrap" / "score.py").read_text(encoding="utf-8")


def _registration() -> dict:
    return registry.load_registration(REGISTRY / "dataset.json")


def test_the_registration_names_the_dataset_the_package_measures():
    document = _registration()

    assert document["schema_version"] == registry.DATASET_SCHEMA
    assert document["dataset_version"] == registry.DATASET_VERSION
    assert document["corpus"]["pairs"] == len(document["pairs"]) == 333
    assert document["split"]["development"] == {"pairs": 263, "pages": 1589}
    assert document["split"]["holdout"] == {"pairs": 70, "pages": 409}
    assert document["split"]["seed"] == 720 and document["split"]["holdout_fraction"] == 0.2
    assert document["split"]["grouping_rule"] == registry.GROUPING_RULE
    assert document["limits"] == registry.LIMITS
    assert document["holdout_policy"] == registry.HOLDOUT_POLICY
    assert document["expected_values"]["independently_established"] is True
    assert list(document["gate"]) == list(registry.GATE)

    split_file = REPO_ROOT / document["split"]["file"]
    assert registry.sha256_file(split_file) == document["split"]["sha256"]
    sealed = {record["key"]: record for record in json.loads(split_file.read_text(encoding="utf-8"))["pairs"]}
    key_manifest = REPO_ROOT / document["reference"]["manifest"]
    assert registry.sha256_file(key_manifest) == document["reference"]["manifest_sha256"]
    keys_retained = {entry["path"] for entry in json.loads(key_manifest.read_text(encoding="utf-8"))["entries"]}
    for pair in document["pairs"]:
        assert pair["key"] == registry.pair_key(pair["pdf_sha256"], pair["book_sha256"])
        assert sealed[pair["key"]]["holdout"] == pair["holdout"]
        assert sealed[pair["key"]]["family"] == pair["family"]
        assert pair["agency"] == pair["folder"].split("_", 1)[0]
        assert f"{pair['key']}.json" in keys_retained
    assert sum(pair["pages"] for pair in document["pairs"] if pair["holdout"]) == 409
    assert len(document["exclusions"]) == 310
    assert all(exclusion["reason"] for exclusion in document["exclusions"])
    assert sum(1 for exclusion in document["exclusions"] if exclusion["duplicate_of"]) == 119
    registered = {pair["pdf_sha256"] for pair in document["pairs"]}
    assert not any(exclusion["pdf_sha256"] in registered and not exclusion["duplicate_of"] for exclusion in document["exclusions"])


def _emitted_by_the_scorer() -> dict[str, set[str]]:
    """Every (class, subclass) the scorer's source can emit, read from the source itself."""
    tree = ast.parse(SCORE_SOURCE)
    literal_subs = {
        "classify_mismatch": set(),
        "where": set(),
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in literal_subs:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Constant) and isinstance(inner.value.value, str):
                    literal_subs[node.name].add(inner.value.value)
    emitted: dict[str, set[str]] = {}

    def add(cls: str, sub: str) -> None:
        emitted.setdefault(cls, set()).add(sub)

    for cls, sub in re.findall(r'error\(\s*"(\w+)",\s*"(\w+)"', SCORE_SOURCE):
        add(cls, sub)
    for cls, sub in re.findall(r'\{"class": "(\w+)", "sub": "(\w+)"', SCORE_SOURCE):
        add(cls, sub)
    for cls, first, second in re.findall(r'error\(\s*"(\w+)",\s*"(\w+)" if [^\n]* else "(\w+)"', SCORE_SOURCE):
        add(cls, first)
        add(cls, second)
    for cls in re.findall(r'error\("(\w+)", where\(', SCORE_SOURCE):
        for sub in literal_subs["where"]:
            add(cls, sub)
    # `error("value_mismatch", sub, ...)`: sub is classify_mismatch's answer or
    # one of the two clipped explanations assigned in place.
    assert 'error("value_mismatch", sub,' in SCORE_SOURCE
    for sub in literal_subs["classify_mismatch"] | set(re.findall(r'sub = "(clipped_\w+)"', SCORE_SOURCE)):
        add("value_mismatch", sub)
    # `error("extra_value", sub, ...)`: sub is one of four literals assigned just above it.
    assert 'error("extra_value", sub,' in SCORE_SOURCE
    extra = re.search(r'if got_key in sheet_row\.values\(\):\s*sub = "(\w+)"\s*elif [^\n]*:\s*sub = "(\w+)"\s*elif [^\n]*:\s*sub = "(\w+)"\s*else:\s*sub = "(\w+)"', SCORE_SOURCE)
    assert extra is not None
    for sub in extra.groups():
        add("extra_value", sub)
    # `{"class": "unexplained_text", "sub": sub, ...}`: sub is one of three literals decided in place.
    assert '{"class": "unexplained_text", "sub": sub,' in SCORE_SOURCE
    unexplained = re.search(r'sub = "(\w+)" if numeric_key\(key\) is not None else \("(\w+)" if edge else "(\w+)"\)', SCORE_SOURCE)
    assert unexplained is not None
    for sub in unexplained.groups():
        add("unexplained_text", sub)
    return emitted


def test_the_taxonomy_names_exactly_what_the_scorer_emits_and_flags_what_the_gate_ignores():
    taxonomy = _registration()["taxonomy"]
    registered = {cls: set(entry["subclasses"]) for cls, entry in taxonomy["classes"].items()}

    assert registered == _emitted_by_the_scorer()
    assert registered == {cls: set(entry["subclasses"]) for cls, entry in registry.TAXONOMY.items()}
    assert set(registered) >= set(score.ERROR_CLASSES) - {"unexplained_text", "outside_table"} | {"unexplained_text", "outside_table"}
    assert all(entry["meaning"] and all(sub["meaning"] for sub in entry["subclasses"].values()) for entry in taxonomy["classes"].values())

    non_failing = {
        (cls, sub) for cls, entry in taxonomy["classes"].items() for sub, item in entry["subclasses"].items() if not item["failing"]
    }
    scorer_rule = {("value_mismatch", sub) for sub in score.CLIPPED} | {
        (cls, sub) for cls in ("unverifiable", "prose_row", "columns_folded", "merged_rows") for sub in registered[cls]
    }
    assert non_failing == scorer_rule
    assert set(tally.SKIPPED_CLASSES) == {"unverifiable", "prose_row", "columns_folded", "merged_rows"}
    assert all(any(sub.startswith(prefix) for prefix in tally.SKIPPED_MISMATCHES) for sub in score.CLIPPED)

    uncovered = re.search(r'in mismatched:\s*sub = "(\w+)"\s*elif key in outside_seen:\s*sub = "(\w+)"\s*else:\s*sub = "(\w+)"\s*uncovered\.append', SCORE_SOURCE)
    assert uncovered is not None
    assert set(taxonomy["uncovered"]) == set(uncovered.groups()) == set(registry.UNCOVERED)
    assert set(taxonomy["skipped_tables"]) == set(re.findall(r'"skipped": "(\w+)"', SCORE_SOURCE))
    assert set(taxonomy["table_notes"]) == {"columns_refined", "merges_unreported"}
    assert all(f'"{name}":' in SCORE_SOURCE for name in taxonomy["table_notes"])
    assert set(taxonomy["pagination"]) == {"pagination_lines", "lone_page_number_cells"}


def test_the_holdout_ledger_holds_the_history_and_every_retained_holdout_run():
    entries = registry.load_holdout_ledger(REGISTRY / "holdout-access.jsonl")

    assert [entry["run"] for entry in entries[:3]] == ["loop-007", "loop-020", "reproduction-2026-09-06"]
    assert all(entry["schema_version"] == registry.LEDGER_SCHEMA for entry in entries)
    for entry in entries:
        for field in ("run", "actor", "reason", "purpose", "configuration", "holdout", "result"):
            assert entry[field], (entry["run"], field)
    loop_007, loop_020, reproduction = entries[:3]
    assert loop_007["result"]["pages"] == [316, 712] and loop_007["holdout"]["pairs"] == 104
    assert loop_007["accessed_at"] is None and "LOOP-LOG.md" in loop_007["accessed_on"]
    assert loop_020["result"]["pairs"] == [70, 70] and loop_020["result"]["pages"] == [409, 409]
    assert loop_020["accessed_at"] == "2026-09-05" and loop_020["configuration"]["commit"].startswith("c39363e")
    assert reproduction["result"] == {
        "status": "scored", "pairs": [70, 70], "pages": [409, 409], "cells_exact": [125498, 125529], "failing_pairs": [], "loop_020": "reproduces loop-020 exactly",
    }
    registration = registry.registration_identity(REGISTRY / "dataset.json")
    for entry in entries[1:]:
        assert entry["dataset"]["registration_sha256"] == registration["sha256"]

    index = registry.load_receipt_index(REGISTRY / "receipts.json")
    retained_holdout_runs = {entry["run"] for entry in index["receipts"] if entry.get("holdout_access")}
    assert retained_holdout_runs <= {entry["run"] for entry in entries}
    for entry in entries[3:]:
        assert entry["result"]["status"] in {"scored", "failed"}
        assert entry["configuration"]["package_digest"] and entry["holdout"]["keys_sha256"]


def test_every_indexed_receipt_exists_with_its_digests_and_keeps_the_three_measures_apart():
    index = registry.load_receipt_index(REGISTRY / "receipts.json")
    runs = [entry["run"] for entry in index["receipts"]]

    assert index["schema_version"] == registry.INDEX_SCHEMA
    assert len(runs) == len(set(runs))
    assert {"loop-020", "reproduction-2026-09-06", "syn-003", "reader-ten", "textract-A", "textract-B", "textract-C"} <= set(runs)
    roles = {entry["run"]: entry["role"] for entry in index["receipts"]}
    assert [run for run, role in roles.items() if role == "baseline"] == ["frozen-reader-2026-09-06"]
    assert "failure-proof" in roles.values()
    for entry in index["receipts"]:
        root = REPO_ROOT / entry["path"]
        assert root.is_dir(), entry["run"]
        assert entry["files"], entry["run"]
        for name, digest in entry["files"].items():
            assert registry.sha256_file(root / name) == digest, (entry["run"], name)
        for measure in entry["measures"].values():
            if measure is None:
                continue
            assert {"pairs", "pages", "cells_exact"} <= set(measure)
            assert len(measure["pairs"]) == 2 and len(measure["pages"]) == 2
            assert measure["cells_exact"] is None or len(measure["cells_exact"]) == 2
        assert entry["configuration"]["engine"] and entry["configuration"]["commit"]

    by_run = {entry["run"]: entry for entry in index["receipts"]}
    assert by_run["loop-020"]["measures"]["development"] == {"pairs": [262, 263], "pages": [1589, 1589], "cells_exact": [483210, 483336], "failing_pairs": ["fcbe0ceb23e61b0f"]}
    assert by_run["loop-020"]["measures"]["holdout"]["pairs"] == [70, 70] and by_run["loop-020"]["measures"]["holdout"]["pages"] == [409, 409]
    assert by_run["syn-003"]["measures"]["development"] == {"pairs": [261, 281], "pages": [1592, 1627], "cells_exact": [535687, 536967], "failing_pairs": None}
    assert by_run["syn-003"]["dataset"]["not_the_registered_dataset"] is True
    lanes = {run: by_run[run]["measures"]["development"] for run in ("reader-ten", "textract-A", "textract-B", "textract-C")}
    assert [lanes[run]["pairs"] for run in lanes] == [[9, 10], [0, 10], [0, 10], [0, 10]]
    assert [lanes[run]["pages"] for run in lanes] == [[53, 53], [25, 53], [7, 53], [6, 53]]
    assert [lanes[run]["cells_exact"] for run in lanes] == [[14615, 14641], [14342, 14641], [13450, 14641], [12427, 14641]]


def test_the_harness_baseline_reproduces_loop_020_and_the_failure_proof_regresses_against_it():
    index = registry.load_receipt_index(REGISTRY / "receipts.json")
    by_run = {entry["run"]: entry for entry in index["receipts"]}
    baseline = by_run["frozen-reader-2026-09-06"]
    receipt = json.loads((REPO_ROOT / baseline["path"] / "receipt.json").read_text(encoding="utf-8"))

    assert receipt["schema_version"] == registry.RECEIPT_SCHEMA
    assert receipt["limits"] == registry.LIMITS
    assert receipt["dataset"]["sha256"] == registry.registration_digest(REGISTRY / "dataset.json")
    assert receipt["configuration"]["name"] == "frozen-reader" and receipt["configuration"]["engine"] == "tagged"
    assert receipt["configuration"]["reader"]["matches_commit"] is True
    assert baseline["measures"]["development"] == by_run["loop-020"]["measures"]["development"]
    assert baseline["measures"]["holdout"] == by_run["reproduction-2026-09-06"]["measures"]["holdout"]
    assert receipt["reference"]["identical_bytes"] == receipt["reference"]["keys"] == 333
    assert receipt["holdout"]["included"] is True and receipt["holdout"]["ledger_entry"] == baseline["holdout_access"]
    assert (REPO_ROOT / baseline["path"] / "scores").is_dir()
    assert len(list((REPO_ROOT / baseline["path"] / "scores").glob("*.json"))) == 333

    proofs = [entry for entry in index["receipts"] if entry["role"] == "failure-proof"]
    assert proofs
    for proof in proofs:
        failing = json.loads((REPO_ROOT / proof["path"] / "receipt.json").read_text(encoding="utf-8"))
        assert failing["configuration"]["name"] != "frozen-reader"
        assert failing["gate"]["development"]["all_pairs_pass"] is False
        assert failing["baseline_comparison"]["against"] == "frozen-reader-2026-09-06"
        assert failing["baseline_comparison"]["regressed"] is True
        assert failing["baseline_comparison"]["by_set"]["development"]["pairs_regressed"]
        assert failing["limits"] == registry.LIMITS
        assert proof["baseline_comparison"] == {"against": "frozen-reader-2026-09-06", "regressed": True}


def test_the_frozen_reader_gold_pdf_v1_run_is_retained_with_its_threshold_record():
    from corridor_pdf_reader import gold_evaluation

    run = REGISTRY / "pdf-v1" / "frozen-reader-2026-09-06"
    for name in gold_evaluation.RETAINED_FILES:
        assert (run / name).is_file(), name
    evaluation = json.loads((run / "evaluation.json").read_text(encoding="utf-8"))
    thresholds = json.loads((run / "thresholds.json").read_text(encoding="utf-8"))
    adapter = json.loads((run / "adapter.json").read_text(encoding="utf-8"))

    assert evaluation["schema_version"] == "corridor.pdf-evaluation.v1"
    assert evaluation["engine"] == "corridor_pdf_reader frozen-reader"
    assert evaluation["configuration_sha256"] == adapter["configuration_sha256"] == gold_evaluation.configuration_sha256(adapter["configuration"])
    assert {name: item["met"] for name, item in thresholds["thresholds"].items()} == evaluation["thresholds_met"]
    assert {name: item["applies"] for name, item in thresholds["thresholds"].items()} == {name: applies for name, (applies, _) in gold_evaluation.THRESHOLD_APPLICABILITY.items()}
    assert thresholds["passed_as_the_contract_scores_it"] is evaluation["passed"]
    assert evaluation["overall"]["pages"] == 8 and evaluation["overall"]["documents"] == 6
    assert adapter["holdout_included"] is True and adapter["limits"] == registry.LIMITS
    assert adapter["configuration"]["absent"]["page_class"] == gold_evaluation.UNCLASSIFIED
    assert all(not document.get("failure") for document in adapter["documents"])
    ledger = (REPO_ROOT / "gold" / "pdf" / "v1" / "holdout-access.jsonl").read_text(encoding="utf-8").splitlines()
    assert any("#731" in json.loads(line)["reason"] for line in ledger)
