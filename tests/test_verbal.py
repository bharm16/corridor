"""A coordinator's stated phone call belongs on the record, not beside it."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from corridor.changes import record_run
from corridor.db import Session, engine
from corridor.dependency_events import project_committed_date
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import (
    AuditLog,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventTiming,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
    ReportRun,
)
from corridor.principals import HumanPrincipal
from corridor.report import (
    Assertion,
    Verbal,
    assert_no_bare_cells,
    build_report,
    render,
)
from corridor.verbal import VerbalRefusal, record_verbal
from corridor.web.app import app, get_human_principal, get_session


RECORDER = HumanPrincipal("local:phone-coordinator")


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
def dependency(session):
    project = Project(
        slug="verbal-test",
        name="Verbal Test",
        is_synthetic=True,
        project_side_parties=["Project Engineer"],
    )
    party = ExternalOrg(name="AT&T Texas", aliases=["AT&T"])
    session.add_all((project, party))
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code="TEL-1",
        dep_type="utility_relocation",
        title="Telecom conflict",
        external_org_id=party.id,
    )
    session.add(dependency)
    session.flush()
    return dependency


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _record_cited(
    session,
    dependency,
    document,
    *,
    event_date,
    committed_date,
    description,
    verified=True,
):
    """Create the exact-day cited compatibility fixture through #217's seam."""
    project = session.get(Project, dependency.project_id)
    party = session.get(ExternalOrg, dependency.external_org_id)
    if verified:
        return record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party="AT&T",
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=event_date,
            description=description,
            new_timing=StatementTiming.day(committed_date.isoformat(), committed_date),
            scope=StatementScope.selected((dependency.id,)),
            created_by="corridor:event-admission",
            evidence=CitedStatementEvidence(document.id, 1, "AT&T committed to June 15"),
        )

    # A reader must still fail closed when it reads an old incomplete cited
    # event. Production writes cannot create this shape.
    event = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_external_org_id=party.id,
        scope_mode="selected",
        event_type="commitment",
        source_kind="cited",
        stated_party="AT&T",
        event_date=event_date,
        description=description,
        created_by="corridor:event-admission",
    )
    session.add(event)
    session.flush()
    session.add_all(
        (
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text=committed_date.isoformat(),
                precision="day",
                start_date=committed_date,
                end_date=committed_date,
            ),
            DependencyEventScope(event_id=event.id, dependency_id=dependency.id),
        )
    )
    session.flush()
    return event


def test_recording_a_verbal_projects_its_date_and_attributes_the_call(
    session, dependency
):
    event = record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T said relocation will finish in August.",
        conversation_date=date(2026, 5, 8),
        committed_date=date(2026, 8, 15),
        principal=RECORDER,
    )

    assert event.source_kind == "verbal"
    assert event.stated_party == "AT&T"
    assert event.created_by == RECORDER.subject
    assert event.event_date == date(2026, 5, 8)
    assert event.new_timing.start_date == date(2026, 8, 15)
    assert event.event_type == "commitment"
    assert dependency.committed_date == date(2026, 8, 15)
    audit = session.scalars(
        select(AuditLog).where(AuditLog.entity_id == dependency.id)
    ).one()
    assert audit.action == "record_verbal"
    assert audit.actor == RECORDER.subject


def test_a_verbal_cannot_be_rewritten_or_deleted(session, dependency):
    event = record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T said relocation will finish in August.",
        conversation_date=date(2026, 5, 8),
        committed_date=date(2026, 8, 15),
        principal=RECORDER,
    )

    with pytest.raises(IntegrityError, match="verbal dependency events are append-only"):
        with session.begin_nested():
            event.description = "AT&T said relocation will finish in September."
            session.flush()

    with pytest.raises(IntegrityError, match="verbal dependency events are append-only"):
        with session.begin_nested():
            session.delete(event)
            session.flush()


def test_a_verbal_refuses_missing_required_call_facts(
    session, dependency
):
    with pytest.raises(VerbalRefusal, match="date the party gave"):
        record_verbal(
            session,
            dependency,
            stated_party="AT&T",
            description="AT&T gave no date.",
            conversation_date=date(2026, 5, 8),
            committed_date=None,
            principal=RECORDER,
        )


