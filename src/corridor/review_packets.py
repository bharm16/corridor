"""Resolve one Review Packet in one atomic transaction (#526).

ADR-0085 makes a Review Packet a **derived presentation** over open Proposed
Deltas: nothing about the packet is stored, it has no lifecycle, and two
readings of the same state rebuild the same packets in the same order.  What
this module owns is the other half — the one attributable *act* a coordinator
commits over that presentation, and the guarantee that the act is complete or
absent.

ADR-0085 gives the act four primary outcomes and one secondary one:

``apply``            the incoming value becomes the accepted value;
``keep_current``     the accepted value stands;
``edit_and_apply``   a constrained edit becomes the accepted value; and
``needs_coordination`` the coordinator cannot settle it, so the question,
                     who owes the answer, when it returns, what it affects,
                     and the evidence that raised it become a separately
                     identified **Follow-up Plan decision** while the proposed
                     value stays unaccepted and the delta stays open.

The secondary ``defer`` is different in kind.  ADR-0084 settled that deferral
is Work List scheduling, not a semantic disposition: it writes a dated
attributable receipt, leaves the delta open, creates no Follow-up Plan, and
writes **no Project Record revision**.  So a packet carrying any semantic or
Follow-up Plan decision commits exactly one revision holding all of them, and
a packet of dated deferrals alone commits none.

**Why this module has no validation of its own.**  The obvious shape for a
bulk command is a bulk code path, and that is how two sets of decision rules
are born: the packet accepts what the single-delta command refuses, and the
difference is only found in production.  So every semantic child here is one
``ChildDecisionRequest`` put through #519's ``validate_child_decision`` and,
if the whole packet validates, through ``commit_child_decision`` with the
packet's own ``revision_id``.  That argument is exactly what makes a child
contribute its separately identified decision to the packet's one revision
and create no intermediate one.  Nothing about apply, keep current, or edit
and apply is decided here; this module decides only *which* children, in which
order, under one principal, against one reviewed predecessor revision.

**Why validation and writing are two passes.**  Every child is re-read and
re-checked — status, source-version identity, accepted predecessor revision,
allowed outcome, required support, project boundary — before anything is
written at all.  One stale, superseded, cross-project, missing, or invalid
child refuses the whole act, and the refusal carries what the reading needs
to refresh itself and nothing the coordinator typed, so a refresh never
discards their selections.

**What was tried before, and rejected.**  A stored packet with its own open
and closed states was rejected by ADR-0085: it would compete with the Proposed
Delta set, go stale the moment a new source arrived mid-review, and need its
own reconciliation machinery.  Recording a packet as one decision over many
deltas was rejected by ADR-0035, which forbids one Save collapsing the
identity of the distinct domain acts inside it.  Hence a receipt that keeps
every child's own identity beside the one revision they shared.

Undo is compensation, never deletion (ADR-0072, ADR-0080).
``reverse_review_packet`` appends one compensating revision restoring each
predecessor decision the packet superseded, and refuses outright when a later
act already depends on a result.  A later *correction* is not this command: it
is one new Proposed Delta resolved by a later attributable decision (#519),
which targets one child and leaves the original receipt and its children
exactly as they were recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import BigInteger, bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.analytics import (
    AnalyticsBinding,
    default_binding,
    emit_event,
    follow_up_plan_creation_event,
    packet_save_event,
)
from corridor.delta_refusals import RefusalCode, database_refusal_code
from corridor.delta_resolution import (
    ACCEPT,
    COORDINATION_NEEDED,
    DEFER,
    EDIT,
    REFUSED,
    REJECT,
    RESOLVED,
    STALE,
    UNSUPPORTED,
    ChildDecisionRequest,
    EditBasis,
    RecordEffect,
    Refusal,
    ValidatedChildDecision,
    commit_child_decision,
    current_accepted_revision_id,
    live_delta_status,
    open_resolution_revision,
    refresh_context,
    validate_child_decision,
)
from corridor.models import (
    PACKET_CHILD_OUTCOMES,
    PACKET_GROUPING_KEY_KINDS,
    PACKET_SEMANTIC_OUTCOMES,
    DeltaFollowUpPlan,
    DeltaReviewPacketChild,
    DeltaReviewPacketReversal,
    ProposedDelta,
    SupportAssessment,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.proposed_deltas import record_delta_deferral


APPLY = "apply"
KEEP_CURRENT = "keep_current"
EDIT_AND_APPLY = "edit_and_apply"
NEEDS_COORDINATION = "needs_coordination"
OUTCOMES = PACKET_CHILD_OUTCOMES
SEMANTIC_OUTCOMES = PACKET_SEMANTIC_OUTCOMES
GROUPING_KEY_KINDS = PACKET_GROUPING_KEY_KINDS

# The one place the coordinator's project-language outcome becomes #519's
# semantic disposition.  There is no second mapping anywhere.
_SEMANTIC_ACTION = {
    APPLY: ACCEPT,
    KEEP_CURRENT: REJECT,
    EDIT_AND_APPLY: EDIT,
}

SAVED = "saved"
REVERSED = "reversed"

# Every refusal the commands raise carries a stable leading token, exactly as
# #519's do, so the Python and SQL halves of one rule agree on what refused.
# The tokens and the status each carries are declared once, in
# ``corridor.delta_refusals``, beside #519's own family.


class ReviewPacketRefused(ValueError):
    """A caller cannot construct an attributable packet act at all."""


# --- What one coordinator asks of one packet ------------------------------


@dataclass(frozen=True)
class CoordinationRequest:
    """The Needs coordination answer: the question, and who owes it.

    ADR-0084 forbids settling an external fact with free text.  This is what
    the coordinator records instead, and it is a decision rather than a
    dropped selection.
    """

    question: str
    responsible_principal: str | None = None
    responsible_organization: str | None = None
    return_date: datetime | None = None
    affected_scope: dict[str, Any] | None = None
    evidence_support_assessment_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class DeferralRequest:
    """The dated Defer: Work List scheduling with a return condition."""

    deferred_until: datetime | None = None
    wake_condition: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PacketChildRequest:
    """One child delta and the one explicit outcome chosen for it.

    ``observed_source_revision`` is required, not defaulted: the coordinator
    decided against a source version they had read, and a child that does not
    say which one is exactly the implicitly selected child this command
    refuses to have.
    """

    delta_id: int
    outcome: str
    observed_source_revision: str
    record_effects: tuple[RecordEffect, ...] = ()
    support_assessment_ids: tuple[int, ...] = ()
    edit_basis: EditBasis | None = None
    organization_change_kind: str | None = None
    rationale: str | None = None
    effective_value: Any | None = None
    contradiction: bool = False
    coordination: CoordinationRequest | None = None
    deferral: DeferralRequest | None = None


@dataclass(frozen=True)
class ReviewPacketRequest:
    """One bounded set of Proposed Deltas, decided at once by one person.

    ``observed_accepted_revision_id`` is the packet's, not the child's: every
    child is validated against the same reviewed predecessor revision, which
    is what makes "the coordinator decided this from one reading" true.
    """

    project_id: int
    grouping_rule_version: str
    grouping_key_kind: str
    grouping_key: str
    principal: HumanPrincipal
    idempotency_key: str
    decided_at: datetime
    children: tuple[PacketChildRequest, ...]
    observed_accepted_revision_id: int | None = None


# --- What came back -------------------------------------------------------


@dataclass(frozen=True)
class PacketChildResult:
    """One child's own retained identity inside the one act."""

    ordinal: int
    delta_id: int
    outcome: str
    decision_id: int | None = None
    disposition_id: int | None = None
    follow_up_plan_id: int | None = None
    deferral_id: int | None = None
    effect_kind: str | None = None
    fact_decision_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReviewPacketResult:
    """The complete result of one packet act, or the complete absence of one."""

    status: str
    project_id: int
    receipt_id: int | None = None
    revision_id: int | None = None
    children: tuple[PacketChildResult, ...] = ()
    support_assessment_ids: tuple[int, ...] = ()
    created: bool = False
    refusals: tuple[Refusal, ...] = ()
    # Echoed back verbatim so a refused reading can refresh what moved without
    # discarding what the coordinator had chosen.
    preserved_selections: tuple[PacketChildRequest, ...] = ()

    @property
    def wrote_revision(self) -> bool:
        return self.revision_id is not None


