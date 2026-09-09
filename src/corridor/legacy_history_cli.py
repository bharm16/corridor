"""Inventory, rehearse and export a named legacy-history migration batch.

The operator reviews a digest before capture. The migration database principal
must own the narrow commands; runtime login credentials cannot execute them.
No command here switches accepted writers or deletes history.
"""

from dataclasses import replace
import argparse
import json
from pathlib import Path

from sqlalchemy import text

from corridor.db import Session, engine
from corridor.legacy_history import (
    backfill_evidence_sources, capture_history, inventory_history, read_history, reverse_history,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inventory", "capture", "backfill-evidence", "migrate-coordination", "reverse", "export"))
    parser.add_argument("--project", required=True, type=int)
    parser.add_argument("--batch", type=int)
    parser.add_argument("--expected-digest")
    parser.add_argument("--run-key")
    parser.add_argument("--executor")
    parser.add_argument("--code-revision")
    parser.add_argument("--reason")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.action == "capture" and not all((args.expected_digest, args.run_key, args.executor, args.code_revision)):
        parser.error("capture requires --expected-digest, --run-key, --executor and --code-revision")
    if args.action in {"backfill-evidence", "migrate-coordination", "reverse", "export"} and args.batch is None:
        parser.error("this action requires --batch")
    if args.action == "reverse" and not all((args.executor, args.reason)):
        parser.error("reverse requires --executor and --reason")
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            with Session(bind=connection) as session:
                if args.action in {"inventory", "capture"}:
                    inventory = inventory_history(session, args.project)
                    result = {"project_id": args.project, "content_sha256": inventory.content_sha256,
                              "counts": inventory.counts,
                              "classes": {key: {"treatment": value["treatment"], "expiry": value["expiry"]}
                                          for key, value in inventory.classes.items()}}
                    if args.action == "capture":
                        # The command handles both replay of an existing run
                        # and compare-and-swap of a new reviewed inventory.
                        inventory = replace(inventory, content_sha256=args.expected_digest)
                        batch = capture_history(session, inventory, run_key=args.run_key,
                                                executor=args.executor, code_revision=args.code_revision)
                        result["batch_id"] = batch.id
                        result["content_sha256"] = batch.content_sha256
                        result["counts"] = batch.counts
                else:
                    batch = read_history(session, args.project, args.batch)
                    result = {"project_id": args.project, "batch_id": batch.id, "content_sha256": batch.content_sha256,
                              "captured_at": batch.captured_at.isoformat(), "executor": batch.executor,
                              "code_revision": batch.code_revision, "counts": batch.counts, "reversed": batch.reversed}
                    if args.action == "backfill-evidence":
                        result["evidence_migrations"] = backfill_evidence_sources(session, batch)
                    elif args.action == "migrate-coordination":
                        from corridor.coordination_history import migrate_coordination_history, coordination_migration_gaps
                        decisions = migrate_coordination_history(session, batch)
                        result["native_decision_ids"] = [row.id for row in decisions]
                        result["retained_compatibility"] = coordination_migration_gaps(session, batch)
                    elif args.action == "reverse":
                        result["reversed"] = reverse_history(session, batch, actor=args.executor, reason=args.reason).reversed
                    else:
                        # Export the actual canonical bytes whose digest the
                        # database verified, so offline verification is simple
                        # sha256(canonical_history.encode('utf-8')).
                        result["canonical_history"] = session.scalar(text(
                            "select payload::text from legacy_history_batches where id=:id and project_id=:project"),
                            {"id": batch.id, "project": args.project})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
