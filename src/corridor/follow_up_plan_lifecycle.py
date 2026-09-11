"""The one home for what happens to a Follow-up Plan after it is recorded (#835).

#526 gave Needs coordination a recorded decision — the exact question, who owes
the answer, when it returns, the evidence that raised it — and gave it no way to
end.  ``delta_follow_up_plans`` is insert-only, and the two readers that decide
whether a plan is still waiting dropped one for exactly two reasons: its Proposed
Delta stopped being open, or the packet act that recorded it was reversed.  A
coordinator who had recorded the wrong question, named the wrong External
Organization, or no longer needed an outside answer had nothing to record.

**Appending a corrected plan was not the fix; it was the bug.**  Nothing in the
record said one plan replaced another, so the week listed both and the chase list
would contact somebody twice: *two live outside asks for one question*.  #835
adds the statement that one closed, and this module is the only place that
writes it.

**Two acts, and the difference between them is what the record must keep.**

``update_follow_up_plan`` records a *new* plan carrying the corrected question,
party and return date, then closes the old one as ``superseded`` naming the new.
Both in one transaction, so a reader never sees either a gap with no ask or two
asks at once.  The old plan is not edited: what a coordinator asked, and when,
stays exactly as recorded, and the correction stands beside it.

``cancel_follow_up_plan`` closes the plan with a structured reason and records no
successor.  The Proposed Delta is untouched and still open: cancelling the ask
says nobody outside owes an answer, never that the proposed change is settled.
Settling it is still a decision on the review screen.

**Neither writes a Project Record revision.**  ADR-0084 §1 leaves accept, edit
and reject as the only dispositions that write one.  A plan writes no
disposition and leaves the proposed value unaccepted; closing one changes the
accepted record even less.  The successor an update records cites the project's
**current accepted revision head** as the revision it was recorded against —
which is how ``native_follow_up_reading`` already reads that column, bounding an
as-of reading — rather than opening a revision this act has no business writing.

**It is not #834's Undo.**  ``reverse_review_packet`` says the recorded act never
stood: it appends a compensating revision, restores each predecessor decision,
and takes the packet's own revision and cited evidence back with it.  A closure
says the opposite — the ask was real, it was recorded correctly, and it is
finished.  Cancelling through the reversal would erase the fact that anybody was
ever asked, and would cascade into the record decisions the same packet made.

**What is deliberately absent.**  Nothing here sends anything.  A coordinator
sends from their own mail client and records what they sent; #652 holds the
relation for that and #837 owns the surface.  A reply from an External
Organization and a resolved record question stay distinct facts, and neither a
cancellation nor a supersession claims either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.delta_refusals import REFUSED, database_refusal_code
from corridor.models import (
    CANCELLATION_REASONS,
    DeltaFollowUpPlan,
    DeltaFollowUpPlanClosure,
    ProjectRecordRevision,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.review_packets import record_follow_up_plan


CANCELLED = "cancelled"
SUPERSEDED = "superseded"

#: The structured cancellation reasons, in the coordinator's own words.  The
#: tokens are the record's (``models.delta.CANCELLATION_REASONS``); these are
#: what a person picks between, kept here rather than in a template so the
#: screen cannot offer a reason the relation would refuse.
CANCELLATION_REASON_WORDS: dict[str, str] = {
    "answered_another_way": "The answer arrived some other way",
    "no_longer_needed": "This no longer needs an outside answer",
    "raised_in_error": "This should not have been asked",
    "asked_of_the_wrong_party": "The wrong party was asked",
}


class FollowUpPlanLifecycleRefused(ValueError):
    """A closure this module refused before anything was read for writing."""


@dataclass(frozen=True, slots=True)
class ClosureRefusal:
    """Why nothing was written, in the declared vocabulary's own terms.

    Deliberately not ``delta_resolution.Refusal``: that type describes a
    refusal *about a Proposed Delta* and requires one, carrying the subject,
    the field, the proposed and accepted values and what the Work List should
    re-read.  A closure refuses about a plan, and manufacturing a delta
    identity to fit the other shape would say the delta was what refused.
    """

    status: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class ClosureOutcome:
    """What one attempt to close a Follow-up Plan produced."""

    status: str
    plan_id: int
    closure_kind: str
    closure_id: int | None = None
    successor_plan_id: int | None = None
    created: bool = False
    refusal: ClosureRefusal | None = None

    @property
    def closed(self) -> bool:
        return self.status == CLOSED


#: The one status this module returns on success.  It is not one of
#: ``delta_refusals``' outcome statuses: no delta was resolved, deferred or
#: left stale, and borrowing one of those words would say something about the
#: Proposed Delta that did not happen.
CLOSED = "closed"


def cancel_follow_up_plan(
    session: Session,
    *,
    project_id: int,
    plan_id: int,
    principal: HumanPrincipal,
    cancellation_reason: str,
    closed_at: datetime,
    note: str | None = None,
    idempotency_key: str,
) -> ClosureOutcome:
    """Record that no outside answer is needed for this question any more.

    The Proposed Delta stays open. Cancelling the ask is not deciding the
    change, and this writes nothing that could be read as having decided it.
    """

    if cancellation_reason not in CANCELLATION_REASONS:
        raise FollowUpPlanLifecycleRefused(
            "a cancelled Follow-up Plan records one of the structured reasons: "
            + ", ".join(CANCELLATION_REASONS)
        )
    return _close(
        session,
        project_id=project_id,
        plan_id=plan_id,
        principal=principal,
        closure_kind=CANCELLED,
        successor_plan_id=None,
        cancellation_reason=cancellation_reason,
        note=note,
        closed_at=closed_at,
        idempotency_key=idempotency_key,
    )


def update_follow_up_plan(
    session: Session,
    *,
    project_id: int,
    plan_id: int,
    principal: HumanPrincipal,
    open_question: str,
    responsible_principal: str | None = None,
    responsible_organization: str | None = None,
    return_date: datetime | None = None,
    note: str | None = None,
    closed_at: datetime,
    idempotency_key: str,
) -> ClosureOutcome:
    """Replace one plan with a corrected one, as one act.

    The successor is a plan in its own right — same Proposed Delta, its own
    question, party, return date and recorder — and the closure is what makes
    it *the* ask rather than a second one. Both are written inside one nested
    transaction: a reader between them would otherwise see either no ask or
    two.
    """

    acting = require_human_principal(principal)
    plan = session.get(DeltaFollowUpPlan, plan_id)
    if plan is None or plan.project_id != project_id:
        return ClosureOutcome(
            status=REFUSED,
            plan_id=plan_id,
            closure_kind=SUPERSEDED,
            refusal=ClosureRefusal(
                status=REFUSED,
                reason="cross_project_plan",
                detail=(
                    f"Follow-up Plan {plan_id} is not this project's to close"
                ),
            ),
        )
    if not open_question.strip():
        raise FollowUpPlanLifecycleRefused(
            "a corrected Follow-up Plan records the exact open question"
        )
    revision_id = session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == project_id
        )
    )
    if revision_id is None:
        # Unreachable while a plan exists: a Needs coordination packet writes a
        # revision, and the plan's own foreign key names it. Stated rather than
        # assumed, because "cite the accepted head" is only honest where there
        # is one.
        raise FollowUpPlanLifecycleRefused(
            "this project has accepted nothing, so a Follow-up Plan cannot "
            "cite the accepted record it was recorded against"
        )
    try:
        with session.begin_nested():
            successor = record_follow_up_plan(
                session,
                project_id=project_id,
                delta_id=int(plan.delta_id),
                revision_id=int(revision_id),
                principal=acting,
                question=open_question.strip(),
                responsible_principal=responsible_principal,
                responsible_organization=responsible_organization,
                return_date=return_date,
                affected_scope=dict(plan.affected_scope or {}),
                evidence_ids=(),
                recorded_at=closed_at,
                idempotency_key=f"{idempotency_key}:successor",
            )
            closure = _write_closure(
                session,
                project_id=project_id,
                plan_id=plan_id,
                principal=acting.subject,
                closure_kind=SUPERSEDED,
                successor_plan_id=successor,
                cancellation_reason=None,
                note=note,
                closed_at=closed_at,
                idempotency_key=idempotency_key,
            )
    except DBAPIError as exc:
        return _refused(plan_id, SUPERSEDED, exc)
    session.expire_all()
    return ClosureOutcome(
        status=CLOSED,
        plan_id=plan_id,
        closure_kind=SUPERSEDED,
        closure_id=int(closure["closure_id"]),
        successor_plan_id=successor,
        created=bool(closure["created"]),
    )


def _close(
    session: Session,
    *,
    project_id: int,
    plan_id: int,
    principal: HumanPrincipal,
    closure_kind: str,
    successor_plan_id: int | None,
    cancellation_reason: str | None,
    note: str | None,
    closed_at: datetime,
    idempotency_key: str,
) -> ClosureOutcome:
    acting = require_human_principal(principal)
    if closed_at.tzinfo is None:
        raise FollowUpPlanLifecycleRefused(
            "a closure records an aware time from its caller's clock"
        )
    try:
        with session.begin_nested():
            closure = _write_closure(
                session,
                project_id=project_id,
                plan_id=plan_id,
                principal=acting.subject,
                closure_kind=closure_kind,
                successor_plan_id=successor_plan_id,
                cancellation_reason=cancellation_reason,
                note=note,
                closed_at=closed_at,
                idempotency_key=idempotency_key,
            )
    except DBAPIError as exc:
        return _refused(plan_id, closure_kind, exc)
    session.expire_all()
    return ClosureOutcome(
        status=CLOSED,
        plan_id=plan_id,
        closure_kind=closure_kind,
        closure_id=int(closure["closure_id"]),
        successor_plan_id=successor_plan_id,
        created=bool(closure["created"]),
    )


def _write_closure(
    session: Session,
    *,
    project_id: int,
    plan_id: int,
    principal: str,
    closure_kind: str,
    successor_plan_id: int | None,
    cancellation_reason: str | None,
    note: str | None,
    closed_at: datetime,
    idempotency_key: str,
) -> dict:
    """Call the one command that may write a closure.

    The runtime capabilities hold no write on ``delta_follow_up_plan_closures``
    and two triggers stand behind the grant: one refuses a write that did not
    arrive as the record-decision role, and #839's refuses one naming a person
    the roster does not designate to coordinate this project.
    """

    return session.execute(
        select(
            func.close_delta_follow_up_plan(
                project_id,
                plan_id,
                principal,
                closure_kind,
                successor_plan_id,
                cancellation_reason,
                note,
                closed_at,
                idempotency_key,
            )
        )
    ).scalar_one()


def _refused(plan_id: int, closure_kind: str, exc: DBAPIError) -> ClosureOutcome:
    """The command's own refusal, through the declared vocabulary.

    Rewriting these sentences here would put a second, quietly divergent
    vocabulary in front of one rule, which is what ``delta_refusals`` exists to
    prevent; only the machine token is taken off by the surface that prints it.
    """

    message = str(getattr(exc, "orig", exc))
    declared = database_refusal_code(message)
    return ClosureOutcome(
        status=declared.status,
        plan_id=plan_id,
        closure_kind=closure_kind,
        refusal=ClosureRefusal(
            status=declared.status,
            reason=declared.code,
            detail=message.strip().splitlines()[0],
        ),
    )


def closure_for(
    session: Session, *, project_id: int, plan_id: int
) -> DeltaFollowUpPlanClosure | None:
    """The one closure this plan holds, or ``None`` while it still stands."""

    return session.scalar(
        select(DeltaFollowUpPlanClosure).where(
            DeltaFollowUpPlanClosure.project_id == project_id,
            DeltaFollowUpPlanClosure.plan_id == plan_id,
        )
    )
