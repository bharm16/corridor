"""Resolve deployment inputs once, and recheck the customer route on every use.

The old factories chose a default database at import. Deployed customer work
now needs the separately configured resolver and explicit process identity.
Only an unconfigured development/test process keeps the historical local path.
Credentials are resolved from named environment variables, never registry text.
"""

from __future__ import annotations

from functools import lru_cache
import os

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from corridor.config import settings
from corridor.control_plane import ControlPlane, RouteRefused
from corridor.customer_routing import CustomerIdentity, CustomerRouter


def environment_credential(reference: str) -> str:
    prefix, _, name = reference.partition(":")
    if prefix != "env" or not name or not os.environ.get(name):
        raise RouteRefused("customer database credential reference is unavailable")
    return os.environ[name]


@lru_cache(maxsize=8)
def build_customer_router(
    url: str, customer: str, environment: str, deployment: str
) -> CustomerRouter:
    from corridor.activation_runtime import require_customer_route
    require_customer_route(CustomerIdentity(customer, environment, deployment))
    engine = None
    try:
        identity = CustomerIdentity(customer, environment, deployment)
        engine = create_engine(url, hide_parameters=True)
        with engine.connect() as connection:
            permitted = connection.scalar(
                text("""
                select pg_has_role(current_user, 'corridor_control_resolver', 'member')
                    and not has_table_privilege(current_user, 'control_plane.customer_environments', 'SELECT')
                    and not has_table_privilege(current_user, 'control_plane.destruction_receipts', 'INSERT')
            """)
            )
        if not permitted:
            raise RouteRefused(
                "customer routing requires the scoped resolver credential"
            )
        return CustomerRouter(ControlPlane(engine), identity, environment_credential)
    except (SQLAlchemyError, ValueError, TypeError):
        if engine is not None:
            engine.dispose()
        raise RouteRefused("customer routing configuration is incomplete") from None


def configured_customer_router() -> CustomerRouter | None:
    from corridor.activation_runtime import current_activation, runtime_data_class
    data_class = runtime_data_class()
    if data_class == "shadow":
        if settings.control_plane_resolver_database_url:
            raise RouteRefused("shadow deployments cannot configure customer routing")
        return None
    current_activation()
    values = (
        settings.control_plane_resolver_database_url,
        settings.customer_id,
        settings.customer_environment_id,
        settings.deployment_id,
    )
    if (
        not any(values)
        and not settings.customer_routing_key
        and settings.environment in {"development", "test"}
    ):
        return None
    if not all(values):
        raise RouteRefused("customer routing configuration is incomplete")
    return build_customer_router(*values)
