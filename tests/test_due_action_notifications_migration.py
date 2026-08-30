"""Fresh and predecessor rehearsals for due-action-notification persistence (#352).

A stamped development database can report the expected head while missing a
trigger or a constraint, so these tests provision disposable databases: one
fresh to head to prove the schema objects and immutability triggers exist, and
one from the predecessor to head to prove a pre-existing project and roster
survive the upgrade, a derived occurrence can be recorded, its record is
immutable, and the history-preserving downgrade refuses to erase it.
"""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import DueActionNotification


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "f362a1b2c3d4"
HEAD = "a1b2c3d4e5f6"

_TABLES = {
    "due_action_notifications",
    "due_action_notification_dispatches",
    "due_action_notification_attempts",
}
_TRIGGERS = {
    "due_action_notifications_are_immutable",
    "due_action_dispatch_identity_is_immutable",
    "due_action_attempts_are_immutable",
}


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


def test_fresh_head_has_the_due_action_schema_and_triggers():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="due_action_fresh_",
    ) as database:
        engine = create_engine(_database_url(database))
        try:
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                tables = set(
                    connection.scalars(
                        text(
                            "select table_name from information_schema.tables "
                            "where table_schema='public' "
                            "and table_name like 'due_action_notification%'"
                        )
                    ).all()
                )
                assert tables == _TABLES
                triggers = set(
                    connection.scalars(
                        text(
                            "select tgname from pg_trigger where not tgisinternal "
                            "and tgname like 'due_action_%'"
                        )
                    ).all()
                )
                assert _TRIGGERS.issubset(triggers)
        finally:
            engine.dispose()


def test_predecessor_upgrade_records_and_protects_due_action_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="due_action_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _database_url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('due-action-predecessor', 'Due action', true) "
                        "returning id"
                    )
                )
                roster_id = connection.scalar(
                    text(
                        "insert into project_roster_entries "
                        "(project_id, principal_subject, display_name, active) "
                        "values (:project_id, 'local:due-action-recipient', "
                        "'Summary Recipient', true) returning id"
                    ),
                    {"project_id": project_id},
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
                assert (
                    session.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                # A derived daily-summary occurrence and its dispatch record.
                occurrence = DueActionNotification(
                    public_id="due-action-notification:migration-rehearsal",
                    project_id=project_id,
                    category="daily_summary",
                    recipient_role="summary",
                    recipient_roster_entry_id=roster_id,
                    recipient_principal_subject="local:due-action-recipient",
                    observation_start=date(2026, 8, 30),
                    observation_end=date(2026, 8, 30),
                    summary_json={"counts": {"overdue": 1}},
                    configuration_version="due-action-notification-v1",
                    occurrence_key="a" * 64,
                    registered_by="runtime:migration",
                )
                session.add(occurrence)
                session.commit()

                # The occurrence is immutable.
                with pytest.raises(ProgrammingError), session.begin_nested():
                    session.execute(
                        update(DueActionNotification)
                        .where(DueActionNotification.id == occurrence.id)
                        .values(category="daily_summary")
                    )
                assert (
                    session.scalar(
                        select(func.count()).select_from(DueActionNotification)
                    )
                    == 1
                )
        finally:
            engine.dispose()

        refused = _run_alembic(database_url, "downgrade", PREDECESSOR)
        assert refused.returncode != 0
        assert "cannot erase retained due action notification history" in (
            refused.stdout + refused.stderr
        )
