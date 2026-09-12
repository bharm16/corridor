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
from dataclasses import dataclass, field
from typing import Callable, Protocol
import json
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.activation import ActivationConfiguration, EvidenceArtifact, processing_authorized, route_manifest_digest
from corridor.db_roles import WEB_CAPABILITY_LOGIN
from corridor.config import settings
from corridor.control_plane import RouteRefused
from corridor.shadow_capabilities import verify_runtime

_BOOTSTRAP = ContextVar("corridor_authorized_source_bootstrap", default=None)
# ADR-0099's pre-activation authority. Deliberately shaped like `_BOOTSTRAP`
# and deliberately *not* the same thing: entering it requires a grant this
# process cannot write, read out of the customer database that is about to be
# written, for the one project and the one operation named. A caller who sets
# it without such a row gets nothing, because `limited_onboarding_authorization`
# refuses before the token exists.
_ONBOARDING = ContextVar("corridor_limited_onboarding_authorization", default=None)


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
            or configuration.boundary_role != WEB_CAPABILITY_LOGIN
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
        verify_runtime(session, project_id=project_id, customer=settings.customer_id,
            environment=settings.customer_environment_id)
        return
    if _onboarding_authorized(session, project_id):
        # The bounded pre-activation authority (ADR-0099). It is not a relaxed
        # activation check: an activation receipt is still required for every
        # operation this authorization does not name, and the grant behind this
        # one was proved in the database, for this project, before the context
        # opened. A shadow deployment never reaches here -- the bootstrap and
        # the shadow runtime stay two credentials and two acts.
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
    """Before reading/storing a newly delivered source, bind exact ingress scope.

    The selection this compares against is a **recorded set** (#886), not the
    single channel/configuration pair the activation configuration used to
    carry. One pair is one channel, so a deployment activated for a project
    alias refused a product upload -- a delivery ADR-0058 keeps as the explicit
    fallback and #823 made a canonical member of the delivery family. The
    repair is not an exception for that channel: every binding, the upload
    included, is authorized by name in `project_source_authorizations` or the
    delivery is refused.

    So the customer and the project are still refused here, before any SQL runs
    and before any byte reaches storage. Whether the delivery's own selection is
    authorized is a database question, for the same reason `require_source_project`
    already asks one: an authorization that can be versioned, withdrawn and read
    back as history is a record, not a file this process happens to hold, and a
    queued delivery must be answered by the set in force when it is processed
    rather than by the set that was in force when it arrived.
    """
    data_class = runtime_data_class()
    if data_class in {"local", "synthetic"}:
        return
    if data_class == "shadow":
        require_source_project(session, binding.project_id)
        if binding.customer != settings.customer_id:
            raise RouteRefused("source delivery is outside the shadow customer")
        return
    if _onboarding_authorized(session, binding.project_id):
        # Bounded receipt and safe staging of material inside the authorized
        # source scope is one of the activities ADR-0099 permits, and the
        # delivery selection an activation would compare against does not yet
        # exist. The project binding is still proved, by the same grant.
        return
    configuration = current_activation()
    if (binding.customer, binding.project_id) != (configuration.customer, configuration.project_id):
        raise RouteRefused("delivery source selection is outside the activated customer or project")
    require_source_project(session, binding.project_id)
    _require_authorized_source_binding(session, configuration, binding)


