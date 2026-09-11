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
from pathlib import Path
import re

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.delta_resolution import (
    ChildDecisionRequest,
    RecordEffect,
    resolve_delta,
)
from corridor.issue_rendering import (
    ACCEPTED_RECORD_CHECK_RULES,
    ACCEPTED_RECORD_CHECK_SET,
    CHECKS_NOT_RUN,
    CHECKS_RAISED_ELSEWHERE,
    CURRENT_STATE_SUMMARY,
    FIRST_ACCEPTED_RECORD_CHECK_SET,
    NO_PRIOR_COMPARISON_STATEMENT,
    SECTION_COMMITMENTS,
    SECTION_CONSTRAINT_ALERTS,
    SECTION_FOLLOW_UP_PLANS,
    SECTION_KEY_DATES,
    SECTION_PENDING_COORDINATION,
    MixedIssueInputs,
    PreviousApprovedIssue,
    ReportSection,
    SourceCoverage,
    FOLLOW_UP_NOT_RETAINED,
    TemplateBinding,
    bind_issue_reading,
    read_issue_artifacts,
    render_change_summary,
    render_weekly_report,
)
from corridor.models import (
    ActiveExtractionRun,
    DocPage,
    Document,
    ExtractionRun,
    Fact,
    FactSource,
    Project,
    SourceSegment,
)
from corridor.native_follow_up_reading import (
    AcceptedFollowUpPlan,
    read_adopted_follow_up_plans,
)
from corridor.presentation import accepted_record_exception_name, exception_name
from corridor.operating_mode import adopt_project_baseline
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
    record_delta_deferral,
)
from harness_support import as_record_decision_role
from delta_supersession_support import record_delta_supersession
from corridor.review_packets import (
    NEEDS_COORDINATION,
    SAVED,
    CoordinationRequest,
    PacketChildRequest,
    ReviewPacketRequest,
    resolve_review_packet,
)
from corridor.support_assessments import FactProposition, record_support_assessment


ALICE = HumanPrincipal("local:alice")
SUBJECT = "Utility Conflicts!7"
OTHER_SUBJECT = "Utility Conflicts!9"
CUTOFF = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
PREPARED_AT = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
ASSESSED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
PLANNED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
RETURNS_AT = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


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

    def segment(self, value: str) -> SourceSegment:
        """One addressable piece of this document, with no Fact captured from it.

        A replacement revision carries the same passage at a new locator, so
        a test that clears the replacement check needs a segment in the
        successor without capturing a second Source Fact from it.
        """

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
        return segment

    def capture(
        self,
        *,
        fact_type: str,
        value: str,
        subject_key: str = SUBJECT,
        date_value: date | None = None,
        supported: bool = True,
    ) -> Fact:
        segment = self.segment(value)
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


def _segment_of(session: Session, fact: Fact) -> int:
    """The Source Segment one captured Fact was read from."""

    return int(
        session.scalar(
            text("select source_segment_id from fact_sources where fact_id = :fact"),
            {"fact": fact.id},
        )
    )


def _supersede(session: Session, replaced: "_Source", successor: "_Source", *, on: date):
    """Register that one Document Revision replaced another (ADR-0015, ADR-0016).

    The registry requires the authority's own replacement date and the page
    it is stated on, so both are supplied; nothing here substitutes a
    document, retrieval, or ingestion date for the replacement date.
    """

    session.add(
        DocPage(
            document_id=successor.document.id,
            page_no=1,
            text="This revision replaces the previous one.",
        )
    )
    session.flush()
    # The registry refuses an edge whose successor is not itself registered,
    # so both identifiers are in the database before the edge is written.
    replaced.document.registry_id = f"reg-{replaced.document.id}"
    successor.document.registry_id = f"reg-{successor.document.id}"
    session.flush()
    replaced.document.superseded_by = successor.document.id
    replaced.document.superseded_on = on
    replaced.document.supersession_source_document_id = successor.document.id
    replaced.document.supersession_source_page = 1
    session.flush()


