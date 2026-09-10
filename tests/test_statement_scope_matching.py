"""ADR-0050 gate and admission wiring for #370's exact statement scope rule."""

from datetime import date

from sqlalchemy import select

from corridor.event_admission import _run_unknown_scope_admission, run_event_admission
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import (
    Candidate,
    Dependency,
    DependencyEvent,
    DocPage,
    Document,
    EventAdmissionOutcome,
    ExternalOrg,
    PolicyRun,
    Project,
)
from corridor.principals import HumanPrincipal
from corridor.statement_scope_matching import (
    NARROWED_SET_REASON,
    POLICY_VERSION,
    replay_matches_human_scope_decisions,
)


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


def _record_answer_key(session, project, party, dependency, document, wording, *, created_by="local:alice"):
    return record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2026, 1, 1),
        description=wording,
        new_timing=StatementTiming(
            "March 15, 2026", "day", date(2026, 3, 15), date(2026, 3, 15)
        ),
        previous_timing=None,
        scope=StatementScope.selected((dependency.id,)),
        created_by=created_by,
        evidence=CitedStatementEvidence(document.id, 1, wording),
    )


def _event_candidate(session, project, party, wording, *, station=None, sha, filename):
    source = Document(
        project_id=project.id,
        sha256=sha,
        filename=filename,
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(source)
    session.flush()
    session.add(DocPage(document_id=source.id, page_no=1, text=wording))
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "commitment",
                "description": wording,
                "external_org": party.name,
                "stated_party": party.name,
                "event_date": "2026-01-02",
                "committed_date": {
                    "text": "March 16, 2026",
                    "precision": "day",
                    "start_date": "2026-03-16",
                    "end_date": "2026-03-16",
                },
                "conflict_ref": None,
                "station": station,
            },
            "citations": [
                {
                    "document_id": source.id,
                    "page": 1,
                    "quote": wording,
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": wording,
            "text_source": "text_layer",
        },
        source_document_id=source.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="minutes_v1",
        model="gpt-test",
        citations_verified=True,
    )
    session.add(candidate)
    run = record_extraction_run(
        session,
        source,
        prompt_version="minutes_v1",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="gpt-test",
        schema_version="matrix_candidate_shape_v1",
        allow_unsealed_legacy=True,
    )
    declare_active_run(
        session, source.id, run.id, principal=HumanPrincipal("local:alice")
    )
    return candidate


def test_zero_human_cases_never_pass_the_replay(session):
    project, *_ = _setup(session)
    replay = replay_matches_human_scope_decisions(session, project.id)
    assert replay.case_count == 0
    assert not replay.passed


def test_machine_selected_scopes_are_not_the_answer_key(session):
    project, party, dependency, document, wording = _setup(session)
    _record_answer_key(
        session,
        project,
        party,
        dependency,
        document,
        wording,
        created_by="corridor:event-admission",
    )
    replay = replay_matches_human_scope_decisions(session, project.id)
    assert replay.case_count == 0
    assert not replay.passed


def test_a_contrary_human_decision_fails_the_replay(session):
    project, party, dependency, document, wording = _setup(session)
    other = Dependency(
        project_id=project.id,
        ref_code="UC-050",
        dep_type="utility_relocation",
        title="water line",
        external_org_id=party.id,
    )
    session.add(other)
    session.flush()
    # The person recorded the gas-main statement against the water line —
    # people know things documents do not.  The stack would answer the gas
    # main exactly; a rule that would overwrite a human's contrary decision
    # fails mechanically (ADR-0050).
    event = _record_answer_key(session, project, party, other, document, wording)
    replay = replay_matches_human_scope_decisions(session, project.id)
    assert replay.case_count == 1
    assert replay.contradictions == (event.id,)
    assert not replay.passed


def test_an_abstention_on_a_human_case_is_not_a_contradiction(session):
    project, party, dependency, document, wording = _setup(session)
    _record_answer_key(session, project, party, dependency, document, wording)
    twin = Dependency(
        project_id=project.id,
        ref_code="UC-042",
        dep_type="utility_relocation",
        title="12-inch gas main",
        station_from="102+00",
        station_to="103+00",
        external_org_id=party.id,
    )
    session.add(twin)
    session.flush()
    # A later identical row makes the stack abstain on the recorded case.
    # Abstaining where a person decided is safe; only a different exact
    # answer contradicts.
    replay = replay_matches_human_scope_decisions(session, project.id)
    assert replay.case_count == 1
    assert replay.passed


