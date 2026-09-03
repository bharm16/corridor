"""The change summary and weekly Coordination Report, from one frozen revision (#534).

ADR-0086 makes one authorized issue the external unit and gives every artifact
in it the same five inputs.  These tests hold the renderers to that: both read
one bound reading, the comparison baseline moves only on an approved issue, an
unaccepted Proposed Delta never reaches an accepted-change section, and a
re-render of the same bound reading says the same thing.

Every time in these tests is supplied by the caller.  One test renders the same
frozen revision under a clock a decade early and a decade late and requires the
two bodies to be identical, because three defects of exactly that shape were
found in this area — one of them in ``report_preparation``, the reading these
renderers stand on.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.db import engine
from corridor.delta_resolution import (
    ChildDecisionRequest,
    RecordEffect,
    resolve_delta,
)
from corridor.issue_rendering import (
    CURRENT_STATE_SUMMARY,
    NO_PRIOR_COMPARISON_STATEMENT,
    SECTION_COMMITMENTS,
    SECTION_CONSTRAINT_ALERTS,
    SECTION_FOLLOW_UP_PLANS,
    SECTION_KEY_DATES,
    SECTION_PENDING_COORDINATION,
    AcceptedFollowUpPlan,
    MixedIssueInputs,
    PreviousApprovedIssue,
    ReportSection,
    SourceCoverage,
    TemplateBinding,
    bind_issue_reading,
    read_issue_artifacts,
    render_change_summary,
    render_weekly_report,
)
from corridor.models import (
    ActiveExtractionRun,
    Document,
    ExtractionRun,
    Fact,
    FactSource,
    Project,
    SourceSegment,
)
from corridor.operating_mode import adopt_project_baseline
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
    record_delta_deferral,
    record_delta_supersession,
)
from corridor.support_assessments import FactProposition, record_support_assessment


ALICE = HumanPrincipal("local:alice")
SUBJECT = "Utility Conflicts!7"
OTHER_SUBJECT = "Utility Conflicts!9"
CUTOFF = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
PREPARED_AT = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
ASSESSED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def project(session: Session) -> Project:
    row = Project(
        slug=f"issue-rendering-{uuid4().hex[:8]}",
        name="Issue Rendering",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    return row


class _Source:
    """One document a source arrived as, and the Facts captured from it."""

    def __init__(self, session: Session, project: Project, name: str):
        self.session = session
        self.project = project
        self.document = Document(
            project_id=project.id,
            sha256=sha256(f"{project.slug}:{name}".encode()).hexdigest(),
            filename=name,
            doc_type="matrix",
            numbering_scheme="project-unique",
            pages=1,
            parse_status="parsed",
        )
        session.add(self.document)
        session.flush()
        self.run = ExtractionRun(
            document_id=self.document.id,
            prompt_version="issue_rendering_fixture_v1",
            outcome="completed",
            candidate_count=0,
            page_errors=0,
        )
        session.add(self.run)
        session.flush()
        session.add(
            ActiveExtractionRun(
                document_id=self.document.id, extraction_run_id=self.run.id
            )
        )
        self._ordinal = 0

    def capture(
        self,
        *,
        fact_type: str,
        value: str,
        subject_key: str = SUBJECT,
        date_value: date | None = None,
        supported: bool = True,
    ) -> Fact:
        self._ordinal += 1
        segment = SourceSegment(
            project_id=self.project.id,
            document_id=self.document.id,
            kind="spreadsheet_cell",
            exact_text=value,
            content_sha256=sha256(
                f"{self.document.filename}:{self._ordinal}:{value}".encode()
            ).hexdigest(),
            ordinal=self._ordinal,
            sheet_name="Utility Conflicts",
            cell_range=f"A{self._ordinal}",
        )
        self.session.add(segment)
        self.session.flush()
        fact = Fact(
            project_id=self.project.id,
            document_id=self.document.id,
            extraction_run_id=self.run.id,
            fact_type=fact_type,
            subject_kind="source_row",
            subject_key=subject_key,
            text_value=None if date_value is not None else value,
            date_value=date_value,
            transformation=(
                "iso_date_cell_v1" if date_value is not None else "trim_cell_text_v1"
            ),
            recorded_by="extractor:issue_rendering_fixture_v1",
            content_sha256=sha256(
                f"{self.document.filename}:{fact_type}:{subject_key}:{value}".encode()
            ).hexdigest(),
        )
        self.session.add(fact)
        self.session.flush()
        self.session.add(
            FactSource(
                project_id=self.project.id,
                document_id=self.document.id,
                fact_id=fact.id,
                source_segment_id=segment.id,
                role="value_source",
                ordinal=1,
            )
        )
        self.session.flush()
        if supported:
            record_support_assessment(
                self.session,
                project_id=self.project.id,
                proposition=FactProposition(fact.id),
                source_segment_ids=[segment.id],
                evidence_role="value_support",
                assessment="supported",
                authority=ALICE,
                assessed_at=ASSESSED_AT,
            )
        return fact


def _adopt(session: Session, project: Project, facts, key: str) -> int:
    """One adopted baseline, written as the record-decision role (#509 writes this)."""

    # Read every identifier before the role changes: the record-decision role
    # cannot select `projects`, so a lazy refresh under it fails.
    project_id = project.id
    decisions = [
        {
            "project_id": project_id,
            "fact_id": fact.id,
            "subject_key": fact.subject_key,
            "fact_type": fact.fact_type,
        }
        for fact in facts
    ]
    session.execute(text("set local role corridor_fact_decision_writer"))
    revision_id = int(
        session.scalar(
            text(
                "insert into project_record_revisions ("
                "project_id, command_type, human_principal, idempotency_key"
                ") values (:project_id, 'adopt_baseline', 'local:adopter', :key)"
                " returning id"
            ),
            {"project_id": project_id, "key": key},
        )
    )
    for decision in decisions:
        session.execute(
            text(
                "insert into fact_decisions ("
                "project_id, fact_id, subject_key, fact_type, revision_id, disposition"
                ") values (:project_id, :fact_id, :subject_key, :fact_type,"
                " :revision_id, 'include')"
            ),
            {**decision, "revision_id": revision_id},
        )
    session.execute(text("reset role"))
    session.expire_all()
    adopt_project_baseline(
        session,
        project_id=project_id,
        adopted_by_principal="local:adopter",
        baseline_source_sha256=sha256(key.encode()).hexdigest(),
        importer_identity="issue_rendering_fixture",
        importer_version="v1",
        idempotency_key=f"adopt:{key}",
        revision_id=revision_id,
    )
    return revision_id


def _delta(
    session: Session,
    project: Project,
    *,
    field: str,
    accepted_value,
    proposed_value,
    subject: str = SUBJECT,
    source_revision: str = "rev-1",
    baseline_revision: int | None = None,
):
    (delta,) = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="ucm-workbook",
        source_revision=source_revision,
        deltas=[
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget(subject_identity=subject, field=field),
                accepted_value=accepted_value,
                proposed_value=proposed_value,
                accepted_baseline_revision=(
                    f"revision:{baseline_revision}"
                    if baseline_revision is not None
                    else None
                ),
            )
        ],
    )
    return delta


