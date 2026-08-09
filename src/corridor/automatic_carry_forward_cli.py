"""Operate project-scoped Automatic Carry-Forward without implicit authority.

``status`` is read-only.  Policy authorization and disablement require an
explicit, validated :class:`HumanPrincipal`; ``run`` performs only the
already-authorized machine policy and never creates an authorization or an
Admission as a side effect.
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
    active_carry_forward_policy,
    automatic_carry_forward_status,
    authorize_automatic_carry_forward,
    disable_automatic_carry_forward,
    run_automatic_carry_forward,
)
from corridor.models import PolicyApproval, Project
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal


def _human_principal(value: str) -> HumanPrincipal:
    try:
        return HumanPrincipal(value)
    except InvalidHumanPrincipal as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="automatic-carry-forward")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser(
        "status", help="show the explicitly active project policy"
    )
    status.add_argument("project_slug")

    authorize = commands.add_parser(
        "authorize", help="authorize and activate the current policy"
    )
    authorize.add_argument("project_slug")
    authorize.add_argument(
        "--principal",
        required=True,
        type=_human_principal,
        help="stable namespaced human subject, for example oidc:00u123",
    )

    disable = commands.add_parser(
        "disable", help="disable the active policy without deleting history"
    )
    disable.add_argument("project_slug")
    disable.add_argument(
        "--principal",
        required=True,
        type=_human_principal,
        help="stable namespaced human subject, for example oidc:00u123",
    )

    run = commands.add_parser(
        "run", help="execute only the already-authorized machine policy"
    )
    run.add_argument("project_slug")
    return parser


def _project_by_slug(session: Session, slug: str) -> Project:
    project = session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise ValueError(f"project {slug!r} does not exist")
    return project


def _policy_payload(
    approval: PolicyApproval | None,
) -> dict[str, Any] | None:
    if approval is None:
        return None
    return {
        "approval_id": approval.id,
        "policy_version": approval.policy_version,
        "policy_sha256": approval.policy_sha256,
        "approved_by": approval.approved_by,
    }


def _base_payload(project: Project, command: str) -> dict[str, Any]:
    return {
        "command": command,
        "project_id": project.id,
        "project_slug": project.slug,
    }


def _status_payload(status) -> dict[str, Any]:
    active_policy = None
    if status.policy_approval_id is not None:
        active_policy = {
            "approval_id": status.policy_approval_id,
            "policy_version": status.policy_version,
            "policy_sha256": status.policy_sha256,
            "approved_by": status.approved_by,
        }
    return {
        "active_policy": active_policy,
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
        from corridor.db import Session as session_factory

    try:
        with session_factory() as session:
            project = _project_by_slug(session, args.project_slug)
            payload = _base_payload(project, args.command)

            if args.command == "status":
                payload.update(
                    _status_payload(
                        automatic_carry_forward_status(session, project.id)
                    )
                )
            elif args.command == "authorize":
                approval = authorize_automatic_carry_forward(
                    session,
                    project.id,
                    principal=args.principal,
                )
                payload["active_policy"] = _policy_payload(approval)
                session.commit()
            elif args.command == "disable":
                disabled = disable_automatic_carry_forward(
                    session,
                    project.id,
                    principal=args.principal,
                )
                payload["active_policy"] = None
                payload["disabled_policy"] = _policy_payload(disabled)
                session.commit()
            else:
                result = run_automatic_carry_forward(session, project.id)
                payload["active_policy"] = _policy_payload(
                    active_carry_forward_policy(session, project.id)
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
