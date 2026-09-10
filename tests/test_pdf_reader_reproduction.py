"""The reproduction driver's bookkeeping, without the corpus (#729).

`make pdf-reader-reproduce` itself is an explicit experiment outside CI. What
CI can prove cheaply is that the driver counts a score set the way the
scorer's summary does, compares built keys with the retained digests file for
file, reports a difference from loop-020 exactly rather than rounding it
away, and writes a holdout-access entry that carries the configuration
identity ADR-0008 asks for -- as prose in the loop log, and as a line in the
one holdout ledger, because the loop log is where the spend was recorded and
the ledger is where every spend is accounted for.
"""

from __future__ import annotations

import json
from pathlib import Path

from corridor import holdout_ledger
from corridor_pdf_reader import reproduction


def _score(path: Path, key: str, *, passed: bool, pages: list[bool], exact: int, reference: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{key}.json").write_text(
        json.dumps(
            {
                "key": key,
                "pass": passed,
                "pages": [{"number": index + 1, "pass": page} for index, page in enumerate(pages)],
                "pages_pass": sum(pages),
                "exact_cells": exact,
                "reference_cells": reference,
            }
        )
    )


def test_score_counts_add_up_the_way_the_summary_does(tmp_path):
    scores = tmp_path / "scores"
    _score(scores, "a", passed=True, pages=[True, True], exact=10, reference=10)
    _score(scores, "b", passed=False, pages=[True, False, True], exact=8, reference=9)
    _score(scores, "c", passed=True, pages=[True], exact=3, reference=3)

    counts = reproduction.score_counts(scores, ["a", "b", "missing"])

    assert counts == {
        "pairs": [1, 2],
        "pages": [4, 5],
        "cells_exact": [18, 19],
        "failing_pairs": ["b"],
    }


def test_compare_keys_names_identical_different_and_absent_keys(tmp_path):
    built = tmp_path / "reference"
    built.mkdir()
    (built / "same.json").write_text('{"key": "same"}\n')
    (built / "changed.json").write_text('{"key": "changed", "cells": 2}\n')
    manifest = tmp_path / "loop-reference-v6.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "entries": [
                    {"path": "same.json", "sha256": reproduction.sha256_file(built / "same.json")},
                    {"path": "changed.json", "sha256": "0" * 64},
                    {"path": "unbuilt.json", "sha256": "1" * 64},
                ]
            }
        )
    )

    comparison = reproduction.compare_keys(built, manifest, ["same", "changed", "unbuilt"])

    assert comparison["keys"] == 3
    assert comparison["identical_bytes"] == 1
    assert comparison["different"] == ["changed"]
    assert comparison["absent"] == ["unbuilt"]
    assert comparison["retained_manifest"] == str(manifest)


def test_a_difference_from_loop_020_is_recorded_exactly():
    exact = reproduction.compare_with_loop_020(
        {"pairs": [262, 263], "pages": [1589, 1589]}, {"pairs": [70, 70], "pages": [409, 409]}
    )
    assert exact["matches"] and exact["differences"] == []

    off = reproduction.compare_with_loop_020(
        {"pairs": [261, 263], "pages": [1589, 1589]}, {"pairs": [70, 70], "pages": [408, 409]}
    )
    assert not off["matches"]
    assert off["differences"] == [
        "development pairs: reproduced 261 of 263, loop-020 recorded 262 of 263",
        "holdout pages: reproduced 408 of 409, loop-020 recorded 409 of 409",
    ]


def _receipt() -> dict:
    return {
        "run": "reproduction-2026-09-06",
        "date": "2026-09-06",
        "source": {"commit": "c39363e26c2726b61c4e707f589093c67173e538", "package_digest": "58ba2e3d18fa690e" + "0" * 48},
        "reader": {"engine": "tagged", "dpi": 36, "jobs": 4},
        "environment": {"packages": {"pypdfium2": "5.13.0", "pypdf": "6.17.0"}, "pdfium_build": "153.0.7999.0"},
        "corpus": {"manifest_sha256": "ca68b55abfa583a1" + "0" * 48},
        "split": {"sha256": "529c5ffb00bc96f5" + "0" * 48},
        "keys": {"identical_bytes": 333, "keys": 333},
        "results": {
            "development": {"pairs": [262, 263], "pages": [1589, 1589], "cells_exact": [483210, 483336], "failing_pairs": []},
            "holdout": {"pairs": [70, 70], "pages": [409, 409], "cells_exact": [120000, 120000], "failing_pairs": []},
        },
        "loop_020": {"matches": True, "differences": []},
    }


def test_the_holdout_access_entry_carries_the_configuration_identity():
    entry = reproduction.loop_log_entry(_receipt())

    assert entry.startswith("\n## Holdout access 2026-09-06: reproduction inside Corridor (#729), no tuning")
    assert "ADR-0008" in entry and "commit `c39363e`" in entry and "engine `tagged`" in entry
    assert "| development | 262 / 263 | 1589 / 1589 | 483,210 / 483,336 |" in entry
    assert "| holdout | 70 / 70 | 409 / 409 |" in entry
    assert "reproduces loop-020 exactly" in entry
    assert "handed to #731" in entry


def test_the_ledger_entry_records_the_same_spend_as_the_loop_log_prose(tmp_path):
    """The prose stays; the ledger gets the same access as a validated line."""
    receipt = _receipt()

    entry = reproduction.holdout_access_entry(receipt, actor="local:tester")

    assert holdout_ledger.validate({"schema_version": holdout_ledger.SCHEMA, **entry}) == {
        "schema_version": holdout_ledger.SCHEMA, **entry,
    }
    assert entry["run"] == "reproduction-2026-09-06"
    assert entry["actor"] == "local:tester"
    assert "ADR-0008" in entry["purpose"] or "ADR-0008" in entry["reason"]
    assert entry["result"] == {
        "status": "scored",
        "pairs": [70, 70],
        "pages": [409, 409],
        "cells_exact": [120000, 120000],
        "failing_pairs": [],
        "loop_020": "reproduces loop-020 exactly",
    }
    assert entry["configuration"]["package_digest"] == receipt["source"]["package_digest"]
    assert entry["configuration"]["commit"] == receipt["source"]["commit"]
    assert entry["dataset"]["split_sha256"] == receipt["split"]["sha256"]
    assert entry["holdout"] == {"all": True, "pairs": 70, "pages": 409}
    assert reproduction.RECEIPTS.name in entry["receipt"] and receipt["run"] in entry["receipt"]


def test_retaining_a_run_without_an_actor_records_nothing(tmp_path):
    """An unattributed holdout access is refused before the loop log is touched."""
    assert reproduction.main(["--output", str(tmp_path / "unused"), "--retain"]) == 2
    assert not (tmp_path / "unused").exists()
