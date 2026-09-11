"""Resolving one Review Packet in one atomic transaction (#526).

A packet act is complete or absent.  Every child is re-read and re-checked
before anything is written, one stale, superseded, cross-project, missing, or
invalid child refuses the whole act, and a refusal returns what the reading
must re-read without discarding what the coordinator chose.

A packet carrying any semantic or Follow-up Plan decision commits exactly one
Project Record revision holding all of them, each keeping its own identity; a
packet of dated deferrals alone commits none (ADR-0084, ADR-0085).

Every time in these tests is supplied by the caller.  Nothing compares a
server-assigned timestamp with a logical one, and every "did the record move"
question is answered from append-only identifiers.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.analytics import EventFamily, capture_events, default_binding
from corridor.delta_resolution import (
    ACCEPT,
    CONSTRAINED_EDIT,
    COORDINATION_NEEDED,
    REFUSED,
    RESOLVED,
    STALE,
    UNSUPPORTED,
    CapturedSupport,
    ChildDecisionRequest,
    ContradictoryDeltaResolution,
    FreeText,
    RecordEffect,
    live_delta_status,
    resolve_delta,
    validate_child_decision,
)
from corridor.models import (
    DeltaDecisionSupport,
    DeltaDeferral,
    DeltaDisposition,
    DeltaFollowUpPlan,
    DeltaFollowUpPlanEvidence,
    DeltaRecordDecision,
    DeltaReviewPacketReceipt,
    DeltaReviewPacketSupport,
    Fact,
    FactDecision,
    Project,
    ProjectRecordRevision,
    ProposedDelta,
    SourceSegment,
)
from corridor.native_follow_up_reading import undone_follow_up_plan_ids
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import (
    read_open_deltas,
    resolved_delta_ids_by_project,
)
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
)
from corridor.db_roles import RECORD_DECISION_ROLE
from harness_support import adopt_baseline_facts, as_role
from source_capture_support import Rendition
from delta_supersession_support import record_delta_supersession
from record_counts import nothing_written
from corridor.review_packets import (
    APPLY,
    EDIT_AND_APPLY,
    KEEP_CURRENT,
    NEEDS_COORDINATION,
    REVERSED,
    SAVED,
    CoordinationRequest,
    DeferralRequest,
    PacketChildRequest,
    ReviewPacketRefused,
    ReviewPacketRequest,
    child_idempotency_key,
    packet_children,
    packet_reversal,
    resolve_review_packet,
    reverse_review_packet,
)
from corridor.review_packets import DEFER as DEFER_OUTCOME
from corridor.support_assessments import FactProposition, record_support_assessment


ALICE = HumanPrincipal("local:alice")
BOB = HumanPrincipal("local:bob")
DECIDED_AT = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)
ASSESSED_AT = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
RETURNS_AT = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
SUBJECT = "Utility Conflicts!7"
SOURCE_REVISION = "rev-1"
RULE_VERSION = "packetizer-v1"


def _rendition(session: Session, project: Project, name: str) -> Rendition:
    """One arriving workbook rendition, concerning this module's one subject."""

    return Rendition(session, project, name, subject_key=SUBJECT)


def _support(
    session: Session,
    project: Project,
    fact: Fact,
    segment: SourceSegment,
    *,
    assessment: str = "supported",
    evidence_role: str = "value_support",
):
    return record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(fact.id),
        source_segment_ids=[segment.id],
        evidence_role=evidence_role,
        assessment=assessment,
        authority=ALICE,
        assessed_at=ASSESSED_AT,
    )


def _delta(
    session: Session,
    project: Project,
    *,
    field: str = "station_from",
    accepted_value: object = "1149+00",
    proposed_value: object = "1200+00",
    subject: str = SUBJECT,
    source_revision: str = SOURCE_REVISION,
    baseline_revision: int | None = None,
) -> ProposedDelta:
    (delta,) = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="ucm-workbook",
        source_revision=source_revision,
        deltas=[
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget(subject_identity=subject, field=field),
                accepted_value=accepted_value,
                proposed_value=proposed_value,
                accepted_baseline_revision=(
                    f"revision:{baseline_revision}"
                    if baseline_revision is not None
                    else None
                ),
            )
        ],
    )
    return delta


def _adopt(session: Session, project: Project, fact: Fact, key: str) -> int:
    """One accepted baseline decision, written as the record-decision role."""

    return adopt_baseline_facts(session, project, fact, key=key)


def _packet(
    project: Project,
    children: tuple[PacketChildRequest, ...],
    **overrides,
) -> ReviewPacketRequest:
    values = {
        "project_id": project.id,
        "grouping_rule_version": RULE_VERSION,
        "grouping_key_kind": "source_revision",
        "grouping_key": SOURCE_REVISION,
        "principal": ALICE,
        "idempotency_key": f"packet:{uuid4().hex[:10]}",
        "decided_at": DECIDED_AT,
        "children": children,
    }
    values.update(overrides)
    return ReviewPacketRequest(**values)


def _apply_child(
    delta: ProposedDelta, fact: Fact, assessment, **overrides
) -> PacketChildRequest:
    values = {
        "delta_id": delta.id,
        "outcome": APPLY,
        "observed_source_revision": delta.source_revision,
        "record_effects": (RecordEffect(fact_id=fact.id),),
        "support_assessment_ids": (assessment.id,),
    }
    values.update(overrides)
    return PacketChildRequest(**values)


def _defer_child(delta: ProposedDelta, **overrides) -> PacketChildRequest:
    values = {
        "delta_id": delta.id,
        "outcome": DEFER_OUTCOME,
        "observed_source_revision": delta.source_revision,
        "deferral": DeferralRequest(
            deferred_until=RETURNS_AT, reason="the utility has not replied"
        ),
    }
    values.update(overrides)
    return PacketChildRequest(**values)


def _revision_count(session: Session, project: Project) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ProjectRecordRevision)
            .where(ProjectRecordRevision.project_id == project.id)
        )
    )


# --- All or nothing: one bad child refuses the whole act ------------------


def test_a_stale_child_refuses_the_whole_packet_and_writes_nothing(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-a.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    first_incoming, first_segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    second_incoming, second_segment = rendition.capture(
        fact_type="station_to", value="1260+00"
    )
    first_support = _support(session, project, first_incoming, first_segment)
    second_support = _support(session, project, second_incoming, second_segment)
    stale_delta = _delta(session, project, field="station_from")
    healthy_delta = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )
    # The coordinator's reading is one revision behind on station_from only,
    # and nothing at all is written, including for the child that was fine.
    with nothing_written(session, project.id):
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    _apply_child(stale_delta, first_incoming, first_support),
                    _apply_child(healthy_delta, second_incoming, second_support),
                ),
                observed_accepted_revision_id=baseline - 1,
            ),
        )

    assert result.status == REFUSED
    assert [refusal.reason for refusal in result.refusals] == [
        "stale_accepted_revision"
    ]
    assert result.refusals[0].status == STALE
    assert result.refusals[0].current_accepted_revision_id == baseline
    assert result.receipt_id is None and result.revision_id is None
    # And the coordinator's own selections come back untouched.
    assert result.preserved_selections == tuple(
        (
            _apply_child(stale_delta, first_incoming, first_support),
            _apply_child(healthy_delta, second_incoming, second_support),
        )
    )


