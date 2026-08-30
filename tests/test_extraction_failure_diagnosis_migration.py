"""Rehearse retained failure-diagnosis receipts on real disposable PostgreSQL."""

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
PREDECESSOR = "f3a5c7d9e1b2"
HEAD = "e4c8b1a6d3f7"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _columns(connection, table: str) -> list[str]:
    return (
        connection.execute(
            text(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' and table_name = :table "
                "order by ordinal_position"
            ),
            {"table": table},
        )
        .scalars()
        .all()
    )


def test_failure_diagnosis_schema_is_one_linear_head_on_a_fresh_database():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue361_fresh_",
    ) as database:
        url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                assert _columns(
                    connection, "extraction_failure_diagnosis_configurations"
                ) == [
                    "id",
                    "project_id",
                    "model",
                    "prompt_version",
                    "max_input_tokens",
                    "max_output_tokens",
                    "timeout_seconds",
                    "max_requests",
                    "retry_policy",
                    "retention_policy",
                    "observation_context",
                    "created_by",
                    "created_at",
                ]
                assert _columns(
                    connection, "extraction_failure_diagnosis_requests"
                ) == [
                    "id",
                    "public_id",
                    "project_id",
                    "document_id",
                    "extraction_run_id",
                    "configuration_id",
                    "requested_by",
                    "input_sha256",
                    "state_token",
                    "model",
                    "prompt_version",
                    "adapter",
                    "adapter_contract_version",
                    "tool_contract_version",
                    "validator_version",
                    "status",
                    "reason",
                    "source_context_json",
                    "diagnosis_json",
                    "execution_lineage_json",
                    "read_fingerprint",
                    "budget_json",
                    "usage_json",
                    "non_authoritative",
                    "created_at",
                    "completed_at",
                ]
                for trigger_name in (
                    "extraction_failure_diagnosis_configurations_are_immutable",
                    "extraction_failure_diagnosis_requests_are_immutable",
                    "extraction_failure_diagnosis_configurations_reject_truncate",
                    "extraction_failure_diagnosis_requests_reject_truncate",
                ):
                    assert connection.scalar(
                        text(
                            "select exists (select 1 from pg_trigger "
                            "where tgname = :trigger_name)"
                        ),
                        {"trigger_name": trigger_name},
                    )
        finally:
            engine.dispose()


def test_predecessor_to_head_preserves_project_rows_and_adds_receipts():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue361_predecessor_",
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
                        "values ('issue361', 'Issue 361', true)"
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
                            "select to_regclass"
                            "('extraction_failure_diagnosis_requests')"
                        )
                    )
                    is not None
                )
                assert (
                    connection.scalar(
                        text("select count(*) from projects where slug = 'issue361'")
                    )
                    == 1
                )
                assert (
                    connection.scalar(
                        text(
                            "select count(*) from "
                            "extraction_failure_diagnosis_requests"
                        )
                    )
                    == 0
                )
        finally:
            engine.dispose()
