from datetime import date

import pytest
from sqlalchemy import select

from corridor.changes import diff_since_last, record_run, snapshot
from corridor.db import Session, engine
from corridor.models import (
    Dependency,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
    ReportRun,
)


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
    d = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type="utility_relocation",
        title="Telecom — Example",
        internal_owner="Bryce",
        status=kw.pop("status", "identified"),
        criticality=kw.pop("criticality", "normal"),
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
        satisfies_requirement=satisfies,
    )
    session.add(link)
    session.flush()
    return link


# ----------------------------------------------------------------- snapshot


def test_a_snapshot_captures_state_and_its_ruleset(session, project, document):
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 9, 1))
    add_evidence(session, dep, document)

    snap = snapshot(session, project.id)
    assert snap["ruleset_version"]
    entry = snap["dependencies"]["DEP-1"]
    assert entry["status"] == "identified"
    assert entry["committed_date"] == "2026-09-01"
    assert entry["ready"] is False
    assert "ORPHAN" in entry["exceptions"]


# --------------------------------------------------------------------- diff


def test_the_first_report_says_so_rather_than_reporting_no_changes(
    session, project, document
):
    """"0 changes" reads as "nothing moved", which is a different claim."""
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)

    diff = diff_since_last(session, project.id)
    assert diff.is_first_report is True
    assert diff.changes == []


def test_a_new_dependency_is_reported_as_new(session, project, document):
    first = make_dep(session, project, "DEP-1")
    add_evidence(session, first, document)
    record_run(session, project.id)

    second = make_dep(session, project, "DEP-2")
    add_evidence(session, second, document)

    diff = diff_since_last(session, project.id)
    assert [c.ref_code for c in diff.of_kind("new")] == ["DEP-2"]


def test_a_later_committed_date_is_a_slip(session, project, document):
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 6, 3))
    add_evidence(session, dep, document)
    record_run(session, project.id)

    dep.committed_date = date(2026, 8, 15)
    session.flush()

    [change] = diff_since_last(session, project.id).of_kind("slipped")
    assert "2026-06-03" in change.detail and "2026-08-15" in change.detail


def test_an_earlier_committed_date_is_not_a_slip(session, project, document):
    """Pulling a date forward is good news, not a slip."""
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 8, 15))
    add_evidence(session, dep, document)
    record_run(session, project.id)

    dep.committed_date = date(2026, 6, 3)
    session.flush()

    assert diff_since_last(session, project.id).of_kind("slipped") == []


def test_becoming_ready_is_reported(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    link = add_evidence(session, dep, document)
    record_run(session, project.id)

    link.satisfies_requirement = True
    session.flush()

    [change] = diff_since_last(session, project.id).of_kind("became_ready")
    assert change.ref_code == "DEP-1"


def test_raising_criticality_is_an_escalation(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    record_run(session, project.id)

    dep.criticality = "critical"
    session.flush()

    escalations = diff_since_last(session, project.id).of_kind("escalated")
    assert any("normal" in c.detail and "critical" in c.detail for c in escalations)


def test_lowering_criticality_is_not_an_escalation(session, project, document):
    dep = make_dep(session, project, "DEP-1", criticality="critical")
    add_evidence(session, dep, document)
    record_run(session, project.id)

    dep.criticality = "normal"
    session.flush()

    escalations = diff_since_last(session, project.id).of_kind("escalated")
    assert not any("criticality" in c.detail for c in escalations)


def test_a_closed_dependency_is_reported_as_closed(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    record_run(session, project.id)

    dep.status = "closed"
    session.flush()

    [change] = diff_since_last(session, project.id).of_kind("closed")
    assert change.ref_code == "DEP-1"


def test_a_new_exception_is_reported(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    record_run(session, project.id)

    dep.internal_owner = None
    session.flush()

    escalations = diff_since_last(session, project.id).of_kind("escalated")
    assert any("MISSING_OWNER" in c.detail for c in escalations)


# ------------------------------------------------------- the ruleset guard


def test_a_ruleset_change_is_flagged(session, project, document):
    """A figure that moved must be attributable to rules or to data."""
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    record_run(session, project.id)

    run = latest_run(session, project)
    run.ruleset_version = "v0.0-old"
    session.flush()

    diff = diff_since_last(session, project.id)
    assert diff.ruleset_changed is True
    assert diff.previous_ruleset == "v0.0-old"


def test_exception_churn_is_suppressed_across_a_ruleset_change(
    session, project, document
):
    """Across a version change, a rule appearing may only mean the rule
    changed — reporting that as project movement would be a lie."""
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)
    record_run(session, project.id)

    run = latest_run(session, project)
    run.ruleset_version = "v0.0-old"
    dep.internal_owner = None  # would normally add MISSING_OWNER
    session.flush()

    diff = diff_since_last(session, project.id)
    assert not any("MISSING_OWNER" in c.detail for c in diff.changes)


def test_real_movement_still_reports_across_a_ruleset_change(
    session, project, document
):
    """Only exception churn is suppressed; a slipped date is a fact about
    the project regardless of which ruleset was in force."""
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 6, 3))
    add_evidence(session, dep, document)
    record_run(session, project.id)

    run = latest_run(session, project)
    run.ruleset_version = "v0.0-old"
    dep.committed_date = date(2026, 9, 9)
    session.flush()

    assert diff_since_last(session, project.id).of_kind("slipped")


# ------------------------------------------------------------------ storage


def test_recording_a_run_stores_its_ruleset_and_snapshot(session, project, document):
    dep = make_dep(session, project, "DEP-1")
    add_evidence(session, dep, document)

    run = record_run(session, project.id, output_path="out/report.html")
    assert run.ruleset_version
    assert "DEP-1" in run.snapshot_json["dependencies"]
    assert run.output_path == "out/report.html"


def test_the_diff_reads_the_most_recent_run(session, project, document):
    dep = make_dep(session, project, "DEP-1", committed_date=date(2026, 6, 3))
    add_evidence(session, dep, document)
    record_run(session, project.id)

    dep.committed_date = date(2026, 7, 1)
    session.flush()
    record_run(session, project.id)

    dep.committed_date = date(2026, 8, 1)
    session.flush()

    # Diffed against the July snapshot, not the June one.
    [change] = diff_since_last(session, project.id).of_kind("slipped")
    assert "2026-07-01" in change.detail
