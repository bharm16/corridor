from datetime import date, datetime, timezone
import hashlib
from html import escape as escape_html

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from corridor.adjudicate import accept_candidate
from corridor.changes import record_run, snapshot
from corridor.db import Session, engine
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_closure,
    record_external_party_statement,
)
from corridor.exceptions import evaluate_project, format_exception_label
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.export import to_xlsx
from corridor.ledger import browse, mark_satisfies
from corridor.models import (
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.operative_support import designate_publication_support
from corridor.report import (
    RULESET_VERSION,
    Assertion,
    BareCell,
    Cell,
    Derivation,
    Report,
    assert_no_bare_cells,
    build_report,
    render,
)
from corridor.principals import HumanPrincipal
from corridor.project_reading import freeze_project_reading
from corridor.supersession import SupersessionDeclaration, register_supersessions

TEST_PRINCIPAL = HumanPrincipal("local:bryce")


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
def project_with_two_dependencies(session):
    project = Project(slug="report-test", name="Report Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="c" * 64,
        filename="nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=28,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocPage(
            document_id=doc.id,
            page_no=1,
            text=(
                "FOC1-1 AT&T Texas (SWBT) Telecom 1149+00 1153+17\n"
                "E1 CenterPoint Energy Telecom 1149+00 1153+17"
            ),
            image_path="/tmp/corridor-missing-page.png",
        )
    )
    session.flush()

    candidates = []
    for ref, org in [("FOC1-1", "AT&T Texas (SWBT)"), ("E1", "CenterPoint Energy")]:
        candidate = Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": {
                    "utility_id": ref,
                    "external_org": org,
                    "utility_type": "Telecom",
                    "station_from": "1149+00",
                    "station_to": "1153+17",
                },
                "citations": [
                    {
                        "document_id": doc.id,
                        "page": 1,
                        "quote": f"{ref} {org} Telecom",
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "confidence": 1.0,
                "dedupe_hint": f"{org}|Telecom|1149+00-1153+17",
            },
            source_document_id=doc.id,
            source_pages=[1],
            confidence=1.0,
            prompt_version="txdot_ucm_v1",
            citations_verified=True,
        )
        candidates.append(candidate)

    session.add_all(candidates)
    run = record_extraction_run(
        session,
        doc,
        prompt_version="txdot_ucm_v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=tuple(candidates),
    )
    session.flush()
    declare_active_run(session, doc.id, run.id, principal=TEST_PRINCIPAL)
    session.flush()
    for candidate in candidates:
        accept_candidate(session, candidate, principal=TEST_PRINCIPAL)

    return project


def _a_link_of(session, project_id):
    """Scope to this project's data.

    `make demo` commits real dependencies to the same database, so an
    unscoped query silently picks those up instead of the fixture's.
    """
    from corridor.models import Dependency

    return session.scalars(
        select(EvidenceLink)
        .join(Dependency, EvidenceLink.dependency_id == Dependency.id)
        .where(Dependency.project_id == project_id)
        .order_by(EvidenceLink.id)
    ).first()


def test_frozen_project_reading_pairs_one_population_and_statement_read(
    session, project_with_two_dependencies
):
    reading = freeze_project_reading(
        session,
        project_with_two_dependencies.id,
        today=date(2026, 8, 24),
    )

    assert reading.evaluation.statement_publication is reading.statement_publication
    assert reading.evaluation.committed_dates == (
        reading.statement_publication.committed_dates
    )
    assert {row.dependency.id for row in reading.rows} == set(
        reading.statement_publication.by_dependency
    )
    assert reading.dependency_ids == tuple(row.dependency.id for row in reading.rows)


def _record_exact_cited_statement(
    session, dependency, *, event_date: date, committed_date: date
):
    """Give a fixture record an authoritative exact-day commitment."""
    document = session.scalars(
        select(Document).where(Document.project_id == dependency.project_id)
    ).first()
    party = session.get(ExternalOrg, dependency.external_org_id)
    assert document is not None and party is not None
    quote = f"{party.name} will complete on {committed_date.isoformat()}."
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == 1,
        )
    )
    assert page is not None
    page.text = f"{page.text}\n{quote}"
    return record_external_party_statement(
        session,
        project_id=dependency.project_id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=event_date,
        description=quote,
        new_timing=StatementTiming.day(committed_date.isoformat(), committed_date),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id,
            1,
            quote,
        ),
    )


def test_a_bare_cell_is_refused(session):
    report = Report(project_name="x", generated_at=None, summary=[Cell("Total", "47")])
    with pytest.raises(BareCell, match="Total"):
        assert_no_bare_cells(report)


def test_rendering_refuses_a_bare_cell_rather_than_omitting_provenance(session):
    """The rule is enforced by rendering, not by remembering to check."""
    report = Report(project_name="x", generated_at=None, summary=[Cell("Total", "47")])
    with pytest.raises(BareCell):
        render(report)


def test_every_cell_in_a_built_report_carries_provenance(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    assert report.cells
    assert_no_bare_cells(report)  # does not raise


def test_summary_figures_are_derivations_carrying_their_records(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    total = next(c for c in report.summary if c.label == "Dependencies")

    assert total.value == "2"
    assert isinstance(total.provenance, Derivation)
    assert total.provenance.ruleset_version == RULESET_VERSION
    assert len(total.provenance.record_ids) == 2
    assert "over 2 records" in total.provenance.marker


def section(report, title):
    return next(s for s in report.sections if s.title.startswith(title))


def test_row_figures_are_assertions_carrying_a_page_and_quote(
    session, project_with_two_dependencies
):
    """Per-record facts cite a document, page and quote."""
    make_critical(session, project_with_two_dependencies)
    report = build_report(session, project_with_two_dependencies.id)
    ref = section(report, "Critical items").rows[0][0]

    assert isinstance(ref.provenance, Assertion)
    assert ref.provenance.page_no == 1
    assert ref.provenance.quote
    assert ref.provenance.marker.startswith("[D")
    assert "p.1]" in ref.provenance.marker


def _unverify_evidence_of(session, dependency):
    """Leave the links in place and strike their verification.

    Not "delete the links" and not "accept a candidate that cites
    nothing" — adjudication refuses the second outright now, and building
    a `Dependency(...)` by hand to get the first would pin a state
    production can no longer reach. This is the surviving path:
    `_evidence_link` copies `verified` off the citation, so any quote the
    verifier could not find on its page lands here.
    """
    links = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).all()
    assert links, "the fixture's records are supposed to carry evidence"
    for link in links:
        link.verified = False
    session.flush()
    return links


def _make_every_record_critical(session, project):
    """Both fixture records, because one row cannot tell the fix apart.

    `make_critical` marks the first. With a single row in the section, a
    cell citing *its own* record and a cell citing every record in the
    section are the same tuple — and which record the cell drills to is
    the entire content of the fix.
    """
    from corridor.models import Dependency

    records = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project.id)
        .order_by(Dependency.ref_code)
    ).all()
    for record in records:
        record.resolution_strategy = "relocate"
    session.flush()
    return records