def _accept(session: Session, project: Project, delta, fact, *, at, observed):
    support = record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(fact.id),
        source_segment_ids=[
            session.scalar(
                text(
                    "select source_segment_id from fact_sources where fact_id = :fact"
                ),
                {"fact": fact.id},
            )
        ],
        evidence_role="value_support",
        assessment="supported",
        authority=ALICE,
        assessed_at=ASSESSED_AT,
    )
    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project.id,
            delta_id=delta.id,
            action="accept",
            principal=ALICE,
            idempotency_key=f"accept:{delta.id}",
            decided_at=at,
            observed_accepted_revision_id=observed,
            record_effects=(RecordEffect(fact_id=fact.id),),
            support_assessment_ids=(support.id,),
        ),
    )
    assert outcome.status == "resolved", outcome.refusal
    session.expire_all()
    return outcome


def _reject(session: Session, project: Project, delta, *, at):
    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project.id,
            delta_id=delta.id,
            action="reject",
            principal=ALICE,
            idempotency_key=f"reject:{delta.id}",
            decided_at=at,
        ),
    )
    assert outcome.status == "resolved", outcome.refusal
    return outcome


def _preparation(project_id: int, revision_id: int, **overrides) -> dict:
    """One ``report_preparation`` reading, in the shape that pass returns."""

    body = {
        "schema_version": "report-preparation-result-v1",
        "project_id": project_id,
        "configuration_version": "report-preparation-v1",
        "observed_at": PREPARED_AT.isoformat(),
        "health": "healthy",
        "window_start": "",
        "through_delta_id": 10**9,
        "through_disposition_id": 10**9,
        "accepted_revision_id": revision_id,
        "resolved_accepted": 0,
        "resolved_edited": 0,
        "resolved_rejected": 0,
        "proposed_new": 0,
        "open_actionable": 0,
        "open_deferred": 0,
        "superseded": 0,
    }
    body.update(overrides)
    return body


