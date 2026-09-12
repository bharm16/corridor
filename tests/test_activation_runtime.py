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
from corridor.source_authorization import (
    AuthorizedSourceBinding,
    record_source_authorization,
)
from corridor.source_delivery import DeliveryBinding
from test_activation import evidence

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)

# The two source bindings a pilot deployment needs at once (#886): the genuine
# project-connected channel #845 measures coverage on, and the manual upload
# ADR-0058 keeps as the explicit rare fallback. Before the authorized set
# existed the activation configuration could name one of them.
CONNECTED = AuthorizedSourceBinding(
    channel="project_alias", configuration_identity="credential:7",
    permitted_source_classes=("email", "ucm_revision"),
    authentication_mode="machine_credential")
UPLOAD = AuthorizedSourceBinding(
    channel="product_upload", configuration_identity="product-upload",
    permitted_source_classes=("ucm_revision",),
    authentication_mode="human_principal")
AUTHORIZATION = "fixture-source-authorization"


class NoSql:
    """An existing session does not itself prove activation."""
    info = {"activated": True, "trusted": True}

    def execute(self, *args, **kwargs):
        raise AssertionError("unexpected SQL before activation refusal")

    def scalar(self, *args, **kwargs):
        raise AssertionError("unexpected SQL before activation refusal")


def configuration_for(project_id=1, *, version=1, digest="b" * 64, identity=AUTHORIZATION):
    """The activated deployment contract, naming one recorded source set (#886)."""
    return ActivationConfiguration("fixture-env", "fixture-customer", project_id,
        identity, version, digest,
        511, 606, "us-east-2", "fixture-code", "fixture-db", "fixture-image",
        "fixture-governance", "fixture-security", "a" * 64, "true", "corridor_web",
        route_manifest_digest(), "fixture-deployment")


def activate_deployment(tmp_path, monkeypatch, config, label="deployment"):
    """Freeze one activation and point the deployment settings at it."""
    custody = tmp_path / label
    custody.mkdir(parents=True, exist_ok=True)
    artifact = activate(config, evidence=evidence(custody, config), operator="local:operator",
        revision="fixture-activation", custody=custody / "custody", now=NOW)
    path = custody / "configuration.json"
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


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    return activate_deployment(tmp_path, monkeypatch, configuration_for())


def bind_project(database, customer="fixture-customer", environment="fixture-env",
                 deployment="fixture-deployment"):
    """One project in a real database, bound to the deployment that will read it."""
    with database.session_factory.begin() as owner:
        project_id = owner.scalar(text(
            "insert into projects (slug, name) values ('fixture-project', 'Fixture') "
            "returning id"))
        owner.execute(text("insert into customer_environment_binding"
            "(singleton,customer_id,environment_id,deployment_id) "
            "values (true,:customer,:environment,:deployment)"),
            {"customer": customer, "environment": environment, "deployment": deployment})
    return project_id


def record_set(database, project_id, bindings, *, version=1, identity=AUTHORIZATION):
    """Record one version of the authorized set, as the operations actor would."""
    with database.session_factory.begin() as owner:
        return record_source_authorization(owner, project_id=project_id,
            authorization_identity=identity, authorization_version=version,
            customer="fixture-customer", environment="fixture-env",
            governing_authorization_identity="customer-authorization-1",
            governing_authorization_version="2026-09-01",
            bindings=bindings, issued_at=NOW, issued_by_actor="operations:issuer",
            recorded_by_actor="operations:recorder")


@pytest.fixture
def delivering(tmp_path, monkeypatch, runtime_database):
    """A deployment activated against a recorded set holding both channels.

    The digest is read back from the record rather than computed here: the
    command derives it from the rows it writes, which is the whole reason a
    changed set cannot pass the activation it no longer matches.
    """
    project_id = bind_project(runtime_database)
    recorded = record_set(runtime_database, project_id, [CONNECTED, UPLOAD])
    config, _, _ = activate_deployment(tmp_path, monkeypatch,
        configuration_for(project_id, digest=recorded.binding_set_sha256))
    return config, runtime_database, project_id


def delivery(config, project_id, binding, **overrides):
    """One delivery binding for a selection, as its own channel would build it."""
    values = dict(customer=config.customer, project_id=project_id,
        project_slug="fixture-project", transport="push", channel=binding.channel,
        configuration_identity=binding.configuration_identity,
        configuration_version=binding.configuration_version,
        credential_id=7 if binding.authentication_mode == "machine_credential" else None,
        delivered_by_principal=("" if binding.authentication_mode == "machine_credential"
                                else "person:coordinator"))
    return DeliveryBinding(**(values | overrides))


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
        path.write_text(json.dumps(asdict(replace(config,
            source_authorization_identity="other-source-authorization"))))
    elif change == "version":
        path.write_text(json.dumps(asdict(replace(config, source_authorization_version=2))))
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