def _alerts(artifacts):
    """Every Constraint Alert line in the weekly report, in the order read."""

    return [
        line
        for line in artifacts.weekly_report.lines
        if line.section_key == SECTION_CONSTRAINT_ALERTS
    ]


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
    with as_record_decision_role(session):
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


def _plan(
    session: Session,
    project: Project,
    delta,
    revision: int,
    *,
    question: str,
    responsible_organization: str = "City Water",
    return_date: datetime | None = None,
    source_revision: str = "rev-1",
    decided_at: datetime = PLANNED_AT,
):
    """One Needs coordination outcome, recorded the way #526 records it.

    The report's follow-up section is read from these rows and from nothing
    else, so the tests below record one rather than constructing the reading
    type by hand.  Two hand-built plans were the *only* constructions of the
    renderer's own follow-up type, which is how it kept a shape no retained
    record has (#425).
    """

    result = resolve_review_packet(
        session,
        ReviewPacketRequest(
            project_id=project.id,
            grouping_rule_version="packetizer-v1",
            grouping_key_kind="source_revision",
            grouping_key=source_revision,
            principal=ALICE,
            idempotency_key=f"plan:{delta.id}",
            decided_at=decided_at,
            observed_accepted_revision_id=revision,
            children=(
                PacketChildRequest(
                    delta_id=delta.id,
                    outcome=NEEDS_COORDINATION,
                    observed_source_revision=source_revision,
                    coordination=CoordinationRequest(
                        question=question,
                        responsible_organization=responsible_organization,
                        return_date=return_date,
                    ),
                ),
            ),
        ),
    )
    assert result.status == SAVED, result
    session.expire_all()
    return result


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
        fact_type="committed_date", value="2026-11-01",)
    required = source.capture(
        fact_type="need_date", value="2026-12-01",)
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
    with as_record_decision_role(session):
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
        fact_type="committed_date", value="2026-12-15",)
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
        fact_type="committed_date", value="2026-11-20",)
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
        fact_type="need_date", value="2026-12-20",)
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
        fact_type="committed_date", value="2026-11-20",)
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
        fact_type="need_date", value="2026-12-20",)
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
        fact_type="committed_date", value="2026-12-15",)
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
        fact_type="committed_date", value="2026-12-15",)
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
        fact_type="committed_date", value="2026-08-01",)
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


# --- The two ported support checks (#596, ADR-0090) ------------------------


def test_an_accepted_value_with_no_supporting_documentation_raises_the_check(
    session, project
):
    """The ported ``MISSING_EVIDENCE``, read from the Support Assessment relation."""

    source = _Source(session, project, "ucm-support.xlsx")
    supported = source.capture(fact_type="external_org", value="City Water")
    unsupported = source.capture(
        fact_type="resolution_strategy", value="relocate", supported=False
    )
    unsupported_id = unsupported.id
    revision = _adopt(session, project, (supported, unsupported), "support-baseline")
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)

    (alert,) = _alerts(artifacts)
    assert alert.rule == "MISSING_EVIDENCE"
    assert alert.subject_identity == SUBJECT
    # It fires for the one accepted value that stands on nothing, and names
    # it: the value beside it has support and raises nothing.
    assert alert.provenance.input_record_ids == (unsupported_id,)
    assert alert.provenance.rule_identity == "accepted_record_checks"
    assert alert.provenance.rule_version == "v2"
    assert alert.provenance.evaluated_as_of == CUTOFF.date()
    assert alert.provenance.complete
    assert "Resolution method" in alert.statement


def test_the_ported_evidence_check_no_longer_names_the_source_passage_check(
    session, project
):
    """ADR-0082: a locatable passage is not support, so the label cannot say it."""

    source = _Source(session, project, "ucm-relabelled.xlsx")
    unsupported = source.capture(
        fact_type="external_org", value="City Water", supported=False
    )
    revision = _adopt(session, project, (unsupported,), "relabelled-baseline")
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)

    body = render_weekly_report(artifacts.weekly_report).text
    assert "No supporting document in use for this value" in body
    assert "source passage check" not in body.lower()
    # The released legacy ruleset keeps computing the Source Passage Check
    # predicate over a legacy project until ADR-0081 stage 6, so its label is
    # untouched: a label that stopped describing the predicate under it would
    # make the legacy report lie.
    assert exception_name("MISSING_EVIDENCE") == (
        "No supporting document passed the source passage check"
    )
    # Every other released label is carried over verbatim.
    for rule in ACCEPTED_RECORD_CHECK_RULES:
        if rule != "MISSING_EVIDENCE":
            assert accepted_record_exception_name(rule) == exception_name(rule)


