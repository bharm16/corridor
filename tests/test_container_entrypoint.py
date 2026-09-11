"""The container entrypoint's URL composition and credential scrubbing.

These run without Docker on purpose. The image build is slow and belongs in CI;
the part that can silently connect the wrong role to the database, or leak a
password into a subprocess, is pure logic and deserves fast, direct tests.
"""

from __future__ import annotations

import pytest

from scripts import container_entrypoint as entrypoint


BASE = {
    "CORRIDOR_DB_HOST": "corridor.abc123.us-east-2.rds.amazonaws.com",
    "CORRIDOR_DB_PORT": "5432",
    "CORRIDOR_DB_NAME": "corridor",
    "CORRIDOR_DB_ADMIN_USERNAME": "corridor_admin",
    "CORRIDOR_DB_ADMIN_PASSWORD": "admin-secret",
    "CORRIDOR_WEB_DB_PASSWORD": "web-secret",
    "CORRIDOR_WORKER_DB_PASSWORD": "worker-secret",
}


def test_each_role_sets_exactly_one_url_variable():
    for role, expected in (
        ("migration", "DATABASE_URL"),
        ("web", "WEB_DATABASE_URL"),
        ("batch", "WORKER_DATABASE_URL"),
    ):
        prepared = entrypoint.prepare_environment(BASE, role)
        present = {
            name
            for name in ("DATABASE_URL", "WEB_DATABASE_URL", "WORKER_DATABASE_URL")
            if name in prepared
        }
        assert present == {expected}, f"{role} set {present}"


def test_a_runtime_role_never_receives_the_schema_owner_url():
    """#492: no application process may connect as the schema owner.

    corridor.db.capability_url derives a capability URL from DATABASE_URL when
    that capability's own URL is empty, so leaking DATABASE_URL into the web or
    batch container would silently reconnect it as the owner.
    """
    for role in ("web", "batch"):
        prepared = entrypoint.prepare_environment(BASE, role)
        assert "DATABASE_URL" not in prepared


def test_each_role_connects_with_its_own_login():
    for role, login in (
        ("web", "corridor_web"),
        ("batch", "corridor_worker"),
        ("migration", "corridor_admin"),
    ):
        url = entrypoint.compose_url(BASE, role)
        assert url.startswith(f"postgresql+psycopg://{login}:"), url


def test_reserved_characters_in_a_password_cannot_redirect_the_connection():
    """A generated secret may contain @ or /; unescaped, either one moves the
    host component and points the connection somewhere else."""
    hostile = dict(BASE, CORRIDOR_WEB_DB_PASSWORD="p@ss/word:with#chars?")

    url = entrypoint.compose_url(hostile, "web")

    assert "p%40ss%2Fword%3Awith%23chars%3F" in url
    # Exactly one @ separates userinfo from host.
    assert url.count("@") == 1
    assert "@corridor.abc123.us-east-2.rds.amazonaws.com:5432/" in url


def test_tls_is_verified_not_merely_required():
    """`require` encrypts without authenticating the server, which does not
    stop an interception."""
    url = entrypoint.compose_url(BASE, "web")

    assert "sslmode=verify-full" in url
    assert f"sslrootcert={entrypoint.RDS_CA_BUNDLE}" in url


def test_lowering_tls_takes_an_explicit_variable():
    """The smoke test runs against a plain PostgreSQL container that serves no
    certificate. That escape hatch has to be explicit; a deployed task never
    sets it, which infra/tests/test_stacks.py asserts."""
    plain = dict(BASE, CORRIDOR_DB_SSLMODE="disable")

    url = entrypoint.compose_url(plain, "web")

    assert "sslmode=disable" in url
    assert "sslrootcert" not in url


def test_a_deployed_environment_refuses_to_lower_tls():
    """The stack never sets CORRIDOR_DB_SSLMODE, but the guarantee should not
    rest on the stack alone: the container refuses it too."""
    deployed = dict(
        BASE, CORRIDOR_DB_SSLMODE="disable", CORRIDOR_ENVIRONMENT="nonproduction"
    )

    with pytest.raises(entrypoint.EntrypointError, match="deployed environment"):
        entrypoint.compose_url(deployed, "web")


def test_require_is_not_accepted_as_verification():
    """`require` encrypts without authenticating the server."""
    deployed = dict(
        BASE, CORRIDOR_DB_SSLMODE="require", CORRIDOR_ENVIRONMENT="nonproduction"
    )

    with pytest.raises(entrypoint.EntrypointError, match="does not verify"):
        entrypoint.compose_url(deployed, "web")


def test_an_empty_sslmode_still_verifies():
    """An empty string must not read as 'no TLS'."""
    url = entrypoint.compose_url(dict(BASE, CORRIDOR_DB_SSLMODE=""), "web")

    assert "sslmode=verify-full" in url