@pytest.mark.parametrize("changed", [{"customer": "other"}, {"project_id": 2}])
def test_push_outside_the_activated_scope_refuses_before_storage_or_sql(deployed, monkeypatch, changed):
    """Customer and project are still answered by the receipt alone (#886).

    Which *selections* this project authorizes is a record with versions and a
    history, so the gate reads the database for it. Whose project this is is
    not: an activation receipt names one customer and one project, and a
    delivery outside them is refused before a session is touched at all.
    """
    config, _, _ = deployed
    from corridor import push_intake
    monkeypatch.setattr(push_intake, "_stage", lambda *_: pytest.fail("unactivated bytes reached storage"))
    binding = replace(PushBinding(config.customer, config.project_id, "fixture-project",
        CONNECTED.channel, 7), **changed)
    payload = PushPayload(b"fixture", "fixture.xlsx")
    for operation in (accept_delivery, replay_delivery):
        with pytest.raises(RouteRefused, match="outside the activated customer or project"):
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




def test_pull_success_callback_cannot_substitute_for_database_context(deployed):
    from corridor.activation_runtime import require_pull_delivery
    ledger = SimpleNamespace(activation_context=SimpleNamespace(authorize=lambda **kwargs: None))
    with pytest.raises(RouteRefused, match="delivery context"):
        require_pull_delivery(ledger, customer="fixture-customer", project="fixture-project", channel="webhook")


def test_pull_context_checks_real_session_and_database_binding(deployed, runtime_database):
    from contextlib import nullcontext
    from corridor.activation_runtime import DeliveryActivationContext, require_pull_delivery
    config, _, _ = deployed
    binding = delivery(config, config.project_id, CONNECTED)
    fake = DeliveryActivationContext(lambda: nullcontext(NoSql()), binding)
    with pytest.raises(RouteRefused, match="actual database session"):
        require_pull_delivery(SimpleNamespace(activation_context=fake), customer=config.customer,
            project="fixture-project", channel=CONNECTED.channel)
    context = DeliveryActivationContext(runtime_database.session_factory, binding)
    with pytest.raises(RouteRefused, match="outside the activated environment"):
        require_pull_delivery(SimpleNamespace(activation_context=context), customer=config.customer,
            project="fixture-project", channel=CONNECTED.channel)


def test_pull_context_passes_on_an_authorized_binding(delivering):
    from corridor.activation_runtime import DeliveryActivationContext, require_pull_delivery
    config, database, project_id = delivering
    context = DeliveryActivationContext(database.session_factory,
        delivery(config, project_id, CONNECTED))
    require_pull_delivery(SimpleNamespace(activation_context=context), customer=config.customer,
        project="fixture-project", channel=CONNECTED.channel)


# --- #886 The authorized set decides, and manual upload is in it or it is not


def test_one_activated_deployment_takes_a_connected_delivery_and_an_upload(delivering):
    """The defect, and the acceptance criterion: both channels, one activation.

    A pilot's front door is the per-project address (#845 measures coverage on
    it) and a person may still hand a file over (ADR-0058). The single
    channel/configuration pair could name one of the two, so the other was
    refused by a comparison rather than by a decision. Here both pass, because
    both were authorized by name.
    """
    from corridor.activation_runtime import require_source_delivery
    config, database, project_id = delivering
    with database.session_factory.begin() as session:
        require_source_delivery(session, delivery(config, project_id, CONNECTED))
        require_source_delivery(session, delivery(config, project_id, UPLOAD))


def test_an_upload_is_refused_where_the_set_does_not_authorize_one(
    tmp_path, monkeypatch, runtime_database
):
    """Manual upload is authorized, never excepted.

    `channel == product_upload or matches_activation` would have made every
    deployment take uploads forever, unwithdrawably and unattributably. On a
    project whose recorded set holds only the connected channel, the upload is
    refused -- by the absence of a binding, which somebody can add.
    """
    from corridor.activation_runtime import require_source_delivery
    project_id = bind_project(runtime_database)
    recorded = record_set(runtime_database, project_id, [CONNECTED])
    config, _, _ = activate_deployment(tmp_path, monkeypatch,
        configuration_for(project_id, digest=recorded.binding_set_sha256))
    with runtime_database.session_factory.begin() as session:
        require_source_delivery(session, delivery(config, project_id, CONNECTED))
        with pytest.raises(RouteRefused, match="source_binding_not_authorized"):
            require_source_delivery(session, delivery(config, project_id, UPLOAD))


