"""The Report Reading payload: what one dated report occurrence published.

``report_runs.snapshot_json`` and ``scheduled_report_publications.snapshot_json``
were called a *snapshot*, then — when #602 bound each reading to the accepted
Project Record revision it was taken against — a *rebuildable compatibility
cache* awaiting an expiry rule.  Both names were wrong, and #603 is what proved
it.  Rebuilding every retained run's baseline from its revision reproduced the
two record fields the spine carries and the Ledger identity, over 212 field
comparisons with no disagreement, and left three things behind that no revision
answers even in principle: the documentation-requirement result and the
Constraint Alerts of one reading, the population the report covered, and the
date projected from the external party's current statement.

ADR-0092 records what that residue means.  A Report Run **retains the reading it
published**.  Three owners, not two: the Project Record revision owns accepted
values and decisions, the Report Reading occurrence owns the population, the
derived outcomes, the projected Promised For and the rules and thresholds that
produced them, and the release package owns the issued bytes.  The payload is
immutable evidence of a dated occurrence, retained as long as the run or the
released package is retained, and it has no cache TTL — building one would be a
defect.

This module owns the payload's shape and nothing else.  It holds the schema
version, the content digest a retained reading is checked against, the governed
names version 2 writes, and the one translation that lets a version 1 payload be
read without being rewritten.  It deliberately imports nothing from the rest of
the application: a reader of a ten-month-old payload must not depend on today's
report, ledger, or evaluation code to make sense of it.

**Why version 2 renames three keys rather than adding aliases beside them.**
Version 1 stored ``ready``, ``exceptions`` and ``committed_date``.  The first two
are implementation words for quantities ADR-0002 and ADR-0010 already govern.
The third is the actual defect: the spine carries a ``committed_date`` Fact that
is the *source cell*, while the payload's ``committed_date`` is the date
projected from the external party's current statement — ``None`` where no
statement was recorded, whatever the cell says.  One key carried two quantities,
and any reader that let one stand in for the other would change a diff because
its inputs changed shape.  Writing both names in version 2 would preserve the
ambiguity and duplicate a value inside one row, so version 2 writes only the
governed names and ``read_entries`` translates version 1 on the way in.
"""

from __future__ import annotations

from corridor import digests
from collections.abc import Mapping
from typing import Any

# The version this code writes.  A payload with no version key at all was
# written before the key existed; it is version 1, and it is read as written
# rather than rewritten or backfilled (ADR-0092).
PAYLOAD_SCHEMA_VERSION = "report-reading-v2"
LEGACY_PAYLOAD_SCHEMA_VERSION = "report-reading-v1"

# The key the content digest is stored under, excluded from its own input.
DIGEST_KEY = "content_sha256"
SCHEMA_VERSION_KEY = "payload_schema_version"

# The rule that turns an External Party Statement into the date a report
# publishes as Promised For.  It names the projection performed by
# ``dependency_events.StatementPublication.committed_dates``; a change to that
# projection advances this version, so a retained payload always says which
# rule produced the value it holds.
PROMISED_FOR_PROJECTION_RULE_VERSION = "statement-projected-promised-for-v1"
PROJECTION_RULE_KEY = "promised_for_projection_rule_version"

# Version 1's per-record key, and the governed name version 2 writes for the
# same quantity.  ``published_promised_for`` is the statement-projected value
# (internally the statement-projected Promised For); it is never the record's
# own ``committed_date`` Fact, which is why the name had to change rather than
# be aliased.
LEGACY_TO_GOVERNED: Mapping[str, str] = {
    "ready": "documentation_requirement_met",
    "exceptions": "constraint_alerts",
    "committed_date": "published_promised_for",
}


def payload_schema_version(payload: Mapping[str, Any] | None) -> str:
    """The version a retained payload was written under.

    An absent key is version 1 rather than an error: those payloads are
    retained evidence and are readable exactly as they were written.
    """

    if not payload:
        return LEGACY_PAYLOAD_SCHEMA_VERSION
    return str(payload.get(SCHEMA_VERSION_KEY) or LEGACY_PAYLOAD_SCHEMA_VERSION)


def content_digest(payload: Mapping[str, Any]) -> str:
    """The digest of one payload's canonical bytes, excluding the digest key.

    Canonical means sorted keys and no incidental whitespace, so a payload
    round-tripped through JSONB digests to the same value it was sealed with.
    This is the reading's own identity and is deliberately separate from the
    release package's digest over the issued artifact (ADR-0086): one says the
    retained reading is intact, the other says which bytes were issued.
    """

    without_digest = {
        key: value for key, value in payload.items() if key != DIGEST_KEY
    }
    # Retained encoding: every sealed reading already carries a digest
    # computed with non-ASCII escaped, and verification re-derives it.
    return digests.ascii_escaped_sha256(without_digest)


def seal(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Stamp a freshly built payload with its schema version and digest."""

    sealed = {**payload, SCHEMA_VERSION_KEY: PAYLOAD_SCHEMA_VERSION}
    sealed[DIGEST_KEY] = content_digest(sealed)
    return sealed


def digest_is_intact(payload: Mapping[str, Any] | None) -> bool | None:
    """Whether a retained payload still matches the digest it was sealed with.

    ``None`` where the payload carries no digest — every version 1 payload —
    because "not checkable" and "checked and wrong" are different answers and
    a caller must not read the first as the second.
    """

    if not payload or DIGEST_KEY not in payload:
        return None
    return payload[DIGEST_KEY] == content_digest(payload)


def read_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One retained per-record entry under the governed names.

    A key absent from the payload stays absent.  That matters: a payload
    written before #96 recorded no ``resolution_strategy`` at all, and the diff
    reads that absence as "unknown", never as "was not critical" (ADR-0044).
    Inventing a key here would report an escalation for every record on the
    first report after a migration.
    """

    translated = {
        key: value for key, value in entry.items() if key not in LEGACY_TO_GOVERNED
    }
    for legacy, governed in LEGACY_TO_GOVERNED.items():
        if governed in entry or legacy not in entry:
            continue
        translated[governed] = entry[legacy]
    return translated


def read_entries(payload: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The population a payload published, keyed by ``ref_code``.

    The population is the occurrence's own — which subjects this report
    included under this report's scope and rules — so it is read from the
    payload and never from a revision, which cannot answer it (ADR-0092).
    """

    entries = (payload or {}).get("dependencies") or {}
    return {
        ref_code: read_entry(entry)
        for ref_code, entry in entries.items()
        if isinstance(entry, Mapping)
    }
