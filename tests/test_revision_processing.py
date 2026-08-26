"""Production ordering for exact Revision Comparison then Carry-Forward."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor.adjudicate import accept_candidate
from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    Project,
)
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import CorruptRevisionComparison
from corridor.revision_processing import process_revision_pair
from corridor.supersession import (
    SupersessionDeclaration,
    register_supersessions,
)
from corridor.supersession_review import build_reviewer_worklist


REVIEWER = HumanPrincipal("local:revision-reviewer")


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


def _document(
    session,
    project: Project,
    *,
    registry_id: str,
    sha_character: str,
    filename: str,
    page_text: str,
) -> Document:
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha_character * 64,
        filename=filename,
        doc_type="other" if registry_id == "INDEX" else "matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=page_text,
            image_path=f"/tmp/{filename}.png",
        )
    )
    session.flush()
    return document


def _fields(*, station_from: str = "100+00") -> dict[str, str]:
    return {
        "utility_id": "FOC1-1",
        "external_org": "AT&T",
        "utility_type": "Telecom",
        "station_from": station_from,
    }


def _quote(fields: dict[str, str]) -> str:
    return " ".join(
        (
            fields["utility_id"],
            fields["external_org"],
            fields["utility_type"],
            fields["station_from"],
        )
    )


def _candidate(
    project: Project, document: Document, fields: dict[str, str]
) -> Candidate:
    citation = {
        "document_id": document.id,
        "page": 1,
        "quote": _quote(fields),
        "verified": True,
    }
    return Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": dict(fields),
            "citations": [citation],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="matrix-v1",
        model="test-model",
        citations_verified=True,
    )


def _completed_run(session, document: Document, *candidates: Candidate):
    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix-v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="test-model",
        schema_version="candidate-v1",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=REVIEWER)
    session.flush()
    return run


def _seed_transition(session):
    project = Project(
        slug=f"revision-processing-{uuid4().hex}",
        name="Revision Processing",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])

    predecessor_fields = _fields()
    successor_fields = _fields()
    predecessor = _document(
        session,
        project,
        registry_id="REV-A",
        sha_character="a",
        filename="revision-a.pdf",
        page_text=_quote(predecessor_fields),
    )
    successor = _document(
        session,
        project,
        registry_id="REV-B",
        sha_character="b",
        filename="revision-b.pdf",
        page_text=_quote(successor_fields),
    )
    index = _document(
        session,
        project,
        registry_id="INDEX",
        sha_character="c",
        filename="index.pdf",
        page_text="REV-A superseded by REV-B on 2026-08-01",
    )

    predecessor_candidate = _candidate(project, predecessor, predecessor_fields)
    predecessor_run = _completed_run(session, predecessor, predecessor_candidate)
    dependency = accept_candidate(session, predecessor_candidate, principal=REVIEWER)
    evidence = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == dependency.id)
        .order_by(EvidenceLink.id)
    ).one()
    mark_satisfies(session, dependency.id, evidence.id, principal=REVIEWER)

    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-A",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    successor_candidate = _candidate(project, successor, successor_fields)
    successor_run = _completed_run(session, successor, successor_candidate)
    return {
        "project": project,
        "dependency": dependency,
        "predecessor_run": predecessor_run,
        "successor_run": successor_run,
        "successor_candidate": successor_candidate,
        "index": index,
    }


def test_process_revision_pair_creates_verifies_then_routes_carry_forward(
    session,
):
    scenario = _seed_transition(session)
    result = process_revision_pair(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=scenario["successor_run"].id,
    )

    assert result.comparison.comparison.project_id == scenario["project"].id
    assert len(result.carry_forward.carried) == 1
    assert result.carry_forward.abstentions == ()
    assert build_reviewer_worklist(session, scenario["project"].id).reviews == ()


def test_process_revision_pair_does_not_route_carry_forward_when_readback_fails(
    session, monkeypatch
):
    scenario = _seed_transition(session)
    seen = []

    monkeypatch.setattr(
        "corridor.revision_processing.read_revision_comparison",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CorruptRevisionComparison("digest mismatch")
        ),
    )
    monkeypatch.setattr(
        "corridor.revision_processing.run_automatic_carry_forward",
        lambda *_args, **_kwargs: seen.append("called"),
    )

    with pytest.raises(CorruptRevisionComparison, match="digest mismatch"):
        process_revision_pair(
            session,
            predecessor_extraction_run_id=scenario["predecessor_run"].id,
            successor_extraction_run_id=scenario["successor_run"].id,
        )

    assert seen == []
    assert (
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id == scenario["dependency"].id,
                AuditLog.action == "automatic_carry_forward",
            )
        ).all()
        == []
    )
