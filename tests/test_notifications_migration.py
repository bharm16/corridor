"""Fresh and predecessor rehearsals for assignment-notification persistence (#351).

A stamped development database can report the expected head while missing a
trigger or a constraint, so these tests provision disposable databases: one
fresh to head to prove the schema objects and immutability triggers exist, and
one from the predecessor to head to prove a pre-existing assignment and its
project survive the upgrade, the new occurrence registers, its record is
immutable, and the history-preserving downgrade refuses to erase it.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from corridor import notifications
from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import (
    AssignmentNotification,
    ProjectRosterEntry,
    WorkDecision,
)
from corridor.principals import HumanPrincipal


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "f1c0d17e0a2b"
HEAD = "b7d3f9a1c2e5"

_TABLES = {
    "assignment_notifications",
    "assignment_notification_dispatches",
    "assignment_notification_attempts",
    "assignment_notification_feedback",
}
_TRIGGERS = {
    "assignment_notifications_are_immutable",
    "assignment_dispatch_identity_is_immutable",
    "assignment_attempts_are_immutable",
    "assignment_feedback_are_immutable",
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


def test_fresh_head_has_the_notification_schema_and_triggers():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="notif_fresh_",
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
                            "and table_name like 'assignment_notification%'"
                        )
                    ).all()
                )
                assert tables == _TABLES
                triggers = set(
                    connection.scalars(
                        text(
                            "select tgname from pg_trigger where not tgisinternal "
                            "and tgname like 'assignment_%'"
                        )
                    ).all()
                )
                assert _TRIGGERS.issubset(triggers)
        finally:
            engine.dispose()


def test_predecessor_upgrade_preserves_assignments_and_protects_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="notif_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _database_url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('notif-predecessor', 'Notif predecessor', true) "
                        "returning id"
                    )
                )
                dependency_id = connection.scalar(
                    text(
                        "insert into dependencies (project_id, ref_code, dep_type, title) "
                        "values (:project_id, 'NP-1', 'utility_relocation', 'Historic') "
                        "returning id"
                    ),
                    {"project_id": project_id},
                )
                roster_id = connection.scalar(
                    text(
                        "insert into project_roster_entries "
                        "(project_id, principal_subject, display_name, active) "
                        "values (:project_id, 'local:migration-assignee', 'Historic Owner', true) "
                        "returning id"
                    ),
                    {"project_id": project_id},
                )
                # A pre-existing assignment recorded before this feature existed.
                decision_id = connection.scalar(
                    text(
                        "insert into work_decisions "
                        "(dependency_id, decision_type, field, after_value, recorded_by) "
                        "values (:dep, 'assign_internal_owner', 'internal_owner', "
                        "'Historic Owner', 'local:migration-recorder') returning id"
                    ),
                    {"dep": dependency_id},
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
                # The pre-existing project, dependency, and assignment survived.
                assert (
                    session.scalar(
                        text("select count(*) from work_decisions where id=:id"),
                        {"id": decision_id},
                    )
                    == 1
                )
                # The new occurrence registers against the preserved assignment.
                decision = session.get(WorkDecision, decision_id)
                roster = session.get(ProjectRosterEntry, roster_id)
                notification = notifications.register_new_assignment_notification(
                    session,
                    assignment_decision=decision,
                    roster_entry=roster,
                    principal=HumanPrincipal("local:migration-recorder"),
                )
                session.commit()
                assert notification.subject_kind == "constraint"

                # The occurrence is immutable.
                with pytest.raises(ProgrammingError), session.begin_nested():
                    session.execute(
                        update(AssignmentNotification)
                        .where(AssignmentNotification.id == notification.id)
                        .values(category="new_assignment")
                    )
                assert (
                    session.scalar(
                        select(func.count()).select_from(AssignmentNotification)
                    )
                    == 1
                )
        finally:
            engine.dispose()

        refused = _run_alembic(database_url, "downgrade", PREDECESSOR)
        assert refused.returncode != 0
        assert "cannot erase retained assignment notification history" in (
            refused.stdout + refused.stderr
        )
