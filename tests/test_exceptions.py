from datetime import date, timedelta

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.exceptions import (
    DUE_SOON_DAYS,
    RULES,
    RULESET_VERSION,
    STALE_DAYS,
    Thresholds,
    evaluate,
    exceptions_for,
)
from corridor.models import (
    Assertion,
    Candidate,
    Dependency,
    DependencyEvent,
    Document,
    EvidenceLink,
    ExternalOrg,
    Milestone,
    Project,
)

TODAY = date(2026, 8, 3)


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
    p = Project(slug="exc-test", name="Exceptions Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def document(session, project):
    d = Document(
        project_id=project.id,
        sha256="e1" * 32,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
        doc_date=TODAY,
    )
    session.add(d)
    session.flush()
    return d


def make_dep(session, project, ref="DEP-1", **kw):
    dep = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type="utility_relocation",
        title="Telecom — Example Utility",
        internal_owner=kw.pop("internal_owner", "Bryce"),
        status=kw.pop("status", "identified"),
        criticality=kw.pop("criticality", "normal"),
        **kw,
    )
    session.add(dep)
    session.flush()
    return dep


def add_evidence(
    session, dep, document, *, verified=True, satisfies=False, doc_date=None
):
    if doc_date is not None:
        document = Document(
            project_id=dep.project_id,
            sha256=f"{dep.id:02d}{doc_date.isoformat()}".ljust(64, "x")[:64],
            filename=f"note-{doc_date}.pdf",
            doc_type="minutes",
            parse_status="parsed",
            pages=1,
            doc_date=doc_date,
        )
        session.add(document)
        session.flush()
    link = EvidenceLink(
        dependency_id=dep.id,
        document_id=document.id,
        page_no=1,
        quote="a quote",
        verified=verified,
        satisfies_requirement=satisfies,
    )
    session.add(link)
    session.flush()
    return link


def codes(session, dep, today=TODAY):
    return {e.rule for e in exceptions_for(session, dep.id, today=today)}


# --------------------------------------------------------------- the ruleset


def test_the_ruleset_has_the_eight_documented_rules():
    assert set(RULES) == {
        "MISSING_OWNER",
        "MISSING_DATE",
        "MISSING_EVIDENCE",
        "STALE",
        "DUE_SOON",
        "OVERDUE",
        "CONTRADICTION",
        "ORPHAN",
    }


def test_thresholds_are_the_documented_ones():
    """v0-build-spec.md 9 fixes these; drifting them changes every count."""
    assert STALE_DAYS == 14
    assert DUE_SOON_DAYS == 30


def test_the_ruleset_carries_a_version():
    """Recorded on every report run and eval run, so a number that moved
    can be attributed to a rule change rather than a data change."""
    assert RULESET_VERSION


# ------------------------------------------------------------------- rules


def test_missing_owner_fires_without_an_internal_owner(session, project, document):
    dep = make_dep(session, project, internal_owner=None)
    add_evidence(session, dep, document)
    assert "MISSING_OWNER" in codes(session, dep)


def test_missing_owner_does_not_fire_on_a_closed_record(session, project, document):
    dep = make_dep(session, project, internal_owner=None, status="closed")
    assert "MISSING_OWNER" not in codes(session, dep)


def test_missing_date_fires_without_a_committed_date(session, project, document):
    dep = make_dep(session, project)
    add_evidence(session, dep, document)
    assert "MISSING_DATE" in codes(session, dep)


def test_missing_date_does_not_fire_once_a_date_exists(session, project, document):
    dep = make_dep(session, project, committed_date=TODAY + timedelta(days=60))
    add_evidence(session, dep, document)
    assert "MISSING_DATE" not in codes(session, dep)


def test_missing_evidence_fires_with_no_verified_evidence(session, project, document):
    dep = make_dep(session, project)
    add_evidence(session, dep, document, verified=False)
    assert "MISSING_EVIDENCE" in codes(session, dep)


def test_missing_evidence_cannot_fire_on_a_ready_record(session, project, document):
    """True by construction: readiness requires verified evidence (ADR-0002).

    If this ever fails, readiness has become settable some other way.
    """
    dep = make_dep(session, project)
    add_evidence(session, dep, document, verified=True, satisfies=True)
    found = codes(session, dep)
    assert "MISSING_EVIDENCE" not in found


