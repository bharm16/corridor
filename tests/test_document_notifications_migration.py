"""Fresh and predecessor rehearsals for document-notification persistence (#353).

A stamped development database can report the expected head while missing a
trigger or a constraint, so these tests provision disposable databases: one
fresh to head to prove the schema objects and immutability triggers exist, and
one from the predecessor to head to prove a pre-existing project survives the
upgrade, a new occurrence registers, its record is immutable, and the
history-preserving downgrade refuses to erase it.
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

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import (
    DocumentNotification,
    DocumentNotificationDispatch,
)


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "a1b2c3d4e5f6"
HEAD = "b7d3f9a1c2e5"

_TABLES = {
    "document_notifications",
    "document_notification_dispatches",
    "document_notification_attempts",
}
_TRIGGERS = {
    "document_notifications_are_immutable",
    "document_dispatch_identity_is_immutable",
    "document_attempts_are_immutable",
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


def test_fresh_head_has_the_document_notification_schema_and_triggers():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="docnotif_fresh_",
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
                            "and table_name like 'document_notification%'"
                        )
                    ).all()
                )
                assert tables == _TABLES
                triggers = set(
                    connection.scalars(
                        text(
                            "select tgname from pg_trigger where not tgisinternal "
                            "and tgname like 'document_%'"
                        )
                    ).all()
                )
                assert _TRIGGERS.issubset(triggers)
        finally:
            engine.dispose()


def test_predecessor_upgrade_registers_and_protects_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="docnotif_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _database_url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('docnotif-predecessor', 'Doc notif predecessor', true) "
                        "returning id"
                    )
                )
                dependency_id = connection.scalar(
                    text(
                        "insert into dependencies (project_id, ref_code, dep_type, title) "
                        "values (:project_id, 'DN-1', 'utility_relocation', 'Historic') "
                        "returning id"
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
                # The pre-existing project and dependency survived the upgrade.
                assert (
                    session.scalar(
                        text("select count(*) from dependencies where id=:id"),
                        {"id": dependency_id},
                    )
                    == 1
                )
                # A new occurrence registers against the preserved subject and
                # carries an authentic proven-transition identity.
                notification = DocumentNotification(
                    public_id="document-notification:migration",
                    project_id=project_id,
                    category="document_change",
                    subject_kind="constraint",
                    dependency_id=dependency_id,
                    recipient_principal_subject="local:migration-owner",
                    recipient_role="current_assignee",
                    comparison_id=1,
                    finding_id=1,
                    reason_code="comparison_changed",
                    occurrence_key="0" * 64,
                    registered_by="runtime:migration",
                )
                session.add(notification)
                session.flush([notification])
                session.add(
                    DocumentNotificationDispatch(
                        public_id="document-dispatch:migration",
                        notification_id=notification.id,
                        project_id=project_id,
                        channel="email",
                        delivery_state="queued",
                        attempt_count=0,
                        idempotency_key="f" * 64,
                    )
                )
                session.commit()

                # The occurrence is immutable.
                with pytest.raises(ProgrammingError), session.begin_nested():
                    session.execute(
                        update(DocumentNotification)
                        .where(DocumentNotification.id == notification.id)
                        .values(reason_code="comparison_dropped")
                    )
                assert (
                    session.scalar(
                        select(func.count()).select_from(DocumentNotification)
                    )
                    == 1
                )
        finally:
            engine.dispose()

        refused = _run_alembic(database_url, "downgrade", PREDECESSOR)
        assert refused.returncode != 0
        assert "cannot erase retained document notification history" in (
            refused.stdout + refused.stderr
        )
