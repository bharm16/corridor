"""Rehearse the unreadable-cell reading schema on real disposable PostgreSQL."""

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


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "d3f1a9c05b21"
HEAD = "e4c8b1a6d3f7"

_TABLES = (
    "unreadable_cell_reading_profiles",
    "unreadable_cell_reading_runs",
    "unreadable_cell_reading_steps",
    "unreadable_cell_resolutions",
    "unreadable_cell_admission_activations",
)


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_unreadable_cell_schema_is_one_linear_head_on_a_fresh_database():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue369_fresh_",
    ) as database:
        url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                for table in _TABLES:
                    assert (
                        connection.scalar(
                            text("select to_regclass(:t)"), {"t": table}
                        )
                        is not None
                    )
                for table in _TABLES:
                    for suffix in ("are_immutable", "reject_truncate"):
                        assert connection.scalar(
                            text(
                                "select exists (select 1 from pg_trigger "
                                "where tgname = :name)"
                            ),
                            {"name": f"{table}_{suffix}"},
                        )
        finally:
            engine.dispose()


def test_predecessor_to_head_preserves_projects_and_adds_tables():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue369_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        url = make_url(settings.database_url).set(database=database.name)
        rendered = url.render_as_string(hide_password=False)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue369', 'Issue 369', true)"
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
                            "select to_regclass('unreadable_cell_resolutions')"
                        )
                    )
                    is not None
                )
                assert (
                    connection.scalar(
                        text("select count(*) from projects where slug = 'issue369'")
                    )
                    == 1
                )
                assert (
                    connection.scalar(
                        text("select count(*) from unreadable_cell_reading_runs")
                    )
                    == 0
                )
        finally:
            engine.dispose()