def test_a_critical_record_with_no_verified_evidence_is_still_published(
    session, project_with_two_dependencies
):
    """The section selects records that may have nothing to quote.

    Its filter is "not ready and critical", and MISSING_EVIDENCE fires on
    the unsettled ones among those — so `primary_evidence` returning
    nothing is a normal state of the section, not an anomaly. It published
    five cells with no provenance, `assert_no_bare_cells` refused them,
    and `render` produced no report at all.
    """
    records = _make_every_record_critical(session, project_with_two_dependencies)
    for record in records:
        _unverify_evidence_of(session, record)

    report = build_report(session, project_with_two_dependencies.id)
    assert_no_bare_cells(report)

    rows = section(report, "Critical items").rows
    assert len(rows) == len(records) > 1
    by_ref = {record.ref_code: record for record in records}
    for row in rows:
        record = by_ref[row[0].value]
        for cell in row[:4]:
            assert isinstance(cell.provenance, Derivation)
            # Its own record. A tuple of every critical record would drill
            # to a set the reader did not ask about, and with one row in
            # the section the two are indistinguishable.
            assert cell.provenance.record_ids == (record.id,)
            assert cell.provenance.resolves


def test_an_unverified_quote_is_never_published_as_the_citation(
    session, project_with_two_dependencies
):
    """The other way to make the report render, and the wrong one.

    `cell_html` picks its class on `isinstance(p, Assertion)` alone, so a
    quote the verifier rejected would render byte-for-byte like one it
    accepted. Dropping the verified filter from `ledger.primary_evidence`
    is the same mistake wearing a different hat.

    Both halves are load-bearing. The provenance check is what fails under
    that mutation; the string check is escaped because `cell_html` escapes
    what it publishes, and the fixture's `AT&T` renders as `AT&amp;T` —
    so searching for the raw quote passes on the verified path too, where
    that quote *is* the published citation, and proves nothing.
    """
    critical = make_critical(session, project_with_two_dependencies)
    links = _unverify_evidence_of(session, critical)
    quotes = [link.quote for link in links]

    report = build_report(session, project_with_two_dependencies.id)
    markup = render(report)
    row = section(report, "Critical items").rows[0]

    assert not isinstance(row[0].provenance, Assertion)
    assert quotes and all(escape_html(quote) not in markup for quote in quotes)


def test_striking_a_records_verification_removes_no_row_from_the_report(
    session, project_with_two_dependencies
):
    """Omitting the row was the other candidate fix, and it is the worst.

    A critical record whose evidence does not hold is the record a reader
    most needs in front of them. ADR-0013 omits a milestone with no
    records because there is no population to measure; here there is a
    record, and it is the finding.
    """
    critical = make_critical(session, project_with_two_dependencies)
    before = [
        row[0].value
        for row in section(
            build_report(session, project_with_two_dependencies.id), "Critical items"
        ).rows
    ]

    _unverify_evidence_of(session, critical)
    report = build_report(session, project_with_two_dependencies.id)
    after = [row[0].value for row in section(report, "Critical items").rows]

    assert before == after
    assert critical.ref_code in after
    assert critical.ref_code in render(report)


def test_the_report_has_the_documented_sections(session, project_with_two_dependencies):
    report = build_report(session, project_with_two_dependencies.id)
    titles = [s.title for s in report.sections]
    assert titles == [
        "Milestone readiness",
        "Critical items",
        "Coordination",
        "External Party commitments",
        "Exceptions",
        "Changes since last report",
        "Aging",
        "Appendix — full ledger",
    ]


def test_an_empty_section_says_why_rather_than_showing_nothing(
    session, project_with_two_dependencies
):
    """ "No milestones imported" and "nothing is overdue" are different
    facts, and a blank table conveys neither."""
    report = build_report(session, project_with_two_dependencies.id)
    milestones = section(report, "Milestone readiness")
    assert milestones.rows or "No milestones imported" in milestones.empty_message
    assert "overdue" in section(report, "Aging").empty_message


def test_the_first_report_says_so_instead_of_claiming_nothing_changed(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    changes = section(report, "Changes since last report")
    assert "nothing to compare" in changes.empty_message.lower()


def test_a_new_report_calls_a_later_date_a_committed_date_change(
    session, project_with_two_dependencies
):
    """Customer output uses the domain name, while old snapshots stay readable."""
    project = project_with_two_dependencies
    dependency = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project.id)
        .order_by(Dependency.id)
    ).first()
    _record_exact_cited_statement(
        session,
        dependency,
        event_date=date(2026, 6, 1),
        committed_date=date(2026, 6, 3),
    )
    first = build_report(session, project.id, today=date(2026, 8, 1))
    record_run(
        session,
        project.id,
        evaluation=first.evaluation,
        committed_dates=first.committed_dates,
    )
    _record_exact_cited_statement(
        session,
        dependency,
        event_date=date(2026, 7, 1),
        committed_date=date(2026, 8, 15),
    )

    report = build_report(session, project.id, today=date(2026, 8, 1))
    changes = section(report, "Changes since last report")

    assert "Committed Date Change" in [row[1].value for row in changes.rows]
    assert "slipped" not in render(report).lower()


def test_the_report_states_what_it_does_not_cover(
    session, project_with_two_dependencies
):
    """A report over 6 of 1,340 extracted records is a report about almost
    nothing, and it must say so."""
    from corridor.models import Candidate, Document

    doc = session.scalars(
        select(Document).where(Document.project_id == project_with_two_dependencies.id)
    ).first()
    session.add(
        Candidate(
            project_id=project_with_two_dependencies.id,
            kind="dependency",
            payload_json={"kind": "dependency", "fields": {}, "citations": []},
            source_document_id=doc.id,
            source_pages=[1],
            confidence=1.0,
            prompt_version="x",
            state="pending",
        )
    )
    session.flush()

    report = build_report(session, project_with_two_dependencies.id)
    assert "awaiting adjudication" in report.coverage_note
    assert "awaiting adjudication" in render(report)


def test_the_exceptions_section_counts_by_rule(session, project_with_two_dependencies):
    report = build_report(session, project_with_two_dependencies.id)
    rules = {row[0].value for row in section(report, "Exceptions").rows}
    assert "No Milestone" in rules


