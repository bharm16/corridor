"""Current/as-of Project Record view and four-reader equivalence gate."""

from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest
from sqlalchemy import select, text

from corridor.current_record import (
    freeze_project_reading_from_current_view,
    measure_current_record_view,
    prove_reader_equivalence,
    read_current_project_record,
    read_project_record_as_of_revision,
)
from corridor.db import Session, engine
from corridor.fact_decisions import include_stationing_fact_by_policy
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    ExtractionRun,
    Fact,
    FactSource,
    Project,
    SourceSegment,
)


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
def record_case(session):
    project = Project(slug="current-record", name="Current Record", is_synthetic=True)
    session.add(project)
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="Utility conflict",
        station_from="200+00",
        station_to=None,
    )
    session.add(dependency)
    document = Document(
        project_id=project.id,
        sha256="d" * 64,
        filename="matrix.xlsx",
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    run = ExtractionRun(
        document_id=document.id,
        prompt_version="current_record_fixture_v1",
        outcome="completed",
        candidate_count=0,
        page_errors=0,
    )
    session.add(run)
    session.flush()
    session.add(ActiveExtractionRun(document_id=document.id, extraction_run_id=run.id))
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"fields": {}},
        source_document_id=document.id,
        source_pages=[1],
        prompt_version=run.prompt_version,
        citations_verified=True,
        state="accepted",
        merged_into=dependency.id,
    )
    session.add(candidate)
    session.flush()
    proposal = ExtractedProposal(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=run.id,
        candidate_id=candidate.id,
        kind="dependency",
        subject_key="Utility Conflicts!3",
        candidate_metadata_json={"state": "pending", "source_pages": [1]},
    )
    session.add(proposal)
    session.flush()
    facts = []
    for ordinal, value in enumerate(("100+00", "200+00"), 1):
        segment = SourceSegment(
            project_id=project.id,
            document_id=document.id,
            kind="spreadsheet_cell",
            exact_text=value,
            content_sha256=sha256(value.encode()).hexdigest(),
            ordinal=ordinal,
            sheet_name="Utility Conflicts",
            cell_range=f"D{ordinal + 2}",
        )
        session.add(segment)
        session.flush()
        fact = Fact(
            project_id=project.id,
            document_id=document.id,
            extraction_run_id=run.id,
            fact_type="station_from",
            subject_kind="source_row",
            subject_key=proposal.subject_key,
            text_value=value,
            date_value=None,
            date_range_start=None,
            date_range_end=None,
            external_org_value_id=None,
            document_value_id=None,
            transformation="trim_cell_text_v1",
            recorded_by="extractor:current_record_fixture_v1",
            content_sha256=("e" if ordinal == 1 else "f") * 64,
        )
        session.add(fact)
        session.flush()
        session.add(
            FactSource(
                project_id=project.id,
                document_id=document.id,
                fact_id=fact.id,
                source_segment_id=segment.id,
                role="value_source",
                ordinal=1,
            )
        )
        session.add(
            ExtractedProposalFact(
                project_id=project.id,
                document_id=document.id,
                extraction_run_id=run.id,
                proposal_id=proposal.id,
                fact_id=fact.id,
                ordinal=ordinal,
            )
        )
        facts.append(fact)
    session.flush()
    first = include_stationing_fact_by_policy(
        session, facts[0], idempotency_key="current-record:first"
    )
    second = include_stationing_fact_by_policy(
        session, facts[1], idempotency_key="current-record:second"
    )
    return project, dependency, first, second


def test_plain_view_and_as_of_revision_project_effective_values(session, record_case):
    project, dependency, first, second = record_case
    assert session.scalar(
        text(
            "select count(*) from pg_views where schemaname='public' "
            "and viewname='current_project_record'"
        )
    ) == 1
    assert session.scalar(
        text("select count(*) from pg_matviews where matviewname='current_project_record'")
    ) == 0
    [current] = read_current_project_record(session, project.id)
    assert (current.dependency_id, current.fact_type, current.text_value) == (
        dependency.id,
        "station_from",
        "200+00",
    )
    assert (
        current.date_value,
        current.date_range_start,
        current.date_range_end,
        current.external_org_value_id,
        current.document_value_id,
    ) == (None, None, None, None, None)
    [before] = read_project_record_as_of_revision(
        session, project.id, first.revision.id
    )
    [after] = read_project_record_as_of_revision(
        session, project.id, second.revision.id
    )
    assert before.text_value == "100+00"
    assert after.text_value == "200+00"


