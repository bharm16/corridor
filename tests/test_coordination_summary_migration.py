"""Rehearse retained Coordination Summary receipts on real disposable PostgreSQL."""

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
PREDECESSOR = "b4d1e2f3a5c6"
HEAD = "c355a7d9e2f1"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_predecessor_to_head_preserves_project_rows_and_adds_immutable_receipts():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue355_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        url = make_url(settings.database_url).set(database=database.name)
        rendered = url.render_as_string(hide_password=False)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) values ('issue355', 'Issue 355', true)"
                    )
                )
            _upgrade(rendered, "head")
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                assert (
                    connection.scalar(
                        text(
                            "select to_regclass('coordination_summary_configurations')"
                        )
                    )
                    is not None
                )
                assert (
                    connection.scalar(
                        text("select to_regclass('coordination_summary_requests')")
                    )
                    is not None
                )
                assert (
                    connection.scalar(
                        text("select count(*) from projects where slug = 'issue355'")
                    )
                    == 1
                )
                assert (
                    connection.scalar(
                        text("select count(*) from coordination_summary_requests")
                    )
                    == 0
                )
                assert (
                    connection.scalar(
                        text(
                            "select exists (select 1 from pg_trigger where tgname = 'coordination_summary_requests_are_immutable')"
                        )
                    )
                    is True
                )
        finally:
            engine.dispose()
