"""Attributable, privacy-safe receipts for ordinary frontend requests.

Browser screenshots establish what was visible, not which HTTP handler
committed a Project Record act.  These receipts are written by the exact
ordinary route, in that route's SQLAlchemy transaction, so Product Proving can
join visible use to the durable act without trusting a browser-driver claim.

Only route identity, response status, bounded subject ids, and a digest of the
request fields are retained.  Raw query/form values are never stored here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session
from starlette.responses import Response

from corridor import audit
from corridor.models import AuditLog
from corridor.principals import HumanPrincipal


SCHEMA_VERSION = "corridor.product-proving-frontend-request.v2"
_FIELD_DIGEST_DOMAIN = b"corridor.frontend-request-fields.v2\0"

# One closed route vocabulary prevents a caller from spelling an arbitrary
# handler identity into an otherwise well-formed receipt. Status remains
# response-derived, then must be one the registered route can actually return.
ROUTE_CONTRACTS: Mapping[str, tuple[str, str, frozenset[int]]] = {
    "coordinate_statement_screen": (
        "/statements/{slug}/{candidate_id}/coordinate", "GET", frozenset({200})
    ),
    "save_coordinated_statement": (
        "/statements/{slug}/{candidate_id}/coordinate", "POST", frozenset({303})
    ),
    "save_admitted_statement_scope": (
        "/statements/{slug}/{candidate_id}/admitted/scope", "POST", frozenset({400})
    ),
    "keep_unresolved_statement": (
        "/statements/{slug}/{candidate_id}/keep-unresolved", "POST", frozenset({303})
    ),
    "save_admitted_statement_owner": (
        "/statements/{slug}/{candidate_id}/admitted/owner", "POST", frozenset({303})
    ),
    "save_admitted_statement_next_action": (
        "/statements/{slug}/{candidate_id}/admitted/next-action",
        "POST",
        frozenset({303}),
    ),
    "mark_waiting_statement_not_relevant": (
        "/statements/{slug}/{candidate_id}/not-relevant", "POST", frozenset({303})
    ),
    "correct_statement_screen": (
        "/statements/{slug}/{candidate_id}/correct", "GET", frozenset({200})
    ),
    "correct_statement_scope_from_screen": (
        "/statements/{slug}/{candidate_id}/correct/scope",
        "POST",
        frozenset({303, 400, 409}),
    ),
    "correct_statement_facts_from_screen": (
        "/statements/{slug}/{candidate_id}/correct/facts", "POST", frozenset({303})
    ),
    "clarify_dispute": (
        "/ledger/{slug}/{dependency_id}/clarify", "POST", frozenset({303})
    ),
    "reports": ("/reports/{slug}", "GET", frozenset({200})),
    "review_report": (
        "/reports/{slug}/prepared/{artifact_id}", "GET", frozenset({200})
    ),
    "download_prepared_report": (
        "/reports/{slug}/prepared/{artifact_id}/download", "GET", frozenset({200})
    ),
    "preview_prepared_report": (
        "/reports/{slug}/prepared/{artifact_id}/preview", "GET", frozenset({200})
    ),
    "release_prepared_report": (
        "/reports/{slug}/prepared/{artifact_id}/release", "POST", frozenset({201})
    ),
    "render_report": ("/reports/{slug}/render", "POST", frozenset({201})),
    "release_report": ("/reports/{slug}/release", "POST", frozenset({201})),
    "coordinator_home": ("/work/{slug}", "GET", frozenset({200})),
    "queue": ("/queue/{slug}", "GET", frozenset({200})),
    "internal_report": ("/internal-report/{slug}", "GET", frozenset({200})),
    "internal_report_full": (
        "/internal-report/{slug}/full", "GET", frozenset({200})
    ),
    "internal_report_alerts": (
        "/internal-report/{slug}/alerts/{rule}", "GET", frozenset({200})
    ),
    "internal_report_workbook": (
        "/internal-report/{slug}/workbook.xlsx", "GET", frozenset({200})
    ),
    "operations_checks": (
        "/operations/{slug}/checks", "GET", frozenset({200})
    ),
    "operations_checks_preview": (
        "/operations/{slug}/checks/preview", "POST", frozenset({200, 400})
    ),
    "save_operations_checks": (
        "/operations/{slug}/checks", "POST", frozenset({303, 400})
    ),
    "processing_operations": ("/operations/{slug}", "GET", frozenset({200})),
    "declare_operations_active_run": (
        "/operations/{slug}/runs/{document_id}/declare", "POST", frozenset({303})
    ),
    "suspend_operations_unknown_scope": (
        "/operations/{slug}/unknown-scope/suspend", "POST", frozenset({303})
    ),
    "lift_operations_unknown_scope": (
        "/operations/{slug}/unknown-scope/lift", "POST", frozenset({303})
    ),
    "assign_owner": (
        "/dependencies/{dependency_id}/owner", "POST", frozenset({303})
    ),
    "record_next_action": (
        "/dependencies/{dependency_id}/action", "POST", frozenset({303})
    ),
    "keep_unresolved_candidate": (
        "/candidates/{candidate_id}/keep-unresolved", "POST", frozenset({303})
    ),
    "accept": ("/candidates/{candidate_id}/accept", "POST", frozenset({303})),
    "edit_accept": (
        "/candidates/{candidate_id}/edit-accept", "POST", frozenset({303})
    ),
    "merge": ("/candidates/{candidate_id}/merge", "POST", frozenset({303})),
    "reject": ("/candidates/{candidate_id}/reject", "POST", frozenset({303})),
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


def record_frontend_request(
    session: Session,
    *,
    principal: HumanPrincipal,
    route_name: str,
    route_template: str,
    method: str,
    response: Response,
    subject: FrontendRequestSubject,
    request_fields: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
) -> AuditLog:
    """Append one server-observed request in the caller's transaction."""

    normalized_method = method.strip().upper()
    if normalized_method not in {"GET", "POST"}:
        raise ValueError("frontend request method is unsupported")
    contract = ROUTE_CONTRACTS.get(route_name)
    if contract is None or not route_template.startswith("/"):
        raise ValueError("frontend request route identity is invalid")
    status = response.status_code
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("frontend request status is invalid")
    expected_template, expected_method, expected_statuses = contract
    if (
        route_template != expected_template
        or normalized_method != expected_method
        or status not in expected_statuses
    ):
        raise ValueError("frontend request does not match its registered route")
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