def test_report_names_superseded_citations_as_reconfirmation_work(
    session, project_with_two_dependencies
):
    predecessor = session.scalars(
        select(Document).where(Document.project_id == project_with_two_dependencies.id)
    ).one()
    successor = Document(
        project_id=project_with_two_dependencies.id,
        sha256="d" * 64,
        filename="current-revision.pdf",
        doc_type="matrix",
        parse_status="pending",
        pages=1,
        registry_id="report-current-revision",
    )
    predecessor.registry_id = "report-predecessor-revision"
    session.add(successor)
    session.flush()
    # The successor states what it replaces, so it carries the cited page.
    session.add(
        DocPage(document_id=successor.id, page_no=1, text="Replaces the prior revision")
    )
    session.flush()
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=date(2026, 8, 5),
                source_registry_id=successor.registry_id,
                source_page=1,
            )
        ],
        project_id=project_with_two_dependencies.id,
    )

    report = build_report(
        session,
        project_with_two_dependencies.id,
        today=date(2026, 8, 5),
    )
    rules = {row[0].value for row in section(report, "Exceptions").rows}

    assert "Evidence is not current · re-confirmation" in rules


def test_the_appendix_lists_every_ledger_record(session, project_with_two_dependencies):
    report = build_report(session, project_with_two_dependencies.id)
    assert len(section(report, "Appendix").rows) == 2


def test_readiness_in_the_report_is_computed_not_stored(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    ready_summary = next(c for c in report.summary if c.label == "Ready")
    assert ready_summary.value == "0"

    link = _a_link_of(session, project_with_two_dependencies.id)
    dependency = session.get(Dependency, link.dependency_id)
    assert dependency is not None
    mark_satisfies(
        session,
        dependency.id,
        link.id,
        principal=TEST_PRINCIPAL,
    )

    after = build_report(session, project_with_two_dependencies.id)
    assert next(c for c in after.summary if c.label == "Ready").value == "1"


def test_percentage_is_a_derivation_not_an_assertion(
    session, project_with_two_dependencies
):
    """No document contains '100.0%'. It must not claim a page."""
    report = build_report(session, project_with_two_dependencies.id)
    pct = next(c for c in report.summary if c.label.startswith("%"))
    assert isinstance(pct.provenance, Derivation)
    assert pct.value.endswith("%")


def test_rendered_html_shows_a_marker_for_every_value(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    out = render(report)

    assert out.lstrip().startswith("<!doctype html>")
    assert "AT&amp;T Texas (SWBT)" in out
    # One marker per published cell, no more and no fewer.
    assert out.count('class="marker"') == len(report.cells)
    assert f"ruleset {RULESET_VERSION}" in out


def test_quotes_are_escaped_into_the_markup(session, project_with_two_dependencies):
    """Document text is untrusted input; it lands in a title attribute."""
    make_critical(session, project_with_two_dependencies)
    link = _a_link_of(session, project_with_two_dependencies.id)
    link.quote = '<script>alert("x")</script>'
    session.flush()

    out = render(build_report(session, project_with_two_dependencies.id))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ------------------- Critical items and Exceptions restated (#116, ADR-0010)


def make_critical(session, project, *, need_days_out=None):
    """Mark the first fixture record critical, optionally with a need date.

    Criticality is a reading of the stored strategy (ADR-0009), so the
    fixture asserts one the reading maps — never a flag.
    """
    from datetime import date, timedelta

    from corridor.models import Dependency

    dep = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project.id)
        .order_by(Dependency.ref_code)
    ).first()
    dep.resolution_strategy = "relocate"
    if need_days_out is not None:
        dep.need_date = date.today() + timedelta(days=need_days_out)
    session.flush()
    return dep


def test_critical_items_is_a_filter_not_a_weighting(
    session, project_with_two_dependencies
):
    """The section means what it says: the critical records. A weight let
    a non-critical record outrank a critical one by piling on exceptions;
    a filter cannot — the non-critical record is simply not this section's
    subject."""
    project = project_with_two_dependencies
    critical = make_critical(session, project)

    report = build_report(session, project.id)
    refs = {row[0].value for row in section(report, "Critical items").rows}

    assert refs == {critical.ref_code}


def test_critical_items_orders_by_need_date_proximity(
    session, project_with_two_dependencies
):
    """Nearest need first — a fact with a unit, declared in the note."""
    from datetime import date, timedelta

    from corridor.models import Dependency

    project = project_with_two_dependencies
    deps = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project.id)
        .order_by(Dependency.ref_code)
    ).all()
    for dep, days in zip(deps, (200, 10)):
        dep.resolution_strategy = "relocate"
        dep.need_date = date.today() + timedelta(days=days)
    session.flush()

    report = build_report(session, project.id)
    rows = section(report, "Critical items").rows

    assert [row[0].value for row in rows] == [deps[1].ref_code, deps[0].ref_code]


def test_critical_items_with_no_dates_says_so(session, project_with_two_dependencies):
    """The live corpus's case: critical records, no dates anywhere. The
    section shows them and says there is nothing to order by, instead of
    ref-code order dressed as a ranking — ADR-0010's own exhibit."""
    project = project_with_two_dependencies
    make_critical(session, project, need_days_out=None)

    report = build_report(session, project.id)
    found = section(report, "Critical items")

    assert len(found.rows) == 1
    assert "No dates known" in found.note


def test_the_critical_items_note_declares_the_ordering_and_never_a_weight(
    session, project_with_two_dependencies
):
    """ "Declared presentation": the note names the one quantity the list
    is ordered by. Nothing multiplies, so nothing says ×."""
    project = project_with_two_dependencies
    make_critical(session, project, need_days_out=30)

    report = build_report(session, project.id)
    note = section(report, "Critical items").note

    assert "earliest need first" in note
    assert "undated records follow" in note
    assert "×" not in note
    assert "weighted" not in note


def test_a_critical_row_lists_its_exceptions_as_facts(
    session, project_with_two_dependencies
):
    """No cross-rule "worst" pick — the device ADR-0010 forbids. The row
    shows its exceptions with their quantities, and the reader judges."""
    project = project_with_two_dependencies
    critical = make_critical(session, project)
    _record_exact_cited_statement(
        session,
        critical,
        event_date=date(2026, 7, 27),
        committed_date=date(2026, 7, 28),
    )
    critical.need_date = date(2026, 8, 8)
    session.flush()

    report = build_report(session, project.id, today=date(2026, 8, 5))
    found = section(report, "Critical items")
    exceptions_cell = found.rows[0][found.columns.index("Exceptions")]
    by_rule = {e.rule: e for e in report.evaluation.for_dependency(critical.id)}

    assert "No Milestone" in exceptions_cell.value
    assert format_exception_label(by_rule["OVERDUE"]) in exceptions_cell.value
    assert format_exception_label(by_rule["DUE_SOON"]) in exceptions_cell.value