TEMPLATE = TemplateBinding(
    template_identity="partner-weekly",
    template_version="3",
    mapping_identity="partner-weekly-mapping",
    mapping_version="2",
    sections=(
        ReportSection(key=SECTION_CONSTRAINT_ALERTS, heading="Items needing attention"),
        ReportSection(key=SECTION_COMMITMENTS, heading="Utility commitments"),
        ReportSection(key=SECTION_KEY_DATES, heading="Dates we need work by"),
        ReportSection(key=SECTION_FOLLOW_UP_PLANS, heading="Our next steps"),
        ReportSection(
            key=SECTION_PENDING_COORDINATION, heading="Open questions with owners"
        ),
    ),
)

COVERAGE = (
    SourceCoverage(
        source_name="Weekly utility conflict matrix",
        requirement="required",
        state="read",
        detail="the 2026-09-01 revision was read in full",
    ),
)


def _bind(session, project, revision, **overrides):
    arguments = {
        "project_id": project.id,
        "preparation": _preparation(project.id, revision),
        "source_cutoff": CUTOFF,
        "coverage": COVERAGE,
        "templates": TEMPLATE,
        "first_issue_behavior": NO_PRIOR_COMPARISON_STATEMENT,
        "prepared_at": PREPARED_AT,
    }
    arguments.update(overrides)
    return bind_issue_reading(session, **arguments)


def _baseline(session, project):
    """An adopted project with a Promised For, a Required By, and an owner."""

    source = _Source(session, project, "ucm-2026-08-01.xlsx")
    promised = source.capture(
        fact_type="committed_date", value="2026-11-01", date_value=date(2026, 11, 1)
    )
    required = source.capture(
        fact_type="need_date", value="2026-12-01", date_value=date(2026, 12, 1)
    )
    owner = source.capture(fact_type="external_org", value="City Water")
    revision = _adopt(session, project, (promised, required, owner), "baseline")
    return source, revision


# --- One frozen revision --------------------------------------------------


def test_both_artifacts_are_read_from_one_bound_frozen_revision(session, project):
    _, revision = _baseline(session, project)
    reading = _bind(session, project, revision)

    artifacts = read_issue_artifacts(session, reading)

    assert artifacts.change_summary.bound is artifacts.weekly_report.bound
    assert artifacts.change_summary.bound is reading
    assert reading.accepted_revision_id == revision
    summary = render_change_summary(artifacts.change_summary)
    report = render_weekly_report(artifacts.weekly_report)
    # A reader can name the exact revision each output was rendered from.
    assert summary.reading_identity == report.reading_identity
    assert f"revision {revision}" in summary.text
    assert f"revision {revision}" in report.text


def test_binding_refuses_a_preparation_reading_of_another_revision(session, project):
    _, revision = _baseline(session, project)

    with pytest.raises(MixedIssueInputs) as refused:
        _bind(
            session,
            project,
            revision,
            preparation=_preparation(project.id, revision + 500),
        )

    assert "revision" in str(refused.value)


def test_binding_refuses_a_previous_issue_ahead_of_the_frozen_revision(
    session, project
):
    _, revision = _baseline(session, project)

    with pytest.raises(MixedIssueInputs):
        _bind(
            session,
            project,
            revision,
            previous_issue=PreviousApprovedIssue(
                issue_identity="2026-W35",
                accepted_revision_id=revision + 1,
                approved_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
            ),
        )


def test_binding_refuses_a_legacy_project(session, project):
    """A legacy project keeps its own readers; nothing is dual-written for it."""

    source = _Source(session, project, "ucm-legacy.xlsx")
    fact = source.capture(fact_type="external_org", value="City Water")
    # Read the identifiers before the role changes; see _adopt.
    project_id = project.id
    decision = {
        "project_id": project_id,
        "fact_id": fact.id,
        "subject_key": fact.subject_key,
        "fact_type": fact.fact_type,
    }
    session.execute(text("set local role corridor_fact_decision_writer"))
    revision = int(
        session.scalar(
            text(
                "insert into project_record_revisions ("
                "project_id, command_type, human_principal, idempotency_key"
                ") values (:project_id, 'adopt_baseline', 'local:adopter', 'legacy')"
                " returning id"
            ),
            {"project_id": project_id},
        )
    )
    session.execute(
        text(
            "insert into fact_decisions ("
            "project_id, fact_id, subject_key, fact_type, revision_id, disposition"
            ") values (:project_id, :fact_id, :subject_key, :fact_type,"
            " :revision_id, 'include')"
        ),
        {**decision, "revision_id": revision},
    )
    session.execute(text("reset role"))

    with pytest.raises(MixedIssueInputs) as refused:
        _bind(session, project, revision)

    assert "corridor.report" in str(refused.value)


