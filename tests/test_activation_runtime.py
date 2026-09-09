"""Actual public entry points refuse before SQL/storage when activation drifts."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from corridor.activation import ActivationConfiguration, activate, route_manifest_digest
from corridor.activation_runtime import current_activation, owner_source_bootstrap, require_source_project
from corridor.config import settings
from corridor.control_plane import RouteRefused
from corridor.customer_routing import CustomerIdentity, CustomerRouter
from corridor.push_intake import PushBinding, PushPayload, accept_delivery, replay_delivery
from corridor.source_append import append_source_segments
from corridor.source_delivery import DeliveryBinding
from test_activation import evidence

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


class NoSql:
    """An existing session does not itself prove activation."""
    info = {"activated": True, "trusted": True}

    def execute(self, *args, **kwargs):
        raise AssertionError("unexpected SQL before activation refusal")

    def scalar(self, *args, **kwargs):
        raise AssertionError("unexpected SQL before activation refusal")


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    config = ActivationConfiguration("fixture-env", "fixture-customer", 1, "credential:7", "webhook",
        511, 606, "us-east-2", "fixture-code", "fixture-db", "fixture-image",
        "fixture-governance", "fixture-security", "a" * 64, "true", "corridor_web",
        route_manifest_digest(), "fixture-deployment")
    artifact = activate(config, evidence=evidence(tmp_path, config), operator="local:operator",
        revision="fixture-activation", custody=tmp_path / "custody", now=NOW)
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(asdict(config)))
    for name, value in {
        "environment": "production", "deployment_data_class": "customer",
        "customer_id": config.customer, "customer_environment_id": config.environment,
        "deployment_id": config.deployment_id, "live_pilot_web_boundary": True,
        "deployment_image_digest": config.image_digest, "deployment_data_region": config.data_region,
        "activation_configuration_path": str(path), "activation_receipt_path": str(artifact.path),
        "activation_receipt_sha256": artifact.sha256,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setenv("CORRIDOR_CODE_REVISION", config.code_revision)
    return config, path, artifact


def router_for(config):
    def unexpected(*args, **kwargs):
        raise AssertionError("route reached registry SQL or credential resolution")
    return CustomerRouter(SimpleNamespace(lookup=unexpected),
        CustomerIdentity(config.customer, config.environment, config.deployment_id), unexpected)


def test_missing_activation_prevents_routed_credentials_and_existing_session_append(deployed, monkeypatch):
    config, _, _ = deployed
    monkeypatch.setattr(settings, "activation_receipt_path", "")
    router = router_for(config)
    with pytest.raises(RouteRefused, match="current activation"):
        router.open_session(router.identity)
    # Even a caller bypassing CustomerRouter and supplying a trusted-looking
    # session cannot issue the source command, including its empty fast path.
    with pytest.raises(RouteRefused, match="current activation"):
        append_source_segments(NoSql(), project_id=1, document_id=1,
            recorded_verbal_origin_id=None, segments=[])


@pytest.mark.parametrize("change", ["source", "version", "code", "image", "region", "boundary", "receipt"])
def test_each_current_input_is_rechecked_before_routed_sql(deployed, monkeypatch, change):
    config, path, artifact = deployed
    assert current_activation() == config
    if change == "source":
        path.write_text(json.dumps(asdict(replace(config, source_configuration="credential:other"))))
    elif change == "version":
        path.write_text(json.dumps(asdict(replace(config, source_configuration_version="revision-2"))))
    elif change == "code":
        monkeypatch.setenv("CORRIDOR_CODE_REVISION", "new-code")
    elif change == "image":
        monkeypatch.setattr(settings, "deployment_image_digest", "other-image")
    elif change == "region":
        monkeypatch.setattr(settings, "deployment_data_region", "us-west-2")
    elif change == "boundary":
        monkeypatch.setattr(settings, "live_pilot_web_boundary", False)
    else:
        artifact.path.write_text('{}')
    router = router_for(config)
    with pytest.raises(RouteRefused, match="current activation"):
        router.open_session(router.identity)


@pytest.mark.parametrize("changed", [{"customer": "other"}, {"project_id": 2},
    {"channel": "project_alias"}, {"credential_id": 8}])
def test_push_mismatch_refuses_before_storage_or_sql(deployed, monkeypatch, changed):
    config, _, _ = deployed
    from corridor import push_intake
    monkeypatch.setattr(push_intake, "_stage", lambda *_: pytest.fail("unactivated bytes reached storage"))
    binding = replace(PushBinding(config.customer, config.project_id, "fixture-project", "webhook", 7), **changed)
    payload = PushPayload(b"fixture", "fixture.xlsx")
    for operation in (accept_delivery, replay_delivery):
        with pytest.raises(RouteRefused, match="source selection"):
            operation(NoSql(), binding, payload)


def test_pull_requires_bound_real_ledger_before_fetch(deployed):
    from corridor.connectors.pull_connector import sync_pull_connector
    def unexpected(*args):
        pytest.fail("unactivated pull reached provider")
    connector = SimpleNamespace(list_changes=unexpected, fetch_version=unexpected)
    with pytest.raises(RouteRefused, match="server-owned"):
        sync_pull_connector(connector, customer="fixture-customer", project="fixture-project",
            channel="webhook", ledger=SimpleNamespace(record=lambda **_: 1))


def test_unknown_production_class_and_shadow_refuse_customer_routes(deployed, monkeypatch):
    config, _, _ = deployed
    router = router_for(config)
    for value in ("", "unknown", "shadow"):
        monkeypatch.setattr(settings, "deployment_data_class", value)
        with pytest.raises(RouteRefused):
            router.open_session(router.identity)


def test_explicit_synthetic_and_local_development_preserve_source_behavior(monkeypatch):
    for environment, data_class in (("production", "synthetic"), ("development", ""), ("test", "")):
        monkeypatch.setattr(settings, "environment", environment)
        monkeypatch.setattr(settings, "deployment_data_class", data_class)
        assert current_activation() is None
        assert append_source_segments(NoSql(), project_id=1, document_id=1,
            recorded_verbal_origin_id=None, segments=[]) == ()


def test_direct_source_command_checks_actual_environment_and_project(deployed, runtime_database):
    config, _, _ = deployed
    with runtime_database.session_factory.begin() as owner:
        with pytest.raises(RouteRefused, match="outside the activated environment"):
            require_source_project(owner, config.project_id)
        owner.execute(text("insert into customer_environment_binding(singleton,customer_id,environment_id,deployment_id) values (true,:customer,:environment,:deployment)"),
            {"customer": config.customer, "environment": config.environment, "deployment": config.deployment_id})
        require_source_project(owner, config.project_id)
        with pytest.raises(RouteRefused, match="outside the activated project"):
            require_source_project(owner, config.project_id + 1)


def test_explicit_owner_bootstrap_does_not_authorize_another_session(deployed, runtime_database, monkeypatch):
    monkeypatch.setattr(settings, "activation_receipt_path", "")
    with runtime_database.session_factory.begin() as owner:
        with owner_source_bootstrap(owner):
            require_source_project(owner, 1)
            with pytest.raises(RouteRefused, match="current activation"):
                require_source_project(NoSql(), 1)
        with pytest.raises(RouteRefused, match="current activation"):
            require_source_project(owner, 1)


def test_delivery_version_binding_is_exact_before_any_sql(deployed):
    from corridor.activation_runtime import require_source_delivery
    config, _, _ = deployed
    binding = DeliveryBinding(config.customer, config.project_id, "fixture-project", "push",
        config.source_channel, config.source_configuration, "unexpected-version", 7)
    with pytest.raises(RouteRefused, match="source selection"):
        require_source_delivery(NoSql(), binding)
