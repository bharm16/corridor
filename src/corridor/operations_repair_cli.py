"""Operator entry point for the receipted source repair (#842).

The repair is a managed technical operation, not customer domain work
(ADR-0034), and the 2026-09-10 customer-journey audit said a staff-only runbook
may perform it at first so long as it is attributable and repeatable. This is
that runbook. It is deliberately *not* a web surface: the legacy operations
screens stay outside the live-pilot boundary, and adding a route to reach this
would have put a new state-changing door on the enabled surface before anybody
had asked for one.

Attribution comes from ``CORRIDOR_HUMAN_PRINCIPAL``, fail-closed, exactly as
``location_discovery_cli`` resolves it: an unset principal refuses rather than
writing an unattributed record. The designation is not this command's to decide
-- ``operations_repair`` reads it live from the project roster, so an operator
whose technical-operations designation was withdrawn is refused here for the
same reason and by the same reader the product would refuse them by.

``show`` writes nothing. It prints what the record already holds about one
project's blocked sources, so an operator can see which document id to name
before naming one.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from corridor.config import settings
from corridor.models import Document, Project
from corridor.operations_repair import (
    CORRECTED_MAPPING,
    PROCEDURES,
    RETRY_PROCESSING,
    OperationsRepairRefused,
    repair_receipts,
    repair_source_processing,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_register import read_source_register


_CONTRACT = f"""\
The receipted repair of one source the standing pass stopped taking (#842).
Attribution comes from CORRIDOR_HUMAN_PRINCIPAL, and the acting principal must
hold the technical-operations designation on the project:
  make operations-repair ARGS="show <project-slug>"
  make operations-repair ARGS="repair <project-slug> --document-id=<id>"
  make operations-repair ARGS="repair <project-slug> --document-id=<id> \\
      --procedure={CORRECTED_MAPPING}"

{RETRY_PROCESSING!r} reads the source again as it stands. {CORRECTED_MAPPING!r}
says the reason is the corrected field mapping this project has registered
since, and is refused when no such registration post-dates the failed reading;
registering one is #829's act under the project-coordination designation.

The source-grounded re-capture #842 also names is not here. What it preserves
and what it returns through is #832's decision, which is not accepted, so there
is nothing yet for a runbook to perform.
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="operations-repair",
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("show")
    show.add_argument("project_slug")

    repair = commands.add_parser("repair")
    repair.add_argument("project_slug")
    repair.add_argument("--document-id", required=True, type=int)
    repair.add_argument("--procedure", choices=PROCEDURES, default=RETRY_PROCESSING)
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if session_factory is None:
        from corridor.db import WorkerSession

        session_factory = WorkerSession

    try:
        if args.command == "repair":
            principal = _principal()
            with session_factory() as session:
                with session.begin():
                    project = _project(session, args.project_slug)
                    document = session.get(Document, args.document_id)
                    if document is None or document.project_id != project.id:
                        raise OperationsRepairRefused(
                            "unknown_document",
                            f"document {args.document_id} is not a source of "
                            f"{args.project_slug!r}.",
                        )
                    outcome = repair_source_processing(
                        session,
                        document_id=args.document_id,
                        principal=principal,
                        procedure=args.procedure,
                    )
                    payload = {
                        "command": args.command,
                        "document_id": outcome.document_id,
                        "procedure": outcome.procedure,
                        "blocked_by": outcome.blocked_by,
                        "outcome": outcome.outcome,
                        "detail": outcome.detail,
                        "audit_id": outcome.audit_id,
                    }
        else:
            with session_factory() as session:
                project = _project(session, args.project_slug)
                register = read_source_register(session, project_id=project.id)
                receipts = repair_receipts(
                    session,
                    document_ids=[
                        row.document_id
                        for row in register.rows
                        if row.document_id is not None
                    ],
                )
                payload = {
                    "command": args.command,
                    "project_id": project.id,
                    "blocked": [
                        {
                            "document_id": row.document_id,
                            "filename": row.filename,
                            "state": row.state,
                            "owner": row.owner,
                            "repair": (
                                None if row.repair is None else row.repair.sentence
                            ),
                            "repaired_by": (
                                receipts[row.document_id].performed_by
                                if row.document_id in receipts
                                else ""
                            ),
                        }
                        for row in register.rows
                        if row.repair is not None
                    ],
                }
    except (OperationsRepairRefused, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _principal() -> HumanPrincipal:
    """The deployment-resolved operator, refused rather than invented."""

    if not settings.human_principal:
        raise OperationsRepairRefused(
            "no_principal",
            "CORRIDOR_HUMAN_PRINCIPAL must be set to attribute this operation",
        )
    return require_human_principal(HumanPrincipal(settings.human_principal))


def _project(session, slug: str) -> Project:
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise OperationsRepairRefused(
            "unknown_project", f"no project {slug!r}"
        )
    return project


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
