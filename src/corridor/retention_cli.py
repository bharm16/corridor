"""Operate the manifest-gated Class B TTL boundary.

Previously each processing family either kept intermediaries forever or would
have needed its own cleanup script. This adapter exposes the shared retention
module as explicit plan/execute/hold/lift commands and never accepts a table
name, so the command line cannot broaden the Class B allowlist.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json

from sqlalchemy import select

from corridor.config import settings
from corridor.db import WorkerSession as Session
from corridor.models import RetentionManifestItem
from corridor.principals import HumanPrincipal
from corridor.retention import (
    delete_rebuildable_page_data,
    execute_retention,
    lift_hold,
    place_hold,
    plan_retention,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retention")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="persist and print a dry-run manifest")
    plan.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    execute = commands.add_parser("execute", help="execute one exact dry-run")
    execute.add_argument("manifest_id", type=int)
    execute.add_argument("--expected-sha256", required=True)
    hold = commands.add_parser("hold", help="suspend every project deletion path")
    hold.add_argument("project_id", type=int)
    hold.add_argument("--reason", required=True)
    lift = commands.add_parser("lift", help="lift one attributable hold")
    lift.add_argument("hold_id", type=int)
    commands.add_parser("clear-class-c", help="clear rebuildable page projections")
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = _parser().parse_args(argv)
    principal = HumanPrincipal(settings.human_principal)
    factory = session_factory or Session
    with factory() as session:
        if args.command == "plan":
            manifest = plan_retention(session, as_of=args.as_of, principal=principal)
            items = session.scalars(
                select(RetentionManifestItem)
                .where(RetentionManifestItem.manifest_id == manifest.id)
                .order_by(RetentionManifestItem.id)
            ).all()
            payload = {
                "manifest_id": manifest.id,
                "public_id": manifest.public_id,
                "status": manifest.status,
                "content_sha256": manifest.content_sha256,
                "items": [
                    {
                        "family": item.family,
                        "source_row_id": item.source_row_id,
                        "content_sha256": item.content_sha256,
                        "delete_after": item.delete_after.isoformat(),
                    }
                    for item in items
                ],
            }
        elif args.command == "execute":
            manifest = execute_retention(
                session,
                manifest_id=args.manifest_id,
                expected_sha256=args.expected_sha256,
            )
            payload = {"manifest_id": manifest.id, "status": manifest.status}
        elif args.command == "hold":
            hold = place_hold(
                session,
                project_id=args.project_id,
                reason=args.reason,
                principal=principal,
            )
            payload = {"hold_id": hold.id, "status": "active"}
        elif args.command == "lift":
            hold = lift_hold(session, hold_id=args.hold_id, principal=principal)
            payload = {"hold_id": hold.id, "status": "lifted"}
        else:
            delete_rebuildable_page_data(session)
            payload = {"status": "class_c_cleared"}
        session.commit()
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
