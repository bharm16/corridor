"""Build-only activation evidence, deployed boundary smoke and frozen revisions.

Customer authorization stays an explicit operator act. This module validates
exact evidence artifacts and checks the actual web login and HTTP responses;
it never switches routing on or invents signed governance. A receipt is useful
only while every deployment/source configuration input still has its digest.
The control-plane operator must consult processing_authorized before enabling
an authoritative source, and after every configuration change.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from sqlalchemy import text

from corridor.web_boundary import PILOT_ROUTES, PROTECTED_RELATIONS

VERSION = "activation-v1"
BOUNDARY_VERSION = "live-pilot-web-boundary-v1"
BASE_GATES = frozenset({"customer_authorization", "identity_offboarding", "intake_hardening",
    "disposition", "due_work_recovery", "adopted_baseline", "single_project_workflow",
    "shadow_processing", "measurement_readiness", "customer_routing", "project_partition",
    "selected_ingress", "selected_source_class", "web_boundary"})


class ActivationRefused(ValueError):
    """A frozen deployment is missing successful, matching activation evidence."""


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value):
    return sha256(_bytes(value)).hexdigest()


def route_manifest_digest():
    return _digest({f"{method} {route}": sorted(item.relations)
        for (method, route), item in PILOT_ROUTES.items()})


@dataclass(frozen=True)
class ActivationConfiguration:
    environment: str
    customer: str
    project_id: int
    source_configuration: str
    source_channel: str
    ingress_issue: int
    source_class_issue: int
    data_region: str
    code_revision: str
    database_revision: str
    image_digest: str
    governance_revision: str
    security_revision: str
    disposition_inventory_digest: str
    boundary_mode: str
    boundary_role: str
    boundary_route_digest: str
    deployment_id: str
    model_provider_posture: str = "deterministic-no-model"
    processes_pdf: bool = False
    pulls_source: bool = False

    @property
    def identity(self):
        return _digest(asdict(self))


@dataclass(frozen=True)
class EvidenceArtifact:
    path: Path
    sha256: str

    def read(self):
        body = self.path.read_bytes()
        if sha256(body).hexdigest() != self.sha256:
            raise ActivationRefused("prerequisite artifact digest changed")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ActivationRefused("prerequisite artifact is not JSON") from exc


def collect_boundary_smoke(session, *, configuration: ActivationConfiguration,
                           request: Callable, cases: list[dict], now: datetime):
    """Observe deployed HTTP routes and database capability, under explicit scope.

    ``request`` is the deployed authenticated client. Cases specify a concrete
    URL, method and route template, and successful expected status for each
    enabled route; the operator supplies safe fixture requests for write routes.
    This collects observations, never accepts a caller-supplied pass flag.
    """
    role = session.execute(text("select current_user, session_user")).one()
    if tuple(role) != ("corridor_web", "corridor_web"):
        raise ActivationRefused("boundary smoke requires actual corridor_web login")
    binding = session.execute(text("select customer_id, environment_id, deployment_id from customer_environment_binding")).all()
    expected_binding = (configuration.customer, configuration.environment, configuration.deployment_id)
    if len(binding) != 1 or tuple(binding[0]) != expected_binding:
        raise ActivationRefused("actual database customer/environment/deployment binding differs")
    if configuration.boundary_mode not in {"true", "1", "yes", "on"}:
        raise ActivationRefused("live pilot boundary must be explicitly enabled")
    if configuration.boundary_route_digest != route_manifest_digest():
        raise ActivationRefused("approved route manifest differs from deployed code")
    readable = set(session.scalars(text("""select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='public' and c.relkind in ('r','v','m','p')
        and has_table_privilege(current_user,c.oid,'SELECT')""")))
    if readable - PROTECTED_RELATIONS:
        raise ActivationRefused("web login can read a revoked relation")
    approved = {(case["method"].upper(), case["template"]) for case in cases
        if (case["method"].upper(), case["template"]) in PILOT_ROUTES}
    if approved != set(PILOT_ROUTES):
        raise ActivationRefused("deployed smoke must exercise every enabled pilot route")
    if not any((case["method"].upper(), case["template"]) not in PILOT_ROUTES for case in cases):
        raise ActivationRefused("deployed smoke must exercise a disabled route")
    health = request("GET", "/health")
    if health.status_code != 200 or not any(
        item.get("component") == "live_pilot_web_boundary"
        and item.get("healthy") is True and item.get("detail") == "enforced"
        for item in health.json().get("checks", [])
    ):
        raise ActivationRefused("deployed service does not report an enforced web boundary")
    observations = []
    for case in cases:
        key = (case["method"].upper(), case["template"])
        enabled = key in PILOT_ROUTES
        expected = case["expected_status"] if enabled else 404
        if enabled and not 200 <= expected < 400:
            raise ActivationRefused("enabled route smoke must demonstrate success")
        response = request(case["method"], case["url"], **case.get("request", {}))
        if response.status_code != expected or "permission denied" in response.text.lower():
            raise ActivationRefused(f"deployed route smoke failed: {key}")
        observations.append({"method": key[0], "template": key[1], "status": response.status_code})
    return {"gate": "web_boundary", "outcome": "passed", "configuration": configuration.identity,
        "observed_at": now.isoformat(), "contract": BOUNDARY_VERSION,
        "actual_database_role": role[0], "actual_login": role[1],
        "database_binding": {"customer": binding[0][0], "environment": binding[0][1], "deployment_id": binding[0][2]},
        "route_manifest_digest": route_manifest_digest(), "boundary_state": "enforced", "observations": observations}


def activate(configuration: ActivationConfiguration, *, evidence: dict[str, EvidenceArtifact],
             operator: str, revision: str, custody: Path, now: datetime | None = None):
    """Freeze a successful explicit activation revision in external receipt custody.

    Evidence producers are trusted operational boundaries; supplying synthetic
    fixtures proves software only. The output never enables a control-plane route.
    An existing revision is immutable; a changed configuration requires a new
    explicit revision and processing_authorized rejects the preceding receipt.
    """
    now = now or datetime.now(timezone.utc)
    values = asdict(configuration)
    if now.tzinfo is None or not operator or not revision or not all(
        value for key, value in values.items() if key not in {"processes_pdf", "pulls_source"}
    ):
        raise ActivationRefused("activation must identify every configuration, operator and revision")
    if configuration.boundary_mode not in {"true", "1", "yes", "on"} or configuration.boundary_role != "corridor_web":
        raise ActivationRefused("live pilot web boundary is disabled or uses the wrong role")
    if configuration.boundary_route_digest != route_manifest_digest():
        raise ActivationRefused("activation route manifest is stale")
    required = set(BASE_GATES)
    if configuration.processes_pdf:
        required |= {"pdf_image_audit"}
    if configuration.model_provider_posture != "deterministic-no-model":
        required |= {"model_provider_governance"}
    if configuration.pulls_source:
        required |= {"delivery_checkpoint"}
    if required - evidence.keys():
        raise ActivationRefused("missing prerequisites: " + ", ".join(sorted(required - evidence.keys())))
    receipts = {}
    for gate in sorted(required):
        payload = evidence[gate].read()
        if payload.get("gate") != gate or payload.get("outcome") != "passed" or payload.get("configuration") != configuration.identity:
            raise ActivationRefused(f"failed or stale prerequisite: {gate}")
        try:
            observed = datetime.fromisoformat(payload["observed_at"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ActivationRefused(f"prerequisite lacks observation time: {gate}") from exc
        if observed.tzinfo is None or observed > now:
            raise ActivationRefused(f"invalid prerequisite observation time: {gate}")
        if gate == "web_boundary" and (payload.get("actual_login") != "corridor_web"
            or payload.get("actual_database_role") != "corridor_web"
            or payload.get("database_binding") != {"customer": configuration.customer,
                "environment": configuration.environment, "deployment_id": configuration.deployment_id}
            or payload.get("contract") != BOUNDARY_VERSION
            or payload.get("route_manifest_digest") != configuration.boundary_route_digest
            or payload.get("boundary_state") != "enforced"
            or not _complete_route_observations(payload.get("observations", []))):
            raise ActivationRefused("web boundary lacks actual deployment smoke")
        if gate == "disposition" and (payload.get("inventory_digest") != configuration.disposition_inventory_digest
            or not payload.get("external_receipt_reference") or not payload.get("rehearsal_receipt_sha256")):
            raise ActivationRefused("disposition lacks matching inventory and external rehearsal custody")
        receipts[gate] = evidence[gate].sha256
    payload = {"version": VERSION, "configuration": values, "configuration_digest": configuration.identity,
        "operator": operator, "revision": revision, "activated_at": now.isoformat(), "evidence": receipts}
    custody.mkdir(parents=True, exist_ok=True)
    # Derive filenames from the revision; arbitrary operator text cannot escape
    # the dedicated receipt namespace. O_EXCL also serializes competing writers.
    path = custody / (_digest({"environment": configuration.environment,
        "project": configuration.project_id, "revision": revision}) + ".json")
    body = _bytes(payload)
    # Publish only a complete fsynced object. A crash before link leaves an
    # unreferenced temporary file; a crash after link leaves a complete receipt.
    descriptor, temporary_name = tempfile.mkstemp(prefix=".activation-", dir=custody)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            previous = json.loads(path.read_bytes())
            if any(previous.get(key) != payload[key] for key in payload if key != "activated_at"):
                raise ActivationRefused("activation revision already binds different evidence or configuration")
            return EvidenceArtifact(path, sha256(path.read_bytes()).hexdigest())
        return EvidenceArtifact(path, sha256(body).hexdigest())
    finally:
        temporary.unlink(missing_ok=True)


def _complete_route_observations(observations):
    approved = set()
    disabled = False
    for item in observations:
        key = (item.get("method"), item.get("template"))
        status = item.get("status", 0)
        if key in PILOT_ROUTES and 200 <= status < 400:
            approved.add(key)
        elif key not in PILOT_ROUTES and status == 404:
            disabled = True
        else:
            return False
    return approved == set(PILOT_ROUTES) and disabled


def processing_authorized(configuration: ActivationConfiguration, receipt: EvidenceArtifact) -> bool:
    """Any changed deployment/source/security input disables the old activation."""
    try:
        payload = receipt.read()
    except (OSError, ActivationRefused):
        return False
    return (payload.get("version") == VERSION
        and payload.get("configuration_digest") == configuration.identity
        and payload.get("configuration") == asdict(configuration))