@dataclass(frozen=True)
class ReviewPacketReversalResult:
    """The compensating act, or why it was refused."""

    status: str
    receipt_id: int
    reversal_id: int | None = None
    revision_id: int | None = None
    restored_fact_decision_ids: tuple[int, ...] = ()
    created: bool = False
    refusal: Refusal | None = None


# --- Saving one packet ----------------------------------------------------


def resolve_review_packet(
    session: Session,
    request: ReviewPacketRequest,
    *,
    binding: AnalyticsBinding | None = None,
) -> ReviewPacketResult:
    """Decide one bounded packet, completely or not at all."""

    principal = require_human_principal(request.principal)
    _require_well_formed(request)
    packet_binding = _packet_binding(request, binding)

    # Pass one reads and checks every child. Nothing below writes.
    validated: list[_ValidatedChild] = []
    refusals: list[Refusal] = []
    for ordinal, child in enumerate(request.children, start=1):
        outcome = _validate_child(session, request, child, ordinal)
        if isinstance(outcome, Refusal):
            refusals.append(outcome)
        else:
            validated.append(outcome)

    refusals.extend(_rival_effects(session, request))

    if refusals:
        _emit_packet_save(
            packet_binding,
            request,
            outcome=REFUSED,
            reason=refusals[0].reason,
            receipt_id=None,
            revision_id=None,
        )
        return ReviewPacketResult(
            status=REFUSED,
            project_id=request.project_id,
            refusals=tuple(refusals),
            preserved_selections=tuple(request.children),
        )

    # Pass two writes the complete result or nothing. The savepoint is what
    # makes "or nothing" true even when the database refuses a child the
    # Python half believed lawful.
    needs_revision = any(
        child.request.outcome != DEFER for child in validated
    )
    try:
        with session.begin_nested():
            revision_id: int | None = None
            if needs_revision:
                revision_id = open_resolution_revision(
                    session,
                    project_id=request.project_id,
                    principal=principal,
                    idempotency_key=request.idempotency_key,
                )
            children = tuple(
                _commit_child(
                    session,
                    request,
                    child,
                    revision_id=revision_id,
                    binding=packet_binding,
                )
                for child in validated
            )
            support_ids = _packet_support_ids(validated)
            receipt_id = _record_receipt(
                session,
                request,
                principal=principal,
                revision_id=revision_id,
                children=children,
                support_ids=support_ids,
            )
    except _ChildRefused as refused:
        _emit_packet_save(
            packet_binding,
            request,
            outcome=REFUSED,
            reason=refused.refusal.reason,
            receipt_id=None,
            revision_id=None,
        )
        return ReviewPacketResult(
            status=REFUSED,
            project_id=request.project_id,
            refusals=(refused.refusal,),
            preserved_selections=tuple(request.children),
        )
    except DBAPIError as exc:
        refusal = _database_refusal(None, exc)
        _emit_packet_save(
            packet_binding,
            request,
            outcome=REFUSED,
            reason=refusal.reason,
            receipt_id=None,
            revision_id=None,
        )
        return ReviewPacketResult(
            status=REFUSED,
            project_id=request.project_id,
            refusals=(refusal,),
            preserved_selections=tuple(request.children),
        )

    session.expire_all()
    _emit_packet_save(
        packet_binding,
        request,
        outcome=SAVED,
        reason=None,
        receipt_id=receipt_id,
        revision_id=revision_id,
    )
    return ReviewPacketResult(
        status=SAVED,
        project_id=request.project_id,
        receipt_id=receipt_id,
        revision_id=revision_id,
        children=children,
        support_assessment_ids=support_ids,
        created=True,
    )


