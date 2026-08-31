"""Permanent database baseline and supported-upgrade contract."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.product_proving_database import fingerprint_database_url


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "src" / "corridor" / "migrations" / "baseline_versions"
RELEASED_HEAD = "b7d3f9a1c2e5"
BASELINE_HEAD = "c0a1d0b5e11e"
EXPECTED_SCHEMA_SHA256 = (
    "72c7ffac9606259affaf705075b6daa53c4279724a48811e926cc7076e7f6703"
)

pytestmark = [pytest.mark.slow, pytest.mark.migration]


def test_migration_inventory_is_one_schema_builder_and_one_baseline_marker():
    assert {path.name for path in VERSIONS.glob("*.py")} == {
        f"{RELEASED_HEAD}_current_schema_baseline.py",
        f"{BASELINE_HEAD}_establish_current_baseline.py",
    }


def test_fresh_database_matches_the_released_schema_exactly():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_fresh_",
    ) as database:
        database_url = configured.set(database=database.name).render_as_string(
            hide_password=False
        )
        fingerprint = fingerprint_database_url(database_url)
        security = _statement_retirement_security(database.session_factory)

    assert database.migration_head == BASELINE_HEAD
    assert fingerprint.schema_sha256 == EXPECTED_SCHEMA_SHA256
    assert security == {
        "can_login": False,
        "inherits": False,
        "function_owner": "corridor_statement_retirement",
        "function_public_execute": False,
        "tables": {
            "commitment_lineages": ("DELETE", "SELECT"),
            "dependency_event_evidence": ("DELETE", "SELECT"),
            "dependency_event_scope_decisions": ("DELETE", "SELECT"),
            "dependency_event_scopes": ("DELETE", "SELECT"),
            "dependency_event_timings": ("DELETE", "SELECT"),
            "dependency_events": ("DELETE", "SELECT"),
            "evidence_links": ("DELETE", "SELECT"),
            "legacy_ledger_archives": ("DELETE", "SELECT"),
            "projects": ("DELETE", "SELECT"),
            "work_decisions": ("SELECT",),
        },
    }


def test_released_head_advances_without_changing_project_record_rows():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_bridge_",
        migration_revision=RELEASED_HEAD,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            session.execute(
                text(
                    "insert into projects (slug, name, is_synthetic) "
                    "values ('baseline-bridge', 'Baseline Bridge', true)"
                )
            )
        before = _project_row(database.session_factory)

        completed = _alembic(database_url, "upgrade", "head")

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert _migration_head(database.session_factory) == BASELINE_HEAD
        assert _project_row(database.session_factory) == before


def test_downgrade_across_the_baseline_is_unsupported():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_downgrade_",
    ) as database:
        database_url = configured.set(database=database.name)
        completed = _alembic(database_url, "downgrade", RELEASED_HEAD)

    assert completed.returncode != 0
    assert "baseline downgrade is unsupported" in completed.stderr


def _project_row(session_factory):
    with session_factory() as session:
        return session.execute(
            text(
                "select slug, name, is_synthetic from projects "
                "where slug = 'baseline-bridge'"
            )
        ).one()


def _migration_head(session_factory) -> str:
    with session_factory() as session:
        return str(session.scalar(text("select version_num from alembic_version")))


def _statement_retirement_security(session_factory) -> dict:
    with session_factory() as session:
        can_login, inherits = session.execute(
            text(
                "select rolcanlogin, rolinherit from pg_roles "
                "where rolname = 'corridor_statement_retirement'"
            )
        ).one()
        function_owner = session.scalar(
            text(
                "select owner.rolname from pg_proc function "
                "join pg_namespace namespace on namespace.oid = function.pronamespace "
                "join pg_roles owner on owner.oid = function.proowner "
                "where namespace.nspname = 'public' "
                "and function.proname = 'purge_external_party_statement_rows'"
            )
        )
        public_execute = session.scalar(
            text(
                "select has_function_privilege("
                "'public', "
                "'public.purge_external_party_statement_rows(bigint,text)', "
                "'execute')"
            )
        )
        rows = session.execute(
            text(
                "select table_name, privilege_type "
                "from information_schema.role_table_grants "
                "where table_schema = 'public' "
                "and grantee = 'corridor_statement_retirement' "
                "order by table_name, privilege_type"
            )
        ).all()
    tables: dict[str, list[str]] = {}
    for table_name, privilege in rows:
        tables.setdefault(str(table_name), []).append(str(privilege))
    return {
        "can_login": bool(can_login),
        "inherits": bool(inherits),
        "function_owner": str(function_owner),
        "function_public_execute": bool(public_execute),
        "tables": {name: tuple(values) for name, values in tables.items()},
    }


def _alembic(database_url, command: str, target: str):
    environment = {
        **os.environ,
        "DATABASE_URL": database_url.render_as_string(hide_password=False),
    }
    return subprocess.run(
        ["uv", "run", "alembic", command, target],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