# --- The change summary ---------------------------------------------------


def test_a_change_since_the_previous_issue_names_both_accepted_values(
    session, project
):
    source, baseline = _baseline(session, project)
    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-12-15",
        baseline_revision=baseline,
    )
    moved = source.capture(
        fact_type="committed_date", value="2026-12-15", date_value=date(2026, 12, 15)
    )
    outcome = _accept(
        session,
        project,
        delta,
        moved,
        at=datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
        observed=baseline,
    )

    reading = _bind(
        session,
        project,
        outcome.revision_id,
        preparation=_preparation(project.id, outcome.revision_id),
        previous_issue=PreviousApprovedIssue(
            issue_identity="2026-W35",
            accepted_revision_id=baseline,
            approved_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        ),
    )
    artifacts = read_issue_artifacts(session, reading)

    (change,) = artifacts.change_summary.changes
    assert change.subject_identity == SUBJECT
    assert change.field == "committed_date"
    assert change.prior_value == "2026-11-01"
    assert change.current_value == "2026-12-15"
    assert change.decided_at == datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    assert change.decided_by == "local:alice"
    assert change.revision_id == outcome.revision_id
    assert change.provenance.complete
    assert change.provenance.support_assessment_ids
    assert change.provenance.source_segment_ids

    text_out = render_change_summary(artifacts.change_summary).text
    assert "Promised for" in text_out
    assert "2026-11-01" in text_out
    assert "2026-12-15" in text_out
    assert "local:alice" in text_out


def test_a_decision_before_the_previous_issue_is_outside_this_window(
    session, project
):
    source, baseline = _baseline(session, project)
    early = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-11-20",
        baseline_revision=baseline,
    )
    early_fact = source.capture(
        fact_type="committed_date", value="2026-11-20", date_value=date(2026, 11, 20)
    )
    first = _accept(
        session,
        project,
        early,
        early_fact,
        at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
        observed=baseline,
    )
    late = _delta(
        session,
        project,
        field="need_date",
        accepted_value="2026-12-01",
        proposed_value="2026-12-20",
        source_revision="rev-2",
        baseline_revision=baseline,
    )
    late_fact = source.capture(
        fact_type="need_date", value="2026-12-20", date_value=date(2026, 12, 20)
    )
    second = _accept(
        session,
        project,
        late,
        late_fact,
        at=datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
        observed=first.revision_id,
    )

    reading = _bind(
        session,
        project,
        second.revision_id,
        preparation=_preparation(project.id, second.revision_id),
        previous_issue=PreviousApprovedIssue(
            issue_identity="2026-W35",
            accepted_revision_id=first.revision_id,
            approved_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        ),
    )
    artifacts = read_issue_artifacts(session, reading)

    fields = [change.field for change in artifacts.change_summary.changes]
    assert fields == ["need_date"]


def test_a_missed_week_widens_the_window_rather_than_dropping_a_change(
    session, project
):
    """Nothing was approved for two weeks, so both changes belong to this issue."""

    source, baseline = _baseline(session, project)
    first_delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-11-20",
        baseline_revision=baseline,
    )
    first_fact = source.capture(
        fact_type="committed_date", value="2026-11-20", date_value=date(2026, 11, 20)
    )
    first = _accept(
        session,
        project,
        first_delta,
        first_fact,
        at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
        observed=baseline,
    )
    second_delta = _delta(
        session,
        project,
        field="need_date",
        accepted_value="2026-12-01",
        proposed_value="2026-12-20",
        source_revision="rev-2",
        baseline_revision=baseline,
    )
    second_fact = source.capture(
        fact_type="need_date", value="2026-12-20", date_value=date(2026, 12, 20)
    )
    second = _accept(
        session,
        project,
        second_delta,
        second_fact,
        at=datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
        observed=first.revision_id,
    )

    reading = _bind(
        session,
        project,
        second.revision_id,
        preparation=_preparation(project.id, second.revision_id),
        previous_issue=PreviousApprovedIssue(
            issue_identity="2026-W34",
            accepted_revision_id=baseline,
            approved_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ),
    )
    artifacts = read_issue_artifacts(session, reading)

    assert sorted(
        change.field for change in artifacts.change_summary.changes
    ) == ["committed_date", "need_date"]


