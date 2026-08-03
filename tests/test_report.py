import pytest
from sqlalchemy import select

from corridor.adjudicate import accept_candidate
from corridor.db import Session, engine
from corridor.models import Candidate, Document, EvidenceLink, Project
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
        session.add(candidate)
        session.flush()
        accept_candidate(session, candidate, actor="bryce")

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


def test_row_figures_are_assertions_carrying_a_page_and_quote(
    session, project_with_two_dependencies
):
    report = build_report(session, project_with_two_dependencies.id)
    ref = report.rows[0][0]

    assert isinstance(ref.provenance, Assertion)
    assert ref.provenance.page_no == 1
    assert ref.provenance.quote
    assert ref.provenance.marker.startswith("[D")
    assert "p.1]" in ref.provenance.marker


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
    link = _a_link_of(session, project_with_two_dependencies.id)
    link.quote = '<script>alert("x")</script>'
    session.flush()

    out = render(build_report(session, project_with_two_dependencies.id))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