def test_a_superseded_child_refuses_the_whole_packet(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-b.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    superseded = _delta(session, project, field="station_from")
    newer = _delta(session, project, field="station_from", source_revision="rev-2")
    record_delta_supersession(
        session,
        project_id=project.id,
        prior_delta_id=superseded.id,
        superseding_delta_id=newer.id,
    )
    with nothing_written(session, project.id):
        result = resolve_review_packet(
            session,
            _packet(project, (_apply_child(superseded, incoming, support),)),
        )

    assert result.status == REFUSED
    assert result.refusals[0].reason == "superseded_delta"
    assert result.refusals[0].delta_status == "superseded"


def test_a_cross_project_child_refuses_the_whole_packet(
    session: Session, project: Project
) -> None:
    other = Project(
        slug=f"review-packet-other-{uuid4().hex[:8]}",
        name="Other",
        is_synthetic=True,
    )
    session.add(other)
    session.flush()
    rendition = _rendition(session, project, "ucm-c.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    mine = _delta(session, project)
    theirs = _delta(session, other)
    with nothing_written(session, project.id, other.id):
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    _apply_child(mine, incoming, support),
                    PacketChildRequest(
                        delta_id=theirs.id,
                        outcome=KEEP_CURRENT,
                        observed_source_revision=theirs.source_revision,
                    ),
                ),
            ),
        )

    assert result.status == REFUSED
    assert result.refusals[0].reason == "cross_project_delta"

    # A child that names no delta at all refuses on the same rule.
    with nothing_written(session, project.id, other.id):
        missing = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    PacketChildRequest(
                        delta_id=theirs.id + 10_000,
                        outcome=KEEP_CURRENT,
                        observed_source_revision=SOURCE_REVISION,
                    ),
                ),
            ),
        )
    assert missing.status == REFUSED
    assert missing.refusals[0].reason == "cross_project_delta"


def test_a_child_read_against_another_source_version_refuses_the_packet(
    session: Session, project: Project
) -> None:
    """The source version is named per child, so no child is implicitly selected."""

    rendition = _rendition(session, project, "ucm-d.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project, source_revision="rev-7")
    with nothing_written(session, project.id):
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (_apply_child(delta, incoming, support, observed_source_revision="rev-6"),),
            ),
        )

    assert result.status == REFUSED
    assert result.refusals[0].reason == "source_version_mismatch"


def test_a_child_without_named_support_refuses_the_whole_packet(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-e.xlsx")
    good, good_segment = rendition.capture(fact_type="station_from", value="1200+00")
    unsupported, _ = rendition.capture(fact_type="station_to", value="1260+00")
    good_support = _support(session, project, good, good_segment)
    first = _delta(session, project, field="station_from")
    second = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )
    with nothing_written(session, project.id):
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    _apply_child(first, good, good_support),
                    PacketChildRequest(
                        delta_id=second.id,
                        outcome=APPLY,
                        observed_source_revision=second.source_revision,
                        record_effects=(RecordEffect(fact_id=unsupported.id),),
                    ),
                ),
            ),
        )

    assert result.status == REFUSED
    assert result.refusals[0].status == UNSUPPORTED
    assert result.refusals[0].reason == "missing_support"


def test_an_already_resolved_child_refuses_the_whole_packet(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-f.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    resolved = _delta(session, project)
    assert (
        resolve_delta(
            session,
            ChildDecisionRequest(
                project_id=project.id,
                delta_id=resolved.id,
                action=ACCEPT,
                principal=ALICE,
                idempotency_key=f"standalone:{uuid4().hex[:8]}",
                decided_at=DECIDED_AT,
                record_effects=(RecordEffect(fact_id=incoming.id),),
                support_assessment_ids=(support.id,),
            ),
        ).status
        == RESOLVED
    )
    with nothing_written(session, project.id):
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    PacketChildRequest(
                        delta_id=resolved.id,
                        outcome=KEEP_CURRENT,
                        observed_source_revision=resolved.source_revision,
                    ),
                ),
            ),
        )

    assert result.status == REFUSED
    assert result.refusals[0].reason == "already_resolved"


def test_a_packet_names_its_exact_ordered_child_set(
    session: Session, project: Project
) -> None:
    """No wildcard, no hidden child, and no delta decided twice in one act."""

    delta = _delta(session, project)

    with pytest.raises(ReviewPacketRefused, match="exact ordered child set"):
        resolve_review_packet(session, _packet(project, ()))

    with pytest.raises(ReviewPacketRefused, match="twice in one packet"):
        resolve_review_packet(
            session,
            _packet(
                project,
                (
                    PacketChildRequest(
                        delta_id=delta.id,
                        outcome=KEEP_CURRENT,
                        observed_source_revision=delta.source_revision,
                    ),
                    PacketChildRequest(
                        delta_id=delta.id,
                        outcome=KEEP_CURRENT,
                        observed_source_revision=delta.source_revision,
                    ),
                ),
            ),
        )

    with pytest.raises(ReviewPacketRefused, match="source version"):
        resolve_review_packet(
            session,
            _packet(
                project,
                (
                    PacketChildRequest(
                        delta_id=delta.id,
                        outcome=KEEP_CURRENT,
                        observed_source_revision="  ",
                    ),
                ),
            ),
        )


# --- Scheduling alone writes no revision ----------------------------------