def test_a_runtime_role_keeps_no_raw_password_in_its_environment():
    for role in ("web", "batch"):
        prepared = entrypoint.prepare_environment(BASE, role)
        for name in (
            "CORRIDOR_DB_ADMIN_PASSWORD",
            "CORRIDOR_WEB_DB_PASSWORD",
            "CORRIDOR_WORKER_DB_PASSWORD",
        ):
            assert name not in prepared, f"{role} still carries {name}"


def test_migration_keeps_the_two_runtime_passwords_it_must_set():
    """The baseline migration reads these to create corridor_web and
    corridor_worker; scrubbing them would make it invent predictable ones."""
    prepared = entrypoint.prepare_environment(BASE, "migration")

    assert prepared["CORRIDOR_WEB_DB_PASSWORD"] == "web-secret"
    assert prepared["CORRIDOR_WORKER_DB_PASSWORD"] == "worker-secret"
    # Its own password is already inside DATABASE_URL and is not needed raw.
    assert "CORRIDOR_DB_ADMIN_PASSWORD" not in prepared


def test_an_unknown_role_is_refused():
    with pytest.raises(entrypoint.EntrypointError, match="CORRIDOR_TASK_ROLE"):
        entrypoint.compose_url(BASE, "admin")


def test_a_missing_connection_part_is_refused_rather_than_guessed():
    for missing in ("CORRIDOR_DB_HOST", "CORRIDOR_DB_PORT", "CORRIDOR_DB_NAME"):
        env = dict(BASE)
        del env[missing]
        with pytest.raises(entrypoint.EntrypointError, match=missing):
            entrypoint.compose_url(env, "web")


def test_a_missing_password_is_refused_rather_than_defaulted():
    env = dict(BASE)
    del env["CORRIDOR_WEB_DB_PASSWORD"]
    with pytest.raises(entrypoint.EntrypointError, match="CORRIDOR_WEB_DB_PASSWORD"):
        entrypoint.compose_url(env, "web")


def test_a_preexisting_url_for_another_role_is_removed():
    """An inherited or injected DATABASE_URL must not survive into a runtime
    container: db.capability_url reads it whenever that capability's own URL is
    empty, which would reconnect web or batch as the schema owner."""
    contaminated = dict(
        BASE,
        DATABASE_URL="postgresql+psycopg://corridor:owner@evil/corridor",
        WEB_DATABASE_URL="postgresql+psycopg://stale@old/corridor",
        WORKER_DATABASE_URL="postgresql+psycopg://stale@old/corridor",
    )

    for role, expected in (
        ("web", "WEB_DATABASE_URL"),
        ("batch", "WORKER_DATABASE_URL"),
        ("migration", "DATABASE_URL"),
    ):
        prepared = entrypoint.prepare_environment(contaminated, role)
        present = {
            name
            for name in ("DATABASE_URL", "WEB_DATABASE_URL", "WORKER_DATABASE_URL")
            if name in prepared
        }
        assert present == {expected}, f"{role} ended with {present}"
        assert "evil" not in prepared[expected]
        assert "stale" not in prepared[expected]


CONTROL = {
    "CORRIDOR_ENVIRONMENT": "nonproduction",
    "CORRIDOR_CONTROL_DB_HOST": "control.abc123.us-east-2.rds.amazonaws.com",
    "CORRIDOR_CONTROL_DB_PORT": "5432",
    "CORRIDOR_CONTROL_DB_NAME": "corridor_control",
    "CORRIDOR_CONTROL_OWNER_DB_USERNAME": "corridor_control_owner",
    "CORRIDOR_CONTROL_OWNER_DB_PASSWORD": "control-owner-secret",
    "CORRIDOR_CONTROL_OPERATIONS_DB_USERNAME": "corridor_control_operator",
    "CORRIDOR_CONTROL_OPERATIONS_DB_PASSWORD": "operations-secret",
    "CORRIDOR_CONTROL_RESOLVER_DB_USERNAME": "corridor_control_runtime",
    "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD": "resolver-secret",
    "CORRIDOR_CUSTOMER_ID": "synthetic-a",
    "CORRIDOR_CUSTOMER_ENVIRONMENT_ID": "synthetic-nonproduction",
    "CORRIDOR_DEPLOYMENT_ID": "corridor-nonproduction",
    "CORRIDOR_CUSTOMER_ROUTING_KEY": "k" * 64,
}