# The maintainer's own words, approved 2026-09-03 (#613) after the terminology
# research found no industry counterpart to adopt, and the legacy wording they
# expressly kept.  Both are written out here rather than read from the code, so
# that editing either string in ``presentation.py`` turns this test red instead
# of silently redefining a customer term.
APPROVED_SUPPORT_ALERT_LABEL = "No supporting document in use for this value"
LEGACY_SUPPORT_ALERT_LABEL = "No supporting document passed the source passage check"


def test_the_approved_support_alert_label_is_the_one_word_for_word():
    """One approved string, in the code, the glossary, and the research note.

    Terminology procedure step 6 made this wording a maintainer decision, not
    an implementer's: the sources supply a phrase for the positive condition
    ("supported by adequate records", 23 CFR 645.117(b)) and audit practice an
    accusatory name for the negative ("unsupported", 2 CFR 200.1), and neither
    is a status a coordination record can carry.  So the string is only as good
    as the record of its approval, and drift in any one of the three places
    would leave the product saying something nobody agreed to.
    """
    repo_root = Path(__file__).parents[1]

    assert (
        accepted_record_exception_name("MISSING_EVIDENCE")
        == APPROVED_SUPPORT_ALERT_LABEL
    )

    # Step 7 put the approved label in the Project Record glossary, on the
    # entry for the term whose absence it reports.
    glossary = (repo_root / "CONTEXT.md").read_text()
    entry = glossary.split("**Supporting Documentation in Use**:")[1].split("\n\n")[0]
    assert APPROVED_SUPPORT_ALERT_LABEL in entry
    # And the wordings rejected on the way there stay rejected.
    for rejected in ("Unsupported value", "Missing evidence"):
        assert rejected in entry.split("_Avoid_:")[1]

    # The negative research finding and the approval that resolved it are kept
    # together, so a later reader can see why plain-language wording was
    # allowed here at all.
    note = (
        repo_root / "docs" / "research" / "missing-evidence-alert-label-2026-09-03.md"
    ).read_text()
    assert "## 5. Open item for the maintainer, settled 2026-09-03" in note
    assert "The maintainer approved the proposed wording on 2026-09-03" in note
    assert APPROVED_SUPPORT_ALERT_LABEL in note


def test_the_legacy_support_alert_label_is_a_different_label_for_a_different_rule():
    """The two check sets are allowed to differ, and here they must.

    The released legacy ruleset keeps computing the Source Passage Check
    predicate over ``dependencies`` for a legacy project until ADR-0081 stage 6
    retires those tables.  Its label still describes that predicate, so
    replacing it with the accepted record's wording would make the legacy
    report describe a check it did not run.
    """
    assert exception_name("MISSING_EVIDENCE") == LEGACY_SUPPORT_ALERT_LABEL
    assert LEGACY_SUPPORT_ALERT_LABEL != APPROVED_SUPPORT_ALERT_LABEL
    # Neither label may be readable as the other's finding: the accepted
    # record's rule never looks at a passage, a citation, or a location, and
    # #600's Source Passage Check states are the only place those words belong.
    for forbidden in ("passage", "citation", "location", "cited"):
        assert forbidden not in APPROVED_SUPPORT_ALERT_LABEL.lower()


def test_an_assessment_that_contradicts_the_value_is_not_supporting_documentation(
    session, project
):
    """A named person's adverse reading is a reading, and it is not support."""

    source = _Source(session, project, "ucm-contradicted.xlsx")
    fact = source.capture(
        fact_type="external_org", value="City Water", supported=False
    )
    record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(fact.id),
        source_segment_ids=[_segment_of(session, fact)],
        evidence_role="value_support",
        assessment="contradicted",
        authority=ALICE,
        assessed_at=ASSESSED_AT,
    )
    revision = _adopt(session, project, (fact,), "contradicted-baseline")
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)

    assert [alert.rule for alert in _alerts(artifacts)] == ["MISSING_EVIDENCE"]


