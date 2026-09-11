"""Operate the content-addressed store: migrate local content, reconcile manifests.

Both passes are operator commands rather than product behavior. Migration is
the one-time (but rerunnable) move of a filesystem store into the configured
backend; reconciliation is the check an operator runs after a crash or a
restore. Each prints one JSON report and exits non-zero when the store and
the manifests disagree, so a scheduled run can alert on it.
"""

from __future__ import annotations

import argparse
import json
import sys

from corridor.db import WorkerSession
from corridor.object_storage import content_store, default_storage_root
from corridor.storage_operations import migrate_local_content, reconcile


_CONTRACT = """\
Operate the content-addressed store (ADR-0079). `migrate` puts every local
file under its own digest into the configured backend, idempotently and
digest-verified; `reconcile` compares the PostgreSQL manifests with the store
and reports orphans in both directions. Repairs are opt-in:
  make storage ARGS="migrate"
  make storage ARGS="reconcile --repair"
  make storage ARGS="reconcile --remove-unreferenced"
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="storage",
        description="Migrate local content into the configured store or reconcile it.",
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser(
        "migrate", help="put every local file under its digest; idempotent"
    )
    migrate.add_argument(
        "--source-root",
        default=None,
        help="local store layout to migrate (default: the configured corpus_store)",
    )
    reconcile_parser = commands.add_parser(
        "reconcile", help="compare manifests with the store and report orphans"
    )
    reconcile_parser.add_argument(
        "--repair",
        action="store_true",
        help="restore missing objects from verified local staged copies",
    )
    reconcile_parser.add_argument(
        "--remove-unreferenced",
        action="store_true",
        help="delete objects no manifest references; refused while any hold is active",
    )
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = _parser().parse_args(argv)
    factory = session_factory or WorkerSession
    store = content_store()
    with factory() as session:
        if args.command == "migrate":
            report = migrate_local_content(
                store,
                source_root=args.source_root or default_storage_root(),
                session=session,
            )
            payload = report.as_dict()
            failed = bool(report.mismatched or report.conflicting)
        else:
            report = reconcile(
                session,
                store,
                repair=args.repair,
                remove_unreferenced=args.remove_unreferenced,
            )
            session.commit()
            payload = report.as_dict()
            failed = not report.consistent
    print(json.dumps(payload, indent=2, sort_keys=True))
    if failed:
        print("storage and manifests disagree; see the report", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
