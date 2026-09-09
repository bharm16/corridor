"""Recheck trusted activation inputs at customer routing and source-write seams.

Only deployment settings select configuration and receipt files. Requests and
session metadata never select or cache authority. Synthetic/local execution is
explicitly separate; shadow writes prove the actual provisioned source-only DB.
A narrow owner bootstrap context exists for the separately authorized baseline,
not as an application-session flag that any caller can set.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path

from sqlalchemy import text

from corridor.activation import ActivationConfiguration, EvidenceArtifact, processing_authorized, route_manifest_digest
from corridor.config import settings
from corridor.control_plane import RouteRefused

_BOOTSTRAP = ContextVar("corridor_authorized_source_bootstrap", default=None)


def runtime_data_class():
    value = settings.deployment_data_class
    if value in {"customer", "synthetic", "shadow"}:
        return value
    if not value and settings.environment in {"development", "test"}:
        return "local"
    raise RouteRefused("deployment data class must explicitly identify customer, synthetic or shadow")


def current_activation():
    """Read the complete current deployment contract and receipt on every use."""
    if runtime_data_class() != "customer":
        return None
    try:
        if not all((settings.activation_configuration_path, settings.activation_receipt_path,
                    settings.activation_receipt_sha256, settings.deployment_image_digest,
                    settings.deployment_data_region, os.environ.get("CORRIDOR_CODE_REVISION"))):
            raise ValueError("missing trusted deployment input")
        configuration = ActivationConfiguration(**json.loads(Path(settings.activation_configuration_path).read_bytes()))
        if (
            (configuration.customer, configuration.environment, configuration.deployment_id)
            != (settings.customer_id, settings.customer_environment_id, settings.deployment_id)
            or configuration.code_revision != os.environ["CORRIDOR_CODE_REVISION"]
            or configuration.image_digest != settings.deployment_image_digest
            or configuration.data_region != settings.deployment_data_region
            or settings.live_pilot_web_boundary is not True
            or configuration.boundary_mode not in {"true", "1", "yes", "on"}
            or configuration.boundary_role != "corridor_web"
            or configuration.boundary_route_digest != route_manifest_digest()
        ):
            raise ValueError("deployment differs from activation")
        receipt = EvidenceArtifact(Path(settings.activation_receipt_path), settings.activation_receipt_sha256)
        if not processing_authorized(configuration, receipt):
            raise ValueError("activation absent or stale")
        return configuration
    except (OSError, ValueError, TypeError, KeyError):
        raise RouteRefused("customer processing requires a current activation receipt") from None


def require_customer_route(identity):
    """Before control-plane SQL, credential resolution or customer connection."""
    if runtime_data_class() == "shadow":
        raise RouteRefused("shadow deployments cannot open customer routes")
    configuration = current_activation()
    if configuration is not None and (
        identity.customer_id, identity.environment_id, identity.deployment_id
    ) != (configuration.customer, configuration.environment, configuration.deployment_id):
        raise RouteRefused("customer route differs from activation")


def require_source_project(session, project_id):
    """Check every source command, including direct calls with an existing session.

    Corrections can reference older captured source versions. They still require
    the current project activation, but do not pretend the old source is a new
    delivery under the current connector version.
    """
    data_class = runtime_data_class()
    if data_class in {"local", "synthetic"}:
        return
    if _BOOTSTRAP.get() is session:
        return
    if data_class == "shadow":
        from corridor.shadow_processing import verify_runtime
        verify_runtime(session, project_id=project_id, customer=settings.customer_id,
            environment=settings.customer_environment_id)
        return
    configuration = current_activation()
    if configuration.project_id != project_id:
        raise RouteRefused("source project is outside the activated project")
    # No assumption that a Session came through CustomerRouter: direct appends
    # must also prove which database their otherwise-valid receipt would reach.
    rows = session.execute(text("select customer_id, environment_id, deployment_id from customer_environment_binding")).all()
    if len(rows) != 1 or tuple(rows[0]) != (configuration.customer, configuration.environment, configuration.deployment_id):
        raise RouteRefused("source database is outside the activated environment")


def require_source_delivery(session, binding):
    """Before reading/storing a newly delivered source, bind exact ingress scope."""
    data_class = runtime_data_class()
    if data_class in {"local", "synthetic"}:
        return
    if data_class == "shadow":
        require_source_project(session, binding.project_id)
        if binding.customer != settings.customer_id:
            raise RouteRefused("source delivery is outside the shadow customer")
        return
    configuration = current_activation()
    if (binding.customer, binding.project_id, binding.channel, binding.configuration_identity,
        binding.configuration_version) != (configuration.customer, configuration.project_id,
        configuration.source_channel, configuration.source_configuration, configuration.source_configuration_version):
        raise RouteRefused("delivery source selection differs from activation")
    require_source_project(session, binding.project_id)


@contextmanager
def owner_source_bootstrap(session):
    """Explicit separate owner capability for human-authorized baseline bootstrap."""
    if runtime_data_class() in {"customer", "shadow"}:
        owner = session.scalar(text("""
            select current_user = session_user and current_user = pg_get_userbyid(relowner)
            from pg_class where oid='public.projects'::regclass
        """))
        if owner is not True:
            raise RouteRefused("pre-activation baseline bootstrap requires the separate schema owner")
    token = _BOOTSTRAP.set(session)
    try:
        yield
    finally:
        _BOOTSTRAP.reset(token)


def require_pull_delivery(ledger, *, customer, project, channel):
    """Only the production ledger can authorize source access in deployed lanes."""
    if runtime_data_class() in {"local", "synthetic"}:
        return
    # Refuse stale deployment evidence before invoking even the ledger's
    # session factory, which may itself open a routed customer session.
    current_activation()
    # The concrete ledger owns the real session factory and DeliveryBinding.
    # An arbitrary caller-provided function claiming success proves neither.
    from corridor.connector_polling import LedgerWriter
    if not isinstance(ledger, LedgerWriter):
        raise RouteRefused("customer pull requires the server-owned activated delivery ledger")
    ledger.authorize_source(customer=customer, project=project, channel=channel)
