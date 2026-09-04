from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.changes import (
    accepted_revision_id,
    diff_since_last,
    record_run,
    snapshot,
)
from corridor.check_configuration import effective_thresholds, save_configuration
from corridor.db import Session, engine
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.exceptions import evaluate_project
from corridor.ledger import mark_satisfies
from corridor.models import (
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
    ProjectRecordRevision,
    ReportRun,
)
from corridor.principals import HumanPrincipal


TEST_PRINCIPAL = HumanPrincipal("local:changes-reviewer")


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def project(session):
    p = Project(slug="chg-test", name="Changes Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def document(session, project):
    d = Document(
        project_id=project.id,
        sha256="c9" * 32,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
        doc_date=date(2026, 8, 1),
    )
    session.add(d)
    session.flush()
    return d


def latest_run(session, project):
    """Scoped to this project.

    `make demo` commits real report runs to the same database, so an
    unscoped query picks those up instead of the fixture's — the third time
    this pattern has bitten, after the report and merge tests.
    """
    return session.scalars(
        select(ReportRun)
        .where(ReportRun.project_id == project.id)
        .order_by(ReportRun.id.desc())
    ).first()


def make_dep(session, project, ref, **kw):
    kw.pop("status", None)
    d = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type="utility_relocation",
        title="Telecom — Example",
        internal_owner="Bryce",
        resolution_strategy=kw.pop("resolution_strategy", None),
        **kw,
    )
    session.add(d)
    session.flush()
    return d


def add_evidence(session, dep, document, *, satisfies=False):
    link = EvidenceLink(
        dependency_id=dep.id,
        document_id=document.id,
        page_no=1,
        quote="a quote",
        verified=True,
    )
    session.add(link)
    session.flush()
    if satisfies:
        mark_satisfies(
            session,
            dep.id,
            link.id,
            principal=TEST_PRINCIPAL,
        )
    if dep.committed_date is not None:
        _record_exact_cited_statement(
            session,
            dep,
            document,
            committed_date=dep.committed_date,
            event_date=dep.committed_date,
        )
    return link


def _record_exact_cited_statement(
    session, dep, document, *, committed_date: date, event_date: date
):
    """Give a test record an authoritative exact-day external statement."""
    if dep.external_org_id is None:
        party = ExternalOrg(name=f"{dep.ref_code} Test Party")
        session.add(party)
        session.flush()
        dep.external_org_id = party.id
    else:
        party = session.get(ExternalOrg, dep.external_org_id)
    assert party is not None
    quote = f"{party.name} will complete on {committed_date.isoformat()}."
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == 1,
        )
    )
    if page is None:
        page = DocPage(document_id=document.id, page_no=1, text=quote)
        session.add(page)
    else:
        page.text = f"{page.text}\n{quote}"
    return record_external_party_statement(
        session,
        project_id=dep.project_id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=event_date,
        description=quote,
        new_timing=StatementTiming.day(committed_date.isoformat(), committed_date),
        scope=StatementScope.selected((dep.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id,
            1,
            quote,
        ),
    )


# ----------------------------------------------------------------- snapshot


# These three require their evaluation rather than defaulting to a fresh one,
# so a report cannot publish one reading and snapshot another. Each test
# publishes at the moment it calls, which is what these say.
def _snapshot(session, project):
    return snapshot(
        session, project.id, evaluation=evaluate_project(session, project.id)
    )


def _diff(session, project):
    return diff_since_last(
        session, project.id, evaluation=evaluate_project(session, project.id)
    )


def _record(session, project, **kwargs):
    return record_run(
        session, project.id, evaluation=evaluate_project(session, project.id), **kwargs
    )


@pytest.mark.parametrize("operation", (snapshot, record_run))
def test_snapshot_writers_refuse_an_evaluation_from_another_project(
    session, project, operation
):
    foreign = Project(
        slug=f"foreign-{operation.__name__}", name="Foreign", is_synthetic=True
    )
    session.add(foreign)
    session.flush()

    with pytest.raises(ValueError, match="evaluation belongs to another project"):
        operation(
            session,
            project.id,
            evaluation=evaluate_project(session, foreign.id),
        )


@pytest.mark.parametrize("operation", (snapshot, record_run))
def test_snapshot_writers_refuse_dates_that_disagree_with_the_evaluation(
    session, project, operation
):
    dependency = make_dep(session, project, f"DEP-{operation.__name__}")
    evaluation = evaluate_project(session, project.id)

    with pytest.raises(ValueError, match="different Committed Date readings"):
        operation(
            session,
            project.id,
            evaluation=evaluation,
            committed_dates={dependency.id: date(2026, 9, 1)},
        )


def test_a_snapshot_captures_state_and_its_ruleset(session, project, document):
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 9, 1))
    add_evidence(session, dep, document)

    snap = _snapshot(session, project)
    assert snap["ruleset_version"]
    entry = snap["dependencies"]["DEP-1"]
    assert "status" not in entry
    assert entry["published_promised_for"] == "2026-09-01"
    assert entry["documentation_requirement_met"] is False
    assert "ORPHAN" in entry["constraint_alerts"]