@pytest.mark.parametrize("role,customer_url", [("web", "WEB_DATABASE_URL"), ("batch", "WORKER_DATABASE_URL")])
def test_deployed_runtime_receives_only_its_customer_login_and_control_resolver(role, customer_url):
    contaminated = {
        **BASE, **CONTROL,
        "DATABASE_URL": "owner-url",
        "CONTROL_PLANE_DATABASE_URL": "control-owner-url",
        "CONTROL_PLANE_OPERATIONS_DATABASE_URL": "operations-url",
        "CONTROL_PLANE_RESOLVER_DATABASE_URL": "stale-resolver-url",
    }

    prepared = entrypoint.prepare_environment(contaminated, role)

    assert {key for key in prepared if key.endswith("DATABASE_URL")} == {
        customer_url, "CONTROL_PLANE_RESOLVER_DATABASE_URL",
    }
    resolver = prepared["CONTROL_PLANE_RESOLVER_DATABASE_URL"]
    assert resolver.startswith("postgresql+psycopg://corridor_control_runtime:resolver-secret@control.")
    assert "sslmode=verify-full" in resolver
    assert not any(key.endswith("_PASSWORD") for key in prepared)
    assert ("CORRIDOR_CUSTOMER_ROUTING_KEY" in prepared) is (role == "web")


def test_migration_operations_mode_exposes_only_the_control_plane_operations_capability():
    prepared = entrypoint.prepare_environment(
        {**BASE, **CONTROL, "CORRIDOR_MIGRATION_MODE": "operations"}, "migration",
    )

    assert {key for key in prepared if key.endswith("DATABASE_URL")} == {
        "CONTROL_PLANE_OPERATIONS_DATABASE_URL",
    }
    assert "corridor_control_operator:operations-secret@control." in prepared["CONTROL_PLANE_OPERATIONS_DATABASE_URL"]
    assert not any(key.endswith("_PASSWORD") for key in prepared)
    assert "CORRIDOR_CUSTOMER_ROUTING_KEY" not in prepared


def test_configure_migration_has_both_owners_and_the_two_scoped_control_plane_logins():
    prepared = entrypoint.prepare_environment({**BASE, **CONTROL}, "migration")

    assert {key for key in prepared if key.endswith("DATABASE_URL")} == {
        "DATABASE_URL", "CONTROL_PLANE_DATABASE_URL",
        "CONTROL_PLANE_OPERATIONS_DATABASE_URL", "CONTROL_PLANE_RESOLVER_DATABASE_URL",
    }
    assert {key for key in prepared if key.endswith("_PASSWORD")} == {
        "CORRIDOR_WEB_DB_PASSWORD", "CORRIDOR_WORKER_DB_PASSWORD",
    }


@pytest.mark.parametrize("missing", [
    "CORRIDOR_CONTROL_DB_HOST", "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD",
    "CORRIDOR_CUSTOMER_ID", "CORRIDOR_CUSTOMER_ENVIRONMENT_ID", "CORRIDOR_DEPLOYMENT_ID",
])
def test_deployed_runtime_refuses_incomplete_control_plane_configuration(missing):
    values = {**BASE, **CONTROL}
    del values[missing]

    with pytest.raises(entrypoint.EntrypointError, match=missing):
        entrypoint.prepare_environment(values, "web")


@pytest.mark.parametrize("refused", ["-leading-hyphen", "has space", "a" * 129, "caf\u00e9"])
def test_a_malformed_deployment_identifier_is_refused_at_start_up(refused):
    """The rule this applies is `corridor.control_plane.identifier`'s.

    This script cannot import it, so it keeps a copy that
    `tests/test_vocabulary_owners.py` asserts equal to the owner's. That
    equality is only worth having while the copy is actually applied, which is
    what these refusals prove.
    """
    values = {**BASE, **CONTROL, "CORRIDOR_CUSTOMER_ID": refused}

    with pytest.raises(entrypoint.EntrypointError, match="stable bounded identifier"):
        entrypoint.prepare_environment(values, "web")


def test_only_web_can_keep_a_sufficient_customer_signing_key():
    with pytest.raises(entrypoint.EntrypointError, match="at least 32 bytes"):
        entrypoint.prepare_environment({**BASE, **CONTROL, "CORRIDOR_CUSTOMER_ROUTING_KEY": "short"}, "web")
    with pytest.raises(entrypoint.EntrypointError, match="only the migration task"):
        entrypoint.prepare_environment({**BASE, **CONTROL, "CORRIDOR_MIGRATION_MODE": "operations"}, "web")


def test_control_plane_urls_encode_passwords_and_cannot_lower_deployed_tls():
    values = {**BASE, **CONTROL, "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD": "p@ss/word?#"}
    url = entrypoint.compose_control_url(values, "resolver")
    assert "p%40ss%2Fword%3F%23@control." in url
    assert url.count("@") == 1
    with pytest.raises(entrypoint.EntrypointError, match="does not verify"):
        entrypoint.compose_control_url({**values, "CORRIDOR_DB_SSLMODE": "require"}, "resolver")
