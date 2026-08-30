"""Rehearse #347's append-only checklist schema on real PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "b4d1e2f3a5c6"
HEAD = "b5d1e2f3a5c6"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_predecessor_upgrade_preserves_legacy_marks_and_guards_new_confirmations():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue347_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (id, slug, name, is_synthetic) "
                        "values (347, 'issue347', 'Issue 347', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into documents (id, project_id, sha256, filename, "
                        "doc_type, parse_status) values "
                        "(347, 347, '347doc', 'legacy.pdf', 'agreement', 'parsed')"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies (id, project_id, ref_code, dep_type, "
                        "title) values (347, 347, 'DOC-347', 'utility_relocation', 'Legacy')"
                    )
                )
                connection.execute(
                    text(
                        "insert into evidence_links (id, dependency_id, document_id, "
                        "page_no, quote, verified) values "
                        "(347, 347, 347, 1, 'legacy supporting passage', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependency_evidence_sufficiencies "
                        "(dependency_id, evidence_link_id, scope_link_id) "
                        "values (347, 347, null)"
                    )
                )
                assert connection.scalar(
                    text("select to_regclass('documentation_field_confirmations')")
                ) is None

            _upgrade(rendered, "head")

            with engine.begin() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert connection.scalar(
                    text("select to_regclass('documentation_field_confirmations')")
                ) == "documentation_field_confirmations"
                assert connection.scalar(
                    text(
                        "select count(*) from dependency_evidence_sufficiencies "
                        "where dependency_id = 347 and evidence_link_id = 347"
                    )
                ) == 1
                assert connection.scalar(
                    text("select cost_responsibility from dependencies where id = 347")
                ) is None
                connection.execute(
                    text(
                        "insert into documentation_field_confirmations "
                        "(id, dependency_id, evidence_link_id, field_name, classification, "
                        "conclusion, confirmed_by) values "
                        "(347, 347, 347, 'approval_interpretation', 'approved', "
                        "'approved', 'local:reviewer')"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies (id, project_id, ref_code, dep_type, "
                        "title) values (349, 347, 'DOC-349', 'utility_relocation', 'Other')"
                    )
                )

            # The composite foreign key is the database-bypass boundary: a
            # confirmation cannot cite another Constraint's passage even if a
            # caller bypasses the Python ownership check.
            with pytest.raises(Exception):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into documentation_field_confirmations "
                            "(id, dependency_id, evidence_link_id, field_name, "
                            "classification, conclusion, confirmed_by) values "
                            "(349, 349, 347, 'approval_interpretation', 'approved', "
                            "'approved', 'local:reviewer')"
                        )
                    )

            # The ADR-0060 override row is a lawful stored answer; any other
            # classification or a non-approved conclusion never reaches disk.
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into documentation_field_confirmations "
                        "(id, dependency_id, evidence_link_id, field_name, "
                        "classification, conclusion, confirmed_by) values "
                        "(350, 347, 347, 'approval_interpretation', 'conditional', "
                        "'approved', 'local:reviewer')"
                    )
                )
            for rejected in (
                "(351, 347, 347, 'approval_interpretation', 'rejected', "
                "'approved', 'local:reviewer')",
                "(352, 347, 347, 'approval_interpretation', 'conditional', "
                "'conditional', 'local:reviewer')",
            ):
                with pytest.raises(Exception):
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "insert into documentation_field_confirmations "
                                "(id, dependency_id, evidence_link_id, field_name, "
                                "classification, conclusion, confirmed_by) values "
                                + rejected
                            )
                        )

            for statement in (
                "update documentation_field_confirmations set conclusion = 'approved'",
                "delete from documentation_field_confirmations",
            ):
                with pytest.raises(Exception):
                    with engine.begin() as connection:
                        connection.execute(text(statement))
        finally:
            engine.dispose()


def test_downgrade_refuses_to_erase_documentation_confirmation_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue347_downgrade_",
        migration_revision=HEAD,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into projects (id, slug, name, is_synthetic) "
                        "values (348, 'issue347-downgrade', 'Issue 347 downgrade', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into documents (id, project_id, sha256, filename, "
                        "doc_type, parse_status) values "
                        "(348, 348, '348doc', 'approval.pdf', 'agreement', 'parsed')"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies (id, project_id, ref_code, dep_type, "
                        "title) values (348, 348, 'DOC-348', 'utility_relocation', 'Current')"
                    )
                )
                connection.execute(
                    text(
                        "insert into evidence_links (id, dependency_id, document_id, "
                        "page_no, quote, verified) values "
                        "(348, 348, 348, 1, 'approved', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into documentation_field_confirmations "
                        "(id, dependency_id, evidence_link_id, field_name, classification, "
                        "conclusion, confirmed_by) values "
                        "(348, 348, 348, 'approval_interpretation', 'approved', "
                        "'approved', 'local:reviewer')"
                    )
                )
            refused = subprocess.run(
                ["uv", "run", "alembic", "downgrade", PREDECESSOR],
                cwd=ROOT,
                env={**os.environ, "DATABASE_URL": rendered},
                capture_output=True,
                text=True,
            )
            assert refused.returncode != 0
            assert "cannot erase documentation confirmation history" in (
                refused.stdout + refused.stderr
            )
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
        finally:
            engine.dispose()
