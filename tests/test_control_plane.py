"""Registry, routing and external custody through real PostgreSQL boundaries (#656)."""

from dataclasses import replace
import pytest

from corridor.control_plane import ControlPlane, EnvironmentRegistration
from corridor.control_plane_schema import initialize_control_plane


def test_registered_environment_is_readable_without_customer_content(
    customer_environment_databases,
):
    control_engine, first, _ = customer_environment_databases
    initialize_control_plane(control_engine)
    owner_url = first.session_factory.kw["bind"].url
    registry = ControlPlane(control_engine)
    registration = EnvironmentRegistration(
        customer_id="synthetic-a",
        environment_id="environment-a",
        deployment_id="local-proof",
        database_host=owner_url.host,
        database_port=owner_url.port,
        database_name="registration_only",
        web_credential_ref="env:WEB_A",
        worker_credential_ref="env:WORKER_A",
        object_namespace_ref="namespace:synthetic-a",
        connector_configuration_ref="configuration:synthetic-a",
        hold=False,
    )
    assert registry.register(registration) == registration
    assert (
        registry.lookup("synthetic-a", "environment-a", "local-proof") == registration
    )
    registry.set_state("environment-a", enabled=False, hold=True)
    assert registry.inspect("environment-a") == replace(
        registration, enabled=False, hold=True
    )


def test_initialize_admits_the_disposition_plan_table_and_reruns_idempotently(
    customer_environment_databases,
):
    from sqlalchemy import inspect as sa_inspect

    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    # A re-run inspects the existing inventory; the disposition plan table (#514)
    # must be in the allowlist or this raises "customer or unrelated relations".
    initialize_control_plane(owner)
    tables = set(sa_inspect(owner).get_table_names(schema="control_plane"))
    assert {
        "customer_environments",
        "destruction_receipts",
        "disposition_plans",
    } <= tables


def test_customer_binding_authenticates_the_exact_browser_session():
    from corridor.customer_routing import CustomerIdentity, CustomerSessionSigner
    from corridor.control_plane import RouteRefused

    signer = CustomerSessionSigner("r" * 32)
    identity = CustomerIdentity("synthetic-a", "environment-a", "local-proof")
    binding = signer.issue(identity, "opaque-live-session")
    assert signer.authenticate(binding, "opaque-live-session") == identity
    for token, session in (
        (binding, "other-session"),
        (binding + "x", "opaque-live-session"),
        ("", "opaque-live-session"),
    ):
        with pytest.raises(RouteRefused):
            signer.authenticate(token, session)


def test_customer_routes_keep_colliding_project_ids_in_their_database(
    customer_environment_databases,
):
    from sqlalchemy import select, text
    from corridor import access
    from corridor.customer_routing import (
        CustomerIdentity,
        CustomerRouter,
        bind_customer_environment,
    )
    from corridor.models import Document, Project, ProjectRosterEntry, SourceSegment
    from hashlib import sha256

    control_engine, first, another_customer = customer_environment_databases
    initialize_control_plane(control_engine)
    registry = ControlPlane(control_engine)
    with another_customer() as second:
        for suffix, database in (("a", first), ("b", second)):
            identity = CustomerIdentity(
                f"routing-{suffix}", f"route-{suffix}", "routing-proof"
            )
            engine = database.session_factory.kw["bind"]
            bind_customer_environment(engine, identity)
            web_url = engine.url.set(
                username="corridor_web", password="corridor_web"
            ).render_as_string(hide_password=False)
            registration = EnvironmentRegistration(
                **identity.__dict__,
                database_host=engine.url.host,
                database_port=engine.url.port,
                database_name=database.name,
                web_credential_ref=f"env:ROUTE_{suffix.upper()}",
                worker_credential_ref=f"env:WORKER_{suffix.upper()}",
                object_namespace_ref=f"namespace:route-{suffix}",
                connector_configuration_ref="configuration:none",
            )
            registry.register(registration)
            with database.session_factory.begin() as session:
                for project_id, slug in ((7001, "mine"), (7002, "hidden")):
                    project = Project(
                        id=project_id,
                        slug=slug,
                        name=f"{suffix}-{slug}",
                        is_synthetic=True,
                    )
                    session.add(project)
                    session.flush()
                    document = Document(
                        project_id=project.id,
                        sha256=sha256(f"{suffix}-{slug}".encode()).hexdigest(),
                        filename=f"{slug}.xlsx",
                        doc_type="matrix",
                    )
                    session.add(document)
                    session.flush()
                    value = f"{suffix}-{slug}-source"
                    session.add(
                        SourceSegment(
                            project_id=project.id,
                            document_id=document.id,
                            kind="spreadsheet_cell",
                            exact_text=value,
                            content_sha256=sha256(value.encode()).hexdigest(),
                            ordinal=1,
                            sheet_name="Conflicts",
                            cell_range="A2",
                        )
                    )
                session.add(
                    ProjectRosterEntry(
                        project_id=7001,
                        principal_subject="local:member",
                        display_name="Member",
                        active=True,
                    )
                )
            router = CustomerRouter(registry, identity, lambda reference: web_url)
            try:
                with router.session(identity) as session:
                    assert (
                        session.scalar(text("select current_database()"))
                        == database.name
                    )
                    assert (
                        access.resolve_membership(session, "local:member", 7001)
                        is not None
                    )
                    assert (
                        access.resolve_membership(session, "local:member", 7002) is None
                    )
                    access.open_project_partition(
                        session, principal_subject="local:member", project_id=7001
                    )
                    assert list(session.scalars(select(SourceSegment.exact_text))) == [
                        f"{suffix}-mine-source"
                    ]
            finally:
                router.close()


