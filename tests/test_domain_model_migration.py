"""Fresh and predecessor rehearsals for ADR-0034 and ADR-0044/0045."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "e314a3d8c6f2"
HEAD = "e4c8b1a6d3f7"


def _upgrade(database_url: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", HEAD],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_domain_model_schema_is_on_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="domain_model_fresh_",
    ) as database:
        with database.session_factory().connection() as connection:
            inspector = inspect(connection)
            dependency_columns = {
                column["name"] for column in inspector.get_columns("dependencies")
            }
            receipt_columns = {
                column["name"]
                for column in inspector.get_columns("automatic_carry_forward_receipts")
            }
            assert (
                connection.scalar(text("select version_num from alembic_version"))
                == HEAD
            )
            assert "status" not in dependency_columns
            assert "milestone_registration_id" in dependency_columns
            assert {"policy_version", "policy_sha256"}.issubset(receipt_columns)
            assert inspector.has_table("milestone_registrations")
            assert inspector.has_table("retired_dependency_statuses")
            assert inspector.has_table(
                "retired_automatic_carry_forward_policy_activations"
            )


def test_predecessor_upgrade_preserves_retired_status_and_policy_activation():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="domain_model_predecessor_",
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
                        "(44001, 'domain-predecessor', 'Domain predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into milestones "
                        "(id, project_id, code, name, need_date, source) values "
                        "(44002, 44001, 'LET', 'Letting', '2027-01-20', "
                        "'legacy-schedule.csv')"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies "
                        "(id, project_id, ref_code, dep_type, title, status, "
                        "milestone_id, need_date) values "
                        "(44003, 44001, 'DEP-44003', 'permit', 'Permit', "
                        "'blocked', 44002, '2027-01-20')"
                    )
                )
                connection.execute(
                    text(
                        "insert into policy_approvals "
                        "(id, project_id, family, policy_version, approved_by, "
                        "policy_json, policy_sha256) values "
                        "(44004, 44001, 'automatic-carry-forward', "
                        "'automatic-carry-forward-v1', 'local:legacy-approver', "
                        '\'{"policy_version":"automatic-carry-forward-v1"}\', :sha)'
                    ),
                    {"sha": "a" * 64},
                )
                connection.execute(
                    text(
                        "insert into active_automatic_carry_forward_policies "
                        "(project_id, family, policy_approval_id) values "
                        "(44001, 'automatic-carry-forward', 44004)"
                    )
                )
            engine.dispose()
            _upgrade(rendered)
            engine = create_engine(database_url)
            with engine.connect() as connection:
                retired = connection.execute(
                    text(
                        "select status from retired_dependency_statuses "
                        "where dependency_id = 44003"
                    )
                ).scalar_one()
                registration = (
                    connection.execute(
                        text(
                            "select id, source_name, source_sha256, source_row_json "
                            "from milestone_registrations where milestone_id = 44002"
                        )
                    )
                    .mappings()
                    .one()
                )
                dependency_registration = connection.scalar(
                    text(
                        "select milestone_registration_id from dependencies "
                        "where id = 44003"
                    )
                )
                retired_activation = connection.scalar(
                    text(
                        "select policy_approval_id from "
                        "retired_automatic_carry_forward_policy_activations "
                        "where project_id = 44001"
                    )
                )
                assert retired == "blocked"
                assert registration.source_name == "legacy-schedule.csv"
                assert registration.source_sha256 is None
                assert registration.source_row_json["need_date"] == "2027-01-20"
                assert dependency_registration == registration.id
                assert retired_activation == 44004
        finally:
            engine.dispose()
