"""Recording a verbal dual-writes it onto the spine (#451 stage 2).

The legacy DependencyEvent stays the source of truth until cutover (#458); each
verbal recording additionally appends the recorder's exact-words segment, the
typed statement_wording / statement_timing / applies_to Facts, and one
attributable Human Record Decision per Fact, all in the same atomic act.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from access_support import seed_membership
from corridor.current_record import read_current_project_record
from corridor.db import Session, engine
from corridor.external_statements import StatementScope, StatementTiming
from corridor.facts import (
    replay_recorded_applies_to_fact,
    replay_recorded_statement_timing_fact,
    replay_recorded_statement_wording_fact,
)
from corridor.models import (
    Dependency,
    ExternalOrg,
    Fact,
    FactDecision,
    Project,
    ProjectRecordRevision,
)
from corridor.principals import HumanPrincipal
from corridor.verbal import record_verbal, record_verbal_change, record_verbal_statement


RECORDER = HumanPrincipal("local:phone-coordinator")


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
def dependency(session):
    project = Project(
        slug="verbal-spine",
        name="Verbal Spine",
        is_synthetic=True,
        project_side_parties=["Project Engineer"],
    )
    party = ExternalOrg(name="AT&T Texas", aliases=["AT&T"])
    session.add_all((project, party))
    session.flush()
    seed_membership(session, project, RECORDER)
    dependency = Dependency(
        project_id=project.id,
        ref_code="TEL-1",
        dep_type="utility_relocation",
        title="Telecom conflict",
        external_org_id=party.id,
    )
    session.add(dependency)
    session.flush()
    return dependency


def _decisions(session, project_id, subject_key):
    return {
        decision.fact_type: decision
        for decision in session.scalars(
            select(FactDecision).where(
                FactDecision.project_id == project_id,
                FactDecision.subject_key == subject_key,
                FactDecision.superseded_by.is_(None),
            )
        ).all()
    }


def test_recording_a_verbal_writes_the_three_facts_and_decisions(session, dependency):
    event = record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T will relocate the line by June 15.",
        conversation_date=date(2025, 5, 1),
        committed_date=date(2025, 6, 15),
        principal=RECORDER,
    )
    subject_key = f"lineage:{event.commitment_lineage_id}"

    current = _decisions(session, dependency.project_id, subject_key)
    assert set(current) == {"statement_wording", "statement_timing", "applies_to"}
    for decision in current.values():
        revision = session.get(ProjectRecordRevision, decision.revision_id)
        assert revision.human_principal == "local:phone-coordinator"
        assert revision.released_policy is None
        assert revision.command_type == "record_verbal_statement"

    # The legacy event still stands beside the spine (dual-write, not replace).
    assert event.source_kind == "verbal"


def test_the_verbal_facts_replay_from_their_own_words(session, dependency):
    event = record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T will relocate the line by June 15.",
        conversation_date=date(2025, 5, 1),
        committed_date=date(2025, 6, 15),
        principal=RECORDER,
    )
    subject_key = f"lineage:{event.commitment_lineage_id}"
    current = _decisions(session, dependency.project_id, subject_key)

    wording = session.get(Fact, current["statement_wording"].fact_id)
    timing = session.get(Fact, current["statement_timing"].fact_id)
    applies = session.get(Fact, current["applies_to"].fact_id)

    assert (
        replay_recorded_statement_wording_fact(session, wording)
        == "AT&T will relocate the line by June 15."
    )
    replayed_timing = replay_recorded_statement_timing_fact(session, timing)
    assert replayed_timing.timings[0][1].start_date == date(2025, 6, 15)
    assert replay_recorded_applies_to_fact(session, applies).dependency_ids == (
        dependency.id,
    )


def test_the_current_record_shows_the_verbal_decisions(session, dependency):
    event = record_verbal(
        session,
        dependency,
        stated_party="AT&T",
        description="AT&T will relocate the line by June 15.",
        conversation_date=date(2025, 5, 1),
        committed_date=date(2025, 6, 15),
        principal=RECORDER,
    )
    subject_key = f"lineage:{event.commitment_lineage_id}"
    session.expire_all()

    current = read_current_project_record(session, dependency.project_id)
    verbal_facts = {
        value.fact_type for value in current if value.subject_key == subject_key
    }
    assert verbal_facts == {"statement_wording", "statement_timing", "applies_to"}


def test_a_stated_change_supersedes_wording_and_timing_but_keeps_scope(
    session, dependency
):
    first = record_verbal_statement(
        session,
        project_id=dependency.project_id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T will relocate by June.",
        conversation_date=date(2025, 5, 1),
        new_timing=StatementTiming.month("June 2025", 2025, 6),
        scope=StatementScope.selected((dependency.id,)),
        principal=RECORDER,
    )
    subject_key = f"lineage:{first.commitment_lineage_id}"
    before = _decisions(session, dependency.project_id, subject_key)

    record_verbal_change(
        session,
        commitment_lineage_id=first.commitment_lineage_id,
        stated_party="AT&T",
        description="AT&T now says July 10.",
        conversation_date=date(2025, 6, 2),
        new_timing=StatementTiming.day("July 10, 2025", date(2025, 7, 10)),
        principal=RECORDER,
    )
    session.expire_all()
    after = _decisions(session, dependency.project_id, subject_key)

    # Wording and timing changed, so their decisions were superseded.
    assert after["statement_wording"].id != before["statement_wording"].id
    assert after["statement_timing"].id != before["statement_timing"].id
    # The scope did not change, so no attributable Applies To no-op was recorded.
    assert after["applies_to"].id == before["applies_to"].id
    timing = session.get(Fact, after["statement_timing"].fact_id)
    replayed = replay_recorded_statement_timing_fact(session, timing)
    by_role = {role: value for role, value in replayed.timings}
    assert by_role["new"].start_date == date(2025, 7, 10)
    assert by_role["previous"].precision == "month"


def test_unknown_scope_records_an_explicit_empty_applies_to(session, dependency):
    event = record_verbal_statement(
        session,
        project_id=dependency.project_id,
        external_org_id=dependency.external_org_id,
        stated_party="AT&T",
        description="AT&T made a commitment; scope still unknown.",
        conversation_date=date(2025, 5, 1),
        new_timing=StatementTiming.approximate("later this summer"),
        scope=StatementScope.unknown(),
        principal=RECORDER,
    )
    subject_key = f"lineage:{event.commitment_lineage_id}"
    current = _decisions(session, dependency.project_id, subject_key)

    assert "applies_to" in current  # unknown scope is explicit, never omitted
    applies = session.get(Fact, current["applies_to"].fact_id)
    assert replay_recorded_applies_to_fact(session, applies).dependency_ids == ()