def test_a_later_verbal_is_another_commitment_without_inventing_a_change(
    session, dependency
):
    first = record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T said relocation will finish in June.",
        conversation_date=date(2026, 1, 8),
        committed_date=date(2026, 6, 15),
        principal=RECORDER,
    )
    later = record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T said relocation will finish in August.",
        conversation_date=date(2026, 5, 8),
        committed_date=date(2026, 8, 15),
        principal=RECORDER,
    )

    assert (first.event_type, later.event_type) == ("commitment", "commitment")
    assert [event.new_timing.start_date for event in session.scalars(
        select(DependencyEvent)
        .join(DependencyEventScope, DependencyEventScope.event_id == DependencyEvent.id)
        .where(DependencyEventScope.dependency_id == dependency.id)
        .order_by(DependencyEvent.event_date, DependencyEvent.id)
    )] == [date(2026, 6, 15), date(2026, 8, 15)]
    assert dependency.committed_date == date(2026, 8, 15)


@pytest.mark.parametrize(
    ("stated_party", "committed_date", "message"),
    [
        ("Project Engineer", date(2026, 8, 15), "project's own side"),
        ("Different Utility", date(2026, 8, 15), "registered alias"),
        ("AT&T", None, "date the party gave"),
    ],
)
def test_a_verbal_refuses_a_project_side_party_mismatch_or_missing_date(
    session, dependency, stated_party, committed_date, message
):
    with pytest.raises(VerbalRefusal, match=message):
        record_verbal(
            session,
            dependency,
            stated_party=stated_party,
            description="The party gave an update.",
            conversation_date=date(2026, 5, 8),
            committed_date=committed_date,
            principal=RECORDER,
        )


def test_a_verbal_refuses_a_dismissed_record(session, dependency):
    dependency.dismissed_at = datetime.now(timezone.utc)
    session.flush()

    with pytest.raises(VerbalRefusal, match="was dismissed"):
        record_verbal(
            session,
            dependency,
            stated_party="AT&T",
            description="AT&T said relocation will finish in August.",
            conversation_date=date(2026, 5, 8),
            committed_date=date(2026, 8, 15),
            principal=RECORDER,
        )


def test_a_coordinator_records_a_verbal_from_the_conflict_page(
    session, client, dependency
):
    project = session.get(Project, dependency.project_id)
    committed_date = date.today() - timedelta(days=1)

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/verbal",
        data={
            "stated_party": "AT&T",
            "description": "AT&T said relocation will finish in August.",
            "conversation_date": date.today().isoformat(),
            "committed_date": committed_date.isoformat(),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text
    assert "Verbal" in page
    assert "AT&amp;T" in page
    assert RECORDER.subject in page
    assert "AT&amp;T said relocation will finish in August." in page
    list_page = client.get(f"/ledger/{project.slug}").text
    assert "verbal" in list_page
    assert committed_date.isoformat() in list_page
    assert "OVERDUE" in list_page


def test_the_record_page_interleaves_cited_and_verbal_events_by_when_stated(
    session, client, dependency
):
    project = session.get(Project, dependency.project_id)
    cited_description = "The party committed to finish in June in the minutes."
    verbal_description = "The party said relocation will finish in August."
    document = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    _record_cited(
        session,
        dependency,
        document,
        event_date=date(2026, 1, 8),
        committed_date=date(2026, 6, 15),
        description=cited_description,
    )
    record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description=verbal_description,
        conversation_date=date(2026, 5, 8),
        committed_date=date(2026, 8, 15),
        principal=RECORDER,
    )

    page = client.get(f"/ledger/{project.slug}/{dependency.id}").text
    event_history = page[page.index("<h2>Events"):page.index("<h2>Evidence")]

    assert page.index(cited_description) < page.index(verbal_description)
    assert event_history.index("Cited statement") < event_history.index("Verbal")


def test_reports_mark_a_verbal_and_can_fall_back_to_a_cited_commitment(
    session, dependency
):
    project = session.get(Project, dependency.project_id)
    dependency.resolution_strategy = "relocate"
    minutes = Document(
        project_id=project.id,
        sha256="e" * 64,
        filename="minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(minutes)
    session.flush()
    session.add(DocPage(document_id=minutes.id, page_no=1, text="AT&T commitment"))
    _record_cited(
        session,
        dependency,
        minutes,
        event_date=date(2026, 1, 8),
        committed_date=date(2026, 6, 15),
        description="AT&T committed in the minutes.",
    )
    # This is a newer cited event, but it has no verified event Evidence.
    # The document-only surface must skip it and retain the earlier cited
    # statement that it can actually show its reader.
    _record_cited(
        session,
        dependency,
        minutes,
        event_date=date(2026, 4, 8),
        committed_date=date(2026, 7, 15),
        description="A later date appeared without verified support.",
        verified=False,
    )
    project_committed_date(session, dependency.id)
    record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T said relocation will finish in August.",
        conversation_date=date(2026, 5, 8),
        committed_date=date(2026, 8, 15),
        principal=RECORDER,
    )

    report = build_report(session, project.id, today=date(2026, 7, 1))
    committed = next(
        row[2]
        for section in report.sections
        if section.title == "Critical items"
        for row in section.rows
    )
    assert committed.value == "2026-08-15"
    assert isinstance(committed.provenance, Verbal)
    assert_no_bare_cells(report)

    document_only = build_report(
        session, project.id, today=date(2026, 7, 1), document_only=True
    )
    cited_committed = next(
        row[2]
        for section in document_only.sections
        if section.title == "Critical items"
        for row in section.rows
    )
    assert cited_committed.value == "2026-06-15"
    assert isinstance(cited_committed.provenance, Assertion)
    assert not any(
        isinstance(cell.provenance, Verbal) for cell in document_only.cells
    )
    aging = next(
        section for section in document_only.sections if section.title == "Aging"
    )
    assert [row[2].value for row in aging.rows] == ["2026-06-15"]
    assert "Document-only report" in render(document_only)
    assert_no_bare_cells(document_only)


