"""Fresh and predecessor rehearsals for the Record Inclusion watermark.

Trusting a stamped development database was rejected because a missing check or
a wrongly-added immutability trigger can still report the expected Alembic head.
These tests prove the fresh schema object, its check constraints, its deliberate
mutability (unlike the append-only receipt tables), and that a populated
predecessor database upgrades without losing rows.
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
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import RecordInclusionRequest
from corridor.admission import reconcile_record_inclusion
from corridor.record_inclusion import request_record_inclusion


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "a364b7c9e2f1"
HEAD = "b7d3f9a1c2e5"


def _run_alembic(database_url: str, *args: str):
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def _database_url(database) -> str:
    return (
        make_url(settings.database_url)
        .set(database=database.name)
        .render_as_string(hide_password=False)
    )


def test_record_inclusion_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="record_inclusion_fresh_",
    ) as database:
        engine = create_engine(_database_url(database))
        try:
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                assert connection.scalar(
                    text(
                        "select to_regclass('public.record_inclusion_requests') "
                        "is not null"
                    )
                )
                checks = set(
                    connection.scalars(
                        text(
                            "select conname from pg_constraint "
                            "where conrelid = 'record_inclusion_requests'::regclass "
                            "and contype = 'c'"
                        )
                    ).all()
                )
                assert {
                    "ck_record_inclusion_requests_non_negative",
                    "ck_record_inclusion_requests_watermark_order",
                }.issubset(checks)
                # The watermark is mutable state, so it must carry no immutability
                # trigger like the append-only receipt tables do.
                triggers = list(
                    connection.scalars(
                        text(
                            "select tgname from pg_trigger where not tgisinternal "
                            "and tgrelid = 'record_inclusion_requests'::regclass"
                        )
                    ).all()
                )
                assert triggers == []
        finally:
            engine.dispose()


def test_predecessor_upgrade_preserves_rows_and_watermark_stays_mutable():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="record_inclusion_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _database_url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert connection.scalar(
                    text("select to_regclass('public.record_inclusion_requests')")
                ) is None
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('record-inclusion-predecessor', 'RI predecessor', true) "
                        "returning id"
                    )
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
                    text("select count(*) from record_inclusion_requests")
                ) == 0
                # A producer bump then a reconcile UPDATEs the same row — which a
                # wrongly-added immutability trigger would have blocked.
                request_record_inclusion(session, project_id, "extraction_completed")
                request_record_inclusion(session, project_id, "extraction_completed")
                result = reconcile_record_inclusion(session, project_id)
                assert result.did_load is True
                row = session.get(RecordInclusionRequest, project_id)
                assert (row.dirty_seq, row.reconciled_seq) == (2, 2)
                session.commit()
        finally:
            engine.dispose()

        # The watermark holds only recoverable state, so downgrade drops it cleanly.
        downgraded = _run_alembic(database_url, "downgrade", PREDECESSOR)
        assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text("select to_regclass('public.record_inclusion_requests')")
                ) is None
                assert connection.scalar(
                    text("select count(*) from projects where id=:id"),
                    {"id": project_id},
                ) == 1
        finally:
            engine.dispose()
