"""ADR-0050's replay gate, asserted once for every policy family.

Four expansions used to carry their own copy of the comparison, the pass rule
and the activation ledger. These are the invariants that copy stated four times:
zero real cases never pass, an abstention is not a contradiction, a
contradiction fails, and the ledger's inactive/active/suspended transitions turn
on the policy family and the rule fingerprint.
"""

import pytest
from sqlalchemy import select

from corridor.models import (
    EventAdmissionActivation,
    OrganizationIdentityActivation,
    PolicyActivation,
    Project,
    ScheduleLinkActivation,
    UnreadableCellAdmissionActivation,
)
from corridor.replay_gate import (
    ABSTAINED,
    ACTIVE,
    FAMILY_EVENT_ADMISSION,
    FAMILY_ORGANIZATION_IDENTITY,
    FAMILY_SCHEDULE_LINK,
    FAMILY_UNREADABLE_CELL_ADMISSION,
    INACTIVE,
    SUSPENDED,
    ReplayGateRefusal,
    RuleFingerprint,
    activation_status,
    family_statuses,
    ledger_families,
    ledger_model,
    latest_ledger_entry,
    record_activation,
    record_suspension,
    replay,
)

FINGERPRINT = RuleFingerprint("schedule-conflict-link-v1", "a" * 64)
CHANGED = RuleFingerprint("schedule-conflict-link-v1", "b" * 64)


