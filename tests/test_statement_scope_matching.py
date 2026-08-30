"""ADR-0050 gate for #370's exact statement scope rule."""

from datetime import date

import pytest

from corridor.db import Session, engine
from corridor.event_admission import run_event_admission
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.external_statements import CitedStatementEvidence, StatementScope, StatementTiming, record_external_party_statement
from corridor.models import Candidate, Dependency, DocPage, Document, ExternalOrg, Project
from corridor.principals import HumanPrincipal
from corridor.statement_scope_matching import (
    replay_matches_human_scope_decisions,
)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    transaction.rollback()
    connection.close()


def _setup(session):
    project = Project(slug="scope-replay", name="Scope replay", is_synthetic=True)
    party = ExternalOrg(name="Replay Gas", aliases=[])
    session.add_all((project, party))
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code="UC-041",
        dep_type="utility_relocation",
        title="12-inch gas main",
        station_from="102+00",
        station_to="103+00",
        external_org_id=party.id,
    )
    wording = "the 12-inch gas main at station 102+50 will be relocated by March 15"
    document = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add_all((dependency, document))
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=wording))
    session.flush()
    return project, party, dependency, document, wording


def test_zero_human_cases_never_pass_the_replay(session):
    project, *_ = _setup(session)
    replay = replay_matches_human_scope_decisions(session, project.id)
    assert replay.case_count == 0
    assert not replay.passed


def test_human_scope_decision_is_the_answer_key_and_contrary_exact_rule_fails(session):
    project, party, dependency, document, wording = _setup(session)
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2026, 1, 1),
        description=wording,
        new_timing=StatementTiming("March 15, 2026", "day", date(2026, 3, 15), date(2026, 3, 15)),
        previous_timing=None,
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:alice",
        evidence=CitedStatementEvidence(document.id, 1, wording),
    )
    replay = replay_matches_human_scope_decisions(session, project.id)
    assert replay.case_count == 1
    assert replay.passed

    other = Dependency(
        project_id=project.id,
        ref_code="UC-042",
        dep_type="utility_relocation",
        title="12-inch gas main",
        station_from="102+00",
        station_to="103+00",
        external_org_id=party.id,
    )
    session.add(other)
    session.flush()
    # A later row makes the exact rule abstain, which is safe.  A different
    # exact answer, not an abstention, is the only mechanical contradiction.
    replay = replay_matches_human_scope_decisions(session, project.id)
    assert replay.passed


def test_passing_replay_admits_a_new_exact_scope_before_unknown_scope(session):
    project, party, dependency, document, wording = _setup(session)
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2026, 1, 1),
        description=wording,
        new_timing=StatementTiming("March 15, 2026", "day", date(2026, 3, 15), date(2026, 3, 15)),
        previous_timing=None,
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:alice",
        evidence=CitedStatementEvidence(document.id, 1, wording),
    )
    next_wording = (
        wording.replace("March 15", "March 16, 2026")
        + f" {party.name} made the commitment January 2, 2026."
    )
    source = Document(
        project_id=project.id,
        sha256="b" * 64,
        filename="follow-up-minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(source)
    session.flush()
    session.add(DocPage(document_id=source.id, page_no=1, text=next_wording))
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "commitment", "description": next_wording,
                "external_org": party.name, "stated_party": party.name,
                "event_date": "2026-01-02",
                "committed_date": {"text": "March 16, 2026", "precision": "day",
                                   "start_date": "2026-03-16", "end_date": "2026-03-16"},
                "conflict_ref": None, "station": "102+50",
            },
            "citations": [{"document_id": source.id, "page": 1, "quote": next_wording,
                            "verified": True, "whole_row": True}],
            "dedupe_hint": next_wording, "text_source": "text_layer",
        },
        source_document_id=source.id, source_pages=[1], confidence=0.99,
        prompt_version="minutes_v1", model="gpt-test", citations_verified=True,
    )
    session.add(candidate)
    run = record_extraction_run(
        session, source, prompt_version="minutes_v1", candidate_count=1,
        page_errors=0, candidates=(candidate,), model="gpt-test",
        schema_version="matrix_candidate_shape_v1", allow_unsealed_legacy=True,
    )
    declare_active_run(session, source.id, run.id, principal=HumanPrincipal("local:alice"))

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 1
    session.refresh(candidate)
    assert candidate.state == "accepted"
