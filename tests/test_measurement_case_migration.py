"""Fresh and predecessor rehearsals for human-ruling measurement cases.

ORM-only append discipline was rejected because an operator or later writer
could still rewrite the cases that future model comparisons rely on. These
tests prove the database keeps a linear successor chain and refuses mutation
or destructive downgrade after a human case exists.
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
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "e319f8a0b2c5"
HEAD = "b5d1e2f3a5c6"


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


def test_human_case_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="measurement_cases_fresh_",
    ) as database:
        engine = create_engine(_url(database))
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text("select version_num from alembic_version")
                ) == HEAD
                assert connection.scalar(
                    text("select to_regclass('extraction_measurement_case_states')")
                ) == "extraction_measurement_case_states"
        finally:
            engine.dispose()


def test_human_case_states_append_after_predecessor_and_refuse_erasure():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="measurement_cases_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _url(database)
        bound = database.session_factory.kw.get("bind")
        if bound is not None:
            bound.dispose()
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('measurement-cases', 'Measurement Cases', true) "
                        "returning id"
                    )
                )
                root_id = connection.scalar(
                    text(
                        "insert into extraction_measurement_case_states "
                        "(public_id, project_id, case_key, kind, state, ruling_type, "
                        "ruling_id, source_identity_json, expected_json, recorded_by) "
                        "values ('00000000-0000-0000-0000-000000000001', :project, "
                        "'candidate:1:correction', 'candidate_correction', 'active', "
                        "'audit_log', 1, '{\"candidate_id\": 1, "
                        "\"extraction_run_id\": 1, \"documents\": "
                        "[{\"document_id\": 1}]}', "
                        "'{\"scoring_rule\": \"candidate_fields_include\"}', "
                        "'local:reviewer') returning id"
                    ),
                    {"project": project_id},
                )
                successor_id = connection.scalar(
                    text(
                        "insert into extraction_measurement_case_states "
                        "(public_id, project_id, case_key, predecessor_state_id, kind, "
                        "state, ruling_type, ruling_id, source_identity_json, "
                        "expected_json, recorded_by) values "
                        "('00000000-0000-0000-0000-000000000002', :project, "
                        "'candidate:1:correction', :root, 'candidate_correction', "
                        "'active', 'audit_log', 2, '{\"candidate_id\": 1, "
                        "\"extraction_run_id\": 1, \"documents\": "
                        "[{\"document_id\": 1}]}', "
                        "'{\"scoring_rule\": \"candidate_fields_include\"}', "
                        "'local:reviewer') returning id"
                    ),
                    {"project": project_id, "root": root_id},
                )
                assert successor_id > root_id

            with pytest.raises((IntegrityError, DBAPIError)):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "update extraction_measurement_case_states "
                            "set state='reversed' where id=:root"
                        ),
                        {"root": root_id},
                    )

            with pytest.raises((IntegrityError, DBAPIError)):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into extraction_measurement_case_states "
                            "(public_id, project_id, case_key, predecessor_state_id, "
                            "kind, state, ruling_type, ruling_id, source_identity_json, "
                            "expected_json, recorded_by) values "
                            "('00000000-0000-0000-0000-000000000003', :project, "
                            "'another-case', :successor, 'candidate_correction', "
                            "'active', 'audit_log', 3, '{\"candidate_id\": 1, "
                            "\"extraction_run_id\": 1, \"documents\": "
                            "[{\"document_id\": 1}]}', "
                            "'{\"scoring_rule\": \"candidate_fields_include\"}', "
                            "'local:reviewer')"
                        ),
                        {"project": project_id, "successor": successor_id},
                    )
        finally:
            engine.dispose()

        refused = _alembic(database_url, "downgrade", PREDECESSOR)
        assert refused.returncode != 0
        assert "cannot erase retained human measurement cases" in (
            refused.stdout + refused.stderr
        )
