"""Fresh and predecessor rehearsal for the inbound-email storage boundary."""

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
HEAD = "e7a2f4c9d1b6"


def _upgrade(url: str, target: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_predecessor_upgrade_adds_email_intake_without_rewriting_existing_projects():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue372_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        url = make_url(settings.database_url).set(database=database.name)
        rendered = url.render_as_string(hide_password=False)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(text("insert into projects (slug, name, is_synthetic) values ('mail-legacy', 'Mail legacy', true)"))
            _upgrade(rendered, "head")
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert connection.scalar(text("select to_regclass('inbound_messages')")) == "inbound_messages"
                assert connection.scalar(text("select to_regclass('inbound_threads')")) == "inbound_threads"
                assert connection.scalar(text("select to_regclass('inbound_thread_readings')")) == "inbound_thread_readings"
                assert connection.scalar(text("select count(*) from projects where slug = 'mail-legacy'")) == 1
        finally:
            engine.dispose()
