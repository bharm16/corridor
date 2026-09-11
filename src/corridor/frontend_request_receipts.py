"""Attributable, privacy-safe receipts for ordinary frontend requests.

Browser screenshots establish what was visible, not which HTTP handler
committed a Project Record act.  These receipts are written by the exact
ordinary route, in that route's SQLAlchemy transaction, so Product Proving can
join visible use to the durable act without trusting a browser-driver claim.

Only route identity, response status, bounded subject ids, and a digest of the
request fields are retained.  Raw query/form values are never stored here.

The route template and method are read from the request's matched route (the
``route`` FastAPI leaves in the ASGI scope, as ``telemetry`` already reads it),
not retyped by the caller.  Each call site used to spell the template and
method beside a decorator that already declared them, and nothing compared the
two: a handler renamed in its decorator with the call site left alone wrote a
well-formed receipt naming a path that no longer existed.  The contract table
therefore holds only what the router cannot know: which route names may carry
a receipt at all, and which statuses each may return.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from corridor import audit
from corridor.models import AuditLog
from corridor.principals import HumanPrincipal


SCHEMA_VERSION = "corridor.product-proving-frontend-request.v2"
_FIELD_DIGEST_DOMAIN = b"corridor.frontend-request-fields.v2\0"
_METHODS = frozenset({"GET", "POST"})

# One closed route vocabulary prevents a caller from spelling an arbitrary
# handler identity into an otherwise well-formed receipt: a route name must
# be registered here and must name the route the router matched. Status
# remains response-derived, then must be one the registered route can
# actually return. The template and method come from the matched route.
ROUTE_CONTRACTS: Mapping[str, frozenset[int]] = {
    "coordinate_statement_screen": frozenset({200}),
    "save_coordinated_statement": frozenset({303}),
    "save_admitted_statement_scope": frozenset({400}),
    "keep_unresolved_statement": frozenset({303}),
    "save_admitted_statement_owner": frozenset({303}),
    "save_admitted_statement_next_action": frozenset({303}),
    "mark_waiting_statement_not_relevant": frozenset({303}),
    "correct_statement_screen": frozenset({200}),
    "correct_statement_scope_from_screen": frozenset({303, 400, 409}),
    "correct_statement_facts_from_screen": frozenset({303}),
    "clarify_dispute": frozenset({303}),
    "reports": frozenset({200}),
    "review_report": frozenset({200}),
    "download_prepared_report": frozenset({200}),
    "preview_prepared_report": frozenset({200}),
    "release_prepared_report": frozenset({201}),
    "render_report": frozenset({201}),
    "release_report": frozenset({201}),
    "coordinator_home": frozenset({200}),
    # The one act #536's ordered week carries (#533). A refusal is a 403
    # where PostgreSQL proved no external-release designation and a 409
    # where the candidate is blocked, stale, or no longer whole; each
    # renders the week around the refusal rather than redirecting.
    "authorize_project_issue": frozenset({201, 403, 409}),
    # The act that makes the candidate the one above approves (#675). A 202
    # says the coverage was confirmed and the preparation was queued -- the
    # work itself happens in a worker, so the response cannot claim it is
    # done. A 409 says the profile, revision, cutoff or coverage digest moved
    # after the coordinator was shown them, and nothing was appended. A 403
    # says PostgreSQL proved no project-coordination designation for the
    # person submitting, which is the same answer the approval above gives a
    # person holding no external-release designation (#839).
    "prepare_project_issue": frozenset({202, 403, 409}),
    # Work List scheduling on the week (#835, ADR-0084). A 201 records the new
    # scheduling receipt; a 400 says the reschedule arrived without the date
    # it needs; a 409 says the change is not deferred under this reading, or
    # the command refused to schedule it. None of the four writes a Project
    # Record revision, which is exactly why this is not among the decisions.
    "reschedule_deferred_change": frozenset({200, 201, 400, 409}),
    # The Follow-up Plan lifecycle (#835). A 201 records the closure -- a
    # correction that supersedes the plan, or a cancellation with a structured
    # reason; a 400 says the form arrived without the question, the party or
    # the reason its act needs; a 409 says the plan is not one this project is
    # still waiting on, or PostgreSQL refused the closure. None writes a
    # Project Record revision (ADR-0084).
    "close_project_follow_up_plan": frozenset({201, 400, 409}),
    "queue": frozenset({200}),
    "internal_report": frozenset({200}),
    "internal_report_full": frozenset({200}),
    "internal_report_alerts": frozenset({200}),
    "internal_report_workbook": frozenset({200}),
    "read_internal_coordination_summary": frozenset({200}),
    "operations_checks": frozenset({200}),
    "operations_checks_preview": frozenset({200, 400}),
    "save_operations_checks": frozenset({303, 400}),
    "save_dependency_follow_up_plan": frozenset({303}),
    "processing_operations": frozenset({200}),
    "declare_operations_active_run": frozenset({303}),
    "suspend_operations_unknown_scope": frozenset({303}),
    "lift_operations_unknown_scope": frozenset({303}),
    "confirm_documentation_approval": frozenset({303}),
    "clarify_documentation_review": frozenset({303}),
    "keep_unresolved_candidate": frozenset({303}),
    "accept": frozenset({303}),
    "confirm_organization": frozenset({303}),
    "edit_accept": frozenset({303}),
    "merge": frozenset({303}),
    "reject": frozenset({303}),
}


@dataclass(frozen=True)
class FrontendRequestSubject:
    """The bounded Project Record identity named by one ordinary route."""

    project_id: int
    candidate_id: int | None = None
    dependency_id: int | None = None
    commitment_lineage_id: int | None = None
    statement_event_id: int | None = None
    predecessor_statement_event_id: int | None = None
    successor_statement_event_id: int | None = None
    work_decision_id: int | None = None
    artifact_id: int | None = None
    release_id: int | None = None
    report_run_id: int | None = None
    check_configuration_id: int | None = None

    def as_json(self) -> dict[str, int]:
        values = {
            "project_id": self.project_id,
            "candidate_id": self.candidate_id,
            "dependency_id": self.dependency_id,
            "commitment_lineage_id": self.commitment_lineage_id,
            "statement_event_id": self.statement_event_id,
            "predecessor_statement_event_id": self.predecessor_statement_event_id,
            "successor_statement_event_id": self.successor_statement_event_id,
            "work_decision_id": self.work_decision_id,
            "artifact_id": self.artifact_id,
            "release_id": self.release_id,
            "report_run_id": self.report_run_id,
            "check_configuration_id": self.check_configuration_id,
        }
        result = {key: value for key, value in values.items() if value is not None}
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for value in result.values()
        ):
            raise ValueError("frontend request subject ids must be positive")
        return result


def request_fields_sha256(
    fields: Mapping[str, Any] | Iterable[tuple[str, Any]] | None,
) -> str:
    """Digest a canonical request-field sequence without retaining its values."""

    if fields is None:
        pairs: list[tuple[str, dict[str, object]]] = []
    elif hasattr(fields, "multi_items"):
        pairs = [
            (str(key), _field_value(value))
            for key, value in fields.multi_items()  # type: ignore[attr-defined]
        ]
    elif isinstance(fields, Mapping):
        pairs = []
        for key, value in fields.items():
            if isinstance(value, (list, tuple)):
                pairs.extend((str(key), _field_value(item)) for item in value)
            else:
                pairs.append((str(key), _field_value(value)))
    else:
        pairs = [(str(key), _field_value(value)) for key, value in fields]
    canonical_pairs = sorted(
        pairs,
        key=lambda pair: (
            pair[0],
            json.dumps(pair[1], sort_keys=True, separators=(",", ":")),
        ),
    )
    canonical = json.dumps(
        canonical_pairs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(_FIELD_DIGEST_DOMAIN + canonical).hexdigest()


def served_route_identity(
    routes: Iterable[Any], route_name: str
) -> tuple[str, str] | None:
    """The template and method of the one served route with that name.

    ``None`` when the router serves no route of that name, more than one, or one
    whose methods are not exactly one of the receipt vocabulary's GET or POST.
    """

    matches = [
        route
        for route in routes
        if isinstance(route, Route) and route.name == route_name
    ]
    if len(matches) != 1:
        return None
    (route,) = matches
    methods = set(route.methods or ())
    if len(methods) != 1 or not methods <= _METHODS:
        return None
    (method,) = methods
    return route.path, method


def record_frontend_request(
    session: Session,
    *,
    principal: HumanPrincipal,
    route_name: str,
    request: Request,
    response: Response,
    subject: FrontendRequestSubject,
    request_fields: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
) -> AuditLog:
    """Append one server-observed request in the caller's transaction.

    The template and method are those of the route the router matched for
    ``request``; ``route_name`` must be that route's name, so the receipt can
    only describe the handler that actually served the request.
    """

    normalized_method = request.method.strip().upper()
    if normalized_method not in _METHODS:
        raise ValueError("frontend request method is unsupported")
    expected_statuses = ROUTE_CONTRACTS.get(route_name)
    if expected_statuses is None:
        raise ValueError("frontend request route identity is invalid")
    status = response.status_code
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("frontend request status is invalid")
    route = request.scope.get("route")
    if (
        not isinstance(route, Route)
        or route.name != route_name
        or normalized_method not in (route.methods or ())
        or status not in expected_statuses
    ):
        raise ValueError("frontend request does not match its registered route")
    route_template = route.path
    subject_json = subject.as_json()
    return audit.record(
        session,
        principal=principal,
        action=audit.PRODUCT_PROVING_FRONTEND_REQUEST,
        entity_type=audit.PROJECT,
        entity_id=subject.project_id,
        after={
            "schema_version": SCHEMA_VERSION,
            "route_name": route_name,
            "route_template": route_template,
            "method": normalized_method,
            "status": status,
            "request_fields_sha256": request_fields_sha256(request_fields),
            "subject": subject_json,
        },
    )


def _field_value(value: Any) -> dict[str, object]:
    """Produce type-tagged hash input; returned values are never persisted.

    This redacts request values from the receipt. It is not a password hash and
    does not claim secrecy for enumerable form values.
    """

    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "length": len(value),
            "sha256": sha256(value).hexdigest(),
        }
    # Upload-like objects must not leak reprs containing paths or handles.
    filename = getattr(value, "filename", None)
    if filename is not None:
        encoded = str(filename).encode()
        return {
            "type": "upload",
            "filename_sha256": sha256(encoded).hexdigest(),
        }
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": repr(value)}
    return {
        "type": f"object:{type(value).__module__}.{type(value).__qualname__}",
        "value": str(value),
    }
