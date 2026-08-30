"""Migration rehearsal for the verbal precision/scope guard relaxation (#335).

The change replaces two deferred-constraint trigger function bodies and touches
no rows, so every existing verbal and documentary statement must survive with
its stored precision, actor, and source unchanged, and the head must still
require a verbal's conversation date while accepting the newly supported shapes.
"""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres

pytestmark = pytest.mark.slow


ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "e1f2a3b4c5d6"
HEAD = "a1b2c3d4e5f6"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _function_source(connection, name: str) -> str:
    return connection.scalar(
        text("select pg_get_functiondef(oid) from pg_proc where proname = :name"),
        {"name": name},
    )


def test_verbal_precision_relaxation_is_one_linear_head_on_a_fresh_database():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue335_fresh_",
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text("select version_num from alembic_version")
                ) == HEAD
                # The verbal shape guard keeps only the conversation-date rule.
                verbal_shape = _function_source(
                    connection, "validate_verbal_statement_shape"
                )
                assert "conversation date" in verbal_shape
                assert "exact-day commitment timing" not in verbal_shape
                # The scope guard no longer singles verbal statements out.
                scope_guard = _function_source(
                    connection, "validate_dependency_event_scope_decision"
                )
                assert "require one selected Dependency scope" not in scope_guard
                # The generic guards that reject impossible timing remain.
                assert connection.scalar(
                    text(
                        "select exists (select 1 from pg_constraint "
                        "where conname = 'ck_dependency_event_timing_bounds')"
                    )
                ) is True
        finally:
            engine.dispose()


def test_migration_preserves_existing_verbal_and_documentary_statements():
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )
    from corridor.models import DocPage, Document, ExternalOrg, Project

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue335_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        predecessor_url = make_url(settings.database_url).set(database=database.name)
        rendered = predecessor_url.render_as_string(hide_password=False)
        engine = create_engine(predecessor_url)
        try:
            with Session(engine) as session:
                project = Project(
                    slug="issue335-predecessor",
                    name="Issue 335 predecessor",
                    is_synthetic=True,
                )
                party = ExternalOrg(name="Equistar 335")
                session.add_all((project, party))
                session.flush()
                from corridor.models import Dependency

                dependency = Dependency(
                    project_id=project.id,
                    ref_code="EQ-LEGACY",
                    dep_type="utility_relocation",
                    title="Legacy verbal conflict",
                    external_org_id=party.id,
                )
                session.add(dependency)
                session.flush()
                quote = "Equistar to provide chain of title (Due date of 01/2025)."
                document = Document(
                    project_id=project.id,
                    sha256="c" * 64,
                    filename="minutes.pdf",
                    doc_type="minutes",
                    parse_status="parsed",
                )
                session.add(document)
                session.flush()
                session.add(DocPage(document_id=document.id, page_no=1, text=quote))
                session.flush()

                # A documentary (cited) month party-level statement.
                cited = record_external_party_statement(
                    session,
                    project_id=project.id,
                    affected_external_org_id=party.id,
                    stated_party="Equistar 335",
                    stated_external_org_id=party.id,
                    source_kind="cited",
                    event_date=date(2025, 1, 16),
                    description=quote,
                    new_timing=StatementTiming.month("01/2025", 2025, 1),
                    scope=StatementScope.unknown(),
                    created_by="corridor:event-admission",
                    evidence=CitedStatementEvidence(document.id, 1, quote),
                )
                # A legacy exact-day, single-Constraint verbal statement.
                verbal = record_external_party_statement(
                    session,
                    project_id=project.id,
                    affected_external_org_id=party.id,
                    stated_party="Equistar 335",
                    stated_external_org_id=party.id,
                    source_kind="verbal",
                    event_date=date(2025, 2, 3),
                    description="Equistar said August 15 by phone.",
                    new_timing=StatementTiming.day("2025-08-15", date(2025, 8, 15)),
                    scope=StatementScope.selected((dependency.id,)),
                    created_by="local:legacy-recorder",
                )
                cited_id, verbal_id = cited.id, verbal.id
                session.commit()

            _upgrade(rendered, "head")

            with engine.connect() as connection:
                assert connection.scalar(
                    text("select version_num from alembic_version")
                ) == HEAD
                # The documentary statement is preserved with its month precision.
                cited_row = connection.execute(
                    text(
                        "select event.source_kind, event.created_by, timing.precision, "
                        "timing.text, timing.start_date, timing.end_date "
                        "from dependency_events event "
                        "join dependency_event_timings timing "
                        "  on timing.event_id = event.id and timing.kind = 'new' "
                        "where event.id = :id"
                    ),
                    {"id": cited_id},
                ).one()
                assert cited_row.source_kind == "cited"
                assert cited_row.created_by == "corridor:event-admission"
                assert cited_row.precision == "month"
                assert cited_row.text == "01/2025"
                assert str(cited_row.start_date) == "2025-01-01"
                assert str(cited_row.end_date) == "2025-01-31"

                # The verbal statement keeps its actor, source, and exact-day timing.
                verbal_row = connection.execute(
                    text(
                        "select event.source_kind, event.created_by, event.event_date, "
                        "timing.precision, timing.start_date "
                        "from dependency_events event "
                        "join dependency_event_timings timing "
                        "  on timing.event_id = event.id and timing.kind = 'new' "
                        "where event.id = :id"
                    ),
                    {"id": verbal_id},
                ).one()
                assert verbal_row.source_kind == "verbal"
                assert verbal_row.created_by == "local:legacy-recorder"
                assert str(verbal_row.event_date) == "2025-02-03"
                assert verbal_row.precision == "day"
                assert str(verbal_row.start_date) == "2025-08-15"
        finally:
            engine.dispose()
