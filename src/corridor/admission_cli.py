"""Operator entry points for the admission policies (ADR-0026, ADR-0027).

Authorization is an attributable human act: the principal comes from the
deployment configuration, exactly as the web ingress resolves it — never
free text on a command line. Running an authorized policy is the
machine's act, invoked by the operator.
"""

from __future__ import annotations

import sys

from sqlalchemy import select


def _usage() -> int:
    print(
        "usage: admission authorize-dependencies <project-slug> "
        "<document-id> <document-id> [...]\n"
        "       admission run-dependencies <project-slug>\n"
        "       admission authorize-events <project-slug>\n"
        "       admission run-events <project-slug>",
        file=sys.stderr,
    )
    return 2


def _principal():
    from corridor.config import settings
    from corridor.principals import HumanPrincipal, InvalidHumanPrincipal

    try:
        return HumanPrincipal(settings.human_principal)
    except InvalidHumanPrincipal:
        print(
            "authorizing an admission policy is an attributable act: set "
            "CORRIDOR_HUMAN_PRINCIPAL to a namespaced subject such as "
            "'local:alice'",
            file=sys.stderr,
        )
        return None


def _project_id(session, slug: str) -> int | None:
    from corridor.models import Project

    project_id = session.scalar(select(Project.id).where(Project.slug == slug))
    if project_id is None:
        print(f"no project with slug {slug!r}", file=sys.stderr)
    return project_id


def main(argv: list[str], *, session_factory=None) -> int:
    if len(argv) < 2:
        return _usage()
    command, slug, *rest = argv

    if session_factory is None:
        from corridor.db import Session as session_factory

    from corridor import dependency_admission, event_admission

    with session_factory() as session:
        project_id = _project_id(session, slug)
        if project_id is None:
            return 2

        if command == "authorize-dependencies":
            if not rest:
                return _usage()
            principal = _principal()
            if principal is None:
                return 2
            try:
                document_ids = [int(value) for value in rest]
            except ValueError:
                print("document ids must be integers", file=sys.stderr)
                return 2
            try:
                approval = dependency_admission.authorize_dependency_admission(
                    session,
                    project_id,
                    principal=principal,
                    agreement_document_ids=document_ids,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            session.commit()
            print(
                f"authorized {approval.policy_version} for {slug} "
                f"(approval {approval.id}, digest "
                f"{approval.policy_sha256[:12]}) by {principal.subject}"
            )
            return 0

        if command == "authorize-events":
            if rest:
                return _usage()
            principal = _principal()
            if principal is None:
                return 2
            approval = event_admission.authorize_event_admission(
                session, project_id, principal=principal
            )
            session.commit()
            print(
                f"authorized {approval.policy_version} for {slug} "
                f"(approval {approval.id}, digest "
                f"{approval.policy_sha256[:12]}) by {principal.subject}"
            )
            return 0

        if command == "run-dependencies":
            if rest:
                return _usage()
            try:
                result = dependency_admission.run_dependency_admission(
                    session, project_id
                )
            except dependency_admission.DependencyAdmissionNotAuthorized as exc:
                print(str(exc), file=sys.stderr)
                return 1
            session.commit()
            print(
                f"run {result.run_id}: {result.admitted_count} admitted, "
                f"{result.abstained_count} abstained"
            )
            for abstention in result.abstentions:
                print(
                    f"  candidate {abstention.candidate_id}: "
                    f"{abstention.reason}"
                )
            return 0

        if command == "run-events":
            if rest:
                return _usage()
            try:
                result = event_admission.run_event_admission(
                    session, project_id
                )
            except event_admission.EventAdmissionNotAuthorized as exc:
                print(str(exc), file=sys.stderr)
                return 1
            session.commit()
            reasons: dict[str, int] = {}
            for abstention in result.abstentions:
                reasons[abstention.reason] = reasons.get(abstention.reason, 0) + 1
            print(
                f"run {result.run_id}: {result.admitted_count} admitted, "
                f"{result.abstained_count} abstained"
            )
            for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
                print(f"  {count:5}  {reason}")
            return 0

    return _usage()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