def test_a_prepared_reading_never_moves_the_comparison_baseline(session, project):
    """Only an approved issue is a predecessor; a preparation is not one."""

    source, baseline = _baseline(session, project)
    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-12-15",
        baseline_revision=baseline,
    )
    moved = source.capture(
        fact_type="committed_date", value="2026-12-15", date_value=date(2026, 12, 15)
    )
    accepted = _accept(
        session,
        project,
        delta,
        moved,
        at=datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
        observed=baseline,
    )
    previous = PreviousApprovedIssue(
        issue_identity="2026-W35",
        accepted_revision_id=baseline,
        approved_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    # A first preparation is discarded; a second is prepared from the same
    # approved predecessor and must still report the change.
    for attempt in range(2):
        reading = _bind(
            session,
            project,
            accepted.revision_id,
            preparation=_preparation(
                project.id, accepted.revision_id, observed_at=str(attempt)
            ),
            previous_issue=previous,
        )
        artifacts = read_issue_artifacts(session, reading)
        assert artifacts.change_summary.previous_issue_identity == "2026-W35"
        assert [c.field for c in artifacts.change_summary.changes] == [
            "committed_date"
        ]


def test_without_a_previous_issue_the_summary_states_there_is_no_comparison(
    session, project
):
    _, revision = _baseline(session, project)
    reading = _bind(
        session,
        project,
        revision,
        first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
    )
    artifacts = read_issue_artifacts(session, reading)

    assert artifacts.change_summary.changes == ()
    assert artifacts.change_summary.previous_issue_identity is None
    body = render_change_summary(artifacts.change_summary).text
    assert "no earlier approved issue" in body
    # Never an invented predecessor, window, or change count.
    assert "changes were accepted" not in body


def test_without_a_previous_issue_the_configured_current_state_summary_renders(
    session, project
):
    _, revision = _baseline(session, project)
    reading = _bind(
        session, project, revision, first_issue_behavior=CURRENT_STATE_SUMMARY
    )
    artifacts = read_issue_artifacts(session, reading)

    assert artifacts.change_summary.changes == ()
    assert artifacts.change_summary.current_state
    body = render_change_summary(artifacts.change_summary).text
    assert "no earlier approved issue" in body
    assert "2026-11-01" in body
    assert "Promised for" in body


def test_unaccepted_deltas_never_appear_as_changes_but_stay_distinguishable(
    session, project
):
    source, baseline = _baseline(session, project)
    open_delta = _delta(
        session,
        project,
        field="external_org",
        accepted_value="City Water",
        proposed_value="City Water District",
        baseline_revision=baseline,
    )
    deferred = _delta(
        session,
        project,
        field="need_date",
        accepted_value="2026-12-01",
        proposed_value="2027-01-15",
        source_revision="rev-2",
        baseline_revision=baseline,
    )
    record_delta_deferral(
        session,
        project_id=project.id,
        delta_id=deferred.id,
        deferred_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        scheduled_by_principal="local:alice",
        deferred_until=datetime(2026, 10, 1, tzinfo=timezone.utc),
        wake_condition="awaiting the utility's reply",
    )
    rejected = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-10-01",
        source_revision="rev-3",
        baseline_revision=baseline,
    )
    _reject(session, project, rejected, at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    prior = _delta(
        session,
        project,
        field="station_from",
        accepted_value="1149+00",
        proposed_value="1150+00",
        source_revision="rev-4",
        baseline_revision=baseline,
    )
    superseding = _delta(
        session,
        project,
        field="station_from",
        accepted_value="1149+00",
        proposed_value="1151+00",
        source_revision="rev-5",
        baseline_revision=baseline,
    )
    record_delta_supersession(
        session,
        project_id=project.id,
        prior_delta_id=prior.id,
        superseding_delta_id=superseding.id,
        reason="newer_source_revision",
    )

    reading = _bind(
        session,
        project,
        baseline,
        previous_issue=PreviousApprovedIssue(
            issue_identity="2026-W35",
            accepted_revision_id=baseline,
            approved_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        ),
    )
    artifacts = read_issue_artifacts(session, reading)

    assert artifacts.change_summary.changes == ()
    states = {
        excluded.delta_id: excluded.state
        for excluded in artifacts.change_summary.unaccepted_deltas
    }
    assert states[open_delta.id] == "open"
    assert states[deferred.id] == "deferred"
    assert states[rejected.id] == "rejected"
    assert states[prior.id] == "superseded"

    body = render_change_summary(artifacts.change_summary).text
    assert "City Water District" not in body
    assert "2027-01-15" not in body


def test_a_delta_raised_against_a_moved_accepted_value_reads_as_stale(
    session, project
):
    source, baseline = _baseline(session, project)
    moved = source.capture(
        fact_type="committed_date", value="2026-12-15", date_value=date(2026, 12, 15)
    )
    mover = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-12-15",
        baseline_revision=baseline,
    )
    accepted = _accept(
        session,
        project,
        mover,
        moved,
        at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        observed=baseline,
    )
    stale = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-11-10",
        source_revision="rev-stale",
        baseline_revision=baseline,
    )

    reading = _bind(
        session,
        project,
        accepted.revision_id,
        preparation=_preparation(project.id, accepted.revision_id),
        previous_issue=PreviousApprovedIssue(
            issue_identity="2026-W35",
            accepted_revision_id=accepted.revision_id,
            approved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
    )
    artifacts = read_issue_artifacts(session, reading)

    states = {
        excluded.delta_id: excluded.state
        for excluded in artifacts.change_summary.unaccepted_deltas
    }
    assert states[stale.id] == "stale"


# --- The weekly Coordination Report ---------------------------------------


def test_the_report_renders_only_the_sections_the_template_supports(session, project):
    _, revision = _baseline(session, project)
    template = TemplateBinding(
        template_identity="partner-weekly",
        template_version="3",
        mapping_identity="partner-weekly-mapping",
        mapping_version="2",
        sections=(
            ReportSection(key=SECTION_COMMITMENTS, heading="Utility commitments"),
        ),
    )
    reading = _bind(session, project, revision, templates=template)
    artifacts = read_issue_artifacts(session, reading)

    body = render_weekly_report(artifacts.weekly_report).text
    assert "Utility commitments" in body
    assert "Items needing attention" not in body
    assert "Open questions with owners" not in body
    assert {line.section_key for line in artifacts.weekly_report.lines} <= {
        SECTION_COMMITMENTS
    }


def test_the_report_states_the_accepted_commitments_dates_and_alerts(
    session, project
):
    _, revision = _baseline(session, project)
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)

    body = render_weekly_report(artifacts.weekly_report).text
    assert "Utility commitments" in body
    assert "Promised for" in body
    assert "2026-11-01" in body
    assert "Required by" in body
    assert "2026-12-01" in body
    for line in artifacts.weekly_report.lines:
        assert line.provenance.complete, line


