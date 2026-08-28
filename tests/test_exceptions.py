import hashlib
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
    evaluate_project,
    facets,
    exceptions_for,
    format_exception_label,
)
from corridor.models import (
    Assertion,
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventTiming,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Milestone,
    Project,
)
from corridor.ledger import mark_satisfies
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.operative_support import (
    designate_publication_support,
    resolve_operative_support,
)
from corridor.principals import HumanPrincipal
from corridor.supersession import SupersessionDeclaration, register_supersessions

TODAY = date(2026, 8, 3)
TEST_PRINCIPAL = HumanPrincipal("local:exceptions-reviewer")


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
    committed_date = kw.pop("committed_date", None)
    kw.pop("status", None)
    dep = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type="utility_relocation",
        title="Telecom — Example Utility",
        internal_owner=kw.pop("internal_owner", "Bryce"),
        resolution_strategy=kw.pop("resolution_strategy", None),
        **kw,
    )
    session.add(dep)
    session.flush()
    if committed_date is not None:
        party = ExternalOrg(name=f"Example Utility {dep.id}")
        session.add(party)
        session.flush()
        dep.external_org_id = party.id
        event_document = Document(
            project_id=project.id,
            sha256=f"statement-{dep.id}".ljust(64, "x")[:64],
            filename=f"statement-{dep.id}.pdf",
            doc_type="minutes",
            parse_status="parsed",
            pages=1,
            doc_date=TODAY - timedelta(days=60),
        )
        session.add(event_document)
        session.flush()
        quote = "Example Utility stated its completion date."
        session.add(
            DocPage(
                document_id=event_document.id,
                page_no=1,
                text=quote,
                image_path=None,
            )
        )
        session.flush()
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=TODAY - timedelta(days=60),
            description=quote,
            new_timing=StatementTiming.day(committed_date.isoformat(), committed_date),
            scope=StatementScope.selected((dep.id,)),
            created_by="corridor:event-admission",
            evidence=CitedStatementEvidence(event_document.id, 1, quote),
        )
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
    return link


def codes(session, dep, today=TODAY):
    return {e.rule for e in exceptions_for(session, dep.id, today=today)}


def register_chain(session, project, documents, replacement_dates):
    """Declare a test revision chain from one authority-index page.

    The index is a document of its own rather than the chain's first
    revision: a predecessor cannot be the authority for its own
    replacement, since the replacement postdates it.
    """
    assert len(documents) == len(replacement_dates) + 1
    for document in documents:
        document.registry_id = f"exceptions-{project.id}-{document.id}"
    source = Document(
        project_id=project.id,
        sha256=hashlib.sha256(f"exceptions-index-{project.id}".encode()).hexdigest(),
        filename="authority-index.pdf",
        doc_type="other",
        parse_status="parsed",
        pages=1,
        registry_id=f"exceptions-{project.id}-index",
    )
    session.add(source)
    session.flush()
    session.add(
        DocPage(
            document_id=source.id,
            page_no=1,
            text="authority supersession index",
        )
    )
    session.flush()
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=replacement_date,
                source_registry_id=source.registry_id,
                source_page=1,
            )
            for predecessor, successor, replacement_date in zip(
                documents[:-1],
                documents[1:],
                replacement_dates,
                strict=True,
            )
        ],
        project_id=project.id,
    )


# --------------------------------------------------------------- the ruleset


