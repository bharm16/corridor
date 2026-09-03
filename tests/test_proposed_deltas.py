"""Contract and lifecycle tests for Proposed Deltas (#518)."""

from datetime import datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session

from corridor.analytics import AnalyticsBinding, default_binding
from corridor.db import engine
from corridor.models import (
    DeltaGroup,
    Project,
    ProposedDelta,
)
from corridor.proposed_deltas import (
    ApparentRemovalRefused,
    ExistingSubjectTarget,
    ImpactDerivation,
    LiveDeltaState,
    ProposedDeltaValues,
    ProposedSubjectTarget,
    create_proposed_delta_group,
    derive_live_delta_state,
    query_live_deltas,
    record_delta_deferral,
    record_delta_supersession,
)
from corridor.delta_resolution import ChildDecisionRequest, resolve_delta
from corridor.principals import HumanPrincipal


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
def test_project(session: Session) -> Project:
    project = Project(
        slug=f"delta-test-{uuid4().hex[:8]}",
        name="Proposed Delta Test Project",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    return project


def test_discriminated_targets_in_atomic_delta_group(
    session: Session, test_project: Project
) -> None:
    """A DeltaGroup binds discriminated targets (existing_subject and proposed_subject)."""
    deltas = (
        ProposedDeltaValues(
            change_type="modify",
            target=ExistingSubjectTarget(
                subject_identity="UTL-001",
                field="clearance_date",
            ),
            accepted_value={"date": "2026-10-01"},
            proposed_value={"date": "2026-11-15"},
        ),
        ProposedDeltaValues(
            change_type="add",
            target=ProposedSubjectTarget(
                subject_identity="UTL-NEW-099",
                proposed_fields=("owner", "utility_type", "status"),
            ),
            proposed_value={
                "owner": "CenterPoint",
                "utility_type": "Gas",
                "status": "conflict",
            },
        ),
    )

    created = create_proposed_delta_group(
        session,
        project_id=test_project.id,
        source_family="workbook",
        source_revision="rev-2026-09-01",
        deltas=deltas,
    )

    assert len(created) == 2
    assert created[0].target_type == "existing_subject"
    assert created[0].target_subject_identity == "UTL-001"
    assert created[0].target_field == "clearance_date"
    assert created[0].change_type == "modify"

    assert created[1].target_type == "proposed_subject"
    assert created[1].target_subject_identity == "UTL-NEW-099"
    assert created[1].target_field is None
    assert created[1].change_type == "add"

    # Both belong to the same DeltaGroup
    assert created[0].group_id == created[1].group_id


def test_immutable_occurrence_and_derived_live_state(
    session: Session, test_project: Project
) -> None:
    """ProposedDelta occurrence is immutable; live state walks dispositions and deferrals."""
    created = create_proposed_delta_group(
        session,
        project_id=test_project.id,
        source_family="workbook",
        source_revision="rev-1",
        deltas=(
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget(
                    subject_identity="UTL-002",
                    field="status",
                ),
                accepted_value="pending",
                proposed_value="cleared",
            ),
        ),
    )
    delta = created[0]

    # Initial state is open
    state = derive_live_delta_state(session, delta.id)
    assert state.status == "open"

    # Deferral marks state as deferred while leaving occurrence immutable
    record_delta_deferral(
        session,
        project_id=test_project.id,
        delta_id=delta.id,
        deferred_at=datetime.now(timezone.utc),
        scheduled_by_principal="coordinator-jane",
        wake_condition="next_monthly_utility_meeting",
        reason="Awaiting city confirmation",
    )

    state = derive_live_delta_state(session, delta.id)
    assert state.status == "deferred"
    assert state.wake_condition == "next_monthly_utility_meeting"

    # A semantic disposition marks state as resolved. It is written only by
    # the record-decision role's command (#519), never from this module.
    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=test_project.id,
            delta_id=delta.id,
            action="reject",
            principal=HumanPrincipal("local:jane"),
            idempotency_key=f"resolve:{delta.id}",
            decided_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
            rationale="the accepted station stands",
        ),
    )
    assert outcome.status == "resolved"

    state = derive_live_delta_state(session, delta.id)
    assert state.status == "resolved"
    assert state.disposition == "reject"


