"""Rehearse the append-only check-configuration schema on PostgreSQL.

Fresh and predecessor-to-head, with a legacy report run whose snapshot never
recorded its thresholds preserved unchanged and left explicitly unknown.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "a364b7c9e2f1"
HEAD = "f1c0d17e0a2b"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_predecessor_upgrade_adds_the_table_and_preserves_legacy_report_runs():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue339_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            # A legacy report run whose snapshot never recorded its thresholds.
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue339-predecessor', 'Issue 339 predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into report_runs "
                        "(project_id, ruleset_version, snapshot_json, output_path, "
                        "document_only) select id, 'v0.4', "
                        "'{\"dependencies\": {}}'::jsonb, 'out/legacy.html', false "
                        "from projects where slug = 'issue339-predecessor'"
                    )
                )
                # The table does not exist before the upgrade.
                assert (
                    connection.scalar(
                        text("select to_regclass('project_check_configurations')")
                    )
                    is None
                )

            _upgrade(rendered, "head")

            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("select version_num from alembic_version")
                    )
                    == HEAD
                )
                # The new table exists and starts empty.
                assert (
                    connection.scalar(
                        text("select to_regclass('project_check_configurations')")
                    )
                    is not None
                )
                assert (
                    connection.scalar(
                        text("select count(*) from project_check_configurations")
                    )
                    == 0
                )
                # The append-only trigger is in place.
                assert (
                    connection.scalar(
                        text(
                            "select exists (select 1 from pg_trigger where tgname = "
                            "'project_check_configurations_are_immutable')"
                        )
                    )
                    is True
                )
                # The legacy report run is preserved unchanged, still without a
                # thresholds key — its threshold identity stays unknown.
                snapshot = connection.scalar(
                    text(
                        "select snapshot_json from report_runs where project_id = "
                        "(select id from projects where slug = 'issue339-predecessor')"
                    )
                )
                assert snapshot == {"dependencies": {}}
                assert "thresholds" not in snapshot

            # Append works; update and delete are refused by the trigger.
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into project_check_configurations "
                        "(project_id, ruleset_version, stale_days, due_soon_days, "
                        "action_due_soon_days, created_by) select id, 'v0.4', 10, 20, "
                        "5, 'local:migration-rehearsal' from projects "
                        "where slug = 'issue339-predecessor'"
                    )
                )
            for statement in (
                "update project_check_configurations set stale_days = 99",
                "delete from project_check_configurations",
            ):
                with pytest.raises(Exception):
                    with engine.begin() as connection:
                        connection.execute(text(statement))
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text(
                            "select stale_days from project_check_configurations"
                        )
                    )
                    == 10
                )
        finally:
            engine.dispose()


def test_downgrade_refuses_to_erase_declared_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue339_downgrade_",
        migration_revision=HEAD,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue339-downgrade', 'Issue 339 downgrade', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into project_check_configurations "
                        "(project_id, ruleset_version, stale_days, due_soon_days, "
                        "action_due_soon_days, created_by) select id, 'v0.4', 10, 20, "
                        "5, 'local:migration-rehearsal' from projects "
                        "where slug = 'issue339-downgrade'"
                    )
                )
            refused = subprocess.run(
                ["uv", "run", "alembic", "downgrade", PREDECESSOR],
                cwd=ROOT,
                env={**os.environ, "DATABASE_URL": rendered},
                capture_output=True,
                text=True,
            )
            assert refused.returncode != 0
            assert "cannot erase declared check configuration history" in (
                refused.stdout + refused.stderr
            )
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("select version_num from alembic_version")
                    )
                    == HEAD
                )
        finally:
            engine.dispose()
