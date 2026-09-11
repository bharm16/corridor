"""Native Follow-up Plan reads shared by review and publication consumers.

A question raised by Needs coordination is a separately identified human plan,
not an accepted Constraint owner or value. Keeping this reader below packet and
publication modules preserves that boundary and avoids importing the full
constraint population into a review screen. Reversals, accepted revisions and
an explicit review cutoff determine which questions may be shown.

**When a plan stops standing, decided once (#835).**  Three rules retire a
plan and all three live here: its Proposed Delta stopped being open, the one
packet act that recorded it was undone, or a coordinator closed it —
superseded by a corrected plan, or cancelled because no outside answer is
needed.  ``project_workflow.outstanding_follow_up`` reads the same two
subqueries, so the week and this reader cannot disagree about which asks still
stand.  Before the closure relation existed a corrected plan was simply a
second plan, and both were listed: two live outside asks for one question.

**One shape, one name.** ``AcceptedFollowUpPlan`` is the only reading type for
a retained Follow-up Plan, and it is defined here because this is the lowest
module every renderer of one can import: the internal report, the issue
renderers and the review screen. A second dataclass of the same name lived in
``issue_rendering`` with a different field for every value — ``assigned_to``,
``next_action``, ``action_due_date`` — none of which any retained record holds,
and nothing converted between the two, so the issue report's follow-up section
was structurally empty in every production path (#425, ADR-0084 section 1).
The fields below are exactly what ``delta_follow_up_plans`` retains.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from corridor.models import (DeltaFollowUpPlan, DeltaFollowUpPlanClosure, ProposedDelta,
    DeltaRecordDecision, DeltaSupersession, DeltaFollowUpPlanEvidence,
    SupportAssessmentSource, DeltaReviewPacketChild, DeltaReviewPacketReversal)


@dataclass(frozen=True)
class AcceptedFollowUpPlan:
    """One retained Follow-up Plan, in the words the record itself holds.

    ADR-0084 section 1 and ADR-0085 keep this apart from a Defer receipt: a
    plan names an **open question**, the party who owes the answer, and the
    date the question returns. It carries no next-action sentence and no
    internal assignee, because the coordinator never recorded either; a
    renderer that wants one has to compose it, and composing customer prose
    from an absent record is exactly what the report may not do. It has
    nowhere to put a proposed value either, so an unaccepted incoming value
    cannot reach a customer artifact through this type.
    """

    plan_id: int
    delta_id: int
    revision_id: int
    target_subject_identity: str
    target_field: str | None
    open_question: str
    responsible_principal: str | None
    responsible_organization: str | None
    return_date: datetime | None
    recorded_by: str
    recorded_at: datetime
    support_assessment_ids: tuple[int, ...] = ()
    source_segment_ids: tuple[int, ...] = ()

    @property
    def responsible(self) -> str:
        """Whoever owes the answer, in the order the row's own check allows."""

        return (self.responsible_principal or self.responsible_organization or "").strip()


def undone_follow_up_plan_ids(project_ids: Sequence[int], *, revision_id: int | None = None,
                              as_of: datetime | None = None):
    """The plans whose one recording packet act was undone, as a subquery.

    "An act that never stood raises no ask" is one rule, and it was written
    twice — here and in ``project_workflow.outstanding_follow_up`` — over the
    same two tables, so two renderings of one plan could disagree about a
    reversal. It is decided here once. ``revision_id`` and ``as_of`` bound it
    for an as-of reading; the current reading passes neither, because a
    reversal that has happened has happened.
    """

    query = select(DeltaReviewPacketChild.follow_up_plan_id).join(
        DeltaReviewPacketReversal,
        DeltaReviewPacketReversal.receipt_id == DeltaReviewPacketChild.receipt_id).where(
        DeltaReviewPacketChild.project_id.in_(tuple(project_ids)),
        DeltaReviewPacketChild.follow_up_plan_id.is_not(None))
    if revision_id is not None:
        query = query.where(DeltaReviewPacketReversal.revision_id <= revision_id)
    if as_of is not None:
        query = query.where(DeltaReviewPacketReversal.reversed_at <= as_of)
    return query


def closed_follow_up_plan_ids(project_ids: Sequence[int], *, as_of: datetime | None = None):
    """The plans a closure retired, as a subquery (#835).

    Beside ``undone_follow_up_plan_ids`` and for the same reason: "a plan that
    was superseded or cancelled raises no ask" is one rule, and the two readers
    that decide whether a plan is still waiting must not each write their own
    join over the closure relation. A plan is closed once — the relation's own
    unique constraint says so — so this is a plain membership test.

    ``as_of`` bounds it for an as-of reading against the instant the
    coordinator declared closing it; the current reading passes none, because
    a closure that has happened has happened.
    """

    query = select(DeltaFollowUpPlanClosure.plan_id).where(
        DeltaFollowUpPlanClosure.project_id.in_(tuple(project_ids)))
    if as_of is not None:
        query = query.where(DeltaFollowUpPlanClosure.closed_at <= as_of)
    return query


def plan_subject_identity(scope: Mapping[str, Any] | None, delta_subject: str) -> str:
    """The subject one plan affects: its own recorded scope, else its delta's."""

    return str((scope or {}).get("subject_identity") or delta_subject)


def plan_field(scope: Mapping[str, Any] | None, delta_field: str | None) -> str | None:
    """The field one plan affects: its own recorded scope, else its delta's."""

    return str((scope or {}).get("field") or "") or delta_field or None


def read_adopted_follow_up_plans(session, project_id, revision_id, *, current, as_of=None):
    """Read questions on unresolved deltas without making them accepted values.

    Needs coordination owns a native Follow-up Plan (#526). Its responsible
    party is not the accepted Constraint's assigned person. Current work also
    excludes source-superseded deltas; historical reads name plans recorded by
    the requested accepted revision and do not infer historical source workflow
    supersession from a later clock.
    """
    query = select(DeltaFollowUpPlan, ProposedDelta).join(
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
    query = query.where(~DeltaFollowUpPlan.id.in_(
        undone_follow_up_plan_ids((project_id,), revision_id=revision_id, as_of=as_of)))
    # A superseded or cancelled plan is no longer an outside ask (#835). It is
    # excluded here rather than by each caller, so the review screen, the issue
    # report and the week cannot disagree about which plans still stand.
    query = query.where(~DeltaFollowUpPlan.id.in_(
        closed_follow_up_plan_ids((project_id,), as_of=as_of)))
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
    return tuple(AcceptedFollowUpPlan(plan.id, plan.delta_id, plan.revision_id,
        plan_subject_identity(plan.affected_scope, delta.target_subject_identity),
        plan_field(plan.affected_scope, delta.target_field),
        plan.open_question, plan.responsible_principal, plan.responsible_organization,
        plan.return_date, plan.recorded_by_principal, plan.recorded_at,
        tuple(sorted(evidence.get(plan.id, ()))), tuple(sorted(segments.get(plan.id, ())))) for plan, delta in plans)
