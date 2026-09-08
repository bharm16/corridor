"""Inventory the persisted citations that retained decisions and releases use.

#741 may not remove a reading implementation that some accepted decision or
released artifact still depends on for its meaning.  Answering that needs the
actual persisted population, not a reconstruction: a corpus re-read tells you
what the reader does today, and says nothing about which stored rows exist and
what produced them.

So this walks the schema rather than a hand-written list.  It asks PostgreSQL
which tables hold a foreign key into `source_segments`, counts the rows in
each, and for every segment those tables reach it records the four things a
retirement decision needs:

  * the original document's digest, and whether the registered document still
    carries the same one;
  * the reading identity -- the exact reader result the citation was taken
    from;
  * the reader and configuration identity, which names the engine, its
    version and the settings it ran under;
  * the locator scheme, which is what a replacement has to be able to
    interpret.

It exports no source text and no customer names.  It runs in a read-only
REPEATABLE READ transaction and rolls back.

What it cannot do is tell you about an environment it was not pointed at.  A
zero-row answer here is evidence about *this* database and nothing else, and
the receipt says so in those words rather than reporting a passing check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parent.parent
RECEIPT_DIR = REPO_ROOT / "artifacts" / "pdf-engine-retirement"

# The rows whose meaning a retirement could damage.  Named with the reason
# each one qualifies, because "we checked thirty tables" is not evidence
# unless the reader can see why those thirty.
ANCHORS: tuple[tuple[str, str], ...] = (
    ("fact_decisions", "an accepted Source Fact decision"),
    ("delta_record_decisions", "a resolved Proposed Delta"),
    ("project_record_revisions", "an atomic accepted-record revision"),
    ("project_baseline_adoptions", "an adopted baseline receipt"),
    ("report_runs", "a report run, which retains the reading it published"),
    ("release_candidates", "a prepared release candidate"),
    ("release_candidate_artifacts", "a candidate's rendered bytes"),
    ("release_packages", "an authorized release package"),
    ("release_package_artifacts", "a released package's rendered bytes"),
    ("external_report_releases", "a legacy external release"),
    ("external_report_artifacts", "a legacy external release's bytes"),
    ("documents", "a registered source document"),
    ("source_segments", "a persisted citation"),
    ("facts", "a captured Source Fact"),
)

# The native reading columns.  Their absence is schema staleness, which is a
# different fact from an absence of rows and must not be reported as one: a
# database whose `source_segments` cannot hold a reader identity would return
# zero native citations however many it had.
NATIVE_COLUMNS = (
    "rendition_sha256",
    "reading_sha256",
    "reader_identity",
    "location_json",
    "span_stream",
    "table_index",
    "cell_row",
    "cell_column",
    "row_span",
    "column_span",
)

# `information_schema` cannot answer this: for a composite foreign key it
# reports every column of the constraint, so joining it produces the scoping
# columns alongside the segment column and multiplies them together. The
# catalogue pairs each referring column with the column it references, which
# is what "a foreign key into source_segments.id" actually means.
REFERENCE_TABLES = text(
    """
    SELECT DISTINCT
           referring.relname AS referring_table,
           referring_column.attname AS referring_column,
           constraint_.conname AS constraint_name
    FROM pg_constraint AS constraint_
    JOIN pg_class AS referring ON referring.oid = constraint_.conrelid
    JOIN pg_class AS referenced ON referenced.oid = constraint_.confrelid
    JOIN LATERAL unnest(constraint_.conkey, constraint_.confkey)
         AS pair(referring_attnum, referenced_attnum) ON true
    JOIN pg_attribute AS referring_column
      ON referring_column.attrelid = constraint_.conrelid
     AND referring_column.attnum = pair.referring_attnum
    JOIN pg_attribute AS referenced_column
      ON referenced_column.attrelid = constraint_.confrelid
     AND referenced_column.attnum = pair.referenced_attnum
    WHERE constraint_.contype = 'f'
      AND referenced.relname = :referenced
      AND referenced_column.attname = 'id'
    ORDER BY referring_table, referring_column, constraint_name
    """
)

SEGMENT_INVENTORY = """
    SELECT s.id,
           s.kind,
           s.document_id,
           s.rendition_sha256,
           s.reading_sha256,
           s.reader_identity,
           s.content_sha256,
           d.sha256 AS document_sha256,
           d.media_type
    FROM source_segments AS s
    LEFT JOIN documents AS d ON d.id = s.document_id
    WHERE s.id IN (SELECT segment_id FROM referenced)
    ORDER BY s.id
