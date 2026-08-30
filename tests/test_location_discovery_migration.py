"""Fresh and predecessor rehearsals for the connected-location tables (#350).

Trusting a stamped development database was rejected: a missing table or a dropped
check constraint can still report the expected Alembic head. These tests prove the
fresh schema objects exist and that a populated predecessor upgrades without losing
domain rows, then that the new tables actually enforce their contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "d359a1b2c3e4"
HEAD = "e1f2a3b4c5d6"


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


def test_location_discovery_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="location_discovery_fresh_",
    ) as database:
        engine = create_engine(_database_url(database))
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text("select version_num from alembic_version")
                ) == HEAD
                tables = set(
                    connection.scalars(
                        text(
                            "select table_name from information_schema.tables "
                            "where table_schema='public' and table_name in "
                            "('discovered_references', 'source_fetch_attempts')"
                        )
                    ).all()
                )
                assert tables == {"discovered_references", "source_fetch_attempts"}
                columns = set(
                    connection.scalars(
                        text(
                            "select column_name from information_schema.columns "
                            "where table_name='discovered_references'"
                        )
                    ).all()
                )
                assert {
                    "reference_key",
                    "source_url",
                    "archive_url",
                    "member",
                    "state",
                    "authorized_doc_type",
                    "first_observed_at",
                    "observed_count",
                }.issubset(columns)
        finally:
            engine.dispose()


def test_predecessor_upgrade_preserves_domain_rows_and_enforces_contracts():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="location_discovery_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _database_url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('loc-predecessor', 'Location predecessor', true) "
                        "returning id"
                    )
                )
                document_id = connection.scalar(
                    text(
                        "insert into documents "
                        "(project_id, sha256, filename, doc_type, parse_status) "
                        "values (:pid, :sha, 'predecessor.pdf', 'matrix', 'parsed') "
                        "returning id"
                    ),
                    {"pid": project_id, "sha": "a" * 64},
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
            with engine.begin() as connection:
                assert connection.scalar(
                    text("select count(*) from projects where id=:id"),
                    {"id": project_id},
                ) == 1
                assert connection.scalar(
                    text("select count(*) from documents where id=:id"),
                    {"id": document_id},
                ) == 1
                # The new tables accept a well-formed row.
                connection.execute(
                    text(
                        "insert into discovered_references "
                        "(project_id, location_id, reference_key, source_url, "
                        "first_observed_at, last_observed_at, observed_count, state) "
                        "values (:pid, 'loc', :key, 'https://x/y.pdf', now(), now(), "
                        "1, 'proposed')"
                    ),
                    {"pid": project_id, "key": "b" * 64},
                )
                connection.execute(
                    text(
                        "insert into source_fetch_attempts "
                        "(project_id, location_id, attempted_at, outcome, resumable) "
                        "values (:pid, 'loc', now(), 'registered', false)"
                    ),
                    {"pid": project_id},
                )
            # A registered state without an authorization violates the state check.
            with engine.begin() as connection:
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "insert into discovered_references "
                            "(project_id, location_id, reference_key, source_url, "
                            "first_observed_at, last_observed_at, observed_count, "
                            "state) values (:pid, 'loc', :key, 'https://x/z.pdf', "
                            "now(), now(), 1, 'registered')"
                        ),
                        {"pid": project_id, "key": "c" * 64},
                    )
            # An unknown fetch outcome is refused.
            with engine.begin() as connection:
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "insert into source_fetch_attempts "
                            "(project_id, location_id, attempted_at, outcome) "
                            "values (:pid, 'loc', now(), 'made_up')"
                        ),
                        {"pid": project_id},
                    )
        finally:
            engine.dispose()
