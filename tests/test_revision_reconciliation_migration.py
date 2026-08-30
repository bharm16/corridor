"""Fresh and predecessor rehearsals for the revision-reconciliation watermark.

Trusting a stamped development database was rejected because a missing check or a
wrongly-added immutability trigger can still report the expected Alembic head.
These tests prove the fresh schema object, its check constraints, its deliberate
mutability (unlike the append-only receipt tables), and that a populated
predecessor database upgrades and downgrades without losing project rows.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import RevisionReconciliationRequest
from corridor.revision_reconciliation_request import request_revision_reconciliation


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "86edb31fd81a"
HEAD = "f360a1b2c3d4"


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


def test_revision_reconciliation_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="revision_reconciliation_fresh_",
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
                        "select to_regclass('public.revision_reconciliation_requests') "
                        "is not null"
                    )
                )
                checks = set(
                    connection.scalars(
                        text(
                            "select conname from pg_constraint "
                            "where conrelid = "
                            "'revision_reconciliation_requests'::regclass "
                            "and contype = 'c'"
                        )
                    ).all()
                )
                assert {
                    "ck_revision_reconciliation_requests_non_negative",
                    "ck_revision_reconciliation_requests_watermark_order",
                }.issubset(checks)
                # The watermark is mutable state, so it must carry no immutability
                # trigger like the append-only receipt tables do.
                triggers = list(
                    connection.scalars(
                        text(
                            "select tgname from pg_trigger where not tgisinternal "
                            "and tgrelid = "
                            "'revision_reconciliation_requests'::regclass"
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
        database_prefix="revision_reconciliation_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _database_url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert connection.scalar(
                    text(
                        "select to_regclass("
                        "'public.revision_reconciliation_requests')"
                    )
                ) is None
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('revision-predecessor', 'Rev predecessor', true) "
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
                    text("select count(*) from revision_reconciliation_requests")
                ) == 0
                # Two producer bumps then a direct reconciled-seq advance UPDATE the
                # same row — which a wrongly-added immutability trigger would block.
                request_revision_reconciliation(
                    session, project_id, "supersession_registered"
                )
                request_revision_reconciliation(
                    session, project_id, "active_run_declared"
                )
                session.execute(
                    update(RevisionReconciliationRequest)
                    .where(RevisionReconciliationRequest.project_id == project_id)
                    .values(reconciled_seq=2)
                )
                row = session.get(RevisionReconciliationRequest, project_id)
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
                    text(
                        "select to_regclass("
                        "'public.revision_reconciliation_requests')"
                    )
                ) is None
                assert connection.scalar(
                    text("select count(*) from projects where id=:id"),
                    {"id": project_id},
                ) == 1
        finally:
            engine.dispose()
