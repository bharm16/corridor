"""Public, deterministic statement-scope suggestion behavior."""

from datetime import date
import hashlib

import pytest
from sqlalchemy import select

from corridor.models import (
    Candidate,
    Dependency,
    DependencyEvent,
    Document,
    EvidenceInvestigationShadowCase,
    ExternalOrg,
    Project,
)
from corridor.statement_suggestions import (
    declare_statement_suggestion_eligibility,
    declare_statement_suggestion_protection,
    end_statement_suggestion_protection,
    read_statement_suggestions,
)


def _statement_with_constraint(session):
    project = Project(
        slug="statement-suggestions", name="Statement Suggestions", is_synthetic=True
    )
    session.add(project)
    session.flush()
    org = ExternalOrg(name="Kinder Morgan")
    session.add(org)
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"statement-suggestions").hexdigest(),
        filename="minutes.pdf",
        doc_type="minutes",
        doc_date=date(2026, 5, 4),
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "description": "Kinder Morgan will relocate the gas main near Station 6609+00.",
                "external_org": "Kinder Morgan",
                "station_from": "6609+00",
                "station_to": "6609+00",
                "conflict_ref": "KM-17",
            },
            "citations": [],
        },
        source_document_id=document.id,
        source_pages=[1],
        citations_verified=True,
    )
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00017",
        source_ref="KM-17",
        dep_type="utility_relocation",
        title="Relocate Kinder Morgan gas main",
        location_desc="Station 6609+00 crossing",
        station_from="6608+50",
        station_to="6609+50",
        external_org_id=org.id,
    )
    session.add_all((candidate, dependency))
    session.flush()
    return project, candidate, dependency


def test_suggestions_require_explicit_eligibility_and_never_select_scope(session):
    project, candidate, dependency = _statement_with_constraint(session)

    assert read_statement_suggestions(session, project.id, candidate.id) == ()

    declare_statement_suggestion_eligibility(session, project.id, candidate.id)
    suggestions = read_statement_suggestions(session, project.id, candidate.id)

    assert [item.dependency_id for item in suggestions] == [dependency.id]
    assert suggestions[0].signals == (
        "registered_party_match",
        "explicit_constraint_reference",
        "station_overlap",
        "source_term_match",
    )
    assert suggestions[0].selected is False


def test_protection_wins_over_eligibility_until_every_declared_window_has_ended(session):
    project, candidate, _dependency = _statement_with_constraint(session)
    declare_statement_suggestion_eligibility(session, project.id, candidate.id)
    first = declare_statement_suggestion_protection(
        session,
        project.id,
        candidate.id,
        kind="no_agent_baseline",
        observation_contract="review-and-correction-window-v1",
    )
    second = declare_statement_suggestion_protection(
        session,
        project.id,
        candidate.id,
        kind="no_agent_baseline",
        observation_contract="correction-observation-window-v1",
    )

    assert read_statement_suggestions(session, project.id, candidate.id) == ()

    end_statement_suggestion_protection(session, first.id)
    assert read_statement_suggestions(session, project.id, candidate.id) == ()

    end_statement_suggestion_protection(session, second.id)
    assert read_statement_suggestions(session, project.id, candidate.id)


def test_existing_shadow_history_cannot_be_declared_ordinary_after_the_fact(session):
    project, candidate, _dependency = _statement_with_constraint(session)
    shadow = EvidenceInvestigationShadowCase(
        public_id="0" * 36,
        project_id=project.id,
        candidate_id=candidate.id,
        extraction_run_id=None,
        candidate_payload_sha256="a" * 64,
        read_fingerprint="b" * 64,
        model="test-model",
        prompt_version="test-prompt",
        prompt_sha256="c" * 64,
        adapter_contract_version="test-adapter",
        tool_contract_version="test-tools",
        transport_gate_sha256="d" * 64,
        budget_json={},
        case_json={},
        registered_evidence_json=[],
        option_population_json={},
        option_population_sha256="e" * 64,
        frozen_at=date(2026, 5, 4),
    )
    session.add(shadow)
    session.flush()

    with pytest.raises(ValueError, match="before review or a human outcome"):
        declare_statement_suggestion_eligibility(session, project.id, candidate.id)
    assert read_statement_suggestions(session, project.id, candidate.id) == ()


def test_a_shadow_cohort_window_withholds_and_cannot_be_explicitly_ended(session):
    project, candidate, _dependency = _statement_with_constraint(session)
    declare_statement_suggestion_eligibility(session, project.id, candidate.id)
    protection = declare_statement_suggestion_protection(
        session,
        project.id,
        candidate.id,
        kind="shadow_cohort",
        observation_contract="frozen-v2-membership",
    )

    assert read_statement_suggestions(session, project.id, candidate.id) == ()
    with pytest.raises(ValueError, match="immutable frozen membership"):
        end_statement_suggestion_protection(session, protection.id)
    assert read_statement_suggestions(session, project.id, candidate.id) == ()


def test_reading_suggestions_writes_nothing_and_repeats_deterministically(session):
    project, candidate, _dependency = _statement_with_constraint(session)
    declare_statement_suggestion_eligibility(session, project.id, candidate.id)

    first = read_statement_suggestions(session, project.id, candidate.id)
    second = read_statement_suggestions(session, project.id, candidate.id)

    assert first == second
    assert candidate.state == "pending"
    assert session.scalars(select(DependencyEvent)).all() == []


def test_a_changed_current_context_withdraws_the_read(session):
    project, candidate, _dependency = _statement_with_constraint(session)
    declare_statement_suggestion_eligibility(session, project.id, candidate.id)
    assert read_statement_suggestions(session, project.id, candidate.id)

    candidate.state = "accepted"
    session.flush()

    with pytest.raises(ValueError, match="current eligible Candidate"):
        read_statement_suggestions(session, project.id, candidate.id)


def test_suggestions_refuse_cross_project_candidate_references(session):
    project, candidate, _dependency = _statement_with_constraint(session)
    other = Project(slug="other-suggestions", name="Other Suggestions", is_synthetic=True)
    session.add(other)
    session.flush()

    with pytest.raises(ValueError, match="current eligible Candidate"):
        read_statement_suggestions(session, other.id, candidate.id)
