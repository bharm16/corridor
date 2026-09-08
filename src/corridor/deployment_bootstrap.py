"""Configure the existing synthetic deployment before either service starts.

Task definitions alone cannot satisfy #656: the separate control plane needs
real scoped logins, and the migrated customer database must attest the same
immutable identity as its registry row. The existing Migration task owns this
bounded bootstrap. Runtime tasks never receive either schema owner's URL.

Configuration creates a missing route disabled and never changes enabled,
hold, or connector state. Enabling is the existing explicit control-plane
operations command; a release requesting running services only verifies it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
import json
import os
import re
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from corridor.control_plane import ControlPlane, EnvironmentRegistration, RouteRefused
from corridor.control_plane_schema import initialize_control_plane
from corridor.customer_routing import (
    CustomerIdentity, CustomerRouter, bind_customer_environment, database_address,
)
from corridor.customer_routing_runtime import build_customer_router


class DeploymentRefused(ValueError):
    """The supplied deployment inputs do not prove a safe customer route."""


@dataclass(frozen=True)
class DeploymentConfiguration:
    identity: CustomerIdentity
    customer_owner_url: str = field(repr=False)
    control_owner_url: str = field(repr=False)
    control_operations_url: str = field(repr=False)
    control_resolver_url: str = field(repr=False)
    web_url: str = field(repr=False)
    worker_url: str = field(repr=False)
    object_namespace_ref: str
    data_class: str
    environment: str

    def __post_init__(self) -> None:
        if self.data_class != "synthetic":
            raise DeploymentRefused("bootstrap requires explicit synthetic deployment configuration")
        urls = [make_url(value) for value in (
            self.customer_owner_url, self.control_owner_url,
            self.control_operations_url, self.control_resolver_url,
            self.web_url, self.worker_url,
        )]
        customer, control, operations, resolver, web, worker = urls
        for url in urls:
            if (
                url.drivername != "postgresql+psycopg" or not url.username
                or not url.password or not url.host or not url.database or not url.port
                or set(url.query) - {"sslmode", "sslrootcert", "connect_timeout", "application_name"}
            ):
                raise DeploymentRefused("explicit PostgreSQL connection inputs are required")
            if self.environment not in {"development", "test"} and (
                url.query.get("sslmode") not in {"verify-full", "verify-ca"}
                or not url.query.get("sslrootcert")
            ):
                raise DeploymentRefused("deployed database connections require verified TLS")
        if database_address(customer) == database_address(control):
            raise DeploymentRefused("control plane must be outside the customer database")
        if self.environment not in {"development", "test"} and (
            (customer.host, customer.port) == (control.host, control.port)
        ):
            raise DeploymentRefused("deployed control plane requires an independent database instance")
        if any(database_address(url) != database_address(control) for url in (operations, resolver)):
            raise DeploymentRefused("control-plane capability endpoints disagree")
        if len({control.username, operations.username, resolver.username}) != 3:
            raise DeploymentRefused("control-plane owner, operations and resolver logins must differ")
        for capability, url in (("web", web), ("worker", worker)):
            if url.username != f"corridor_{capability}" or database_address(url) != database_address(customer):
                raise DeploymentRefused("customer capability endpoint or login disagrees")
        # Applies the existing reference/identity validation before opening any
        # database or running a migration.
        self.registration()

    def registration(self) -> EnvironmentRegistration:
        address = make_url(self.customer_owner_url)
        assert address.host is not None and address.port is not None and address.database is not None
        return EnvironmentRegistration(
            **asdict(self.identity), database_host=address.host,
            database_port=address.port, database_name=address.database,
            web_credential_ref="env:WEB_DATABASE_URL",
            worker_credential_ref="env:WORKER_DATABASE_URL",
            object_namespace_ref=self.object_namespace_ref,
            connector_configuration_ref="configuration:none", enabled=False, hold=False,
        )

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> DeploymentConfiguration:
        def required(name: str) -> str:
            value = values.get(name)
            if not value:
                raise DeploymentRefused(f"{name} is required")
            return value

        owner = make_url(required("DATABASE_URL"))
        bucket = required("CORRIDOR_S3_BUCKET")
        prefix = values.get("CORRIDOR_S3_PREFIX", "").strip("/")
        return cls(
            identity=CustomerIdentity(
                required("CORRIDOR_CUSTOMER_ID"), required("CORRIDOR_CUSTOMER_ENVIRONMENT_ID"),
                required("CORRIDOR_DEPLOYMENT_ID"),
            ),
            customer_owner_url=owner.render_as_string(hide_password=False),
            control_owner_url=required("CONTROL_PLANE_DATABASE_URL"),
            control_operations_url=required("CONTROL_PLANE_OPERATIONS_DATABASE_URL"),
            control_resolver_url=required("CONTROL_PLANE_RESOLVER_DATABASE_URL"),
            # Only the bounded owner task builds these from its supplied parts;
            # neither runtime process receives DATABASE_URL or the other login.
            web_url=owner.set(username="corridor_web", password=required("CORRIDOR_WEB_DB_PASSWORD"))
                .render_as_string(hide_password=False),
            worker_url=owner.set(username="corridor_worker", password=required("CORRIDOR_WORKER_DB_PASSWORD"))
                .render_as_string(hide_password=False),
            object_namespace_ref=f"s3:{bucket}" + (f"/{prefix}" if prefix else ""),
            data_class=required("CORRIDOR_DEPLOYMENT_DATA_CLASS"),
            environment=required("CORRIDOR_ENVIRONMENT"),
        )


def provision_control_logins(owner: Engine, *, operations_url: str, resolver_url: str) -> None:
    """Create scoped LOGIN identities without widening existing role grants.

    Deployment supplies generated passwords. SQL parameters keep them out of
    the statement text sent by the client; the transaction-local value is used
    only by the server's bounded ALTER ROLE and disappears on transaction end.
    """

    capabilities = (
        (make_url(operations_url), "corridor_control_operations"),
        (make_url(resolver_url), "corridor_control_resolver"),
    )
    with owner.begin() as connection:
        for url, capability in capabilities:
            login = url.username or ""
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", login) or not url.password:
                raise DeploymentRefused("bounded control-plane login inputs are required")
            if database_address(url) != database_address(owner.url) or login in {
                owner.url.username, "corridor_control_operations", "corridor_control_resolver",
            }:
                raise DeploymentRefused("control-plane login must be distinct from its owner and capability")
            role = connection.execute(text(
                "select rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                "from pg_roles where rolname = :login"
            ), {"login": login}).mappings().first()
            if role is None:
                connection.execute(text(
                    f"create role {login} login nosuperuser nocreatedb nocreaterole noreplication nobypassrls"
                ))
            elif not role["rolcanlogin"] or not role["rolinherit"] or any(role[key] for key in (
                "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
            )):
                raise DeploymentRefused("existing control-plane login has unexpected authority")
            memberships = connection.execute(text(
                "select granted.rolname, membership.admin_option from pg_auth_members membership "
                "join pg_roles granted on granted.oid = membership.roleid "
                "join pg_roles member on member.oid = membership.member "
                "where member.rolname = :login"
            ), {"login": login}).mappings().all()
            if any(row["rolname"] != capability or row["admin_option"] for row in memberships):
                raise DeploymentRefused("existing control-plane login has unexpected memberships")
            connection.execute(text(f"grant {capability} to {login}"))
            connection.execute(
                text("select set_config('corridor.bootstrap_password', :password, true)"),
                {"password": url.password},
            )
            connection.execute(text(
                "do $$ begin execute format('alter role %I password %L', "
                f"'{login}', current_setting('corridor.bootstrap_password')); end $$"
            ))


def _same_binding(current: EnvironmentRegistration, expected: EnvironmentRegistration) -> bool:
    return replace(
        current, enabled=expected.enabled, hold=expected.hold,
        connector_configuration_ref=expected.connector_configuration_ref,
    ) == expected


def configure_deployment(
    configuration: DeploymentConfiguration, *, migrate_customer: Callable[[], None],
    require_enabled: bool = False,
) -> dict[str, str | bool]:
    """Bootstrap, migrate, bind and prove one explicit synthetic environment.

    The migration callable is the existing Alembic adapter. Keeping it at this
    boundary permits an already-migrated harness database to exercise the
    actual registration, login, binding and route contracts without replaying
    baseline history. Any failure propagates while the release services are zero.
    """

    owner = create_engine(configuration.control_owner_url, hide_parameters=True)
    customer = create_engine(configuration.customer_owner_url, hide_parameters=True)
    operations = create_engine(configuration.control_operations_url, hide_parameters=True)
    runtime = None
    try:
        # A restored or already-used destination may already be bound. Check
        # its identity before Alembic can mutate it, including when no route
        # exists yet in this control plane. A fresh database may have no
        # binding table; the post-migration binder establishes it below.
        with customer.connect() as connection:
            if inspect(connection).has_table("customer_environment_binding", schema="public"):
                bindings = connection.execute(text(
                    "select customer_id, environment_id, deployment_id "
                    "from public.customer_environment_binding"
                )).mappings().all()
                if bindings and (
                    len(bindings) != 1 or dict(bindings[0]) != asdict(configuration.identity)
                ):
                    raise DeploymentRefused("customer database is already bound to another environment")
        initialize_control_plane(owner)
        provision_control_logins(
            owner, operations_url=configuration.control_operations_url,
            resolver_url=configuration.control_resolver_url,
        )
        registry = ControlPlane(operations)
        expected = configuration.registration()
        try:
            current = registry.inspect(expected.environment_id)
        except RouteRefused:
            current = None
        if current is not None and not _same_binding(current, expected):
            raise DeploymentRefused("existing registry binding does not match the deployment")
        migrate_customer()
        bind_customer_environment(customer, configuration.identity)
        if current is None:
            registry.register(expected)
        # Verify each injected customer credential even while a newly created
        # route is intentionally disabled. These reads inspect identity only.
        for capability, url in (("web", configuration.web_url), ("worker", configuration.worker_url)):
            engine = create_engine(url, hide_parameters=True)
            try:
                with engine.connect() as connection:
                    if connection.scalar(text("select current_user")) != f"corridor_{capability}":
                        raise DeploymentRefused("customer credential has the wrong capability")
                    rows = connection.execute(text(
                        "select customer_id, environment_id, deployment_id from public.customer_environment_binding"
                    )).mappings().all()
                    if len(rows) != 1 or dict(rows[0]) != asdict(configuration.identity):
                        raise DeploymentRefused("customer credential reached a different environment")
            finally:
                engine.dispose()
        runtime = build_customer_router(
            configuration.control_resolver_url, configuration.identity.customer_id,
            configuration.identity.environment_id, configuration.identity.deployment_id,
        )
        # Use the same router as deployed processes, but resolve credentials
        # from this bounded owner operation instead of exposing both to web.
        credentials = {"env:WEB_DATABASE_URL": configuration.web_url,
                       "env:WORKER_DATABASE_URL": configuration.worker_url}
        runtime = CustomerRouter(runtime.control_plane, runtime.identity, credentials.__getitem__)
        current = registry.inspect(expected.environment_id)
        if current.enabled:
            for capability in ("web", "worker"):
                with runtime.open_session(runtime.identity, capability=capability):
                    pass
        elif require_enabled:
            raise DeploymentRefused("customer route is disabled; explicit operations enablement is required")
        else:
            try:
                runtime.control_plane.lookup(**asdict(runtime.identity))
            except RouteRefused:
                pass
            else:
                raise DeploymentRefused("disabled customer route unexpectedly resolved")
        return {"status": "configured", **asdict(configuration.identity),
                "enabled": current.enabled, "hold": current.hold}
    finally:
        if runtime is not None:
            runtime.close()
            runtime.control_plane.engine.dispose()
        operations.dispose()
        customer.dispose()
        owner.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--require-enabled", action="store_true")
    args = parser.parse_args(argv)
    try:
        configuration = DeploymentConfiguration.from_environment(os.environ)

        def migrate_customer() -> None:
            # Alembic owns customer schema history. Give its subprocess only
            # the customer owner URL and the passwords its baseline requires.
            environment = dict(os.environ)
            for name in (
                "CONTROL_PLANE_DATABASE_URL", "CONTROL_PLANE_OPERATIONS_DATABASE_URL",
                "CONTROL_PLANE_RESOLVER_DATABASE_URL",
            ):
                environment.pop(name, None)
            subprocess.run(["alembic", "upgrade", "head"], env=environment, check=True)

        result = configure_deployment(
            configuration, migrate_customer=migrate_customer,
            require_enabled=args.require_enabled,
        )
    except (SQLAlchemyError, ValueError, TypeError, KeyError, OSError, subprocess.CalledProcessError):
        # SQLAlchemy/driver messages can contain URLs or parameters. The
        # release receipt exposes identifiers and bounded status only.
        print("deployment bootstrap refused; inspect the configured identities, privileges and migration outcome", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
