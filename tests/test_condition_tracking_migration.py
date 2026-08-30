"""Rehearse #373's condition-tracking schema on real PostgreSQL (ADR-0060)."""

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
PREDECESSOR = "e7a2f4c9d1b6"
HEAD = "f362a1b2c3d4"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _seed_constraint(connection) -> None:
    connection.execute(
        text(
            "insert into projects (id, slug, name, is_synthetic) "
            "values (373, 'issue373', 'Issue 373', true)"
        )
    )
    connection.execute(
        text(
            "insert into documents (id, project_id, sha256, filename, doc_type, "
            "parse_status) values "
            "(373, 373, '373letter', 'approval.pdf', 'agreement', 'parsed'), "
            "(374, 373, '374later', 'inspection.pdf', 'agreement', 'parsed')"
        )
    )
    connection.execute(
        text(
            "insert into dependencies (id, project_id, ref_code, dep_type, title) "
            "values (373, 373, 'DOC-373', 'utility_relocation', 'Conditional')"
        )
    )
    connection.execute(
        text(
            "insert into evidence_links (id, dependency_id, document_id, page_no, "
            "quote, verified) values "
            "(373, 373, 373, 1, 'approved pending final inspection', true), "
            "(374, 373, 374, 1, 'the final inspection passed', true)"
        )
    )


def test_predecessor_upgrade_adds_condition_tracking_and_guards_it():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue373_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_constraint(connection)
                assert (
                    connection.scalar(text("select to_regclass('condition_resolutions')"))
                    is None
                )

            _upgrade(rendered, "head")

            with engine.begin() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                assert (
                    connection.scalar(text("select to_regclass('condition_resolutions')"))
                    == "condition_resolutions"
                )
                # The two override columns exist and default sanely.
                connection.execute(
                    text(
                        "insert into documentation_field_confirmations "
                        "(id, dependency_id, evidence_link_id, field_name, "
                        "classification, conclusion, confirmed_by) values "
                        "(373, 373, 373, 'approval_interpretation', 'approved', "
                        "'approved', 'local:reviewer')"
                    )
                )
                assert (
                    connection.scalar(
                        text(
                            "select condition_immaterial from "
                            "documentation_field_confirmations where id = 373"
                        )
                    )
                    is False
                )
                # A lawful override row records the hedge it overrode.
                connection.execute(
                    text(
                        "insert into documentation_field_confirmations "
                        "(id, dependency_id, evidence_link_id, field_name, "
                        "classification, conclusion, confirmed_by, condition_immaterial, "
                        "overridden_condition_text) values "
                        "(374, 373, 373, 'approval_interpretation', 'conditional', "
                        "'approved', 'local:reviewer', true, 'approved pending final inspection')"
                    )
                )

            # An override that names no hedge never reaches disk.
            with pytest.raises(Exception):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into documentation_field_confirmations "
                            "(id, dependency_id, evidence_link_id, field_name, "
                            "classification, conclusion, confirmed_by, condition_immaterial) "
                            "values (375, 373, 373, 'approval_interpretation', 'conditional', "
                            "'approved', 'local:reviewer', true)"
                        )
                    )

            # A lawful clear cites its basis and stays put; the composite FK
            # forbids a resolution about another Constraint's letter, and a
            # clear with no basis or a dismissal with no reason is refused.
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "insert into condition_resolutions "
                        "(id, dependency_id, evidence_link_id, kind, condition_text, "
                        "basis_evidence_link_id, resolved_by) values "
                        "(1, 373, 373, 'cleared', 'approved pending final inspection', "
                        "374, 'local:reviewer')"
                    )
                )
                connection.execute(
                    text(
                        "insert into dependencies (id, project_id, ref_code, dep_type, "
                        "title) values (999, 373, 'DOC-999', 'utility_relocation', 'Other')"
                    )
                )
            for bad in (
                # kind not in the enum
                "(2, 373, 373, 'wishful', 'x', 374, 'local:reviewer')",
                # a clear with no basis at all
                "(3, 373, 373, 'cleared', 'x', null, 'local:reviewer')",
                # a resolution about a passage this Constraint does not own
                "(4, 999, 373, 'cleared', 'x', 374, 'local:reviewer')",
            ):
                with pytest.raises(Exception):
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "insert into condition_resolutions (id, dependency_id, "
                                "evidence_link_id, kind, condition_text, "
                                "basis_evidence_link_id, resolved_by) values " + bad
                            )
                        )
            # A dismissal must record a reason.
            with pytest.raises(Exception):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into condition_resolutions (id, dependency_id, "
                            "evidence_link_id, kind, condition_text, resolved_by) values "
                            "(5, 373, 373, 'dismissed', 'x', 'local:reviewer')"
                        )
                    )

            # The history is append-only.
            for statement in (
                "update condition_resolutions set resolved_by = 'x'",
                "delete from condition_resolutions",
            ):
                with pytest.raises(Exception):
                    with engine.begin() as connection:
                        connection.execute(text(statement))
        finally:
            engine.dispose()


def test_downgrade_refuses_to_erase_condition_history():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue373_downgrade_",
        migration_revision=HEAD,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_constraint(connection)
                connection.execute(
                    text(
                        "insert into condition_resolutions "
                        "(id, dependency_id, evidence_link_id, kind, condition_text, "
                        "basis_evidence_link_id, resolved_by) values "
                        "(1, 373, 373, 'cleared', 'approved pending final inspection', "
                        "374, 'local:reviewer')"
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
            assert "cannot erase condition resolution history" in (
                refused.stdout + refused.stderr
            )
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
        finally:
            engine.dispose()
