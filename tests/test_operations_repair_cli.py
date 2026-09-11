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


# --- the third procedure: the source-grounded re-capture (ADR-0100, ADR-0101) ---


@pytest.fixture
def reported(runtime_database):
    """One project with a coordinator's extraction-error report standing on it."""

    from capture_correction_support import ALICE, Misread
    from access_support import seed_membership

    factory = runtime_database.session_factory
    with factory() as session:
        project = Project(
            slug=f"correct-cli-{uuid4().hex[:8]}",
            name="Correction CLI",
            is_synthetic=True,
        )
        session.add(project)
        session.flush()
        seed_membership(session, project, ALICE)
        session.add(
            ProjectRosterEntry(
                project_id=project.id,
                principal_subject=OPERATOR,
                display_name="Operator",
                active=True,
                is_technical_operator=True,
            )
        )
        session.flush()
        misread = Misread(session, project)
        report = misread.report()
        session.commit()
        return factory, project.slug, int(report.id), int(misread.delta.id)


def test_the_runbook_lists_the_reports_the_correction_procedure_acts_on(
    reported, capsys
):
    """An operator needs the request id before they can name one, so it prints them."""

    factory, slug, request_id, delta_id = reported

    assert main(["reports", slug], session_factory=factory) == 0

    payload = _payload(capsys)
    (row,) = payload["reports"]
    assert row["request_id"] == request_id
    assert row["delta_id"] == delta_id
    assert row["outcomes"] == []


def test_the_runbook_performs_the_correction_and_leaves_its_receipt(
    reported, principal, capsys
):
    """The whole procedure, driven the way an operator drives it.

    What the runbook proves beyond the module tests is that the entry point
    exists, attributes the act to the deployment's own principal, and commits:
    the retirement is there in a later transaction.
    """

    from corridor.models import DeltaCaptureCorrection

    factory, slug, request_id, delta_id = reported

    assert (
        main(
            ["correct-capture", slug, f"--request-id={request_id}"],
            session_factory=factory,
        )
        == 0
    )

    payload = _payload(capsys)
    assert payload["outcome"] == "no_change"
    assert payload["retirement_id"] is not None
    assert payload["replacement_delta_id"] is None
    with factory() as verify:
        assert verify.scalars(
            select(DeltaCaptureCorrection).where(
                DeltaCaptureCorrection.delta_id == delta_id
            )
        ).one()

    # And the report now says what became of it.
    assert main(["reports", slug], session_factory=factory) == 0
    (row,) = _payload(capsys)["reports"]
    assert row["outcomes"] == ["no_change"]


def test_the_runbook_records_an_investigation_it_could_not_substantiate(
    reported, principal, capsys
):
    """`--unsubstantiated` claims no correction and retires nothing."""

    from corridor.models import DeltaCaptureCorrection

    factory, slug, request_id, delta_id = reported

    assert (
        main(
            [
                "correct-capture",
                slug,
                f"--request-id={request_id}",
                "--unsubstantiated",
                "--finding=the cell is legible and says neither value",
            ],
            session_factory=factory,
        )
        == 0
    )

    payload = _payload(capsys)
    assert payload["outcome"] == "inconclusive"
    assert payload["retirement_id"] is None
    assert payload["corrected_fact_id"] is None
    with factory() as verify:
        assert not verify.scalars(
            select(DeltaCaptureCorrection).where(
                DeltaCaptureCorrection.delta_id == delta_id
            )
        ).first()
