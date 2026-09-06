"""The measurement command's receipt logic, proven without the corpus (#731).

`make pdf-pairs-measure` is an explicit experiment outside CI. What CI proves
here is the part that decides: on a fake corpus of two pairs with fake answer
keys, an exact read passes the gate and a wrong read fails it through the
imported scorer itself; the failing read is a regression against a baseline;
pair, page and cell measures stay three numbers; the command refuses the
holdout without an actor and a reason; and a holdout access is appended to
the ledger with its result, whether the run scored or failed.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from corridor_pdf_reader import measurement, registry

KEYS = {"aaaa": ("11" * 32, "22" * 32), "bbbb": ("33" * 32, "44" * 32)}


def _corpus(root: Path) -> None:
    root.mkdir(parents=True)
    with open(root / "MANIFEST.csv", "w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["folder", "pdf", "spreadsheet", "tier", "reading", "pdf_pages", "visible_cells", "printed_sheets", "unprinted_sheets", "duplicate_of", "pdf_sha256", "book_sha256"],
        )
        writer.writeheader()
        for key, (pdf, book) in KEYS.items():
            writer.writerow(
                {
                    "folder": f"WSDOT_{key}", "pdf": f"{key}.pdf", "spreadsheet": f"{key}.xlsx", "tier": "A", "reading": "poppler",
                    "pdf_pages": 1, "visible_cells": 4, "printed_sheets": "S", "unprinted_sheets": "", "duplicate_of": "",
                    "pdf_sha256": pdf, "book_sha256": book,
                }
            )


def _split(path: Path, corpus: Path, *, holdout: set[str]) -> dict[str, dict]:
    from corridor_pdf_reader.bootstrap.corpus import load_pairs

    records = []
    for pair in load_pairs(corpus):
        name = pair.pdf[:-4]
        records.append({"key": pair.key, "folder": pair.folder, "pdf": pair.pdf, "spreadsheet": pair.spreadsheet, "agency": "WSDOT", "family": "Excel", "tagged": True, "tier": "A", "reading": "poppler", "pages": 1, "visible_cells": 4, "holdout": name in holdout})
    path.write_text(json.dumps({"seed": 720, "holdout_fraction": 0.2, "strata": "agency x producer family", "pairs": records}))
    return {record["key"]: record for record in records}


def _sheet() -> dict:
    return {"name": "S", "cells": [[1, 1, "Owner", "text"], [1, 2, "Status", "text"], [2, 1, "Gas", "text"], [2, 2, "Open", "text"]], "merged": [], "print_area": None, "outside_text": [], "uncached": [], "print_titles": None, "header_footer": []}


def _read(rows: list[list[str]]) -> dict:
    cells = [
        {"row": i, "column": j, "row_span": 1, "column_span": 1, "text": text, "box": [50.0 + j * 50.0, 100.0 + i * 20.0, 100.0 + j * 50.0, 120.0 + i * 20.0]}
        for i, row in enumerate(rows)
        for j, text in enumerate(row)
    ]
    return {"pages": [{"number": 1, "size": [612.0, 792.0], "rotation": 0, "tables": [{"method": "test", "box": [0, 0, 300, 40], "cells": cells}], "outside": []}]}


def _run(tmp_path: Path, reads: dict[str, dict]) -> tuple[Path, Path, dict[str, dict]]:
    """A fake corpus, split, answer keys and reads; returns (run, reference, split records)."""
    corpus = tmp_path / "corpus"
    _corpus(corpus)
    records = _split(tmp_path / "pairs.json", corpus, holdout={"bbbb"})
    by_name = {record["pdf"][:-4]: key for key, record in records.items()}
    reference = tmp_path / "reference"
    reference.mkdir()
    run = tmp_path / "loop"
    (run / "reads").mkdir(parents=True)
    receipts = []
    for name, read in reads.items():
        key = by_name[name]
        (reference / f"{key}.json").write_text(json.dumps({"key": key, "sheets": [_sheet()]}))
        (run / "reads" / f"{key}.json").write_text(json.dumps({"key": key, **read}))
        receipts.append({"key": key, "pages": 1, "seconds": 0.1})
    (run / "read-receipts.json").write_text(json.dumps({"engine": "tagged", "seconds": 0.2, "receipts": receipts}))
    return run, reference, records


def test_an_exact_read_passes_and_a_wrong_read_fails_the_gate_through_the_imported_scorer(tmp_path):
    run, reference, records = _run(tmp_path, {"aaaa": _read([["Owner", "Status"], ["Gas", "Open"]]), "bbbb": _read([["Owner", "Status"], ["Gas", "Closed"]])})
    keys = sorted(records)
    development = [key for key in keys if not records[key]["holdout"]]
    holdout = [key for key in keys if records[key]["holdout"]]

    summary = measurement.score_selected(run, reference, keys, records, holdout_included=True)
    assert summary["pairs"] == 2 and summary["pairs_pass"] == 1
    assert (run / "SUMMARY.md").is_file() and (run / "summary.json").is_file()

    passing = measurement.measures(run / "scores", development, records, {})
    failing = measurement.measures(run / "scores", holdout, records, {})
    assert (passing["pairs"], passing["pages"], passing["cells_exact"]) == ([1, 1], [1, 1], [4, 4])
    assert (failing["pairs"], failing["pages"], failing["cells_exact"]) == ([0, 1], [0, 1], [3, 4])
    assert failing["failing_pairs"] == holdout
    assert failing["by_subclass"] == {"value_mismatch/different": 1, "uncovered/value_mismatch": 1}

    assert measurement.gate(passing) == {"all_pairs_pass": True, "failing_pairs": [], "failing_pages": 0, "unscored_keys": [], "read_errors": []}
    assert measurement.gate(failing)["all_pairs_pass"] is False
    assert measurement.gate(failing)["failing_pairs"] == holdout


def test_a_read_error_or_an_unscored_key_fails_the_gate(tmp_path):
    run, reference, records = _run(tmp_path, {"aaaa": _read([["Owner", "Status"], ["Gas", "Open"]])})
    keys = sorted(records)
    measurement.score_selected(run, reference, keys, records, holdout_included=True)

    result = measurement.measures(run / "scores", keys, records, {})
    assert result["pairs"] == [1, 1]
    assert result["unscored_keys"] == [key for key in keys if records[key]["holdout"]]
    assert measurement.gate(result)["all_pairs_pass"] is False

    errored = measurement.measures(run / "scores", keys, records, {keys[0]: "ValueError: broken"})
    assert measurement.gate(errored)["read_errors"] == [keys[0]]
    assert measurement.gate(errored)["all_pairs_pass"] is False


def test_a_pair_the_baseline_passed_and_this_run_fails_is_a_regression(tmp_path):
    baseline_run, reference, records = _run(tmp_path / "baseline", {"aaaa": _read([["Owner", "Status"], ["Gas", "Open"]]), "bbbb": _read([["Owner", "Status"], ["Gas", "Open"]])})
    keys = sorted(records)
    measurement.score_selected(baseline_run, reference, keys, records, holdout_included=True)
    run, reference, _ = _run(tmp_path / "candidate", {"aaaa": _read([["Owner", "Status"], ["Gas", "Open"]]), "bbbb": _read([["Owner", "Status"], ["Gas", "Closed"]])})
    measurement.score_selected(run, reference, keys, records, holdout_included=True)
    wrong = next(key for key in keys if records[key]["holdout"])

    comparison = measurement.compare_with_baseline(run / "scores", baseline_run / "scores", keys)

    assert comparison["pairs_compared"] == 2
    assert comparison["pairs_regressed"] == [wrong]
    assert comparison["pairs_improved"] == []
    assert comparison["pages_pass"] == {"baseline": 2, "this_run": 1}
    assert comparison["cells_exact"] == {"baseline": 8, "this_run": 7}
    assert comparison["regressed"] is True

    same = measurement.compare_with_baseline(baseline_run / "scores", baseline_run / "scores", keys)
    assert same["regressed"] is False and same["pairs_regressed"] == []


def test_the_holdout_is_refused_without_an_actor_and_a_reason(tmp_path, capsys):
    assert measurement.main(["--configuration", "frozen-reader", "--output", str(tmp_path / "run"), "--include-holdout"]) == 2
    assert "ADR-0008" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()

    assert measurement.main(["--configuration", "frozen-reader", "--output", str(tmp_path / "run"), "--include-holdout", "--holdout-actor", "someone"]) == 2


def test_a_run_without_a_registration_is_refused_before_anything_is_read(tmp_path, capsys):
    assert measurement.main(["--configuration", "drawn-grid", "--output", str(tmp_path / "run"), "--registry", str(tmp_path / "registry")]) == 2
    assert "not registered" in capsys.readouterr().err


def test_a_holdout_access_is_appended_with_its_result_and_never_rewritten(tmp_path):
    ledger = tmp_path / "holdout-access.jsonl"
    configuration = {
        "name": "frozen-reader",
        "engine": "tagged",
        "dpi": 36,
        "reader": {"commit": "c39363e26c2726b61c4e707f589093c67173e538", "package_digest": "58ba2e3d" + "0" * 56},
        "environment": {"packages": {"pypdfium2": "5.13.0", "pypdf": "6.17.0"}, "pdfium_build": "153.0.7999.0"},
    }
    dataset = {"dataset_version": "2026-09-06.1", "sha256": "ab" * 32}

    scored = measurement.holdout_entry(
        run="run-1", receipt_path="out/run-1/receipt.json", actor="codex/731 lane for bharm16", reason="regression check",
        configuration=configuration, dataset=dataset, holdout_keys=["k1", "k2"], holdout_pages=9, all_holdout=False,
        result={"status": "scored", "pairs": [2, 2], "pages": [9, 9], "cells_exact": [40, 40], "failing_pairs": []},
    )
    registry.append_holdout_access(scored, ledger)
    failed = measurement.holdout_entry(
        run="run-2", receipt_path="out/run-2/receipt.json", actor="codex/731 lane for bharm16", reason="regression check",
        configuration=configuration, dataset=dataset, holdout_keys=["k1"], holdout_pages=4, all_holdout=False,
        result={"status": "failed", "error": "RuntimeError: read failed"},
    )
    registry.append_holdout_access(failed, ledger)

    entries = registry.load_holdout_ledger(ledger)
    assert [entry["run"] for entry in entries] == ["run-1", "run-2"]
    assert all(entry["schema_version"] == registry.LEDGER_SCHEMA for entry in entries)
    assert entries[0]["result"]["pairs"] == [2, 2] and entries[0]["holdout"]["pairs"] == 2
    assert entries[0]["configuration"]["commit"].startswith("c39363e") and entries[0]["configuration"]["pdfium"] == "153.0.7999.0"
    assert entries[0]["dataset"]["registration_sha256"] == "ab" * 32
    assert entries[1]["result"]["status"] == "failed"
    assert entries[0]["holdout"]["keys_sha256"] != entries[1]["holdout"]["keys_sha256"]

    with pytest.raises(ValueError, match="needs reason"):
        registry.append_holdout_access({"run": "x", "actor": "a", "purpose": "p", "configuration": {}, "result": {}}, ledger)
    assert len(registry.load_holdout_ledger(ledger)) == 2


def test_the_receipt_index_refuses_a_second_receipt_under_one_run_name(tmp_path):
    index = tmp_path / "receipts.json"
    entry = {"run": "r", "role": "measurement", "path": "gold/pdf-pairs/v1/receipts/r", "configuration": {}, "measures": {}, "files": {}}
    registry.register_receipt(entry, index)

    with pytest.raises(ValueError, match="already holds"):
        registry.register_receipt(entry, index)
    assert registry.baseline_receipt(index) is None
    registry.register_receipt({**entry, "run": "b", "role": "baseline"}, index)
    assert registry.baseline_receipt(index)["run"] == "b"


def test_the_markdown_receipt_keeps_the_three_measures_and_the_limits(tmp_path):
    receipt = {
        "run": "run-1",
        "configuration": {"name": "frozen-reader", "description": "d", "engine": "tagged", "dpi": 36, "jobs": 4, "reader": {"commit": "c39363e" + "0" * 33, "package_digest": "58ba2e3d" + "0" * 56}, "environment": {"packages": {"pypdfium2": "5.13.0", "pypdf": "6.17.0"}, "pdfium_build": "153.0.7999.0"}},
        "dataset": {"dataset_version": "2026-09-06.1", "sha256": "ab" * 32, "corpus_manifest_sha256": "cd" * 32, "split_sha256": "ef" * 32},
        "selection": {"development_keys": 1, "holdout_keys": 1, "subset": True},
        "reference": {"identical_bytes": 2, "keys": 2},
        "results": {"development": {"pairs": [1, 1], "pages": [1, 1], "cells_exact": [4, 4], "failing_pairs": []}, "holdout": {"pairs": [0, 1], "pages": [0, 1], "cells_exact": [3, 4], "failing_pairs": ["bbbb"]}},
        "gate": {"development": {"all_pairs_pass": True, "failing_pairs": [], "failing_pages": 0, "read_errors": []}, "holdout": {"all_pairs_pass": False, "failing_pairs": ["bbbb"], "failing_pages": 1, "read_errors": []}},
        "baseline_comparison": {"against": "base", "regressed": True, "by_set": {"holdout": {"pairs_compared": 1, "pairs_regressed": ["bbbb"], "pairs_improved": [], "pages_pass": {"baseline": 1, "this_run": 0}, "cells_exact": {"baseline": 4, "this_run": 3}}}},
        "holdout": {"included": True, "actor": "codex/731 lane for bharm16", "reason": "r", "ledger": "gold/pdf-pairs/v1/holdout-access.jsonl"},
        "limits": registry.LIMITS,
    }

    text = measurement.receipt_markdown(receipt)

    assert "| development | 1 / 1 | 1 / 1 | 4 / 4 | none |" in text
    assert "| holdout | 0 / 1 | 0 / 1 | 3 / 4 | bbbb |" in text
    assert "regression against the baseline: **yes**" in text
    assert registry.LIMITS in text
