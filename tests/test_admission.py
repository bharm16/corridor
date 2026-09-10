"""The landing stage: documents arrive and the list builds itself.

ADR-0029 deleted the screen that stood here. What is left is one entry
point that declares the unambiguous readings, admits the conflicts the
matrices can anchor, and attaches the statements the minutes place —
unattended, in that order, with a receipt for each act.
"""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
from sqlalchemy import select

from corridor.admission import load_and_report, load_project
from corridor.extraction_runs import record_extraction_run
from corridor.models import (
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DocPage,
    Document,
    ExternalOrg,
    PolicyRun,
    Project,
)

PIPELINE = "Tejas Pipeline Co"


@pytest.fixture
def project(session):
    p = Project(
        slug="landing-stage-test",
        name="Landing Stage Test",
        is_synthetic=True,
        project_side_parties=["LJA Engineering"],
    )
    session.add(p)
    session.flush()
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    return p


def _document(session, project, filename, doc_type="matrix", doc_date=None):
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(filename.encode()).hexdigest(),
        filename=filename,
        doc_type=doc_type,
        parse_status="parsed",
        pages=1,
        doc_date=doc_date,
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text="rows"))
    session.flush()
    return document


def _conflict(document, uid):
    fields = {
        "utility_id": uid,
        "external_org": PIPELINE,
        "utility_type": "Petroleum and Gaseous Materials",
        "station_from": "1102+20",
        "station_to": "1102+80",
    }
    quote = " | ".join(fields.values())
    return Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields,
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": quote,
            "text_source": "text_layer",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="matrix_v1",
        model="gpt-test",
        citations_verified=True,
    )


def _statement(document, ref):
    return Candidate(
        project_id=document.project_id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "commitment",
                "description": f"Tejas committed on {ref}",
                "external_org": PIPELINE,
                "stated_party": PIPELINE,
                "event_date": "2025-01-16",
                "committed_date": "2025-06-01",
                "conflict_ref": ref,
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "rows",
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": f"event|{ref}",
            "text_source": "text_layer",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="minutes_v1",
        model="gpt-test",
        citations_verified=True,
    )


def _read(session, document, candidates, prompt_version="matrix_v1"):
    # These landing fixtures are about Active Runs and admission ordering.  The
    # named organizations are established registry inputs, not an implicit
    # side effect of the admission writer (ADR-0051).
    for candidate in candidates:
        fields = (candidate.payload_json or {}).get("fields", {})
        name = fields.get("external_org") if isinstance(fields, dict) else None
        if name and not session.scalar(select(ExternalOrg).where(ExternalOrg.name == name)):
            session.add(ExternalOrg(name=name, aliases=[]))
    session.flush()
    for candidate in candidates:
        candidate.prompt_version = prompt_version
        session.add(candidate)
    run = record_extraction_run(
        session,
        document,
        prompt_version=prompt_version,
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="gpt-test",
        schema_version="matrix_candidate_shape_v1",
        allow_unsealed_legacy=True,
    )
    session.flush()
    return run


def test_one_matrix_landing_is_the_whole_setup(session, project):
    """No signature, no second revision, no command: documents land and
    the conflicts are on the record."""
    matrix = _document(session, project, "ucm.pdf")
    _read(session, matrix, [_conflict(matrix, "PL1"), _conflict(matrix, "PL2")])

    result = load_project(session, project.id)

    assert result.declared_documents == 1
    assert result.admitted_count == 2
    assert result.waiting_count == 0
    assert {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"PL1", "PL2"}


def test_the_declaration_names_the_machine_that_made_it(session, project):
    """Nothing pretends a human declared this."""
    matrix = _document(session, project, "ucm.pdf")
    _read(session, matrix, [_conflict(matrix, "PL1")])

    load_project(session, project.id)

    declaration = session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == matrix.id
        )
    ).one()
    assert declaration.declared_by == "corridor:active-run-declaration"