# --- Validation: #519's seam for every semantic child ---------------------


@dataclass(frozen=True)
class _ValidatedChild:
    """One child that passed every check, ready for the write pass."""

    ordinal: int
    request: PacketChildRequest
    decision: ValidatedChildDecision | None = None


def _rival_effects(
    session: Session, request: ReviewPacketRequest
) -> list[Refusal]:
    """Refuse an act whose children would each make the same field effective.

    A cross-source coordination question offers two sources answering one
    field, and exactly one of those answers can become the accepted value
    (#528).  Nothing downstream would say so: #519 validates one child at a
    time, and two accepts for the same subject and field would each commit,
    leaving the accepted value decided by the order the children happened to
    be written in.  The rule lives here rather than on the screen because both
    the batch and the focused path build packets through this command, and an
    invariant enforced at one door is not enforced.

    Read from the request rather than from what passed validation, so a rival
    pair is still reported when one of the two is also stale — otherwise the
    check would be silently skipped exactly when the packet is most confused.
    """

    seen: dict[tuple[str, str | None], int] = {}
    refusals: list[Refusal] = []
    for child in request.children:
        if child.outcome not in (APPLY, EDIT_AND_APPLY):
            continue
        delta = session.get(ProposedDelta, child.delta_id)
        if delta is None or delta.project_id != request.project_id:
            continue
        key = (delta.target_subject_identity, delta.target_field)
        rival = seen.get(key)
        if rival is None:
            seen[key] = delta.id
            continue
        refusals.append(
            Refusal(
                status=REFUSED,
                reason="rival_effects",
                detail=(
                    "two of the changes selected would each become the accepted "
                    f"value for the same field, so nothing was saved; proposed "
                    f"change {rival} and proposed change {delta.id} answer "
                    "the same question and only one of them can stand"
                ),
                delta_id=delta.id,
                **refresh_context(session, delta),
            )
        )
    return refusals