def test_view_query_is_measured_without_materialization(session, record_case):
    project, _dependency, _first, _second = record_case
    measured = measure_current_record_view(session, project.id)
    assert measured.row_count == 1
    assert measured.execution_time_ms >= 0
    assert measured.materialized_view_needed is False


class CoveringClient:
    model = "test-briefing"

    def complete(self, *, user, **_kwargs):
        refs = sorted(set(re.findall(r"\b(?:XB|X|E|A|V)\d+\b", user)))
        return {"sentences": [{"text": "Frozen reading.", "cites": refs}]}


def test_four_reader_surfaces_match_view_fed_frozen_reading(
    session, record_case, tmp_path
):
    project, _dependency, _first, _second = record_case
    result = prove_reader_equivalence(
        session,
        project.id,
        today=date(2026, 8, 31),
        output_dir=tmp_path,
        briefing_client_factory=CoveringClient,
    )
    assert result.passed is True
    assert result.explanations == (
        "XLSX package timestamps are excluded; every workbook cell is compared.",
        "PDF container metadata is excluded; normalized rendered page text is compared.",
    )
    viewed = freeze_project_reading_from_current_view(session, project.id)
    assert viewed.rows[0].dependency.station_from == "200+00"


def test_release_pdf_text_comparison_discriminates_content_not_the_timestamp():
    """The equivalence gate compares what the release PDF says, page by page.

    Two renders that differ only in their "Generated ... UTC" stamp read the
    same; a render that changes one word, or gains a page, does not. The
    fixture declares its own text, so the reading is checked against what was
    placed rather than against another reader.
    """
    from pdf_fixture_support import PdfFixture

    from corridor.current_record import _pdf_text

    def release(stamp: str, statement: str, *, extra_page: bool = False) -> bytes:
        fixture = PdfFixture()
        page = fixture.add_page()
        page.text((72, 72), f"Generated {stamp} UTC · evaluated 2026-08-31", fontsize=10)
        page.text((72, 120), statement, fontsize=11)
        if extra_page:
            fixture.add_page().text((72, 72), "Appendix", fontsize=11)
        return fixture.tobytes()

    first = release("2026-08-31 09:00", "CenterPoint will relocate the gas main.")
    restamped = release("2026-08-31 09:07", "CenterPoint will relocate the gas main.")
    reworded = release("2026-08-31 09:00", "CenterPoint will retain the gas main.")
    longer = release("2026-08-31 09:00", "CenterPoint will relocate the gas main.", extra_page=True)

    assert _pdf_text(first) == _pdf_text(restamped)
    assert _pdf_text(first) == (
        "Generated <normalized> UTC · evaluated 2026-08-31\n"
        "CenterPoint will relocate the gas main.",
    )
    assert _pdf_text(first) != _pdf_text(reworded)
    assert len(_pdf_text(longer)) == 2 and _pdf_text(longer)[0] == _pdf_text(first)[0]


def test_phase0_scale_performance_and_equivalence_receipt_is_recorded():
    path = (
        Path(__file__).resolve().parents[1]
        / "artifacts/current-record-view/phase0-scale-receipt.json"
    )
    receipt = json.loads(path.read_text())
    assert receipt["view_row_count"] == 1009
    assert receipt["legacy_dependency_count"] == 515
    assert receipt["frozen_phase0_dependency_count"] == 516
    assert receipt["materialized_view_needed"] is False
    assert receipt["escalation"] == "none; retain the plain PostgreSQL view"
    assert all(receipt["reader_equivalence"][name] is True for name in (
        "coordination_report_identical",
        "briefing_identical",
        "workbook_cells_identical",
        "release_pdf_text_identical",
    ))