def test_a_packet_of_dated_deferrals_alone_writes_no_project_record_revision(
    session: Session, project: Project
) -> None:
    first = _delta(session, project, field="station_from")
    second = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )
    before = _revision_count(session, project)

    result = resolve_review_packet(
        session, _packet(project, (_defer_child(first), _defer_child(second)))
    )

    assert result.status == SAVED
    assert result.revision_id is None
    assert not result.wrote_revision
    assert _revision_count(session, project) == before
    # The deltas stay open, and only the exact scheduling receipts exist.
    assert live_delta_status(session, first.id) == "deferred"
    assert live_delta_status(session, second.id) == "deferred"
    assert session.scalar(
        select(func.count()).select_from(DeltaDisposition).where(
            DeltaDisposition.project_id == project.id
        )
    ) == 0
    receipts = session.scalars(
        select(DeltaDeferral)
        .where(DeltaDeferral.project_id == project.id)
        .order_by(DeltaDeferral.id)
    ).all()
    assert [row.delta_id for row in receipts] == [first.id, second.id]
    assert [row.deferred_until for row in receipts] == [RETURNS_AT, RETURNS_AT]
    assert {row.scheduled_by_principal for row in receipts} == {ALICE.subject}
    # Scheduling creates no external follow-up and moves no accepted value.
    assert session.scalar(
        select(func.count()).select_from(DeltaFollowUpPlan).where(
            DeltaFollowUpPlan.project_id == project.id
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(FactDecision).where(
            FactDecision.project_id == project.id
        )
    ) == 0
    # And the receipt itself carries no revision.
    receipt = session.get(DeltaReviewPacketReceipt, result.receipt_id)
    assert receipt.revision_id is None
    assert [child.outcome for child in packet_children(session, receipt.id)] == [
        DEFER_OUTCOME,
        DEFER_OUTCOME,
    ]


# --- One revision, several separately identified decisions ----------------


def test_one_packet_writes_one_revision_holding_every_child_decision(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-g.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, incoming_segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    chosen, chosen_segment = rendition.capture(
        fact_type="station_to", value="1265+00"
    )
    incoming_support = _support(session, project, incoming, incoming_segment)
    chosen_support = _support(session, project, chosen, chosen_segment)
    applied = _delta(session, project, field="station_from", baseline_revision=baseline)
    kept = _delta(
        session,
        project,
        field="conflict_description",
        accepted_value="crossing",
        proposed_value="parallel",
    )
    edited = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )
    before = _revision_count(session, project)

    result = resolve_review_packet(
        session,
        _packet(
            project,
            (
                _apply_child(applied, incoming, incoming_support),
                PacketChildRequest(
                    delta_id=kept.id,
                    outcome=KEEP_CURRENT,
                    observed_source_revision=kept.source_revision,
                    rationale="the workbook row was keyed to the wrong conflict",
                ),
                PacketChildRequest(
                    delta_id=edited.id,
                    outcome=EDIT_AND_APPLY,
                    observed_source_revision=edited.source_revision,
                    edit_basis=CapturedSupport(fact_id=chosen.id),
                    support_assessment_ids=(chosen_support.id,),
                ),
            ),
            observed_accepted_revision_id=baseline,
        ),
    )

    assert result.status == SAVED
    # Exactly one revision, and no artificial intermediate ones.
    assert _revision_count(session, project) == before + 1
    revision = session.get(ProjectRecordRevision, result.revision_id)
    assert revision.command_type == "resolve_delta"
    assert revision.human_principal == ALICE.subject
    # Each child keeps its own separately identified decision inside it.
    decisions = session.scalars(
        select(DeltaRecordDecision)
        .where(DeltaRecordDecision.revision_id == result.revision_id)
        .order_by(DeltaRecordDecision.id)
    ).all()
    assert [row.delta_id for row in decisions] == [applied.id, kept.id, edited.id]
    assert [row.disposition for row in decisions] == ["accept", "reject", "edit"]
    assert len({row.disposition_id for row in decisions}) == 3
    assert [child.outcome for child in result.children] == [
        APPLY,
        KEEP_CURRENT,
        EDIT_AND_APPLY,
    ]
    assert all(child.decision_id is not None for child in result.children)
    assert {child.delta_id for child in result.children} == {
        applied.id,
        kept.id,
        edited.id,
    }
    effective = {
        row.fact_type: row.fact_id
        for row in session.scalars(
            select(FactDecision).where(
                FactDecision.project_id == project.id,
                FactDecision.superseded_by.is_(None),
            )
        ).all()
    }
    assert effective["station_from"] == incoming.id
    assert effective["station_to"] == chosen.id


def test_a_packet_child_and_a_standalone_resolution_validate_identically(
    session: Session, project: Project
) -> None:
    """One validation and decision-construction seam, not two (#519)."""

    rendition = _rendition(session, project, "ucm-h.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project)
    packet = _packet(project, (_apply_child(delta, incoming, support),))
    child = packet.children[0]

    standalone = validate_child_decision(
        session,
        ChildDecisionRequest(
            project_id=project.id,
            delta_id=delta.id,
            action=ACCEPT,
            principal=ALICE,
            idempotency_key=child_idempotency_key(packet.idempotency_key, delta.id),
            decided_at=DECIDED_AT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(support.id,),
        ),
    )
    result = resolve_review_packet(session, packet)

    assert result.status == SAVED
    written = session.get(DeltaRecordDecision, result.children[0].decision_id)
    assert written.disposition == standalone.disposition
    assert written.effect_kind == standalone.effect_kind
    assert written.decided_by_principal == standalone.principal_subject
    assert written.idempotency_key == standalone.idempotency_key
    assert written.edit_basis == standalone.edit_basis
    assert [row.support_assessment_id for row in session.scalars(
        select(DeltaDecisionSupport)
        .where(DeltaDecisionSupport.decision_id == written.id)
        .order_by(DeltaDecisionSupport.ordinal)
    ).all()] == list(standalone.support_assessment_ids)


# --- #519's constrained edit governs a packet child -----------------------


def test_an_edit_child_of_unsupported_free_text_refuses_the_whole_packet(
    session: Session, project: Project
) -> None:
    """ADR-0084's rule is #519's, and the packet does not soften it."""

    rendition = _rendition(session, project, "ucm-q.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    healthy = _delta(session, project, field="station_from")
    typed = _delta(
        session,
        project,
        field="conflict_description",
        accepted_value="crossing",
        proposed_value="parallel",
    )
    with nothing_written(session, project.id):
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    _apply_child(healthy, incoming, support),
                    PacketChildRequest(
                        delta_id=typed.id,
                        outcome=EDIT_AND_APPLY,
                        observed_source_revision=typed.source_revision,
                        edit_basis=FreeText("skewed crossing"),
                        support_assessment_ids=(support.id,),
                    ),
                ),
            ),
        )

    assert result.status == REFUSED
    assert result.refusals[0].status == CONSTRAINED_EDIT
    assert result.refusals[0].reason == "unsupported_free_text"


def test_free_text_on_an_external_fact_returns_the_coordination_question(
    session: Session, project: Project
) -> None:
    """The bounded result #519 returns so #526 can open the Follow-up Plan form."""

    rendition = _rendition(session, project, "ucm-r.xlsx")
    incoming, segment = rendition.capture(
        fact_type="external_org", value="Regional Water"
    )
    support = _support(session, project, incoming, segment)
    delta = _delta(
        session,
        project,
        field="external_org",
        accepted_value="City Water",
        proposed_value="Regional Water",
    )
    with nothing_written(session, project.id):
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    PacketChildRequest(
                        delta_id=delta.id,
                        outcome=EDIT_AND_APPLY,
                        observed_source_revision=delta.source_revision,
                        edit_basis=FreeText("Regional Water Authority"),
                        support_assessment_ids=(support.id,),
                        organization_change_kind="correction",
                    ),
                ),
            ),
        )

    assert result.status == REFUSED
    assert result.refusals[0].status == COORDINATION_NEEDED
    assert result.refusals[0].reason == "external_fact_needs_coordination"
    assert "Regional Water Authority" in result.refusals[0].open_question
    # The proposed value stays unaccepted and the delta stays open, so the
    # coordinator can resubmit the same packet with this one child answered
    # as Needs coordination.
    assert live_delta_status(session, delta.id) == "open"
    resubmitted = resolve_review_packet(
        session,
        _packet(
            project,
            (
                PacketChildRequest(
                    delta_id=delta.id,
                    outcome=NEEDS_COORDINATION,
                    observed_source_revision=delta.source_revision,
                    coordination=CoordinationRequest(
                        question=result.refusals[0].open_question,
                        responsible_organization="Regional Water",
                        return_date=RETURNS_AT,
                    ),
                ),
            ),
        ),
    )
    assert resubmitted.status == SAVED
    plan = session.get(
        DeltaFollowUpPlan, resubmitted.children[0].follow_up_plan_id
    )
    assert plan.open_question == result.refusals[0].open_question


# --- Needs coordination ---------------------------------------------------


def test_needs_coordination_records_a_plan_and_leaves_the_delta_open(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-i.xlsx")
    incoming, segment = rendition.capture(
        fact_type="committed_date", value="2026-11-02",)
    evidence = _support(session, project, incoming, segment)
    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-10-01",
        proposed_value="2026-11-02",
    )
    before = _revision_count(session, project)

    result = resolve_review_packet(
        session,
        _packet(
            project,
            (
                PacketChildRequest(
                    delta_id=delta.id,
                    outcome=NEEDS_COORDINATION,
                    observed_source_revision=delta.source_revision,
                    coordination=CoordinationRequest(
                        question=(
                            "Which Promised For date does the utility stand behind?"
                        ),
                        responsible_organization="City Water",
                        responsible_principal="local:alice",
                        return_date=RETURNS_AT,
                        affected_scope={"subject": SUBJECT, "field": "committed_date"},
                        evidence_support_assessment_ids=(evidence.id,),
                    ),
                ),
            ),
        ),
    )

    assert result.status == SAVED
    # It is a decision inside the packet's one revision...
    assert _revision_count(session, project) == before + 1
    plan = session.get(DeltaFollowUpPlan, result.children[0].follow_up_plan_id)
    assert plan.revision_id == result.revision_id
    assert plan.open_question.startswith("Which Promised For date")
    assert plan.responsible_organization == "City Water"
    assert plan.responsible_principal == "local:alice"
    assert plan.return_date == RETURNS_AT
    assert plan.affected_scope == {"subject": SUBJECT, "field": "committed_date"}
    assert plan.recorded_by_principal == ALICE.subject
    assert [row.support_assessment_id for row in session.scalars(
        select(DeltaFollowUpPlanEvidence)
        .where(DeltaFollowUpPlanEvidence.plan_id == plan.id)
        .order_by(DeltaFollowUpPlanEvidence.ordinal)
    ).all()] == [evidence.id]
    # ...that accepts nothing and leaves the delta open.
    assert live_delta_status(session, delta.id) == "open"
    assert session.scalar(
        select(func.count()).select_from(DeltaDisposition).where(
            DeltaDisposition.delta_id == delta.id
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(FactDecision).where(
            FactDecision.project_id == project.id
        )
    ) == 0


def test_needs_coordination_without_a_responsible_party_refuses_the_packet(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project)
    with nothing_written(session, project.id):
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    PacketChildRequest(
                        delta_id=delta.id,
                        outcome=NEEDS_COORDINATION,
                        observed_source_revision=delta.source_revision,
                        coordination=CoordinationRequest(question="Who owns this line?"),
                    ),
                ),
            ),
        )

    assert result.status == REFUSED
    assert result.refusals[0].reason == "missing_responsible_party"
    assert result.refusals[0].open_question == "Who owns this line?"