def test_the_ruleset_has_the_twelve_documented_rules():
    assert set(RULES) == {
        "MISSING_OWNER",
        "MISSING_DATE",
        "MISSING_EVIDENCE",
        "STALE",
        "DUE_SOON",
        "OVERDUE",
        "CONTRADICTION",
        "ORPHAN",
        "SUPERSEDED_CITATION",
        "MISSING_ACTION",
        "ACTION_DUE_SOON",
        "ACTION_OVERDUE",
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


def test_missing_owner_does_not_fire_on_a_ready_record(session, project, document):
    dep = make_dep(session, project, internal_owner=None)
    add_evidence(session, dep, document, verified=True, satisfies=True)
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
    add_evidence(
        session, dep, document, doc_date=TODAY - timedelta(days=STALE_DAYS + 1)
    )
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
    dep = make_dep(
        session, project, need_date=TODAY + timedelta(days=DUE_SOON_DAYS + 5)
    )
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


def test_overdue_reads_the_current_exact_day_statement_not_a_stale_scalar(
    session, project, document
):
    dep = make_dep(session, project)
    add_evidence(session, dep, document)
    party = ExternalOrg(name="Example Utility")
    session.add(party)
    session.flush()
    dep.external_org_id = party.id
    quote = "Example Utility committed to a past date."
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=quote,
            image_path=None,
        )
    )
    session.flush()
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=TODAY - timedelta(days=30),
        description=quote,
        new_timing=StatementTiming.day(
            (TODAY - timedelta(days=1)).isoformat(), TODAY - timedelta(days=1)
        ),
        scope=StatementScope.selected((dep.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )
    assert dep.committed_date == TODAY - timedelta(days=1)

    assert "OVERDUE" in codes(session, dep)


def test_a_current_month_statement_suppresses_overdue_from_a_stale_scalar(
    session, project, document
):
    """A month statement is not a Dependency-level exact date calculation."""
    dep = make_dep(session, project)
    add_evidence(session, dep, document)
    event = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=dep.external_org_id,
        stated_external_org_id=dep.external_org_id,
        scope_mode="selected",
        event_type="commitment",
        source_kind="cited",
        stated_party="Example Utility",
        event_date=TODAY - timedelta(days=30),
        description="Example Utility now expects completion in August 2026.",
        created_by="corridor:event-admission",
    )
    session.add(event)
    session.flush()
    session.add_all(
        (
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text="August 2026",
                precision="month",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
            ),
            DependencyEventScope(event_id=event.id, dependency_id=dep.id),
        )
    )
    session.flush()

    assert "OVERDUE" not in codes(session, dep)


def test_overdue_does_not_fire_once_closed_out(session, project, document):
    """A closure event is what stops the clock, not the passage of time."""
    dep = make_dep(session, project, committed_date=TODAY - timedelta(days=30))
    add_evidence(session, dep, document)
    event = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=dep.external_org_id,
        stated_external_org_id=dep.external_org_id,
        scope_mode="selected",
        event_type="closure",
        event_date=TODAY - timedelta(days=2),
        description="Relocation complete, clearance letter received",
        created_by="local:closure-reviewer",
    )
    session.add(event)
    session.flush()
    session.add(DependencyEventScope(event_id=event.id, dependency_id=dep.id))
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
    link_dependency(session, dep, milestone, actor="tester")
    assert "ORPHAN" not in codes(session, dep)


def test_exceptions_lapse_readiness_without_recasting_it_as_schedule_failure(
    session, project, document
):
    successor = Document(
        project_id=project.id,
        sha256="e2" * 32,
        filename="matrix-successor.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
        doc_date=TODAY,
    )
    session.add(successor)
    session.flush()
    dependency = make_dep(
        session,
        project,
        ref="DEP-superseded-ready",
        need_date=TODAY + timedelta(days=5),
    )
    link = add_evidence(
        session,
        dependency,
        document,
        verified=True,
        satisfies=True,
    )
    register_chain(session, project, [document, successor], [TODAY])

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]
    found = codes(session, dependency)

    assert {support.evidence_link_id for support in resolved.readiness} == {link.id}
    assert resolved.current_readiness == ()
    assert resolved.is_ready is False
    assert set(resolved.superseded_roles) == {"readiness"}
    assert "DUE_SOON" not in found
    assert "SUPERSEDED_CITATION" in found
    # The historical quote remains mechanically verified even though it is
    # no longer current enough to satisfy readiness.
    assert "MISSING_EVIDENCE" not in found
    assert link.verified is True
    assert link.id in {support.evidence_link_id for support in resolved.readiness}


