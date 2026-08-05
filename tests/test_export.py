import pytest
from openpyxl import load_workbook

from corridor.adjudicate import accept_candidate
from corridor.db import Session, engine
from corridor.exceptions import evaluate_project
from corridor.export import COLUMNS, to_pdf, to_xlsx
from corridor.models import Candidate, Document, Project
from corridor.report import build_report, render


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
    accept_candidate(session, candidate, actor="tester")
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
    path = to_xlsx(
        session,
        project.id,
        tmp_path / "ledger.xlsx",
        evaluation=evaluate_project(session, project.id),
    )
    workbook = load_workbook(path)
    assert "Provenance" in workbook.sheetnames

    meta = {row[0]: row[1] for row in workbook["Provenance"].values}
    assert meta["Project"] == "Export Test"
    assert meta["Ruleset version"]
    assert meta["Records"] == 1


def test_the_xlsx_carries_computed_exceptions(session, project, tmp_path):
    path = to_xlsx(
        session,
        project.id,
        tmp_path / "ledger.xlsx",
        evaluation=evaluate_project(session, project.id),
    )
    sheet = load_workbook(path)["Ledger"]
    headers = [c.value for c in sheet[1]]
    row = {h: c.value for h, c in zip(headers, sheet[2])}
    assert "ORPHAN" in (row["Exceptions"] or "")


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
