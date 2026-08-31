"""Rehearse retained Key date draft receipts on real disposable PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = [pytest.mark.slow, pytest.mark.migration]

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "b4d1e2f3a5c6"
HEAD = "b7d3f9a1c2e5"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_predecessor_to_head_preserves_project_rows_and_adds_append_only_receipts():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue363_predecessor_",
        reuse_migrated_template=True,
        migration_revision=PREDECESSOR,
    ) as database:
        url = make_url(settings.database_url).set(database=database.name)
        rendered = url.render_as_string(hide_password=False)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) values ('issue363', 'Issue 363', true)"
                    )
                )
            _upgrade(rendered, "head")
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                for table in (
                    "key_date_draft_receipts",
                    "key_date_draft_row_receipts",
                ):
                    assert (
                        connection.scalar(
                            text("select to_regclass(:table)").bindparams(table=table)
                        )
                        is not None
                    )
                    assert (
                        connection.scalar(
                            text(
                                "select exists (select 1 from pg_trigger "
                                "where tgname = :trigger)"
                            ).bindparams(trigger=f"{table}_append_only")
                        )
                        is True
                    )
                assert (
                    connection.scalar(
                        text("select count(*) from projects where slug = 'issue363'")
                    )
                    == 1
                )
                assert (
                    connection.scalar(
                        text("select count(*) from key_date_draft_receipts")
                    )
                    == 0
                )
        finally:
            engine.dispose()
