"""Permanent database baseline and supported-upgrade contract."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.product_proving_database import fingerprint_database_url


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "src" / "corridor" / "migrations" / "baseline_versions"
SCHEMA_BUILDER = "b7d3f9a1c2e5"
PREDECESSOR_HEAD = "c0a1d0b5e11e"
CURRENT_HEAD = "0ca809014df7"
EXPECTED_SCHEMA_SHA256 = (
    "b1079aec0f827a78dd3e41b84549f58b4d134f75488fa0d7eec6d1cd5fb3409b"
)

pytestmark = [pytest.mark.slow, pytest.mark.migration]


def test_migration_inventory_is_one_builder_marker_and_linear_successor():
    assert {path.name for path in VERSIONS.glob("*.py")} == {
        f"{SCHEMA_BUILDER}_current_schema_baseline.py",
        f"{PREDECESSOR_HEAD}_establish_current_baseline.py",
        f"{CURRENT_HEAD}_add_spreadsheet_source_segments.py",
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

    assert database.migration_head == CURRENT_HEAD
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


def test_supported_predecessor_adds_empty_spine_without_changing_record_rows():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_bridge_",
        migration_revision=PREDECESSOR_HEAD,
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
        assert _migration_head(database.session_factory) == CURRENT_HEAD
        assert _project_row(database.session_factory) == before
        assert _source_segment_rows(database.session_factory) == []


def test_supported_predecessor_creates_strict_append_only_source_segments():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_segment_bridge_",
        migration_revision=PREDECESSOR_HEAD,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            project_id = session.scalar(
                text(
                    "insert into projects (slug, name, is_synthetic) "
                    "values ('segment-bridge', 'Segment Bridge', true) "
                    "returning id"
                )
            )
            document_id = session.scalar(
                text(
                    "insert into documents "
                    "(project_id, sha256, filename, doc_type, numbering_scheme, "
                    "pages, parse_status) values "
                    "(:project_id, :sha256, 'matrix.xlsx', 'matrix', "
                    "'project-unique', 1, 'parsed') returning id"
                ),
                {"project_id": project_id, "sha256": "a" * 64},
            )

        completed = _alembic(database_url, "upgrade", "head")
        assert completed.returncode == 0, completed.stdout + completed.stderr

        with database.session_factory.begin() as session:
            segment_id = session.scalar(
                text(
                    "insert into source_segments "
                    "(project_id, document_id, kind, exact_text, content_sha256, "
                    "ordinal, sheet_name, cell_range) values "
                    "(:project_id, :document_id, 'spreadsheet_cell', 'UC-1', "
                    ":digest, 1, 'Conflicts', 'A2') returning id"
                ),
                {
                    "project_id": project_id,
                    "document_id": document_id,
                    "digest": "1c4fc7e2bdaf4b219c00ce662b927dc5d7e17091e467df61b9582d7fb359a39e",
                },
            )

        assert _source_segment_rows(database.session_factory) == [
            (segment_id, project_id, document_id, "spreadsheet_cell", "UC-1", "A2")
        ]
        with pytest.raises(DBAPIError, match="source segments are append-only"):
            with database.session_factory.begin() as session:
                session.execute(
                    text(
                        "update source_segments set exact_text = 'rewritten' "
                        "where id = :segment_id"
                    ),
                    {"segment_id": segment_id},
                )
        assert _source_segment_rows(database.session_factory) == [
            (segment_id, project_id, document_id, "spreadsheet_cell", "UC-1", "A2")
        ]


def test_downgrade_that_would_delete_source_segments_is_unsupported():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_downgrade_",
    ) as database:
        database_url = configured.set(database=database.name)
        completed = _alembic(database_url, "downgrade", PREDECESSOR_HEAD)

    assert completed.returncode != 0
    assert "source segment migration downgrade is unsupported" in completed.stderr


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


def _source_segment_rows(session_factory) -> list[tuple]:
    with session_factory() as session:
        return list(
            session.execute(
                text(
                    "select id, project_id, document_id, kind, exact_text, cell_range "
                    "from source_segments order by id"
                )
            ).all()
        )


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
