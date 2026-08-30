"""Fresh and predecessor-to-head rehearsal for ADR-0061 chronology history."""

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
PREDECESSOR = "b4d1e2f3a5c6"
HEAD = "e1f2a3b4c5d6"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_dispute_history_schema_is_one_head_on_a_fresh_database():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue346_fresh_",
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert connection.scalar(
                    text("select to_regclass('dispute_history_resolutions')")
                ) == "dispute_history_resolutions"
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger where tgname = "
                        "'dispute_history_resolutions_are_immutable')"
                    )
                )
        finally:
            engine.dispose()


def test_predecessor_upgrade_adds_append_only_history_without_rewriting_claims():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue346_predecessor_",
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
                        "values ('issue346-predecessor', 'Issue 346 predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies (project_id, ref_code, dep_type, title) "
                        "select id, 'DEP-346', 'utility_relocation', 'Legacy pipe' "
                        "from projects where slug = 'issue346-predecessor'"
                    )
                )
                assert connection.scalar(
                    text("select to_regclass('dispute_history_resolutions')")
                ) is None

            _upgrade(rendered, "head")

            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert connection.scalar(
                    text("select count(*) from dependencies where ref_code = 'DEP-346'")
                ) == 1
                assert connection.scalar(
                    text("select count(*) from dispute_history_resolutions")
                ) == 0
        finally:
            engine.dispose()
