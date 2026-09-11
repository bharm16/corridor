"""The staff runbook that performs the receipted source repair (#842).

The audit allowed a staff-only runbook to perform the repair at first, on the
condition that it is attributable and leaves a receipt. These tests drive the
same command an operator runs, over committed transactions, so what is proved
is that the entry point exists, refuses without an attributable principal,
refuses without the designation, and writes the receipt the register reads.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor import access
from corridor.config import settings
from corridor.extraction_runs import record_extraction_run
from corridor.models import Document, Project, ProjectRosterEntry
from corridor.operations_repair_cli import main

OPERATOR = "local:operator"


def _payload(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


@pytest.fixture
def principal(monkeypatch):
    monkeypatch.setattr(settings, "human_principal", OPERATOR)


@pytest.fixture
def blocked(runtime_database):
    """One project with one source the standing pass has stopped taking."""

    factory = runtime_database.session_factory
    with factory() as session:
        project = Project(
            slug=f"repair-cli-{uuid4().hex[:8]}", name="Repair CLI", is_synthetic=True
        )
        session.add(project)
        session.flush()
        session.add(
            ProjectRosterEntry(
                project_id=project.id,
                principal_subject=OPERATOR,
                display_name="Operator",
                active=True,
                is_technical_operator=True,
            )
        )
        document = Document(
            project_id=project.id,
            sha256="d" * 64,
            filename="unreadable.pdf",
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        session.add(document)
        session.flush()
        record_extraction_run(
            session,
            document,
            prompt_version="matrix_v1",
            candidate_count=0,
            page_errors=1,
            outcome="unreadable",
            model="gpt-test",
            schema_version="matrix_candidate_shape_v1",
            allow_unsealed_legacy=True,
        )
        session.commit()
        return factory, project.slug, document.id


def test_the_runbook_repairs_one_source_and_prints_its_receipt(
    blocked, principal, capsys
):
    factory, slug, document_id = blocked

    assert main(["show", slug], session_factory=factory) == 0
    before = _payload(capsys)
    assert [item["document_id"] for item in before["blocked"]] == [document_id]
    assert before["blocked"][0]["owner"] == "Corridor Operations"
    assert before["blocked"][0]["repair"] == (
        "No repair has been recorded for this source."
    )

    assert (
        main(
            ["repair", slug, f"--document-id={document_id}"], session_factory=factory
        )
        == 0
    )
    receipt = _payload(capsys)
    assert receipt["blocked_by"] == "unreadable_permanent"
    assert receipt["outcome"] == "readmitted"
    assert receipt["audit_id"]

    assert main(["show", slug], session_factory=factory) == 0
    after = _payload(capsys)
    assert after["blocked"][0]["repaired_by"] == OPERATOR
    assert "put this source back" in after["blocked"][0]["repair"]


def test_the_runbook_refuses_without_an_attributable_principal(
    blocked, monkeypatch, capsys
):
    factory, slug, document_id = blocked
    monkeypatch.setattr(settings, "human_principal", "")

    assert (
        main(
            ["repair", slug, f"--document-id={document_id}"], session_factory=factory
        )
        == 1
    )
    assert "CORRIDOR_HUMAN_PRINCIPAL" in capsys.readouterr().err


def test_the_runbook_refuses_an_operator_without_the_designation(
    blocked, principal, capsys
):
    factory, slug, document_id = blocked
    with factory() as session:
        entry = session.scalars(
            select(ProjectRosterEntry).where(
                ProjectRosterEntry.principal_subject == OPERATOR
            )
        ).one()
        entry.is_technical_operator = False
        session.commit()

    assert (
        main(
            ["repair", slug, f"--document-id={document_id}"], session_factory=factory
        )
        == 1
    )
    assert access.TECHNICAL_OPERATIONS.replace("_", "-") in capsys.readouterr().err