def test_the_exceptions_summary_exemplar_is_the_largest_quantity_or_nothing(
    session, project_with_two_dependencies
):
    """ "Worst" regains a meaning: the most days, checkable against the
    record. A rule whose fact is an absence has no exemplar — every row
    is the same finding, and electing one would be an arbitrary pick
    wearing a superlative."""
    report = build_report(session, project_with_two_dependencies.id)
    found = section(report, "Exceptions")
    by_rule = {row[0].value: row for row in found.rows}

    most_days = found.columns.index("Most days")
    assert by_rule["No Milestone"][most_days].value == "—"
    # The fixture's document is undated, so STALE is the absence case —
    # "no dated evidence at all" has no age, and no exemplar either. The
    # largest-quantity path is pinned at the engine seam and in the
    # critical row's exception listing.
    assert by_rule["Evidence is stale"][most_days].value == "—"


def _overdue_by(session, project_id, days):
    """Give every dependency a committed date `days` in the past."""
    from datetime import date, timedelta

    from corridor.models import Dependency

    committed = date(2026, 6, 1)
    for dependency in session.scalars(
        select(Dependency).where(Dependency.project_id == project_id)
    ).all():
        _record_exact_cited_statement(
            session,
            dependency,
            event_date=committed - timedelta(days=1),
            committed_date=committed,
        )
    return committed + timedelta(days=days)


def test_the_report_is_built_against_the_date_it_was_asked_for(
    session, project_with_two_dependencies
):
    """`today` reaches the engine, so the Aging section exists at all.

    Before the evaluation carried the clock, `today` reached two sections
    while every exception was computed against `date.today()` — so a
    report built for a stated date could not produce an overdue row.
    """
    today = _overdue_by(session, project_with_two_dependencies.id, 40)

    report = build_report(session, project_with_two_dependencies.id, today=today)
    aging = section(report, "Aging")

    assert len(aging.rows) == 2
    days = aging.columns.index("Days overdue")
    assert {row[days].value for row in aging.rows} == {"40"}


def test_days_overdue_is_the_exception_s_own_quantity_not_a_recount(
    session, project_with_two_dependencies
):
    """One fact, one number (ADR-0010).

    The Aging column used to subtract the report's `today` from the
    committed date while the OVERDUE fact beside it counted from the
    engine's. Same page, two answers, whenever the clocks differed.
    """
    today = _overdue_by(session, project_with_two_dependencies.id, 17)

    report = build_report(session, project_with_two_dependencies.id, today=today)
    aging = section(report, "Aging")
    days = aging.columns.index("Days overdue")
    ref = aging.columns.index("Ref")

    quantities = {
        e.ref_code: e.quantity_days
        for e in report.evaluation.found
        if e.rule == "OVERDUE"
    }
    assert quantities
    for row in aging.rows:
        assert row[days].value == str(quantities[row[ref].value])


def test_the_report_publishes_the_evaluation_the_export_records(
    session, project_with_two_dependencies
):
    """The export and the recorded run describe this reading, not their own."""
    today = _overdue_by(session, project_with_two_dependencies.id, 3)
    report = build_report(session, project_with_two_dependencies.id, today=today)

    assert report.evaluation is not None
    assert report.evaluation.today == today
    assert report.evaluation.ruleset_version == report.ruleset_version


def _due_tomorrow(session, project_id):
    """Committed in the past, but not yet due at the report's own date.

    Anchored on `date.today()` so the two readings genuinely disagree: a
    fresh evaluation finds these OVERDUE, and an evaluation taken the day
    before the committed date does not.
    """
    from datetime import timedelta

    from corridor.models import Dependency

    committed = date.today() - timedelta(days=10)
    for dependency in session.scalars(
        select(Dependency).where(Dependency.project_id == project_id)
    ).all():
        _record_exact_cited_statement(
            session,
            dependency,
            event_date=committed - timedelta(days=1),
            committed_date=committed,
        )
    return committed - timedelta(days=1)


def test_the_export_and_the_recorded_run_read_the_report_s_evaluation(
    session, project_with_two_dependencies, tmp_path
):
    """One publication, one reading of the Ledger.

    `make demo` used to call `to_xlsx` and `record_run` without passing the
    report's evaluation, and both defaulted to taking their own — so the
    HTML, the workbook and the snapshot the *next* report diffs against
    were three readings at three clocks. The parameter is required now, and
    this pins that what is passed is what gets published.
    """
    today = _due_tomorrow(session, project_with_two_dependencies.id)
    report = build_report(session, project_with_two_dependencies.id, today=today)

    # Nothing is overdue at the report's own date; everything is overdue now.
    assert not any(e.rule == "OVERDUE" for e in report.evaluation.found)
    assert any(
        e.rule == "OVERDUE"
        for e in evaluate_project(session, project_with_two_dependencies.id).found
    )

    path = to_xlsx(
        session,
        project_with_two_dependencies.id,
        tmp_path / "ledger.xlsx",
        evaluation=report.evaluation,
        statement_publication=report.statement_publication,
    )
    sheet = load_workbook(path)["Ledger"]
    headers = [c.value for c in sheet[1]]
    exceptions = headers.index("Exceptions")
    for row in sheet.iter_rows(min_row=2, values_only=True):
        assert "OVERDUE" not in (row[exceptions] or "")

    run = record_run(
        session,
        project_with_two_dependencies.id,
        evaluation=report.evaluation,
    )
    recorded = run.snapshot_json["dependencies"].values()
    assert recorded
    assert all("OVERDUE" not in entry["exceptions"] for entry in recorded)


def test_publishing_without_an_evaluation_is_refused(
    session, project_with_two_dependencies, tmp_path
):
    """The seam is the signature, not the caller's memory.

    While `evaluation` defaulted to a fresh `evaluate_project`, omitting it
    was silent and produced a second reading. Omitting it is now a
    TypeError, which is what stops the demo bug coming back.
    """
    project_id = project_with_two_dependencies.id

    with pytest.raises(TypeError):
        to_xlsx(session, project_id, tmp_path / "ledger.xlsx")
    with pytest.raises(TypeError):
        record_run(session, project_id)
    with pytest.raises(TypeError):
        snapshot(session, project_id)
    with pytest.raises(TypeError):
        browse(session, project_id)


