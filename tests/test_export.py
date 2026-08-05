from datetime import date

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from corridor.adjudicate import accept_candidate
from corridor.db import Session, engine
from corridor.exceptions import Thresholds, evaluate_project, format_exception_label
from corridor.export import COLUMNS, to_pdf, to_xlsx
from corridor.models import (
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    Project,
)
from corridor.operative_support import designate_publication_support
from corridor.principals import HumanPrincipal
from corridor.report import build_report, render

TEST_PRINCIPAL = HumanPrincipal("local:tester")


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
    project = Project(slug="exp-test", name="Export Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="x9" * 32,
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
            page_no=4,
            text="FOC1-1 Export Test Utility Telecom",
            image_path="/tmp/corridor-missing-page.png",
        )
    )
    session.flush()

    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": "FOC1-1",
                "external_org": "Export Test Utility",
                "utility_type": "Telecom",
                "station_from": "1149+00",
                "station_to": "1153+17",
            },
            "citations": [
                {
                    "document_id": doc.id,
                    "page": 4,
                    "quote": "FOC1-1 Export Test Utility Telecom",
                    "verified": True,
                }
            ],
            "confidence": 1.0,
            "dedupe_hint": "x",
        },
        source_document_id=doc.id,
        source_pages=[4],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    accept_candidate(session, candidate, principal=TEST_PRINCIPAL)
    return project


def test_the_xlsx_keeps_the_citation_columns(session, project, tmp_path):
    """A spreadsheet that drops the provenance is just the matrix they had."""
    path = to_xlsx(
        session,
        project.id,
        tmp_path / "ledger.xlsx",
        evaluation=evaluate_project(session, project.id),
    )
    sheet = load_workbook(path)["Ledger"]

    headers = [c.value for c in sheet[1]]
    assert headers == COLUMNS
    assert "Evidence document" in headers
    assert "Evidence page" in headers
    assert "Evidence quote" in headers

    row = {h: c.value for h, c in zip(headers, sheet[2])}
    assert row["Ref"].startswith("DEP-")
    assert row["Source ID"] == "FOC1-1"
    assert row["External party"] == "Export Test Utility"
    assert row["Evidence page"] == 4
    assert "FOC1-1" in row["Evidence quote"]


def test_the_xlsx_records_what_produced_it(session, project, tmp_path):
    """A snapshot with no ruleset version cannot be reproduced or dated."""
    evaluation = evaluate_project(
        session,
        project.id,
        today=date(2026, 8, 1),
        thresholds=Thresholds(stale_days=21, due_soon_days=9),
    )
    path = to_xlsx(
        session,
        project.id,
        tmp_path / "ledger.xlsx",
        evaluation=evaluation,
    )
    workbook = load_workbook(path)
    assert "Provenance" in workbook.sheetnames

    meta = {row[0]: row[1] for row in workbook["Provenance"].values}
    assert meta["Project"] == "Export Test"
    assert meta["Ruleset version"]
    assert meta["Evaluated on"] == "2026-08-01"
    assert meta["STALE threshold (days)"] == 21
    assert meta["DUE_SOON threshold (days)"] == 9
    assert meta["Records"] == 1


def test_the_xlsx_carries_computed_exceptions(session, project, tmp_path):
    dep = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    dep.committed_date = date(2026, 7, 28)
    dep.need_date = date(2026, 8, 8)
    session.flush()
    evaluation = evaluate_project(session, project.id, today=date(2026, 8, 5))

    path = to_xlsx(
        session,
        project.id,
        tmp_path / "ledger.xlsx",
        evaluation=evaluation,
    )
    sheet = load_workbook(path)["Ledger"]
    headers = [c.value for c in sheet[1]]
    row = {h: c.value for h, c in zip(headers, sheet[2])}
    by_rule = {
        e.rule: e
        for e in evaluation.for_dependency(dep.id)
    }
    assert format_exception_label(by_rule["OVERDUE"]) in (row["Exceptions"] or "")
    assert format_exception_label(by_rule["DUE_SOON"]) in (row["Exceptions"] or "")


def test_the_pdf_renders(session, project, tmp_path):
    html = render(build_report(session, project.id))
    path = to_pdf(html, tmp_path / "report.pdf")
    assert path.exists()
    assert path.read_bytes()[:5] == b"%PDF-"


def test_an_empty_ledger_still_exports(session, tmp_path):
    """A project with nothing adjudicated yet must not crash the export."""
    empty = Project(slug="exp-empty", name="Empty", is_synthetic=True)
    session.add(empty)
    session.flush()
    path = to_xlsx(
        session,
        empty.id,
        tmp_path / "empty.xlsx",
        evaluation=evaluate_project(session, empty.id),
    )
    sheet = load_workbook(path)["Ledger"]
    assert [c.value for c in sheet[1]] == COLUMNS
    assert sheet.max_row == 1


def test_xlsx_uses_publication_support_while_readiness_stays_independent(
    session, project, tmp_path
):
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    original = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    completion = EvidenceLink(
        dependency_id=dependency.id,
        document_id=original.document_id,
        page_no=4,
        quote="completion evidence only",
        verified=True,
        satisfies_requirement=True,
    )
    publication = EvidenceLink(
        dependency_id=dependency.id,
        document_id=original.document_id,
        page_no=4,
        quote="explicit publication evidence",
        verified=True,
    )
    session.add_all([completion, publication])
    session.flush()
    designate_publication_support(
        session,
        dependency.id,
        publication.id,
        principal=TEST_PRINCIPAL,
    )

    path = to_xlsx(
        session,
        project.id,
        tmp_path / "role-scoped.xlsx",
        evaluation=evaluate_project(session, project.id),
    )
    sheet = load_workbook(path)["Ledger"]
    headers = [cell.value for cell in sheet[1]]
    row = {header: cell.value for header, cell in zip(headers, sheet[2])}

    assert row["Ready"] == "yes"
    assert row["Evidence quote"] == "explicit publication evidence"
    assert row["Evidence quote"] != "completion evidence only"
