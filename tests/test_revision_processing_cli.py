"""Operator CLI for exact Revision Comparison then authorized carry only."""

from __future__ import annotations

from datetime import date
import json
from types import SimpleNamespace
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
from corridor.revision_comparison import (
    CorruptRevisionComparison,
    list_revision_comparisons,
)
from corridor.revision_processing_cli import main
from corridor.supersession import SupersessionDeclaration, register_supersessions


REVIEWER = HumanPrincipal("local:revision-reviewer")
APPROVER = HumanPrincipal("local:revision-approver")


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


class _OpenSession:
    """Use the test transaction without letting the CLI close the Session."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        session = self.session

        class Context:
            def __enter__(self):
                return session

            def __exit__(self, *_):
                return False

        return Context()


class _RecordingSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _SessionFactory:
    def __init__(self, session):
        self.session = session
        self.opens = 0

    def __call__(self):
        factory = self

        class Context:
            def __enter__(self):
                factory.opens += 1
                return factory.session

            def __exit__(self, *_):
                return False

        return Context()


def _json_output(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


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
        slug=f"revision-processing-cli-{uuid4().hex}",
        name="Revision Processing CLI",
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
        "dependency": dependency,
        "predecessor_run": predecessor_run,
        "project": project,
        "successor_run": successor_run,
    }


def test_revision_process_cli_runs_exact_pair_and_reports_compact_json(session, capsys):
    scenario = _seed_transition(session)
    assert (
        main(
            [
                str(scenario["predecessor_run"].id),
                str(scenario["successor_run"].id),
            ],
            session_factory=_OpenSession(session),
        )
        == 0
    )

    payload = _json_output(capsys)
    assert payload == {
        "abstentions": {
            "count": 0,
            "reason_version": "automatic-carry-forward-abstentions-v1",
            "reasons": {},
        },
        "carried_count": 1,
        "comparison": {
            "content_sha256": payload["comparison"]["content_sha256"],
            "finding_count": 1,
            "finding_counts": {"unchanged": 1},
            "id": payload["comparison"]["id"],
        },
        "predecessor_extraction_run_id": scenario["predecessor_run"].id,
        "successor_extraction_run_id": scenario["successor_run"].id,
    }
    assert len(payload["comparison"]["content_sha256"]) == 64
    assert (
        len(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == "dependency",
                    AuditLog.entity_id == scenario["dependency"].id,
                    AuditLog.action == "automatic_carry_forward",
                )
            ).all()
        )
        == 1
    )


def test_revision_process_cli_retry_reports_the_same_comparison(session, capsys):
    scenario = _seed_transition(session)
    argv = [
        str(scenario["predecessor_run"].id),
        str(scenario["successor_run"].id),
    ]

    assert main(argv, session_factory=_OpenSession(session)) == 0
    first = _json_output(capsys)
    assert main(argv, session_factory=_OpenSession(session)) == 0
    retried = _json_output(capsys)

    assert retried["comparison"] == first["comparison"]
    assert [
        comparison.id
        for comparison in list_revision_comparisons(
            session,
            scenario["predecessor_run"].id,
            scenario["successor_run"].id,
        )
    ] == [first["comparison"]["id"]]


def test_revision_process_cli_runs_released_policy_without_project_authorization(
    session, capsys
):
    scenario = _seed_transition(session)

    assert (
        main(
            [
                str(scenario["predecessor_run"].id),
                str(scenario["successor_run"].id),
            ],
            session_factory=_OpenSession(session),
        )
        == 0
    )

    payload = _json_output(capsys)
    assert payload["carried_count"] == 1
    assert payload["abstentions"] == {
        "count": 0,
        "reason_version": "automatic-carry-forward-abstentions-v1",
        "reasons": {},
    }
    assert payload["comparison"]["finding_counts"] == {"unchanged": 1}
    assert (
        len(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == "dependency",
                    AuditLog.entity_id == scenario["dependency"].id,
                    AuditLog.action == "automatic_carry_forward",
                )
            ).all()
        )
        == 1
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["0", "2"],
        ["-1", "2"],
        ["nope", "2"],
        ["1", "2", "--principal=local:bryce"],
    ],
)
def test_revision_process_cli_rejects_invalid_ids_and_policy_flags(argv, capsys):
    class MustNotConnect:
        def __call__(self):
            raise AssertionError("argument validation must precede DB access")

    assert main(argv, session_factory=MustNotConnect()) == 2
    assert capsys.readouterr().err


def test_revision_process_cli_rolls_back_and_exits_cleanly_on_failure(
    monkeypatch, capsys
):
    session = _RecordingSession()
    factory = _SessionFactory(session)
    seen = []

    def fail(db, **kwargs):
        seen.append((db, kwargs))
        raise CorruptRevisionComparison("digest mismatch")

    monkeypatch.setattr("corridor.revision_processing_cli.process_revision_pair", fail)

    assert main(["11", "22"], session_factory=factory) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "digest mismatch" in captured.err
    assert seen == [
        (
            session,
            {
                "predecessor_extraction_run_id": 11,
                "successor_extraction_run_id": 22,
            },
        )
    ]
    assert session.commits == 0
    assert session.rollbacks == 1