# --------------------------------------------------------------------- diff


def test_the_first_report_says_so_rather_than_reporting_no_changes(
    session, project, document
):
    """ "0 changes" reads as "nothing moved", which is a different claim."""
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)

    diff = _diff(session, project)
    assert diff.is_first_report is True
    assert diff.changes == []


def test_a_new_dependency_is_reported_as_new(session, project, document):
    first = make_dep(session, project, "DEP-1")
    add_evidence(session, first, document)
    _record(session, project)

    second = make_dep(session, project, "DEP-2")
    add_evidence(session, second, document)

    diff = _diff(session, project)
    assert [c.ref_code for c in diff.of_kind("new")] == ["DEP-2"]


def test_a_later_committed_date_is_a_committed_date_change(session, project, document):
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 6, 3))
    add_evidence(session, dep, document)
    _record(session, project)

    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 7, 1),
        committed_date=date(2026, 8, 15),
    )

    [change] = _diff(session, project).of_kind("committed_date_change")
    assert "2026-06-03" in change.detail and "2026-08-15" in change.detail


def test_snapshot_and_diff_read_the_current_statement_over_a_stale_scalar(
    session, project, document
):
    """A historical snapshot stays readable while new reads use the statement."""
    party = ExternalOrg(name="Changes Test Party")
    session.add(party)
    session.flush()
    dep = make_dep(
        session,
        project,
        "DEP-1",
        external_org_id=party.id,
        committed_date=date(2026, 6, 3),
    )
    add_evidence(session, dep, document)
    _record(session, project)

    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 7, 1),
        committed_date=date(2026, 8, 15),
    )
    # A stale materialized scalar is not a fallback authority.
    dep.committed_date = date(2026, 6, 3)
    session.flush()

    assert (
        _snapshot(session, project)["dependencies"]["DEP-1"][
            "published_promised_for"
        ]
        == "2026-08-15"
    )
    [change] = _diff(session, project).of_kind("committed_date_change")
    assert "2026-06-03" in change.detail and "2026-08-15" in change.detail


def test_an_earlier_committed_date_is_a_committed_date_change(
    session, project, document
):
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 8, 15))
    add_evidence(session, dep, document)
    _record(session, project)

    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 9, 1),
        committed_date=date(2026, 6, 3),
    )

    [change] = _diff(session, project).of_kind("committed_date_change")
    assert "2026-08-15" in change.detail
    assert "2026-06-03" in change.detail