def test_the_xlsx_refuses_an_evaluation_from_another_project(
    session, project_with_two_dependencies, tmp_path
):
    from corridor.dependency_events import published_dependency_statements
    from corridor.models import Project

    other = Project(slug="foreign-evaluation", name="Foreign", is_synthetic=True)
    session.add(other)
    session.flush()
    evaluation = evaluate_project(session, other.id)
    publication = published_dependency_statements(
        session, (), project_id=project_with_two_dependencies.id
    )

    with pytest.raises(ValueError, match="another project"):
        to_xlsx(
            session,
            project_with_two_dependencies.id,
            tmp_path / "foreign.xlsx",
            evaluation=evaluation,
            statement_publication=publication,
        )


def test_the_report_states_the_date_its_figures_were_counted_from(
    session, project_with_two_dependencies
):
    """The header used to print only the moment the file was written.

    A report built for a stated date therefore printed today's date over
    last week's numbers, and nothing on the page said which day the
    Exceptions had been counted against.
    """
    today = _overdue_by(session, project_with_two_dependencies.id, 40)
    report = build_report(session, project_with_two_dependencies.id, today=today)

    assert f"evaluated {today:%Y-%m-%d}" in render(report)


def test_a_derivation_over_zero_records_is_refused(session):
    """A marker that drills through to nothing is a bare cell wearing one.

    ADR-0003's argument is that a Derivation drills through to the
    records' Evidence — "making a percentage clickable down to the
    evidence beneath it". Checking only that `provenance` was present let
    `Derivation(version, ())` satisfy the rule, and the whole "Changes
    since last report" section was built that way.
    """
    report = Report(
        project_name="x",
        generated_at=None,
        summary=[Cell("Changed", "3", Derivation(RULESET_VERSION, ()))],
    )
    with pytest.raises(BareCell, match="Changed"):
        assert_no_bare_cells(report)


def test_a_derivation_may_name_a_scope_where_no_record_can_answer(session):
    """An empty ledger and a departed record have nothing to drill to."""
    scoped = Derivation(RULESET_VERSION, (), "an empty ledger")

    assert scoped.resolves
    assert "an empty ledger" in scoped.marker
    assert "an empty ledger" in scoped.drill
    assert_no_bare_cells(
        Report(
            project_name="x",
            generated_at=None,
            summary=[Cell("Dependencies", "0", scoped)],
        )
    )


def test_a_change_cites_the_record_it_describes(session, project_with_two_dependencies):
    """Every cell of the Changes section used to carry an empty tuple."""
    from corridor.models import Dependency

    project_id = project_with_two_dependencies.id
    record_run(session, project_id, evaluation=evaluate_project(session, project_id))
    dependency = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project_with_two_dependencies.id)
        .order_by(Dependency.id)
    ).first()
    evidence = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).first()
    mark_satisfies(
        session,
        dependency.id,
        evidence.id,
        principal=TEST_PRINCIPAL,
    )

    report = build_report(session, project_with_two_dependencies.id)
    changes = section(report, "Changes since last report")

    assert changes.rows
    for row in changes.rows:
        for cell in row:
            assert cell.provenance.resolves
    ready = next(r for r in changes.rows if r[1].value == "became_ready")
    assert ready[0].provenance.record_ids == (dependency.id,)


def _summary(report, label):
    return next(c for c in report.summary if c.label == label)


def test_the_verified_evidence_figures_count_only_evidence_that_holds(
    session, project_with_two_dependencies
):
    """The tile says "verified" and the count said "linked"."""
    before = build_report(session, project_with_two_dependencies.id)
    assert _summary(before, "With verified evidence").value == "2"
    assert _summary(before, "% with verified evidence").value == "100.0%"

    first = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project_with_two_dependencies.id)
        .order_by(Dependency.ref_code)
    ).first()
    _unverify_evidence_of(session, first)

    after = build_report(session, project_with_two_dependencies.id)
    assert _summary(after, "With verified evidence").value == "1"
    assert _summary(after, "% with verified evidence").value == "50.0%"


def test_the_report_never_claims_evidence_for_records_it_says_have_none(
    session, project_with_two_dependencies
):
    """The two sentences could appear in one document, about one record.

    MISSING_EVIDENCE reads "no verified evidence on this record" and the
    tile above it counted the link anyway, so the report contradicted
    itself in the direction that flatters the project — which is the worst
    direction for a document a project forwards to an External Party.
    """
    for record in session.scalars(
        select(Dependency).where(
            Dependency.project_id == project_with_two_dependencies.id
        )
    ).all():
        _unverify_evidence_of(session, record)

    report = build_report(session, project_with_two_dependencies.id)
    markup = render(report)

    assert "No verified Evidence" in markup
    assert _summary(report, "With verified evidence").value == "0"
    assert _summary(report, "% with verified evidence").value == "0.0%"


def test_percent_evidenced_on_a_milestone_counts_only_evidence_that_holds(
    session, project_with_two_dependencies
):
    """The rollup's own copy of the same figure, and the same rule."""
    from corridor.models import Milestone

    milestone = Milestone(
        project_id=project_with_two_dependencies.id,
        code="RELO-CONSTR",
        name="RELO-CONSTR",
        need_date=date(2026, 12, 1),
    )
    session.add(milestone)
    session.flush()
    records = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project_with_two_dependencies.id)
        .order_by(Dependency.ref_code)
    ).all()
    for record in records:
        record.milestone_id = milestone.id
    session.flush()

    def evidenced_cell():
        row = section(
            build_report(session, project_with_two_dependencies.id),
            "Milestone readiness",
        ).rows[0]
        return next(c for c in row if c.label == "% evidenced")

    assert evidenced_cell().value == "100%"

    _unverify_evidence_of(session, records[0])

    assert evidenced_cell().value == "50%"


def test_a_milestone_nothing_is_linked_to_is_named_not_scored(
    session, project_with_two_dependencies
):
    """ "Ready 0" for an unlinked milestone reads as a measurement.

    It is not one — nothing was measured, and the section says so rather
    than publishing four zeroes over no records.
    """
    from corridor.models import Milestone

    session.add(
        Milestone(
            project_id=project_with_two_dependencies.id,
            code="RELO-CONSTR",
            name="RELO-CONSTR",
            need_date=date(2026, 12, 1),
        )
    )
    session.flush()

    report = build_report(session, project_with_two_dependencies.id)
    milestones = section(report, "Milestone readiness")

    assert "Nothing is linked to RELO-CONSTR" in milestones.note
    assert "RELO-CONSTR" not in {row[0].value for row in milestones.rows}
    assert_no_bare_cells(report)


