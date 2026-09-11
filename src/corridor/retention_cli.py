"""Operate the manifest-gated Class B TTL boundary, and the sign-in expiry.

Previously each processing family either kept intermediaries forever or would
have needed its own cleanup script. This adapter exposes the shared retention
module as explicit plan/execute/hold/lift commands and never accepts a table
name, so the command line cannot broaden the Class B allowlist.

``expire-sign-in-records`` is a second, deliberately separate boundary over
``sign_in_retention`` (#907, ADR-0102): per person rather than per project, no
manifest, and a deleted row rather than a nulled content column. It is here
because this is the retention surface an operator already knows, and because it
is what a deployed schedule is meant to invoke once per customer environment --
ADR-0102 names that deployment task. Its ``--as-of`` is therefore optional and
defaults to now, so a scheduled invocation needs no per-run argument, while an
operator reproducing a past pass can still pin the moment it measures from.

**A stated period is when a row becomes eligible for deletion, and this command
is what removes it.** The two are separate terms and neither may be quoted as
the other. Whatever interval invokes ``expire-sign-in-records`` is an
additional lag on top of the period, so under the daily schedule ADR-0102
specifies an eligible row goes within a further 24 hours. A hold, a failed run
or a gap the next run has to catch up on lengthens that lag without changing
the period, and each is reported here rather than folded into the number: a
hold prints ``"outcome": "refused"`` at exit status zero, a failure exits
non-zero with its traceback, and a scheduled window with no printed receipt at
all is how a run that never happened shows up.

**``hold`` says what it won, and no more.** It prints its acknowledgement only
after taking the ordering boundary ADR-0102 specifies, so ``"status":
"active"`` means every deletion batch beginning after this command commits is
refused. It does not mean a batch already running stopped, and it does not mean
rows such a batch removed come back; nothing recovers a deleted row.

Every command prints its payload, and the sign-in pass returns one whether it
deleted, refused or found nothing due. That printed line is the run record: an
idle pass writes no domain audit event, so the scheduled job's own log is what
shows it ran at all.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json

from sqlalchemy import select

from corridor.config import settings
from corridor.db import WorkerSession
from corridor.models import RetentionManifestItem
from corridor.principals import HumanPrincipal
from corridor.retention import (
    delete_rebuildable_page_data,
    execute_retention,
    lift_hold,
    place_hold,
    plan_retention,
)
from corridor.sign_in_retention import sweep_sign_in_records


_CONTRACT = """\
Plan first; execute requires the exact manifest digest. Holds and lifts are
separate attributable commands through CORRIDOR_HUMAN_PRINCIPAL.
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retention",
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="persist and print a dry-run manifest")
    plan.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    execute = commands.add_parser("execute", help="execute one exact dry-run")
    execute.add_argument("manifest_id", type=int)
    execute.add_argument("--expected-sha256", required=True)
    hold = commands.add_parser(
        "hold", help="suspend every deletion batch that begins after this commits"
    )
    hold.add_argument("project_id", type=int)
    hold.add_argument("--reason", required=True)
    lift = commands.add_parser("lift", help="lift one attributable hold")
    lift.add_argument("hold_id", type=int)
    commands.add_parser("clear-class-c", help="clear rebuildable page projections")
    expire = commands.add_parser(
        "expire-sign-in-records",
        help="delete sessions, links and attempts past their stated retention",
    )
    # Optional, unlike `plan`'s: the scheduled invocation this command exists
    # for cannot compute a fresh timestamp for a fixed command line.
    expire.add_argument("--as-of", type=datetime.fromisoformat, default=None)
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = _parser().parse_args(argv)
    factory = session_factory or WorkerSession

    def principal() -> HumanPrincipal:
        # Read only by the commands that are attributable to a person. The
        # sign-in expiry is not one: its actor is
        # `audit.SIGN_IN_RECORD_EXPIRY_ACTOR`, so a scheduled invocation must
        # not have to declare a human -- and must not be made to name a false
        # one -- in order to run.
        return HumanPrincipal(settings.human_principal)

    with factory() as session:
        if args.command == "plan":
            manifest = plan_retention(
                session, as_of=args.as_of, principal=principal()
            )
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
                principal=principal(),
            )
            payload = {"hold_id": hold.id, "status": "active"}
        elif args.command == "lift":
            hold = lift_hold(session, hold_id=args.hold_id, principal=principal())
            payload = {"hold_id": hold.id, "status": "lifted"}
        elif args.command == "expire-sign-in-records":
            payload = sweep_sign_in_records(
                session, as_of=args.as_of or datetime.now(timezone.utc)
            )
        else:
            delete_rebuildable_page_data(session)
            payload = {"status": "class_c_cleared"}
        session.commit()
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