def test_an_overdue_promised_date_is_named_by_its_released_check(session, project):
    """The alert reuses the released rule and its customer label, unchanged."""

    source = _Source(session, project, "ucm-overdue.xlsx")
    promised = source.capture(
        fact_type="committed_date", value="2026-08-01", date_value=date(2026, 8, 1)
    )
    revision = _adopt(session, project, (promised,), "overdue-baseline")
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)

    alerts = [
        line
        for line in artifacts.weekly_report.lines
        if line.section_key == SECTION_CONSTRAINT_ALERTS
    ]
    assert [alert.rule for alert in alerts] == ["OVERDUE"]
    assert "Promised timing passed" in render_weekly_report(
        artifacts.weekly_report
    ).text


def test_the_pending_question_is_neutral_and_states_the_accepted_position(
    session, project
):
    source, baseline = _baseline(session, project)
    _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2027-03-01",
        baseline_revision=baseline,
    )
    plan = AcceptedFollowUpPlan(
        plan_identity="plan-88",
        subject_identity=SUBJECT,
        field="committed_date",
        assigned_to="Dana Reyes",
        next_action="Ask City Water to confirm the relocation date",
        action_due_date=date(2026, 9, 10),
        open_question="Has the relocation date changed?",
        recorded_by="local:alice",
        recorded_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    reading = _bind(session, project, baseline, follow_up_plans=(plan,))
    artifacts = read_issue_artifacts(session, reading)

    body = render_weekly_report(artifacts.weekly_report).text
    assert "Has the relocation date changed?" in body
    assert "2026-11-01" in body
    # The incoming value is not accepted, so it is nowhere in the report.
    assert "2027-03-01" not in body


def test_a_pending_question_that_is_not_a_question_is_refused(session, project):
    _, baseline = _baseline(session, project)
    plan = AcceptedFollowUpPlan(
        plan_identity="plan-89",
        subject_identity=SUBJECT,
        field="committed_date",
        assigned_to="Dana Reyes",
        next_action="Confirm the date",
        action_due_date=None,
        open_question="The relocation date is now 2027-03-01.",
        recorded_by="local:alice",
        recorded_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    with pytest.raises(MixedIssueInputs):
        _bind(session, project, baseline, follow_up_plans=(plan,))


# --- Coverage, provenance, and determinism --------------------------------


def test_a_required_source_failure_produces_a_bounded_coverage_statement(
    session, project
):
    _, revision = _baseline(session, project)
    coverage = (
        SourceCoverage(
            source_name="Weekly utility conflict matrix",
            requirement="required",
            state="read",
            detail="the 2026-09-01 revision was read in full",
        ),
        SourceCoverage(
            source_name="City Water relocation schedule",
            requirement="required",
            state="failed",
            detail="the download returned no file on 2026-09-02",
        ),
        SourceCoverage(
            source_name="Contractor progress email",
            requirement="optional",
            state="late",
            detail="it arrived after the cutoff and is held for the next issue",
        ),
    )
    reading = _bind(session, project, revision, coverage=coverage)
    artifacts = read_issue_artifacts(session, reading)

    for rendered in (
        render_change_summary(artifacts.change_summary),
        render_weekly_report(artifacts.weekly_report),
    ):
        assert "City Water relocation schedule" in rendered.text
        assert "was not read" in rendered.text
        assert "Contractor progress email" in rendered.text
    assert artifacts.weekly_report.coverage_complete is False


def test_a_value_without_class_complete_provenance_is_refused(session, project):
    """Support is named, never inferred from a passed source passage check."""

    source = _Source(session, project, "ucm-unsupported.xlsx")
    promised = source.capture(
        fact_type="committed_date",
        value="2026-11-01",
        date_value=date(2026, 11, 1),
        supported=False,
    )
    revision = _adopt(session, project, (promised,), "unsupported-baseline")
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)

    line = next(
        line
        for line in artifacts.weekly_report.lines
        if line.section_key == SECTION_COMMITMENTS
    )
    assert not line.provenance.complete
    assert "support assessment" in " ".join(line.provenance.missing)
    with pytest.raises(Exception) as refused:
        render_weekly_report(artifacts.weekly_report)
    assert "provenance" in str(refused.value).lower()


def test_the_module_never_reads_locator_validation_as_support():
    from pathlib import Path

    source = Path("src/corridor/issue_rendering.py").read_text(encoding="utf-8")
    for forbidden in ("locator_validation_status", "EvidenceLink", ".verified"):
        assert forbidden not in source


def test_re_rendering_the_same_reading_under_absurd_clocks_is_identical(
    session, project
):
    """No clock may change what an issue says about the project.

    Only the observation timestamp differs, and it lives in the container,
    never in the body, so a difference there is never read as project change.
    """

    _, revision = _baseline(session, project)
    rendered = []
    for prepared_at in (
        datetime(2016, 1, 1, tzinfo=timezone.utc),
        datetime(2036, 1, 1, tzinfo=timezone.utc),
    ):
        reading = _bind(
            session,
            project,
            revision,
            prepared_at=prepared_at,
            first_issue_behavior=CURRENT_STATE_SUMMARY,
        )
        artifacts = read_issue_artifacts(session, reading)
        rendered.append(
            (
                render_change_summary(artifacts.change_summary),
                render_weekly_report(artifacts.weekly_report),
            )
        )

    early, late = rendered
    assert early[0].body == late[0].body
    assert early[1].body == late[1].body
    assert early[0].reading_identity == late[0].reading_identity
    assert early[0].container != late[0].container
    assert "2016" not in early[0].body and "2036" not in late[0].body


def test_a_changed_template_or_mapping_changes_the_reading_identity(session, project):
    _, revision = _baseline(session, project)
    first = _bind(session, project, revision)
    second = _bind(
        session,
        project,
        revision,
        templates=TemplateBinding(
            template_identity=TEMPLATE.template_identity,
            template_version="4",
            mapping_identity=TEMPLATE.mapping_identity,
            mapping_version=TEMPLATE.mapping_version,
            sections=TEMPLATE.sections,
        ),
    )

    assert first.reading_identity != second.reading_identity


def test_the_structured_readings_carry_what_the_package_needs(session, project):
    """#529 consumes the readings, never the rendered bytes."""

    source, baseline = _baseline(session, project)
    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-12-15",
        baseline_revision=baseline,
    )
    moved = source.capture(
        fact_type="committed_date", value="2026-12-15", date_value=date(2026, 12, 15)
    )
    accepted = _accept(
        session,
        project,
        delta,
        moved,
        at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        observed=baseline,
    )
    reading = _bind(
        session,
        project,
        accepted.revision_id,
        preparation=_preparation(
            project.id, accepted.revision_id, open_actionable=2, open_deferred=1
        ),
        previous_issue=PreviousApprovedIssue(
            issue_identity="2026-W35",
            accepted_revision_id=baseline,
            approved_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        ),
    )
    artifacts = read_issue_artifacts(session, reading)

    assert artifacts.reading_identity == reading.reading_identity
    assert artifacts.accepted_revision_id == accepted.revision_id
    assert artifacts.change_summary.source_cutoff == CUTOFF
    assert artifacts.change_summary.template_identity == "partner-weekly"
    assert artifacts.change_summary.mapping_identity == "partner-weekly-mapping"
    assert artifacts.weekly_report.open_actionable == 2
    assert artifacts.weekly_report.open_deferred == 1
    assert artifacts.change_summary.count_provenance.rule_version
    assert artifacts.change_summary.count_provenance.input_record_ids