"""


def _exists(connection, table: str) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": table},
        ).first()
    )


def _columns(connection, table: str) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": table},
        )
    }


def inventory(url: str, label: str) -> dict[str, object]:
    engine = create_engine(url)
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        connection.execute(text("SET TRANSACTION READ ONLY"))

        server = dict(
            connection.execute(
                text(
                    "SELECT current_database() AS database_name, "
                    "current_user AS role_name, "
                    "inet_server_addr()::text AS server_address, "
                    "inet_server_port() AS server_port, "
                    "version() AS server_version, "
                    "current_setting('transaction_read_only') AS read_only, "
                    "current_setting('transaction_isolation') AS isolation"
                )
            ).mappings().one()
        )
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()

        segment_columns = _columns(connection, "source_segments")
        absent_native = [c for c in NATIVE_COLUMNS if c not in segment_columns]

        # One entry per referring column, not per constraint: two constraints
        # can enforce different scopes through the same column, and counting
        # them twice would overstate what was inspected.
        found: dict[tuple[str, str], dict[str, object]] = {}
        for row in connection.execute(
            REFERENCE_TABLES, {"referenced": "source_segments"}
        ).mappings():
            key = (row["referring_table"], row["referring_column"])
            entry = found.setdefault(
                key,
                {
                    "referring_table": row["referring_table"],
                    "referring_column": row["referring_column"],
                    "constraints": [],
                },
            )
            entry["constraints"].append(row["constraint_name"])  # type: ignore[union-attr]
        references = list(found.values())
        for reference in references:
            table, column = reference["referring_table"], reference["referring_column"]
            reference["rows"] = connection.execute(
                text(f'SELECT count(*) FROM "{table}"')  # noqa: S608 - schema names
            ).scalar_one()
            reference["links"] = connection.execute(
                text(  # noqa: S608 - schema names, not user input
                    f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NOT NULL'
                )
            ).scalar_one()

        anchors = []
        for table, reason in ANCHORS:
            present = _exists(connection, table)
            anchors.append(
                {
                    "table": table,
                    "why_it_qualifies": reason,
                    "present": present,
                    "rows": connection.execute(
                        text(f'SELECT count(*) FROM "{table}"')  # noqa: S608
                    ).scalar_one()
                    if present
                    else None,
                }
            )

        linked = [r for r in references if r["links"]]
        citations: list[dict[str, object]] = []
        if linked:
            union = " UNION ".join(
                f'SELECT "{r["referring_column"]}" AS segment_id '
                f'FROM "{r["referring_table"]}" '
                f'WHERE "{r["referring_column"]}" IS NOT NULL'
                for r in linked
            )
            rows = connection.execute(
                text(f"WITH referenced AS ({union}) {SEGMENT_INVENTORY}")  # noqa: S608
            ).mappings()
            for row in rows:
                identity = row["reader_identity"] or {}
                native = (identity or {}).get("native_layer") or {}
                citations.append(
                    {
                        "segment_id": row["id"],
                        "locator_scheme": row["kind"],
                        "document_id": row["document_id"],
                        "original_document_sha256": row["document_sha256"],
                        "cited_rendition_sha256": row["rendition_sha256"],
                        "digest_agrees_with_registered_document": (
                            row["rendition_sha256"] is None
                            or row["rendition_sha256"] == row["document_sha256"]
                        ),
                        "reading_sha256": row["reading_sha256"],
                        "reader_scheme": (identity or {}).get("scheme"),
                        "reader_engine": native.get("engine"),
                        "reader_engine_version": native.get("engine_version"),
                        "reader_configuration": native.get("configuration"),
                        "content_sha256": row["content_sha256"],
                        "media_type": row["media_type"],
                    }
                )
        connection.rollback()

    by_scheme: dict[str, int] = {}
    for citation in citations:
        scheme = str(citation["locator_scheme"])
        by_scheme[scheme] = by_scheme.get(scheme, 0) + 1

    return {
        "environment_label": label,
        "connection": {
            "configured_url_without_credentials": url.split("@")[-1],
            "server": {k: str(v) for k, v in server.items()},
            "alembic_revision": revision,
        },
        "schema": {
            "source_segments_columns": sorted(segment_columns),
            "absent_native_reading_columns": absent_native,
            "can_hold_a_native_reading_identity": not absent_native,
        },
        "segment_reference_tables": references,
        "retained_anchors": anchors,
        "citations": citations,
        "citations_by_locator_scheme": by_scheme,
    }


def summary(receipt: dict[str, object]) -> str:
    body = receipt["inventory"]
    references = body["segment_reference_tables"]  # type: ignore[index]
    anchors = body["retained_anchors"]  # type: ignore[index]
    citations = body["citations"]  # type: ignore[index]
    populated = [a for a in anchors if a["rows"]]
    linked = [r for r in references if r["links"]]

    lines = [
        "# Retained-citation inventory for the PDF engine retirement",
        "",
        f"Environment inspected: **{body['environment_label']}**, recorded "  # type: ignore[index]
        f"{receipt['recorded_at']}.",
        "",
        f"Database `{body['connection']['server']['database_name']}` at "  # type: ignore[index]
        f"`{body['connection']['server']['server_address']}:"  # type: ignore[index]
        f"{body['connection']['server']['server_port']}`, "  # type: ignore[index]
        f"schema revision `{body['connection']['alembic_revision']}`, read in a "  # type: ignore[index]
        "read-only REPEATABLE READ transaction that was rolled back.",
        "",
        "## Finding",
        "",
    ]
    if citations:
        lines += [
            f"{len(citations)} persisted citations are referenced by retained "
            "decisions or released artifacts. Their locator schemes: "
            + ", ".join(
                f"{scheme} ({count})"
                for scheme, count in sorted(
                    body["citations_by_locator_scheme"].items()  # type: ignore[index]
                )
            )
            + ".",
        ]
    else:
        lines += [
            "**No affected retained citations in this environment.** Every "
            f"one of the {len(references)} tables that hold a foreign key into "
            "`source_segments` holds zero links, and every one of the "
            f"{len(anchors)} retained decision and release tables inspected "
            "holds zero rows. There is therefore no persisted citation in "
            "this database whose meaning either retired engine could carry.",
        ]
    lines += [
        "",
        "## What was inspected",
        "",
        "Source Segment reference tables, discovered from the live schema "
        "rather than listed by hand:",
        "",
    ]
    for reference in references:
        lines.append(
            f"- `{reference['referring_table']}.{reference['referring_column']}`: "
            f"{reference['rows']} rows, {reference['links']} non-null links"
        )
    lines += ["", "Retained decision and release tables:", ""]
    for anchor in anchors:
        state = (
            f"{anchor['rows']} rows" if anchor["present"] else "**table absent**"
        )
        lines.append(f"- `{anchor['table']}` ({anchor['why_it_qualifies']}): {state}")

    absent = body["schema"]["absent_native_reading_columns"]  # type: ignore[index]
    lines += ["", "## Limits of this evidence", ""]
    lines += [
        "- This is one database: the one this checkout is configured against. "
        "It is not evidence about a deployed environment, a customer "
        "database, a backup, or any other database, and nothing here should "
        "be read as proving anything about them.",
        "- A zero-row result is an absence of population, not a passing "
        "historical-compatibility check. The question 'can a retained "
        "citation survive retirement?' is answered by the seeded contract "
        "tests, not by this count.",
        "- No corpus population was reconstructed and offered as customer "
        "history. Re-reading corpus files would describe what the reader does "
        "now, not which rows exist and what produced them.",
        "- No source text, quoted passage or customer name was read or "
        "exported. Digests and identities only.",
    ]
    if absent:
        lines.append(
            "- This schema is stale for native readings: "
            + ", ".join(f"`{c}`" for c in absent)
            + " are absent from `source_segments`, so it could not record a "
            "native reader identity even if rows existed. Schema staleness is "
            "a separate fact from an absence of rows."
        )
    if populated:
        lines.append(
            "- Populated anchors: "
            + ", ".join(f"`{a['table']}` ({a['rows']})" for a in populated)
        )
    if linked:
        lines.append(
            "- Linked reference tables: "
            + ", ".join(
                f"`{r['referring_table']}.{r['referring_column']}` ({r['links']})"
                for r in linked
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="defaults to the configured database for this checkout",
    )
    parser.add_argument(
        "--environment-label",
        default="local development database for this checkout",
        help="what environment the receipt should say was inspected",
    )
    parser.add_argument("--receipt", type=Path, default=None)
    options = parser.parse_args()

    url = options.database_url or os.environ.get("CORRIDOR_DATABASE_URL")
    if url is None:
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from corridor.config import settings  # noqa: PLC0415 - optional import

        url = settings.database_url

    receipt = {
        "schema_version": "corridor.retained-citation-inventory.v1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "issue": 741,
        "inventory": inventory(url, options.environment_label),
    }
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = options.receipt or (
        RECEIPT_DIR / f"retained-citation-inventory-{stamp}.json"
    )
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n")
    destination.with_suffix(".md").write_text(summary(receipt))
    print(f"receipt: {destination.relative_to(REPO_ROOT)}")
    body = receipt["inventory"]
    print(
        f"{len(body['citations'])} referenced citations; "
        f"{len(body['segment_reference_tables'])} reference tables; "
        f"{len(body['retained_anchors'])} retained anchors"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
