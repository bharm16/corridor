"""One holdout access ledger, whichever spent dataset was read (ADR-0008).

ADR-0008 spends a holdout once, on one complete measurement, and the ledger is
the only thing that keeps the next reader honest about what has already been
read. Two ledgers grew instead of one: `gold/pdf-pairs/v1/holdout-access.jsonl`
in the paired-rendition registry's rich shape, and `gold/pdf/v1/
holdout-access.jsonl` in the Stage 0 evaluator's two-field one, each appended
by its own writer, neither validated by anything the other ran. A spend
recorded in a shape the other writer does not understand is a spend the other
writer cannot see, which is exactly the accounting ADR-0008 exists to keep.

So appending, reading and validating live here, once, for every spent dataset.
The written schema is the richer paired-rendition shape, because it is the one
that says what was measured and what came out: which run, who read it, why,
for what purpose, under which configuration, and with which result. The ledger
file itself is a parameter: the datasets are separate assets with separate
seals, and one schema over several files is what ADR-0008 asks for, not one
file.

The retained `corridor.pdf-holdout-access.v1` lines stay exactly as they were
appended. They are read as a declared legacy variant — `LEGACY_SCHEMAS` says
why each one exists — and never written again; a ledger is append-only, so a
correction is another entry rather than an edit, and every later access to
`gold/pdf/v1` is appended in the one schema beside them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = "corridor.pdf-pairs-holdout-access.v1"

# Read, never written. The name keeps its `pdf-pairs` spelling because the
# retained lines carry it and a ledger is not rewritten to tidy a name.
LEGACY_SCHEMAS: dict[str, str] = {
    "corridor.pdf-holdout-access.v1": (
        "the six accesses to the gold/pdf/v1 Stage 0 holdout that the PDF "
        "evaluation CLI appended before this module existed; they record the "
        "actor, the reason, the dataset version and the documents read, but "
        "no purpose, configuration identity or result, so what was measured "
        "has to be recovered from the receipt the entry does not name"
    ),
}

REQUIRED_FIELDS = ("run", "actor", "reason", "purpose", "configuration", "result")
LEGACY_REQUIRED_FIELDS = ("actor", "reason", "dataset_version", "document_sha256s")
# An access nobody is answerable for is not a record of a spend.
ATTRIBUTION_FIELDS = ("actor", "reason")


class HoldoutLedgerError(ValueError):
    """A holdout access entry does not say what was spent, by whom, or why."""


def validate(record: Any) -> dict[str, Any]:
    """Return the entry if it is a ledger line in the one schema or a legacy one."""
    if not isinstance(record, dict):
        raise HoldoutLedgerError("a holdout access entry is a JSON object")
    schema = record.get("schema_version")
    if schema == SCHEMA:
        required = REQUIRED_FIELDS
    elif schema in LEGACY_SCHEMAS:
        required = LEGACY_REQUIRED_FIELDS
    else:
        raise HoldoutLedgerError(f"{schema!r} is not a holdout access schema this ledger holds")
    for field in required:
        if field not in record:
            raise HoldoutLedgerError(f"a holdout access entry needs {field}")
    for field in ATTRIBUTION_FIELDS:
        if not record[field]:
            raise HoldoutLedgerError(f"a holdout access entry needs a {field}, not an empty one")
    return record


def append(entry: dict[str, Any], *, ledger: Path) -> dict[str, Any]:
    """Append one access to `ledger` in the one schema. The ledger is never rewritten."""
    declared = entry.get("schema_version", SCHEMA)
    if declared != SCHEMA:
        raise HoldoutLedgerError(
            f"{declared!r} is read but never written; every access is appended as {SCHEMA}"
        )
    record = validate({"schema_version": SCHEMA, **entry})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    return record


def read(ledger: Path) -> list[dict[str, Any]]:
    """Every validated access in `ledger`, oldest first; an absent ledger is empty."""
    if not Path(ledger).is_file():
        return []
    return [
        validate(json.loads(line))
        for line in Path(ledger).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