def test_report_cites_designated_publication_support_not_the_first_link(
    session, project_with_two_dependencies
):
    dependency = make_critical(session, project_with_two_dependencies)
    first = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == dependency.id)
        .order_by(EvidenceLink.id)
    ).first()
    designated = EvidenceLink(
        dependency_id=dependency.id,
        document_id=first.document_id,
        page_no=1,
        quote="human-designated publication quote",
        verified=True,
    )
    session.add(designated)
    session.flush()
    designate_publication_support(
        session,
        dependency.id,
        designated.id,
        principal=TEST_PRINCIPAL,
    )

    report = build_report(session, project_with_two_dependencies.id)
    row = next(
        row
        for row in section(report, "Critical items").rows
        if row[0].value == dependency.ref_code
    )

    assert isinstance(row[0].provenance, Assertion)
    assert row[0].provenance.quote == "human-designated publication quote"


# --- The coordination section and the third provenance class (#177) ----------


def test_coordination_prints_project_decisions_as_project_decisions(
    session, project_with_two_dependencies
):
    from corridor.principals import HumanPrincipal
    from corridor.report import WorkDecision as WorkDecisionProvenance
    from corridor.work_decisions import assign_internal_owner, set_next_action

    recorder = HumanPrincipal("local:coordination-reporter")
    project = project_with_two_dependencies
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).first()
    owner_decision = assign_internal_owner(
        session, dependency.id, "Dana Fields", principal=recorder
    )
    action_decision = set_next_action(
        session,
        dependency.id,
        "Request the relocation schedule",
        due_date=date(2026, 9, 1),
        principal=recorder,
    )

    report = build_report(session, project.id)
    coordination = section(report, "Coordination")
    [row] = coordination.rows
    ref, owner, action, due = row

    assert owner.value == "Dana Fields"
    assert isinstance(owner.provenance, WorkDecisionProvenance)
    assert owner.provenance.decision_ids == (owner_decision.id,)
    assert action.provenance.decision_ids == (action_decision.id,)
    assert due.value == "2026-09-01"
    assert due.provenance.decision_ids == (action_decision.id,)
    # Field-exact: the owner cell and the action cell pin different
    # receipts — nothing borrows a neighbour's provenance.
    assert owner.provenance.decision_ids != action.provenance.decision_ids

    html_out = render(report)
    assert 'class="decision"' in html_out
    assert f"decided {recorder.subject}" in html_out
    assert_no_bare_cells(report)


def test_dense_external_report_stays_inside_a4_and_keeps_appendix_rows_together(
    session,
):
    """The fixed PDF must preserve prose, provenance, and logical rows."""
    import pymupdf

    from corridor.export import to_pdf_bytes
    from corridor.work_decisions import assign_internal_owner, set_next_action

    project = Project(
        slug="dense-report-layout",
        name="Dense External Report Layout",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()

    dependencies = []
    for index in range(1, 18):
        party = ExternalOrg(name=f"UtilityParty{index:02d}")
        dependency = Dependency(
            project_id=project.id,
            ref_code=f"REFROW{index:02d}",
            source_ref=f"SRCROW{index:02d}",
            dep_type="utility_relocation",
            title=f"Dense report layout record {index:02d}",
            station_from=f"{1000 + index}+01",
            station_to=f"{1000 + index}+09",
            external_org_id=None,
        )
        session.add_all((party, dependency))
        session.flush()
        dependency.external_org_id = party.id
        dependencies.append(dependency)
    session.flush()

    recorder = HumanPrincipal("local:report-layout-verifier")
    long_action = (
        "Review the complete public utility status record, reconcile every "
        "facility-specific date and unresolved handoff, then prepare the "
        "targeted follow-up request without omitting its decision provenance"
    )
    assign_internal_owner(
        session,
        dependencies[0].id,
        "Bryce Harmon",
        principal=recorder,
    )
    set_next_action(
        session,
        dependencies[0].id,
        long_action,
        due_date_unknown_reason="awaiting_external_information",
        principal=recorder,
    )

    report = build_report(session, project.id, today=date(2026, 8, 21))
    coordination_action = section(report, "Coordination").rows[0][2]
    milestone_at_risk = section(report, "Milestone readiness").rows[0][4]
    exception_whys = [row[3] for row in section(report, "Exceptions").rows]
    exception_whys[0].value = (
        "The current coordination record cannot establish readiness because its "
        "facility-specific completion date, responsible handoff, and cited closure "
        "evidence all remain unresolved in the published project record"
    )
    appendix_rows = section(report, "Appendix").rows
    pdf_bytes = to_pdf_bytes(render(report))

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        assert pdf.page_count > 1
        page_text = [
            " ".join(page.get_text().split()).replace("- ", "-") for page in pdf
        ]
        compact_page_text = ["".join(text.split()) for text in page_text]
        all_text = " ".join(page_text)

        for cell in (coordination_action, milestone_at_risk, *exception_whys):
            published_cell = " ".join(f"{cell.value} {cell.provenance.marker}".split())
            assert published_cell in all_text

        for row in appendix_rows:
            source_id = row[1].value
            source_pages = [
                page_number
                for page_number, text in enumerate(compact_page_text)
                if source_id in text
            ]
            assert len(source_pages) == 1
            row_page = compact_page_text[source_pages[0]]
            for cell in row:
                published_cell = "".join(
                    f"{cell.value} {cell.provenance.marker}".split()
                )
                assert published_cell in row_page

        report_margin_points = 1.5 * 72 / 2.54
        for page in pdf:
            assert page.rect.width == pytest.approx(595.276, abs=0.1)
            assert page.rect.height == pytest.approx(841.89, abs=0.1)
            for x0, _y0, x1, _y1, text, *_rest in page.get_text("blocks"):
                if not text.strip():
                    continue
                assert x0 >= report_margin_points - 1
                assert x1 <= page.rect.width - report_margin_points + 1


def test_a_decision_cell_over_no_receipts_is_bare(session):
    from corridor.report import WorkDecision as WorkDecisionProvenance

    report = Report(
        project_name="p",
        generated_at=datetime.now(timezone.utc),
        summary=[
            Cell(
                "Internal owner",
                "Dana Fields",
                WorkDecisionProvenance((), "local:x", date(2026, 8, 7)),
            )
        ],
    )
    with pytest.raises(BareCell):
        assert_no_bare_cells(report)


def test_the_report_has_the_seven_documented_sections_now(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    titles = [s.title for s in report.sections]
    assert "Coordination" in titles
    assert titles.index("Critical items") < titles.index("Coordination")


# --- Open unknown-scope External Party commitments (#254) ------------------


def test_report_publishes_open_unknown_scope_party_commitments_with_their_plans(
    session, project_with_two_dependencies
):
    """The working Report preserves party-level truth without a Dependency."""
    from corridor.report import WorkDecision as WorkDecisionProvenance
    from corridor.work_decisions import (
        CoordinationSubject,
        assign_internal_owner,
        set_milestone_impact,
        set_next_action,
    )

    project = project_with_two_dependencies
    party = session.scalars(
        select(ExternalOrg).where(ExternalOrg.name == "AT&T Texas (SWBT)")
    ).one()

    def record(quote, new_timing, *, previous_timing=None):
        document = Document(
            project_id=project.id,
            sha256=hashlib.sha256(quote.encode()).hexdigest(),
            filename=f"{hashlib.sha256(quote.encode()).hexdigest()[:12]}.pdf",
            doc_type="minutes",
            parse_status="parsed",
        )
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text=quote))
        session.flush()
        return record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=date(2025, 1, 16),
            description=quote,
            new_timing=new_timing,
            previous_timing=previous_timing,
            scope=StatementScope.unknown(),
            created_by="local:report-coordinator",
            evidence=CitedStatementEvidence(document.id, 1, quote),
        )

    january = record(
        "AT&T Texas (SWBT) will provide the chain of title in January 2025.",
        StatementTiming.month("January 2025", 2025, 1),
    )
    due_soon = record(
        "AT&T Texas (SWBT) will provide the utility release on February 2, 2025.",
        StatementTiming.day("February 2, 2025", date(2025, 2, 2)),
    )
    changed = record(
        "AT&T Texas (SWBT) moved delivery from December 2024 to February 2025.",
        StatementTiming.month("February 2025", 2025, 2),
        previous_timing=StatementTiming.month("December 2024", 2024, 12),
    )
    subject = CoordinationSubject.statement(changed.commitment_lineage_id)
    owner = assign_internal_owner(
        session, subject, "Dana Fields", principal=TEST_PRINCIPAL
    )
    action = set_next_action(
        session,
        subject,
        "Confirm the revised delivery plan",
        due_date=date(2025, 2, 15),
        principal=TEST_PRINCIPAL,
    )
    impact = set_milestone_impact(
        session,
        subject,
        "not_yet_known",
        principal=TEST_PRINCIPAL,
    )

    report = build_report(session, project.id, today=date(2025, 2, 1))
    commitments = section(report, "External Party commitments")
    assert commitments.columns == [
        "External Party",
        "Supported statement",
        "Timing",
        "Timing precision",
        "Statement type",
        "Commitment Scope",
        "Open / past-due status",
        "Internal Owner",
        "Next Action",
        "Action Due",
        "Milestone Impact",
    ]
    assert len(commitments.rows) == 3

    by_statement = {row[1].value: row for row in commitments.rows}
    january_row = by_statement[january.description]
    due_soon_row = by_statement[due_soon.description]
    changed_row = by_statement[changed.description]
    assert january_row[2].value == "January 2025"
    assert "January 1" not in january_row[2].value
    assert january_row[6].value == "Open · past due"
    assert due_soon_row[6].value == "Open · not past due"
    assert changed_row[2].value == "Previous: December 2024; Current: February 2025"
    assert changed_row[3].value == "Previous: month; Current: month"
    assert changed_row[4].value == "Committed Date Change · later"
    assert changed_row[5].value == "Scope not yet known"
    assert changed_row[7].value == "Dana Fields"
    assert changed_row[8].value == "Confirm the revised delivery plan"
    assert changed_row[9].value == "2025-02-15"
    assert changed_row[10].value == "Not yet known"
    assert isinstance(changed_row[0].provenance, Assertion)
    assert isinstance(changed_row[7].provenance, WorkDecisionProvenance)
    assert changed_row[7].provenance.decision_ids == (owner.id,)
    assert changed_row[8].provenance.decision_ids == (action.id,)
    assert changed_row[10].provenance.decision_ids == (impact.id,)
    assert report.evaluation.statement_publication is report.statement_publication
    captured = snapshot(
        session,
        project.id,
        evaluation=report.evaluation,
        committed_dates=report.committed_dates,
    )
    assert {
        entry["current_event_id"]
        for entry in captured["external_party_commitments"].values()
    } == {january.id, due_soon.id, changed.id}
    from corridor.export import to_pdf_bytes
    import pymupdf

    pdf_bytes = to_pdf_bytes(render(report))
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        pdf_text = "\n".join(page.get_text() for page in pdf)
    normalized_pdf_text = " ".join(pdf_text.split())
    for expected in (
        "Statement type",
        "Committed Date Change · later",
        "Commitment Scope",
        "Scope not yet known",
        "Open / past-due status",
        "Internal Owner",
        "Dana Fields",
        "Next Action",
        "Confirm the revised delivery plan",
        "Action Due",
        "2025-02-15",
        "Milestone Impact",
        "Not yet known",
    ):
        assert expected in normalized_pdf_text
    assert_no_bare_cells(report)


