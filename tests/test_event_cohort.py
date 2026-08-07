"""The event cohort: a stated rule over the event stream, an immutable receipt.

A sibling of the rehearsal cohort (test_cohort.py) for cohorts no Revision
Comparison selects: membership is a pure function of the declared Active
Runs the rule reads and one rule version. A conflict enters when at least
one dated commitment, slip, or closure event references it and the
reference matches a dependency Candidate's utility_id. Re-deriving from
the same declared inputs yields the same members and the same digest, or
it refuses (docs/sh99-date-rehearsal.md; ADR-0026 is the admission path
that will consume the boundary).
"""

import hashlib

import pytest
from sqlalchemy import select, update

from corridor.cohort import (
    EVENT_COHORT_RULE_VERSION,
    CohortDerivationError,
    CohortScopeViolation,
    derive_event_cohort_receipt,
    event_cohort_candidate_ids,
    require_event_cohort_member,
)
from corridor.db import Session, engine
from corridor.extraction_runs import (
    MultipleRunsNeedExplicitChoice,
    declare_single_run_documents,
    record_extraction_run,
)
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Document,
    EventCohortReceipt,
    Project,
)
from corridor.principals import HumanPrincipal

DECLARER = HumanPrincipal("local:event-cohort-declarer")
PIPELINE = "Acme Pipeline"
ELECTRIC = "Volt Transmission"


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
    p = Project(
        slug="event-cohort-test", name="Event Cohort Test", is_synthetic=True
    )
    session.add(p)
    session.flush()
    return p


def _document(session, project, *, registry_id, filename, doc_type="matrix"):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=hashlib.sha256(registry_id.encode()).hexdigest(),
        filename=filename,
        doc_type=doc_type,
        parse_status="parsed",
        pages=4,
    )
    session.add(document)
    session.flush()
    return document


def _candidate(document, *, kind, fields):
    quote = " | ".join(str(v) for v in fields.values() if v is not None)
    return Candidate(
        project_id=document.project_id,
        kind=kind,
        payload_json={
            "kind": kind,
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
        prompt_version="minutes_v1",
        model="gpt-test",
        citations_verified=True,
    )


def _run(session, document, candidates):
    for candidate in candidates:
        session.add(candidate)
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes_v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="gpt-test",
        schema_version="matrix_candidate_shape_v1",
    )
    session.flush()
    return run


def _dep_fields(utility_id, *, org=PIPELINE):
    return {
        "utility_id": utility_id,
        "external_org": org,
        "utility_type": "Petroleum and Gaseous Materials",
        "baseline": "SR-BL",
        "station_from": "1102+20",
        "station_to": "1102+80",
    }


def _event_fields(
    *,
    event_type,
    conflict_ref,
    event_date=None,
    committed_date=None,
    org=PIPELINE,
):
    return {
        "event_type": event_type,
        "description": f"{org} spoke about {conflict_ref or 'the project'}",
        "external_org": org,
        "event_date": event_date,
        "committed_date": committed_date,
        "conflict_ref": conflict_ref,
    }


@pytest.fixture
def corpus(session, project):
    """One matrix run (dependencies) and one minutes run (events)."""
    matrix = _document(
        session, project, registry_id="EC-MATRIX-1", filename="matrix.pdf"
    )
    matrix_run = _run(
        session,
        matrix,
        [
            _candidate(matrix, kind="dependency", fields=_dep_fields("PL1")),
            _candidate(
                matrix,
                kind="dependency",
                fields=_dep_fields("ET7", org=ELECTRIC),
            ),
        ],
    )
    minutes = _document(
        session,
        project,
        registry_id="EC-MIN-1",
        filename="minutes.pdf",
        doc_type="minutes",
    )
    minutes_run = _run(
        session,
        minutes,
        [
            # Qualifies: dated commitment referencing a known dependency.
            _candidate(
                minutes,
                kind="event",
                fields=_event_fields(
                    event_type="commitment",
                    conflict_ref="PL1",
                    event_date="2025-01-16",
                ),
            ),
            # Qualifies: slip with only a committed date, second party.
            _candidate(
                minutes,
                kind="event",
                fields=_event_fields(
                    event_type="slip",
                    conflict_ref="ET7",
                    committed_date="2026-03-31",
                    org=ELECTRIC,
                ),
            ),
            # Excluded: commitment with no date of either kind.
            _candidate(
                minutes,
                kind="event",
                fields=_event_fields(
                    event_type="commitment", conflict_ref="PL1"
                ),
            ),
            # Excluded: dated event of a type outside the rule.
            _candidate(
                minutes,
                kind="event",
                fields=_event_fields(
                    event_type="response",
                    conflict_ref="PL1",
                    event_date="2025-01-16",
                ),
            ),
            # Excluded: dated commitment whose reference matches nothing.
            _candidate(
                minutes,
                kind="event",
                fields=_event_fields(
                    event_type="commitment",
                    conflict_ref="PL99",
                    event_date="2025-01-16",
                ),
            ),
            # Excluded: dated commitment with no reference at all.
            _candidate(
                minutes,
                kind="event",
                fields=_event_fields(
                    event_type="commitment",
                    conflict_ref=None,
                    event_date="2025-01-16",
                ),
            ),
        ],
    )
    return {
        "matrix": matrix,
        "matrix_run": matrix_run,
        "minutes": minutes,
        "minutes_run": minutes_run,
    }


