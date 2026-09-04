"""The container entrypoint's URL composition and credential scrubbing.

These run without Docker on purpose. The image build is slow and belongs in CI;
the part that can silently connect the wrong role to the database, or leak a
password into a subprocess, is pure logic and deserves fast, direct tests.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "container_entrypoint",
    pathlib.Path(__file__).parents[1] / "scripts" / "container_entrypoint.py",
)
entrypoint = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(entrypoint)


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