def test_support_resting_only_on_a_replaced_revision_raises_the_replacement_check(
    session, project
):
    """ADR-0016's predicate, carried over: the value still depends on it."""

    replaced = _Source(session, project, "relocation-letter-rev-a.xlsx")
    successor = _Source(session, project, "relocation-letter-rev-b.xlsx")
    fact = replaced.capture(fact_type="external_org", value="City Water")
    _supersede(session, replaced, successor, on=date(2026, 8, 15))
    revision = _adopt(session, project, (fact,), "replaced-baseline")
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)

    (alert,) = _alerts(artifacts)
    assert alert.rule == "SUPERSEDED_CITATION"
    # The quantity is measured from the authority's own replacement date to
    # the declared source cutoff. No clock takes part.
    assert alert.quantity_days == (CUTOFF.date() - date(2026, 8, 15)).days
    assert alert.provenance.rule_version == "v2"
    assert "Supporting document replaced" in render_weekly_report(
        artifacts.weekly_report
    ).text


def test_a_replaced_revision_goes_quiet_once_the_record_stands_on_the_successor(
    session, project
):
    """Never "a document has a successor" — only "nothing current replaced it".

    The alert has to be able to clear, and it has to clear the right way.
    ADR-0016 refused the blanket reading because an alert on it could only be
    silenced by deleting provenance, which is pressure to erase exactly what
    makes the record checkable. Here the replaced revision, its Source Fact
    and its Support Assessment all stay in the project; what changes is that
    the accepted record now stands on the successor's own statement.
    """

    replaced = _Source(session, project, "letter-rev-a.xlsx")
    successor = _Source(session, project, "letter-rev-b.xlsx")
    promised = replaced.capture(
        fact_type="committed_date", value="2026-11-01",)
    baseline = _adopt(session, project, (promised,), "replacement-baseline")
    _supersede(session, replaced, successor, on=date(2026, 8, 15))

    before = read_issue_artifacts(session, _bind(session, project, baseline))
    assert [alert.rule for alert in _alerts(before)] == ["SUPERSEDED_CITATION"]

    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-12-15",
        baseline_revision=baseline,
    )
    moved = successor.capture(
        fact_type="committed_date", value="2026-12-15",)
    accepted = _accept(
        session,
        project,
        delta,
        moved,
        at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        observed=baseline,
    )

    after = read_issue_artifacts(
        session, _bind(session, project, accepted.revision_id)
    )
    assert _alerts(after) == []
    # The replaced revision is still registered and still carries the earlier
    # Support Assessment; nothing was deleted to silence the check.
    assert replaced.document.superseded_by == successor.document.id


def test_the_two_support_checks_never_both_fire_on_one_value(session, project):
    """A value that stands on nothing cannot also depend on a replaced revision."""

    replaced = _Source(session, project, "orphan-rev-a.xlsx")
    successor = _Source(session, project, "orphan-rev-b.xlsx")
    fact = replaced.capture(
        fact_type="external_org", value="City Water", supported=False
    )
    _supersede(session, replaced, successor, on=date(2026, 8, 15))
    revision = _adopt(session, project, (fact,), "exclusive-baseline")
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)

    assert [alert.rule for alert in _alerts(artifacts)] == ["MISSING_EVIDENCE"]