def _require_well_formed(request: ReviewPacketRequest) -> None:
    """The shape a caller must supply, before anything is read at all."""

    if not request.idempotency_key.strip():
        raise ReviewPacketRefused("a packet act needs an idempotency key")
    if request.decided_at.tzinfo is None:
        raise ReviewPacketRefused(
            "a packet act records an aware decision time from its caller's clock"
        )
    if not request.grouping_rule_version.strip():
        raise ReviewPacketRefused(
            "a packet act records the grouping-rule version it was built by"
        )
    if request.grouping_key_kind not in GROUPING_KEY_KINDS:
        raise ReviewPacketRefused(
            f"{request.grouping_key_kind!r} is not an adaptive packet key "
            f"(ADR-0085): one of {', '.join(GROUPING_KEY_KINDS)}"
        )
    if not request.children:
        raise ReviewPacketRefused(
            "a packet act names the exact ordered child set it decided; "
            "there is no wildcard and no implicit selection"
        )
    seen: set[int] = set()
    for child in request.children:
        if child.outcome not in OUTCOMES:
            raise ReviewPacketRefused(
                f"{child.outcome!r} is not a packet child outcome"
            )
        if child.delta_id in seen:
            raise ReviewPacketRefused(
                f"Proposed Delta {child.delta_id} appears twice in one packet"
            )
        seen.add(child.delta_id)
        if not str(child.observed_source_revision).strip():
            raise ReviewPacketRefused(
                "every child names the source version the coordinator read"
            )


def _validate_child(
    session: Session,
    request: ReviewPacketRequest,
    child: PacketChildRequest,
    ordinal: int,
) -> _ValidatedChild | Refusal:
    """Re-read one child and re-check it, whatever its outcome."""

    delta = session.get(ProposedDelta, child.delta_id)
    if delta is None or delta.project_id != request.project_id:
        return Refusal(
            status=REFUSED,
            reason="cross_project_delta",
            detail="the Proposed Delta is not this project's to decide",
            delta_id=child.delta_id,
        )
    if delta.source_revision != child.observed_source_revision:
        return Refusal(
            status=REFUSED,
            reason="source_version_mismatch",
            detail=(
                f"the packet was read against source version "
                f"{child.observed_source_revision!r}, and this delta states "
                f"{delta.source_revision!r}"
            ),
            delta_id=delta.id,
            **refresh_context(session, delta),
        )
    status = live_delta_status(session, delta.id)
    if status in ("resolved", "superseded", "capture_corrected"):
        return Refusal(
            status=REFUSED,
            reason=(
                "already_resolved"
                if status == "resolved"
                else (
                    "superseded_delta"
                    if status == "superseded"
                    else "capture_corrected_delta"
                )
            ),
            detail=(
                "the Proposed Delta is already resolved; correct it with a "
                "later decision"
                if status == "resolved"
                else (
                    "a newer source version superseded this Proposed Delta"
                    if status == "superseded"
                    # ADR-0101's approved words. A packet rendered before the
                    # correction and submitted after it applies nothing, and
                    # says so rather than dropping the child quietly.
                    else (
                        "This proposal can no longer be applied because its "
                        "source reading was corrected. Nothing was applied. "
                        "View the correction result."
                    )
                )
            ),
            delta_id=delta.id,
            **refresh_context(session, delta),
        )

    # Every child, semantic or not, is judged against the same reviewed
    # predecessor revision. #519 checks staleness per decided Source Fact;
    # this checks it for the delta's own subject and field, which is the only
    # check a keep-current, coordination, or deferral child would otherwise
    # never get.
    standing = current_accepted_revision_id(
        session,
        project_id=request.project_id,
        subject_identity=delta.target_subject_identity,
        field_name=delta.target_field,
    )
    observed = request.observed_accepted_revision_id or 0
    if standing is not None and standing > observed:
        return Refusal(
            status=STALE,
            reason="stale_accepted_revision",
            detail=(
                f"the accepted record moved to revision {standing} after "
                f"revision {observed} was read"
            ),
            delta_id=delta.id,
            **refresh_context(session, delta),
        )

    if child.outcome in SEMANTIC_OUTCOMES:
        return _validate_semantic_child(session, request, child, ordinal)
    if child.outcome == NEEDS_COORDINATION:
        return _validate_coordination_child(session, request, child, ordinal, delta)
    return _validate_deferral_child(child, ordinal, delta)


