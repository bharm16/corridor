"""Operator entry point for the managed connected-location operations (#350).

Discovery and fetching run on the shared Due Work runtime; the two acts that
require a person — authorizing a proposed reference for processing and running
the bounded parse recovery on a failed document — are managed technical
operations, not customer domain work (ADR-0034), and live here beside a
read-only operations view. Each act is attributed to the deployment-resolved
human principal, which is fail-closed: an unset principal refuses rather than
writing an unattributed record.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from sqlalchemy import select

from corridor.config import settings
from corridor.location_discovery import (
    LocationDiscoveryRefused,
    authorize_reference,
    operations_view,
    recover_document_parse,
)
from corridor.models import Document, Project
from corridor.principals import HumanPrincipal, require_human_principal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="location-discovery")
    commands = parser.add_subparsers(dest="command", required=True)

    authorize = commands.add_parser("authorize")
    authorize.add_argument("project_slug")
    authorize.add_argument("--reference-key", required=True)
    authorize.add_argument("--doc-type", required=True)
    authorize.add_argument("--registry-id")

    recover = commands.add_parser("recover-parse")
    recover.add_argument("project_slug")
    recover.add_argument("--document-id", required=True, type=int)

    view = commands.add_parser("view")
    view.add_argument("project_slug")
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if session_factory is None:
        from corridor.db import WorkerSession as session_factory

    try:
        if args.command == "authorize":
            principal = _principal()
            with session_factory() as session:
                with session.begin():
                    project = _project(session, args.project_slug)
                    result = authorize_reference(
                        session,
                        project_id=project.id,
                        reference_key=args.reference_key,
                        doc_type=args.doc_type,
                        principal=principal,
                        registry_id=args.registry_id,
                    )
                    payload = {
                        "command": args.command,
                        "reference_id": result.reference_id,
                        "reference_key": result.reference_key,
                        "doc_type": result.doc_type,
                        "registry_id": result.registry_id,
                    }
        elif args.command == "recover-parse":
            principal = _principal()
            with session_factory() as session:
                with session.begin():
                    project = _project(session, args.project_slug)
                    document = session.get(Document, args.document_id)
                    if document is None or document.project_id != project.id:
                        raise LocationDiscoveryRefused(
                            "unknown_document",
                            f"document {args.document_id} is not in {args.project_slug!r}",
                        )
                    result = recover_document_parse(
                        session,
                        document_id=args.document_id,
                        principal=principal,
                    )
                    payload = {
                        "command": args.command,
                        "document_id": result.document_id,
                        "outcome": result.outcome,
                        "pages": result.pages,
                        "reason": result.reason,
                    }
        else:
            with session_factory() as session:
                project = _project(session, args.project_slug)
                view = operations_view(session, project.id)
                payload = {
                    "command": args.command,
                    "project_id": project.id,
                    **asdict(view),
                }
    except (LocationDiscoveryRefused, ValueError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1

    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _principal() -> HumanPrincipal:
    if not settings.human_principal:
        raise LocationDiscoveryRefused(
            "no_principal",
            "CORRIDOR_HUMAN_PRINCIPAL must be set to attribute this operation",
        )
    return require_human_principal(HumanPrincipal(settings.human_principal))


def _project(session, slug: str) -> Project:
    project = session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise ValueError(f"project {slug!r} does not exist")
    return project


if __name__ == "__main__":
    raise SystemExit(main())