def test_the_ported_checks_do_not_move_under_an_absurd_clock(session, project):
    """Three defects of exactly this shape were found in this area (#534)."""

    replaced = _Source(session, project, "clock-rev-a.xlsx")
    successor = _Source(session, project, "clock-rev-b.xlsx")
    dated = replaced.capture(fact_type="external_org", value="City Water")
    bare = replaced.capture(
        fact_type="resolution_strategy", value="relocate", supported=False
    )
    _supersede(session, replaced, successor, on=date(2026, 8, 15))
    revision = _adopt(session, project, (dated, bare), "clock-baseline")

    rendered = []
    for prepared_at in (
        datetime(2016, 1, 1, tzinfo=timezone.utc),
        datetime(2036, 1, 1, tzinfo=timezone.utc),
    ):
        reading = _bind(session, project, revision, prepared_at=prepared_at)
        artifacts = read_issue_artifacts(session, reading)
        rendered.append(
            (
                render_weekly_report(artifacts.weekly_report).body,
                tuple(
                    (alert.rule, alert.quantity_days) for alert in _alerts(artifacts)
                ),
            )
        )

    early, late = rendered
    assert early == late
    assert set(dict(early[1])) == {"MISSING_EVIDENCE", "SUPERSEDED_CITATION"}


# --- The declared check set, and what it deliberately does not run ---------


def test_the_report_declares_its_check_set_and_the_checks_it_does_not_run(
    session, project
):
    """ADR-0090: the difference from a legacy project's alerts is declared."""

    from corridor.exceptions import RULES as RELEASED_RULES

    run = set(ACCEPTED_RECORD_CHECK_RULES)
    elsewhere = {rule for rule, _ in CHECKS_RAISED_ELSEWHERE}
    retired = {rule for rule, _ in CHECKS_NOT_RUN}
    # Every released rule is inventoried exactly once: keep 3, port 2,
    # supersede 3, retire 4.
    assert run | elsewhere | retired == set(RELEASED_RULES)
    assert len(run) + len(elsewhere) + len(retired) == len(RELEASED_RULES) == 12
    assert run == {
        "OVERDUE",
        "DUE_SOON",
        "MISSING_DATE",
        "MISSING_EVIDENCE",
        "SUPERSEDED_CITATION",
    }

    _, revision = _baseline(session, project)
    reading = _bind(session, project, revision)
    artifacts = read_issue_artifacts(session, reading)
    coverage = artifacts.weekly_report.check_coverage
    assert coverage.check_set == ACCEPTED_RECORD_CHECK_SET == "accepted_record_checks_v2"

    body = render_weekly_report(artifacts.weekly_report).text
    assert "accepted_record_checks_v2" in body
    # Every rule this set does not run is named, with the reason, so a
    # customer is never left to notice that findings stopped appearing.
    for rule, why in CHECKS_RAISED_ELSEWHERE + CHECKS_NOT_RUN:
        assert accepted_record_exception_name(rule).lower() in body.lower(), rule
        assert why in body, rule
    # And none of them is ever rendered as an alert.
    assert not {line.rule for line in artifacts.weekly_report.lines} & (
        elsewhere | retired
    )


# --- A ruleset change is a rules change, never project movement (#534) ----


def _moved_promise(session, project):
    """One project with a real accepted change, and the revisions either side."""

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
        fact_type="committed_date", value="2026-12-15",)
    accepted = _accept(
        session,
        project,
        delta,
        moved,
        at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        observed=baseline,
    )
    return baseline, accepted.revision_id


def _previous_issue(revision_id: int, check_set: str | None):
    return PreviousApprovedIssue(
        issue_identity="2026-W35",
        accepted_revision_id=revision_id,
        approved_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        check_set=check_set,
    )


def test_a_check_set_change_is_reported_as_a_rules_change_not_project_movement(
    session, project
):
    """The distinction ``changes`` draws for the legacy path, on the spine.

    An alert that moved because the checks changed and an alert that moved
    because the project changed look identical in a count and call for
    opposite responses. #534 could not test this because there was only one
    released accepted-record check set; ADR-0090's v1 to v2 advance supplies
    the second.
    """

    baseline, current = _moved_promise(session, project)
    reading = _bind(
        session,
        project,
        current,
        previous_issue=_previous_issue(baseline, FIRST_ACCEPTED_RECORD_CHECK_SET),
    )
    artifacts = read_issue_artifacts(session, reading)

    change = artifacts.change_summary.check_set_change
    assert change.changed is True
    assert change.unknown is False
    assert change.previous == "accepted_record_date_checks_v1"
    assert change.current == "accepted_record_checks_v2"

    body = render_change_summary(artifacts.change_summary).text
    assert "accepted_record_date_checks_v1" in body
    assert "accepted_record_checks_v2" in body
    assert "change in the checks and not a change in your project" in body

    # And it stays out of the accepted-change section entirely: every change
    # named there is a Human Record Decision on the project record.
    assert [one.field for one in artifacts.change_summary.changes] == [
        "committed_date"
    ]
    assert all(one.decision_id for one in artifacts.change_summary.changes)
    assert all(
        one.disposition in ("accept", "edit")
        for one in artifacts.change_summary.changes
    )