def _declare_all(session, project):
    return declare_single_run_documents(
        session, project.id, principal=DECLARER
    )


# ── Bulk declaration ─────────────────────────────────────────────────────


def test_bulk_declaration_declares_every_single_run_document(
    session, project, corpus
):
    declared = _declare_all(session, project)
    assert len(declared) == 2

    for run in (corpus["matrix_run"], corpus["minutes_run"]):
        active = session.get(ActiveExtractionRun, run.document_id)
        assert active is not None
        assert active.extraction_run_id == run.id

    # An identical rerun declares nothing new — no duplicated outcomes.
    assert _declare_all(session, project) == []


def test_bulk_declaration_refuses_a_document_with_two_runs(
    session, project, corpus
):
    second = _run(
        session,
        corpus["matrix"],
        [_candidate(corpus["matrix"], kind="dependency", fields=_dep_fields("PL1"))],
    )
    assert second.id != corpus["matrix_run"].id

    with pytest.raises(MultipleRunsNeedExplicitChoice) as excinfo:
        _declare_all(session, project)
    assert corpus["matrix"].registry_id in str(excinfo.value)

    # All-or-nothing: the refusal declared nothing, not even the
    # single-run minutes document.
    assert (
        session.get(ActiveExtractionRun, corpus["minutes"].id) is None
    )


def test_bulk_declaration_cli_declares_and_reports(
    session, project, corpus, capsys, monkeypatch
):
    from corridor import extraction_runs
    from corridor.config import settings

    monkeypatch.setattr(settings, "human_principal", "local:operator")

    class ScopedSession:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            session.close()
            return False

    assert (
        extraction_runs.main(
            ["--single-run-documents", project.slug],
            session_factory=ScopedSession,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "2 declaration(s)" in out
    assert "declared by local:operator" in out


# ── Derivation ───────────────────────────────────────────────────────────


def test_derivation_refuses_undeclared_work(session, project, corpus):
    with pytest.raises(CohortDerivationError) as excinfo:
        derive_event_cohort_receipt(session, project.id)
    assert "declared" in str(excinfo.value)


def test_members_are_a_pure_function_of_declared_runs(
    session, project, corpus
):
    _declare_all(session, project)
    receipt = derive_event_cohort_receipt(session, project.id)

    assert receipt.rule_version == EVENT_COHORT_RULE_VERSION
    assert receipt.member_count == 2
    assert receipt.members == [
        {
            "conflict_ref": "ET7",
            "external_org": ELECTRIC,
            "dated_event_count": 1,
        },
        {
            "conflict_ref": "PL1",
            "external_org": PIPELINE,
            "dated_event_count": 1,
        },
    ]
    assert sorted(receipt.input_run_ids) == sorted(
        [corpus["matrix_run"].id, corpus["minutes_run"].id]
    )

    again = derive_event_cohort_receipt(session, project.id)
    assert again.id == receipt.id
    assert again.content_sha256 == receipt.content_sha256


def test_existing_receipt_disagreement_refuses(session, project, corpus):
    _declare_all(session, project)
    receipt = derive_event_cohort_receipt(session, project.id)

    session.execute(
        update(EventCohortReceipt)
        .where(EventCohortReceipt.id == receipt.id)
        .values(members=receipt.members[:1], member_count=1)
    )
    session.expire_all()

    with pytest.raises(CohortDerivationError) as excinfo:
        derive_event_cohort_receipt(session, project.id)
    assert "version" in str(excinfo.value)


# ── The mutation boundary ────────────────────────────────────────────────


def test_mutation_outside_the_boundary_refuses(session, project, corpus):
    _declare_all(session, project)
    receipt = derive_event_cohort_receipt(session, project.id)

    ids = event_cohort_candidate_ids(session, receipt)
    member_events = session.scalars(
        select(Candidate).where(
            Candidate.kind == "event",
            Candidate.project_id == project.id,
        )
    ).all()
    inside = [c for c in member_events if c.id in ids]
    outside = [
        c
        for c in member_events
        if (c.payload_json["fields"].get("conflict_ref")) == "PL99"
    ]
    assert inside and outside

    assert (
        require_event_cohort_member(session, receipt.id, inside[0].id).id
        == receipt.id
    )
    with pytest.raises(CohortScopeViolation):
        require_event_cohort_member(session, receipt.id, outside[0].id)