def test_report_marks_a_superseded_statement_plan_for_review(
    session, project_with_two_dependencies
):
    """A changed External Party fact cannot make its old response look current."""
    from corridor.work_decisions import (
        CoordinationSubject,
        assign_internal_owner,
        set_next_action,
    )

    project = project_with_two_dependencies
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).first()
    party = session.get(ExternalOrg, dependency.external_org_id)
    assert party is not None

    def record(quote, timing, *, lineage_id=None):
        document = Document(
            project_id=project.id,
            sha256=hashlib.sha256(quote.encode()).hexdigest(),
            filename=f"{hashlib.sha256(quote.encode()).hexdigest()[:12]}.pdf",
            doc_type="minutes",
            parse_status="parsed",
        )
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text=quote))
        session.flush()
        return record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=date(2025, 1, 16),
            description=quote,
            new_timing=timing,
            scope=StatementScope.unknown(),
            created_by="local:report-coordinator",
            evidence=CitedStatementEvidence(document.id, 1, quote),
            commitment_lineage_id=lineage_id,
        )

    original = record(
        "AT&T Texas (SWBT) will complete the original work in January 2025.",
        StatementTiming.month("January 2025", 2025, 1),
    )
    subject = CoordinationSubject.statement(original.commitment_lineage_id)
    assign_internal_owner(session, subject, "Dana Fields", principal=TEST_PRINCIPAL)
    set_next_action(
        session,
        subject,
        "Confirm the original delivery plan",
        due_date=date(2025, 2, 15),
        principal=TEST_PRINCIPAL,
    )
    revised = record(
        "AT&T Texas (SWBT) now commits to complete work in February 2025.",
        StatementTiming.month("February 2025", 2025, 2),
        lineage_id=original.commitment_lineage_id,
    )

    report = build_report(session, project.id, today=date(2025, 2, 1))
    [row] = section(report, "External Party commitments").rows
    assert row[1].value == revised.description
    assert [cell.value for cell in row[7:]] == [
        "Plan needs review",
        "Plan needs review",
        "Plan needs review",
        "Plan needs review",
    ]
    assert "Dana Fields" not in render(report)
    assert_no_bare_cells(report)


