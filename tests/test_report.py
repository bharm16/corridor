from datetime import date
from html import escape as escape_html

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from corridor.adjudicate import accept_candidate
from corridor.changes import record_run, snapshot
from corridor.db import Session, engine
from corridor.exceptions import evaluate_project, format_exception_label
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.export import to_xlsx
from corridor.ledger import browse
from corridor.models import (
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
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
    declare_active_run(session, doc.id, run.id)
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
        for cell in row[:5]:
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
    before = [row[0].value for row in section(
        build_report(session, project_with_two_dependencies.id), "Critical items"
    ).rows]

    _unverify_evidence_of(session, critical)
    report = build_report(session, project_with_two_dependencies.id)
    after = [row[0].value for row in section(report, "Critical items").rows]

    assert before == after
    assert critical.ref_code in after
    assert critical.ref_code in render(report)


def test_the_report_has_the_six_documented_sections(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    titles = [s.title for s in report.sections]
    assert titles == [
        "Milestone readiness",
        "Critical items",
        "Exceptions",
        "Changes since last report",
        "Aging",
        "Appendix — full ledger",
    ]


def test_an_empty_section_says_why_rather_than_showing_nothing(
    session, project_with_two_dependencies
):
    """"No milestones imported" and "nothing is overdue" are different
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


def test_the_report_states_what_it_does_not_cover(
    session, project_with_two_dependencies
):
    """A report over 6 of 1,340 extracted records is a report about almost
    nothing, and it must say so."""
    from corridor.models import Candidate, Document

    doc = session.scalars(
        select(Document).where(
            Document.project_id == project_with_two_dependencies.id
        )
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


def test_the_exceptions_section_counts_by_rule(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    rules = {row[0].value for row in section(report, "Exceptions").rows}
    assert "ORPHAN" in rules


def test_the_appendix_lists_every_ledger_record(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    assert len(section(report, "Appendix").rows) == 2


def test_readiness_in_the_report_is_computed_not_stored(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    ready_summary = next(c for c in report.summary if c.label == "Ready")
    assert ready_summary.value == "0"

    link = _a_link_of(session, project_with_two_dependencies.id)
    link.satisfies_requirement = True
    session.flush()

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


def test_critical_items_with_no_dates_says_so(
    session, project_with_two_dependencies
):
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
    """"Declared presentation": the note names the one quantity the list
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
    critical.committed_date = date(2026, 7, 28)
    critical.need_date = date(2026, 8, 8)
    session.flush()

    report = build_report(session, project.id, today=date(2026, 8, 5))
    found = section(report, "Critical items")
    exceptions_cell = found.rows[0][found.columns.index("Exceptions")]
    by_rule = {
        e.rule: e
        for e in report.evaluation.for_dependency(critical.id)
    }

    assert "ORPHAN" in exceptions_cell.value
    assert format_exception_label(by_rule["OVERDUE"]) in exceptions_cell.value
    assert format_exception_label(by_rule["DUE_SOON"]) in exceptions_cell.value


def test_the_exceptions_summary_exemplar_is_the_largest_quantity_or_nothing(
    session, project_with_two_dependencies
):
    """"Worst" regains a meaning: the most days, checkable against the
    record. A rule whose fact is an absence has no exemplar — every row
    is the same finding, and electing one would be an arbitrary pick
    wearing a superlative."""
    report = build_report(
        session, project_with_two_dependencies.id
    )
    found = section(report, "Exceptions")
    by_rule = {row[0].value: row for row in found.rows}

    most_days = found.columns.index("Most days")
    assert by_rule["ORPHAN"][most_days].value == "—"
    # The fixture's document is undated, so STALE is the absence case —
    # "no dated evidence at all" has no age, and no exemplar either. The
    # largest-quantity path is pinned at the engine seam and in the
    # critical row's exception listing.
    assert by_rule["STALE"][most_days].value == "—"


def _overdue_by(session, project_id, days):
    """Give every dependency a committed date `days` in the past."""
    from datetime import date, timedelta

    from corridor.models import Dependency

    committed = date(2026, 6, 1)
    for dependency in session.scalars(
        select(Dependency).where(Dependency.project_id == project_id)
    ).all():
        dependency.committed_date = committed
    session.flush()
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
        dependency.committed_date = committed
    session.flush()
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


def test_a_change_cites_the_record_it_describes(
    session, project_with_two_dependencies
):
    """Every cell of the Changes section used to carry an empty tuple."""
    from corridor.models import Dependency

    project_id = project_with_two_dependencies.id
    record_run(session, project_id, evaluation=evaluate_project(session, project_id))
    dependency = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project_with_two_dependencies.id)
        .order_by(Dependency.id)
    ).first()
    dependency.status = "closed"
    session.flush()

    report = build_report(session, project_with_two_dependencies.id)
    changes = section(report, "Changes since last report")

    assert changes.rows
    for row in changes.rows:
        for cell in row:
            assert cell.provenance.resolves
    closed = next(r for r in changes.rows if r[1].value == "closed")
    assert closed[0].provenance.record_ids == (dependency.id,)


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

    assert "MISSING_EVIDENCE" in markup
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
    """"Ready 0" for an unlinked milestone reads as a measurement.

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
