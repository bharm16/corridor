"""Exercise #217's migration boundary on a disposable PostgreSQL database."""

from __future__ import annotations

from datetime import date
import hashlib
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.legacy_ledger_archive import plan_retirement, retire_legacy_ledger
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    DependencyEvent,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)


_ROOT = Path(__file__).resolve().parents[1]
_PRE_STATEMENT_REVISION = "e216f5a4b3c2"


def _run_alembic(database_url: str, command: str, revision: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "-c", str(_ROOT / "alembic.ini"), command, revision],
        cwd=_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _assert_rejected(session_factory, statement: str, parameters: dict[str, int]) -> None:
    with session_factory() as session, pytest.raises(DBAPIError, match="append-only"):
        session.execute(text(statement), parameters)


def test_external_statement_migration_preserves_verbal_slips_and_seals_cited_rows():
    """A safe #216 Verbal downgrades, upgrades, and remains append-only."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a217_statement_",
    ) as database:
        database_url = make_url(settings.database_url).set(
            database=database.name
        ).render_as_string(hide_password=False)
        session_factory = database.session_factory

        with session_factory() as session:
            project = Project(
                slug="a217-statement-migration",
                name="A217 statement migration",
                is_synthetic=True,
            )
            party = ExternalOrg(name="Equistar")
            session.add_all([project, party])
            session.flush()
            document = Document(
                project_id=project.id,
                sha256=hashlib.sha256(b"a217 statement migration").hexdigest(),
                filename="a217-statement-migration.pdf",
                doc_type="minutes",
                parse_status="parsed",
            )
            session.add(document)
            session.flush()
            candidate = Candidate(
                project_id=project.id,
                kind="dependency",
                payload_json={
                    "kind": "dependency",
                    "fields": {"utility_id": "A217-1"},
                    "citations": [],
                },
                source_document_id=document.id,
                source_pages=[1],
                confidence=1.0,
                prompt_version="legacy-v1",
                model="legacy-model",
                citations_verified=True,
                state="accepted",
            )
            dependency = Dependency(
                project_id=project.id,
                ref_code="DEP-A217-1",
                dep_type="utility_relocation",
                title="Equistar relocation",
                external_org_id=party.id,
                status="identified",
            )
            session.add_all([candidate, dependency])
            session.flush()
            session.add(
                AuditLog(
                    actor="agent",
                    action="accept_candidate",
                    entity_type="dependency",
                    entity_id=dependency.id,
                    after_json={"candidate_id": candidate.id},
                )
            )
            verbal = record_external_party_statement(
                session,
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_party="Equistar",
                stated_external_org_id=party.id,
                source_kind="verbal",
                event_date=date(2025, 1, 16),
                description="Equistar said it will complete by June 1.",
                new_timing=StatementTiming.day("June 1", date(2025, 6, 1)),
                scope=StatementScope.selected((dependency.id,)),
                created_by="local:a217-recorder",
            )
            ids = {
                "project": project.id,
                "party": party.id,
                "document": document.id,
                "dependency": dependency.id,
                "verbal": verbal.id,
            }
            session.commit()

        _run_alembic(database_url, "downgrade", _PRE_STATEMENT_REVISION)
        legacy_engine = create_engine(database_url)
        try:
            with legacy_engine.begin() as connection:
                connection.execute(
                    text(
                        "alter table dependency_events disable trigger "
                        "verbal_dependency_events_are_immutable"
                    )
                )
                connection.execute(
                    text(
                        "update dependency_events set event_type = 'slip' "
                        "where id = :event_id"
                    ),
                    {"event_id": ids["verbal"]},
                )
                connection.execute(
                    text(
                        "alter table dependency_events enable trigger "
                        "verbal_dependency_events_are_immutable"
                    )
                )
        finally:
            legacy_engine.dispose()

        _run_alembic(database_url, "upgrade", "head")
        with session_factory() as session:
            verbal = session.get(DependencyEvent, ids["verbal"])
            assert verbal is not None
            assert verbal.event_type == "commitment"
            assert verbal.new_timing.precision == "day"
            assert verbal.new_timing.start_date == verbal.new_timing.end_date == date(
                2025, 6, 1
            )

        # This is the safe #216-compatible downgrade that must not be stopped
        # by the inherited Verbal append-only trigger.
        _run_alembic(database_url, "downgrade", _PRE_STATEMENT_REVISION)
        legacy_engine = create_engine(database_url)
        try:
            with legacy_engine.connect() as connection:
                assert connection.scalar(
                    text(
                        "select committed_date from dependency_events "
                        "where id = :event_id"
                    ),
                    {"event_id": ids["verbal"]},
                ) == date(2025, 6, 1)
        finally:
            legacy_engine.dispose()

        _run_alembic(database_url, "upgrade", "head")
        with session_factory() as session:
            cited = record_external_party_statement(
                session,
                project_id=ids["project"],
                affected_external_org_id=ids["party"],
                stated_party="Equistar",
                stated_external_org_id=ids["party"],
                source_kind="cited",
                event_date=date(2025, 1, 16),
                description="Equistar will complete by June 1.",
                new_timing=StatementTiming.day("June 1", date(2025, 6, 1)),
                scope=StatementScope.selected((ids["dependency"],)),
                created_by="corridor:event-admission",
                evidence=CitedStatementEvidence(
                    document_id=ids["document"],
                    page_no=1,
                    quote="Equistar will complete by June 1.",
                ),
            )
            ids["cited"] = cited.id
            ids["timing"] = cited.new_timing.id
            ids["scope"] = cited.scope_links[0].id
            ids["evidence"] = session.scalar(
                select(EvidenceLink.id).where(EvidenceLink.event_id == cited.id)
            )
            session.commit()

        _assert_rejected(
            session_factory,
            "update dependency_events set description = 'rewritten' where id = :event_id",
            {"event_id": ids["cited"]},
        )
        _assert_rejected(
            session_factory,
            "delete from dependency_event_timings where id = :timing_id",
            {"timing_id": ids["timing"]},
        )
        _assert_rejected(
            session_factory,
            "delete from dependency_event_scopes where id = :scope_id",
            {"scope_id": ids["scope"]},
        )
        _assert_rejected(
            session_factory,
            "update evidence_links set quote = 'rewritten' where id = :evidence_id",
            {"evidence_id": ids["evidence"]},
        )

        with session_factory() as session:
            plan = plan_retirement(session, ids["project"])
            retire_legacy_ledger(
                session,
                ids["project"],
                expected_sha256=plan.content_sha256,
                expected_dependency_count=1,
            )
            session.commit()
            assert session.scalars(
                select(Dependency).where(Dependency.project_id == ids["project"])
            ).all() == []
            assert session.scalars(
                select(DependencyEvent).where(
                    DependencyEvent.project_id == ids["project"]
                )
            ).all() == []