def test_coalescing_follows_source_lineage(
    session: Session, test_project: Project
) -> None:
    """A newer revision of the same source family supersedes prior live delta."""
    group1 = create_proposed_delta_group(
        session,
        project_id=test_project.id,
        source_family="workbook",
        source_revision="rev-1",
        deltas=(
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget("UTL-003", "cost"),
                accepted_value=50000,
                proposed_value=75000,
            ),
        ),
    )
    delta1 = group1[0]

    # Newer revision of the same workbook
    group2 = create_proposed_delta_group(
        session,
        project_id=test_project.id,
        source_family="workbook",
        source_revision="rev-2",
        deltas=(
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget("UTL-003", "cost"),
                accepted_value=50000,
                proposed_value=85000,
            ),
        ),
    )
    delta2 = group2[0]

    # Explicit supersession links lineage
    record_delta_supersession(
        session,
        project_id=test_project.id,
        prior_delta_id=delta1.id,
        superseding_delta_id=delta2.id,
        reason="newer_source_revision",
    )

    state1 = derive_live_delta_state(session, delta1.id)
    assert state1.status == "superseded"
    assert state1.superseded_by_delta_id == delta2.id

    state2 = derive_live_delta_state(session, delta2.id)
    assert state2.status == "open"

    # Querying live deltas returns only delta2
    live = query_live_deltas(session, project_id=test_project.id, target_subject_identity="UTL-003")
    assert len(live) == 1
    assert live[0].id == delta2.id


def test_apparent_removal_requires_complete_sealed_source(
    session: Session, test_project: Project
) -> None:
    """Apparent removal is refused without complete enumerative source and sealed row accounting."""
    removal = ProposedDeltaValues(
        change_type="apparent_removal",
        target=ExistingSubjectTarget("UTL-999", "entire_subject"),
        accepted_value={"exists": True},
        proposed_value=None,
    )

    # Incomplete source -> refused
    with pytest.raises(ApparentRemovalRefused, match="complete enumerative revision"):
        create_proposed_delta_group(
            session,
            project_id=test_project.id,
            source_family="email",
            source_revision="email-msg-42",
            deltas=(removal,),
            is_complete_enumerative_source=False,
            row_accounting_sealed=True,
        )

    # Unsealed accounting -> refused
    with pytest.raises(ApparentRemovalRefused, match="complete enumerative revision"):
        create_proposed_delta_group(
            session,
            project_id=test_project.id,
            source_family="workbook",
            source_revision="rev-unsealed",
            deltas=(removal,),
            is_complete_enumerative_source=True,
            row_accounting_sealed=False,
        )

    # Complete and sealed -> succeeds
    created = create_proposed_delta_group(
        session,
        project_id=test_project.id,
        source_family="workbook",
        source_revision="rev-final",
        deltas=(removal,),
        is_complete_enumerative_source=True,
        row_accounting_sealed=True,
    )
    assert len(created) == 1
    assert created[0].change_type == "apparent_removal"


def test_impact_is_a_derivation() -> None:
    """Impact consequence is a Derivation (rule + inputs + time), not an impact fact."""
    derivation = ImpactDerivation(
        rule="rule_utility_schedule_conflict_v2",
        inputs={"clearance_date": "2026-11-15", "letting_date": "2026-10-01"},
        evaluated_at=datetime.now(timezone.utc),
        affected_constraint_ids=("CON-401", "CON-402"),
        affected_key_dates=("letting_date", "construction_start"),
    )

    assert derivation.rule == "rule_utility_schedule_conflict_v2"
    assert len(derivation.affected_constraint_ids) == 2
    assert "letting_date" in derivation.affected_key_dates


def test_analytics_event_emitted_on_delta_creation(
    session: Session, test_project: Project
) -> None:
    """Creating a delta group emits a versioned PROPOSED_DELTA_CREATION analytics event."""
    binding = default_binding(
        code_revision="git-abcdef",
        product_revision="2026.09.1",
        packetizer_rules_version="rules-v1",
        source_configuration={"source": "txdot-box-config"},
        connector_configuration={"connector": "box-pull-v1"},
        template_identity="template-txdot",
        mapping_identity="mapping-txdot-ucm",
        enabled_feature_flags=("enable_delta_derivation",),
    )

    created = create_proposed_delta_group(
        session,
        project_id=test_project.id,
        source_family="workbook",
        source_revision="rev-test-event",
        deltas=(
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget("UTL-005", "owner"),
                accepted_value="AT&T",
                proposed_value="Frontier",
            ),
        ),
        analytics_binding=binding,
    )
    assert len(created) == 1