# --- Mixed packets --------------------------------------------------------


def test_a_mixed_packet_commits_one_revision_its_plans_and_its_deferrals_at_once(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-j.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    applied = _delta(session, project, field="station_from")
    coordinated = _delta(
        session,
        project,
        field="external_org",
        accepted_value="City Water",
        proposed_value="Regional Water",
    )
    deferred = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )
    before = _revision_count(session, project)

    result = resolve_review_packet(
        session,
        _packet(
            project,
            (
                _apply_child(applied, incoming, support),
                PacketChildRequest(
                    delta_id=coordinated.id,
                    outcome=NEEDS_COORDINATION,
                    observed_source_revision=coordinated.source_revision,
                    coordination=CoordinationRequest(
                        question="Did the utility owner actually change?",
                        responsible_organization="Regional Water",
                        return_date=RETURNS_AT,
                    ),
                ),
                _defer_child(deferred),
            ),
            grouping_key_kind="coordination_question",
            grouping_key="Utility Conflict 7",
        ),
    )

    assert result.status == SAVED
    # One revision for the semantic and Follow-up Plan decisions...
    assert _revision_count(session, project) == before + 1
    assert result.children[0].decision_id is not None
    assert result.children[1].follow_up_plan_id is not None
    # ...and the deferral receipt written in the same transaction, with no
    # revision of its own.
    deferral = session.get(DeltaDeferral, result.children[2].deferral_id)
    assert deferral.delta_id == deferred.id
    assert live_delta_status(session, deferred.id) == "deferred"
    assert live_delta_status(session, coordinated.id) == "open"
    assert live_delta_status(session, applied.id) == "resolved"


def test_the_receipt_enumerates_the_rule_children_outcomes_and_support(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-k.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    evidence_fact, evidence_segment = rendition.capture(
        fact_type="external_org", value="Regional Water"
    )
    evidence = _support(session, project, evidence_fact, evidence_segment)
    applied = _delta(session, project, field="station_from", baseline_revision=baseline)
    coordinated = _delta(
        session,
        project,
        field="external_org",
        accepted_value="City Water",
        proposed_value="Regional Water",
    )
    deferred = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )

    result = resolve_review_packet(
        session,
        _packet(
            project,
            (
                _apply_child(applied, incoming, support),
                PacketChildRequest(
                    delta_id=coordinated.id,
                    outcome=NEEDS_COORDINATION,
                    observed_source_revision=coordinated.source_revision,
                    coordination=CoordinationRequest(
                        question="Did the utility owner actually change?",
                        responsible_organization="Regional Water",
                        evidence_support_assessment_ids=(evidence.id,),
                    ),
                ),
                _defer_child(deferred),
            ),
            observed_accepted_revision_id=baseline,
        ),
    )

    assert result.status == SAVED
    receipt = session.get(DeltaReviewPacketReceipt, result.receipt_id)
    assert receipt.grouping_rule_version == RULE_VERSION
    assert receipt.grouping_key_kind == "source_revision"
    assert receipt.grouping_key == SOURCE_REVISION
    assert receipt.decided_by_principal == ALICE.subject
    assert receipt.observed_accepted_revision_id == baseline
    assert receipt.revision_id == result.revision_id
    assert receipt.decided_at == DECIDED_AT

    children = packet_children(session, receipt.id)
    assert [row.ordinal for row in children] == [1, 2, 3]
    assert [row.delta_id for row in children] == [
        applied.id,
        coordinated.id,
        deferred.id,
    ]
    assert [row.outcome for row in children] == [
        APPLY,
        NEEDS_COORDINATION,
        DEFER_OUTCOME,
    ]
    assert [row.observed_source_revision for row in children] == [SOURCE_REVISION] * 3
    assert children[0].decision_id == result.children[0].decision_id
    assert children[1].follow_up_plan_id == result.children[1].follow_up_plan_id
    assert children[2].deferral_id == result.children[2].deferral_id

    # The Support Assessments the whole act used, in the order it named them.
    used = session.scalars(
        select(DeltaReviewPacketSupport)
        .where(DeltaReviewPacketSupport.receipt_id == receipt.id)
        .order_by(DeltaReviewPacketSupport.ordinal)
    ).all()
    assert [row.support_assessment_id for row in used] == [support.id, evidence.id]


def test_a_replay_of_the_same_packet_returns_what_it_already_wrote(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-l.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project)
    packet = _packet(project, (_apply_child(delta, incoming, support),))

    first = resolve_review_packet(session, packet)
    assert first.status == SAVED
    before = _revision_count(session, project)

    # The delta is now resolved, so the replay refuses rather than deciding
    # twice; the first act's identities remain exactly as recorded.
    second = resolve_review_packet(session, packet)
    assert second.status == REFUSED
    assert second.refusals[0].reason == "already_resolved"
    assert _revision_count(session, project) == before
    assert session.get(DeltaReviewPacketReceipt, first.receipt_id) is not None


# --- Undo, and the later targeted correction ------------------------------


def test_undo_reverses_the_complete_packet_when_no_later_act_depends_on_it(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-m.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    applied = _delta(session, project, field="station_from", baseline_revision=baseline)
    deferred = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )
    saved = resolve_review_packet(
        session,
        _packet(
            project,
            (
                _apply_child(applied, incoming, support),
                _defer_child(deferred),
            ),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert saved.status == SAVED

    undone = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )

    assert undone.status == REVERSED
    assert undone.created
    # The accepted record is back where the packet found it, by a compensating
    # decision rather than by deleting anything.
    effective = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.fact_type == "station_from",
            FactDecision.superseded_by.is_(None),
        )
    ).all()
    assert [row.fact_id for row in effective] == [accepted.id]
    assert effective[0].revision_id == undone.revision_id
    compensating = session.get(ProjectRecordRevision, undone.revision_id)
    assert compensating.command_type == "reverse_review_packet"
    assert compensating.human_principal == BOB.subject
    # The original act is untouched and still readable.
    assert session.get(DeltaRecordDecision, saved.children[0].decision_id) is not None
    receipt = session.get(DeltaReviewPacketReceipt, saved.receipt_id)
    assert receipt.revision_id == saved.revision_id
    assert len(packet_children(session, receipt.id)) == 2
    assert packet_reversal(session, receipt.id).id == undone.reversal_id
    # And both children are back: the deferred one in immediate work, and the
    # applied one as the question it was before the decision (#948, ADR-0035).
    assert live_delta_status(session, deferred.id) == "open"
    assert session.get(DeltaDeferral, saved.children[1].deferral_id) is not None
    assert live_delta_status(session, applied.id) == "open"


