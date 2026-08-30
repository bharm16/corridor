"""Fresh and predecessor rehearsals for retained organization identity history."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "b4d1e2f3a5c6"
HEAD = "e4c8b1a6d3f7"


def _url(database) -> str:
    return make_url(settings.database_url).set(database=database.name).render_as_string(
        hide_password=False
    )


def _alembic(database_url: str, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def test_organization_identity_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="organization_identity_fresh_",
    ) as database:
        engine = create_engine(_url(database))
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert connection.scalar(
                    text("select to_regclass('public.organization_identity_receipts')")
                ) is not None
                assert connection.scalar(
                    text("select to_regclass('public.organization_identity_activations')")
                ) is not None
                receipt_checks = set(
                    connection.scalars(
                        text(
                            "select conname from pg_constraint where conrelid = "
                            "'organization_identity_receipts'::regclass"
                        )
                    ).all()
                )
                assert {
                    "ck_organization_identity_receipt_method",
                    "ck_organization_identity_receipt_scope",
                }.issubset(receipt_checks)
                receipt_triggers = set(
                    connection.scalars(
                        text(
                            "select tgname from pg_trigger where not tgisinternal "
                            "and tgrelid = 'organization_identity_receipts'::regclass"
                        )
                    ).all()
                )
                assert receipt_triggers == {
                    "organization_identity_receipts_are_immutable",
                    "organization_identity_receipts_reject_truncate",
                }
        finally:
            engine.dispose()


def test_predecessor_upgrade_preserves_projects_and_retained_history_blocks_downgrade():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="organization_identity_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert connection.scalar(
                    text("select to_regclass('public.organization_identity_receipts')")
                ) is None
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('organization-identity-predecessor', "
                        "'Organization identity predecessor', true) returning id"
                    )
                )
        finally:
            engine.dispose()
        bound = database.session_factory.kw.get("bind")
        if bound is not None:
            bound.dispose()

        upgraded = _alembic(database_url, "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert connection.scalar(
                    text("select count(*) from projects where id = :id"), {"id": project_id}
                ) == 1
                activation_id = connection.scalar(
                    text(
                        "insert into organization_identity_activations "
                        "(project_id, action, policy_version, policy_sha256, "
                        "replay_case_count, reason, recorded_by) values "
                        "(:project_id, 'activate', 'organization-identity-v1', :sha, "
                        "1, 'recorded replay passed', 'corridor:test') returning id"
                    ),
                    {"project_id": project_id, "sha": "a" * 64},
                )
                with pytest.raises(ProgrammingError), connection.begin_nested():
                    connection.execute(
                        text(
                            "update organization_identity_activations set "
                            "reason = 'rewritten' where id = :id"
                        ),
                        {"id": activation_id},
                    )
        finally:
            engine.dispose()

        downgraded = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", PREDECESSOR],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": database_url},
            capture_output=True,
            text=True,
        )
        assert downgraded.returncode != 0
        assert "cannot erase retained organization identity history" in (
            downgraded.stdout + downgraded.stderr
        )