def test_becoming_ready_is_reported(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    link = add_evidence(session, dep, document)
    _record(session, project)

    mark_satisfies(
        session,
        dep.id,
        link.id,
        principal=TEST_PRINCIPAL,
    )

    [change] = _diff(session, project).of_kind("became_ready")
    assert change.ref_code == "DEP-1"


def test_a_strategy_becoming_critical_is_an_escalation(session, project, document):
    dep = make_dep(session, project, "DEP-1", resolution_strategy="protect_in_place")
    add_evidence(session, dep, document)
    _record(session, project)

    dep.resolution_strategy = "relocate"
    session.flush()

    escalations = _diff(session, project).of_kind("escalated")
    assert any("relocate" in c.detail and "critical" in c.detail for c in escalations)


def test_a_strategy_ceasing_to_be_critical_is_not_an_escalation(
    session, project, document
):
    dep = make_dep(session, project, "DEP-1", resolution_strategy="relocate")
    add_evidence(session, dep, document)
    _record(session, project)

    dep.resolution_strategy = "adjust_vertical"
    session.flush()

    escalations = _diff(session, project).of_kind("escalated")
    assert not any("strategy" in c.detail for c in escalations)


def test_a_snapshot_written_before_the_strategy_existed_does_not_escalate(
    session, project, document
):
    """Four report runs predate #96 and carry a `criticality` key instead.

    Reading an absent key as "was not critical" would announce an
    escalation for every record that merely became readable — a change in
    the schema reported as a change in the world, on the first report after
    the migration.
    """
    dep = make_dep(session, project, "DEP-1", resolution_strategy="relocate")
    add_evidence(session, dep, document)
    run = _record(session, project)

    # Rewrite the stored snapshot into its pre-#96 shape.
    snapshot = dict(run.snapshot_json)
    for entry in snapshot["dependencies"].values():
        entry.pop("resolution_strategy", None)
        entry["criticality"] = "normal"
    run.snapshot_json = snapshot
    session.flush()

    assert _diff(session, project).of_kind("escalated") == []


def test_a_new_exception_is_reported(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    _record(session, project)

    dep.internal_owner = None
    session.flush()

    escalations = _diff(session, project).of_kind("escalated")
    assert any("MISSING_OWNER" in c.detail for c in escalations)


# ------------------------------------------------------- the ruleset guard


def test_a_ruleset_change_is_flagged(session, project, document):
    """A figure that moved must be attributable to rules or to data."""
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    _record(session, project)

    run = latest_run(session, project)
    run.ruleset_version = "v0.0-old"
    session.flush()

    diff = _diff(session, project)
    assert diff.ruleset_changed is True
    assert diff.previous_ruleset == "v0.0-old"


def test_exception_churn_is_suppressed_across_a_ruleset_change(
    session, project, document
):
    """Across a version change, a rule appearing may only mean the rule
    changed — reporting that as project movement would be a lie."""
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    _record(session, project)

    run = latest_run(session, project)
    run.ruleset_version = "v0.0-old"
    dep.internal_owner = None  # would normally add MISSING_OWNER
    session.flush()

    diff = _diff(session, project)
    assert not any("MISSING_OWNER" in c.detail for c in diff.changes)


def test_real_movement_still_reports_across_a_ruleset_change(
    session, project, document
):
    """Only exception churn is suppressed; a Committed Date Change is a fact about
    the project regardless of which ruleset was in force."""
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 6, 3))
    add_evidence(session, dep, document)
    _record(session, project)

    run = latest_run(session, project)
    run.ruleset_version = "v0.0-old"
    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 7, 1),
        committed_date=date(2026, 9, 9),
    )

    assert _diff(session, project).of_kind("committed_date_change")


# ------------------------------------------------------------------ storage


def test_recording_a_run_stores_its_ruleset_and_snapshot(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)

    run = _record(session, project, output_path="out/report.html")
    assert run.ruleset_version
    assert "DEP-1" in run.snapshot_json["dependencies"]
    assert run.output_path == "out/report.html"


def test_the_diff_reads_the_most_recent_run(session, project, document):
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 6, 3))
    add_evidence(session, dep, document)
    _record(session, project)

    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 6, 15),
        committed_date=date(2026, 7, 1),
    )
    _record(session, project)

    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 7, 15),
        committed_date=date(2026, 8, 1),
    )

    # Diffed against the July snapshot, not the June one.
    [change] = _diff(session, project).of_kind("committed_date_change")
    assert "2026-07-01" in change.detail


def test_the_predecessor_is_the_id_watermark_not_the_wall_clock(
    session, project, document
):
    """Two runs whose recorded times disagree with the order they were written.

    A clock adjustment, a replayed or backfilled run, or two writers on
    different hosts can leave the newer row carrying the older ``ts``. Ordering
    by ``ts`` then diffs this week's reading against a run that is not the one
    before it, and the result is a well-formed diff about the wrong pair, which
    nothing downstream can detect. The id is append-only, so it is what names
    the predecessor (#634).

    Every timestamp here is set explicitly: a test about clock independence
    that read the clock would prove nothing.
    """

    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 6, 3))
    add_evidence(session, dep, document)
    first = _record(session, project)
    first.ts = datetime(2026, 7, 20, tzinfo=timezone.utc)

    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 6, 15),
        committed_date=date(2026, 7, 1),
    )
    second = _record(session, project)
    second.ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
    session.flush()

    # The second run is the later row and carries the earlier recorded time.
    assert second.id > first.id
    assert second.ts < first.ts

    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 7, 15),
        committed_date=date(2026, 8, 1),
    )

    diff = _diff(session, project)
    assert diff.previous_run_id == second.id
    [change] = diff.of_kind("committed_date_change")
    assert "2026-07-01" in change.detail and "2026-08-01" in change.detail


# ------------------------------------------- the configuration guard (#339)


