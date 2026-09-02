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
    AssignmentNotificationDeclaration,
    DocumentNotificationDeclaration,
    DueWorkRefusal,
    EventAdmissionReproofDeclaration,
    LocationDiscoveryDeclaration,
    ProcessingHealthDeclaration,
    ProjectProcessingDeclaration,
    ReportPublicationDeclaration,
    configure_due_work,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
    supervise_due_work,
)
from corridor.models import Project


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _common_declaration_kwargs(args, project_id: int) -> dict:
    return {
        "project_id": project_id,
        "configuration_version": args.configuration_version,
        "starts_at": _datetime(args.starts_at),
        "cadence": args.cadence,
        "timezone_name": args.timezone_name,
        "missed_run_policy": args.missed_run_policy,
        "retention_days": args.retention_days,
        "max_attempts": args.max_attempts,
        "backoff_seconds": args.backoff_seconds,
        "claim_ttl_seconds": args.claim_ttl_seconds,
        "deadline_seconds": args.deadline_seconds,
        "concurrency_limit": args.concurrency_limit,
        "model_token_budget": args.model_token_budget,
        "notification_budget": args.notification_budget,
    }


def _health_declaration(args, project_id: int):
    return ProcessingHealthDeclaration(**_common_declaration_kwargs(args, project_id))


def _processing_declaration(args, project_id: int):
    return ProjectProcessingDeclaration(
        **_common_declaration_kwargs(args, project_id),
        extractor_identity=args.extractor_identity,
    )


def _discovery_declaration(args, project_id: int):
    return LocationDiscoveryDeclaration(
        **_common_declaration_kwargs(args, project_id),
        location_id=args.location_id,
        adapter_identity=args.adapter_identity,
        source_manifest_id=args.source_manifest_id,
        index_url=args.index_url,
        rid_link_text=args.rid_link_text,
        authorized_hosts=tuple(args.authorized_hosts),
        sealed=args.sealed,
        nested_archive_depth=args.nested_archive_depth,
        max_archive_compressed_mib=args.max_archive_compressed_mib,
        max_member_decompressed_mib=args.max_member_decompressed_mib,
        enumeration_limit=args.enumeration_limit,
        request_limit=args.request_limit,
        document_limit=args.document_limit,
    )


def _assignment_notification_declaration(args, project_id: int):
    return AssignmentNotificationDeclaration(
        **_common_declaration_kwargs(args, project_id), channel=args.channel
    )


def _document_notification_declaration(args, project_id: int):
    return DocumentNotificationDeclaration(
        **_common_declaration_kwargs(args, project_id), channel=args.channel
    )


def _reproof_declaration(args, project_id: int):
    return EventAdmissionReproofDeclaration(
        **_common_declaration_kwargs(args, project_id),
        policy_version=args.policy_version,
        reason_version=args.reason_version,
        selection_rule=args.selection_rule,
        clone_budget=args.clone_budget,
    )


def _publication_declaration(args, project_id: int):
    return ReportPublicationDeclaration(
        **_common_declaration_kwargs(args, project_id),
        provenance_mode=args.provenance_mode,
        prepare_external_pdf=args.prepare_external_pdf,
        comparison_window_policy=args.comparison_window_policy,
    )


def _add_schedule_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("project_slug")
    command.add_argument("--configuration-version", required=True)
    command.add_argument("--starts-at", required=True)
    command.add_argument("--cadence", required=True)
    command.add_argument("--timezone", required=True, dest="timezone_name")
    command.add_argument("--missed-run-policy", required=True)
    command.add_argument("--retention-days", required=True, type=int)
    command.add_argument("--max-attempts", required=True, type=int)
    command.add_argument("--backoff-seconds", required=True, type=int)
    command.add_argument("--claim-ttl-seconds", required=True, type=int)
    command.add_argument("--deadline-seconds", required=True, type=int)
    command.add_argument("--concurrency-limit", required=True, type=int)
    command.add_argument("--model-token-budget", required=True, type=int)
    command.add_argument("--notification-budget", required=True, type=int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="due-work")
    commands = parser.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure-health")
    _add_schedule_arguments(configure)
    configure.set_defaults(declaration_builder=_health_declaration)

    processing = commands.add_parser("configure-processing")
    processing.add_argument("--extractor-identity", required=True)
    _add_schedule_arguments(processing)
    processing.set_defaults(declaration_builder=_processing_declaration)

    discovery = commands.add_parser("configure-discovery")
    discovery.add_argument("--location-id", required=True)
    discovery.add_argument("--adapter-identity", required=True)
    discovery.add_argument("--source-manifest-id", required=True)
    discovery.add_argument("--index-url", required=True)
    discovery.add_argument("--rid-link-text")
    discovery.add_argument(
        "--authorized-host",
        action="append",
        required=True,
        dest="authorized_hosts",
        help="an authorized host; repeat for more than one",
    )
    discovery.add_argument("--sealed", action="store_true")
    discovery.add_argument("--nested-archive-depth", type=int, default=2)
    discovery.add_argument("--max-archive-compressed-mib", type=int, default=512)
    discovery.add_argument("--max-member-decompressed-mib", type=int, default=128)
    discovery.add_argument("--enumeration-limit", type=int, default=500)
    discovery.add_argument("--request-limit", type=int, default=200)
    discovery.add_argument("--document-limit", type=int, default=100)
    _add_schedule_arguments(discovery)
    discovery.set_defaults(
        declaration_builder=_discovery_declaration,
        payload_extra=lambda args: {
            "location_id": args.location_id,
            "sealed": args.sealed,
        },
    )

    notifications = commands.add_parser("configure-notifications")
    notifications.add_argument("--channel", required=True)
    _add_schedule_arguments(notifications)
    notifications.set_defaults(
        declaration_builder=_assignment_notification_declaration
    )

    document_notifications = commands.add_parser("configure-document-notifications")
    document_notifications.add_argument("--channel", required=True)
    _add_schedule_arguments(document_notifications)
    document_notifications.set_defaults(
        declaration_builder=_document_notification_declaration
    )

    reproof = commands.add_parser("configure-reproof")
    reproof.add_argument("--policy-version", required=True)
    reproof.add_argument("--reason-version", required=True)
    reproof.add_argument("--selection-rule", required=True)
    reproof.add_argument("--clone-budget", required=True, type=int)
    _add_schedule_arguments(reproof)
    reproof.set_defaults(declaration_builder=_reproof_declaration)

    publication = commands.add_parser("configure-publication")
    publication.add_argument(
        "--provenance-mode",
        required=True,
        choices=("all-supported-sources", "document-only"),
    )
    external = publication.add_mutually_exclusive_group(required=True)
    external.add_argument(
        "--prepare-external-pdf", dest="prepare_external_pdf", action="store_true"
    )
    external.add_argument(
        "--internal-snapshot-only", dest="prepare_external_pdf", action="store_false"
    )
    publication.add_argument("--comparison-window-policy", required=True)
    _add_schedule_arguments(publication)
    publication.set_defaults(declaration_builder=_publication_declaration)

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
        from corridor.db import WorkerSession as session_factory
    clock = clock or SystemClock()

    try:
        declaration_builder = getattr(args, "declaration_builder", None)
        if declaration_builder is not None:
            with session_factory() as session:
                with session.begin():
                    project = _project(session, args.project_slug)
                    declaration = declaration_builder(args, project.id)
                    schedule = configure_due_work(
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
                    payload_extra = getattr(args, "payload_extra", None)
                    if payload_extra is not None:
                        payload.update(payload_extra(args))
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