def test_party_commitments_leave_the_open_section_when_scoped_or_closed(
    session, project_with_two_dependencies
):
    """Known scope stays on a Dependency, while closure preserves history."""
    project = project_with_two_dependencies
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).first()
    party = session.get(ExternalOrg, dependency.external_org_id)
    assert party is not None

    def statement(quote, scope):
        document = Document(
            project_id=project.id,
            sha256=hashlib.sha256(quote.encode()).hexdigest(),
            filename=f"{hashlib.sha256(quote.encode()).hexdigest()[:12]}.pdf",
            doc_type="minutes",
            parse_status="parsed",
        )
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text=quote))
        session.flush()
        return record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=date(2025, 1, 16),
            description=quote,
            new_timing=StatementTiming.month("January 2025", 2025, 1),
            scope=scope,
            created_by="local:report-coordinator",
            evidence=CitedStatementEvidence(document.id, 1, quote),
        )

    known = statement(
        "AT&T Texas (SWBT) will relocate the known-scope facility in January 2025.",
        StatementScope.selected((dependency.id,)),
    )
    unknown = statement(
        "AT&T Texas (SWBT) will supply the unknown-scope material in January 2025.",
        StatementScope.unknown(),
    )

    report = build_report(session, project.id, today=date(2025, 2, 1))
    [row] = section(report, "External Party commitments").rows
    assert row[1].value == unknown.description
    assert known.description not in render(report)
    assert dependency.committed_date is None

    closure_quote = (
        "AT&T Texas (SWBT) confirms the unknown-scope material is delivered."
    )
    closure_document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(closure_quote.encode()).hexdigest(),
        filename="closure.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(closure_document)
    session.flush()
    session.add(DocPage(document_id=closure_document.id, page_no=1, text=closure_quote))
    session.flush()
    record_external_party_closure(
        session,
        project_id=project.id,
        commitment_lineage_id=unknown.commitment_lineage_id,
        source_kind="cited",
        event_date=date(2025, 2, 2),
        description=closure_quote,
        created_by="local:report-coordinator",
        evidence=CitedStatementEvidence(closure_document.id, 1, closure_quote),
    )

    after = build_report(session, project.id, today=date(2025, 2, 3))
    assert section(after, "External Party commitments").rows == []
    assert session.get(type(unknown), unknown.id) is unknown
    assert_no_bare_cells(after)


def test_document_only_report_withholds_an_unsupported_unknown_scope_statement(
    session, project_with_two_dependencies
):
    """A bad current citation cannot surface stale timing in a cited-only Report."""
    project = project_with_two_dependencies
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).first()
    party = session.get(ExternalOrg, dependency.external_org_id)
    assert party is not None
    quote = "AT&T Texas (SWBT) will provide the chain of title in January 2025."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(quote.encode()).hexdigest(),
        filename="unsupported-current.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    page = DocPage(document_id=document.id, page_no=1, text=quote)
    session.add(page)
    session.flush()
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description=quote,
        new_timing=StatementTiming.month("January 2025", 2025, 1),
        scope=StatementScope.unknown(),
        created_by="local:report-coordinator",
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )
    page.text = "The registered page no longer supports that statement."
    session.flush()

    report = build_report(
        session,
        project.id,
        today=date(2025, 2, 1),
        document_only=True,
    )
    [row] = section(report, "External Party commitments").rows
    assert row[1].value == "Current statement unsupported in this provenance mode"
    assert row[2].value == "Current timing unsupported"
    assert "January 2025" not in row[2].value
    assert_no_bare_cells(report)


# --- Field-exact provenance in legacy sections (#178) ------------------------


def test_a_field_value_never_wears_the_record_quote(session):
    """The defect this ticket exists for, pinned.

    A record-level publication quote names the row; it says nothing about
    the committed date. The Committed cell therefore falls back to a
    Derivation over the record — never the record quote — until a
    field-scoped support is designated for it.
    """
    from corridor.report import Assertion as ReportAssertion

    project, dependency = _critical_dependency(session)

    report = build_report(session, project.id)
    critical = section(report, "Critical items")
    [row] = critical.rows
    ref, party, committed, need, exceptions = row

    assert isinstance(ref.provenance, ReportAssertion)
    assert isinstance(committed.provenance, Derivation)
    assert committed.provenance.record_ids == (dependency.id,)
    assert isinstance(need.provenance, Derivation)


def test_a_designated_field_support_backs_exactly_its_own_cell(session):
    from corridor.operative_support import designate_publication_support
    from corridor.principals import HumanPrincipal
    from corridor.report import Assertion as ReportAssertion

    project, dependency = _critical_dependency(session)
    field_link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=session.scalars(
            select(Document).where(Document.project_id == project.id)
        )
        .first()
        .id,
        page_no=1,
        quote="committed to relocate by 2026-06-01",
        verified=True,
    )
    session.add(field_link)
    session.flush()
    designate_publication_support(
        session,
        dependency.id,
        field_link.id,
        field_name="committed_date",
        principal=HumanPrincipal("local:field-exact-tester"),
    )

    report = build_report(session, project.id)
    critical = section(report, "Critical items")
    [row] = critical.rows
    committed = row[2]
    party = row[1]

    assert isinstance(committed.provenance, ReportAssertion)
    assert committed.provenance.quote == "committed to relocate by 2026-06-01"
    # The neighbour keeps its own provenance: nothing borrowed sideways.
    assert not (
        isinstance(party.provenance, ReportAssertion)
        and party.provenance.quote == committed.provenance.quote
    )


def _critical_dependency(session):
    """One critical, not-ready record with record-level support only."""
    from corridor.adjudicate import accept_candidate
    from corridor.extraction_runs import (
        declare_active_run,
        record_extraction_run,
    )
    from corridor.principals import HumanPrincipal

    principal = HumanPrincipal("local:field-exact-tester")
    project = Project(slug="field-exact-test", name="Field Exact", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        sha256="fe" * 32,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=(
                "FE-1 Example Water Relocate 1102+20\n"
                "committed to relocate by 2026-06-01"
            ),
        )
    )
    session.flush()
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": "FE-1",
                "external_org": "Example Water",
                "utility_type": "WW",
                "resolution_strategy": "To be removed",
                "committed_date": "2026-06-01",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "FE-1 Example Water Relocate 1102+20",
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": "FE-1",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="fe_v1",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    run = record_extraction_run(
        session,
        document,
        prompt_version="fe_v1",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
    )
    declare_active_run(session, document.id, run.id, principal=principal)
    dependency = accept_candidate(session, candidate, principal=principal)
    dependency.resolution_strategy = "remove"
    session.flush()
    return project, dependency
