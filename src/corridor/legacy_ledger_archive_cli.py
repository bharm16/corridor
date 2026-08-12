"""Operate the one-time Development Ledger retirement compare-and-swap.

The destructive command deliberately has no shorthand: an operator first
captures ``plan`` output, then repeats both its digest and Dependency count to
``retire``.  ``verify`` and ``export`` always cross the archive verification
seam before reporting success.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.legacy_ledger_archive import (
    ArchiveReadback,
    LegacyLedgerArchiveError,
    export_archive,
    plan_retirement,
    retire_legacy_ledger,
    verify_archive,
)
from corridor.models import Project

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legacy-ledger-archive")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser(
        "plan", help="print the read-only content digest for one project"
    )
    plan.add_argument("project_slug")

    retire = commands.add_parser(
        "retire",
        help="seal and retire only an exactly planned Ledger (maintenance DB role required)",
    )
    retire.add_argument("project_slug")
    retire.add_argument(
        "--expected-sha256",
        required=True,
        type=_sha256,
        help="content_sha256 printed by plan",
    )
    retire.add_argument(
        "--expected-dependency-count",
        required=True,
        type=_positive_int,
        help="Dependency count printed by plan",
    )

    verify = commands.add_parser(
        "verify", help="read and verify an immutable archive receipt"
    )
    verify.add_argument("archive_id", type=_positive_int)

    export = commands.add_parser(
        "export", help="verify and export the exact canonical archived bytes"
    )
    export.add_argument("archive_id", type=_positive_int)
    export.add_argument("path", type=Path)
    return parser


def _project_by_slug(session: Session, slug: str) -> Project:
    project = session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise ValueError(f"project {slug!r} does not exist")
    return project


def _plan_payload(project: Project, plan) -> dict[str, Any]:
    return {
        "project_id": project.id,
        "project_slug": project.slug,
        "counts": plan.counts,
        "ref_code_high_watermark": plan.ref_code_high_watermark,
        "content_sha256": plan.content_sha256,
    }


def _retirement_payload(readback: ArchiveReadback) -> dict[str, Any]:
    archive = readback.archive
    project = readback.content["project"]
    return {
        "archive_id": archive.id,
        "project_id": project["id"],
        "project_slug": project["slug"],
        "dependency_count": archive.dependency_count,
        "content_sha256": archive.content_sha256,
    }


def _readback_payload(readback: ArchiveReadback) -> dict[str, Any]:
    archive = readback.archive
    project = readback.content["project"]
    return {
        "archive_id": archive.id,
        "project_id": project["id"],
        "project_slug": project["slug"],
        "format_version": archive.format_version,
        "content_sha256": archive.content_sha256,
        "counts": {
            "dependencies": archive.dependency_count,
            "assertions": archive.assertion_count,
            "evidence_links": archive.evidence_link_count,
            "audit_log": archive.audit_log_count,
        },
        "ref_code_high_watermark": archive.ref_code_high_watermark,
        "retired_by": archive.retired_by,
    }


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Run a plan, explicit retirement, verified readback, or verified export."""

    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if session_factory is None:
        from corridor.db import Session as session_factory

    try:
        with session_factory() as session:
            if args.command == "plan":
                project = _project_by_slug(session, args.project_slug)
                payload = _plan_payload(
                    project, plan_retirement(session, project.id)
                )
            elif args.command == "retire":
                project = _project_by_slug(session, args.project_slug)
                archive = retire_legacy_ledger(
                    session,
                    project.id,
                    expected_sha256=args.expected_sha256,
                    expected_dependency_count=args.expected_dependency_count,
                )
                payload = _retirement_payload(
                    verify_archive(session, archive.id)
                )
                session.commit()
            elif args.command == "verify":
                readback = verify_archive(session, args.archive_id)
                payload = _readback_payload(readback)
            else:
                readback = verify_archive(session, args.archive_id)
                output = export_archive(session, args.archive_id, args.path)
                payload = {
                    **_readback_payload(readback),
                    "path": str(output),
                }
    except (LegacyLedgerArchiveError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _print_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
