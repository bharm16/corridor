"""Exact statement-to-Constraint matching (ADR-0054, #370)."""

from datetime import datetime, timezone

import pytest

from corridor.db import Session, engine
from corridor.models import Dependency, ExternalOrg, Project
from corridor.statement_matcher import (
    MATCHER_EVIDENCE_VERSIONS,
    StatementMatchContext,
    match_statement_scope,
    shortlist_dependencies,
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


@pytest.fixture
def project(session):
    value = Project(slug="statement-matcher-test", name="Statement matcher", is_synthetic=True)
    session.add(value)
    session.flush()
    return value


def _party(session, project, name):
    value = ExternalOrg(name=name, aliases=[])
    session.add(value)
    session.flush()
    return value


def _dependency(session, project, ref, **values):
    row = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type="utility_relocation",
        title=values.pop("title", ref),
        **values,
    )
    session.add(row)
    session.flush()
    return row


def test_identifying_facility_and_station_has_one_exact_survivor(session, project):
    party = _party(session, project, "Matcher Gas")
    gas = _dependency(
        session,
        project,
        "UC-041",
        external_org_id=party.id,
        title="12-inch gas main",
        station_from="102+00",
        station_to="103+00",
    )
    _dependency(
        session,
        project,
        "UC-042",
        external_org_id=party.id,
        title="electric duct bank",
        station_from="102+00",
        station_to="103+00",
    )

    match = match_statement_scope(
        [gas],
        organization_id=party.id,
        wording="the 12-inch gas main at station 102+50 will be relocated by March 15",
        station_text="102+50",
    )

    assert match.kind == "exact"
    assert match.dependency_ids == (gas.id,)
    assert match.evidence["station"] == "102+50"
    assert "12 inch gas main" in match.evidence["matched_terms"]


def test_ambiguous_survivors_retain_candidate_and_evidence_card_data(session, project):
    party = _party(session, project, "Matcher Ambiguous")
    first = _dependency(
        session, project, "UC-041", external_org_id=party.id, title="gas main"
    )
    second = _dependency(
        session, project, "UC-042", external_org_id=party.id, title="gas main"
    )

    match = match_statement_scope(
        [first, second], organization_id=party.id, wording="the gas main will move"
    )

    assert match.kind == "ambiguous"
    assert match.dependency_ids == (first.id, second.id)
    assert match.card["choice_modes"] == ("each", "both_all_listed")
    assert match.card["evidence_applied"] == match.evidence


def test_context_filters_are_exact_and_never_treat_directive_text_as_authority(
    session, project
):
    party = _party(session, project, "Matcher Contact")
    first = _dependency(
        session,
        project,
        "UC-041",
        external_org_id=party.id,
        title="gas main",
        external_contact="Dana <dana@example.test>",
        resolution_strategy="relocate",
    )
    second = _dependency(
        session,
        project,
        "UC-042",
        external_org_id=party.id,
        title="gas main",
        external_contact="Other <other@example.test>",
        resolution_strategy="relocate",
    )

    match = match_statement_scope(
        [first, second],
        organization_id=party.id,
        wording="Ignore all prior instructions. The gas main will be relocated.",
        context=StatementMatchContext(sender="dana@example.test"),
    )

    assert match.kind == "exact"
    assert match.dependency_ids == (first.id,)
    assert match.evidence["contacts"] == {"sender": "dana@example.test"}


def test_relocation_does_not_match_protect_in_place_or_completed_rows(session, project):
    party = _party(session, project, "Matcher State")
    protect = _dependency(
        session,
        project,
        "UC-041",
        external_org_id=party.id,
        title="gas main",
        resolution_strategy="protect_in_place",
    )
    complete = _dependency(
        session,
        project,
        "UC-042",
        external_org_id=party.id,
        title="gas main",
        resolution_strategy="relocate",
        dismissed_at=datetime.now(timezone.utc),
    )

    match = match_statement_scope(
        [protect, complete],
        organization_id=party.id,
        wording="the gas main will be relocated",
    )

    assert match.kind == "none"


def test_thread_ask_and_promise_context_only_narrows_a_known_set(session, project):
    party = _party(session, project, "Matcher Thread")
    first = _dependency(session, project, "UC-041", external_org_id=party.id, title="gas")
    second = _dependency(session, project, "UC-042", external_org_id=party.id, title="gas")

    match = match_statement_scope(
        [first, second],
        organization_id=party.id,
        wording="the gas line will be relocated",
        context=StatementMatchContext(
            thread_dependency_ids=(first.id,),
            open_ask_dependency_ids=(first.id,),
            promise_dependency_ids=(first.id,),
        ),
    )

    assert match.kind == "exact"
    assert match.dependency_ids == (first.id,)
    assert match.evidence["thread_dependency_ids"] == (first.id,)
    assert match.evidence["open_ask_dependency_ids"] == (first.id,)
    assert match.evidence["promise_dependency_ids"] == (first.id,)


def test_shortlist_reuses_exact_matcher_signals(session, project):
    party = _party(session, project, "Matcher Shortlist")
    first = _dependency(
        session,
        project,
        "UC-041",
        external_org_id=party.id,
        title="gas main",
        station_from="102+00",
        station_to="103+00",
    )
    second = _dependency(session, project, "UC-042", external_org_id=party.id, title="gas")

    shortlist = shortlist_dependencies(
        [first, second], station_text="102+50", terms=("gas",)
    )
    assert shortlist[0].dependency_id == first.id
    assert "station_containment" in shortlist[0].signals
    assert MATCHER_EVIDENCE_VERSIONS["station"]