def test_an_unrecorded_configuration_on_an_authorized_channel_is_refused(delivering):
    """A second alias into the project is a second binding, not the same one."""
    from corridor.activation_runtime import require_source_delivery
    config, database, project_id = delivering
    with database.session_factory.begin() as session:
        with pytest.raises(RouteRefused, match="source_binding_not_authorized"):
            require_source_delivery(session, delivery(config, project_id, CONNECTED,
                configuration_identity="credential:9"))


def test_a_changed_configuration_version_is_a_different_binding(delivering):
    """The version is part of the key, so a rotated configuration is re-authorized."""
    from corridor.activation_runtime import require_source_delivery
    config, database, project_id = delivering
    with database.session_factory.begin() as session:
        with pytest.raises(RouteRefused, match="source_binding_not_authorized"):
            require_source_delivery(session, delivery(config, project_id, CONNECTED,
                configuration_version="revision-2"))


def test_an_authorized_configuration_is_not_authorized_for_any_authentication(delivering):
    """The upload surface is authorized for a person, not for a machine secret."""
    from corridor.activation_runtime import require_source_delivery
    config, database, project_id = delivering
    with database.session_factory.begin() as session:
        with pytest.raises(RouteRefused, match="source_authentication_mode_not_authorized"):
            require_source_delivery(session, delivery(config, project_id, UPLOAD,
                credential_id=7, delivered_by_principal=""))


def test_a_removed_configuration_stops_new_deliveries_and_keeps_its_history(
    tmp_path, monkeypatch, delivering
):
    """Withdrawal is a version, and the version it replaced stays readable.

    Recording version 2 without the upload binding withdraws it; re-activating
    against version 2 is the deployment-authority half of the same act. The
    connected channel keeps working, the upload is refused because nothing
    authorizes it, and version 1 -- the terms the upload already delivered
    under -- is still there to read.
    """
    from corridor.activation_runtime import require_source_delivery
    from corridor.source_authorization import authorized_bindings, recorded_authorizations
    config, database, project_id = delivering
    with database.session_factory.begin() as session:
        require_source_delivery(session, delivery(config, project_id, UPLOAD))

    narrowed = record_set(database, project_id, [CONNECTED], version=2)
    config, _, _ = activate_deployment(tmp_path, monkeypatch,
        configuration_for(project_id, version=2, digest=narrowed.binding_set_sha256),
        label="narrowed")

    with database.session_factory.begin() as session:
        require_source_delivery(session, delivery(config, project_id, CONNECTED))
        with pytest.raises(RouteRefused, match="source_binding_not_authorized"):
            require_source_delivery(session, delivery(config, project_id, UPLOAD))

    with database.session_factory.begin() as session:
        history = recorded_authorizations(session, project_id)
        assert [record.authorization_version for record in history] == [1, 2]
        assert UPLOAD in authorized_bindings(session, history[0])
        assert UPLOAD not in authorized_bindings(session, history[1])


def test_a_queued_delivery_is_answered_by_the_set_in_force_when_it_is_processed(
    delivering
):
    """The record moved on and the receipt did not, so processing stops.

    A delivery that was queued while the upload was authorized is not carried
    through on the authorization it arrived under: the gate reads the set in
    force at the moment it processes, and a deployment activated against a
    version the database has replaced processes nothing new until an activation
    revision says it may. Nothing recorded under the old version is erased --
    the refusal names supersession, not a missing record.
    """
    from corridor.activation_runtime import require_source_delivery
    from corridor.source_authorization import recorded_authorizations
    config, database, project_id = delivering
    queued = delivery(config, project_id, UPLOAD)
    with database.session_factory.begin() as session:
        require_source_delivery(session, queued)

    record_set(database, project_id, [CONNECTED], version=2)

    with database.session_factory.begin() as session:
        for binding in (queued, delivery(config, project_id, CONNECTED)):
            with pytest.raises(RouteRefused, match="source_authorization_superseded"):
                require_source_delivery(session, binding)
        assert len(recorded_authorizations(session, project_id)) == 2


def test_a_project_with_no_recorded_set_delivers_nothing(tmp_path, monkeypatch, runtime_database):
    """Fail closed: an unauthorized project is refused, never defaulted open."""
    from corridor.activation_runtime import require_source_delivery
    project_id = bind_project(runtime_database)
    config, _, _ = activate_deployment(tmp_path, monkeypatch, configuration_for(project_id))
    with runtime_database.session_factory.begin() as session:
        with pytest.raises(RouteRefused, match="no_source_authorization"):
            require_source_delivery(session, delivery(config, project_id, CONNECTED))


