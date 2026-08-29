"""Operator entry point for the one shared supervised Due Work runtime.

Feature modules starting background loops were rejected. This process owns the
production supervisor, while bounded tick/run-once/recover commands expose the
same durable interfaces for operations and controlled validation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import signal
from threading import Event
import time

from sqlalchemy import select

from corridor.due_work import (
    DueWorkRefusal,
    ProcessingHealthDeclaration,
    configure_processing_health,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
    supervise_due_work,
)
from corridor.models import Project


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="due-work")
    commands = parser.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure-health")
    configure.add_argument("project_slug")
    configure.add_argument("--configuration-version", required=True)
    configure.add_argument("--starts-at", required=True)
    configure.add_argument("--cadence", required=True)
    configure.add_argument("--timezone", required=True, dest="timezone_name")
    configure.add_argument("--missed-run-policy", required=True)
    configure.add_argument("--retention-days", required=True, type=int)
    configure.add_argument("--max-attempts", required=True, type=int)
    configure.add_argument("--backoff-seconds", required=True, type=int)
    configure.add_argument("--claim-ttl-seconds", required=True, type=int)
    configure.add_argument("--deadline-seconds", required=True, type=int)
    configure.add_argument("--concurrency-limit", required=True, type=int)
    configure.add_argument("--model-token-budget", required=True, type=int)
    configure.add_argument("--notification-budget", required=True, type=int)

    commands.add_parser("tick")
    for name in ("run-once", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--owner", required=True)
    status = commands.add_parser("status")
    status.add_argument("--project-slug")
    supervise = commands.add_parser("supervise")
    supervise.add_argument("--owner", required=True)
    supervise.add_argument("--poll-seconds", type=float, required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory=None,
    clock=None,
    stop_requested=None,
    wait=time.sleep,
) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if session_factory is None:
        from corridor.db import Session as session_factory
    clock = clock or SystemClock()

    try:
        if args.command == "configure-health":
            with session_factory() as session:
                with session.begin():
                    project = _project(session, args.project_slug)
                    declaration = ProcessingHealthDeclaration(
                        project_id=project.id,
                        configuration_version=args.configuration_version,
                        starts_at=_datetime(args.starts_at),
                        cadence=args.cadence,
                        timezone_name=args.timezone_name,
                        missed_run_policy=args.missed_run_policy,
                        retention_days=args.retention_days,
                        max_attempts=args.max_attempts,
                        backoff_seconds=args.backoff_seconds,
                        claim_ttl_seconds=args.claim_ttl_seconds,
                        deadline_seconds=args.deadline_seconds,
                        concurrency_limit=args.concurrency_limit,
                        model_token_budget=args.model_token_budget,
                        notification_budget=args.notification_budget,
                    )
                    schedule = configure_processing_health(
                        session,
                        declaration,
                        now=clock.now(),
                    )
                    payload = {
                        "command": args.command,
                        "job_id": schedule.public_id,
                        "project_id": project.id,
                        "configuration_sha256": schedule.configuration_sha256,
                        "enabled": schedule.disabled_at is None,
                    }
        elif args.command == "tick":
            with session_factory() as session:
                with session.begin():
                    occurrences = enqueue_due_work(session, now=clock.now())
                    payload = {
                        "command": args.command,
                        "occurrence_ids": [item.public_id for item in occurrences],
                    }
        elif args.command in {"run-once", "recover"}:
            with session_factory() as session:
                with session.begin():
                    enqueue_due_work(session, now=clock.now())
            result = run_due_work_once(
                session_factory,
                clock=clock,
                owner=args.owner,
            )
            payload = {
                "command": args.command,
                "result": (
                    None
                    if result is None
                    else {
                        "occurrence_id": result.occurrence_public_id,
                        "receipt_id": result.receipt_public_id,
                        "job_id": result.job_public_id,
                        "attempt_id": result.attempt_id,
                        "project_id": result.project_id,
                        "handler": result.handler_key,
                        "configuration_version": result.configuration_version,
                        "input_identity_sha256": result.input_identity_sha256,
                        "execution_outcome": result.execution_outcome,
                        "handler_result": result.handler_result,
                        "error_code": result.error_code,
                        "safe_next_step": result.safe_next_step,
                    }
                ),
            }
        elif args.command == "status":
            with session_factory() as session:
                project_id = (
                    _project(session, args.project_slug).id
                    if args.project_slug
                    else None
                )
                payload = {
                    "command": args.command,
                    **due_work_status(session, project_id=project_id),
                }
        else:
            if stop_requested is None:
                shutdown = Event()

                def request_shutdown(*_args):
                    shutdown.set()

                signal.signal(signal.SIGTERM, request_shutdown)
                signal.signal(signal.SIGINT, request_shutdown)
                stop_requested = shutdown.is_set
            cycles = supervise_due_work(
                session_factory,
                clock=clock,
                owner=args.owner,
                stop_requested=stop_requested,
                wait=wait,
                poll_seconds=args.poll_seconds,
            )
            payload = {"command": args.command, "completed_cycles": cycles}
    except (DueWorkRefusal, ValueError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1

    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _project(session, slug: str) -> Project:
    project = session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise ValueError(f"project {slug!r} does not exist")
    return project


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Due Work starts_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("Due Work starts_at must include a timezone")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