def test_external_receipts_survive_actual_customer_database_removal(
    customer_environment_databases, control_plane_capabilities
):
    from datetime import datetime, timezone
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from corridor.control_plane import DestructionReceipt

    owner, _, another_customer = customer_environment_databases
    operations, resolver = control_plane_capabilities
    registry = ControlPlane(operations)
    with another_customer() as database:
        url = database.session_factory.kw["bind"].url
        registration = EnvironmentRegistration(
            customer_id="disposition-customer",
            environment_id="disposition-environment",
            deployment_id="disposition-proof",
            database_host=url.host,
            database_port=url.port,
            database_name=database.name,
            web_credential_ref="env:DISPOSITION_WEB",
            worker_credential_ref="env:DISPOSITION_WORKER",
            object_namespace_ref="namespace:disposition",
            connector_configuration_ref="configuration:none",
            enabled=False,
        )
        registry.register(registration)
        first = DestructionReceipt(
            "receipt-first",
            registration.environment_id,
            "operation-one",
            "postgresql",
            "failed",
            "receipt:attempt-one",
            "local:operator",
            datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        )
        registry.record_destruction(first)
        assert registry.record_destruction(first) == first
    # The harness context exited and really dropped the customer database.
    with owner.connect() as connection:
        assert (
            connection.scalar(
                text("select count(*) from pg_database where datname = :name"),
                {"name": registration.database_name},
            )
            == 0
        )
    completed = replace(
        first,
        receipt_id="receipt-second",
        outcome="completed",
        evidence_ref="receipt:database-absent",
        observed_at=datetime(2026, 9, 8, 12, 1, tzinfo=timezone.utc),
    )
    registry.record_destruction(completed)
    assert registry.destruction_receipts(registration.environment_id) == (
        first,
        completed,
    )
    with pytest.raises(ValueError, match="different observation"):
        registry.record_destruction(replace(completed, outcome="failed"))
    for engine in (operations, owner):
        for statement in (
            "update control_plane.destruction_receipts set outcome = 'failed'",
            "delete from control_plane.destruction_receipts",
            "truncate control_plane.destruction_receipts",
        ):
            with pytest.raises(DBAPIError), engine.begin() as connection:
                connection.execute(text(statement))
    with pytest.raises(DBAPIError):
        ControlPlane(resolver).destruction_receipts(registration.environment_id)


def test_control_plane_logins_cannot_cross_the_content_and_operations_boundary(
    customer_environment_databases, control_plane_capabilities
):
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.exc import DBAPIError
    from corridor.models import Project

    owner, _, _ = customer_environment_databases
    operations, resolver = control_plane_capabilities
    registry = ControlPlane(operations)
    registration = EnvironmentRegistration(
        customer_id="capability-customer",
        environment_id="capability-environment",
        deployment_id="capability-proof",
        database_host="localhost",
        database_port=5433,
        database_name="capability_only",
        web_credential_ref="env:CAPABILITY_WEB",
        worker_credential_ref="env:CAPABILITY_WORKER",
        object_namespace_ref="namespace:capability",
        connector_configuration_ref="configuration:none",
    )
    registry.register(registration)
    assert (
        ControlPlane(resolver).lookup(
            registration.customer_id,
            registration.environment_id,
            registration.deployment_id,
        )
        == registration
    )
    for engine in (operations, resolver):
        with pytest.raises(DBAPIError), engine.connect() as connection:
            connection.execute(select(Project))
    with pytest.raises(DBAPIError):
        ControlPlane(resolver).inspect(registration.environment_id)
    with pytest.raises(DBAPIError):
        ControlPlane(resolver).set_state(
            registration.environment_id, enabled=False, hold=False
        )
    web = create_engine(owner.url.set(username="corridor_web", password="corridor_web"))
    try:
        with pytest.raises(DBAPIError), web.connect() as connection:
            connection.execute(
                text("select * from control_plane.customer_environments")
            )
        with pytest.raises(DBAPIError), web.connect() as connection:
            connection.execute(
                text(
                    "select * from control_plane.resolve_environment('capability-customer','capability-environment','capability-proof')"
                )
            )
    finally:
        web.dispose()