def test_registering_a_successor_immediately_creates_provenance_review_work(
    session, project, document
):
    """The registry event is enough; no successor extraction gates the signal."""
    successor = Document(
        project_id=project.id,
        sha256="e3" * 32,
        filename="matrix-successor-not-extracted.pdf",
        doc_type="matrix",
        parse_status="pending",
        pages=1,
        # Deliberately unrelated to the authority's replacement date.
        doc_date=date(2025, 1, 1),
    )
    session.add(successor)
    session.flush()
    dependency = make_dep(session, project, ref="DEP-review-now")
    add_evidence(
        session,
        dependency,
        document,
        verified=True,
        satisfies=True,
    )
    replacement_date = TODAY - timedelta(days=11)
    register_chain(
        session,
        project,
        [document, successor],
        [replacement_date],
    )

    exception = next(
        item
        for item in exceptions_for(session, dependency.id, today=TODAY)
        if item.rule == "SUPERSEDED_CITATION"
    )

    assert exception.quantity_days == 11
    assert replacement_date.isoformat() in exception.detail
    assert "re-confirmation" in exception.detail
    assert "overdue" not in exception.detail.lower()
    assert format_exception_label(exception) == (
        "Supporting document replaced 11d"
    )


def test_readiness_currency_lapse_does_not_invent_new_schedule_failures(
    session, project, document
):
    """A registry event creates review work, not a fictional schedule change."""
    document.doc_date = TODAY - timedelta(days=60)
    successor = Document(
        project_id=project.id,
        sha256="e4" * 32,
        filename="matrix-current.pdf",
        doc_type="matrix",
        parse_status="pending",
        pages=1,
    )
    session.add(successor)
    session.flush()
    dependency = make_dep(
        session,
        project,
        ref="DEP-currency-lapse",
        internal_owner=None,
        need_date=TODAY + timedelta(days=5),
    )
    add_evidence(
        session,
        dependency,
        document,
        verified=True,
        satisfies=True,
    )
    before = codes(session, dependency)
    register_chain(session, project, [document, successor], [TODAY])
    after = codes(session, dependency)

    assert {"MISSING_OWNER", "DUE_SOON", "STALE"}.isdisjoint(before)
    assert {"MISSING_OWNER", "DUE_SOON", "STALE"}.isdisjoint(after)
    assert "SUPERSEDED_CITATION" in after


def test_publication_supersession_keeps_real_schedule_findings(
    session, project, document
):
    """Provenance review must not hide facts on a genuinely unready record."""
    document.doc_date = TODAY - timedelta(days=60)
    successor = Document(
        project_id=project.id,
        sha256="e8" * 32,
        filename="matrix-publication-current.pdf",
        doc_type="matrix",
        parse_status="pending",
        pages=1,
    )
    session.add(successor)
    session.flush()
    dependency = make_dep(
        session,
        project,
        ref="DEP-publication-still-unready",
        internal_owner=None,
        need_date=TODAY + timedelta(days=5),
    )
    publication = add_evidence(session, dependency, document, verified=True)
    designate_publication_support(
        session,
        dependency.id,
        publication.id,
        principal=TEST_PRINCIPAL,
    )
    before = codes(session, dependency)
    register_chain(session, project, [document, successor], [TODAY])
    after = codes(session, dependency)

    schedule_rules = {"MISSING_OWNER", "DUE_SOON", "STALE"}
    assert schedule_rules <= before
    assert schedule_rules <= after
    assert "SUPERSEDED_CITATION" in after


