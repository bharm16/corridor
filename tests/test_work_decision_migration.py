"""Migration rehearsal for the additive Coordination Subject expansion."""

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
PREDECESSOR = "b230e4f5a6b7"
REASON_PROJECTION_PREDECESSOR = "c249d7e1f4a3"
HEAD = "f362a1b2c3d4"
WORK_LIST_PREDECESSOR = "e253a7c4d9e2"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_work_decision_expansion_is_one_linear_head_on_a_fresh_database():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue249_fresh_",
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert connection.scalar(
                    text(
                        "select exists (select 1 from information_schema.tables "
                        "where table_schema = 'public' "
                        "and table_name = 'commitment_lineages')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_constraint "
                        "where conname = 'ck_work_decisions_deferral_shape')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger "
                        "where tgname = 'dependency_event_closure_link_is_valid')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from information_schema.tables "
                        "where table_schema = 'public' "
                        "and table_name = 'candidate_dispositions')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from information_schema.tables "
                        "where table_schema = 'public' "
                        "and table_name = 'statement_coordination_reversals')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_trigger "
                        "where tgname = 'statement_coordination_reversals_are_immutable')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "select exists (select 1 from information_schema.tables "
                        "where table_schema = 'public' "
                        "and table_name = 'statement_coordination_receipts')"
                    )
                ) is True
        finally:
            engine.dispose()


def test_work_decision_expansion_preserves_exact_predecessor_receipts():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue249_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue249-predecessor', 'Issue 249 predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies "
                        "(project_id, ref_code, dep_type, title, status, "
                        "internal_owner, next_action, action_due_date) "
                        "select id, 'WD-LEGACY', 'utility_relocation', "
                        "'Legacy Work Decision', 'identified', 'Dana Fields', "
                        "'Call the party', date '2026-08-20' from projects "
                        "where slug = 'issue249-predecessor'"
                    )
                )
                connection.execute(
                    text(
                        "insert into work_decisions "
                        "(dependency_id, decision_type, field, before_value, "
                        "after_value, recorded_by) "
                        "select id, 'assign_internal_owner', 'internal_owner', null, "
                        "'Dana Fields', 'local:coordination-recorder' "
                        "from dependencies where ref_code = 'WD-LEGACY'"
                    )
                )
                connection.execute(
                    text(
                        "insert into work_decisions "
                        "(dependency_id, decision_type, field, before_value, "
                        "after_value, recorded_by) "
                        "select id, 'set_next_action', 'next_action', null, "
                        "'{\"action\":\"Call the party\",\"due_date\":\"2026-08-20\"}', "
                        "'local:coordination-recorder' from dependencies "
                        "where ref_code = 'WD-LEGACY'"
                    )
                )

            _upgrade(rendered, "head")

            with engine.connect() as connection:
                receipts = connection.execute(
                    text(
                        "select dependency_id, commitment_lineage_id, decision_type, "
                        "field, before_value, after_value from work_decisions "
                        "order by id"
                    )
                ).mappings().all()
                assert [dict(receipt) for receipt in receipts] == [
                    {
                        "dependency_id": 1,
                        "commitment_lineage_id": None,
                        "decision_type": "assign_internal_owner",
                        "field": "internal_owner",
                        "before_value": None,
                        "after_value": "Dana Fields",
                    },
                    {
                        "dependency_id": 1,
                        "commitment_lineage_id": None,
                        "decision_type": "set_next_action",
                        "field": "next_action",
                        "before_value": None,
                        "after_value": (
                            '{"action":"Call the party","due_date":"2026-08-20"}'
                        ),
                    },
                ]
        finally:
            engine.dispose()


def test_reason_projection_backfills_an_undated_dependency_action_tail():
    """The successor migration preserves b249's receipt/projection equality."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue249_reason_predecessor_",
        migration_revision=REASON_PROJECTION_PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue249-reason', 'Issue 249 reason', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies "
                        "(project_id, ref_code, dep_type, title, status, next_action) "
                        "select id, 'WD-REASON', 'utility_relocation', "
                        "'Undated action', 'identified', 'Call the party' "
                        "from projects where slug = 'issue249-reason'"
                    )
                )
                connection.execute(
                    text(
                        "insert into work_decisions "
                        "(dependency_id, decision_type, field, before_value, "
                        "after_value, recorded_by, action_due_date_reason) "
                        "select id, 'set_next_action', 'next_action', null, "
                        "json_build_object('action', 'Call the party', "
                        "'due_date', null)::text, "
                        "'local:coordination-recorder', "
                        "'awaiting_external_information' "
                        "from dependencies where ref_code = 'WD-REASON'"
                    )
                )

            _upgrade(rendered, "head")

            with engine.connect() as connection:
                assert connection.scalar(
                    text(
                        "select action_due_date_reason from dependencies "
                        "where ref_code = 'WD-REASON'"
                    )
                ) == "awaiting_external_information"
        finally:
            engine.dispose()


def test_work_list_successors_upgrade_the_immediate_predecessor_exactly():
    """A stamped b252 database receives every #253 column and guard once."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue253_predecessor_",
        migration_revision=WORK_LIST_PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('issue253-predecessor', 'Issue 253 predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies "
                        "(project_id, ref_code, dep_type, title, status) "
                        "select id, 'WORK-LIST-LEGACY', 'utility_relocation', "
                        "'Predecessor Dependency', 'identified' from projects "
                        "where slug = 'issue253-predecessor'"
                    )
                )

            _upgrade(rendered, "head")

            with engine.connect() as connection:
                assert connection.scalar(
                    text("select version_num from alembic_version")
                ) == HEAD
                assert connection.execute(
                    text(
                        "select deferral_reason, deferral_return_date from dependencies "
                        "where ref_code = 'WORK-LIST-LEGACY'"
                    )
                ).one() == (None, None)
                assert connection.execute(
                    text(
                        "select column_name from information_schema.columns "
                        "where table_schema = 'public' and table_name = 'work_decisions' "
                        "and column_name in ("
                        "'deferral_reason', 'deferral_return_date', "
                        "'observed_statement_event_id', 'observed_scope_decision_id', "
                        "'observed_milestone_impact_decision_id') "
                        "order by column_name"
                    )
                ).scalars().all() == [
                    "deferral_reason",
                    "deferral_return_date",
                    "observed_milestone_impact_decision_id",
                    "observed_scope_decision_id",
                    "observed_statement_event_id",
                ]
                assert connection.scalar(
                    text(
                        "select exists (select 1 from information_schema.columns "
                        "where table_schema = 'public' and table_name = 'dependency_events' "
                        "and column_name = 'closes_commitment_lineage_id')"
                    )
                ) is True
        finally:
            engine.dispose()