def test_unknown_disabled_and_mismatched_customer_routes_refuse_before_customer_connection(
    customer_environment_databases,
):
    from corridor.control_plane import RouteRefused
    from corridor.customer_routing import CustomerIdentity, CustomerRouter

    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    identity = CustomerIdentity(
        "refused-customer", "refused-environment", "refused-proof"
    )

    def no_credential_may_be_requested(reference):
        pytest.fail("a refused route reached the customer credential boundary")

    router = CustomerRouter(registry, identity, no_credential_may_be_requested)
    with pytest.raises(RouteRefused):
        router.open_session(identity)
    registry.register(
        EnvironmentRegistration(
            **identity.__dict__,
            database_host="localhost",
            database_port=5433,
            database_name="refused_only",
            web_credential_ref="env:REFUSED_WEB",
            worker_credential_ref="env:REFUSED_WORKER",
            object_namespace_ref="namespace:refused",
            connector_configuration_ref="configuration:none",
            enabled=False,
        )
    )
    with pytest.raises(RouteRefused):
        router.open_session(identity)
    registry.set_state(identity.environment_id, enabled=True, hold=False)
    for wrong in (
        replace(identity, customer_id="other"),
        replace(identity, environment_id="other"),
        replace(identity, deployment_id="other"),
    ):
        with pytest.raises(RouteRefused):
            router.open_session(wrong)


def test_wrong_credential_destination_and_local_database_binding_refuse(
    customer_environment_databases, control_plane_capabilities
):
    from corridor.control_plane import RouteRefused
    from corridor.customer_routing import (
        CustomerIdentity,
        CustomerRouter,
        bind_customer_environment,
    )
    from corridor.customer_routing_runtime import build_customer_router

    owner, _, another_customer = customer_environment_databases
    operations, resolver = control_plane_capabilities
    with pytest.raises(RouteRefused, match="configuration"):
        build_customer_router(
            operations.url.render_as_string(hide_password=False),
            "customer",
            "environment",
            "deployment",
        )
    with another_customer() as database:
        engine = database.session_factory.kw["bind"]
        actual = CustomerIdentity(
            "actual-customer", "actual-environment", "mismatch-proof"
        )
        configured = replace(
            actual, customer_id="wrong-customer", environment_id="wrong-environment"
        )
        bind_customer_environment(engine, actual)
        with pytest.raises(RouteRefused, match="does not match"):
            bind_customer_environment(engine, configured)
        registry = ControlPlane(operations)
        registry.register(
            EnvironmentRegistration(
                **configured.__dict__,
                database_host=engine.url.host,
                database_port=engine.url.port,
                database_name=database.name,
                web_credential_ref="env:MISMATCH_WEB",
                worker_credential_ref="env:MISMATCH_WORKER",
                object_namespace_ref="namespace:mismatch",
                connector_configuration_ref="configuration:none",
            )
        )
        url = engine.url.set(username="corridor_web", password="corridor_web")
        for target in (
            url.set(database="other_database"),
            url.update_query_dict({"options": "-c role=corridor"}),
            url,
        ):
            router = CustomerRouter(
                ControlPlane(resolver),
                configured,
                lambda reference: target.render_as_string(hide_password=False),
            )
            try:
                with pytest.raises(RouteRefused):
                    router.open_session(configured)
            finally:
                router.close()
        with pytest.raises(ValueError, match="customer or unrelated"):
            initialize_control_plane(engine)