def test_stale_fires_when_no_document_has_spoken_recently(session, project, document):
    """STALE measures document silence, not reviewer attention."""
    dep = make_dep(session, project)
    add_evidence(session, dep, document, doc_date=TODAY - timedelta(days=STALE_DAYS + 1))
    assert "STALE" in codes(session, dep)


def test_stale_does_not_fire_on_fresh_evidence(session, project, document):
    dep = make_dep(session, project)
    add_evidence(session, dep, document, doc_date=TODAY - timedelta(days=3))
    assert "STALE" not in codes(session, dep)


def test_stale_boundary_is_exclusive_at_the_threshold(session, project, document):
    """Exactly 14 days old is not yet stale; 15 is."""
    dep = make_dep(session, project, ref="DEP-edge")
    add_evidence(session, dep, document, doc_date=TODAY - timedelta(days=STALE_DAYS))
    assert "STALE" not in codes(session, dep)


def test_due_soon_fires_inside_the_window(session, project, document):
    dep = make_dep(session, project, need_date=TODAY + timedelta(days=10))
    add_evidence(session, dep, document)
    assert "DUE_SOON" in codes(session, dep)


def test_due_soon_does_not_fire_far_out(session, project, document):
    dep = make_dep(session, project, need_date=TODAY + timedelta(days=DUE_SOON_DAYS + 5))
    add_evidence(session, dep, document)
    assert "DUE_SOON" not in codes(session, dep)


def test_due_soon_does_not_fire_on_a_ready_record(session, project, document):
    dep = make_dep(session, project, need_date=TODAY + timedelta(days=5))
    add_evidence(session, dep, document, satisfies=True)
    assert "DUE_SOON" not in codes(session, dep)


def test_overdue_fires_on_a_past_committed_date(session, project, document):
    dep = make_dep(session, project, committed_date=TODAY - timedelta(days=1))
    add_evidence(session, dep, document)
    assert "OVERDUE" in codes(session, dep)


def test_overdue_does_not_fire_once_closed_out(session, project, document):
    """A closure event is what stops the clock, not the passage of time."""
    dep = make_dep(session, project, committed_date=TODAY - timedelta(days=30))
    add_evidence(session, dep, document)
    session.add(
        DependencyEvent(
            dependency_id=dep.id,
            event_type="closure",
            event_date=TODAY - timedelta(days=2),
            description="Relocation complete, clearance letter received",
            created_by="reviewer",
        )
    )
    session.flush()
    assert "OVERDUE" not in codes(session, dep)


def test_contradiction_fires_on_two_verified_disagreeing_claims(
    session, project, document
):
    dep = make_dep(session, project)
    a = add_evidence(session, dep, document)
    b = add_evidence(session, dep, document)
    session.add_all(
        [
            Assertion(
                dependency_id=dep.id,
                field_name="committed_date",
                asserted_value="2026-06-03",
                evidence_link_id=a.id,
            ),
            Assertion(
                dependency_id=dep.id,
                field_name="committed_date",
                asserted_value="2026-08-15",
                evidence_link_id=b.id,
            ),
        ]
    )
    session.flush()
    assert "CONTRADICTION" in codes(session, dep)


def test_agreeing_sources_are_not_a_contradiction(session, project, document):
    dep = make_dep(session, project)
    a = add_evidence(session, dep, document)
    b = add_evidence(session, dep, document)
    for link in (a, b):
        session.add(
            Assertion(
                dependency_id=dep.id,
                field_name="committed_date",
                asserted_value="2026-06-03",
                evidence_link_id=link.id,
            )
        )
    session.flush()
    assert "CONTRADICTION" not in codes(session, dep)


def test_an_unverified_claim_is_not_a_contradiction(session, project, document):
    """A bad citation is a bad citation, not a disagreement between sources."""
    dep = make_dep(session, project)
    good = add_evidence(session, dep, document, verified=True)
    bad = add_evidence(session, dep, document, verified=False)
    session.add_all(
        [
            Assertion(
                dependency_id=dep.id,
                field_name="committed_date",
                asserted_value="2026-06-03",
                evidence_link_id=good.id,
            ),
            Assertion(
                dependency_id=dep.id,
                field_name="committed_date",
                asserted_value="1999-01-01",
                evidence_link_id=bad.id,
            ),
        ]
    )
    session.flush()
    assert "CONTRADICTION" not in codes(session, dep)