def test_the_batched_resolved_reading_refuses_two_effective_decisions(
    session: Session, project: Project
) -> None:
    """The bulk resolved set reads a contradiction the way its siblings do (#961).

    ``resolved_delta_ids_by_project`` is what the portfolio counts its offer
    from, and it collected delta ids into a set -- so two decisions in force on
    one delta collapsed into one ordinary "resolved" entry, exactly the
    integrity fault ``effective_dispositions`` and ``live_delta_status`` refuse
    to answer by taking whichever the order reaches first.  Only the
    record-decision role or a corrupt import can write the second row; no
    command will.
    """

    delta = _delta(session, project)
    saved = resolve_review_packet(
        session,
        _packet(
            project,
            (
                PacketChildRequest(
                    delta_id=delta.id,
                    outcome=KEEP_CURRENT,
                    observed_source_revision=delta.source_revision,
                ),
            ),
        ),
    )
    assert saved.status == SAVED
    # One effective disposition: the delta is in the resolved set exactly once.
    assert resolved_delta_ids_by_project(session, (project.id,)) == {
        project.id: {delta.id}
    }

    # A second in-force disposition on the same delta, written as the only
    # principal the guard trigger admits.
    with as_role(session, RECORD_DECISION_ROLE):
        session.execute(
            text(
                "insert into delta_dispositions ("
                "project_id, delta_id, generation, disposition,"
                " decided_by_principal, decided_at"
                ") values (:project_id, :delta_id, 1, 'reject', :principal, :at)"
            ),
            {
                "project_id": project.id,
                "delta_id": delta.id,
                "principal": ALICE.subject,
                "at": DECIDED_AT,
            },
        )

    with pytest.raises(ContradictoryDeltaResolution) as contradiction:
        resolved_delta_ids_by_project(session, (project.id,))
    assert contradiction.value.delta_id == delta.id
    assert contradiction.value.generations == (0, 1)


def test_undo_of_a_deferral_only_packet_writes_no_revision(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project)
    saved = resolve_review_packet(session, _packet(project, (_defer_child(delta),)))
    assert saved.status == SAVED
    before = _revision_count(session, project)

    undone = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=ALICE,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )

    assert undone.status == REVERSED
    assert undone.revision_id is None
    assert _revision_count(session, project) == before
    assert live_delta_status(session, delta.id) == "open"


def test_undo_is_refused_when_a_later_act_depends_on_a_result(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-n.xlsx")
    accepted, accepted_segment = rendition.capture(
        fact_type="station_from", value="1149+00"
    )
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    applied = _delta(session, project, field="station_from", baseline_revision=baseline)
    saved = resolve_review_packet(
        session,
        _packet(
            project,
            (_apply_child(applied, incoming, support),),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert saved.status == SAVED

    # A later attributable decision moves the same field again.
    correction_delta = _delta(
        session,
        project,
        field="station_from",
        accepted_value="1200+00",
        proposed_value="1149+00",
        source_revision="rev-correction",
    )
    correction_support = _support(session, project, accepted, accepted_segment)
    later = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project.id,
            delta_id=correction_delta.id,
            action=ACCEPT,
            principal=BOB,
            idempotency_key=f"correction:{uuid4().hex[:8]}",
            decided_at=DECIDED_AT,
            observed_accepted_revision_id=saved.revision_id,
            record_effects=(RecordEffect(fact_id=accepted.id),),
            support_assessment_ids=(correction_support.id,),
        ),
    )
    assert later.status == RESOLVED

    refused = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )

    assert refused.status == REFUSED
    assert refused.refusal.reason == "later_act_depends"
    assert packet_reversal(session, saved.receipt_id) is None


def test_a_later_correction_targets_one_child_and_keeps_the_original_receipt(
    session: Session, project: Project
) -> None:
    """Correction is a later decision on a new delta, never a rewritten receipt."""

    rendition = _rendition(session, project, "ucm-o.xlsx")
    accepted, accepted_segment = rendition.capture(
        fact_type="station_from", value="1149+00"
    )
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    other_incoming, other_segment = rendition.capture(
        fact_type="station_to", value="1260+00"
    )
    other_support = _support(session, project, other_incoming, other_segment)
    wrong = _delta(session, project, field="station_from", baseline_revision=baseline)
    right = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )
    saved = resolve_review_packet(
        session,
        _packet(
            project,
            (
                _apply_child(wrong, incoming, support),
                _apply_child(right, other_incoming, other_support),
            ),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert saved.status == SAVED

    correction_delta = _delta(
        session,
        project,
        field="station_from",
        accepted_value="1200+00",
        proposed_value="1149+00",
        source_revision="rev-correction",
    )
    correction_support = _support(session, project, accepted, accepted_segment)
    correction = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project.id,
            delta_id=correction_delta.id,
            action=ACCEPT,
            principal=BOB,
            idempotency_key=f"correction:{uuid4().hex[:8]}",
            decided_at=DECIDED_AT,
            observed_accepted_revision_id=saved.revision_id,
            record_effects=(RecordEffect(fact_id=accepted.id),),
            support_assessment_ids=(correction_support.id,),
        ),
    )

    assert correction.status == RESOLVED
    # Only the corrected child moved.
    effective = {
        row.fact_type: row.fact_id
        for row in session.scalars(
            select(FactDecision).where(
                FactDecision.project_id == project.id,
                FactDecision.superseded_by.is_(None),
            )
        ).all()
    }
    assert effective["station_from"] == accepted.id
    assert effective["station_to"] == other_incoming.id
    # And the original packet act is exactly as it was recorded.
    receipt = session.get(DeltaReviewPacketReceipt, saved.receipt_id)
    assert receipt.revision_id == saved.revision_id
    assert receipt.decided_by_principal == ALICE.subject
    assert [row.delta_id for row in packet_children(session, receipt.id)] == [
        wrong.id,
        right.id,
    ]
    original = session.get(DeltaRecordDecision, saved.children[0].decision_id)
    assert original.decided_by_principal == ALICE.subject
    assert original.revision_id == saved.revision_id


# --- The versioned event contract (#558) ----------------------------------


def test_packet_save_and_follow_up_plan_creation_emit_versioned_events(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-p.xlsx")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    applied = _delta(session, project, field="station_from")
    coordinated = _delta(
        session,
        project,
        field="external_org",
        accepted_value="City Water",
        proposed_value="Regional Water",
    )
    binding = default_binding(
        code_revision="git:abc123",
        source_configuration={"family": "ucm-workbook"},
        connector_configuration={"kind": "upload"},
        enabled_feature_flags=("adopted_baseline",),
    )

    with capture_events() as collector:
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    _apply_child(applied, incoming, support),
                    PacketChildRequest(
                        delta_id=coordinated.id,
                        outcome=NEEDS_COORDINATION,
                        observed_source_revision=coordinated.source_revision,
                        coordination=CoordinationRequest(
                            question="Did the utility owner actually change?",
                            responsible_organization="Regional Water",
                            return_date=RETURNS_AT,
                        ),
                    ),
                ),
            ),
            binding=binding,
        )

    assert result.status == SAVED
    (save,) = collector.by_family(EventFamily.PACKET_SAVE)
    (plan,) = collector.by_family(EventFamily.FOLLOW_UP_PLAN_CREATION)
    for event in (save, plan):
        assert event.version
        assert event.binding.code_revision == "git:abc123"
        assert event.binding.product_revision
        # Bound to the packetizer rule version the act actually used.
        assert event.binding.packetizer_rules_version == RULE_VERSION
        assert event.binding.source_configuration == {"family": "ucm-workbook"}
        assert event.binding.connector_configuration == {"kind": "upload"}
        assert event.binding.template_identity
        assert event.binding.mapping_identity
        assert event.binding.enabled_feature_flags == ("adopted_baseline",)
    assert save.payload["outcome"] == SAVED
    assert save.payload["receipt_id"] == result.receipt_id
    assert save.payload["revision_id"] == result.revision_id
    assert save.payload["child_count"] == 2
    assert save.payload["outcome_counts"][APPLY] == 1
    assert save.payload["outcome_counts"][NEEDS_COORDINATION] == 1
    assert plan.payload["follow_up_plan_id"] == result.children[1].follow_up_plan_id
    assert plan.payload["delta_id"] == coordinated.id
    # Each child decision keeps its own #519 event beside the packet's.
    assert len(collector.by_family(EventFamily.CHILD_DECISION)) == 1