def _validate_semantic_child(
    session: Session,
    request: ReviewPacketRequest,
    child: PacketChildRequest,
    ordinal: int,
) -> _ValidatedChild | Refusal:
    """Apply, keep current, and edit and apply are #519's, entirely."""

    decision = validate_child_decision(session, _child_decision_request(request, child))
    if isinstance(decision, Refusal):
        return decision
    return _ValidatedChild(ordinal=ordinal, request=child, decision=decision)


def _child_decision_request(
    request: ReviewPacketRequest, child: PacketChildRequest
) -> ChildDecisionRequest:
    """The exact single-delta request #519 would have been given standalone.

    The child's idempotency key is derived from the packet's, so replaying one
    packet returns the decisions it already wrote rather than a second set.
    """

    return ChildDecisionRequest(
        project_id=request.project_id,
        delta_id=child.delta_id,
        action=_SEMANTIC_ACTION[child.outcome],
        principal=request.principal,
        idempotency_key=child_idempotency_key(request.idempotency_key, child.delta_id),
        decided_at=request.decided_at,
        observed_accepted_revision_id=request.observed_accepted_revision_id,
        record_effects=tuple(child.record_effects),
        support_assessment_ids=tuple(child.support_assessment_ids),
        edit_basis=child.edit_basis,
        organization_change_kind=child.organization_change_kind,
        rationale=child.rationale,
        effective_value=child.effective_value,
        contradiction=child.contradiction,
    )


def child_idempotency_key(packet_key: str, delta_id: int) -> str:
    """One packet key, one stable key per child, so a replay converges."""

    return f"{packet_key}:{delta_id}"


def _validate_coordination_child(
    session: Session,
    request: ReviewPacketRequest,
    child: PacketChildRequest,
    ordinal: int,
    delta: ProposedDelta,
) -> _ValidatedChild | Refusal:
    coordination = child.coordination
    if coordination is None or not coordination.question.strip():
        return Refusal(
            status=COORDINATION_NEEDED,
            reason="missing_question",
            detail=(
                "a Follow-up Plan records the exact question that must be "
                "answered before this value can be accepted"
            ),
            delta_id=delta.id,
            **refresh_context(session, delta),
        )
    responsible = (coordination.responsible_principal or "").strip() or (
        coordination.responsible_organization or ""
    ).strip()
    if not responsible:
        return Refusal(
            status=COORDINATION_NEEDED,
            reason="missing_responsible_party",
            detail=(
                "a Follow-up Plan names the person or organization who owes "
                "the answer"
            ),
            delta_id=delta.id,
            open_question=coordination.question,
            **refresh_context(session, delta),
        )
    if coordination.return_date is not None and (
        coordination.return_date.tzinfo is None
    ):
        raise ReviewPacketRefused(
            "a Follow-up Plan records an aware return date from its caller's clock"
        )
    if child.record_effects:
        return Refusal(
            status=REFUSED,
            reason="invalid_outcome",
            detail=(
                "Needs coordination leaves the proposed value unaccepted and "
                "changes no effective decision"
            ),
            delta_id=delta.id,
            **refresh_context(session, delta),
        )
    for assessment_id in coordination.evidence_support_assessment_ids:
        assessment = session.get(SupportAssessment, int(assessment_id))
        if (
            assessment is None
            or assessment.project_id != request.project_id
            or assessment.superseded_by is not None
        ):
            return Refusal(
                status=UNSUPPORTED,
                reason="missing_support",
                detail=(
                    f"Support Assessment {assessment_id} is not an effective "
                    "assessment of this project"
                ),
                delta_id=delta.id,
                open_question=coordination.question,
                **refresh_context(session, delta),
            )
    return _ValidatedChild(ordinal=ordinal, request=child)


