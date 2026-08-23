"""Operate real-state unknown-scope Event Admission activation and suspension.

These safety-critical writes live behind one explicit operator command so the
ordinary Admission entry point cannot acquire activation authority by accident.
Folding replay and suspension into normal processing was rejected because it
would blur proof generation, policy selection, and rollback into one command.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from corridor.event_admission_acceptance import (
    EventAdmissionAcceptanceConfig,
    run_event_admission_acceptance,
    suspend_unknown_scope_admission,
)
from corridor.models import Project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="event-admission-acceptance")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--project-slug", required=True)
    replay.add_argument("--source-database-url", required=True)
    replay.add_argument("--postgres-admin-url", required=True)
    replay.add_argument("--expected-clean-git-revision", required=True)
    suspend = commands.add_parser("suspend")
    suspend.add_argument("--project-slug", required=True)
    suspend.add_argument("--database-url", required=True)
    suspend.add_argument("--reason", required=True)
    suspend.add_argument("--recorded-by", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command == "replay":
            result = run_event_admission_acceptance(
                EventAdmissionAcceptanceConfig(
                    project_slug=args.project_slug,
                    source_database_url=args.source_database_url,
                    postgres_admin_url=args.postgres_admin_url,
                    expected_clean_git_revision=args.expected_clean_git_revision,
                )
            )
            payload = {
                "command": "replay",
                "receipt_id": result.receipt_id,
                "status": result.status,
                "activated": result.activated,
                "receipt_sha256": result.receipt_sha256,
                "source_revision": result.source_revision,
                "migration_head": result.migration_head,
                "shared_source_domain_state_mutated": False,
            }
        else:
            engine = create_engine(args.database_url, poolclass=NullPool, future=True)
            try:
                with Session(engine) as session:
                    project = session.scalar(
                        select(Project).where(Project.slug == args.project_slug)
                    )
                    if project is None:
                        raise ValueError(
                            f"no project with slug {args.project_slug!r}"
                        )
                    suspension = suspend_unknown_scope_admission(
                        session,
                        project_id=project.id,
                        reason=args.reason,
                        recorded_by=args.recorded_by,
                    )
                    session.commit()
                    payload = {
                        "command": "suspend",
                        "project_slug": project.slug,
                        "activation_id": suspension.id,
                        "policy_version": suspension.policy_version,
                        "reason": suspension.reason,
                    }
            finally:
                engine.dispose()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