def test_a_previous_issue_under_the_same_check_set_is_not_a_rules_change(
    session, project
):
    baseline, current = _moved_promise(session, project)
    reading = _bind(
        session,
        project,
        current,
        previous_issue=_previous_issue(baseline, ACCEPTED_RECORD_CHECK_SET),
    )
    artifacts = read_issue_artifacts(session, reading)

    change = artifacts.change_summary.check_set_change
    assert change.changed is False
    assert change.unknown is False
    body = render_change_summary(artifacts.change_summary).text
    assert "Both issues were built under the same set of checks" in body


def test_a_previous_issue_that_recorded_no_check_set_is_an_unknown_boundary(
    session, project
):
    """ADR-0044: an unrecorded set is unknown, never the current one."""

    baseline, current = _moved_promise(session, project)
    reading = _bind(
        session, project, current, previous_issue=_previous_issue(baseline, None)
    )
    artifacts = read_issue_artifacts(session, reading)

    change = artifacts.change_summary.check_set_change
    assert change.unknown is True
    assert change.changed is False
    body = render_change_summary(artifacts.change_summary).text
    assert "did not record which set of checks it was built under" in body
    assert "will not assume it was this one" in body


def test_a_previous_issue_naming_an_unreleased_check_set_is_refused(
    session, project
):
    """A name nobody released cannot be reported to a customer as a rules change."""

    baseline, current = _moved_promise(session, project)

    with pytest.raises(MixedIssueInputs):
        _bind(
            session,
            project,
            current,
            previous_issue=_previous_issue(baseline, "accepted_record_checks_v9"),
        )


def test_the_check_set_a_predecessor_recorded_is_part_of_the_reading_identity(
    session, project
):
    baseline, current = _moved_promise(session, project)
    first = _bind(
        session,
        project,
        current,
        previous_issue=_previous_issue(baseline, FIRST_ACCEPTED_RECORD_CHECK_SET),
    )
    second = _bind(
        session,
        project,
        current,
        previous_issue=_previous_issue(baseline, ACCEPTED_RECORD_CHECK_SET),
    )

    assert first.reading_identity != second.reading_identity


def test_the_ported_checks_touch_no_frozen_legacy_table():
    """ADR-0081 freezes them; ADR-0084 section 3 forbids extending them."""

    from pathlib import Path

    module = Path("src/corridor/issue_rendering.py").read_text(encoding="utf-8")
    for frozen in (
        "dependencies",
        "operative_support",
        "work_decisions",
        "dependency_events",
        "OperativeSupport",
        "WorkDecision",
        "contradicted_fields",
    ):
        assert frozen not in module, frozen
    for write in ("session.add", "session.merge", "session.delete", "insert(", "update("):
        assert write not in module, write


def _planned(session, project, *, question: str, return_date=RETURNS_AT):
    """One project with one recorded Follow-up Plan, read the production way."""

    _, baseline = _baseline(session, project)
    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2027-03-01",
        baseline_revision=baseline,
    )
    # The packet that records the plan writes the project's next accepted
    # revision, and that is the revision an issue prepared afterwards is bound
    # to, so it is the revision the plan is read at.
    result = _plan(
        session, project, delta, baseline, question=question, return_date=return_date
    )
    revision = int(result.revision_id)
    plans = read_adopted_follow_up_plans(
        session, project.id, revision, current=True, as_of=CUTOFF
    )
    return revision, plans