def test_passing_replay_admits_an_exact_scope_onto_the_promise_chain(session):
    project, party, dependency, document, wording = _setup(session)
    answer_key = _record_answer_key(
        session, project, party, dependency, document, wording
    )
    next_wording = (
        wording.replace("March 15", "March 16, 2026")
        + f" {party.name} made the commitment January 2, 2026."
    )
    candidate = _event_candidate(
        session,
        project,
        party,
        next_wording,
        station="102+50",
        sha="b" * 64,
        filename="follow-up-minutes.pdf",
    )

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 1
    session.refresh(candidate)
    assert candidate.state == "accepted"
    outcome = session.scalars(
        select(EventAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.policy_version == POLICY_VERSION,
            EventAdmissionOutcome.outcome == "admitted",
            EventAdmissionOutcome.candidate_id == candidate.id,
        )
    ).one()
    event = session.get(DependencyEvent, outcome.dependency_event_id)
    # A timing update to an existing Commitment records on that chain as a
    # Change to Promised Timing, never as a new unplaced statement.
    assert event.event_type == "committed_date_change"
    assert event.commitment_lineage_id == answer_key.commitment_lineage_id
    assert event.created_by == "corridor:statement-scope-matcher"
    match_evidence = outcome.eligibility_json["match"]
    assert match_evidence["station"] == "102+50"
    assert "12 inch gas main" in match_evidence["matched_terms"]
    assert match_evidence["matcher_fingerprint"]

    # Re-running admission with unchanged inputs writes nothing new.
    runs_before = session.scalars(
        select(PolicyRun.id).where(PolicyRun.policy_version == POLICY_VERSION)
    ).all()
    run_event_admission(session, project.id)
    runs_after = session.scalars(
        select(PolicyRun.id).where(PolicyRun.policy_version == POLICY_VERSION)
    ).all()
    assert runs_before == runs_after


def test_narrowed_set_abstains_with_card_and_stays_visibly_pending(session):
    project, party, dependency, document, wording = _setup(session)
    _record_answer_key(session, project, party, dependency, document, wording)
    first_water = Dependency(
        project_id=project.id,
        ref_code="UC-050",
        dep_type="utility_relocation",
        title="water line",
        external_org_id=party.id,
    )
    second_water = Dependency(
        project_id=project.id,
        ref_code="UC-051",
        dep_type="utility_relocation",
        title="water line",
        external_org_id=party.id,
    )
    session.add_all((first_water, second_water))
    session.flush()
    ambiguous_wording = (
        f"{party.name} will relocate the water line by March 16, 2026."
    )
    candidate = _event_candidate(
        session,
        project,
        party,
        ambiguous_wording,
        sha="c" * 64,
        filename="water-minutes.pdf",
    )

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    session.refresh(candidate)
    assert candidate.state == "pending"
    outcome = session.scalars(
        select(EventAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.policy_version == POLICY_VERSION,
            EventAdmissionOutcome.candidate_id == candidate.id,
        )
    ).one()
    assert outcome.outcome == "abstained"
    assert outcome.reason == NARROWED_SET_REASON
    card = outcome.eligibility_json["card"]
    assert card["candidate_dependency_ids"] == [first_water.id, second_water.id]
    assert card["choice_modes"] == ["each", "both_all_listed"]
    assert card["matched_details"] == ["water line"]
    assert card["evidence_applied"]["matcher_fingerprint"]

    # Prior-abstention receipts dedupe: a re-run with unchanged inputs
    # writes no second receipt and no second run.
    outcomes_before = session.scalars(select(EventAdmissionOutcome.id)).all()
    runs_before = session.scalars(
        select(PolicyRun.id).where(PolicyRun.policy_version == POLICY_VERSION)
    ).all()
    run_event_admission(session, project.id)
    outcomes_after = session.scalars(select(EventAdmissionOutcome.id)).all()
    runs_after = session.scalars(
        select(PolicyRun.id).where(PolicyRun.policy_version == POLICY_VERSION)
    ).all()
    assert outcomes_before == outcomes_after
    assert runs_before == runs_after

    # The unknown-scope extension leaves a withheld Candidate pending
    # rather than recording it with Applies To not yet known.
    extension = _run_unknown_scope_admission(
        session, project, withheld_candidate_ids=(candidate.id,)
    )
    assert extension.admitted_count == 0
    session.refresh(candidate)
    assert candidate.state == "pending"


def test_an_inactive_rule_writes_nothing_on_a_project_with_no_history(session):
    project, party, dependency, document, wording = _setup(session)
    candidate = _event_candidate(
        session,
        project,
        party,
        wording + f" {party.name} said so on January 2, 2026. March 16, 2026.",
        station="102+50",
        sha="d" * 64,
        filename="fresh-minutes.pdf",
    )

    run_event_admission(session, project.id)

    session.refresh(candidate)
    # No answer-key case exists, so the exact tier is inactive and the
    # Candidate flows to the existing policies unchanged.
    assert session.scalars(
        select(PolicyRun.id).where(PolicyRun.policy_version == POLICY_VERSION)
    ).all() == []
