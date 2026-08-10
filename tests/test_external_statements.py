"""The External Party statement command is the sole behavioral seam for #217.

These tests exercise the record a coordinator and later readers receive, rather
than the command's helper calls or query shape.  Each fixture owns a real
Postgres transaction, matching the rest of the database test suite.
"""

from datetime import date
import hashlib

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.models import (
    Dependency,
    DependencyEvidenceSufficiency,
    DependencyEvent,
    DependencyEventScope,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.principals import HumanPrincipal


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def statement_record(session):
    project = Project(
        slug="external-statements-test",
        name="External statements test",
        is_synthetic=True,
    )
    party = ExternalOrg(name="Equistar")
    session.add_all([project, party])
    session.flush()
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"statement.pdf").hexdigest(),
        filename="statement.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-EQUISTAR-1",
        dep_type="utility_relocation",
        title="Equistar relocation",
        external_org_id=party.id,
        status="identified",
    )
    session.add_all([document, dependency])
    session.flush()
    return project, party, document, dependency


def test_unknown_scope_preserves_month_timing_without_a_dependency_projection(
    session, statement_record
):
    """Candidate 7129's January 2025 is not an invented January 1 date."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=None,
        description="Equistar to provide a chain of title (Due date of 01/2025).",
        new_timing=StatementTiming.month("01/2025", 2025, 1),
        scope=StatementScope.unknown(),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document_id=document.id,
            page_no=1,
            quote="Equistar to provide a chain of title (Due date of 01/2025).",
        ),
    )

    assert event.event_type == "commitment"
    assert event.project_id == project.id
    assert event.affected_external_org_id == party.id
    assert event.stated_external_org_id == party.id
    assert event.scope_mode == "unknown"
    assert event.new_timing.text == "01/2025"
    assert event.new_timing.precision == "month"
    assert event.new_timing.start_date == date(2025, 1, 1)
    assert event.new_timing.end_date == date(2025, 1, 31)
    assert session.scalars(
        select(DependencyEventScope).where(DependencyEventScope.event_id == event.id)
    ).all() == []
    session.refresh(dependency)
    assert dependency.committed_date is None


def test_one_exact_day_statement_links_each_selected_dependency_once_and_projects_it(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, first = statement_record
    second = Dependency(
        project_id=project.id,
        ref_code="DEP-EQUISTAR-2",
        dep_type="utility_relocation",
        title="Second Equistar relocation",
        external_org_id=party.id,
        status="identified",
    )
    session.add(second)
    session.flush()

    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((first.id, second.id)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, "Equistar will complete relocation."),
    )

    links = session.scalars(
        select(DependencyEventScope)
        .where(DependencyEventScope.event_id == event.id)
        .order_by(DependencyEventScope.dependency_id)
    ).all()
    assert [link.dependency_id for link in links] == [first.id, second.id]
    assert len(session.scalars(select(DependencyEvent)).all()) == 1
    [evidence] = session.scalars(
        select(EvidenceLink).where(EvidenceLink.event_id == event.id)
    ).all()
    assert evidence.dependency_id is None
    session.refresh(first)
    session.refresh(second)
    assert (first.committed_date, second.committed_date) == (
        date(2025, 6, 1),
        date(2025, 6, 1),
    )


def test_all_active_scope_is_a_snapshot_and_change_keeps_both_timings(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, first = statement_record
    second = Dependency(
        project_id=project.id,
        ref_code="DEP-EQUISTAR-2",
        dep_type="utility_relocation",
        title="Second Equistar relocation",
        external_org_id=party.id,
        status="identified",
    )
    session.add(second)
    session.flush()

    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 2, 1),
        description="The March 2026 completion timeline is unattainable. Propose May 16th.",
        previous_timing=StatementTiming.month("March 2026", 2026, 3),
        new_timing=StatementTiming.day("May 16th", date(2026, 5, 16)),
        scope=StatementScope.all_active(),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, "The March 2026 completion timeline is unattainable."),
    )
    later_dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-EQUISTAR-3",
        dep_type="utility_relocation",
        title="Later Equistar relocation",
        external_org_id=party.id,
        status="identified",
    )
    session.add(later_dependency)
    session.flush()

    assert event.event_type == "committed_date_change"
    assert event.timing_direction == "later"
    assert event.previous_timing.text == "March 2026"
    assert event.new_timing.text == "May 16th"
    assert set(
        session.scalars(
            select(DependencyEventScope.dependency_id).where(
                DependencyEventScope.event_id == event.id
            )
        )
    ) == {first.id, second.id}


def test_invalid_known_scope_refuses_before_writing_any_part_of_the_statement(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, _ = statement_record
    other_party = ExternalOrg(name="Other Utility")
    session.add(other_party)
    session.flush()
    wrong_dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-OTHER-1",
        dep_type="utility_relocation",
        title="Other Utility relocation",
        external_org_id=other_party.id,
        status="identified",
    )
    session.add(wrong_dependency)
    session.flush()

    with pytest.raises(StatementRefusal, match="another External Party"):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party="Equistar",
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=None,
            description="Equistar will complete relocation by 2025-06-01.",
            new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
            scope=StatementScope.selected((wrong_dependency.id,)),
            created_by="corridor:event-admission",
            evidence=CitedStatementEvidence(document.id, 1, "Equistar will complete relocation."),
        )
    assert session.scalars(select(DependencyEvent)).all() == []


def test_event_evidence_requires_an_explicit_dependency_sufficiency_judgment(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )
    from corridor.ledger import is_ready, mark_satisfies
    from corridor.ledger import load_dependency
    from corridor.operative_support import (
        designate_publication_support,
        resolve_operative_support,
    )

    project, party, document, dependency = statement_record
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, "Equistar will complete relocation."),
    )
    evidence = session.scalar(select(EvidenceLink).where(EvidenceLink.event_id == event.id))

    assert evidence.dependency_id is None
    assert is_ready(session, dependency.id) is False
    designate_publication_support(
        session,
        dependency.id,
        evidence.id,
        principal=HumanPrincipal("local:statement-reviewer"),
    )
    assert (
        resolve_operative_support(session, [dependency.id])[dependency.id]
        .publication
        .evidence_link_id
        == evidence.id
    )
    assert [link.id for link, _ in load_dependency(session, dependency.id).evidence] == [
        evidence.id
    ]
    assert mark_satisfies(
        session,
        dependency.id,
        evidence.id,
        principal=HumanPrincipal("local:statement-reviewer"),
    ) is True
    assert is_ready(session, dependency.id) is True
    assert session.scalar(
        select(DependencyEvidenceSufficiency).where(
            DependencyEvidenceSufficiency.dependency_id == dependency.id,
            DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
        )
    ) is not None


def test_database_rejects_scope_links_for_unknown_scope_and_empty_known_scope(
    session, statement_record
):
    project, party, _, dependency = statement_record
    unknown = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_external_org_id=party.id,
        scope_mode="unknown",
        event_type="commitment",
        source_kind="cited",
        stated_party="Equistar",
        description="A party-level statement.",
        created_by="corridor:event-admission",
    )
    session.add(unknown)
    session.flush()
    with pytest.raises(IntegrityError, match="unknown statement scope"):
        with session.begin_nested():
            session.add(DependencyEventScope(event_id=unknown.id, dependency_id=dependency.id))
            session.flush()

    with pytest.raises(IntegrityError, match="known statement scope has no"):
        with session.begin_nested():
            session.add(
                DependencyEvent(
                    project_id=project.id,
                    affected_external_org_id=party.id,
                    stated_external_org_id=party.id,
                    scope_mode="selected",
                    event_type="commitment",
                    source_kind="cited",
                    stated_party="Equistar",
                    description="A known-scope statement.",
                    created_by="corridor:event-admission",
                )
            )
            session.flush()
            session.execute(text("set constraints all immediate"))