def _validate_deferral_child(
    child: PacketChildRequest, ordinal: int, delta: ProposedDelta
) -> _ValidatedChild | Refusal:
    deferral = child.deferral
    if deferral is None or (
        deferral.deferred_until is None and deferral.wake_condition is None
    ):
        raise ReviewPacketRefused(
            "a dated Defer carries a return date or a wake condition"
        )
    if deferral.deferred_until is not None and deferral.deferred_until.tzinfo is None:
        raise ReviewPacketRefused(
            "a dated Defer records an aware return date from its caller's clock"
        )
    if child.record_effects or child.support_assessment_ids:
        return Refusal(
            status=REFUSED,
            reason="invalid_outcome",
            detail=(
                "a dated Defer is Work List scheduling: it decides no value "
                "and cites no support"
            ),
            delta_id=delta.id,
        )
    return _ValidatedChild(ordinal=ordinal, request=child)


# --- The write pass -------------------------------------------------------


class _ChildRefused(Exception):
    """Raised to abandon the savepoint so the whole act writes nothing."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.detail)
        self.refusal = refusal


def _commit_child(
    session: Session,
    request: ReviewPacketRequest,
    child: _ValidatedChild,
    *,
    revision_id: int | None,
    binding: AnalyticsBinding,
) -> PacketChildResult:
    outcome = child.request.outcome
    if outcome in SEMANTIC_OUTCOMES:
        assert child.decision is not None
        result = commit_child_decision(
            session, child.decision, revision_id=revision_id, binding=binding
        )
        if result.status != RESOLVED or result.refusal is not None:
            raise _ChildRefused(
                result.refusal
                or Refusal(
                    status=REFUSED,
                    reason="refused",
                    detail="the decision command refused this child",
                    delta_id=child.request.delta_id,
                )
            )
        return PacketChildResult(
            ordinal=child.ordinal,
            delta_id=child.request.delta_id,
            outcome=outcome,
            decision_id=result.decision_id,
            disposition_id=result.disposition_id,
            effect_kind=result.effect_kind,
            fact_decision_ids=result.fact_decision_ids,
        )
    if outcome == NEEDS_COORDINATION:
        assert revision_id is not None
        plan_id = _record_follow_up_plan(
            session, request, child, revision_id=revision_id, binding=binding
        )
        return PacketChildResult(
            ordinal=child.ordinal,
            delta_id=child.request.delta_id,
            outcome=outcome,
            follow_up_plan_id=plan_id,
        )
    deferral = child.request.deferral
    assert deferral is not None
    # The packet act is the request that asked for this schedule, so the child
    # key a replayed submission would compose again is what makes the replay
    # the same scheduling act rather than a second one (#903) -- the same key
    # a semantic child carries. A packet Defer replaces no schedule: a change
    # the coordinator has already deferred is not offered on the review screen
    # (ADR-0085), so it names no predecessor.
    receipt = record_delta_deferral(
        session,
        project_id=request.project_id,
        delta_id=child.request.delta_id,
        deferred_at=request.decided_at.astimezone(timezone.utc),
        scheduled_by_principal=request.principal.subject,
        request_identity=child_idempotency_key(
            request.idempotency_key, child.request.delta_id
        ),
        deferred_until=deferral.deferred_until,
        wake_condition=deferral.wake_condition,
        reason=deferral.reason,
    )
    return PacketChildResult(
        ordinal=child.ordinal,
        delta_id=child.request.delta_id,
        outcome=outcome,
        deferral_id=int(receipt.id),
    )


def record_follow_up_plan(
    session: Session,
    *,
    project_id: int,
    delta_id: int,
    revision_id: int,
    principal: HumanPrincipal,
    question: str,
    responsible_principal: str | None,
    responsible_organization: str | None,
    return_date: datetime | None,
    affected_scope: Mapping[str, Any],
    evidence_ids: Sequence[int],
    recorded_at: datetime,
    idempotency_key: str,
) -> int:
    """Write one Follow-up Plan through the one command that may.

    The packet's own path below calls this, and so does #835's plan lifecycle
    when a coordinator corrects a plan outside a packet act: both record the
    same kind of row through the same command, and a second call site spelling
    the twelve arguments again is how the two would come apart.
    """

    written = session.scalar(
        select(
            func.record_delta_follow_up_plan(
                project_id,
                delta_id,
                revision_id,
                principal.subject,
                question,
                responsible_principal,
                responsible_organization,
                return_date,
                _jsonb(dict(affected_scope)),
                cast(
                    bindparam(None, [int(value) for value in evidence_ids]),
                    ARRAY(BigInteger),
                ),
                recorded_at,
                idempotency_key,
            )
        )
    )
    return int(written["plan_id"])


def _record_follow_up_plan(
    session: Session,
    request: ReviewPacketRequest,
    child: _ValidatedChild,
    *,
    revision_id: int,
    binding: AnalyticsBinding,
) -> int:
    coordination = child.request.coordination
    assert coordination is not None
    evidence = tuple(
        int(value) for value in coordination.evidence_support_assessment_ids
    )
    plan_id = record_follow_up_plan(
        session,
        project_id=request.project_id,
        delta_id=child.request.delta_id,
        revision_id=revision_id,
        principal=request.principal,
        question=coordination.question,
        responsible_principal=coordination.responsible_principal,
        responsible_organization=coordination.responsible_organization,
        return_date=coordination.return_date,
        affected_scope=coordination.affected_scope or {},
        evidence_ids=evidence,
        recorded_at=request.decided_at.astimezone(timezone.utc),
        idempotency_key=child_idempotency_key(
            request.idempotency_key, child.request.delta_id
        ),
    )
    _emit_follow_up_plan(
        binding,
        request,
        child,
        plan_id=plan_id,
        revision_id=revision_id,
        evidence_count=len(evidence),
    )
    return plan_id


def _packet_support_ids(children: Sequence[_ValidatedChild]) -> tuple[int, ...]:
    """Every Support Assessment the act relied on, in the order it named them."""

    ordered: list[int] = []
    for child in children:
        cited: tuple[int, ...] = ()
        if child.decision is not None:
            cited = child.decision.support_assessment_ids
        elif child.request.coordination is not None:
            cited = tuple(
                int(value)
                for value in child.request.coordination.evidence_support_assessment_ids
            )
        for assessment_id in cited:
            if assessment_id not in ordered:
                ordered.append(int(assessment_id))
    return tuple(ordered)


def _record_receipt(
    session: Session,
    request: ReviewPacketRequest,
    *,
    principal: HumanPrincipal,
    revision_id: int | None,
    children: Sequence[PacketChildResult],
    support_ids: Sequence[int],
) -> int:
    payload = [
        {
            "ordinal": child.ordinal,
            "delta_id": child.delta_id,
            "outcome": child.outcome,
            "observed_source_revision": source_revision,
            "decision_id": child.decision_id,
            "follow_up_plan_id": child.follow_up_plan_id,
            "deferral_id": child.deferral_id,
        }
        for child, source_revision in zip(
            children,
            [item.observed_source_revision for item in request.children],
            strict=True,
        )
    ]
    written = session.scalar(
        select(
            func.record_review_packet_receipt(
                request.project_id,
                revision_id,
                request.grouping_rule_version,
                request.grouping_key_kind,
                request.grouping_key,
                principal.subject,
                request.observed_accepted_revision_id,
                request.decided_at.astimezone(timezone.utc),
                request.idempotency_key,
                _jsonb(payload),
                cast(bindparam(None, [int(value) for value in support_ids]), ARRAY(BigInteger)),
            )
        )
    )
    return int(written["receipt_id"])


# --- Undo -----------------------------------------------------------------


def reverse_review_packet(
    session: Session,
    *,
    project_id: int,
    receipt_id: int,
    principal: HumanPrincipal,
    reversed_at: datetime,
    idempotency_key: str,
) -> ReviewPacketReversalResult:
    """Compensate for one complete packet act, when no later act depends on it."""

    actor = require_human_principal(principal)
    if not idempotency_key.strip():
        raise ReviewPacketRefused("an Undo needs an idempotency key")
    if reversed_at.tzinfo is None:
        raise ReviewPacketRefused(
            "an Undo records an aware time from its caller's clock"
        )
    try:
        with session.begin_nested():
            written = session.scalar(
                select(
                    func.reverse_review_packet(
                        project_id,
                        receipt_id,
                        actor.subject,
                        reversed_at.astimezone(timezone.utc),
                        idempotency_key,
                    )
                )
            )
    except DBAPIError as exc:
        refusal = _database_refusal(None, exc)
        return ReviewPacketReversalResult(
            status=REFUSED, receipt_id=receipt_id, refusal=refusal
        )
    session.expire_all()
    return ReviewPacketReversalResult(
        status=REVERSED,
        receipt_id=receipt_id,
        reversal_id=int(written["reversal_id"]),
        revision_id=(
            int(written["revision_id"])
            if written.get("revision_id") is not None
            else None
        ),
        restored_fact_decision_ids=tuple(
            int(value) for value in (written.get("restored_fact_decision_ids") or ())
        ),
        created=bool(written["created"]),
    )


# --- Reading one recorded act ---------------------------------------------


def packet_children(
    session: Session, receipt_id: int
) -> tuple[DeltaReviewPacketChild, ...]:
    """One act's children in the order the coordinator was shown them."""

    return tuple(
        session.scalars(
            select(DeltaReviewPacketChild)
            .where(DeltaReviewPacketChild.receipt_id == receipt_id)
            .order_by(DeltaReviewPacketChild.ordinal)
        ).all()
    )


