"""Link Constraints to key dates by the schedule's own data (ADR-0057, #371)."""

from datetime import date

import pytest
from sqlalchemy import select

from corridor.milestones import import_csv, link_dependency
from corridor.models import (
    Dependency,
    Milestone,
    Project,
    ScheduleGoverningDerivation,
    ScheduleLinkActivation,
    ScheduleLinkReceipt,
)
from corridor.principals import HumanPrincipal
from corridor.schedule_linking import (
    DERIVATION_ACTOR,
    FLOW_THROUGH_ACTOR,
    MACHINE_ACTOR,
    ScheduleLinkRefusal,
    activation_status,
    derive_governing_set,
    flow_through_revisions,
    governing_milestone_ids,
    is_auto_link_active,
    lift_suspension,
    match_constraint,
    governing_coverages,
    pick_governing_activities,
    replay_matches_human_decisions,
    resolve_link,
    run_schedule_linking,
    suspend_auto_link,
)

ALICE = HumanPrincipal("local:alice")
BOB = HumanPrincipal("local:bob")

# Two governing activities, each carrying its own station range in its name.
GOVERNING_CSV = """code,name,need_date
UTIL-RELO-A,Utility relocations 100+00 to 150+00,2026-11-01
UTIL-RELO-B,Utility relocations 150+00 to 200+00,2027-02-15
LET,Letting,2027-06-01
"""

# One wide activity that overlaps A, so a station inside both is a tie.
TIE_CSV = """code,name,need_date
UTIL-RELO-A,Utility relocations 100+00 to 150+00,2026-11-01
UTIL-RELO-WIDE,Utility relocations 100+00 to 200+00,2026-12-01
"""

# A schedule whose codes and names carry no utility convention at all.
UNCODED_CSV = """code,name,need_date
A1000,Grading 100+00 to 150+00,2026-11-01
"""


@pytest.fixture
def project(session):
    p = Project(slug="sl-test", name="Schedule Link Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def seed(session, project, tmp_path, csv_text=GOVERNING_CSV, name="schedule.csv"):
    path = tmp_path / name
    path.write_text(csv_text)
    return import_csv(session, project_id=project.id, path=path)


def make_dep(session, project, ref, *, station_from=None, station_to=None, **kw):
    dep = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type=kw.pop("dep_type", "utility_relocation"),
        title=kw.pop("title", "conflict"),
        station_from=station_from,
        station_to=station_to,
        **kw,
    )
    session.add(dep)
    session.flush()
    return dep


def milestone(session, project, code):
    return session.scalars(
        select(Milestone).where(
            Milestone.project_id == project.id, Milestone.code == code
        )
    ).one()


# --- AC1: governing-set derivation records matched codes/names, no human step ---


def test_coded_schedule_derives_the_governing_set_with_no_human_step(
    session, project, tmp_path
):
    seed(session, project, tmp_path)
    derivation = derive_governing_set(
        session, project.id, source_name="schedule.csv"
    )
    assert derivation.method == "coded"
    # A system label, never a fabricated human identity: nobody was asked.
    assert derivation.recorded_by == DERIVATION_ACTOR
    matched = {m["code"]: m["matched_rule"] for m in derivation.matches_json}
    assert matched == {
        "UTIL-RELO-A": "code_prefix:UTIL",
        "UTIL-RELO-B": "code_prefix:UTIL",
    }
    # The non-utility Letting milestone does not flag itself.
    assert "LET" not in matched
    assert governing_milestone_ids(session, project.id) == {
        milestone(session, project, "UTIL-RELO-A").id,
        milestone(session, project, "UTIL-RELO-B").id,
    }


def test_governing_derivation_is_idempotent(session, project, tmp_path):
    seed(session, project, tmp_path)
    first = derive_governing_set(session, project.id, source_name="schedule.csv")
    assert first is not None
    # Re-deriving the identical governing set writes no redundant history.
    again = derive_governing_set(session, project.id, source_name="schedule.csv")
    assert again is None
    rows = session.scalars(
        select(ScheduleGoverningDerivation).where(
            ScheduleGoverningDerivation.project_id == project.id
        )
    ).all()
    assert len(rows) == 1


# --- AC2 + the ADR-0050 gate: exact single match ---


def test_a_brand_new_rule_never_auto_links_and_waits_for_a_person(
    session, project, tmp_path
):
    """ADR-0050: zero recorded human cases never pass; the rule ships inactive."""
    seed(session, project, tmp_path)
    dep = make_dep(session, project, "C1", station_from="120+00")
    result = run_schedule_linking(session, project.id, source_name="schedule.csv")

    assert is_auto_link_active(session, project.id) is False
    assert result.activation is None
    assert result.auto_linked == ()
    # The single exact match is offered to a person, not written.
    assert [m.dependency_id for m in result.single_awaiting_confirmation] == [dep.id]
    session.refresh(dep)
    assert dep.milestone_id is None


