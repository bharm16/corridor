"""Migration rehearsal for the additive Coordination Subject expansion."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "b230e4f5a6b7"
HEAD = "c249d7e1f4a3"


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