def _declare(session, project, **thresholds):
    """Declare a full per-project check configuration."""
    proposed = {"stale_days": 14, "due_soon_days": 30, "action_due_soon_days": 7}
    proposed.update(thresholds)
    return save_configuration(
        session, project.id, proposed, principal=TEST_PRINCIPAL
    )


def _effective_evaluation(session, project):
    """The reading a report publishes: under the project's declared thresholds.

    The real path resolves the effective configuration and hands the bound
    Evaluation to the snapshot writers; these mirror that so a snapshot records
    the thresholds a report was actually published against.
    """
    return evaluate_project(
        session, project.id, thresholds=effective_thresholds(session, project.id)
    )


def _record_eff(session, project):
    return record_run(
        session, project.id, evaluation=_effective_evaluation(session, project)
    )


def _diff_eff(session, project):
    return diff_since_last(
        session, project.id, evaluation=_effective_evaluation(session, project)
    )


def test_a_snapshot_records_its_thresholds(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    snap = _snapshot(session, project)
    assert snap["thresholds"] == {
        "stale_days": 14,
        "due_soon_days": 30,
        "action_due_soon_days": 7,
    }


def test_a_threshold_configuration_change_is_flagged(session, project, document):
    """A count that moved must be attributable to a setting or to data."""
    dep = make_dep(
        session, project, "DUE-1", need_date=date.today() + timedelta(days=20)
    )
    add_evidence(session, dep, document)
    _declare(session, project, due_soon_days=10)  # DUE_SOON silent at 10d
    _record_eff(session, project)

    _declare(session, project, due_soon_days=30)  # loosened; DUE_SOON now fires
    diff = _diff_eff(session, project)
    assert diff.configuration_changed is True
    assert diff.configuration_unknown is False
    assert diff.calculation_inputs_changed is True


def test_configuration_driven_alert_churn_is_not_project_movement(
    session, project, document
):
    """DUE_SOON appearing only because the horizon widened is not deterioration."""
    dep = make_dep(
        session, project, "DUE-1", need_date=date.today() + timedelta(days=20)
    )
    add_evidence(session, dep, document)
    _declare(session, project, due_soon_days=10)
    _record_eff(session, project)
    # Confirm the baseline really had no DUE_SOON.
    assert "DUE_SOON" not in latest_run(session, project).snapshot_json[
        "dependencies"
    ]["DUE-1"]["constraint_alerts"]

    _declare(session, project, due_soon_days=30)
    diff = _diff_eff(session, project)
    assert not any("DUE_SOON" in c.detail for c in diff.changes)


def test_real_movement_still_reports_across_a_configuration_change(
    session, project, document
):
    """A Committed Date Change is a project fact regardless of the thresholds."""
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 6, 3))
    add_evidence(session, dep, document)
    _declare(session, project, due_soon_days=10)
    _record_eff(session, project)

    _declare(session, project, due_soon_days=30)  # a configuration change …
    _record_exact_cited_statement(
        session,
        dep,
        document,
        event_date=date(2026, 7, 1),
        committed_date=date(2026, 9, 9),
    )
    # … does not hide the real Committed Date Change.
    diff = _diff_eff(session, project)
    assert diff.configuration_changed is True
    assert diff.of_kind("committed_date_change")


def test_a_snapshot_without_recorded_thresholds_is_unknown_not_default(
    session, project, document
):
    """A pre-#339 snapshot lacks the key; that is unknown, never the defaults."""
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    run = _record(session, project)

    snap = dict(run.snapshot_json)
    snap.pop("thresholds", None)
    run.snapshot_json = snap
    dep.internal_owner = None  # would normally add MISSING_OWNER
    session.flush()

    diff = _diff(session, project)
    assert diff.configuration_unknown is True
    assert diff.previous_thresholds is None
    # Not backfilled from current defaults, and churn is not project movement.
    assert not any("MISSING_OWNER" in c.detail for c in diff.changes)


def test_no_configuration_boundary_when_thresholds_are_unchanged(
    session, project, document
):
    """The ordinary case: same thresholds across two reports is no boundary."""
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    _record(session, project)

    dep.internal_owner = None
    session.flush()
    diff = _diff(session, project)
    assert diff.configuration_changed is False
    assert diff.configuration_unknown is False
    # A genuine new exception within one configuration still reports.
    assert any("MISSING_OWNER" in c.detail for c in diff.changes)


# --- The accepted revision a run is bound to (#602) -------------------------
#
# A Report Run used to carry only ``snapshot_json``: a copy of state with
# nothing saying which accepted Project Record revision it was taken against,
# so it could only ever be compared with itself. These cover the reference
# that replaces that, and the two independent guards that keep it honest.