def test_a_column_absent_from_one_source_is_not_a_contradiction(
    session, project, document
):
    """From #2: a column present in some matrix revisions and absent in
    others must not read as a changed value."""
    dep = make_dep(session, project)
    a = add_evidence(session, dep, document)
    b = add_evidence(session, dep, document)
    session.add_all(
        [
            Assertion(
                dependency_id=dep.id,
                field_name="sue_level",
                asserted_value="B",
                evidence_link_id=a.id,
            ),
            Assertion(
                dependency_id=dep.id,
                field_name="sue_level",
                asserted_value=None,
                evidence_link_id=b.id,
            ),
        ]
    )
    session.flush()
    assert "CONTRADICTION" not in codes(session, dep)


def test_orphan_fires_without_a_milestone(session, project, document):
    dep = make_dep(session, project)
    add_evidence(session, dep, document)
    assert "ORPHAN" in codes(session, dep)


def test_orphan_clears_once_linked(session, project, document):
    from corridor.milestones import link_dependency

    milestone = Milestone(
        project_id=project.id,
        code="UTIL-CLEAR",
        name="Utility clearance",
        need_date=TODAY + timedelta(days=200),
    )
    session.add(milestone)
    session.flush()
    dep = make_dep(session, project)
    add_evidence(session, dep, document)
    link_dependency(session, dep, milestone)
    assert "ORPHAN" not in codes(session, dep)


# ------------------------------------------------------------------ severity


def test_severity_scales_with_criticality(session, project, document):
    normal = make_dep(session, project, ref="DEP-n", internal_owner=None)
    critical = make_dep(
        session, project, ref="DEP-c", internal_owner=None, criticality="critical"
    )
    for dep in (normal, critical):
        add_evidence(session, dep, document)

    n = next(e for e in exceptions_for(session, normal.id, today=TODAY) if e.rule == "MISSING_OWNER")
    c = next(e for e in exceptions_for(session, critical.id, today=TODAY) if e.rule == "MISSING_OWNER")
    assert c.severity > n.severity


# ------------------------------------------------------------------ project


def test_evaluate_returns_every_records_exceptions(session, project, document):
    a = make_dep(session, project, ref="DEP-a", internal_owner=None)
    b = make_dep(session, project, ref="DEP-b")
    for dep in (a, b):
        add_evidence(session, dep, document)

    result = evaluate(session, project.id, today=TODAY)
    by_dep = {e.dependency_id for e in result}
    assert {a.id, b.id} <= by_dep
    assert any(e.rule == "MISSING_OWNER" and e.dependency_id == a.id for e in result)


def test_evaluate_sorts_worst_first(session, project, document):
    low = make_dep(session, project, ref="DEP-low")
    high = make_dep(
        session,
        project,
        ref="DEP-high",
        internal_owner=None,
        criticality="critical",
        committed_date=TODAY - timedelta(days=5),
    )
    for dep in (low, high):
        add_evidence(session, dep, document)

    result = evaluate(session, project.id, today=TODAY)
    assert result[0].severity >= result[-1].severity


def test_thresholds_are_configurable_per_project(session, project, document):
    dep = make_dep(session, project)
    add_evidence(session, dep, document, doc_date=TODAY - timedelta(days=20))
    assert "STALE" in codes(session, dep)

    relaxed = Thresholds(stale_days=60, due_soon_days=30)
    found = {
        e.rule for e in exceptions_for(session, dep.id, today=TODAY, thresholds=relaxed)
    }
    assert "STALE" not in found


def test_every_exception_explains_itself(session, project, document):
    """A count with no explanation is not actionable."""
    dep = make_dep(session, project, internal_owner=None)
    add_evidence(session, dep, document)
    for exception in exceptions_for(session, dep.id, today=TODAY):
        assert exception.detail
        assert exception.rule in RULES