def test_reports_withhold_an_unverified_cited_event_date(session, dependency):
    project = session.get(Project, dependency.project_id)
    dependency.resolution_strategy = "relocate"
    document = Document(
        project_id=project.id,
        sha256="b" * 64,
        filename="minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    _record_cited(
        session,
        dependency,
        document,
        event_date=date(2026, 1, 8),
        committed_date=date(2026, 6, 15),
        description="A date without verified event Evidence.",
        verified=False,
    )
    project_committed_date(session, dependency.id)

    report = build_report(session, project.id, today=date(2026, 7, 1))

    committed = next(
        row[2]
        for section in report.sections
        if section.title == "Critical items"
        for row in section.rows
    )
    assert committed.value == "—"
    assert "2026-06-15" not in render(report)
    assert_no_bare_cells(report)


def test_document_only_reports_keep_their_own_cited_history(
    session, dependency
):
    project = session.get(Project, dependency.project_id)
    dependency.resolution_strategy = "relocate"
    document = Document(
        project_id=project.id,
        sha256="f" * 64,
        filename="minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text="AT&T commitment"))
    _record_cited(
        session,
        dependency,
        document,
        event_date=date(2026, 1, 8),
        committed_date=date(2026, 6, 15),
        description="AT&T committed in the minutes.",
    )
    record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T said relocation will finish in August.",
        conversation_date=date(2026, 5, 8),
        committed_date=date(2026, 8, 15),
        principal=RECORDER,
    )

    normal = build_report(session, project.id, today=date(2026, 7, 1))
    record_run(
        session,
        project.id,
        evaluation=normal.evaluation,
        committed_dates=normal.committed_dates,
        document_only=normal.document_only,
    )
    document_only = build_report(
        session, project.id, today=date(2026, 7, 1), document_only=True
    )

    assert document_only.diff.is_first_report
    assert "2026-08-15" not in render(document_only)
    record_run(
        session,
        project.id,
        evaluation=document_only.evaluation,
        committed_dates=document_only.committed_dates,
        document_only=document_only.document_only,
    )
    stored = session.scalars(
        select(ReportRun)
        .where(
            ReportRun.project_id == project.id,
            ReportRun.document_only.is_(True),
        )
        .order_by(ReportRun.id.desc())
    ).one()
    assert (
        stored.snapshot_json["dependencies"][dependency.ref_code]["committed_date"]
        == "2026-06-15"
    )


def test_normal_report_change_marks_a_new_verbal_date(session, dependency):
    project = session.get(Project, dependency.project_id)
    document = Document(
        project_id=project.id,
        sha256="1" * 64,
        filename="minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    _record_cited(
        session,
        dependency,
        document,
        event_date=date(2026, 1, 8),
        committed_date=date(2026, 6, 15),
        description="AT&T committed in the minutes.",
    )
    project_committed_date(session, dependency.id)
    first = build_report(session, project.id, today=date(2026, 7, 1))
    record_run(
        session,
        project.id,
        evaluation=first.evaluation,
        committed_dates=first.committed_dates,
        document_only=first.document_only,
    )
    record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T said relocation will finish in August.",
        conversation_date=date(2026, 5, 8),
        committed_date=date(2026, 8, 15),
        principal=RECORDER,
    )

    report = build_report(session, project.id, today=date(2026, 7, 1))

    change = next(
        row
        for section in report.sections
        if section.title == "Changes since last report"
        for row in section.rows
        if row[1].value == "slipped"
    )
    assert isinstance(change[1].provenance, Verbal)
    assert isinstance(change[2].provenance, Verbal)
