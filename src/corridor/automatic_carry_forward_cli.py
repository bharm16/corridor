"""Inspect or run the released project-scoped Automatic Support Update Rules.

The command and JSON names retain their existing technical identities; this
operation updates supporting documentation with the recorded conclusion unchanged.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.automatic_carry_forward import (
    ABSTENTION_REASON_VERSION,
    automatic_carry_forward_status,
    run_automatic_carry_forward,
)
from corridor.models import Project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automatic-carry-forward",
        description=(
            "Inspect or run Automatic Support Update under the released rules: "
            "supporting documentation updated; recorded conclusion unchanged."
        ),
        epilog="JSON fields and reason codes retain their existing names.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser(
        "status", help="show the released Automatic Support Update Rules and results"
    )
    status.add_argument("project_slug")

    run = commands.add_parser(
        "run", help="apply supporting document updates only where the released rules permit"
    )
    run.add_argument("project_slug")
    return parser


def _project_by_slug(session: Session, slug: str) -> Project:
    project = session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise ValueError(f"project {slug!r} does not exist")
    return project


def _base_payload(project: Project, command: str) -> dict[str, Any]:
    return {
        "command": command,
        "project_id": project.id,
        "project_slug": project.slug,
    }


def _status_payload(status) -> dict[str, Any]:
    return {
        "released_policy": {
            "policy_version": status.policy_version,
            "policy_sha256": status.policy_sha256,
        },
        "carried_count": status.carried_count,
        "eligible_count": status.eligible_count,
        "abstentions": {
            "count": sum(status.abstention_counts.values()),
            "reason_version": status.abstention_reason_version,
            "reasons": status.abstention_counts,
        },
    }


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Show or operate the explicit project policy and emit stable JSON."""

    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if session_factory is None:
        from corridor.db import WorkerSession
        session_factory = WorkerSession

    try:
        with session_factory() as session:
            project = _project_by_slug(session, args.project_slug)
            payload = _base_payload(project, args.command)

            if args.command == "status":
                payload.update(
                    _status_payload(automatic_carry_forward_status(session, project.id))
                )
            else:
                result = run_automatic_carry_forward(session, project.id)
                payload.update(
                    _status_payload(automatic_carry_forward_status(session, project.id))
                )
                payload["carried_receipt_ids"] = sorted(
                    receipt.audit_log_id for receipt in result.carried
                )
                reason_counts = Counter(
                    abstention.reason for abstention in result.abstentions
                )
                payload["abstentions"] = {
                    "count": len(result.abstentions),
                    "reason_version": ABSTENTION_REASON_VERSION,
                    "reasons": dict(sorted(reason_counts.items())),
                }
                session.commit()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _print_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
