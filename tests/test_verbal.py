"""A coordinator's stated phone call belongs on the record, not beside it."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
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
    CommitmentLineage,
    CommitmentScopeMembership,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
    ReportRun,
    WorkDecision,
)
from corridor.principals import HumanPrincipal
from access_support import seed_membership
from corridor.report import (
    Assertion,
    Verbal,
    assert_no_bare_cells,
    build_report,
    render,
)
from corridor.statement_lifecycle import observe_current_statement
from corridor.verbal import (
    StaleVerbalCorrection,
    VerbalRefusal,
    correct_verbal_scope,
    record_verbal,
    record_verbal_change,
    record_verbal_statement,
)
from corridor.work_list import build_work_list, party_commitment_due_after
from corridor.web.app import app, get_human_principal, get_session


RECORDER = HumanPrincipal("local:phone-coordinator")
_CITED_QUOTE = "AT&T committed to June 15"


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
    seed_membership(session, project, RECORDER)
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
        page = session.scalar(
            select(DocPage).where(
                DocPage.document_id == document.id,
                DocPage.page_no == 1,
            )
        )
        if page is None:
            session.add(DocPage(document_id=document.id, page_no=1, text=_CITED_QUOTE))
            session.flush()
        elif _CITED_QUOTE not in page.text:
            page.text = f"{page.text}\n{_CITED_QUOTE}"
            session.flush()
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
            evidence=CitedStatementEvidence(document.id, 1, _CITED_QUOTE),
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


def test_refused_verbal_recording_leaves_no_event_or_audit(session, dependency):
    """The Verbal adapter does not preserve an event when its receipt refuses."""
    project = session.get(Project, dependency.project_id)
    session.execute(
        text(
            """
            create function refuse_test_verbal_audit()
            returns trigger
            language plpgsql
            as $$
            begin
                if new.action = 'record_verbal'
                   and new.after_json->>'committed_date' = '2026-08-15' then
                    raise exception 'verbal audit refused' using errcode = '23514';
                end if;
                return new;
            end;
            $$;
            """
        )
    )
    session.execute(
        text(
            """
            create trigger refuse_test_verbal_audit
            before insert on audit_log
            for each row execute function refuse_test_verbal_audit();
            """
        )
    )

    with pytest.raises(IntegrityError, match="verbal audit refused"):
        record_verbal(
            session,
            dependency,
            stated_party="AT&T",
            description="AT&T said relocation will finish in August.",
            conversation_date=date(2026, 5, 8),
            committed_date=date(2026, 8, 15),
            principal=RECORDER,
        )

    assert session.scalars(
        select(DependencyEvent).where(DependencyEvent.project_id == project.id)
    ).all() == []
    assert session.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == dependency.id,
            AuditLog.action == "record_verbal",
        )
    ).all() == []
    session.refresh(dependency)
    assert dependency.committed_date is None

    event = record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T said relocation will finish in September.",
        conversation_date=date(2026, 5, 9),
        committed_date=date(2026, 9, 15),
        principal=RECORDER,
    )

    assert event.id is not None
    session.refresh(dependency)
    assert dependency.committed_date == date(2026, 9, 15)


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
    event_history = page[
        page.index("<h2>Statement history"):page.index("<h2>Supporting documents")
    ]

    assert page.index(cited_description) < page.index(verbal_description)
    assert event_history.index("Cited statement") < event_history.index("Recorded verbal statement")


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
        if section.title == "Relocation / removal / abandonment"
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
        if section.title == "Relocation / removal / abandonment"
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
        if section.title == "Relocation / removal / abandonment"
        for row in section.rows
    )
    assert committed.value == "—"
    assert "2026-06-15" not in render(report)
    assert_no_bare_cells(report)


def test_reports_do_not_publish_a_stale_day_after_a_current_month_statement(
    session, client, dependency
):
    """A current non-day statement still retires the older scalar projection."""
    project = session.get(Project, dependency.project_id)
    dependency.resolution_strategy = "relocate"
    document = Document(
        project_id=project.id,
        sha256="8" * 64,
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
        description="AT&T committed to June 15.",
    )
    page = session.scalar(
        select(DocPage).where(DocPage.document_id == document.id, DocPage.page_no == 1)
    )
    assert page is not None
    page.text = f"{page.text}\nAT&T now expects completion in August 2026."
    party = session.get(ExternalOrg, dependency.external_org_id)
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="AT&T",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2026, 5, 8),
        description="AT&T now expects completion in August 2026.",
        new_timing=StatementTiming.month("August 2026", 2026, 8),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "AT&T now expects completion in August 2026."
        ),
    )
    # Model an old import or interrupted projection refresh. The structured
    # statement remains authoritative even while this scalar is stale.
    dependency.committed_date = date(2026, 6, 15)
    session.flush()

    for document_only in (False, True):
        report = build_report(
            session,
            project.id,
            today=date(2026, 7, 1),
            document_only=document_only,
        )
        committed = next(
            row[2]
            for report_section in report.sections
            if report_section.title == "Relocation / removal / abandonment"
            for row in report_section.rows
        )

        assert report.committed_dates[dependency.id] is None
        assert committed.value == "—"
        assert "2026-06-15" not in render(report)
        assert report.evaluation is not None
        missing_date = next(
            exception for exception in report.evaluation.for_dependency(dependency.id)
            if exception.rule == "MISSING_DATE"
        )
        assert missing_date.label == "No exact promised date for this check"
        assert "No promised timing" not in render(report)
        assert_no_bare_cells(report)

    ledger_page = client.get(f"/ledger/{project.slug}").text
    assert "2026-06-15" not in ledger_page
    detail_page = client.get(f"/ledger/{project.slug}/{dependency.id}").text
    assert "August 2026" in detail_page
    assert "No exact promised date for this check" in detail_page


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
    rendered = render(document_only)
    assert '<td class="assertion">2026-06-15' in rendered
    assert '<td class="verbal">2026-08-15' not in rendered
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
        stored.snapshot_json["dependencies"][dependency.ref_code][
            "published_promised_for"
        ]
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
        if row[1].value == "Change to promised timing"
    )
    assert isinstance(change[1].provenance, Verbal)
    assert isinstance(change[2].provenance, Verbal)


# --- Recorded Verbal Statement at stated precision and scope (#335) -----------


def _org_dependency(session, dependency, ref_code, title="Another conflict"):
    """Another active Constraint for the same External Party."""
    extra = Dependency(
        project_id=dependency.project_id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title=title,
        external_org_id=dependency.external_org_id,
    )
    session.add(extra)
    session.flush()
    return extra


def _memberships(session, event):
    return set(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id).where(
                CommitmentScopeMembership.event_id == event.id
            )
        ).all()
    )


def test_a_verbal_preserves_month_precision_without_inventing_a_day(
    session, dependency
):
    project = session.get(Project, dependency.project_id)
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T said it will finish in January 2025.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.month("01/2025", 2025, 1),
        scope=StatementScope.selected((dependency.id,)),
        principal=RECORDER,
    )

    assert event.source_kind == "verbal"
    assert event.event_type == "commitment"
    assert event.new_timing.precision == "month"
    assert event.new_timing.text == "01/2025"
    assert event.new_timing.start_date == date(2025, 1, 1)
    assert event.new_timing.end_date == date(2025, 1, 31)
    # A month never becomes a scalar exact-day claim on the Constraint.
    session.refresh(dependency)
    assert dependency.committed_date is None
    # Past due only after the last day of the month.
    assert party_commitment_due_after(event.new_timing) == date(2025, 1, 31)


def test_a_verbal_preserves_approximate_timing_as_words_only(session, dependency):
    project = session.get(Project, dependency.project_id)
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T said relocation will finish around the end of May.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.approximate("end of May"),
        scope=StatementScope.selected((dependency.id,)),
        principal=RECORDER,
    )

    assert event.new_timing.precision == "approximate"
    assert event.new_timing.text == "end of May"
    assert event.new_timing.start_date is None
    assert event.new_timing.end_date is None
    # Approximate wording never manufactures an overdue boundary.
    assert party_commitment_due_after(event.new_timing) is None
    session.refresh(dependency)
    assert dependency.committed_date is None


def test_a_verbal_can_apply_to_several_named_constraints(session, dependency):
    project = session.get(Project, dependency.project_id)
    second = _org_dependency(session, dependency, "TEL-2")
    third = _org_dependency(session, dependency, "TEL-3")
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T spoke about two of the conflicts.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.day("2026-08-15", date(2026, 8, 15)),
        scope=StatementScope.selected((dependency.id, second.id)),
        principal=RECORDER,
    )

    assert _memberships(session, event) == {dependency.id, second.id}
    session.refresh(dependency)
    session.refresh(second)
    session.refresh(third)
    assert dependency.committed_date == date(2026, 8, 15)
    assert second.committed_date == date(2026, 8, 15)
    assert third.committed_date is None


def test_all_active_snapshot_does_not_expand_when_a_constraint_is_added_later(
    session, dependency
):
    project = session.get(Project, dependency.project_id)
    second = _org_dependency(session, dependency, "TEL-2")
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T spoke about all its current conflicts.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.month("March 2026", 2026, 3),
        scope=StatementScope.all_active(),
        principal=RECORDER,
    )
    assert _memberships(session, event) == {dependency.id, second.id}

    # A later Constraint for the same party must not silently join the snapshot.
    _org_dependency(session, dependency, "TEL-3")
    assert _memberships(session, event) == {dependency.id, second.id}


def test_unknown_scope_stays_party_level_with_no_constraint_effect(
    session, dependency
):
    project = session.get(Project, dependency.project_id)
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T made a party-level promise; scope not yet known.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.day("2026-01-15", date(2026, 1, 15)),
        scope=StatementScope.unknown(),
        principal=RECORDER,
    )

    assert event.scope_mode == "unknown"
    assert _memberships(session, event) == set()
    session.refresh(dependency)
    assert dependency.committed_date is None
    # The party-level fact still surfaces as its own work with no Constraint.
    work = build_work_list(session, project.id, today=date(2026, 6, 1))
    item = next(
        work_item
        for work_item in work.immediate
        if work_item.commitment_lineage_id == event.commitment_lineage_id
    )
    assert "unknown_scope" in item.attention_reason_codes
    assert item.dependency_id is None
    assert item.past_due is not None
    assert item.past_due.due_after == date(2026, 1, 15)


def test_a_stated_change_keeps_both_timings_and_derives_direction(
    session, dependency
):
    project = session.get(Project, dependency.project_id)
    first = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T committed to March 2026.",
        conversation_date=date(2026, 1, 8),
        new_timing=StatementTiming.month("March 2026", 2026, 3),
        scope=StatementScope.selected((dependency.id,)),
        principal=RECORDER,
    )
    change = record_verbal_change(
        session,
        commitment_lineage_id=first.commitment_lineage_id,
        stated_party="AT&T",
        description="AT&T now says May 16, 2026.",
        conversation_date=date(2026, 4, 8),
        new_timing=StatementTiming.day("May 16, 2026", date(2026, 5, 16)),
        principal=RECORDER,
    )

    assert change.event_type == "committed_date_change"
    assert change.timing_direction == "later"
    assert change.previous_timing.precision == "month"
    assert change.previous_timing.start_date == date(2026, 3, 1)
    assert change.new_timing.start_date == date(2026, 5, 16)
    assert change.commitment_lineage_id == first.commitment_lineage_id
    # The prior commitment is preserved, superseded, not rewritten.
    assert session.get(DependencyEvent, first.id).description == (
        "AT&T committed to March 2026."
    )
    assert change.supersedes_event_id == first.id
    # Scope carried forward from the predecessor, not re-snapshotted.
    assert _memberships(session, change) == {dependency.id}


def test_a_fresh_verbal_is_a_commitment_even_when_a_cached_date_exists(
    session, dependency
):
    """A fresh promise is never a change just because a scalar is cached."""
    dependency.committed_date = date(2026, 3, 1)
    session.flush()
    project = session.get(Project, dependency.project_id)
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T's first attributable statement.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.day("2026-08-15", date(2026, 8, 15)),
        scope=StatementScope.selected((dependency.id,)),
        principal=RECORDER,
    )

    assert event.event_type == "commitment"
    assert event.previous_timing is None
    assert event.timing_direction is None


def test_a_scope_correction_preserves_the_original_verbal_and_projects(
    session, dependency
):
    project = session.get(Project, dependency.project_id)
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="Party-level promise pending scope.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.day("2026-08-15", date(2026, 8, 15)),
        scope=StatementScope.unknown(),
        principal=RECORDER,
    )
    observation = observe_current_statement(session, event.commitment_lineage_id)
    decision = correct_verbal_scope(
        session,
        event_id=event.id,
        scope=StatementScope.selected((dependency.id,)),
        expected_scope_decision_id=observation.scope_decision.id,
        principal=RECORDER,
    )

    assert decision.scope_mode == "selected"
    assert decision.supersedes_scope_decision_id == observation.scope_decision.id
    # The statement fact itself is untouched, only its scope decision appended.
    assert session.get(DependencyEvent, event.id) is not None
    session.refresh(dependency)
    assert dependency.committed_date == date(2026, 8, 15)
    audit = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "correct_statement_scope",
            AuditLog.entity_id == event.commitment_lineage_id,
        )
    ).one()
    assert audit.actor == RECORDER.subject


def test_a_scope_correction_refuses_a_stale_expected_decision(session, dependency):
    project = session.get(Project, dependency.project_id)
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="Party-level promise pending scope.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.day("2026-08-15", date(2026, 8, 15)),
        scope=StatementScope.unknown(),
        principal=RECORDER,
    )
    with pytest.raises(StaleVerbalCorrection, match="reload the newer state"):
        correct_verbal_scope(
            session,
            event_id=event.id,
            scope=StatementScope.selected((dependency.id,)),
            expected_scope_decision_id=999999,
            principal=RECORDER,
        )
    # Nothing changed: still party-level, still no membership.
    assert _memberships(session, event) == set()


def test_a_verbal_change_marks_the_plan_for_review_without_copying_to_constraints(
    session, dependency
):
    project = session.get(Project, dependency.project_id)
    first = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T committed to March 2026.",
        conversation_date=date(2026, 1, 8),
        new_timing=StatementTiming.month("March 2026", 2026, 3),
        scope=StatementScope.selected((dependency.id,)),
        principal=RECORDER,
    )
    lineage = session.get(CommitmentLineage, first.commitment_lineage_id)
    # A statement-level plan lives on the lineage, never on the Constraint.
    session.add(
        WorkDecision(
            commitment_lineage_id=lineage.id,
            decision_type="assign_internal_owner",
            field="internal_owner",
            before_value=None,
            after_value="Dana Fields",
            recorded_by=RECORDER.subject,
        )
    )
    lineage.internal_owner = "Dana Fields"
    session.flush()

    record_verbal_change(
        session,
        commitment_lineage_id=lineage.id,
        stated_party="AT&T",
        description="AT&T now says May 16, 2026.",
        conversation_date=date(2026, 4, 8),
        new_timing=StatementTiming.day("May 16, 2026", date(2026, 5, 16)),
        principal=RECORDER,
    )

    session.refresh(lineage)
    assert lineage.plan_needs_review is True
    # The plan stayed on the lineage; no Constraint-subject decision was copied.
    constraint_decisions = session.scalars(
        select(WorkDecision).where(WorkDecision.dependency_id == dependency.id)
    ).all()
    assert constraint_decisions == []


def test_a_selected_verbal_scope_cannot_cross_projects(session, dependency):
    project = session.get(Project, dependency.project_id)
    other_project = Project(
        slug="verbal-other",
        name="Other",
        is_synthetic=True,
    )
    other_org = ExternalOrg(name="Bell South", aliases=["Bell"])
    session.add_all((other_project, other_org))
    session.flush()
    other_dependency = Dependency(
        project_id=other_project.id,
        ref_code="OTHER-1",
        dep_type="utility_relocation",
        title="Elsewhere",
        external_org_id=other_org.id,
    )
    session.add(other_dependency)
    session.flush()

    with pytest.raises(VerbalRefusal, match="cross projects|another External Party"):
        record_verbal_statement(
            session,
            project_id=project.id,
            external_org_id=dependency.external_org_id,
            stated_party="AT&T",
            description="Crossing the project boundary.",
            conversation_date=date(2026, 5, 8),
            new_timing=StatementTiming.day("2026-08-15", date(2026, 8, 15)),
            scope=StatementScope.selected((other_dependency.id,)),
            principal=RECORDER,
        )
    assert session.scalars(
        select(DependencyEvent).where(DependencyEvent.project_id == project.id)
    ).all() == []


def test_direct_db_bypass_of_a_verbal_without_a_conversation_date_is_rejected(
    session, dependency
):
    """The extended guard still requires a verbal to carry its conversation date."""
    project = session.get(Project, dependency.project_id)
    party = session.get(ExternalOrg, dependency.external_org_id)
    with pytest.raises(IntegrityError, match="conversation date"):
        with session.begin_nested():
            event = DependencyEvent(
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_external_org_id=party.id,
                scope_mode="unknown",
                event_type="commitment",
                source_kind="verbal",
                stated_party="AT&T",
                event_date=None,
                description="bypass without a conversation date",
                created_by=RECORDER.subject,
            )
            session.add(event)
            session.flush()
            session.add(
                DependencyEventTiming(
                    event_id=event.id,
                    kind="new",
                    text="2026-08-15",
                    precision="day",
                    start_date=date(2026, 8, 15),
                    end_date=date(2026, 8, 15),
                )
            )
            session.flush()
            # Deferred constraint triggers validate the final shape at commit;
            # force them to fire now so the bypass is caught in the test.
            session.execute(text("set constraints all immediate"))


def test_direct_db_bypass_of_a_committed_date_change_missing_a_previous_timing(
    session, dependency
):
    """A change verbal with only a new timing is still refused by the guard."""
    project = session.get(Project, dependency.project_id)
    party = session.get(ExternalOrg, dependency.external_org_id)
    with pytest.raises(IntegrityError, match="previous and new timings"):
        with session.begin_nested():
            event = DependencyEvent(
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_external_org_id=party.id,
                scope_mode="unknown",
                event_type="committed_date_change",
                source_kind="verbal",
                stated_party="AT&T",
                event_date=date(2026, 5, 8),
                description="a change that never states what changed",
                created_by=RECORDER.subject,
            )
            session.add(event)
            session.flush()
            session.add(
                DependencyEventTiming(
                    event_id=event.id,
                    kind="new",
                    text="2026-08-15",
                    precision="day",
                    start_date=date(2026, 8, 15),
                    end_date=date(2026, 8, 15),
                )
            )
            session.flush()
            session.execute(text("set constraints all immediate"))


def test_http_records_a_party_level_month_verbal(session, client, dependency):
    project = session.get(Project, dependency.project_id)

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/verbal",
        data={
            "stated_party": "AT&T",
            "description": "AT&T made a party-level promise for January 2025.",
            "conversation_date": date(2026, 5, 8).isoformat(),
            "timing_precision": "month",
            "committed_month": "2025-01",
            "scope_mode": "unknown",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    event = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.project_id == project.id,
            DependencyEvent.source_kind == "verbal",
        )
    ).one()
    assert event.new_timing.precision == "month"
    assert event.new_timing.start_date == date(2025, 1, 1)
    assert event.scope_mode == "unknown"
    assert _memberships(session, event) == set()
    session.refresh(dependency)
    assert dependency.committed_date is None


def test_http_approximate_verbal_does_not_require_a_day(session, client, dependency):
    project = session.get(Project, dependency.project_id)

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/verbal",
        data={
            "stated_party": "AT&T",
            "description": "AT&T said roughly mid-year.",
            "conversation_date": date(2026, 5, 8).isoformat(),
            "timing_precision": "approximate",
            "timing_text": "around mid-year",
            "scope_mode": "this",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    event = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.project_id == project.id,
            DependencyEvent.source_kind == "verbal",
        )
    ).one()
    assert event.new_timing.precision == "approximate"
    assert event.new_timing.text == "around mid-year"


def test_http_records_several_constraints(session, client, dependency):
    project = session.get(Project, dependency.project_id)
    second = _org_dependency(session, dependency, "TEL-2")

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/verbal",
        data={
            "stated_party": "AT&T",
            "description": "AT&T spoke about two conflicts.",
            "conversation_date": date(2026, 5, 8).isoformat(),
            "timing_precision": "day",
            "committed_date": date(2026, 8, 15).isoformat(),
            "scope_mode": "selected",
            "scope_dependency_ids": [str(dependency.id), str(second.id)],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    event = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.project_id == project.id,
            DependencyEvent.source_kind == "verbal",
        )
    ).one()
    assert _memberships(session, event) == {dependency.id, second.id}


def test_http_records_a_stated_change(session, client, dependency):
    project = session.get(Project, dependency.project_id)
    first = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T committed to March 2026.",
        conversation_date=date(2026, 1, 8),
        new_timing=StatementTiming.month("March 2026", 2026, 3),
        scope=StatementScope.selected((dependency.id,)),
        principal=RECORDER,
    )
    session.commit()

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/verbal-change",
        data={
            "commitment_lineage_id": str(first.commitment_lineage_id),
            "stated_party": "AT&T",
            "description": "AT&T now says May 16, 2026.",
            "conversation_date": date(2026, 4, 8).isoformat(),
            "timing_precision": "day",
            "committed_date": date(2026, 5, 16).isoformat(),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    change = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.event_type == "committed_date_change",
            DependencyEvent.project_id == project.id,
        )
    ).one()
    assert change.timing_direction == "later"
    assert change.previous_timing.start_date == date(2026, 3, 1)


def test_http_verbal_change_refuses_a_lineage_from_another_project(
    session, client, dependency
):
    project = session.get(Project, dependency.project_id)
    other_project = Project(slug="verbal-other-2", name="Other", is_synthetic=True)
    other_org = ExternalOrg(name="Bell", aliases=["Bell"])
    session.add_all((other_project, other_org))
    session.flush()
    other_dependency = Dependency(
        project_id=other_project.id,
        ref_code="OTHER-9",
        dep_type="utility_relocation",
        title="Elsewhere",
        external_org_id=other_org.id,
    )
    session.add(other_dependency)
    session.flush()
    seed_membership(session, other_project, RECORDER)
    foreign = record_verbal_statement(
        session,
        project_id=other_project.id,
        external_org_id=other_org.id,
        stated_party="Bell",
        description="Bell committed elsewhere.",
        conversation_date=date(2026, 1, 8),
        new_timing=StatementTiming.day("2026-03-01", date(2026, 3, 1)),
        scope=StatementScope.selected((other_dependency.id,)),
        principal=RECORDER,
    )
    session.commit()

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/verbal-change",
        data={
            "commitment_lineage_id": str(foreign.commitment_lineage_id),
            "stated_party": "Bell",
            "description": "trying to reach another project's commitment",
            "conversation_date": date(2026, 4, 8).isoformat(),
            "timing_precision": "day",
            "committed_date": date(2026, 5, 16).isoformat(),
        },
        follow_redirects=False,
    )

    assert response.status_code == 404
    # The foreign commitment is untouched.
    assert observe_current_statement(
        session, foreign.commitment_lineage_id
    ).event.id == foreign.id


def test_http_corrects_verbal_scope(session, client, dependency):
    project = session.get(Project, dependency.project_id)
    event = record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="Party-level promise pending scope.",
        conversation_date=date(2026, 5, 8),
        new_timing=StatementTiming.day("2026-08-15", date(2026, 8, 15)),
        scope=StatementScope.unknown(),
        principal=RECORDER,
    )
    observation = observe_current_statement(session, event.commitment_lineage_id)
    session.commit()

    response = client.post(
        f"/ledger/{project.slug}/{dependency.id}/verbal-scope",
        data={
            "statement_event_id": str(event.id),
            "expected_scope_decision_id": str(observation.scope_decision.id),
            "scope_mode": "this",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _memberships(session, event) == {dependency.id}
    session.refresh(dependency)
    assert dependency.committed_date == date(2026, 8, 15)
