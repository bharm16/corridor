"""Fresh and predecessor rehearsals for matrix row-accounting receipts.

Stamp-only checks were rejected because a database can report the new head
without the JSON shape/completion constraints. These tests preserve a real
historical null-accounting run, upgrade it, and prove new reader completions
cannot bypass the accounting column.
"""

from __future__ import annotations

import json
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
PREDECESSOR = "d319e7f9a1b4"
HEAD = "c355a7d9e2f1"


def _url(database) -> str:
    return make_url(settings.database_url).set(database=database.name).render_as_string(
        hide_password=False
    )


def _alembic(database_url: str, *args: str):
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def test_row_accounting_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="row_accounting_fresh_",
    ) as database:
        engine = create_engine(_url(database))
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert connection.scalar(
                    text(
                        "select exists(select 1 from information_schema.columns "
                        "where table_name='extraction_runs' "
                        "and column_name='row_accounting_json')"
                    )
                ) is True
                constraints = set(
                    connection.scalars(
                        text(
                            "select conname from pg_constraint "
                            "where conrelid='extraction_runs'::regclass"
                        )
                    ).all()
                )
                assert {
                    "ck_extraction_runs_row_accounting_shape",
                    "ck_extraction_runs_completed_row_accounting",
                }.issubset(constraints)
        finally:
            engine.dispose()


def test_predecessor_upgrade_preserves_historical_null_and_guards_new_reader():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="row_accounting_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('row-accounting-old', 'Old reading', true) returning id"
                    )
                )
                document_id = connection.scalar(
                    text(
                        "insert into documents "
                        "(project_id, registry_id, sha256, filename, doc_type, parse_status) "
                        "values (:project, 'old-matrix', :sha, 'old.pdf', 'matrix', "
                        "'parsed') returning id"
                    ),
                    {"project": project_id, "sha": "a" * 64},
                )
                old_run_id = connection.scalar(
                    text(
                        "insert into extraction_runs "
                        "(document_id, prompt_version, outcome, candidate_count, "
                        "page_errors, candidate_inputs_json) "
                        "values (:document, 'matrix_tiered_v3', 'completed', 0, 0, '[]') "
                        "returning id"
                    ),
                    {"document": document_id},
                )
        finally:
            engine.dispose()
        bound = database.session_factory.kw.get("bind")
        if bound is not None:
            bound.dispose()
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert connection.scalar(
                    text(
                        "select row_accounting_json is null from extraction_runs "
                        "where id=:run"
                    ),
                    {"run": old_run_id},
                ) is True
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "insert into extraction_runs "
                            "(document_id, prompt_version, outcome, candidate_count, "
                            "page_errors, candidate_inputs_json) "
                            "values (:document, 'matrix_tiered_v4', 'completed', "
                            "0, 0, '[]')"
                        ),
                        {"document": document_id},
                    )
        finally:
            engine.dispose()

        accounting = {
            "schema_version": "matrix-row-accounting-v1",
            "reader_version": "matrix_tiered_v4",
            "reader_path": "page_geometry_and_transcription",
            "detected_row_count": 0,
            "accounted_row_count": 0,
            "extracted_row_count": 0,
            "blank_row_count": 0,
            "skipped_row_count": 0,
            "unaccounted_rows": [],
            "rows": [],
        }
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                accounted_run_id = connection.scalar(
                    text(
                        "insert into extraction_runs "
                        "(document_id, prompt_version, outcome, candidate_count, "
                        "page_errors, candidate_inputs_json, row_accounting_json) "
                        "values (:document, 'matrix_tiered_v4', 'completed', 0, 0, "
                        "'[]', cast(:accounting as jsonb)) returning id"
                    ),
                    {
                        "document": document_id,
                        "accounting": json.dumps(accounting),
                    },
                )
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "update extraction_runs set row_accounting_json = null "
                            "where id=:run"
                        ),
                        {"run": accounted_run_id},
                    )
        finally:
            engine.dispose()

        refused = _alembic(database_url, "downgrade", PREDECESSOR)
        assert refused.returncode != 0
        assert "cannot erase retained matrix row accounting" in (
            refused.stdout + refused.stderr
        )