def test_a_refused_packet_is_emitted_as_a_versioned_event_too(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, source_revision="rev-7")

    with capture_events() as collector:
        result = resolve_review_packet(
            session,
            _packet(
                project,
                (
                    PacketChildRequest(
                        delta_id=delta.id,
                        outcome=KEEP_CURRENT,
                        observed_source_revision="rev-6",
                    ),
                ),
            ),
        )

    assert result.status == REFUSED
    (save,) = collector.by_family(EventFamily.PACKET_SAVE)
    assert save.payload["outcome"] == REFUSED
    assert save.payload["refusal_reason"] == "source_version_mismatch"
    assert save.payload["wrote_revision"] is False


# --- Authority ------------------------------------------------------------


def test_a_packet_act_cannot_be_written_outside_the_command(
    session: Session, project: Project
) -> None:
    """Not even the schema owner may record a packet act raw."""

    with pytest.raises(DBAPIError) as refused:
        session.execute(
            text(
                "insert into delta_review_packet_receipts ("
                "project_id, grouping_rule_version, grouping_key_kind, "
                "grouping_key, decided_by_principal, idempotency_key, decided_at"
                ") values (:project_id, 'v1', 'source_revision', 'rev-1',"
                " 'local:alice', :key, :at)"
            ),
            {"project_id": project.id, "key": uuid4().hex, "at": DECIDED_AT},
        )

    assert "resolve_delta:unauthorized_writer" in str(refused.value)
    session.rollback()


def test_a_recorded_packet_act_is_never_updated_or_deleted(
    session: Session, project: Project
) -> None:
    """Two layers, and neither of them is Python.

    The record-decision role holds ``select, insert`` and nothing else, so
    PostgreSQL refuses an update or delete on privilege alone.  The guard
    trigger then holds the same rule against anyone who does hold the
    privilege, the schema owner included.
    """

    delta = _delta(session, project)
    saved = resolve_review_packet(session, _packet(project, (_defer_child(delta),)))
    assert saved.status == SAVED
    statements = (
        "update delta_review_packet_receipts set grouping_key = 'rewritten' "
        "where id = :id",
        "delete from delta_review_packet_receipts where id = :id",
    )

    # The owner of the schema is refused by the trigger.
    for statement in statements:
        with pytest.raises(DBAPIError) as refused:
            with session.begin_nested():
                session.execute(text(statement), {"id": saved.receipt_id})
        assert "resolve_delta:unauthorized_writer" in str(refused.value)

    # The role the commands run as holds no update or delete at all.
    for statement in statements:
        with as_role(session, RECORD_DECISION_ROLE):
            with pytest.raises(DBAPIError) as refused:
                with session.begin_nested():
                    session.execute(text(statement), {"id": saved.receipt_id})
        assert "permission denied" in str(refused.value)

    session.expire_all()
    assert (
        session.get(DeltaReviewPacketReceipt, saved.receipt_id).grouping_key
        == SOURCE_REVISION
    )


# --- Undo returns the question, and the question can be decided (#948) -----


def _second_apply(
    session: Session,
    project: Project,
    delta: ProposedDelta,
    fact: Fact,
    support,
    *,
    observed_accepted_revision_id: int | None = None,
    generation: int = 1,
):
    """A whole second guided Save on the delta an Undo returned to Review."""

    return resolve_review_packet(
        session,
        _packet(
            project,
            (
                _apply_child(
                    delta,
                    fact,
                    support,
                    observed_decision_generation=generation,
                ),
            ),
            principal=BOB,
            observed_accepted_revision_id=observed_accepted_revision_id,
        ),
    )


def test_undo_returns_the_applied_question_and_a_second_decision_lands(
    session: Session, project: Project
) -> None:
    """#948, and the half of it that is not a reading change.

    ADR-0035 says Undo "returns the Extracted Proposal", and a screen that
    reoffers a proposal whose next Save is structurally impossible has not
    returned it. So this walks the whole way round: apply, undo, watch the
    question come back to the standing set Review counts its offer from, and
    then *decide it again* and watch that decision reach the accepted record.

    The first decision is untouched throughout. It is still the same row,
    saying what it said, in generation 0; the successor took generation 1.
    """

    rendition = _rendition(session, project, "ucm-undo-a.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project, field="station_from", baseline_revision=baseline)

    saved = resolve_review_packet(
        session,
        _packet(
            project,
            (_apply_child(delta, incoming, support),),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert saved.status == SAVED
    first = session.get(DeltaDisposition, saved.children[0].disposition_id)
    recorded = (first.id, first.disposition, first.decided_at, first.generation)
    assert first.generation == 0
    assert live_delta_status(session, delta.id) == "resolved"

    undone = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )
    assert undone.status == REVERSED

    # The question is back: both the clockless authority every write guard
    # consults and the standing set Review counts its offer from say so.
    assert live_delta_status(session, delta.id) == "open"
    assert delta.id not in resolved_delta_ids_by_project(session, (project.id,))[
        project.id
    ]
    assert delta.id in read_open_deltas(
        session, project_id=project.id, as_of=RETURNS_AT
    ).actionable_delta_ids

    # And it can actually be answered. This is the requirement the reading
    # change alone cannot meet: the unique index, the Python pre-check and
    # five plpgsql guards all had to agree that a successor decision is lawful.
    again = _second_apply(
        session,
        project,
        delta,
        incoming,
        support,
        observed_accepted_revision_id=undone.revision_id,
    )
    assert again.status == SAVED, again
    assert live_delta_status(session, delta.id) == "resolved"
    assert delta.id in resolved_delta_ids_by_project(session, (project.id,))[project.id]

    # The second decision moved the accepted record, by its own revision.
    effective = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.fact_type == "station_from",
            FactDecision.superseded_by.is_(None),
        )
    ).all()
    assert [row.fact_id for row in effective] == [incoming.id]
    assert effective[0].revision_id == again.revision_id

    # Nothing was deleted and nothing was rewritten: two decisions stand in
    # history, the first exactly as it was recorded, in its own generation.
    history = session.scalars(
        select(DeltaDisposition)
        .where(DeltaDisposition.delta_id == delta.id)
        .order_by(DeltaDisposition.generation)
    ).all()
    assert [row.generation for row in history] == [0, 1]
    session.expire(first)
    assert (
        first.id,
        first.disposition,
        first.decided_at,
        first.generation,
    ) == recorded
    assert history[1].decided_by_principal == BOB.subject