def _accepted_revision(session, project, key: str) -> ProjectRecordRevision:
    """One accepted revision of ``project``, written as the decision role.

    A revision is written only by the record-decision role's own commands, and
    reaching one here would mean adopting a baseline to prove a property of the
    report binding rather than of the adoption.  The role is taken for this
    statement and handed back immediately.
    """

    # Flush anything still pending as the ordinary role: the decision role is
    # taken for this one insert and nothing else.
    session.flush()
    session.execute(text("set local role corridor_fact_decision_writer"))
    revision = ProjectRecordRevision(
        project_id=project.id,
        command_type="record_verbal_statement",
        human_principal="local:changes-reviewer",
        idempotency_key=key,
    )
    session.add(revision)
    session.flush()
    session.execute(text("reset role"))
    return revision


def _insert_report_run_directly(session, project_id, *, revision_id):
    """Insert straight into the relation, past every Python writer."""

    return session.execute(
        text(
            "insert into report_runs ("
            "project_id, revision_id, ruleset_version, snapshot_json, "
            "document_only"
            ") values ("
            ":project_id, :revision_id, 'r1', '{}'::jsonb, false"
            ") returning id"
        ),
        {"project_id": project_id, "revision_id": revision_id},
    ).scalar_one()


def test_a_recorded_run_names_the_accepted_revision_it_was_produced_against(
    session, project, document
):
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    revision = _accepted_revision(session, project, "changes:bound-run")

    run = _record(session, project)

    assert run.revision_id == revision.id
    assert accepted_revision_id(session, project.id) == revision.id


def test_a_second_run_names_the_revision_standing_when_it_was_written(
    session, project, document
):
    """The binding moves with the record, and an earlier run keeps its own."""

    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    first_revision = _accepted_revision(session, project, "changes:first")
    first_run = _record(session, project)

    second_revision = _accepted_revision(session, project, "changes:second")
    second_run = _record(session, project)

    assert second_revision.id > first_revision.id
    assert first_run.revision_id == first_revision.id
    assert second_run.revision_id == second_revision.id


def test_a_project_with_no_accepted_revision_records_an_explicit_absence(
    session, project, document
):
    """Absence is an answer, not a missing value.

    A legacy project whose accepted record is not on the spine has no revision
    identity to name.  Naming one anyway — a zero, or some other project's
    newest — would be exactly the false reference this binding exists to
    prevent, so the run records none and the database accepts it.
    """

    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)

    assert accepted_revision_id(session, project.id) is None

    run = _record(session, project)

    assert run.revision_id is None
    assert session.scalar(
        select(ReportRun.revision_id).where(ReportRun.id == run.id)
    ) is None


def test_the_database_refuses_a_new_run_that_names_no_accepted_revision(
    session, project
):
    """The rule lives in PostgreSQL, not in the one Python writer.

    ``record_run`` resolves the binding itself, so this reaches past it and
    inserts the row directly.  Nothing else in the statement can be refused:
    every other column is supplied and valid, and a null binding trips no
    foreign key.
    """

    _accepted_revision(session, project, "changes:refusal")

    with pytest.raises(DBAPIError) as refusal:
        _insert_report_run_directly(session, project.id, revision_id=None)

    assert "names the accepted Project Record revision" in str(refusal.value)


def test_the_database_refuses_a_run_bound_to_another_projects_revision(
    session, project
):
    """The second, independent guard: the composite key, not the trigger."""

    other = Project(slug="chg-foreign-revision", name="Foreign", is_synthetic=True)
    session.add(other)
    session.flush()
    foreign = _accepted_revision(session, other, "changes:foreign")
    _accepted_revision(session, project, "changes:own")

    with pytest.raises(IntegrityError) as refusal:
        _insert_report_run_directly(session, project.id, revision_id=foreign.id)

    assert "fk_report_runs_revision" in str(refusal.value)


def test_a_recorded_binding_is_not_rewritten(session, project, document):
    """A run says which revision it was produced against, once and for good."""

    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    revision = _accepted_revision(session, project, "changes:immutable")
    run = _record(session, project)
    later = _accepted_revision(session, project, "changes:immutable-later")
    assert run.revision_id == revision.id

    with pytest.raises(DBAPIError) as refusal:
        session.execute(
            text(
                "update report_runs set revision_id = :later where id = :run_id"
            ),
            {"later": later.id, "run_id": run.id},
        )

    assert "is not rewritten" in str(refusal.value)