def test_the_follow_up_section_states_the_plans_the_records_actually_hold(
    session, project
):
    """The section is read from ``delta_follow_up_plans``, through one type.

    Every field printed here is one a Needs coordination outcome retained. The
    renderer used to declare its own type requiring a next-action sentence and
    an internal assignee, so no production caller could fill it and the section
    was structurally empty in every path that reaches a customer (#425).
    """

    baseline, plans = _planned(
        session, project, question="Has the relocation date changed?"
    )
    (plan,) = plans
    assert isinstance(plan, AcceptedFollowUpPlan)
    assert plan.target_subject_identity == SUBJECT
    assert plan.target_field == "committed_date"
    assert plan.responsible == "City Water"

    reading = _bind(session, project, baseline, follow_up_plans=plans)
    body = render_weekly_report(read_issue_artifacts(session, reading).weekly_report).text

    assert "City Water as the party who can answer it." in body
    assert "We need the answer by 2026-10-01." in body
    # The question, and beside it the position the record actually holds.
    assert "Has the relocation date changed?" in body
    assert "2026-11-01" in body
    # The incoming value is not accepted, so it is nowhere in the report.
    assert "2027-03-01" not in body


def test_the_follow_up_section_declares_the_next_action_it_cannot_state(
    session, project
):
    """No record holds a next action, so the section says so rather than none.

    ADR-0084 section 1 defines a Follow-up Plan as a question, a responsible
    party and a return date. Rendering nothing would have been the third silent
    absence in this area; the section declares it the way the alerts section
    declares the checks it does not run (ADR-0090).
    """

    baseline, plans = _planned(session, project, question="Who owns this valve?")
    reading = _bind(session, project, baseline, follow_up_plans=plans)
    body = render_weekly_report(read_issue_artifacts(session, reading).weekly_report).text

    for what, why in FOLLOW_UP_NOT_RETAINED:
        assert f"It does not state {what}" in body, what
        assert why in body, what


def test_a_plan_with_no_return_date_says_none_was_recorded(session, project):
    baseline, plans = _planned(
        session, project, question="Has the date moved?", return_date=None
    )
    reading = _bind(session, project, baseline, follow_up_plans=plans)
    body = render_weekly_report(read_issue_artifacts(session, reading).weekly_report).text

    assert "No return date has been recorded for this question." in body


def test_a_pending_question_that_is_not_a_question_is_refused(session, project):
    """A recorded plan whose question asserts a value never reaches a customer."""

    baseline, plans = _planned(
        session, project, question="The relocation date is now 2027-03-01."
    )

    with pytest.raises(MixedIssueInputs, match="asks a question"):
        _bind(session, project, baseline, follow_up_plans=plans)


def test_the_follow_up_section_refuses_a_shape_that_is_not_a_plan(session, project):
    """The one type is the contract, not a hint. #425's twin was not typed.

    ``bind_preparation`` and ``prepare_release_candidate`` both declared
    ``Sequence[Any]`` for this parameter, so handing the renderer the wrong
    dataclass was never an error and produced an empty section instead.
    """

    _, baseline = _baseline(session, project)

    class _Twin:
        target_subject_identity = SUBJECT
        open_question = "Has the relocation date changed?"

    with pytest.raises(MixedIssueInputs, match="retained records"):
        _bind(session, project, baseline, follow_up_plans=(_Twin(),))


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



def _rendered_years(body: str) -> set[str]:
    """The years of every date-shaped token in a rendered body."""

    return {match.group(1) for match in re.finditer(r"\b(\d{4})-\d{2}-\d{2}\b", body)}

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
    # Date-shaped tokens, not a bare substring. The body cites database ids —
    # "segment 2016", "support assessment 1261" — and a sequence climbs with
    # the rows a run has already made, so `"2016" not in body` fails whenever
    # some id happens to reach the year under test. It did, in a full-suite
    # run. What the test means is that no *date* in the body came from the
    # clock, which is what this asserts.
    assert _rendered_years(early[0].body).isdisjoint({"2016", "2036"})
    assert _rendered_years(late[0].body).isdisjoint({"2016", "2036"})


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
        fact_type="committed_date", value="2026-12-15",)
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
