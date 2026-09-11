"""Native coordination preserves history and becomes the migrated reader path."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conftest import rollback_scoped_session

from corridor.coordination_history import migrate_coordination_history, read_coordination_record, read_coordination_record_as_of_revision
from corridor.legacy_history import capture_history, inventory_history, reverse_history
from corridor.models import Dependency, Project, ProjectRosterEntry, WorkDecision
from corridor.principals import HumanPrincipal
from corridor.work_decisions import (
    FOLLOW_UP_NEXT_ACTION_CHOICES, FollowUpPlanDraft, assign_internal_owner,
    current_internal_owner_decision, save_follow_up_plan, undo_follow_up_plan,
)


@pytest.fixture
def session():
    """The shared rollback-scoped session, at READ COMMITTED."""

    with rollback_scoped_session(isolation_level="READ COMMITTED") as scoped:
        yield scoped


@pytest.fixture
def dependency(session, project):
    value = Dependency(project_id=project.id, ref_code="DEP-COORD", dep_type="utility_relocation", title="Utility conflict")
    session.add(value)
    session.flush()
    return value


def _batch(session, project, key="coordination-1"):
    return capture_history(session, inventory_history(session, project.id), run_key=key,
                           executor=session.scalar(text("select session_user")), code_revision="a" * 40)


def test_native_migration_keeps_original_actor_time_and_reads_without_copied_fields(session, project, dependency):
    first = WorkDecision(dependency_id=dependency.id, field="internal_owner", decision_type="assign_internal_owner",
        after_value="First owner", recorded_by="local:first", recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.add(first)
    session.flush()
    second = WorkDecision(dependency_id=dependency.id, field="internal_owner", decision_type="assign_internal_owner",
        before_value="First owner", after_value="Second owner", recorded_by="local:second",
        recorded_at=datetime(2026, 2, 1, tzinfo=timezone.utc), predecessor_decision_id=first.id)
    session.add(second)
    dependency.internal_owner = "Second owner"
    session.flush()
    batch = _batch(session, project)
    current = migrate_coordination_history(session, batch)
    assert [(row.value_text, row.recorded_by) for row in current] == [("Second owner", "local:second")]
    assert current[0].subject_id != str(dependency.id)
    assert read_coordination_record(session, project.id, at=datetime(2026, 1, 15, tzinfo=timezone.utc))[0].value_text == "First owner"
    assert migrate_coordination_history(session, batch) == current
    assert current_internal_owner_decision(session, dependency.id).native_decision_id == current[0].id
    dependency.internal_owner = "Forged copied field"
    session.flush()
    assert current_internal_owner_decision(session, dependency.id).after_value == "Second owner"
    dependency.internal_owner = "Second owner"
    session.flush()
    latest = assign_internal_owner(session, dependency.id, "Third owner", principal=HumanPrincipal("local:third"))
    assert read_coordination_record(session, project.id)[0].recorded_by == "local:third"
    assert current_internal_owner_decision(session, dependency.id).id == latest.id
    with pytest.raises(DBAPIError, match="immutable"), session.begin_nested():
        session.execute(text("update coordination_record_decisions set recorded_by='migration:operator' where project_id=:project"), {"project": project.id})
    reverse_history(session, batch, actor=session.scalar(text("select session_user")), reason="rollback rehearsal")
    assert read_coordination_record(session, project.id) == ()
    assert current_internal_owner_decision(session, dependency.id).id == latest.id
    resumed = migrate_coordination_history(session, _batch(session, project, key="coordination-rehearsal-2"))
    assert resumed[0].subject_id == current[0].subject_id
    assert resumed[0].value_text == "Third owner"


def test_grouped_follow_up_save_and_undo_each_have_one_native_revision(session, project, dependency):
    roster = ProjectRosterEntry(project_id=project.id, principal_subject="local:owner", display_name="Owner")
    session.add(roster)
    session.flush()
    migrate_coordination_history(session, _batch(session, project))
    result = save_follow_up_plan(session, FollowUpPlanDraft(
        dependency_id=dependency.id, internal_owner_roster_entry_id=roster.id,
        next_action=FOLLOW_UP_NEXT_ACTION_CHOICES[0], action_due_date=date(2027, 1, 1),
        action_due_date_unknown_reason=None,
    ), principal=HumanPrincipal("local:coordinator"))
    current = read_coordination_record(session, project.id)
    assert {row.field for row in current} == {"internal_owner", "next_action"}
    assert len({row.revision_id for row in current}) == 1
    assert next(row for row in current if row.field == "next_action").action_due_date == date(2027, 1, 1)
    undo_follow_up_plan(session, result.receipt.id, principal=HumanPrincipal("local:coordinator"))
    undone = read_coordination_record(session, project.id)
    assert len({row.revision_id for row in undone}) == 1
    assert all(row.value_text is None for row in undone)
    retained = read_coordination_record_as_of_revision(session, project.id, current[0].revision_id)
    assert {row.field for row in retained} == {"internal_owner", "next_action"}
    assert all(row.value_text is not None for row in retained)
    other = Project(slug="other-native-coordination", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    with pytest.raises(ValueError, match="does not belong"):
        read_coordination_record_as_of_revision(session, other.id, current[0].revision_id)


@pytest.mark.parametrize("actor", ("reviewer", "corridor:automatic-carry-forward"))
def test_unattributed_legacy_actor_remains_explicit_compatibility_history(session, project, dependency, actor):
    from corridor.coordination_history import coordination_migration_gaps

    session.add(WorkDecision(dependency_id=dependency.id, field="internal_owner", decision_type="assign_internal_owner",
        after_value="Historical owner", recorded_by=actor, recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    dependency.internal_owner = "Historical owner"
    session.flush()
    batch = _batch(session, project)
    assert migrate_coordination_history(session, batch) == ()
    gap = coordination_migration_gaps(session, batch)[0]
    assert gap["original_actor"] == actor
    assert current_internal_owner_decision(session, dependency.id).recorded_by == actor