def test_current_human_review_clears_the_signal_without_deleting_history(
    session, project, document
):
    successor = Document(
        project_id=project.id,
        sha256="e5" * 32,
        filename="matrix-reviewed-current.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(successor)
    session.flush()
    dependency = make_dep(session, project, ref="DEP-reconfirmed")
    historical = add_evidence(
        session,
        dependency,
        document,
        verified=True,
        satisfies=True,
    )
    designate_publication_support(
        session,
        dependency.id,
        historical.id,
        principal=TEST_PRINCIPAL,
    )
    assertion = Assertion(
        dependency_id=dependency.id,
        field_name="status",
        asserted_value="ready in predecessor",
        evidence_link_id=historical.id,
    )
    session.add(assertion)
    session.flush()
    register_chain(session, project, [document, successor], [TODAY])
    assert "SUPERSEDED_CITATION" in codes(session, dependency)

    current = EvidenceLink(
        dependency_id=dependency.id,
        document_id=successor.id,
        page_no=1,
        quote="reviewer verified the unchanged current row",
        verified=True,
    )
    session.add(current)
    session.flush()
    designate_publication_support(
        session,
        dependency.id,
        current.id,
        principal=TEST_PRINCIPAL,
    )
    mark_satisfies(
        session,
        dependency.id,
        current.id,
        principal=TEST_PRINCIPAL,
    )

    assert "SUPERSEDED_CITATION" not in codes(session, dependency)
    assert resolve_operative_support(session, [dependency.id])[dependency.id].is_ready
    assert session.get(EvidenceLink, historical.id).verified is True
    historical_support = resolve_operative_support(session, [dependency.id])[
        dependency.id
    ]
    assert historical.id in {
        support.evidence_link_id for support in historical_support.readiness
    }
    assert session.get(Assertion, assertion.id).evidence_link_id == historical.id


def test_multiple_superseded_scopes_keep_each_authority_date(
    session, project, document
):
    middle = Document(
        project_id=project.id,
        sha256="e6" * 32,
        filename="matrix-middle.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    current = Document(
        project_id=project.id,
        sha256="e7" * 32,
        filename="matrix-current-two-edges.pdf",
        doc_type="matrix",
        parse_status="pending",
        pages=1,
    )
    session.add_all([middle, current])
    session.flush()
    dependency = make_dep(session, project, ref="DEP-two-dates")
    oldest_link = add_evidence(session, dependency, document)
    middle_link = add_evidence(session, dependency, middle)
    designate_publication_support(
        session,
        dependency.id,
        middle_link.id,
        principal=TEST_PRINCIPAL,
    )
    designate_publication_support(
        session,
        dependency.id,
        oldest_link.id,
        field_name="external_org",
        principal=TEST_PRINCIPAL,
    )
    oldest_replacement = TODAY - timedelta(days=20)
    middle_replacement = TODAY - timedelta(days=3)
    register_chain(
        session,
        project,
        [document, middle, current],
        [oldest_replacement, middle_replacement],
    )

    exception = next(
        item
        for item in exceptions_for(session, dependency.id, today=TODAY)
        if item.rule == "SUPERSEDED_CITATION"
    )

    assert exception.quantity_days == 20
    assert oldest_replacement.isoformat() in exception.detail
    assert middle_replacement.isoformat() in exception.detail
    assert "publication field external_org" in exception.detail
    assert "record publication" in exception.detail


# --------------------------------------- facts, not scores (#115, ADR-0010)


def test_an_exception_carries_no_score(session, project, document):
    """The weights were ADR-0009's finding one layer up — a scale nothing
    can assert — and ADR-0010 abolishes them. Not renamed, not derived
    differently: gone."""
    dep = make_dep(session, project, internal_owner=None)
    add_evidence(session, dep, document)

    exception = exceptions_for(session, dep.id, today=TODAY)[0]

    assert not hasattr(exception, "severity")


def test_criticality_is_a_flag_to_filter_never_a_multiplier(session, project, document):
    """The moves/stays reading rides along so a view can slice on it.

    Same rule, same detail, whatever the strategy — the reading changes
    which bucket a reader files the row in, never how loud the row is.
    """
    normal = make_dep(session, project, ref="DEP-n", internal_owner=None)
    critical = make_dep(
        session,
        project,
        ref="DEP-c",
        internal_owner=None,
        resolution_strategy="relocate",
    )
    for dep in (normal, critical):
        add_evidence(session, dep, document)

    n = next(
        e
        for e in exceptions_for(session, normal.id, today=TODAY)
        if e.rule == "MISSING_OWNER"
    )
    c = next(
        e
        for e in exceptions_for(session, critical.id, today=TODAY)
        if e.rule == "MISSING_OWNER"
    )

    assert (n.critical, c.critical) == (False, True)
    assert n.detail == c.detail


def test_a_rule_with_a_quantity_states_it_in_days(session, project, document):
    """OVERDUE by how much is the fact a reader can check; a weight was
    not. The quantity is the rule's own: days past the committed date,
    days until the need date, days of document silence."""
    dep = make_dep(
        session,
        project,
        committed_date=TODAY - timedelta(days=44),
        need_date=TODAY + timedelta(days=12),
    )
    add_evidence(session, dep, document, doc_date=TODAY - timedelta(days=21))

    by_rule = {e.rule: e for e in exceptions_for(session, dep.id, today=TODAY)}

    assert by_rule["OVERDUE"].quantity_days == 44
    assert by_rule["DUE_SOON"].quantity_days == 12
    assert by_rule["STALE"].quantity_days == 21


def test_exception_labels_include_the_rule_s_own_days_when_present(
    session, project, document
):
    dep = make_dep(
        session,
        project,
        committed_date=TODAY - timedelta(days=8),
        need_date=TODAY + timedelta(days=3),
    )
    add_evidence(session, dep, document, doc_date=TODAY - timedelta(days=21))

    by_rule = {e.rule: e for e in exceptions_for(session, dep.id, today=TODAY)}

    assert format_exception_label(by_rule["OVERDUE"]) == "Promised timing passed 8d"
    assert format_exception_label(by_rule["DUE_SOON"]) == "Required by date is near 3d"
    assert format_exception_label(by_rule["STALE"]) == "No recent supporting documents 21d"


def test_a_rule_whose_fact_is_an_absence_carries_no_quantity(
    session, project, document
):
    """MISSING_DATE has no number: the finding is that there is nothing to
    count. Inventing 0 or infinity here would be the scalar sneaking back."""
    dep = make_dep(session, project, internal_owner=None)
    add_evidence(session, dep, document)

    by_rule = {e.rule: e for e in exceptions_for(session, dep.id, today=TODAY)}

    assert by_rule["MISSING_OWNER"].quantity_days is None
    assert by_rule["MISSING_DATE"].quantity_days is None
    assert by_rule["ORPHAN"].quantity_days is None


def test_exception_labels_omit_days_for_absence_rules(session, project, document):
    dep = make_dep(session, project, internal_owner=None)
    add_evidence(session, dep, document)

    by_rule = {e.rule: e for e in exceptions_for(session, dep.id, today=TODAY)}

    assert format_exception_label(by_rule["MISSING_OWNER"]) == "No person assigned"
    assert format_exception_label(by_rule["MISSING_DATE"]) == "No exact promised date for this check"


def test_stale_with_no_dated_evidence_at_all_has_no_quantity(
    session, project, document
):
    """ "No document has ever spoken" is an absence, not an age. An age
    would have to be measured from an invented origin."""
    dep = make_dep(session, project)
    add_evidence(session, dep, document)
    document.doc_date = None
    document.retrieved_at = None
    session.flush()

    by_rule = {e.rule: e for e in exceptions_for(session, dep.id, today=TODAY)}

    assert "STALE" in by_rule
    assert by_rule["STALE"].quantity_days is None


# ----------------------------------------------- the facet view (#115)


def test_facets_group_by_rule_and_order_within_by_quantity(session, project, document):
    """One facet function feeds every consumer, so no view invents an
    order. Within a rule, most days first — an ordering a reader can
    check against the record."""
    from corridor.exceptions import facets

    slightly = make_dep(
        session, project, ref="DEP-a", committed_date=TODAY - timedelta(days=3)
    )
    badly = make_dep(
        session, project, ref="DEP-b", committed_date=TODAY - timedelta(days=40)
    )
    for dep in (slightly, badly):
        add_evidence(session, dep, document)

    view = facets(evaluate(session, project.id, today=TODAY))
    overdue = next(f for f in view if f.rule == "OVERDUE")

    assert overdue.count == 2
    assert [e.ref_code for e in overdue.exceptions] == ["DEP-b", "DEP-a"]
    assert overdue.has_quantities is True


def test_a_bucket_with_nothing_to_order_by_says_so(session, project, document):
    """The live corpus's case, and the common one: every date absent. The
    marker is what lets a view print "no dates known" instead of ref-code
    order dressed as a ranking — the pathology ADR-0010's own exhibit
    ridicules."""
    from corridor.exceptions import facets

    for ref in ("DEP-a", "DEP-b"):
        dep = make_dep(session, project, ref=ref, internal_owner=None)
        add_evidence(session, dep, document)

    view = facets(evaluate(session, project.id, today=TODAY))
    missing = next(f for f in view if f.rule == "MISSING_OWNER")

    assert missing.count == 2
    assert missing.has_quantities is False


def test_absent_quantities_sort_after_present_ones_stably(session, project, document):
    """A row nobody can order still appears — after the ones that can be,
    in ref-code order, so two runs render identically."""
    from corridor.exceptions import facets

    dated = make_dep(session, project, ref="DEP-dated")
    add_evidence(session, dated, document, doc_date=TODAY - timedelta(days=30))
    undated = make_dep(session, project, ref="DEP-undated")
    add_evidence(session, undated, document)
    # The shared fixture document goes undated, so DEP-undated's only
    # evidence carries no date at all.
    document.doc_date = None
    document.retrieved_at = None
    session.flush()

    view = facets(evaluate(session, project.id, today=TODAY))
    stale = next(f for f in view if f.rule == "STALE")

    assert [e.ref_code for e in stale.exceptions] == ["DEP-dated", "DEP-undated"]
    assert stale.exceptions[0].quantity_days == 30
    assert stale.exceptions[1].quantity_days is None


def test_the_ruleset_version_is_pinned(session):
    """Adding a rule changes published counts and therefore the contract."""
    assert RULESET_VERSION == "v0.4"


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


def test_evaluate_is_deterministic_and_claims_no_ranking(session, project, document):
    """Flat output is stable — two runs, one answer — and ordered by
    nothing but (ref_code, rule): a filing order, not a verdict. Anything
    that looks like "worst first" belongs to the facet view, where the
    ordering fact is named."""
    low = make_dep(session, project, ref="DEP-low")
    high = make_dep(
        session,
        project,
        ref="DEP-high",
        internal_owner=None,
        resolution_strategy="relocate",
        committed_date=TODAY - timedelta(days=5),
    )
    for dep in (low, high):
        add_evidence(session, dep, document)

    first = evaluate(session, project.id, today=TODAY)
    second = evaluate(session, project.id, today=TODAY)

    assert first == second
    assert [(e.ref_code, e.rule) for e in first] == sorted(
        (e.ref_code, e.rule) for e in first
    )


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


def test_an_evaluation_carries_the_clock_that_produced_it(session, project):
    """A bare list of facts does not say which `today` made it.

    Every consumer that wanted a date used to supply its own, so a view
    could re-derive "days overdue" against a clock the engine never saw.
    """
    make_dep(session, project, committed_date=TODAY - timedelta(days=9))

    evaluation = evaluate_project(session, project.id, today=TODAY)

    assert evaluation.today == TODAY
    assert evaluation.ruleset_version == RULESET_VERSION
    assert evaluation.thresholds == Thresholds()
    overdue = next(e for e in evaluation.found if e.rule == "OVERDUE")
    assert overdue.quantity_days == 9


def test_an_evaluation_publishes_the_thresholds_it_used(session, project):
    """The configuration ADR-0010 kept when it abolished the weights.

    Until the evaluation carried it, no published surface could vary it.
    """
    make_dep(session, project, need_date=TODAY + timedelta(days=20))

    default = evaluate_project(session, project.id, today=TODAY)
    tightened = evaluate_project(
        session, project.id, today=TODAY, thresholds=Thresholds(due_soon_days=10)
    )

    assert any(e.rule == "DUE_SOON" for e in default.found)
    assert not any(e.rule == "DUE_SOON" for e in tightened.found)
    assert tightened.thresholds.due_soon_days == 10


def test_an_evaluation_groups_by_dependency_so_no_consumer_regroups(session, project):
    """The other grouping every consumer needs, beside `facets`."""
    first = make_dep(session, project, ref="DEP-1")
    second = make_dep(session, project, ref="DEP-2")

    evaluation = evaluate_project(session, project.id, today=TODAY)
    grouped = evaluation.by_dependency()

    assert set(grouped) == {first.id, second.id}
    assert grouped[first.id] == evaluation.for_dependency(first.id)
    # Same facts, two views of them — nothing is invented by the grouping.
    assert sum(len(v) for v in grouped.values()) == len(evaluation.found)
    assert [f.rule for f in evaluation.facets()] == [
        f.rule for f in facets(list(evaluation.found))
    ]


# --- Coordination rules (#176) -----------------------------------------------


def test_missing_action_is_an_absence_of_a_current_work_decision(
    session, project, document
):
    from corridor.principals import HumanPrincipal
    from corridor.work_decisions import complete_next_action, set_next_action

    recorder = HumanPrincipal("local:coordination-tester")
    dep = make_dep(session, project, ref="DEP-act")
    add_evidence(session, dep, document)

    assert "MISSING_ACTION" in codes(session, dep)

    set_next_action(
        session,
        dep.id,
        "Walk the crossing",
        due_date_unknown_reason="awaiting_external_information",
        principal=recorder,
    )
    assert "MISSING_ACTION" not in codes(session, dep)

    complete_next_action(
        session,
        dep.id,
        no_follow_up_reason="return_condition_recorded",
        principal=recorder,
    )
    assert "MISSING_ACTION" in codes(session, dep)


def test_missing_owner_clears_when_a_work_decision_records_one(
    session, project, document
):
    from corridor.principals import HumanPrincipal
    from corridor.work_decisions import assign_internal_owner

    dep = make_dep(session, project, ref="DEP-own", internal_owner=None)
    add_evidence(session, dep, document)
    assert "MISSING_OWNER" in codes(session, dep)

    assign_internal_owner(
        session,
        dep.id,
        "Dana Fields",
        principal=HumanPrincipal("local:coordination-tester"),
    )
    assert "MISSING_OWNER" not in codes(session, dep)


def test_action_due_dates_have_their_own_lanes(session, project, document):
    from datetime import timedelta

    from corridor.principals import HumanPrincipal
    from corridor.work_decisions import set_next_action

    recorder = HumanPrincipal("local:coordination-tester")
    soon = make_dep(
        session, project, ref="DEP-soon", need_date=TODAY + timedelta(days=10)
    )
    add_evidence(session, soon, document)
    set_next_action(
        session,
        soon.id,
        "Call the City",
        due_date=TODAY + timedelta(days=3),
        principal=recorder,
    )

    fired = {e.rule: e for e in exceptions_for(session, soon.id, today=TODAY)}
    assert "ACTION_DUE_SOON" in fired and fired["ACTION_DUE_SOON"].quantity_days == 3
    # The Need Date lane fires beside it, never merged with it.
    assert "DUE_SOON" in fired and fired["DUE_SOON"].quantity_days == 10

    late = make_dep(session, project, ref="DEP-late")
    add_evidence(session, late, document)
    set_next_action(
        session,
        late.id,
        "Chase the schedule",
        due_date=TODAY - timedelta(days=4),
        principal=recorder,
    )
    fired = {e.rule: e for e in exceptions_for(session, late.id, today=TODAY)}
    assert "ACTION_OVERDUE" in fired and fired["ACTION_OVERDUE"].quantity_days == 4
    assert "OVERDUE" not in fired


def test_the_action_horizon_is_per_project_overridable(session, project, document):
    from datetime import timedelta

    from corridor.principals import HumanPrincipal
    from corridor.work_decisions import set_next_action

    dep = make_dep(session, project, ref="DEP-horizon")
    add_evidence(session, dep, document)
    set_next_action(
        session,
        dep.id,
        "Call the City",
        due_date=TODAY + timedelta(days=10),
        principal=HumanPrincipal("local:coordination-tester"),
    )

    default = {e.rule for e in exceptions_for(session, dep.id, today=TODAY)}
    assert "ACTION_DUE_SOON" not in default

    widened = {
        e.rule
        for e in exceptions_for(
            session,
            dep.id,
            today=TODAY,
            thresholds=Thresholds(action_due_soon_days=14),
        )
    }
    assert "ACTION_DUE_SOON" in widened


def test_an_evaluation_reuses_a_supplied_statement_publication_without_rereading(
    session, project, monkeypatch
):
    """A paired publisher supplies one frozen statement reading, not a hint."""
    from corridor import exceptions as exceptions_module
    from corridor.dependency_events import published_dependency_statements

    dependency = make_dep(session, project, ref="DEP-frozen-publication")
    publication = published_dependency_statements(
        session, (dependency.id,), project_id=project.id
    )

    def reread_forbidden(*args, **kwargs):
        raise AssertionError("evaluation reread the supplied statement publication")

    monkeypatch.setattr(
        exceptions_module, "current_dependency_statements", reread_forbidden
    )

    evaluation = exceptions_module.evaluate_project(
        session,
        project.id,
        today=TODAY,
        statement_publication=publication,
    )

    assert evaluation.statement_publication is publication
