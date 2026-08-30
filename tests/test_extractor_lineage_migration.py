"""Fresh and predecessor rehearsals for extractor configuration receipts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "a316c5d7e9f1"
HEAD = "c355a7d9e2f1"


def _migrate(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _downgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "downgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_extractor_lineage_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="extractor_lineage_fresh_",
    ) as database:
        with database.session_factory().connection() as connection:
            inspector = inspect(connection)
            columns = {
                column["name"] for column in inspector.get_columns("extraction_runs")
            }
            assert connection.scalar(text("select version_num from alembic_version")) == HEAD
            assert {
                "prompt_sha256",
                "schema_sha256",
                "postprocessor_sha256",
                "extractor_config_json",
                "extractor_config_sha256",
                "token_usage_json",
            }.issubset(columns)
            assert "ck_extraction_runs_config_receipt_shape" in {
                constraint["name"]
                for constraint in inspector.get_check_constraints("extraction_runs")
            }
            assert connection.scalar(
                text(
                    "select exists (select 1 from pg_proc "
                    "where proname = 'extraction_token_usage_membership_is_valid')"
                )
            ) is True


def test_predecessor_upgrade_preserves_null_history_and_guards_new_shapes():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="extractor_lineage_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (id, slug, name, is_synthetic) values "
                        "(31701, 'lineage-predecessor', 'Lineage predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into documents "
                        "(id, project_id, sha256, filename, doc_type, parse_status) "
                        "values (31702, 31701, :sha, 'legacy.pdf', 'matrix', 'parsed')"
                    ),
                    {"sha": "1" * 64},
                )
                connection.execute(
                    text(
                        "insert into extraction_runs "
                        "(id, document_id, prompt_version, outcome, candidate_count, "
                        "page_errors, model, schema_version) values "
                        "(31703, 31702, 'legacy-v1', 'completed', 0, 0, "
                        "'legacy-model', 'legacy-shape-v1')"
                    )
                )
            engine.dispose()
            _migrate(rendered, HEAD)
            engine = create_engine(database_url)

            with engine.connect() as connection:
                historical = connection.execute(
                    text(
                        "select prompt_sha256, schema_sha256, postprocessor_sha256, "
                        "extractor_config_json, extractor_config_sha256, token_usage_json "
                        "from extraction_runs where id = 31703"
                    )
                ).one()
                assert tuple(historical) == (None, None, None, None, None, None)

            config = {
                "receipt_version": 1,
                "extractor": "fixture",
                "prompt_version": "fixture-v1",
                "model": "fixture-model",
                "schema_version": "fixture-shape-v1",
                "prompt_sha256": "2" * 64,
                "schema_sha256": "3" * 64,
                "postprocessor_sha256": "4" * 64,
                "request_controls": {"strict": True},
                "runtime": {
                    "python_implementation": "CPython",
                    "python_version": "3.12.0",
                    "dependency_lock_sha256": "9" * 64,
                    "packages": {},
                },
            }
            usage = {
                "scope": "run",
                "document_ids": [31702],
                "measurement": "exact",
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            }
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into extraction_runs "
                        "(id, document_id, prompt_version, outcome, candidate_count, "
                        "page_errors, model, schema_version, prompt_sha256, "
                        "schema_sha256, postprocessor_sha256, extractor_config_json, "
                        "extractor_config_sha256, token_usage_json) values "
                        "(31704, 31702, 'fixture-v1', 'completed', 0, 0, "
                        "'fixture-model', 'fixture-shape-v1', :prompt_sha, :schema_sha, "
                        ":rules_sha, cast(:config as jsonb), :config_sha, "
                        "cast(:usage as jsonb))"
                    ),
                    {
                        "prompt_sha": "2" * 64,
                        "schema_sha": "3" * 64,
                        "rules_sha": "4" * 64,
                        "config": json.dumps(config),
                        "config_sha": "5" * 64,
                        "usage": json.dumps(usage),
                    },
                )

            with pytest.raises(IntegrityError, match="config_receipt_shape"):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into extraction_runs "
                            "(id, document_id, prompt_version, outcome, candidate_count, "
                            "page_errors, prompt_sha256) values "
                            "(31705, 31702, 'partial-v1', 'completed', 0, 0, :sha)"
                        ),
                        {"sha": "6" * 64},
                    )

            with pytest.raises(IntegrityError, match="config_receipt_shape"):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into extraction_runs "
                            "(id, document_id, prompt_version, outcome, candidate_count, "
                            "page_errors, model, schema_version, prompt_sha256, "
                            "schema_sha256, postprocessor_sha256, extractor_config_json, "
                            "extractor_config_sha256, token_usage_json) "
                            "select 31706, document_id, prompt_version, outcome, "
                            "candidate_count, page_errors, model, schema_version, "
                            "prompt_sha256, schema_sha256, postprocessor_sha256, "
                            "extractor_config_json, extractor_config_sha256, "
                            "'{\"scope\":\"run\",\"document_ids\":[31702],"
                            "\"measurement\":\"unavailable\"}'::jsonb "
                            "from extraction_runs where id = 31704"
                        )
                    )

            invalid_clones = {
                31707: (
                    "jsonb_set(extractor_config_json, '{prompt_version}', "
                    "'null'::jsonb)",
                    "token_usage_json",
                    {},
                ),
                31708: (
                    "extractor_config_json",
                    "jsonb_set(token_usage_json, '{measurement}', 'null'::jsonb)",
                    {},
                ),
                31709: (
                    "extractor_config_json",
                    "cast(:invalid_usage as jsonb)",
                    {
                        "invalid_usage": json.dumps(
                            {
                                **usage,
                                "document_ids": [99999],
                            }
                        )
                    },
                ),
                31710: (
                    "extractor_config_json",
                    "cast(:invalid_usage as jsonb)",
                    {
                        "invalid_usage": json.dumps(
                            {
                                **usage,
                                "scope": "batch",
                                "document_ids": [31702, 31702],
                            }
                        )
                    },
                ),
                31711: (
                    "extractor_config_json",
                    "cast(:invalid_usage as jsonb)",
                    {
                        "invalid_usage": json.dumps(
                            {
                                **usage,
                                "scope": "batch",
                                "document_ids": [31702, "31703"],
                            }
                        )
                    },
                ),
            }
            for run_id, values in invalid_clones.items():
                config_expression, usage_expression, parameters = values
                with pytest.raises(IntegrityError, match="config_receipt_shape"):
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "insert into extraction_runs "
                                "(id, document_id, prompt_version, outcome, "
                                "candidate_count, page_errors, model, schema_version, "
                                "prompt_sha256, schema_sha256, postprocessor_sha256, "
                                "extractor_config_json, extractor_config_sha256, "
                                "token_usage_json) "
                                f"select {run_id}, document_id, prompt_version, outcome, "
                                "candidate_count, page_errors, model, schema_version, "
                                "prompt_sha256, schema_sha256, postprocessor_sha256, "
                                f"{config_expression}, extractor_config_sha256, "
                                f"{usage_expression} from extraction_runs where id = 31704"
                            ),
                            parameters,
                        )
        finally:
            engine.dispose()


def test_head_downgrades_cleanly_and_reupgrade_preserves_unsealed_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="extractor_lineage_downgrade_",
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (id, slug, name, is_synthetic) values "
                        "(31801, 'lineage-downgrade', 'Lineage downgrade', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into documents "
                        "(id, project_id, sha256, filename, doc_type, parse_status) "
                        "values (31802, 31801, :sha, 'history.pdf', 'matrix', 'parsed')"
                    ),
                    {"sha": "8" * 64},
                )
                connection.execute(
                    text(
                        "insert into extraction_runs "
                        "(id, document_id, prompt_version, outcome, candidate_count, "
                        "page_errors, model, schema_version) values "
                        "(31803, 31802, 'history-v1', 'completed', 0, 0, "
                        "'history-model', 'history-shape-v1')"
                    )
                )
            engine.dispose()
            _downgrade(rendered, PREDECESSOR)
            engine = create_engine(database_url)
            with engine.connect() as connection:
                columns = {
                    column["name"]
                    for column in inspect(connection).get_columns("extraction_runs")
                }
                assert "extractor_config_json" not in columns
                assert connection.scalar(
                    text("select count(*) from extraction_runs where id = 31803")
                ) == 1
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_proc "
                        "where proname = 'extraction_token_usage_membership_is_valid')"
                    )
                ) is False
            engine.dispose()
            _migrate(rendered, HEAD)
            engine = create_engine(database_url)
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "select extractor_config_json, token_usage_json "
                        "from extraction_runs where id = 31803"
                    )
                ).one()
                assert tuple(row) == (None, None)
        finally:
            engine.dispose()