def test_operations_cli_accepts_only_references_and_reports_no_credential_values(
    customer_environment_databases,
    control_plane_capabilities,
    monkeypatch,
    tmp_path,
    capsys,
):
    import json
    from dataclasses import asdict
    from corridor.control_plane_cli import main

    operations, _ = control_plane_capabilities
    monkeypatch.setenv(
        "CONTROL_PLANE_OPERATIONS_DATABASE_URL",
        operations.url.render_as_string(hide_password=False),
    )
    registration = EnvironmentRegistration(
        customer_id="cli-customer",
        environment_id="cli-environment",
        deployment_id="cli-proof",
        database_host="localhost",
        database_port=5433,
        database_name="cli_only",
        web_credential_ref="env:CLI_WEB",
        worker_credential_ref="env:CLI_WORKER",
        object_namespace_ref="namespace:cli",
        connector_configuration_ref="configuration:none",
        enabled=False,
    )
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(asdict(registration)))
    assert main(["register", "--file", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["environment_id"] == "cli-environment"
    assert main(["inspect", "cli-environment"]) == 0
    assert "control-proof-password" not in capsys.readouterr().out
    payload = asdict(registration)
    payload["web_credential_ref"] = (
        "postgresql://operator:never-print-this@localhost/database"
    )
    path.write_text(json.dumps(payload))
    assert main(["register", "--file", str(path)]) == 2
    output = capsys.readouterr()
    assert "never-print-this" not in output.out + output.err
    monkeypatch.delenv("CONTROL_PLANE_OPERATIONS_DATABASE_URL")
    assert main(["inspect", "cli-environment"]) == 2


def test_http_and_worker_use_the_registered_environment_and_recheck_disabling(
    customer_environment_databases, control_plane_capabilities, monkeypatch
):
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from corridor import access
    from corridor.config import settings
    from corridor.control_plane import RouteRefused
    from corridor.customer_routing import CustomerIdentity, bind_customer_environment
    from corridor.customer_routing_runtime import build_customer_router
    from corridor.db import WorkerSession
    from corridor.models import PersonIdentity, Project, ProjectRosterEntry
    from corridor.web.app import app
    from corridor.web.customer_routing import CUSTOMER_COOKIE

    _, _, another_customer = customer_environment_databases
    operations, resolver = control_plane_capabilities
    registry = ControlPlane(operations)
    with another_customer() as database:
        identity = CustomerIdentity("http-customer", "http-environment", "http-proof")
        engine = database.session_factory.kw["bind"]
        bind_customer_environment(engine, identity)
        registration = EnvironmentRegistration(
            **identity.__dict__,
            database_host=engine.url.host,
            database_port=engine.url.port,
            database_name=database.name,
            web_credential_ref="env:HTTP_WEB",
            worker_credential_ref="env:HTTP_WORKER",
            object_namespace_ref="namespace:http",
            connector_configuration_ref="configuration:none",
        )
        registry.register(registration)
        for capability in ("web", "worker"):
            monkeypatch.setenv(
                f"HTTP_{capability.upper()}",
                engine.url.set(
                    username=f"corridor_{capability}", password=f"corridor_{capability}"
                ).render_as_string(hide_password=False),
            )
        for name, value in (
            ("customer_id", identity.customer_id),
            ("customer_environment_id", identity.environment_id),
            ("deployment_id", identity.deployment_id),
            (
                "control_plane_resolver_database_url",
                resolver.url.render_as_string(hide_password=False),
            ),
            ("customer_routing_key", "h" * 32),
            ("live_pilot_web_boundary", True),
        ):
            monkeypatch.setattr(settings, name, value)
        with database.session_factory.begin() as session:
            session.add_all(
                [
                    Project(
                        id=8001,
                        slug="http-mine",
                        name="My HTTP project",
                        is_synthetic=True,
                    ),
                    Project(
                        id=8002,
                        slug="http-hidden",
                        name="Hidden HTTP project",
                        is_synthetic=True,
                    ),
                ]
            )
            session.flush()
            session.add(
                PersonIdentity(
                    email_normalized="member@example.test",
                    principal_subject="local:http-member",
                )
            )
            session.add(
                ProjectRosterEntry(
                    project_id=8001,
                    principal_subject="local:http-member",
                    display_name="Member",
                    active=True,
                )
            )
            session.flush()
            token = access.issue_sign_in_token(
                session, "member@example.test", redirect_path="/"
            )
        try:
            with TestClient(app, base_url="https://testserver") as client:
                response = client.get(
                    "/sign-in/consume",
                    params={"token": token.raw_token},
                    follow_redirects=False,
                )
                assert response.status_code == 303
                assert response.cookies.get(CUSTOMER_COOKIE)
                assert response.cookies.get("corridor_session")
                reading = client.get(
                    "/", headers={"x-customer-id": "some-other-customer"}
                )
                assert reading.status_code == 200
                assert (
                    "My HTTP project" in reading.text
                    and "Hidden HTTP project" not in reading.text
                )
                assert client.get("/work/http-hidden").status_code == 404
                with WorkerSession() as worker, worker.begin():
                    assert (
                        worker.scalar(text("select current_database()"))
                        == database.name
                    )
                registry.set_state(identity.environment_id, enabled=False, hold=False)
                assert client.get("/").status_code == 503
                with pytest.raises(RouteRefused):
                    WorkerSession()
                registry.set_state(identity.environment_id, enabled=True, hold=False)
                client.cookies.delete(CUSTOMER_COOKIE)
                assert client.get("/").status_code == 503
                client.cookies.clear()
                monkeypatch.delenv("HTTP_WORKER")
                assert client.get("/health").status_code == 503
                assert client.get("/livez").status_code == 200
                from pathlib import Path

                Path(settings.corpus_store).mkdir()
                assert client.get("/readyz").status_code == 200
        finally:
            router = build_customer_router(
                settings.control_plane_resolver_database_url,
                identity.customer_id,
                identity.environment_id,
                identity.deployment_id,
            )
            router.close()
            router.control_plane.engine.dispose()
            build_customer_router.cache_clear()


def test_only_unconfigured_local_processes_may_use_the_legacy_database(monkeypatch):
    from corridor.config import settings
    from corridor.control_plane import RouteRefused
    from corridor.customer_routing_runtime import configured_customer_router

    for name in (
        "control_plane_resolver_database_url",
        "customer_id",
        "customer_environment_id",
        "deployment_id",
        "customer_routing_key",
    ):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "environment", "development")
    assert configured_customer_router() is None
    monkeypatch.setattr(settings, "customer_routing_key", "k" * 32)
    with pytest.raises(RouteRefused):
        configured_customer_router()
    monkeypatch.setattr(settings, "customer_routing_key", "")
    monkeypatch.setattr(settings, "customer_environment_id", "partial-environment")
    with pytest.raises(RouteRefused):
        configured_customer_router()
    monkeypatch.setattr(settings, "customer_environment_id", "")
    monkeypatch.setattr(settings, "environment", "nonproduction")
    with pytest.raises(RouteRefused):
        configured_customer_router()


def test_key_rotation_recovers_by_fresh_sign_in_without_accepting_foreign_cookies(
    customer_environment_databases, control_plane_capabilities, monkeypatch
):
    from urllib.parse import urlsplit

    from fastapi.testclient import TestClient
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from corridor.config import settings
    from corridor.customer_routing import (
        CustomerIdentity,
        CustomerSessionSigner,
        bind_customer_environment,
    )
    from corridor.customer_routing_runtime import build_customer_router
    from corridor.models import PersonIdentity, Project, ProjectRosterEntry
    from corridor.web import auth
    from corridor.web.app import app
    from corridor.web.customer_routing import CUSTOMER_COOKIE

    class Sender:
        def __init__(self):
            self.links = []

        def send_sign_in_link(self, *, email, link):
            self.links.append(link)

    _, _, another_customer = customer_environment_databases
    operations, resolver = control_plane_capabilities
    registry = ControlPlane(operations)
    with another_customer() as database:
        identity = CustomerIdentity(
            "recovery-customer", "recovery-environment", "recovery-proof"
        )
        engine = database.session_factory.kw["bind"]
        bind_customer_environment(engine, identity)
        registry.register(
            EnvironmentRegistration(
                **identity.__dict__,
                database_host=engine.url.host,
                database_port=engine.url.port,
                database_name=database.name,
                web_credential_ref="env:RECOVERY_WEB",
                worker_credential_ref="env:RECOVERY_WORKER",
                object_namespace_ref="namespace:recovery",
                connector_configuration_ref="configuration:none",
            )
        )
        monkeypatch.setenv(
            "RECOVERY_WEB",
            engine.url.set(
                username="corridor_web", password="corridor_web"
            ).render_as_string(hide_password=False),
        )
        for name, value in (
            ("customer_id", identity.customer_id),
            ("customer_environment_id", identity.environment_id),
            ("deployment_id", identity.deployment_id),
            (
                "control_plane_resolver_database_url",
                resolver.url.render_as_string(hide_password=False),
            ),
            ("customer_routing_key", "old-key-" * 8),
            ("live_pilot_web_boundary", True),
        ):
            monkeypatch.setattr(settings, name, value)
        with database.session_factory.begin() as session:
            project = Project(
                slug="recovery-project", name="Recovered project", is_synthetic=True
            )
            session.add(project)
            session.flush()
            session.add(
                PersonIdentity(
                    email_normalized="recovery@example.test",
                    principal_subject="local:recovery-person",
                )
            )
            session.add(
                ProjectRosterEntry(
                    project_id=project.id,
                    principal_subject="local:recovery-person",
                    display_name="Recovery person",
                    active=True,
                )
            )

        sender = Sender()
        monkeypatch.setitem(
            app.dependency_overrides, auth.get_email_sender, lambda: sender
        )
        customer_reads = []

        def capture_customer_read(
            connection, cursor, statement, parameters, context, executemany
        ):
            if connection.engine.url.database == database.name:
                customer_reads.append(statement)

        try:
            with TestClient(app, base_url="https://testserver") as client:
                assert (
                    client.post(
                        "/sign-in/request", data={"email": "recovery@example.test"}
                    ).status_code
                    == 200
                )
                link = urlsplit(sender.links[-1])
                assert (
                    client.get(
                        link.path + "?" + link.query, follow_redirects=False
                    ).status_code
                    == 303
                )
                assert "Recovered project" in client.get("/").text
                old_session = client.cookies.get(auth.SESSION_COOKIE)
                old_binding = client.cookies.get(CUSTOMER_COOKIE)
                old_headers = {
                    "cookie": f"{auth.SESSION_COOKIE}={old_session}; {CUSTOMER_COOKIE}={old_binding}"
                }

                monkeypatch.setattr(settings, "customer_routing_key", "new-key-" * 8)
                event.listen(Engine, "before_cursor_execute", capture_customer_read)
                foreign = CustomerSessionSigner(settings.customer_routing_key).issue(
                    replace(
                        identity,
                        customer_id="foreign-customer",
                        environment_id="foreign-environment",
                    ),
                    old_session,
                )
                for binding in (old_binding, old_binding + "forged", foreign, ""):
                    headers = {
                        "cookie": f"{auth.SESSION_COOKIE}={old_session}; {CUSTOMER_COOKIE}={binding}"
                    }
                    assert client.get("/", headers=headers).status_code == 503
                    assert (
                        client.get(
                            "/work/recovery-project", headers=headers
                        ).status_code
                        == 503
                    )
                assert customer_reads == []

                # The same browser still carries its old cookies here. It must
                # reach the form instead of authenticating that old DB session.
                form = client.get("/sign-in", follow_redirects=False)
                assert form.status_code == 200
                for cookie in (auth.SESSION_COOKIE, auth.CSRF_COOKIE, CUSTOMER_COOKIE):
                    assert client.cookies.get(cookie) is None

                # A form already open during rotation can post directly, and a
                # fresh link can be opened in another tab still holding stale
                # cookies. Neither endpoint may reuse the stale identity.
                assert (
                    client.post(
                        "/sign-in/request",
                        data={"email": "recovery@example.test"},
                        headers=old_headers,
                    ).status_code
                    == 200
                )
                link = urlsplit(sender.links[-1])
                consumed = client.get(
                    link.path + "?" + link.query,
                    headers=old_headers,
                    follow_redirects=False,
                )
                assert consumed.status_code == 303
                renewed_session = client.cookies.get(auth.SESSION_COOKIE)
                renewed_binding = client.cookies.get(CUSTOMER_COOKIE)
                assert renewed_session != old_session
                assert renewed_binding != old_binding
                assert (
                    CustomerSessionSigner(settings.customer_routing_key).authenticate(
                        renewed_binding, renewed_session
                    )
                    == identity
                )
                assert "Recovered project" in client.get("/").text
                assert client.get("/sign-in", follow_redirects=False).status_code == 303

                registry.set_state(identity.environment_id, enabled=False, hold=False)
                assert client.get("/sign-in", headers=old_headers).status_code == 503
        finally:
            if event.contains(Engine, "before_cursor_execute", capture_customer_read):
                event.remove(Engine, "before_cursor_execute", capture_customer_read)
            router = build_customer_router(
                settings.control_plane_resolver_database_url,
                identity.customer_id,
                identity.environment_id,
                identity.deployment_id,
            )
            router.close()
            router.control_plane.engine.dispose()
            build_customer_router.cache_clear()