def test_exact_match_auto_links_once_a_passing_replay_activates_the_rule(
    session, project, tmp_path
):
    seed(session, project, tmp_path)
    util_a = milestone(session, project, "UTIL-RELO-A")
    util_b = milestone(session, project, "UTIL-RELO-B")

    # A person links the first exact match by hand — the rule's answer key.
    c1 = make_dep(session, project, "C1", station_from="120+00")
    run_schedule_linking(session, project.id, source_name="schedule.csv")
    resolve_link(session, c1, util_a, principal=ALICE)

    replay = replay_matches_human_decisions(session, project.id)
    assert replay.case_count == 1 and replay.passed

    # A new exact match now auto-links, bound to the exact Key Date Version.
    c2 = make_dep(session, project, "C2", station_from="175+00")
    result = run_schedule_linking(session, project.id, source_name="schedule.csv")
    assert result.activation is not None
    assert is_auto_link_active(session, project.id)
    assert [r.dependency_id for r in result.auto_linked] == [c2.id]

    session.refresh(c2)
    assert c2.milestone_id == util_b.id
    assert c2.milestone_registration_id == util_b.current_registration_id
    assert c2.need_date == date(2027, 2, 15)

    [receipt] = [
        r
        for r in session.scalars(
            select(ScheduleLinkReceipt).where(
                ScheduleLinkReceipt.dependency_id == c2.id
            )
        )
    ]
    assert receipt.basis == "exact_station_containment"
    assert receipt.decided_by == MACHINE_ACTOR
    assert receipt.policy_version == "schedule-conflict-link-v1"
    # The deciding values are retained verbatim from both sources.
    assert receipt.deciding_values_json["constraint_station_from"] == "175+00"
    assert receipt.deciding_values_json["activity_code"] == "UTIL-RELO-B"
    assert receipt.deciding_values_json["activity_coverage_from"] == "150+00"
    assert receipt.deciding_values_json["activity_coverage_to"] == "200+00"


def test_a_human_suspension_beats_a_passing_replay(session, project, tmp_path):
    seed(session, project, tmp_path)
    util_a = milestone(session, project, "UTIL-RELO-A")
    c1 = make_dep(session, project, "C1", station_from="120+00")
    run_schedule_linking(session, project.id, source_name="schedule.csv")
    resolve_link(session, c1, util_a, principal=ALICE)
    run_schedule_linking(session, project.id, source_name="schedule.csv")
    assert is_auto_link_active(session, project.id)

    suspend_auto_link(session, project.id, reason="reviewing the rule", principal=BOB)
    assert activation_status(session, project.id) == "suspended"

    c2 = make_dep(session, project, "C2", station_from="175+00")
    result = run_schedule_linking(session, project.id, source_name="schedule.csv")
    assert result.auto_linked == ()
    session.refresh(c2)
    assert c2.milestone_id is None

    # Only a human act lifts it, and only while the replay still passes.
    lift_suspension(session, project.id, principal=BOB)
    assert is_auto_link_active(session, project.id)


# --- AC3: a tie is a card of candidates; nothing auto-links ---


def test_two_covering_activities_make_a_tie_that_never_auto_links(
    session, project, tmp_path
):
    seed(session, project, tmp_path, csv_text=TIE_CSV)
    # Force the rule active so we prove a tie is withheld even when it could write.
    c_seed = make_dep(session, project, "SEED", station_from="175+00")
    # 175+00 is inside WIDE only among these two? No — WIDE covers 100–200 and A
    # covers 100–150, so 175 is inside WIDE alone: an exact case to seed history.
    run_schedule_linking(session, project.id, source_name="schedule.csv")
    wide = milestone(session, project, "UTIL-RELO-WIDE")
    resolve_link(session, c_seed, wide, principal=ALICE)
    run_schedule_linking(session, project.id, source_name="schedule.csv")
    assert is_auto_link_active(session, project.id)

    tie_dep = make_dep(session, project, "TIE", station_from="120+00")
    result = run_schedule_linking(session, project.id, source_name="schedule.csv")
    assert result.auto_linked == ()  # a tie is never written automatically
    [tie] = result.ties
    assert tie.dependency_id == tie_dep.id
    assert {c.code for c in tie.candidates} == {"UTIL-RELO-A", "UTIL-RELO-WIDE"}
    session.refresh(tie_dep)
    assert tie_dep.milestone_id is None


def test_a_person_resolves_a_tie_attributably_with_candidates_recorded(
    session, project, tmp_path
):
    seed(session, project, tmp_path, csv_text=TIE_CSV)
    tie_dep = make_dep(session, project, "TIE", station_from="120+00")
    util_a = milestone(session, project, "UTIL-RELO-A")

    receipt = resolve_link(session, tie_dep, util_a, principal=ALICE)
    assert receipt.basis == "human_choice"
    assert receipt.decided_by == "local:alice"
    session.refresh(tie_dep)
    assert tie_dep.milestone_id == util_a.id
    assert tie_dep.need_date == date(2026, 11, 1)
    # The candidates the person chose between are retained beside the choice.
    candidate_codes = {
        c["activity_code"] for c in receipt.deciding_values_json["candidates"]
    }
    assert candidate_codes == {"UTIL-RELO-A", "UTIL-RELO-WIDE"}