@pytest.fixture
def project(session):
    p = Project(slug="replay-gate", name="Replay Gate", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


# --- The comparison that is the test --------------------------------------- #


def test_zero_real_cases_never_pass():
    """A brand-new rule with no history waits for a real case (ADR-0050)."""
    outcome = replay(
        family=FAMILY_SCHEDULE_LINK,
        human_decisions=(),
        recompute=lambda case: pytest.fail("nothing to recompute"),
    )

    assert outcome.case_count == 0
    assert outcome.contradictions == ()
    assert outcome.passed is False


def test_a_rule_abstaining_is_not_a_contradiction():
    """A tie, or a case the rule no longer reaches, is not evidence against it."""
    outcome = replay(
        family=FAMILY_SCHEDULE_LINK,
        human_decisions=((7, 70), (8, 80)),
        recompute=lambda case: ABSTAINED if case == 8 else 70,
    )

    assert outcome.case_count == 2
    assert outcome.contradictions == ()
    assert outcome.abstentions == (8,)
    assert outcome.passed is True


def test_an_abstention_blocks_only_for_a_family_that_says_so():
    """Whole-row identity tiers claim to reach every case a person reached."""
    decisions = ((7, 70), (8, 80))

    def recompute(case):
        return ABSTAINED if case == 8 else 70

    assert replay(
        family=FAMILY_ORGANIZATION_IDENTITY,
        human_decisions=decisions,
        recompute=recompute,
        abstention_blocks=True,
    ).passed is False
    assert replay(
        family=FAMILY_ORGANIZATION_IDENTITY,
        human_decisions=decisions,
        recompute=recompute,
    ).passed is True


def test_a_contradiction_fails_the_replay():
    """The rule answering differently from the person blocks, mechanically."""
    outcome = replay(
        family=FAMILY_UNREADABLE_CELL_ADMISSION,
        human_decisions=(((1, 2, "b7"), "36 inch"), ((1, 3, "b8"), "24 inch")),
        recompute=lambda case: "48 inch" if case[1] == 3 else "36 inch",
    )

    assert outcome.case_count == 2
    assert outcome.contradictions == ((1, 3, "b8"),)
    assert outcome.passed is False


def test_one_answered_case_with_no_contradiction_passes():
    outcome = replay(
        family=FAMILY_SCHEDULE_LINK,
        human_decisions=((7, 70),),
        recompute=lambda case: 70,
    )

    assert (outcome.case_count, outcome.contradictions, outcome.passed) == (
        1,
        (),
        True,
    )


# --- The activation ledger ------------------------------------------------- #


def test_every_ledger_family_is_one_relation_and_a_typed_view():
    assert ledger_families() == (
        FAMILY_EVENT_ADMISSION,
        FAMILY_ORGANIZATION_IDENTITY,
        FAMILY_SCHEDULE_LINK,
        FAMILY_UNREADABLE_CELL_ADMISSION,
    )
    assert {
        FAMILY_EVENT_ADMISSION: EventAdmissionActivation,
        FAMILY_ORGANIZATION_IDENTITY: OrganizationIdentityActivation,
        FAMILY_SCHEDULE_LINK: ScheduleLinkActivation,
        FAMILY_UNREADABLE_CELL_ADMISSION: UnreadableCellAdmissionActivation,
    } == {family: ledger_model(family) for family in ledger_families()}
    assert {
        ledger_model(family).__table__.name for family in ledger_families()
    } == {"policy_activations"}
    with pytest.raises(ReplayGateRefusal):
        ledger_model("statement_scope")


def test_a_family_with_no_history_is_inactive(session, project):
    assert (
        activation_status(
            session,
            family=FAMILY_SCHEDULE_LINK,
            project_id=project.id,
            fingerprint=FINGERPRINT,
        )
        == INACTIVE
    )
    assert (
        latest_ledger_entry(
            session, family=FAMILY_SCHEDULE_LINK, project_id=project.id
        )
        is None
    )


def test_the_ledger_moves_inactive_to_active_to_suspended(session, project):
    record_activation(
        session,
        family=FAMILY_SCHEDULE_LINK,
        project_id=project.id,
        fingerprint=FINGERPRINT,
        replay_case_count=3,
        reason="regression replay passed on recorded human link decisions",
        recorded_by="corridor:schedule-link-activation",
    )
    assert (
        activation_status(
            session,
            family=FAMILY_SCHEDULE_LINK,
            project_id=project.id,
            fingerprint=FINGERPRINT,
        )
        == ACTIVE
    )

    # A changed rule fingerprint voids the pass; a deploy that changes neither
    # the rule nor the schema does not.
    assert (
        activation_status(
            session,
            family=FAMILY_SCHEDULE_LINK,
            project_id=project.id,
            fingerprint=CHANGED,
        )
        == INACTIVE
    )

    record_suspension(
        session,
        family=FAMILY_SCHEDULE_LINK,
        project_id=project.id,
        fingerprint=FINGERPRINT,
        reason="reviewing the rule",
        recorded_by="local:bob",
    )
    assert (
        activation_status(
            session,
            family=FAMILY_SCHEDULE_LINK,
            project_id=project.id,
            fingerprint=FINGERPRINT,
        )
        == SUSPENDED
    )


def test_a_suspension_records_who_suspended_it_and_no_proof(session, project):
    suspension = record_suspension(
        session,
        family=FAMILY_UNREADABLE_CELL_ADMISSION,
        project_id=project.id,
        fingerprint=RuleFingerprint("unreadable-cell-v1", "c" * 64),
        reason="  a person is checking two readings  ",
        recorded_by="local:carol",
    )

    assert suspension.family == FAMILY_UNREADABLE_CELL_ADMISSION
    assert suspension.action == "suspend"
    assert suspension.reason == "a person is checking two readings"
    assert suspension.recorded_by == "local:carol"
    assert suspension.replay_case_count is None


def test_a_ledger_entry_states_a_reason_and_an_actor(session, project):
    with pytest.raises(ReplayGateRefusal):
        record_suspension(
            session,
            family=FAMILY_SCHEDULE_LINK,
            project_id=project.id,
            fingerprint=FINGERPRINT,
            reason="   ",
            recorded_by="local:bob",
        )
    with pytest.raises(ReplayGateRefusal):
        record_activation(
            session,
            family=FAMILY_SCHEDULE_LINK,
            project_id=project.id,
            fingerprint=FINGERPRINT,
            replay_case_count=1,
            reason="passed",
            recorded_by="  ",
        )


def test_one_family_never_reads_another_familys_rows(session, project):
    record_activation(
        session,
        family=FAMILY_SCHEDULE_LINK,
        project_id=project.id,
        fingerprint=FINGERPRINT,
        replay_case_count=1,
        reason="link replay passed",
        recorded_by="corridor:schedule-link-activation",
    )

    assert (
        activation_status(
            session,
            family=FAMILY_UNREADABLE_CELL_ADMISSION,
            project_id=project.id,
            fingerprint=FINGERPRINT,
        )
        == INACTIVE
    )
    assert (
        session.scalars(
            select(UnreadableCellAdmissionActivation).where(
                UnreadableCellAdmissionActivation.project_id == project.id
            )
        ).all()
        == []
    )
    assert [row.family for row in session.scalars(
        select(PolicyActivation).where(PolicyActivation.project_id == project.id)
    )] == [FAMILY_SCHEDULE_LINK]


def test_one_status_read_answers_for_every_family(session, project):
    link = RuleFingerprint("schedule-conflict-link-v1", "a" * 64)
    cell = RuleFingerprint("unreadable-cell-v1", "c" * 64)
    identity = RuleFingerprint("organization-identity-v1", "d" * 64)
    record_activation(
        session,
        family=FAMILY_SCHEDULE_LINK,
        project_id=project.id,
        fingerprint=link,
        replay_case_count=2,
        reason="link replay passed",
        recorded_by="corridor:schedule-link-activation",
    )
    record_activation(
        session,
        family=FAMILY_ORGANIZATION_IDENTITY,
        project_id=project.id,
        fingerprint=identity,
        replay_case_count=1,
        reason="identity replay passed",
        recorded_by="corridor:organization-identity-activation",
    )
    record_suspension(
        session,
        family=FAMILY_ORGANIZATION_IDENTITY,
        project_id=project.id,
        fingerprint=identity,
        reason="a person is checking one spelling",
        recorded_by="local:dora",
    )

    assert family_statuses(
        session,
        project.id,
        {
            FAMILY_SCHEDULE_LINK: link,
            FAMILY_UNREADABLE_CELL_ADMISSION: cell,
            FAMILY_ORGANIZATION_IDENTITY: identity,
        },
    ) == {
        FAMILY_SCHEDULE_LINK: ACTIVE,
        FAMILY_UNREADABLE_CELL_ADMISSION: INACTIVE,
        FAMILY_ORGANIZATION_IDENTITY: SUSPENDED,
    }


def test_the_ledger_refuses_each_familys_other_shape(session, project):
    """Per-family shape is a database rule, not a convention the callers keep."""
    from sqlalchemy.exc import DatabaseError

    # A fingerprint-bound family cannot record an activation with no digest:
    # there would be nothing for a later deploy to compare against.
    with pytest.raises(DatabaseError):
        with session.begin_nested():
            session.add(
                ScheduleLinkActivation(
                    project_id=project.id,
                    action="activate",
                    policy_version="schedule-conflict-link-v1",
                    policy_sha256=None,
                    replay_case_count=1,
                    reason="a pass bound to nothing",
                    recorded_by="corridor:schedule-link-activation",
                )
            )
            session.flush()

    # The receipt-bound family cannot record one with no receipt, digest or not.
    with pytest.raises(DatabaseError):
        with session.begin_nested():
            session.add(
                EventAdmissionActivation(
                    project_id=project.id,
                    action="activate",
                    policy_version="event-admission-v3-unknown-scope",
                    policy_sha256="e" * 64,
                    acceptance_receipt_id=None,
                    reason="no receipt, and a digest it may not carry",
                    recorded_by="corridor:event-admission",
                )
            )
            session.flush()


def test_an_event_admission_reason_is_bounded_by_the_column_it_returns_to(
    session, project
):
    """The one family whose predecessor column is narrower than this relation.

    ``policy_activations.reason`` is 160 characters because three families'
    predecessors were. ``event_admission_activations.reason`` is 128, and the
    fold's downgrade restores that column, so a longer Event Admission reason
    could only come back truncated. The relation refuses one — the check
    constraint is the authority — and this seam says so in words rather than
    letting an operator's suspension reason reach the database and fail there.
    """

    long_enough_for_the_others = "s" * 160
    entry = record_suspension(
        session,
        family=FAMILY_SCHEDULE_LINK,
        project_id=project.id,
        fingerprint=FINGERPRINT,
        reason=long_enough_for_the_others,
        recorded_by="local:operator",
    )
    assert entry.reason == long_enough_for_the_others

    with pytest.raises(ReplayGateRefusal, match="128"):
        record_suspension(
            session,
            family=FAMILY_EVENT_ADMISSION,
            project_id=project.id,
            fingerprint=RuleFingerprint("unknown-scope-v1"),
            reason="e" * 129,
            recorded_by="local:operator",
        )
