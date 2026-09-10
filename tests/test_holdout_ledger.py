"""One holdout access ledger, over both retained ledger files (ADR-0008).

ADR-0008 spends a holdout once and the ledger is the only record of that spend.
Two ledgers grew instead, in two schemas, and neither writer validated the
other's file. These tests hold the single module to both: it appends the one
schema, reads the retained bytes of both files unchanged, and validates every
line either of them already holds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corridor import holdout_ledger

REPO_ROOT = Path(__file__).parents[1]
PAIRS_LEDGER = REPO_ROOT / "gold" / "pdf-pairs" / "v1" / "holdout-access.jsonl"
PDF_LEDGER = REPO_ROOT / "gold" / "pdf" / "v1" / "holdout-access.jsonl"


def _entry(**overrides) -> dict:
    entry = {
        "run": "measure-2026-09-09",
        "actor": "local:tester",
        "reason": "one predeclared measurement, nothing tuned before or after (ADR-0008)",
        "purpose": "exercise the ledger",
        "configuration": {"engine": "tagged", "dpi": 36},
        "result": {"status": "scored", "pairs": [70, 70]},
    }
    entry.update(overrides)
    return entry


def test_the_written_schema_is_the_richer_paired_rendition_shape():
    assert holdout_ledger.SCHEMA == "corridor.pdf-pairs-holdout-access.v1"
    assert holdout_ledger.REQUIRED_FIELDS == (
        "run", "actor", "reason", "purpose", "configuration", "result",
    )


def test_the_older_shape_is_a_declared_legacy_variant_with_its_reason():
    """A schema this module reads but never writes says why it exists."""
    assert set(holdout_ledger.LEGACY_SCHEMAS) == {"corridor.pdf-holdout-access.v1"}
    reason = holdout_ledger.LEGACY_SCHEMAS["corridor.pdf-holdout-access.v1"]
    assert len(reason.split()) >= 12, "a legacy variant says why it is retained"
    assert "gold/pdf/v1" in reason


def test_append_writes_one_validated_line_and_read_returns_it(tmp_path):
    ledger = tmp_path / "nested" / "holdout-access.jsonl"

    first = holdout_ledger.append(_entry(), ledger=ledger)
    second = holdout_ledger.append(_entry(run="measure-2026-09-10"), ledger=ledger)

    assert first["schema_version"] == holdout_ledger.SCHEMA
    assert holdout_ledger.read(ledger) == [first, second]
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [first, second]
    assert all(line == json.dumps(json.loads(line), sort_keys=True, ensure_ascii=False) for line in lines)


def test_read_of_an_absent_ledger_is_empty_and_creates_nothing(tmp_path):
    absent = tmp_path / "holdout-access.jsonl"

    assert holdout_ledger.read(absent) == []
    assert not absent.exists()


@pytest.mark.parametrize("missing", holdout_ledger.REQUIRED_FIELDS)
def test_append_refuses_an_entry_that_does_not_say_what_was_spent(tmp_path, missing):
    entry = _entry()
    del entry[missing]

    with pytest.raises(holdout_ledger.HoldoutLedgerError, match=missing):
        holdout_ledger.append(entry, ledger=tmp_path / "holdout-access.jsonl")
    assert not (tmp_path / "holdout-access.jsonl").exists()


def test_append_refuses_an_unattributed_access(tmp_path):
    with pytest.raises(holdout_ledger.HoldoutLedgerError, match="actor"):
        holdout_ledger.append(_entry(actor=""), ledger=tmp_path / "holdout-access.jsonl")


def test_append_refuses_to_write_the_legacy_schema(tmp_path):
    """Both files are appended in one shape from here on."""
    with pytest.raises(holdout_ledger.HoldoutLedgerError, match="corridor.pdf-holdout-access.v1"):
        holdout_ledger.append(
            _entry(schema_version="corridor.pdf-holdout-access.v1"),
            ledger=tmp_path / "holdout-access.jsonl",
        )


def test_validate_refuses_a_schema_no_writer_ever_produced():
    with pytest.raises(holdout_ledger.HoldoutLedgerError, match="corridor.invented.v1"):
        holdout_ledger.validate({"schema_version": "corridor.invented.v1", **_entry()})


def test_every_line_of_both_retained_ledgers_validates():
    """One validator over both files, in whichever schema each line was written."""
    pairs = holdout_ledger.read(PAIRS_LEDGER)
    stage0 = holdout_ledger.read(PDF_LEDGER)

    assert [entry["run"] for entry in pairs[:3]] == ["loop-007", "loop-020", "reproduction-2026-09-06"]
    assert {entry["schema_version"] for entry in pairs} == {holdout_ledger.SCHEMA}
    assert {entry["schema_version"] for entry in stage0} <= (
        {holdout_ledger.SCHEMA} | set(holdout_ledger.LEGACY_SCHEMAS)
    )
    assert any(entry["schema_version"] in holdout_ledger.LEGACY_SCHEMAS for entry in stage0), (
        "the retained legacy lines stay exactly as they were appended"
    )
    assert all(entry["actor"] and entry["reason"] for entry in (*pairs, *stage0))


def test_the_legacy_lines_of_the_stage_0_ledger_are_not_rewritten():
    """Reading through the module leaves both files byte-for-byte as retained."""
    before = {path: path.read_bytes() for path in (PAIRS_LEDGER, PDF_LEDGER)}

    for path in before:
        holdout_ledger.read(path)

    assert {path: path.read_bytes() for path in before} == before


def test_both_writers_append_through_the_one_ledger():
    """The registry and the Stage 0 evaluator no longer own an appender each."""
    from corridor import pdf_evaluation_cli
    from corridor_pdf_reader import registry

    assert registry.LEDGER_SCHEMA is holdout_ledger.SCHEMA
    for module in (registry, pdf_evaluation_cli):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "holdout_ledger.append" in source, module.__name__
        assert 'holdout-access.v1"' not in source, (
            f"{module.__name__} still declares a ledger schema of its own"
        )
