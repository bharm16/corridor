"""Deployment bootstrap against the harness-owned customer/control boundaries.

The fixture supplies an already migrated customer database. The test migration
adapter records its invocation; baseline-history coverage remains in Alembic's
own gate. Logins, grants, identity binding and route checks use real PostgreSQL.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from corridor.control_plane import ControlPlane, RouteRefused
from corridor.customer_routing import CustomerIdentity, bind_customer_environment
from corridor.deployment_bootstrap import (
    DeploymentConfiguration, DeploymentRefused, configure_deployment,
    provision_control_logins,
)


@pytest.fixture
def deployment(customer_environment_databases, control_plane_capabilities):
    control, _, customer_database = customer_environment_databases
    operations, resolver = control_plane_capabilities
    with customer_database() as customer:
        owner_url = customer.session_factory.kw["bind"].url
        suffix = uuid4().hex[:12]
        yield DeploymentConfiguration(
            identity=CustomerIdentity(f"synthetic-{suffix}", f"environment-{suffix}", "bootstrap-proof"),
            customer_owner_url=owner_url.render_as_string(hide_password=False),
            control_owner_url=control.url.render_as_string(hide_password=False),
            control_operations_url=operations.url.render_as_string(hide_password=False),
            control_resolver_url=resolver.url.render_as_string(hide_password=False),
            web_url=owner_url.set(username="corridor_web", password="corridor_web")
                .render_as_string(hide_password=False),
            worker_url=owner_url.set(username="corridor_worker", password="corridor_worker")
                .render_as_string(hide_password=False),
            object_namespace_ref=f"s3:synthetic-bucket-{suffix}",
            data_class="synthetic", environment="test",
        ), ControlPlane(operations)


def test_configuration_is_repeatable_and_never_undoes_operator_state(deployment):
    configuration, registry = deployment
    migrations = []

    def migrate():
        migrations.append("customer-migrated")

    first = configure_deployment(configuration, migrate_customer=migrate)
    assert first == {
        "status": "configured", "customer_id": configuration.identity.customer_id,
        "environment_id": configuration.identity.environment_id, "deployment_id": "bootstrap-proof",
        "enabled": False, "hold": False,
    }
    assert registry.inspect(configuration.identity.environment_id) == configuration.registration()
    assert configure_deployment(configuration, migrate_customer=migrate) == first
    assert migrations == ["customer-migrated", "customer-migrated"]

    registry.set_state(
        configuration.identity.environment_id, enabled=True, hold=True,
        connector_configuration_ref="configuration:approved-by-operator",
    )
    serving = configure_deployment(configuration, migrate_customer=migrate, require_enabled=True)
    assert serving["enabled"] is True
    assert serving["hold"] is True
    assert registry.inspect(configuration.identity.environment_id).connector_configuration_ref == "configuration:approved-by-operator"

    registry.set_state(configuration.identity.environment_id, enabled=False, hold=True)
    with pytest.raises(DeploymentRefused, match="route is disabled"):
        configure_deployment(configuration, migrate_customer=migrate, require_enabled=True)
    stopped = configure_deployment(configuration, migrate_customer=migrate)
    assert stopped["enabled"] is False
    assert stopped["hold"] is True


def test_a_conflicting_existing_binding_refuses_before_customer_migration(deployment):
    configuration, registry = deployment
    configure_deployment(configuration, migrate_customer=lambda: None)
    conflicting = replace(configuration, object_namespace_ref="s3:different-customer")
    migrated = []

    with pytest.raises(DeploymentRefused, match="existing registry binding"):
        configure_deployment(conflicting, migrate_customer=lambda: migrated.append(True))

    assert migrated == []
    assert registry.inspect(configuration.identity.environment_id).object_namespace_ref == configuration.object_namespace_ref


def test_a_different_customer_database_refuses_before_any_migration(deployment):
    configuration, registry = deployment
    customer = create_engine(configuration.customer_owner_url, hide_parameters=True)
    try:
        bind_customer_environment(
            customer, CustomerIdentity("another-customer", "another-environment", "another-deployment")
        )
    finally:
        customer.dispose()
    migrated = []

    with pytest.raises((DeploymentRefused, RouteRefused)):
        configure_deployment(configuration, migrate_customer=lambda: migrated.append(True))

    assert migrated == [], "a foreign database must not be migrated before identity refusal"
    with pytest.raises(RouteRefused):
        registry.inspect(configuration.identity.environment_id)


def test_an_isolated_registry_can_route_a_restored_database_with_its_original_identity(deployment):
    configuration, registry = deployment
    # A restored database retains its original immutable identity. Its
    # rehearsal registry is separate and starts empty, so the source registry
    # endpoint never needs to be changed or reused by the rehearsal process.
    customer = create_engine(configuration.customer_owner_url, hide_parameters=True)
    try:
        bind_customer_environment(customer, configuration.identity)
        result = configure_deployment(configuration, migrate_customer=lambda: None)
        assert result["enabled"] is False
        registry.set_state(configuration.identity.environment_id, enabled=True, hold=False)
        assert configure_deployment(
            configuration, migrate_customer=lambda: None, require_enabled=True
        )["enabled"] is True
        with customer.connect() as connection:
            binding = connection.execute(text(
                "select customer_id, environment_id, deployment_id from customer_environment_binding"
            )).one()
        assert tuple(binding) == (
            configuration.identity.customer_id, configuration.identity.environment_id,
            configuration.identity.deployment_id,
        )
    finally:
        customer.dispose()


def test_a_failed_customer_migration_leaves_registration_absent_and_can_be_retried(deployment):
    configuration, registry = deployment

    def failed_migration():
        raise RuntimeError("synthetic migration failure")

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        configure_deployment(configuration, migrate_customer=failed_migration)
    with pytest.raises(RouteRefused):
        registry.inspect(configuration.identity.environment_id)
    assert configure_deployment(configuration, migrate_customer=lambda: None)["enabled"] is False


def test_bootstrap_creates_missing_login_roles_with_only_the_declared_capability(
    customer_environment_databases, control_plane_capabilities,
):
    owner, _, _ = customer_environment_databases
    operations, resolver = control_plane_capabilities
    # The harness owns these unique role names and cleans them up. Remove its
    # initial logins to exercise first installation through the public API.
    for engine in (operations, resolver):
        engine.dispose()
    with owner.begin() as connection:
        for engine in (operations, resolver):
            connection.execute(text(f"drop role {engine.url.username}"))

    provision_control_logins(
        owner, operations_url=operations.url.render_as_string(hide_password=False),
        resolver_url=resolver.url.render_as_string(hide_password=False),
    )

    # A real connection with each generated credential, then public control-
    # plane operations, proves login creation and the declared boundary.
    registry = ControlPlane(operations)
    assert registry.destruction_receipts("unknown-environment") == ()
    with pytest.raises(RouteRefused):
        ControlPlane(resolver).lookup("unknown", "unknown", "unknown")
    with resolver.connect() as connection:
        assert connection.scalar(text(
            "select not has_table_privilege(current_user, 'control_plane.customer_environments', 'select')"
        )) is True


def test_bootstrap_refuses_to_inherit_extra_operations_membership(
    deployment, customer_environment_databases, control_plane_capabilities,
):
    configuration, _ = deployment
    owner, _, _ = customer_environment_databases
    operations, _ = control_plane_capabilities
    login = operations.url.username
    with owner.begin() as connection:
        connection.execute(text(f"grant corridor_control_resolver to {login}"))
    try:
        with pytest.raises(DeploymentRefused, match="unexpected memberships"):
            configure_deployment(configuration, migrate_customer=lambda: None)
    finally:
        with owner.begin() as connection:
            connection.execute(text(f"revoke corridor_control_resolver from {login}"))


def test_configuration_refuses_wrong_endpoint_capability_and_unverified_deployed_tls(deployment):
    configuration, _ = deployment
    with pytest.raises(DeploymentRefused, match="customer capability endpoint or login"):
        replace(configuration, web_url=configuration.customer_owner_url)
    with pytest.raises(DeploymentRefused, match="outside the customer database"):
        replace(configuration, control_owner_url=configuration.customer_owner_url)
    with pytest.raises(DeploymentRefused, match="verified TLS"):
        replace(configuration, environment="nonproduction")
    with pytest.raises(DeploymentRefused, match="explicit synthetic"):
        replace(configuration, data_class="customer")
    assert "password" not in repr(configuration)
