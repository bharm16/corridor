"""Native Follow-up Plan reads shared by review and publication consumers.

A question raised by Needs coordination is a separately identified human plan,
not an accepted Constraint owner or value. Keeping this reader below packet and
publication modules preserves that boundary and avoids importing the full
constraint population into a review screen. Reversals, accepted revisions and
an explicit review cutoff determine which questions may be shown.
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from corridor.models import (DeltaFollowUpPlan, ProposedDelta, DeltaRecordDecision,
    DeltaSupersession, DeltaFollowUpPlanEvidence, SupportAssessmentSource,
    DeltaReviewPacketChild, DeltaReviewPacketReversal)


@dataclass(frozen=True)
class AcceptedFollowUpPlan:
    plan_id: int
    delta_id: int
    revision_id: int
    target_subject_identity: str
    open_question: str
    responsible_principal: str | None
    responsible_organization: str | None
    return_date: datetime | None
    recorded_by: str
    recorded_at: datetime
    support_assessment_ids: tuple[int, ...]
    source_segment_ids: tuple[int, ...]


def read_adopted_follow_up_plans(session, project_id, revision_id, *, current, as_of=None):
    """Read questions on unresolved deltas without making them accepted values.

    Needs coordination owns a native Follow-up Plan (#526). Its responsible
    party is not the accepted Constraint's assigned person. Current work also
    excludes source-superseded deltas; historical reads name plans recorded by
    the requested accepted revision and do not infer historical source workflow
    supersession from a later clock.
    """
    query = select(DeltaFollowUpPlan, ProposedDelta.target_subject_identity).join(
        ProposedDelta, (ProposedDelta.id == DeltaFollowUpPlan.delta_id)
        & (ProposedDelta.project_id == DeltaFollowUpPlan.project_id)).where(
        DeltaFollowUpPlan.project_id == project_id, DeltaFollowUpPlan.revision_id <= revision_id)
    resolved = select(DeltaRecordDecision.delta_id).where(
        DeltaRecordDecision.project_id == project_id, DeltaRecordDecision.revision_id <= revision_id)
    if as_of is not None:
        resolved = resolved.where(DeltaRecordDecision.decided_at <= as_of)
    query = query.where(~DeltaFollowUpPlan.delta_id.in_(resolved))
    if current:
        superseded = select(DeltaSupersession.prior_delta_id).where(DeltaSupersession.project_id == project_id)
        if as_of is not None:
            superseded = superseded.where(DeltaSupersession.superseded_at <= as_of)
        query = query.where(~DeltaFollowUpPlan.delta_id.in_(superseded))
    if as_of is not None:
        if as_of.tzinfo is None:
            raise ValueError("Follow-up Plan reading cutoff must be timezone-aware")
        query = query.where(DeltaFollowUpPlan.recorded_at <= as_of)
    reversed_plans = select(DeltaReviewPacketChild.follow_up_plan_id).join(DeltaReviewPacketReversal,
        DeltaReviewPacketReversal.receipt_id == DeltaReviewPacketChild.receipt_id).where(
        DeltaReviewPacketChild.project_id == project_id, DeltaReviewPacketChild.follow_up_plan_id.is_not(None),
        DeltaReviewPacketReversal.revision_id <= revision_id)
    if as_of is not None:
        reversed_plans = reversed_plans.where(DeltaReviewPacketReversal.reversed_at <= as_of)
    query = query.where(~DeltaFollowUpPlan.id.in_(reversed_plans))
    plans = tuple(session.execute(query.order_by(DeltaFollowUpPlan.id)))
    plan_ids = [plan.id for plan, _ in plans]
    evidence = {}
    segments = {}
    if plan_ids:
        for plan_id, assessment_id, segment_id in session.execute(select(
            DeltaFollowUpPlanEvidence.plan_id, DeltaFollowUpPlanEvidence.support_assessment_id,
            SupportAssessmentSource.source_segment_id).outerjoin(SupportAssessmentSource,
                (SupportAssessmentSource.support_assessment_id == DeltaFollowUpPlanEvidence.support_assessment_id)
                & (SupportAssessmentSource.project_id == DeltaFollowUpPlanEvidence.project_id))
            .where(DeltaFollowUpPlanEvidence.project_id == project_id, DeltaFollowUpPlanEvidence.plan_id.in_(plan_ids))
            .order_by(DeltaFollowUpPlanEvidence.ordinal, SupportAssessmentSource.ordinal)):
            evidence.setdefault(plan_id, set()).add(assessment_id)
            if segment_id is not None:
                segments.setdefault(plan_id, set()).add(segment_id)
    return tuple(AcceptedFollowUpPlan(plan.id, plan.delta_id, plan.revision_id, subject,
        plan.open_question, plan.responsible_principal, plan.responsible_organization,
        plan.return_date, plan.recorded_by_principal, plan.recorded_at,
        tuple(sorted(evidence.get(plan.id, ()))), tuple(sorted(segments.get(plan.id, ())))) for plan, subject in plans)


