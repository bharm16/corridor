"""Fresh and predecessor rehearsals for the supervised Due Work runtime.

Trusting a stamped development database was rejected because missing triggers
or dropped receipt constraints can still report the expected Alembic head. The
tests prove fresh schema objects and populated predecessor preservation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.due_work import ProcessingHealthDeclaration, configure_processing_health
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import DueWorkSchedule


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "c318d6e8f0a3"
HEAD = "f367a8c1d2e4"


def _run_alembic(database_url: str, *args: str):
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def _database_url(database) -> str:
    return make_url(settings.database_url).set(database=database.name).render_as_string(
        hide_password=False
    )


def test_due_work_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="due_work_fresh_",
    ) as database:
        engine = create_engine(_database_url(database))
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                tables = set(
                    connection.scalars(
                        text(
                            "select table_name from information_schema.tables "
                            "where table_schema='public' and table_name like 'due_work_%'"
                        )
                    ).all()
                )
                assert tables == {
                    "due_work_schedules",
                    "due_work_occurrences",
                    "due_work_receipts",
                }
                triggers = set(
                    connection.scalars(
                        text(
                            "select tgname from pg_trigger where not tgisinternal "
                            "and tgname like 'due_work_%'"
                        )
                    ).all()
                )
                assert {
                    "due_work_schedules_are_immutable",
                    "due_work_occurrence_identity_is_immutable",
                    "due_work_receipts_are_immutable",
                }.issubset(triggers)
        finally:
            engine.dispose()


def test_predecessor_upgrade_preserves_domain_rows_and_protects_runtime_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="due_work_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _database_url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('due-work-predecessor', 'Due Work predecessor', true) "
                        "returning id"
                    )
                )
                document_id = connection.scalar(
                    text(
                        "insert into documents "
                        "(project_id, registry_id, sha256, filename, doc_type, parse_status) "
                        "values (:project_id, 'predecessor-doc', :sha, 'predecessor.pdf', "
                        "'matrix', 'parsed') returning id"
                    ),
                    {"project_id": project_id, "sha": "a" * 64},
                )
        finally:
            engine.dispose()
        bound = database.session_factory.kw.get("bind")
        if bound is not None:
            bound.dispose()
        upgraded = _run_alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        engine = create_engine(database_url)
        try:
            with Session(engine) as session:
                assert session.scalar(
                    text("select count(*) from projects where id=:id"),
                    {"id": project_id},
                ) == 1
                assert session.scalar(
                    text("select count(*) from documents where id=:id"),
                    {"id": document_id},
                ) == 1
                assert session.scalar(text("select count(*) from due_work_schedules")) == 0
                schedule = configure_processing_health(
                    session,
                    ProcessingHealthDeclaration.released_hourly(
                        project_id=project_id,
                        configuration_version="processing-health-v1",
                        starts_at=datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc),
                    ),
                    now=datetime(2026, 8, 29, 7, 5, tzinfo=timezone.utc),
                )
                session.commit()

                with pytest.raises(ProgrammingError), session.begin_nested():
                    session.execute(
                        update(DueWorkSchedule)
                        .where(DueWorkSchedule.id == schedule.id)
                        .values(configuration_version="rewritten")
                    )
                assert session.scalar(
                    select(DueWorkSchedule.configuration_version).where(
                        DueWorkSchedule.id == schedule.id
                    )
                ) == "processing-health-v1"
        finally:
            engine.dispose()

        refused = _run_alembic(database_url, "downgrade", PREDECESSOR)
        assert refused.returncode != 0
        assert "cannot erase retained Due Work operational history" in (
            refused.stdout + refused.stderr
        )
