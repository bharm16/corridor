"""Rehearse the immutable External Report release schema on PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.report_release import external_report_release_history

pytestmark = pytest.mark.slow


ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "f253a7c4d9e2"
RELEASE_PREDECESSOR = "e255a7c4d9e2"
HEAD = "b5d1e2f3a5c6"
RELEASE_COLUMNS = [
    "id",
    "project_id",
    "artifact_name",
    "format",
    "pdf_bytes",
    "pdf_sha256",
    "evaluated_on",
    "ruleset_version",
    "provenance_mode",
    "record_context_json",
    "released_by",
    "released_at",
    "evaluation_context_json",
    "artifact_id",
    "released_by_display",
]
ARTIFACT_COLUMNS = [
    "id",
    "project_id",
    "artifact_name",
    "format",
    "pdf_bytes",
    "pdf_sha256",
    "evaluated_on",
    "ruleset_version",
    "evaluation_context_json",
    "provenance_mode",
    "record_context_json",
    "rendered_at",
]


def _upgrade(database_url: str, target: str) -> None:
    completed = _upgrade_result(database_url, target)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _upgrade_result(database_url: str, target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def _release_columns(connection) -> list[str]:
    return connection.execute(
        text(
            "select column_name from information_schema.columns "
            "where table_schema = 'public' "
            "and table_name = 'external_report_releases' "
            "order by ordinal_position"
        )
    ).scalars().all()


def _artifact_columns(connection) -> list[str]:
    return connection.execute(
        text(
            "select column_name from information_schema.columns "
            "where table_schema = 'public' "
            "and table_name = 'external_report_artifacts' "
            "order by ordinal_position"
        )
    ).scalars().all()


def test_release_schema_is_one_linear_head_on_a_fresh_database():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue255_fresh_",
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert _release_columns(connection) == RELEASE_COLUMNS
                assert _artifact_columns(connection) == ARTIFACT_COLUMNS
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger "
                        "where tgname = 'prevent_external_report_release_mutation')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger "
                        "where tgname = 'external_report_releases_reject_truncate')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger "
                        "where tgname = 'external_report_artifacts_are_immutable')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger "
                        "where tgname = 'external_report_artifacts_reject_truncate')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_constraint "
                        "where conname = 'uq_external_report_releases_artifact_id')"
                    )
                ) is True
                assert connection.execute(
                    text(
                        "select conname from pg_constraint "
                        "where conrelid = 'external_report_releases'::regclass "
                        "and conname like 'ck_external_report_releases_%' "
                        "order by conname"
                    )
                ).scalars().all() == [
                    "ck_external_report_releases_artifact_name",
                    "ck_external_report_releases_context_object",
                    "ck_external_report_releases_evaluation_object",
                    "ck_external_report_releases_nonempty_pdf",
                    "ck_external_report_releases_pdf_only",
                    "ck_external_report_releases_pdf_sha256",
                    "ck_external_report_releases_provenance_mode",
                    "ck_external_report_releases_released_by",
                    "ck_external_report_releases_released_by_display",
                ]
        finally:
            engine.dispose()


def test_release_successors_upgrade_the_immediate_predecessor_without_rewriting_report_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue255_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue255-predecessor', 'Issue 255 predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into report_runs "
                        "(project_id, ruleset_version, snapshot_json, output_path, document_only) "
                        "select id, 'v0.4', '{\"dependencies\":{}}'::jsonb, "
                        "'out/report.html', false from projects "
                        "where slug = 'issue255-predecessor'"
                    )
                )

            _upgrade(rendered, "head")

            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert _release_columns(connection) == RELEASE_COLUMNS
                assert connection.execute(
                    text(
                        "select ruleset_version, snapshot_json, output_path, document_only "
                        "from report_runs where project_id = "
                        "(select id from projects where slug = 'issue255-predecessor')"
                    )
                ).one() == (
                    "v0.4",
                    {"dependencies": {}},
                    "out/report.html",
                    False,
                )
                assert connection.scalar(
                    text("select count(*) from external_report_releases")
                ) == 0
        finally:
            engine.dispose()


def test_release_actor_successor_preserves_an_existing_receipt_as_legacy():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue255_existing_release_",
        migration_revision=RELEASE_PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue255-existing-release', "
                        "'Issue 255 existing release', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into external_report_releases "
                        "(project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
                        "evaluated_on, ruleset_version, provenance_mode, "
                        "record_context_json, released_by) "
                        "select id, 'before-a255.pdf', 'pdf', "
                        "decode('255044462d312e370a7072696f720a2525454f46', 'hex'), "
                        "'07b7396f531418f26a52721f1250e82081e2bbb82b76a129e10e80a1b2288790', "
                        "date '2026-08-13', 'v0.4', 'all-supported-sources', "
                        "'{\"dependencies\":[],\"party_statements\":[]}'::jsonb, "
                        "'local:predecessor' from projects "
                        "where slug = 'issue255-existing-release'"
                    )
                )
                connection.execute(
                    text(
                        "insert into project_roster_entries "
                        "(project_id, principal_subject, display_name) "
                        "select id, 'local:predecessor', 'Current Roster Name' "
                        "from projects where slug = 'issue255-existing-release'"
                    )
                )

            _upgrade(rendered, "head")

            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert connection.execute(
                    text(
                        "select artifact_name, pdf_sha256, evaluation_context_json, "
                        "artifact_id, released_by_display "
                        "from external_report_releases"
                    )
                ).one() == (
                    "before-a255.pdf",
                    "07b7396f531418f26a52721f1250e82081e2bbb82b76a129e10e80a1b2288790",
                    None,
                    None,
                    None,
                )

            with Session(bind=engine) as session:
                project_id = session.scalar(
                    text(
                        "select id from projects "
                        "where slug = 'issue255-existing-release'"
                    )
                )
                [history] = external_report_release_history(session, project_id)
                assert history.released_by_display == (
                    "Project person (display name not retained in this legacy release)"
                )
                assert history.released_by == "local:predecessor"

            with pytest.raises(
                IntegrityError,
                match="ck_external_report_releases_released_by_display",
            ):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into external_report_releases "
                            "(project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
                            "evaluated_on, ruleset_version, provenance_mode, "
                            "record_context_json, released_by) "
                            "select id, 'after-f255-without-display.pdf', 'pdf', "
                            "decode('255044462d312e370a7072696f720a2525454f46', 'hex'), "
                            "'07b7396f531418f26a52721f1250e82081e2bbb82b76a129e10e80a1b2288790', "
                            "date '2026-08-14', 'v0.4', 'all-supported-sources', "
                            "'{\"dependencies\":[],\"party_statements\":[]}'::jsonb, "
                            "'local:new-release' from projects "
                            "where slug = 'issue255-existing-release'"
                        )
                    )

            with engine.connect() as connection:
                assert connection.scalar(
                    text("select count(*) from external_report_releases")
                ) == 1
                assert connection.scalar(
                    text(
                        "select convalidated from pg_constraint "
                        "where conname = "
                        "'ck_external_report_releases_released_by_display'"
                    )
                ) is False
        finally:
            engine.dispose()


def test_idempotence_migration_refuses_duplicate_d255_release_history_atomically():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue255_duplicate_d255_",
        migration_revision="d255a7c4d9e2",
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue255-duplicate-d255', 'Issue 255 duplicate d255', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into external_report_artifacts "
                        "(project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
                        "evaluated_on, ruleset_version, evaluation_context_json, "
                        "provenance_mode, record_context_json) "
                        "select id, 'd255.pdf', 'pdf', "
                        "decode('255044462d312e370a7072696f720a2525454f46', 'hex'), "
                        "'07b7396f531418f26a52721f1250e82081e2bbb82b76a129e10e80a1b2288790', "
                        "date '2026-08-13', 'v0.4', '{\"thresholds\":{}}'::jsonb, "
                        "'all-supported-sources', "
                        "'{\"dependencies\":[],\"party_statements\":[]}'::jsonb "
                        "from projects where slug = 'issue255-duplicate-d255'"
                    )
                )
                connection.execute(
                    text(
                        "insert into external_report_releases "
                        "(project_id, artifact_id, artifact_name, format, pdf_bytes, "
                        "pdf_sha256, evaluated_on, ruleset_version, evaluation_context_json, "
                        "provenance_mode, record_context_json, released_by) "
                        "select project_id, id, artifact_name, format, pdf_bytes, "
                        "pdf_sha256, evaluated_on, ruleset_version, evaluation_context_json, "
                        "provenance_mode, record_context_json, 'local:release-retry' "
                        "from external_report_artifacts "
                        "where artifact_name = 'd255.pdf'"
                    )
                )
                connection.execute(
                    text(
                        "insert into external_report_releases "
                        "(project_id, artifact_id, artifact_name, format, pdf_bytes, "
                        "pdf_sha256, evaluated_on, ruleset_version, evaluation_context_json, "
                        "provenance_mode, record_context_json, released_by) "
                        "select project_id, id, artifact_name, format, pdf_bytes, "
                        "pdf_sha256, evaluated_on, ruleset_version, evaluation_context_json, "
                        "provenance_mode, record_context_json, 'local:release-retry' "
                        "from external_report_artifacts "
                        "where artifact_name = 'd255.pdf'"
                    )
                )

            refused = _upgrade_result(rendered, "head")

            assert refused.returncode != 0
            assert "has 2 immutable release receipts" in (
                refused.stdout + refused.stderr
            )
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == (
                    "d255a7c4d9e2"
                )
                assert connection.scalar(
                    text("select count(*) from external_report_releases")
                ) == 2
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_constraint "
                        "where conname = 'uq_external_report_releases_artifact_id')"
                    )
                ) is False
        finally:
            engine.dispose()