def test_a_deferral_is_neither_a_decision_nor_actionable_work(session, project):
    """ADR-0084: three separate counts, and nothing adds them together."""

    source, baseline = _baseline(session, project)
    deferred = _delta(
        session,
        project,
        field="need_date",
        accepted_value="2026-12-01",
        proposed_value="2027-01-15",
        baseline_revision=baseline,
    )
    record_delta_deferral(
        session,
        project_id=project.id,
        delta_id=deferred.id,
        deferred_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        scheduled_by_principal="local:alice",
        deferred_until=datetime(2026, 10, 1, tzinfo=timezone.utc),
        wake_condition="awaiting the utility's reply",
    )
    reading = _bind(
        session,
        project,
        baseline,
        preparation=_preparation(
            project.id,
            baseline,
            resolved_accepted=2,
            resolved_edited=1,
            resolved_rejected=3,
            open_actionable=4,
            open_deferred=1,
        ),
    )
    artifacts = read_issue_artifacts(session, reading)

    weekly = artifacts.weekly_report
    assert weekly.resolved_accepted == 2
    assert weekly.resolved_edited == 1
    assert weekly.resolved_rejected == 3
    assert weekly.open_actionable == 4
    assert weekly.open_deferred == 1

    body = render_weekly_report(weekly).text
    # Each count is stated in its own words; none of them is a total.
    assert "2 proposed changes accepted as the source stated them" in body
    assert "1 proposed change accepted with an edited value" in body
    assert "3 proposed changes decided by keeping the value" in body
    assert "4 proposed changes waiting on a decision" in body
    assert "1 proposed change deferred to a later date" in body
    for total in ("5 proposed changes", "6 proposed changes", "7 proposed changes"):
        assert total not in body
    # The delta itself is open, never resolved, while the deferral runs.
    states = {
        line.delta_id: line.state
        for line in artifacts.change_summary.unaccepted_deltas
    }
    assert states[deferred.id] == "deferred"