def _require_authorized_source_binding(session, configuration, binding):
    """The recorded set decides; the receipt says which version may decide.

    Two different refusals, kept apart because an operator acts on them
    differently: the database has no authorization for this delivery's
    selection, or it has one the deployment was not activated against. The
    second is what a queued delivery meets after somebody records a new
    version -- the record moved on, the receipt did not, and new processing
    stops until an activation revision says otherwise. Neither erases anything
    the previous version admitted.
    """
    from corridor.source_authorization import authentication_mode_of, source_binding_standing
    from corridor import source_class_contract

    standing = source_binding_standing(
        session,
        project_id=binding.project_id,
        customer=binding.customer,
        channel=binding.channel,
        configuration_identity=binding.configuration_identity,
        configuration_version=binding.configuration_version,
        authentication_mode=authentication_mode_of(binding),
    )
    if not standing.recorded:
        raise RouteRefused("delivery source selection is not authorized: no_source_authorization")
    activated = (configuration.source_authorization_identity,
        configuration.source_authorization_version, configuration.source_authorization_sha256)
    if (standing.authorization_identity, standing.authorization_version,
            standing.binding_set_sha256) != activated:
        superseded = standing.authorization_version > configuration.source_authorization_version
        raise RouteRefused("delivery source selection is not authorized: "
            + ("source_authorization_superseded" if superseded
               else "source_authorization_differs_from_activation"))
    if not standing.permitted:
        raise RouteRefused(f"delivery source selection is not authorized: {standing.reason}")
    # The recorded permission names the source classes this binding may carry,
    # and #951 makes that effective: a delivery that declares its class is
    # admitted only if the binding permits it, under the one shared
    # interpretation. A delivery that declares none is a bounded receipt whose
    # semantic class is not yet established, so it is not gated here. The
    # comparison is the last conjunctive restriction inside a matched binding --
    # a matching channel, configuration and mode do not excuse a prohibited
    # class -- and it refuses before any byte is stored or read.
    declared = getattr(binding, "source_class", "") or ""
    if declared:
        decision = source_class_contract.evaluate(
            standing.permitted_source_classes, declared
        )
        if not decision.permitted:
            raise RouteRefused(
                f"delivery source selection is not authorized: {decision.reason}"
            )


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


def _onboarding_authorized(session, project_id) -> bool:
    """Whether this exact session holds ADR-0099's authority for this project."""

    held = _ONBOARDING.get()
    return held is not None and held[0] is session and held[1] == project_id


@contextmanager
def limited_onboarding_authorization(session, *, project_id, operation, at):
    """The bounded authority the work before activation runs under (ADR-0099).

    Not the owner bootstrap. That context is the schema owner's and exists for
    one-time operations bootstrap; this one is the web capability's, is bound
    to one project and one named operation, and is opened only after the
    customer database itself says the grant permits that operation right now.
    A request cannot assert it: `require_onboarding_permission` reads
    `onboarding_grant_standing`, and `corridor_web` can neither write the grant
    nor record anything about it.
    """

    from corridor.onboarding_authorization import require_onboarding_permission

    standing = require_onboarding_permission(
        session, project_id=project_id, operation=operation, at=at
    )
    token = _ONBOARDING.set((session, project_id))
    try:
        yield standing
    finally:
        _ONBOARDING.reset(token)


class SourceDeliveryScope(Protocol):
    """The existing delivery binding's neutral activation contract.

    `transport` and `credential_id` are here because the authorized set names
    the authentication mode each binding permits (#886), and that mode is read
    off the delivery rather than asserted beside it: a pull authenticates
    neither way, and a push names exactly one of the two things that can
    authenticate it.
    """
    customer: str
    project_id: int
    project_slug: str
    transport: str
    channel: str
    configuration_identity: str
    configuration_version: str
    credential_id: int | None


@dataclass(frozen=True)
class DeliveryActivationContext:
    """A real session factory and source scope, checked here rather than asserted.

    Higher-level ledgers supply this context. No success callback, cached flag,
    or ledger implementation identity substitutes for database proof.
    """
    session_factory: Callable[[], Session] = field(repr=False)
    binding: SourceDeliveryScope

    def authorize(self, *, customer, project, channel):
        current_activation()
        if (customer, project, channel) != (self.binding.customer, self.binding.project_slug, self.binding.channel):
            raise RouteRefused("pull source differs from its server-owned delivery binding")
        with self.session_factory() as session:
            if not isinstance(session, Session):
                raise RouteRefused("pull authorization requires an actual database session")
            require_source_delivery(session, self.binding)


def require_pull_delivery(ledger, *, customer, project, channel):
    """Require the real session/binding contract before connector I/O."""
    if runtime_data_class() in {"local", "synthetic"}:
        return
    # Refuse stale evidence before invoking any session factory.
    current_activation()
    context = getattr(ledger, "activation_context", None)
    if not isinstance(context, DeliveryActivationContext):
        raise RouteRefused("customer pull requires the server-owned activated delivery context")
    context.authorize(customer=customer, project=project, channel=channel)
