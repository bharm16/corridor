"""Rehearse the scheduled-report-publication schema on real PostgreSQL.

The retained weekly reading is a new table; these rehearsals prove a fresh
database reaches the one linear head with the table's append-only guards in
place, that upgrading the immediate predecessor preserves prior report history
and invents no publication rows, and that a downgrade refuses to erase a
retained reading.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres

pytestmark = [pytest.mark.slow, pytest.mark.migration]


ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "f360a1b2c3d4"
HEAD = "b7d3f9a1c2e5"
PUBLICATION_COLUMNS = [
    "id",
    "public_id",
    "occurrence_id",
    "schedule_id",
    "project_id",
    "configuration_version",
    "provenance_mode",
    "predecessor_release_id",
    "prepared_artifact_id",
    "evaluated_on",
    "window_start",
    "comparison_window_days",
    "ruleset_version",
    "thresholds_json",
    "snapshot_json",
    "observed_at",
    "created_at",
]


def _upgrade_result(database_url: str, target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def _downgrade_result(database_url: str, target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "alembic", "downgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def _columns(connection, table: str) -> list[str]:
    return (
        connection.execute(
            text(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' and table_name = :table "
                "order by ordinal_position"
            ),
            {"table": table},
        )
        .scalars()
        .all()
    )


def test_publication_schema_is_one_linear_head_on_a_fresh_database():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue354_fresh_",
        reuse_migrated_template=True,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                assert (
                    _columns(connection, "scheduled_report_publications")
                    == PUBLICATION_COLUMNS
                )
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger where tgname = "
                        "'scheduled_report_publications_are_immutable')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger where tgname = "
                        "'scheduled_report_publications_reject_truncate')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_constraint where conname = "
                        "'uq_scheduled_report_publication_occurrence')"
                    )
                ) is True
                assert connection.execute(
                    text(
                        "select conname from pg_constraint "
                        "where conrelid = 'scheduled_report_publications'::regclass "
                        "and conname like 'ck_scheduled_report_publication_%' "
                        "order by conname"
                    )
                ).scalars().all() == [
                    "ck_scheduled_report_publication_predecessor_window",
                    "ck_scheduled_report_publication_provenance_mode",
                    "ck_scheduled_report_publication_snapshot_object",
                    "ck_scheduled_report_publication_thresholds_object",
                    "ck_scheduled_report_publication_window_days",
                    "ck_scheduled_report_publication_window_nonneg",
                ]
        finally:
            engine.dispose()


def test_predecessor_upgrade_preserves_report_history_and_invents_no_publications():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue354_predecessor_",
        reuse_migrated_template=True,
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
                        "values ('issue354-predecessor', 'Issue 354 predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into report_runs "
                        "(project_id, ruleset_version, snapshot_json, output_path, "
                        "document_only) select id, 'v0.4', "
                        "'{\"dependencies\":{}}'::jsonb, 'out/report.html', false "
                        "from projects where slug = 'issue354-predecessor'"
                    )
                )

            upgraded = _upgrade_result(rendered, "head")
            assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                # Prior report history is untouched and no publication is invented.
                assert connection.execute(
                    text(
                        "select ruleset_version, snapshot_json, output_path, "
                        "document_only from report_runs where project_id = "
                        "(select id from projects where slug='issue354-predecessor')"
                    )
                ).one() == ("v0.4", {"dependencies": {}}, "out/report.html", False)
                assert (
                    connection.scalar(
                        text("select count(*) from scheduled_report_publications")
                    )
                    == 0
                )
        finally:
            engine.dispose()


def test_downgrade_refuses_to_erase_a_retained_reading():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue354_downgrade_",
        reuse_migrated_template=True,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue354-downgrade', 'Issue 354 downgrade', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into due_work_schedules "
                        "(public_id, project_id, handler_key, configuration_version, "
                        "scope_json, configuration_json, configuration_sha256, "
                        "input_identity_sha256, starts_at, cadence, timezone_name, "
                        "missed_run_policy, retention_days, max_attempts, "
                        "backoff_seconds, claim_ttl_seconds, deadline_seconds, "
                        "concurrency_limit, model_token_budget, notification_budget, "
                        "enabled_at) select 'due-job:downgrade', id, "
                        "'report_publication', 'report-publication-v1', "
                        "'{}'::jsonb, '{}'::jsonb, "
                        "'" + ("a" * 64) + "', '" + ("b" * 64) + "', now(), 'weekly', "
                        "'UTC', 'latest_only', 3650, 3, 120, 1800, 1800, 1, 0, 0, now() "
                        "from projects where slug='issue354-downgrade'"
                    )
                )
                connection.execute(
                    text(
                        "insert into due_work_occurrences "
                        "(public_id, scheduled_job_id, occurrence_key, due_at, state, "
                        "attempt_count) select 'due-occurrence:downgrade', id, "
                        "'" + ("c" * 64) + "', now(), 'completed', 1 "
                        "from due_work_schedules where public_id='due-job:downgrade'"
                    )
                )
                connection.execute(
                    text(
                        "insert into scheduled_report_publications "
                        "(public_id, occurrence_id, schedule_id, project_id, "
                        "configuration_version, provenance_mode, evaluated_on, "
                        "ruleset_version, thresholds_json, snapshot_json, observed_at) "
                        "select 'report-pub:downgrade', o.id, o.scheduled_job_id, "
                        "s.project_id, 'report-publication-v1', "
                        "'all-supported-sources', date '2026-08-31', 'v0.4', "
                        "'{}'::jsonb, '{\"dependencies\":{}}'::jsonb, now() "
                        "from due_work_occurrences o join due_work_schedules s "
                        "on s.id = o.scheduled_job_id "
                        "where o.public_id='due-occurrence:downgrade'"
                    )
                )

            refused = _downgrade_result(rendered, PREDECESSOR)
            assert refused.returncode != 0
            assert "cannot erase retained scheduled_report_publications" in (
                refused.stdout + refused.stderr
            )
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                assert (
                    connection.scalar(
                        text("select count(*) from scheduled_report_publications")
                    )
                    == 1
                )
        finally:
            engine.dispose()