def test_a_deferral_whose_return_date_has_passed_is_open_again(session, project):
    """Whether a deferral still runs is judged against the declared cutoff."""

    source, baseline = _baseline(session, project)
    returned = _delta(
        session,
        project,
        field="need_date",
        accepted_value="2026-12-01",
        proposed_value="2027-01-15",
        baseline_revision=baseline,
    )
    record_delta_deferral(
        session,
        project_id=project.id,
        delta_id=returned.id,
        deferred_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        scheduled_by_principal="local:alice",
        deferred_until=datetime(2026, 8, 20, tzinfo=timezone.utc),
        wake_condition="the promised reply date",
    )
    reading = _bind(session, project, baseline)
    artifacts = read_issue_artifacts(session, reading)

    states = {
        line.delta_id: line.state
        for line in artifacts.change_summary.unaccepted_deltas
    }
    assert states[returned.id] == "open"


def test_the_standing_figures_name_the_rule_and_watermarks_they_came_from(
    session, project
):
    """A published count is a Derivation and says what it was computed over."""

    _, revision = _baseline(session, project)
    reading = _bind(
        session,
        project,
        revision,
        preparation=_preparation(
            project.id, revision, through_delta_id=42, through_disposition_id=17
        ),
    )
    artifacts = read_issue_artifacts(session, reading)

    provenance = artifacts.weekly_report.standing_provenance
    assert provenance.rule_identity
    assert provenance.rule_version
    assert provenance.input_record_ids == (42, 17)
    assert provenance.evaluated_as_of == CUTOFF.date()
    body = render_weekly_report(artifacts.weekly_report).text
    assert "every proposed change up to number 42" in body
    assert "every decision up to number 17" in body