def test_undo_of_a_keep_current_returns_the_question_it_had_rejected(
    session: Session, project: Project
) -> None:
    """The second row of #948's table.

    Keep current is the customer saying the accepted value stands. Undoing it
    withdraws that answer, so the difference the source proposed is a question
    again -- and one they may answer the other way, which is what the second
    Save here proves.
    """

    rendition = _rendition(session, project, "ucm-undo-b.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project, field="station_from", baseline_revision=baseline)

    saved = resolve_review_packet(
        session,
        _packet(
            project,
            (
                PacketChildRequest(
                    delta_id=delta.id,
                    outcome=KEEP_CURRENT,
                    observed_source_revision=delta.source_revision,
                ),
            ),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert saved.status == SAVED
    assert live_delta_status(session, delta.id) == "resolved"

    undone = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )

    assert undone.status == REVERSED
    assert live_delta_status(session, delta.id) == "open"
    # The rejection no longer settles it, and the other answer is available.
    again = _second_apply(
        session,
        project,
        delta,
        incoming,
        support,
        observed_accepted_revision_id=undone.revision_id or baseline,
    )
    assert again.status == SAVED, again
    assert [
        row.disposition
        for row in session.scalars(
            select(DeltaDisposition)
            .where(DeltaDisposition.delta_id == delta.id)
            .order_by(DeltaDisposition.generation)
        ).all()
    ] == ["reject", "accept"]


def test_undo_of_a_needs_coordination_child_leaves_the_question_actionable(
    session: Session, project: Project
) -> None:
    """The third row of #948's table.

    A Follow-up Plan never settled the change -- it recorded that someone owes
    an answer -- so undoing the act undoes the plan and the unresolved question
    stays exactly what it was: actionable, and decidable now without waiting
    for the ask the coordinator withdrew.
    """

    rendition = _rendition(session, project, "ucm-undo-c.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project, field="station_from", baseline_revision=baseline)

    saved = resolve_review_packet(
        session,
        _packet(
            project,
            (
                PacketChildRequest(
                    delta_id=delta.id,
                    outcome=NEEDS_COORDINATION,
                    observed_source_revision=delta.source_revision,
                    coordination=CoordinationRequest(
                        question="Which station does the utility stand behind?",
                        responsible_organization="City Water",
                        return_date=RETURNS_AT,
                        affected_scope={"subject": SUBJECT, "field": "station_from"},
                        evidence_support_assessment_ids=(support.id,),
                    ),
                ),
            ),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert saved.status == SAVED
    plan_id = saved.children[0].follow_up_plan_id
    assert live_delta_status(session, delta.id) == "open"

    undone = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )

    assert undone.status == REVERSED
    # The plan is no longer in force, and it is still in history.
    assert session.scalars(undone_follow_up_plan_ids((project.id,))).all() == [plan_id]
    assert session.get(DeltaFollowUpPlan, plan_id) is not None
    # The question was never settled and is still answerable -- as its *first*
    # decision. An undone plan is not a decision that was undone, so it opens
    # no new generation and a Save composed against generation 0 is the right
    # one (#948).
    assert live_delta_status(session, delta.id) == "open"
    again = _second_apply(
        session,
        project,
        delta,
        incoming,
        support,
        observed_accepted_revision_id=undone.revision_id or baseline,
        generation=0,
    )
    assert again.status == SAVED, again
    assert (
        session.get(DeltaDisposition, again.children[0].disposition_id).generation == 0
    )


def test_an_undone_later_decision_no_longer_blocks_the_earlier_undo(
    session: Session, project: Project
) -> None:
    """The fifth row of #948's table, read forwards and then backwards.

    Undo never cascades: a packet that left a change open refuses to be undone
    once someone has decided that change. The rule is about a decision that
    *stands*, though, so when the later decision is itself undone the earlier
    act becomes undoable again -- without which one withdrawn decision would
    freeze every act before it for good.
    """

    rendition = _rendition(session, project, "ucm-undo-d.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project, field="station_from", baseline_revision=baseline)

    scheduled = resolve_review_packet(
        session,
        _packet(
            project,
            (_defer_child(delta),),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert scheduled.status == SAVED, scheduled.refusals
    # A released deferral is not a reversed decision either: this is still the
    # delta's first semantic decision, in generation 0 (#948).
    decided = _second_apply(
        session,
        project,
        delta,
        incoming,
        support,
        observed_accepted_revision_id=baseline,
        generation=0,
    )
    assert decided.status == SAVED, decided.refusals

    refused = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=scheduled.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )
    assert refused.status == REFUSED
    assert refused.refusal.reason == "later_act_depends"

    # Undo the decision that depended on it, and the earlier act is free.
    assert (
        reverse_review_packet(
            session,
            project_id=project.id,
            receipt_id=decided.receipt_id,
            principal=BOB,
            reversed_at=DECIDED_AT,
            idempotency_key=f"undo:{uuid4().hex[:10]}",
        ).status
        == REVERSED
    )
    released = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=scheduled.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )

    assert released.status == REVERSED
    assert live_delta_status(session, delta.id) == "open"


def test_undo_does_not_revive_a_proposal_a_newer_source_version_superseded(
    session: Session, project: Project
) -> None:
    """The sixth row of #948's table, on the supersession half.

    Undo returns a question that is still applicable. A change a newer source
    version replaced is not one of those: reviving it would put a comparison
    against a withdrawn revision back in front of the coordinator. The
    supersession is the applicable terminal reason and it survives the Undo.
    """

    delta = _delta(session, project)
    replacement = _delta(session, project, source_revision="rev-2")
    saved = resolve_review_packet(session, _packet(project, (_defer_child(delta),)))
    assert saved.status == SAVED
    record_delta_supersession(
        session,
        project_id=project.id,
        prior_delta_id=delta.id,
        superseding_delta_id=replacement.id,
    )
    assert live_delta_status(session, delta.id) == "superseded"

    undone = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )

    assert undone.status == REVERSED
    assert live_delta_status(session, delta.id) == "superseded"
    assert delta.id not in read_open_deltas(
        session, project_id=project.id, as_of=RETURNS_AT
    ).actionable_delta_ids


def _accepted_fact_id(session: Session, project: Project, field: str) -> int | None:
    """The capture the accepted record stands on, read straight off it."""

    rows = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.fact_type == field,
            FactDecision.superseded_by.is_(None),
        )
    ).all()
    included = [row for row in rows if row.disposition != "do_not_add"]
    assert len(included) <= 1, [row.id for row in included]
    return included[0].fact_id if included else None


def test_a_change_can_be_decided_undone_and_decided_again_the_other_way(
    session: Session, project: Project
) -> None:
    """Apply, Undo, Apply, Undo, Keep current -- the whole sequence (#948).

    Each generation is the database's own: it is assigned under the terminal
    lock from the decisions already reversed, never taken from the caller, and
    the caller only says which one it composed its submission against. What is
    checked between the steps is the accepted value itself rather than a count
    of revisions, because "the record went back" is a statement about what it
    holds.
    """

    rendition = _rendition(session, project, "ucm-undo-e.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project, field="station_from", baseline_revision=baseline)
    assert _accepted_fact_id(session, project, "station_from") == accepted.id

    first = resolve_review_packet(
        session,
        _packet(
            project,
            (_apply_child(delta, incoming, support),),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert first.status == SAVED, first.refusals
    assert _accepted_fact_id(session, project, "station_from") == incoming.id

    first_undo = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=first.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )
    assert first_undo.status == REVERSED
    assert _accepted_fact_id(session, project, "station_from") == accepted.id
    assert live_delta_status(session, delta.id) == "open"

    second = _second_apply(
        session,
        project,
        delta,
        incoming,
        support,
        observed_accepted_revision_id=first_undo.revision_id,
        generation=1,
    )
    assert second.status == SAVED, second.refusals
    assert _accepted_fact_id(session, project, "station_from") == incoming.id

    second_undo = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=second.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )
    assert second_undo.status == REVERSED
    assert _accepted_fact_id(session, project, "station_from") == accepted.id
    assert live_delta_status(session, delta.id) == "open"

    kept = resolve_review_packet(
        session,
        _packet(
            project,
            (
                PacketChildRequest(
                    delta_id=delta.id,
                    outcome=KEEP_CURRENT,
                    observed_source_revision=delta.source_revision,
                    observed_decision_generation=2,
                ),
            ),
            principal=BOB,
            observed_accepted_revision_id=second_undo.revision_id,
        ),
    )

    assert kept.status == SAVED, kept.refusals
    assert live_delta_status(session, delta.id) == "resolved"
    assert _accepted_fact_id(session, project, "station_from") == accepted.id
    # Three decisions, in three generations, none of them rewritten and none
    # of them deleted -- and only the last one in force.
    history = session.scalars(
        select(DeltaDisposition)
        .where(DeltaDisposition.delta_id == delta.id)
        .order_by(DeltaDisposition.generation)
    ).all()
    assert [(row.generation, row.disposition) for row in history] == [
        (0, "accept"),
        (1, "accept"),
        (2, "reject"),
    ]
    assert delta.id in resolved_delta_ids_by_project(session, (project.id,))[project.id]


def test_a_save_composed_before_the_decision_was_undone_is_refused(
    session: Session, project: Project
) -> None:
    """#948's stale form, which the returned values would otherwise hide.

    A page rendered while the change was undecided names generation 0. If it is
    submitted after someone else has decided the change and undone it, every
    value on it reads correct again -- the accepted record is back where it
    was -- and only the history has moved. The command assigns the generation
    itself under the terminal lock, so it is the one that says no.
    """

    rendition = _rendition(session, project, "ucm-undo-f.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project, field="station_from", baseline_revision=baseline)

    saved = resolve_review_packet(
        session,
        _packet(
            project,
            (_apply_child(delta, incoming, support),),
            observed_accepted_revision_id=baseline,
        ),
    )
    assert saved.status == SAVED
    undone = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=BOB,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )
    assert undone.status == REVERSED
    assert _accepted_fact_id(session, project, "station_from") == accepted.id

    with nothing_written(session, project.id):
        stale = _second_apply(
            session,
            project,
            delta,
            incoming,
            support,
            observed_accepted_revision_id=undone.revision_id,
            generation=0,
        )

    assert stale.status == REFUSED
    assert stale.refusals[0].reason == "stale_decision_generation"
    assert live_delta_status(session, delta.id) == "open"
    # And the page opened again saves, because it names what stands now.
    assert (
        _second_apply(
            session,
            project,
            delta,
            incoming,
            support,
            observed_accepted_revision_id=undone.revision_id,
            generation=1,
        ).status
        == SAVED
    )


def test_a_generation_the_caller_invents_cannot_step_over_a_standing_decision(
    session: Session, project: Project
) -> None:
    """The browser cannot choose a generation to get past an effective decision.

    ``unique (delta_id, generation)`` on its own would admit a second
    unreversed decision in a generation nobody had reached. The generation is
    assigned by the command under the terminal lock and the standing decision
    is refused first, so a caller naming generation 1 while generation 0 still
    stands is told the change is resolved rather than given a new slot (#948).
    """

    rendition = _rendition(session, project, "ucm-undo-g.xlsx")
    accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted, f"adopt-{project.id}")
    incoming, segment = rendition.capture(fact_type="station_from", value="1200+00")
    support = _support(session, project, incoming, segment)
    delta = _delta(session, project, field="station_from", baseline_revision=baseline)
    assert (
        resolve_review_packet(
            session,
            _packet(
                project,
                (_apply_child(delta, incoming, support),),
                observed_accepted_revision_id=baseline,
            ),
        ).status
        == SAVED
    )

    with nothing_written(session, project.id):
        invented = _second_apply(
            session,
            project,
            delta,
            incoming,
            support,
            observed_accepted_revision_id=baseline,
            generation=1,
        )

    assert invented.status == REFUSED
    assert invented.refusals[0].reason == "already_resolved"
    assert [
        row.generation
        for row in session.scalars(
            select(DeltaDisposition).where(DeltaDisposition.delta_id == delta.id)
        ).all()
    ] == [0]



@dataclass(frozen=True)
class _Returned:
    """One committed project whose only change was decided and then undone."""

    project_id: int
    delta_id: int
    fact_id: int
    support_id: int
    source_revision: str
    revision_id: int


def _seed_returned_question(factory) -> _Returned:
    """Apply one change and undo it, committed, so the question is back."""

    with factory() as seeding:
        project = Project(
            slug=f"project-{uuid4().hex[:8]}", name="Project", is_synthetic=True
        )
        seeding.add(project)
        seeding.flush()
        rendition = _rendition(seeding, project, f"ucm-{uuid4().hex[:6]}.xlsx")
        accepted, _ = rendition.capture(fact_type="station_from", value="1149+00")
        baseline = _adopt(seeding, project, accepted, f"adopt-{project.id}")
        incoming, segment = rendition.capture(
            fact_type="station_from", value="1200+00"
        )
        assessment = _support(seeding, project, incoming, segment)
        delta = _delta(
            seeding, project, field="station_from", baseline_revision=baseline
        )
        saved = resolve_review_packet(
            seeding,
            _packet(
                project,
                (_apply_child(delta, incoming, assessment),),
                observed_accepted_revision_id=baseline,
            ),
        )
        assert saved.status == SAVED, saved.refusals
        undone = reverse_review_packet(
            seeding,
            project_id=project.id,
            receipt_id=saved.receipt_id,
            principal=BOB,
            reversed_at=DECIDED_AT,
            idempotency_key=f"undo:{uuid4().hex[:10]}",
        )
        assert undone.status == REVERSED
        seeding.commit()
        return _Returned(
            project_id=int(project.id),
            delta_id=int(delta.id),
            fact_id=int(incoming.id),
            support_id=int(assessment.id),
            source_revision=delta.source_revision,
            revision_id=int(undone.revision_id),
        )


@pytest.mark.slow
def test_two_coordinators_answering_the_returned_question_leave_one_decision(
    runtime_database,
) -> None:
    """The successor decision is still one decision (#948).

    Two coordinators open the question an Undo returned and both save. The
    generation is assigned inside the command, under the advisory lock every
    terminal writer takes, so the two serialise: one writes generation 1 and
    the other is refused by name. Nothing here can be proved by a
    rollback-scoped test -- neither transaction sees the other's uncommitted
    rows -- so it uses the harness's own isolated database.
    """

    factory = runtime_database.session_factory
    for _ in range(4):
        seeded = _seed_returned_question(factory)
        both_ready = Barrier(2, timeout=30)

        def answer() -> object:
            with factory() as deciding:
                deciding.execute(select(func.txid_current()))
                both_ready.wait()
                outcome = resolve_delta(
                    deciding,
                    ChildDecisionRequest(
                        project_id=seeded.project_id,
                        delta_id=seeded.delta_id,
                        action=ACCEPT,
                        principal=ALICE,
                        decided_at=DECIDED_AT,
                        idempotency_key=f"apply:{uuid4().hex[:10]}",
                        observed_accepted_revision_id=seeded.revision_id,
                        observed_decision_generation=1,
                        record_effects=(RecordEffect(fact_id=seeded.fact_id),),
                        support_assessment_ids=(seeded.support_id,),
                    ),
                )
                if outcome.status == RESOLVED:
                    deciding.commit()
                else:
                    deciding.rollback()
                return outcome

        with ThreadPoolExecutor(max_workers=2) as pool:
            both = [pool.submit(answer), pool.submit(answer)]
            outcomes = [one.result() for one in both]

        with factory() as verify:
            generations = [
                row.generation
                for row in verify.scalars(
                    select(DeltaDisposition)
                    .where(DeltaDisposition.delta_id == seeded.delta_id)
                    .order_by(DeltaDisposition.generation)
                ).all()
            ]
            assert generations == [0, 1], generations
            assert live_delta_status(verify, seeded.delta_id) == "resolved"

        assert sorted(one.status for one in outcomes) == [REFUSED, RESOLVED]
        (refused,) = [one for one in outcomes if one.status == REFUSED]
        assert refused.refusal.reason in {
            "already_resolved",
            "stale_decision_generation",
        }, refused.refusal
