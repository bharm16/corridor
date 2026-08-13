"""Derive the coordinator's one-item-per-statement work list.

The Ledger keeps all External Party facts and the exceptions reader keeps its
Dependency facts.  Neither can safely stand in for the coordinator's work:
an unknown-scope Commitment is real work but does not belong to an invented
Dependency.  This module is the public read seam for that gap.  It groups
current statement facts by Commitment Lineage, preserves timing precision, and
returns derived Attention Reasons without storing flags or copying facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.dependency_events import current_scope_decision_filter
from corridor.event_admission import waiting_statements
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementRefusal,
    validate_cited_statement_evidence,
)
from corridor.exceptions import contradicted_fields
from corridor.models import (
    CommitmentLineage,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    EvidenceLink,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
    is_critical,
)
from corridor.statement_lifecycle import current_statement_event_filter
from corridor.work_decisions import (
    CoordinationSubject,
    current_deferral_decision,
    current_milestone_impact_decision,
    current_next_action_decision,
)


WORK_LIST_RULESET_VERSION = "work-list-v1"

_REASON_ORDER = {
    "past_due": 0,
    "critical_missing_internal_owner": 1,
    "critical_missing_next_action": 1,
    "committed_date_change": 2,
    "milestone_impact_unknown": 2,
    "disputed_date": 2,
    "unknown_scope": 3,
    "unplaced_statement": 3,
    "missing_internal_owner": 4,
    "missing_next_action": 4,
    "action_due": 4,
    "action_due_date_unknown": 4,
    "external_closure_follow_up": 4,
}


@dataclass(frozen=True)
class PastDueCommitment:
    """The attributable Derivation behind one party-level past-due reason."""

    evaluated_on: date
    ruleset_version: str
    commitment_lineage_id: int
    statement_event_id: int
    timing_id: int
    affected_external_org_id: int | None
    source_kind: str
    source_evidence_link_ids: tuple[int, ...]
    due_after: date


@dataclass(frozen=True)
class WorkItem:
    """One current coordinator question, never one row per reason."""

    kind: str
    commitment_lineage_id: int | None
    statement_event_id: int | None
    dependency_id: int | None
    candidate_id: int | None
    source_candidate_id: int | None
    timing_text: str | None
    attention_reason_codes: tuple[str, ...]
    past_due: PastDueCommitment | None
    deferral_reason: str | None = None
    return_date: date | None = None


@dataclass(frozen=True)
class WorkList:
    """A time-bound, ruleset-named read of immediate and deferred work."""

    project_id: int
    evaluated_on: date
    ruleset_version: str
    immediate: tuple[WorkItem, ...]
    backlog: tuple[WorkItem, ...]


def build_work_list(
    session: Session,
    project_id: int,
    *,
    today: date | None = None,
) -> WorkList:
    """Return current statement work without manufacturing Dependency facts.

    A timing is past due only after its supported boundary: the exact day for
    day precision, or the final day of a stated month.  Approximate and legacy
    unknown timing therefore produce no past-due Derivation at all.
    """
    evaluated_on = today or date.today()
    closed_lineages = _closed_commitment_lineages(session, project_id)
    rows = session.execute(
        select(
            DependencyEvent,
            DependencyEventTiming,
            DependencyEventScopeDecision,
            CommitmentLineage,
        )
        .join(
            DependencyEventTiming,
            DependencyEventTiming.event_id == DependencyEvent.id,
        )
        .join(
            DependencyEventScopeDecision,
            DependencyEventScopeDecision.event_id == DependencyEvent.id,
        )
        .join(
            CommitmentLineage,
            CommitmentLineage.id == DependencyEvent.commitment_lineage_id,
        )
        .where(
            DependencyEvent.project_id == project_id,
            DependencyEvent.event_type.in_(("commitment", "committed_date_change")),
            DependencyEvent.attribution_state == "resolved",
            DependencyEvent.stated_external_org_id.is_not(None),
            DependencyEvent.commitment_lineage_id.is_not(None),
            DependencyEventTiming.kind == "new",
            current_statement_event_filter(DependencyEvent.id),
            current_scope_decision_filter(),
        )
        .order_by(DependencyEvent.id)
    ).all()

    candidate_ids = _source_candidate_ids(session, project_id)
    immediate: list[WorkItem] = []
    backlog: list[WorkItem] = []
    for event, timing, scope, lineage in rows:
        is_closed = event.commitment_lineage_id in closed_lineages
        reason_codes: list[str] = []
        past_due = None if is_closed else _past_due(session, event, timing, evaluated_on)
        if past_due is not None:
            reason_codes.append("past_due")
        if not is_closed and event.event_type == "committed_date_change":
            reason_codes.append("committed_date_change")
            if lineage.milestone_impact in (None, "not_yet_known"):
                reason_codes.append("milestone_impact_unknown")
        if not is_closed and scope.scope_mode == "unknown":
            reason_codes.append("unknown_scope")
        if lineage.next_action is not None and is_closed:
            reason_codes.append("external_closure_follow_up")
        if not lineage.internal_owner and (not is_closed or lineage.next_action is not None):
            reason_codes.append("missing_internal_owner")
        if lineage.next_action:
            if lineage.action_due_date is None:
                reason_codes.append("action_due_date_unknown")
            elif lineage.action_due_date <= evaluated_on:
                reason_codes.append("action_due")
        elif not is_closed:
            reason_codes.append("missing_next_action")
        if not reason_codes:
            continue
        item = WorkItem(
            kind="statement",
            commitment_lineage_id=event.commitment_lineage_id,
            statement_event_id=event.id,
            dependency_id=None,
            candidate_id=None,
            source_candidate_id=candidate_ids.get(event.commitment_lineage_id),
            timing_text=_display_timing(timing),
            attention_reason_codes=tuple(sorted(reason_codes, key=_REASON_ORDER.__getitem__)),
            past_due=past_due,
            deferral_reason=lineage.deferral_reason,
            return_date=lineage.deferral_return_date,
        )
        is_immediate = _is_immediate(lineage, evaluated_on) or _statement_changed(
            session, event, scope, lineage
        )
        (immediate if is_immediate else backlog).append(item)

    for item, projection in _dependency_items(session, project_id):
        (immediate if _is_immediate(projection, evaluated_on) else backlog).append(item)
    immediate.extend(_unplaced_statement_items(session, project_id))

    immediate.sort(key=_item_sort_key)
    backlog.sort(key=_item_sort_key)
    return WorkList(
        project_id=project_id,
        evaluated_on=evaluated_on,
        ruleset_version=WORK_LIST_RULESET_VERSION,
        immediate=tuple(immediate),
        backlog=tuple(backlog),
    )


def _past_due(
    session: Session,
    event: DependencyEvent,
    timing: DependencyEventTiming,
    evaluated_on: date,
) -> PastDueCommitment | None:
    due_after = (
        timing.end_date
        if timing.precision in ("day", "month")
        else None
    )
    if due_after is None or evaluated_on <= due_after:
        return None
    assert event.commitment_lineage_id is not None
    return PastDueCommitment(
        evaluated_on=evaluated_on,
        ruleset_version=WORK_LIST_RULESET_VERSION,
        commitment_lineage_id=event.commitment_lineage_id,
        statement_event_id=event.id,
        timing_id=timing.id,
        affected_external_org_id=event.affected_external_org_id,
        source_kind=event.source_kind,
        source_evidence_link_ids=_source_evidence_link_ids(session, event),
        due_after=due_after,
    )


def _display_timing(timing: DependencyEventTiming) -> str:
    if timing.precision == "month" and timing.start_date is not None:
        return timing.start_date.strftime("%B %Y")
    return timing.text


def _item_sort_key(item: WorkItem) -> tuple[int, int]:
    """Keep the highest-consequence reason in charge of one grouped item."""
    priority = min(_REASON_ORDER[reason] for reason in item.attention_reason_codes)
    identity = item.statement_event_id or item.dependency_id or item.candidate_id or 0
    return priority, identity


def _is_immediate(
    projection: CommitmentLineage | Dependency, evaluated_on: date
) -> bool:
    """A plan needs a future Action Due Date before it may delay attention."""
    if isinstance(projection, CommitmentLineage) and projection.plan_needs_review:
        return True
    return_conditions = [
        due_date
        for due_date in (projection.action_due_date, projection.deferral_return_date)
        if due_date is not None
    ]
    if not return_conditions:
        return True
    return min(return_conditions) <= evaluated_on


def _statement_changed(
    session: Session,
    event: DependencyEvent,
    scope: DependencyEventScopeDecision,
    lineage: CommitmentLineage,
) -> bool:
    """Return delayed work when the External Party fact it answered changes."""
    subject = CoordinationSubject.statement(lineage.id)
    deferral = current_deferral_decision(session, subject)
    action = current_next_action_decision(session, subject)
    decision = (
        deferral
        if deferral is not None and deferral.after_value is not None
        else action
    )
    if decision is None:
        return False

    receipt = session.scalar(
        select(StatementCoordinationReceipt).where(
            StatementCoordinationReceipt.next_action_decision_id == decision.id
        )
    )
    impact = current_milestone_impact_decision(session, subject)
    if receipt is not None:
        return (
            receipt.dependency_event_id != event.id
            or receipt.scope_decision_id != scope.id
            or receipt.milestone_impact_decision_id
            != (impact.id if impact is not None else None)
        )

    return (
        (
            decision.observed_statement_event_id is not None
            and decision.observed_statement_event_id != event.id
        )
        or (
            decision.observed_scope_decision_id is not None
            and decision.observed_scope_decision_id != scope.id
        )
        or (
            decision.observed_milestone_impact_decision_id is not None
            and decision.observed_milestone_impact_decision_id
            != (impact.id if impact is not None else None)
        )
    )


def _dependency_items(
    session: Session, project_id: int
) -> tuple[tuple[WorkItem, Dependency], ...]:
    """Critical coordination gaps and disputed dates retain one Dependency item."""
    dependencies = session.scalars(
        select(Dependency)
        .where(
            Dependency.project_id == project_id,
            Dependency.dismissed_at.is_(None),
            Dependency.status != "closed",
        )
        .order_by(Dependency.id)
    ).all()
    disputed = contradicted_fields(session, [dependency.id for dependency in dependencies])
    items = []
    for dependency in dependencies:
        reason_codes: list[str] = []
        if is_critical(dependency.resolution_strategy):
            if not dependency.internal_owner:
                reason_codes.append("critical_missing_internal_owner")
            if not dependency.next_action:
                reason_codes.append("critical_missing_next_action")
        if set(disputed.get(dependency.id, ())).intersection(
            {"committed_date", "need_date"}
        ):
            reason_codes.append("disputed_date")
        if not reason_codes:
            continue
        items.append(
            (
                WorkItem(
                    kind="dependency",
                    commitment_lineage_id=None,
                    statement_event_id=None,
                    dependency_id=dependency.id,
                    candidate_id=None,
                    source_candidate_id=None,
                    timing_text=None,
                    attention_reason_codes=tuple(
                        sorted(reason_codes, key=_REASON_ORDER.__getitem__)
                    ),
                    past_due=None,
                    deferral_reason=dependency.deferral_reason,
                    return_date=dependency.deferral_return_date,
                ),
                dependency,
            )
        )
    return tuple(items)


def _closed_commitment_lineages(session: Session, project_id: int) -> frozenset[int]:
    """Only a provenance-backed closure of this exact lineage ends past due work."""
    closures = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.project_id == project_id,
            DependencyEvent.event_type == "closure",
            DependencyEvent.closes_commitment_lineage_id.is_not(None),
            DependencyEvent.attribution_state == "resolved",
            DependencyEvent.stated_external_org_id.is_not(None),
            current_statement_event_filter(DependencyEvent.id),
        )
    ).all()
    return frozenset(
        closure.closes_commitment_lineage_id
        for closure in closures
        if _is_provenance_backed_closure(session, closure)
    )


def _source_candidate_ids(session: Session, project_id: int) -> dict[int, int]:
    """Link accepted cards back to their existing guided statement screen."""
    rows = session.execute(
        select(
            StatementCoordinationReceipt.commitment_lineage_id,
            StatementCoordinationReceipt.candidate_id,
        )
        .join(
            CommitmentLineage,
            CommitmentLineage.id == StatementCoordinationReceipt.commitment_lineage_id,
        )
        .outerjoin(
            StatementCoordinationReversal,
            StatementCoordinationReversal.receipt_id == StatementCoordinationReceipt.id,
        )
        .where(
            CommitmentLineage.project_id == project_id,
            StatementCoordinationReversal.id.is_(None),
        )
        .order_by(
            StatementCoordinationReceipt.commitment_lineage_id,
            StatementCoordinationReceipt.id.desc(),
        )
    ).all()
    candidate_ids: dict[int, int] = {}
    for lineage_id, candidate_id in rows:
        candidate_ids.setdefault(lineage_id, candidate_id)
    return candidate_ids


def _source_evidence_link_ids(
    session: Session, event: DependencyEvent
) -> tuple[int, ...]:
    """Expose exact cited source identities without turning them into a Dependency."""
    return tuple(
        session.scalars(
            select(DependencyEventEvidence.evidence_link_id)
            .join(EvidenceLink, EvidenceLink.id == DependencyEventEvidence.evidence_link_id)
            .where(
                DependencyEventEvidence.event_id == event.id,
                EvidenceLink.verified.is_(True),
            )
            .order_by(DependencyEventEvidence.evidence_link_id)
        ).all()
    )


def _is_provenance_backed_closure(
    session: Session, closure: DependencyEvent
) -> bool:
    """A cited closure must still match its registered page and quote exactly."""
    if closure.source_kind == "verbal":
        return closure.event_date is not None
    if closure.source_kind != "cited":
        return False
    evidence_rows = session.execute(
        select(EvidenceLink)
        .join(
            DependencyEventEvidence,
            DependencyEventEvidence.evidence_link_id == EvidenceLink.id,
        )
        .where(
            DependencyEventEvidence.event_id == closure.id,
            EvidenceLink.verified.is_(True),
        )
    ).scalars()
    for evidence in evidence_rows:
        try:
            validate_cited_statement_evidence(
                session,
                CitedStatementEvidence(
                    evidence.document_id,
                    evidence.page_no,
                    evidence.quote,
                ),
                closure.project_id,
            )
        except StatementRefusal:
            continue
        return True
    return False


def _unplaced_statement_items(session: Session, project_id: int) -> tuple[WorkItem, ...]:
    """Candidates remain lower-priority work while a coordinator must place them."""
    return tuple(
        WorkItem(
            kind="candidate",
            commitment_lineage_id=None,
            statement_event_id=None,
            dependency_id=None,
            candidate_id=item["candidate"].id,
            source_candidate_id=None,
            timing_text=item["committed_date"],
            attention_reason_codes=("unplaced_statement",),
            past_due=None,
        )
        for item in waiting_statements(session, project_id)
    )