# --- #951 The recorded source class is compared, not merely recorded ---------
#
# `permitted_source_classes` was carried on every binding and never compared to
# the material a delivery declared itself to be. The activated gate read the
# standing, matched the channel/configuration/authentication, and admitted the
# delivery whatever it claimed to carry -- so a binding authorized for
# `ucm_revision` alone took a `matrix` all the same. The comparison below closes
# that, using the one shared interpretation (`source_class_contract`) the
# onboarding path also reads, and it refuses inside `require_source_delivery`,
# which every channel calls before it stores or reads a byte.


def classified(config, project_id, binding, source_class, **overrides):
    """A delivery whose ingress declares a source class and its basis (#951)."""

    return delivery(
        config, project_id, binding,
        source_class=source_class,
        source_class_basis="coordinator declared it on the upload screen",
        source_class_basis_kind="declared",
        **overrides,
    )


def test_a_recognized_but_prohibited_source_class_is_refused(delivering):
    """The connected binding permits `email`/`ucm_revision`; a `matrix` is not it.

    The class is one the contract recognises, the channel/configuration/mode all
    match, and the delivery is still refused -- because the recorded permission
    names the classes this binding may carry and `matrix` is not among them.
    """
    from corridor.activation_runtime import require_source_delivery
    config, database, project_id = delivering
    with database.session_factory.begin() as session:
        with pytest.raises(RouteRefused, match="source_class_not_permitted"):
            require_source_delivery(
                session, classified(config, project_id, CONNECTED, "matrix")
            )


def test_an_unrecognized_source_class_is_refused(delivering):
    """An unknown classification is refused, never inferred into a permitted one."""
    from corridor.activation_runtime import require_source_delivery
    config, database, project_id = delivering
    with database.session_factory.begin() as session:
        with pytest.raises(RouteRefused, match="source_class_unrecognized"):
            require_source_delivery(
                session, classified(config, project_id, CONNECTED, "mystery_export")
            )


def test_a_permitted_declared_source_class_passes(delivering):
    """A declared class the binding names is admitted, the mode still enforced."""
    from corridor.activation_runtime import require_source_delivery
    config, database, project_id = delivering
    with database.session_factory.begin() as session:
        require_source_delivery(
            session, classified(config, project_id, CONNECTED, "ucm_revision")
        )
        require_source_delivery(
            session, classified(config, project_id, UPLOAD, "ucm_revision")
        )


def test_a_prohibited_source_class_is_refused_before_the_bytes_are_stored(delivering):
    """The refusal precedes storage: no ledger row is written for a bad class.

    `require_source_delivery` runs first inside `record_delivery`, so a class the
    binding does not permit stops the delivery before `take_delivery` inserts the
    row -- the prohibited stage never starts.
    """
    from corridor.models import SourceDelivery
    from corridor.source_delivery import DeliveryObservation, take_delivery
    from sqlalchemy import select

    config, database, project_id = delivering
    observation = DeliveryObservation(
        external_identity="matrix.xlsx", external_version="v1",
        content_digest="c" * 64, bytes_reference="objects/c",
    )
    with database.session_factory.begin() as session:
        with pytest.raises(RouteRefused, match="source_class_not_permitted"):
            take_delivery(
                session, classified(config, project_id, CONNECTED, "matrix"),
                observation, service_identity="tests", run_identity="run-1",
            )
    with database.session_factory.begin() as session:
        stored = session.scalars(
            select(SourceDelivery).where(SourceDelivery.project_id == project_id)
        ).all()
    assert stored == [], "a prohibited class was stored before it was refused"


def test_a_permitted_delivery_retains_its_classification_claim(delivering):
    """A stored delivery keeps the class, the contract version, and its basis.

    ADR-0099's stage two: after receipt and hashing the delivery carries the
    classification claim it was admitted on, so a later stage re-proves the same
    binding rather than re-sniffing the bytes.
    """
    from corridor.models import SourceDelivery
    from corridor.source_class_contract import CONTRACT_VERSION
    from corridor.source_delivery import DeliveryObservation, take_delivery
    from sqlalchemy import select

    config, database, project_id = delivering
    observation = DeliveryObservation(
        external_identity="ucm.xlsx", external_version="v1",
        content_digest="d" * 64, bytes_reference="objects/d",
    )
    with database.session_factory.begin() as session:
        # The upload binding authenticates by a person, so storing needs no
        # machine credential row -- the class, not the credential, is the subject.
        recorded = take_delivery(
            session, classified(config, project_id, UPLOAD, "ucm_revision"),
            observation, service_identity="tests", run_identity="run-1",
        )
    with database.session_factory.begin() as session:
        row = session.get(SourceDelivery, recorded.delivery_id)
        assert row.source_class == "ucm_revision"
        assert row.source_class_contract_version == CONTRACT_VERSION
        assert row.source_class_basis
        assert row.source_class_basis_kind == "declared"
