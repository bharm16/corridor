"""Activate or suspend statement Record Inclusion where Applies To is not yet known.

These safety-critical writes live behind one explicit operator command so the
ordinary Record Inclusion entry point cannot acquire activation authority by accident.
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

from corridor.event_admission import read_event_admission_policy_status
from corridor.event_admission_acceptance import (
    EventAdmissionAcceptanceConfig,
    event_admission_status_payload,
    lift_unknown_scope_admission,
    run_event_admission_acceptance,
    suspend_unknown_scope_admission,
)
from corridor.models import Project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="event-admission-acceptance",
        description=(
            "Test and control statement Record Inclusion rules where Applies To "
            "is not yet known. Command and policy identifiers remain unchanged."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "replay", help="test exact statement Record Inclusion on disposable clones"
    )
    replay.add_argument("--project-slug", required=True)
    replay.add_argument("--source-database-url", required=True)
    replay.add_argument("--postgres-admin-url", required=True)
    replay.add_argument("--expected-clean-git-revision", required=True)
    status = commands.add_parser(
        "status", help="read the effective policy, proof, and permitted operations"
    )
    status.add_argument("--project-slug", required=True)
    status.add_argument("--database-url")
    suspend = commands.add_parser(
        "suspend", help="record a suspension and restore the predecessor rules"
    )
    suspend.add_argument("--project-slug", required=True)
    suspend.add_argument("--database-url")
    suspend.add_argument("--reason", required=True)
    suspend.add_argument("--recorded-by", required=True)
    lift = commands.add_parser(
        "lift", help="lift a standing suspension with a human principal"
    )
    lift.add_argument("--project-slug", required=True)
    lift.add_argument("--database-url")
    lift.add_argument("--recorded-by", required=True)
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
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
        elif args.command == "status":
            if session_factory is None and not args.database_url:
                raise ValueError("status requires --database-url")
            payload = _with_session(
                args.project_slug,
                database_url=args.database_url,
                session_factory=session_factory,
                callback=lambda session, project: {
                    "command": "status",
                    "project_slug": project.slug,
                    **event_admission_status_payload(
                        read_event_admission_policy_status(session, project.id)
                    ),
                },
                commit=False,
            )
        elif args.command == "lift":
            if session_factory is None and not args.database_url:
                raise ValueError("lift requires --database-url")
            payload = _with_session(
                args.project_slug,
                database_url=args.database_url,
                session_factory=session_factory,
                callback=lambda session, project: _lift_payload(
                    session,
                    project,
                    recorded_by=args.recorded_by,
                ),
                commit=True,
            )
        else:
            if session_factory is None and not args.database_url:
                raise ValueError("suspend requires --database-url")
            payload = _with_session(
                args.project_slug,
                database_url=args.database_url,
                session_factory=session_factory,
                callback=lambda session, project: _suspension_payload(
                    session,
                    project,
                    reason=args.reason,
                    recorded_by=args.recorded_by,
                ),
                commit=True,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _with_session(
    project_slug: str,
    *,
    database_url: str | None,
    session_factory,
    callback,
    commit: bool,
):
    if session_factory is None:
        engine = create_engine(database_url, poolclass=NullPool, future=True)
        try:
            with Session(engine) as session:
                return _run_with_project(
                    session,
                    project_slug=project_slug,
                    callback=callback,
                    commit=commit,
                )
        finally:
            engine.dispose()
    with session_factory() as session:
        return _run_with_project(
            session,
            project_slug=project_slug,
            callback=callback,
            commit=commit,
        )


def _run_with_project(session, *, project_slug: str, callback, commit: bool):
    project = session.scalar(select(Project).where(Project.slug == project_slug))
    if project is None:
        raise ValueError(f"no project with slug {project_slug!r}")
    payload = callback(session, project)
    if commit:
        session.commit()
    return payload


def _suspension_payload(session, project, *, reason: str, recorded_by: str) -> dict:
    suspension = suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason=reason,
        recorded_by=recorded_by,
    )
    return {
        "command": "suspend",
        "project_slug": project.slug,
        "activation_id": suspension.id,
        "policy_version": suspension.policy_version,
        "reason": suspension.reason,
        "recorded_by": suspension.recorded_by,
    }


def _lift_payload(session, project, *, recorded_by: str) -> dict:
    activation = lift_unknown_scope_admission(
        session,
        project_id=project.id,
        recorded_by=recorded_by,
    )
    return {
        "command": "lift",
        "project_slug": project.slug,
        "activation_id": activation.id,
        "policy_version": activation.policy_version,
        "reason": activation.reason,
        "recorded_by": activation.recorded_by,
    }


if __name__ == "__main__":
    raise SystemExit(main())