def test_a_human_link_to_a_non_covering_activity_is_refused(
    session, project, tmp_path
):
    seed(session, project, tmp_path)
    util_b = milestone(session, project, "UTIL-RELO-B")  # covers 150+00–200+00
    dep = make_dep(session, project, "C1", station_from="120+00")  # outside B
    with pytest.raises(ScheduleLinkRefusal):
        resolve_link(session, dep, util_b, principal=ALICE)
    session.refresh(dep)
    assert dep.milestone_id is None


# --- AC4: flow-through advances every linked Constraint, retaining history ---


def test_a_moved_date_flows_through_every_linked_constraint_with_no_gate(
    session, project, tmp_path
):
    seed(session, project, tmp_path)
    util_a = milestone(session, project, "UTIL-RELO-A")
    first_version = util_a.current_registration_id
    dep = make_dep(session, project, "C1", station_from="120+00")
    resolve_link(session, dep, util_a, principal=ALICE)
    assert dep.need_date == date(2026, 11, 1)

    # A schedule revision moves UTIL-RELO-A's date.
    revised = GOVERNING_CSV.replace(
        "Utility relocations 100+00 to 150+00,2026-11-01",
        "Utility relocations 100+00 to 150+00,2027-04-01",
    )
    seed(session, project, tmp_path, csv_text=revised)
    session.refresh(util_a)
    assert util_a.current_registration_id != first_version

    flowed = flow_through_revisions(session, project.id)
    assert [r.dependency_id for r in flowed] == [dep.id]
    session.refresh(dep)
    # The Required By basis advanced automatically, per row, no approval question.
    assert dep.need_date == date(2027, 4, 1)
    assert dep.milestone_registration_id == util_a.current_registration_id
    [flow_receipt] = flowed
    assert flow_receipt.basis == "flow_through"
    assert flow_receipt.decided_by == FLOW_THROUGH_ACTOR
    assert flow_receipt.deciding_values_json["prior_need_date"] == "2026-11-01"
    assert flow_receipt.deciding_values_json["new_need_date"] == "2027-04-01"
    # The prior Key Date Version is retained in immutable history.
    assert flow_receipt.deciding_values_json["prior_key_date_version_id"] == first_version


def test_flow_through_is_idempotent_when_nothing_moved(session, project, tmp_path):
    seed(session, project, tmp_path)
    util_a = milestone(session, project, "UTIL-RELO-A")
    dep = make_dep(session, project, "C1", station_from="120+00")
    resolve_link(session, dep, util_a, principal=ALICE)
    assert flow_through_revisions(session, project.id) == ()
    # A re-import of the identical schedule moves no date, so it re-links nothing.
    seed(session, project, tmp_path)
    assert flow_through_revisions(session, project.id) == ()


# --- AC7: uncoded schedule falls back to a recorded one-time human pick ---


def test_uncoded_schedule_awaits_a_recorded_human_pick(session, project, tmp_path):
    seed(session, project, tmp_path, csv_text=UNCODED_CSV)
    derivation = derive_governing_set(
        session, project.id, source_name="schedule.csv"
    )
    assert derivation.method == "awaiting_pick"
    assert derivation.matches_json == []
    assert governing_milestone_ids(session, project.id) == set()

    grading = milestone(session, project, "A1000")
    dep = make_dep(session, project, "C1", station_from="120+00")
    # With no governing set, nothing matches.
    assert match_constraint(dep, governing_coverages(session, project.id)).kind == "none"

    pick = pick_governing_activities(
        session, project.id, [grading.id], principal=ALICE
    )
    assert pick.method == "human_pick"
    assert pick.recorded_by == "local:alice"
    assert governing_milestone_ids(session, project.id) == {grading.id}
    # The picked activity now covers the constraint by its own station range.
    assert match_constraint(dep, governing_coverages(session, project.id)).kind == "exact"


# --- AC8: re-importing an identical schedule is a no-op ---


def test_rerunning_linking_adds_no_redundant_history(session, project, tmp_path):
    seed(session, project, tmp_path)
    util_a = milestone(session, project, "UTIL-RELO-A")
    c1 = make_dep(session, project, "C1", station_from="120+00")
    run_schedule_linking(session, project.id, source_name="schedule.csv")
    resolve_link(session, c1, util_a, principal=ALICE)
    make_dep(session, project, "C2", station_from="175+00")
    run_schedule_linking(session, project.id, source_name="schedule.csv")

    receipts_before = set(
        session.scalars(
            select(ScheduleLinkReceipt.id).where(
                ScheduleLinkReceipt.project_id == project.id
            )
        )
    )
    activations_before = set(
        session.scalars(
            select(ScheduleLinkActivation.id).where(
                ScheduleLinkActivation.project_id == project.id
            )
        )
    )
    run_schedule_linking(session, project.id, source_name="schedule.csv")
    receipts_after = set(
        session.scalars(
            select(ScheduleLinkReceipt.id).where(
                ScheduleLinkReceipt.project_id == project.id
            )
        )
    )
    activations_after = set(
        session.scalars(
            select(ScheduleLinkActivation.id).where(
                ScheduleLinkActivation.project_id == project.id
            )
        )
    )
    assert receipts_after == receipts_before
    assert activations_after == activations_before