def packet_reversal(
    session: Session, receipt_id: int
) -> DeltaReviewPacketReversal | None:
    """The compensating act for one receipt, if one was made."""

    return session.scalar(
        select(DeltaReviewPacketReversal).where(
            DeltaReviewPacketReversal.receipt_id == receipt_id
        )
    )


def follow_up_plans_for_delta(
    session: Session, *, project_id: int, delta_id: int
) -> tuple[DeltaFollowUpPlan, ...]:
    """Every Follow-up Plan raised on one delta, in the order they were made."""

    return tuple(
        session.scalars(
            select(DeltaFollowUpPlan)
            .where(
                DeltaFollowUpPlan.project_id == project_id,
                DeltaFollowUpPlan.delta_id == delta_id,
            )
            .order_by(DeltaFollowUpPlan.id)
        ).all()
    )


# --- The versioned event contract (#558) ----------------------------------


def _packet_binding(
    request: ReviewPacketRequest, binding: AnalyticsBinding | None
) -> AnalyticsBinding:
    """Bind every packet event to the packetizer rule version actually used."""

    base = binding or default_binding()
    if base.packetizer_rules_version == request.grouping_rule_version:
        return base
    return replace(base, packetizer_rules_version=request.grouping_rule_version)


def _emit_packet_save(
    binding: AnalyticsBinding,
    request: ReviewPacketRequest,
    *,
    outcome: str,
    reason: str | None,
    receipt_id: int | None,
    revision_id: int | None,
) -> None:
    """One Packet Save event, refusals included.

    A refused act is emitted exactly like a saved one, because the measurement
    the event exists for — how often a coordinator's whole reading was already
    out of date — is invisible if only the successes are recorded.
    """

    counts: dict[str, int] = {name: 0 for name in OUTCOMES}
    for child in request.children:
        counts[child.outcome] = counts.get(child.outcome, 0) + 1
    emit_event(
        packet_save_event(
            binding,
            occurred_at=request.decided_at.astimezone(timezone.utc),
            project_id=request.project_id,
            receipt_id=receipt_id,
            revision_id=revision_id,
            grouping_rule_version=request.grouping_rule_version,
            grouping_key_kind=request.grouping_key_kind,
            grouping_key=request.grouping_key,
            observed_accepted_revision_id=request.observed_accepted_revision_id,
            child_count=len(request.children),
            outcome_counts=counts,
            outcome=outcome,
            refusal_reason=reason,
        )
    )