def test_statements_attach_to_the_conflicts_admitted_in_the_same_pass(
    session, project
):
    """Order is load-bearing: a statement admitted before its conflict
    would find nothing to attach to."""
    matrix = _document(session, project, "ucm.pdf", doc_date=date(2025, 2, 23))
    minutes = _document(
        session, project, "minutes.pdf", "minutes", date(2025, 1, 16)
    )
    _read(session, matrix, [_conflict(matrix, "PL1")])
    _read(
        session,
        minutes,
        [_statement(minutes, "PL1")],
        prompt_version="minutes_v1",
    )

    result = load_project(session, project.id)

    assert result.dependencies.admitted_count == 1
    assert result.events.admitted_count == 1
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    [event] = session.scalars(
        select(DependencyEvent)
        .join(DependencyEventScope, DependencyEventScope.event_id == DependencyEvent.id)
        .where(DependencyEventScope.dependency_id == dependency.id)
    ).all()
    assert event.event_type == "commitment"


def test_a_second_pass_adds_nothing_and_still_writes_its_receipts(
    session, project
):
    """Re-running when documents land again is safe: the record does not
    grow, and each pass says honestly that it did nothing."""
    matrix = _document(session, project, "ucm.pdf")
    _read(session, matrix, [_conflict(matrix, "PL1")])

    first = load_project(session, project.id)
    second = load_project(session, project.id)

    assert first.admitted_count == 1
    assert second.admitted_count == 0
    assert second.waiting_count == 0
    assert (
        len(
            session.scalars(
                select(Dependency).where(Dependency.project_id == project.id)
            ).all()
        )
        == 1
    )
    runs = session.scalars(
        select(PolicyRun).where(PolicyRun.project_id == project.id)
    ).all()
    assert len(runs) == 4  # two families, two passes
    assert all(run.policy_approval_id is None for run in runs)


def test_an_ambiguous_document_waits_while_the_rest_of_the_project_loads(
    session, project
):
    """One document nobody has chosen a reading for must not keep a whole
    project dark — the wall this decision removed, in miniature."""
    clean = _document(session, project, "ucm.pdf")
    _read(session, clean, [_conflict(clean, "PL1")])

    twice_read = _document(session, project, "ucm-draft.pdf")
    _read(session, twice_read, [_conflict(twice_read, "PL9")])
    _read(session, twice_read, [_conflict(twice_read, "PL9")])

    result = load_project(session, project.id)

    assert result.ambiguous_documents == ["ucm-draft.pdf"]
    assert session.get(ActiveExtractionRun, twice_read.id) is None
    assert result.dependencies.admitted_count == 1
    assert {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"PL1"}

    summary = load_and_report(session, project)
    # The first line remains a legacy acceptance-receipt contract.
    assert "0 conflicts and 0 statements on the record" in summary
    assert "Current Production Run" in summary
    assert "Extracted Proposals" in summary
    assert "ucm-draft.pdf" in summary


def test_a_project_with_nothing_read_yet_loads_to_an_honest_zero(
    session, project
):
    """A project whose documents have not been extracted is not an error
    and not a wall; it is a project with nothing to show yet."""
    _document(session, project, "ucm.pdf")

    result = load_project(session, project.id)

    assert result.declared_documents == 0
    assert result.admitted_count == 0
    assert result.waiting_count == 0


def test_a_per_party_matrix_loads_whole_once_its_scheme_is_declared(
    session, project
):
    """The SR 789 shape: one matrix, nine parties, every party's list
    counting from 1. Declared per-party, all of it lands (ADR-0030)."""
    matrix = _document(session, project, "fdot-ucm.pdf")
    matrix.numbering_scheme = "per-party"
    session.flush()

    rows = []
    for org in ("AT&T TCA", "Synthetic Cable Co", "TECO Peoples Gas"):
        for number in ("1", "2"):
            candidate = _conflict(matrix, number)
            candidate.payload_json["fields"]["external_org"] = org
            candidate.payload_json["dedupe_hint"] = f"{org}|{number}"
            rows.append(candidate)
    _read(session, matrix, rows)

    result = load_project(session, project.id)

    assert result.admitted_count == 6
    assert result.waiting_count == 0
    admitted = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all()
    assert sorted(d.source_ref for d in admitted) == [
        "1", "1", "1", "2", "2", "2",
    ]