def _emit_follow_up_plan(
    binding: AnalyticsBinding,
    request: ReviewPacketRequest,
    child: _ValidatedChild,
    *,
    plan_id: int,
    revision_id: int,
    evidence_count: int,
) -> None:
    coordination = child.request.coordination
    assert coordination is not None
    emit_event(
        follow_up_plan_creation_event(
            binding,
            occurred_at=request.decided_at.astimezone(timezone.utc),
            project_id=request.project_id,
            delta_id=child.request.delta_id,
            follow_up_plan_id=plan_id,
            revision_id=revision_id,
            grouping_rule_version=request.grouping_rule_version,
            has_return_date=coordination.return_date is not None,
            responsible_kind=(
                "principal"
                if (coordination.responsible_principal or "").strip()
                else "organization"
            ),
            evidence_count=evidence_count,
        )
    )


# --- Shared plumbing ------------------------------------------------------


def _database_refusal(delta_id: int | None, exc: DBAPIError) -> Refusal:
    """Map a command's own stable refusal token through the declared vocabulary."""

    message = str(getattr(exc, "orig", exc))
    declared: RefusalCode = database_refusal_code(message)
    return Refusal(
        status=declared.status,
        reason=declared.code,
        detail=message.strip().splitlines()[0],
        delta_id=delta_id or 0,
    )


def _jsonb(value: object):
    if value is None:
        return cast(bindparam(None, None), JSONB)
    return cast(bindparam(None, json.dumps(value, default=str)), JSONB)
