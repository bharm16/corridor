"""Derive the coordinator's one-item-per-statement work list.

The Project Record keeps all External Party facts and the Ledger keeps its
Dependencies. Neither can safely stand in for the coordinator's work:
an unknown-scope Commitment is real work but does not belong to an invented
Dependency.  This module is the public read seam for that gap.  It groups
current statement facts by Commitment Lineage, preserves timing precision, and
returns derived Attention Reasons without storing flags or copying facts.  It
also uses source-visible Candidate proposals only to select a bounded set for
human review; those cards remain explicitly outside the Project Record, and every
other actionable proposal stays searchable in the same read model.
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
from corridor.disputes import contradicted_fields
from corridor.models import (
    Candidate,
    CommitmentLineage,
    Dependency,
    ExternalPartyStatement,
    StatementEvidence,
    CommitmentScopeDecision,
    StatementTimingRecord,
    Document,
    EvidenceLink,
    EventAdmissionOutcome,
    ExternalParty,
    PolicyRun,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
    WorkDecision,
    is_critical,
)
from corridor.statement_lifecycle import (
    CurrentStatementObservation,
    current_statement_event_filter,
    observe_current_statements,
)
from corridor.supersession import actionable_candidate_query
from corridor.supersession_review import ordinary_candidate_ids
from corridor.work_decisions import (
    DEFERRAL,
    NEXT_ACTION,
    current_statement_decision_tails,
)


WORK_LIST_RULESET_VERSION = "work-list-v2"
MAX_IMMEDIATE_WORK_ITEMS = 20
WORK_BACKLOG_PAGE_SIZE = 25
CANDIDATE_BACKLOG_PAGE_SIZE = 25

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
class CandidateSource:
    """Source-first context for a proposal that has no Ledger authority."""

    context_external_org: str | None
    context_reference: str | None
    context_description: str | None
    quote: str | None
    document_name: str
    document_date: date | None
    page: int | None


@dataclass(frozen=True)
class WorkItem:
    """One current coordinator question, never one row per reason."""

    kind: str
    commitment_lineage_id: int | None
    statement_event_id: int | None
    dependency_id: int | None
    candidate_id: int | None
    candidate_kind: str | None
    source_candidate_id: int | None
    timing_text: str | None
    attention_reason_codes: tuple[str, ...]
    past_due: PastDueCommitment | None
    deferral_reason: str | None = None
    return_date: date | None = None
    candidate_source: CandidateSource | None = None
    candidate_decision: str | None = None
    display_name: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class WorkList:
    """A bounded read of immediate, deferred, and extracted-statement work."""

    project_id: int
    evaluated_on: date
    ruleset_version: str
    immediate: tuple[WorkItem, ...]
    backlog: tuple[WorkItem, ...]
    backlog_total: int
    backlog_page: int
    backlog_pages: int
    backlog_search: str
    candidate_backlog: tuple[WorkItem, ...]
    candidate_backlog_total: int
    candidate_backlog_page: int
    candidate_backlog_pages: int
    candidate_search: str
    event_candidate_total: int
    dependency_candidate_total: int


@dataclass(frozen=True)
class _StatementWorkState:
    """Batched current facts needed to derive one population of statement work."""

    evidence_ids_by_event: dict[int, tuple[int, ...]]
    decision_tails: dict[tuple[int, str], WorkDecision]
    receipts_by_decision: dict[int, StatementCoordinationReceipt]
    observations_by_lineage: dict[int, CurrentStatementObservation]


def build_work_list(
    session: Session,
    project_id: int,
    *,
    today: date | None = None,
    backlog_search: str = "",
    backlog_page: int = 1,
    candidate_search: str = "",
    candidate_page: int = 1,
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
            ExternalPartyStatement,
            StatementTimingRecord,
            CommitmentScopeDecision,
            CommitmentLineage,
        )
        .join(
            StatementTimingRecord,
            StatementTimingRecord.event_id == ExternalPartyStatement.id,
        )
        .join(
            CommitmentScopeDecision,
            CommitmentScopeDecision.event_id == ExternalPartyStatement.id,
        )
        .join(
            CommitmentLineage,
            CommitmentLineage.id == ExternalPartyStatement.commitment_lineage_id,
        )
        .where(
            ExternalPartyStatement.project_id == project_id,
            ExternalPartyStatement.event_type.in_(
                ("commitment", "committed_date_change")
            ),
            ExternalPartyStatement.attribution_state == "resolved",
            ExternalPartyStatement.stated_external_org_id.is_not(None),
            ExternalPartyStatement.commitment_lineage_id.is_not(None),
            StatementTimingRecord.kind == "new",
            current_statement_event_filter(ExternalPartyStatement.id),
            current_scope_decision_filter(),
        )
        .order_by(ExternalPartyStatement.id)
    ).all()

    candidate_ids = _source_candidate_ids(session, project_id)
    statement_state = _statement_work_state(session, rows)
    immediate: list[WorkItem] = []
    backlog: list[WorkItem] = []
    for event, timing, scope, lineage in rows:
        is_closed = event.commitment_lineage_id in closed_lineages
        reason_codes: list[str] = []
        past_due = (
            None
            if is_closed
            else _past_due(
                event,
                timing,
                evaluated_on,
                statement_state.evidence_ids_by_event.get(event.id, ()),
            )
        )
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
        if not lineage.internal_owner and (
            not is_closed or lineage.next_action is not None
        ):
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
            candidate_kind=None,
            source_candidate_id=candidate_ids.get(event.commitment_lineage_id),
            timing_text=_display_timing(timing),
            attention_reason_codes=tuple(
                sorted(reason_codes, key=_REASON_ORDER.__getitem__)
            ),
            past_due=past_due,
            deferral_reason=lineage.deferral_reason,
            return_date=lineage.deferral_return_date,
            display_name=event.stated_party,
            description=event.description,
        )
        is_immediate = _is_immediate(lineage, evaluated_on) or _statement_changed(
            lineage, statement_state
        )
        (immediate if is_immediate else backlog).append(item)

    for item, projection in _dependency_items(session, project_id):
        (immediate if _is_immediate(projection, evaluated_on) else backlog).append(item)
    immediate.sort(key=_item_sort_key)
    (
        candidate_leads,
        all_candidates,
        event_candidate_total,
        dependency_candidate_total,
    ) = _candidate_items(session, project_id)
    overflow = immediate[MAX_IMMEDIATE_WORK_ITEMS:]
    immediate = immediate[:MAX_IMMEDIATE_WORK_ITEMS]
    open_slots = MAX_IMMEDIATE_WORK_ITEMS - len(immediate)
    immediate.extend(candidate_leads[:open_slots])
    backlog.extend(overflow)
    backlog.sort(key=_item_sort_key)
    normalized_backlog_search = " ".join(backlog_search.split())
    if normalized_backlog_search:
        backlog = [
            item
            for item in backlog
            if _work_item_matches(item, normalized_backlog_search)
        ]
    backlog_total = len(backlog)
    backlog_pages = (
        backlog_total + WORK_BACKLOG_PAGE_SIZE - 1
    ) // WORK_BACKLOG_PAGE_SIZE
    resolved_backlog_page = max(1, backlog_page)
    if backlog_pages:
        resolved_backlog_page = min(resolved_backlog_page, backlog_pages)
    backlog_start = (resolved_backlog_page - 1) * WORK_BACKLOG_PAGE_SIZE
    backlog_page_items = backlog[backlog_start : backlog_start + WORK_BACKLOG_PAGE_SIZE]
    # Quick cards are only prioritization. Keep the same rows in this complete
    # searchable collection so the immediate-card cap can never hide work.
    candidate_backlog = all_candidates
    normalized_search = " ".join(candidate_search.split())
    if normalized_search:
        candidate_backlog = tuple(
            item
            for item in candidate_backlog
            if _candidate_matches(item, normalized_search)
        )
    candidate_backlog_total = len(candidate_backlog)
    candidate_backlog_pages = (
        candidate_backlog_total + CANDIDATE_BACKLOG_PAGE_SIZE - 1
    ) // CANDIDATE_BACKLOG_PAGE_SIZE
    resolved_page = max(1, candidate_page)
    if candidate_backlog_pages:
        resolved_page = min(resolved_page, candidate_backlog_pages)
    start = (resolved_page - 1) * CANDIDATE_BACKLOG_PAGE_SIZE
    candidate_backlog_page = candidate_backlog[
        start : start + CANDIDATE_BACKLOG_PAGE_SIZE
    ]
    return WorkList(
        project_id=project_id,
        evaluated_on=evaluated_on,
        ruleset_version=WORK_LIST_RULESET_VERSION,
        immediate=tuple(immediate),
        backlog=tuple(backlog_page_items),
        backlog_total=backlog_total,
        backlog_page=resolved_backlog_page,
        backlog_pages=backlog_pages,
        backlog_search=normalized_backlog_search,
        candidate_backlog=tuple(candidate_backlog_page),
        candidate_backlog_total=candidate_backlog_total,
        candidate_backlog_page=resolved_page,
        candidate_backlog_pages=candidate_backlog_pages,
        candidate_search=normalized_search,
        event_candidate_total=event_candidate_total,
        dependency_candidate_total=dependency_candidate_total,
    )


def _past_due(
    event: ExternalPartyStatement,
    timing: StatementTimingRecord,
    evaluated_on: date,
    source_evidence_link_ids: tuple[int, ...],
) -> PastDueCommitment | None:
    due_after = party_commitment_due_after(timing)
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
        source_evidence_link_ids=source_evidence_link_ids,
        due_after=due_after,
    )


def party_commitment_due_after(timing: StatementTimingRecord) -> date | None:
    """Return the last supported day before a party-level Commitment is due.

    Exact-day and month timing can establish a boundary; approximate and
    legacy-unknown wording cannot.  Report readers use this same public rule
    so a report and the coordinator work list never disagree about overdue.
    """
    return timing.end_date if timing.precision in ("day", "month") else None


def _display_timing(timing: StatementTimingRecord) -> str:
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
    lineage: CommitmentLineage,
    state: _StatementWorkState,
) -> bool:
    """Return delayed work when the External Party fact it answered changes."""
    observation = state.observations_by_lineage.get(lineage.id)
    if observation is None:
        return True
    deferral = state.decision_tails.get((lineage.id, DEFERRAL))
    action = state.decision_tails.get((lineage.id, NEXT_ACTION))
    decision = (
        deferral
        if deferral is not None and deferral.after_value is not None
        else action
    )
    if decision is None:
        return False

    receipt = state.receipts_by_decision.get(decision.id)
    if receipt is not None:
        return (
            receipt.dependency_event_id != observation.statement_event_id
            or receipt.scope_decision_id != observation.scope_decision_id
            or receipt.milestone_impact_decision_id
            != observation.milestone_impact_decision_id
        )

    return (
        (
            decision.observed_statement_event_id is not None
            and decision.observed_statement_event_id != observation.statement_event_id
        )
        or (
            decision.observed_scope_decision_id is not None
            and decision.observed_scope_decision_id != observation.scope_decision_id
        )
        or (
            decision.observed_milestone_impact_decision_id is not None
            and decision.observed_milestone_impact_decision_id
            != observation.milestone_impact_decision_id
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
        )
        .order_by(Dependency.id)
    ).all()
    external_org_ids = {
        dependency.external_org_id
        for dependency in dependencies
        if dependency.external_org_id is not None
    }
    external_org_names = dict(
        session.execute(
            select(ExternalParty.id, ExternalParty.name).where(
                ExternalParty.id.in_(external_org_ids)
            )
        ).all()
    )
    disputed = contradicted_fields(
        session, [dependency.id for dependency in dependencies]
    )
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
                    candidate_kind=None,
                    source_candidate_id=None,
                    timing_text=None,
                    attention_reason_codes=tuple(
                        sorted(reason_codes, key=_REASON_ORDER.__getitem__)
                    ),
                    past_due=None,
                    deferral_reason=dependency.deferral_reason,
                    return_date=dependency.deferral_return_date,
                    display_name=external_org_names.get(dependency.external_org_id),
                    description=dependency.title,
                ),
                dependency,
            )
        )
    return tuple(items)


def _closed_commitment_lineages(session: Session, project_id: int) -> frozenset[int]:
    """Only a provenance-backed closure of this exact lineage ends past due work."""
    closures = session.scalars(
        select(ExternalPartyStatement).where(
            ExternalPartyStatement.project_id == project_id,
            ExternalPartyStatement.event_type == "closure",
            ExternalPartyStatement.closes_commitment_lineage_id.is_not(None),
            ExternalPartyStatement.attribution_state == "resolved",
            ExternalPartyStatement.stated_external_org_id.is_not(None),
            current_statement_event_filter(ExternalPartyStatement.id),
        )
    ).all()
    return frozenset(
        closure.closes_commitment_lineage_id
        for closure in closures
        if _is_provenance_backed_closure(session, closure)
    )


def _source_candidate_ids(session: Session, project_id: int) -> dict[int, int]:
    """Link accepted cards back to their existing guided statement screen."""
    admitted_rows = session.execute(
        select(
            EventAdmissionOutcome.commitment_lineage_id,
            EventAdmissionOutcome.candidate_id,
        )
        .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project_id,
            EventAdmissionOutcome.outcome == "admitted",
            EventAdmissionOutcome.commitment_lineage_id.is_not(None),
        )
        .order_by(EventAdmissionOutcome.id.desc())
    ).all()
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
    candidate_ids: dict[int, int] = {
        lineage_id: candidate_id
        for lineage_id, candidate_id in admitted_rows
        if lineage_id is not None
    }
    for lineage_id, candidate_id in rows:
        candidate_ids.setdefault(lineage_id, candidate_id)
    return candidate_ids


def _statement_work_state(session: Session, rows) -> _StatementWorkState:
    """Read current Evidence and plan observations once for all statement rows."""
    event_ids = tuple(event.id for event, _timing, _scope, _lineage in rows)
    lineage_ids = tuple(lineage.id for _event, _timing, _scope, lineage in rows)

    evidence_ids_by_event: dict[int, list[int]] = {}
    if event_ids:
        evidence_rows = session.execute(
            select(
                StatementEvidence.event_id,
                StatementEvidence.evidence_link_id,
            )
            .join(
                EvidenceLink,
                EvidenceLink.id == StatementEvidence.evidence_link_id,
            )
            .where(
                StatementEvidence.event_id.in_(event_ids),
                EvidenceLink.verified.is_(True),
            )
            .order_by(
                StatementEvidence.event_id,
                StatementEvidence.evidence_link_id,
            )
        ).all()
        for event_id, evidence_link_id in evidence_rows:
            evidence_ids_by_event.setdefault(event_id, []).append(evidence_link_id)

    decision_tails = current_statement_decision_tails(
        session,
        lineage_ids,
        fields=(DEFERRAL, NEXT_ACTION),
    )
    decision_ids = tuple(decision.id for decision in decision_tails.values())
    receipts_by_decision = (
        {
            receipt.next_action_decision_id: receipt
            for receipt in session.scalars(
                select(StatementCoordinationReceipt).where(
                    StatementCoordinationReceipt.next_action_decision_id.in_(
                        decision_ids
                    )
                )
            ).all()
        }
        if decision_ids
        else {}
    )
    return _StatementWorkState(
        evidence_ids_by_event={
            event_id: tuple(evidence_ids)
            for event_id, evidence_ids in evidence_ids_by_event.items()
        },
        decision_tails=decision_tails,
        receipts_by_decision=receipts_by_decision,
        observations_by_lineage=observe_current_statements(session, lineage_ids),
    )


def _is_provenance_backed_closure(
    session: Session, closure: ExternalPartyStatement
) -> bool:
    """A cited closure must still match its registered page and quote exactly."""
    if closure.source_kind == "verbal":
        return closure.event_date is not None
    if closure.source_kind != "cited":
        return False
    evidence_rows = session.execute(
        select(EvidenceLink)
        .join(
            StatementEvidence,
            StatementEvidence.evidence_link_id == EvidenceLink.id,
        )
        .where(
            StatementEvidence.event_id == closure.id,
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


def _candidate_items(
    session: Session, project_id: int
) -> tuple[tuple[WorkItem, ...], tuple[WorkItem, ...], int, int]:
    """Return prioritized statement cards and every ordinary Active Run proposal."""
    waiting_items = waiting_statements(session, project_id, include_attachability=False)
    dependency_candidates = tuple(
        session.scalars(
            actionable_candidate_query(project_id)
            .where(Candidate.kind == "dependency")
            .order_by(Candidate.id)
        ).all()
    )
    if dependency_candidates:
        ordinary_ids = ordinary_candidate_ids(session, project_id)
        dependency_candidates = tuple(
            candidate
            for candidate in dependency_candidates
            if candidate.id in ordinary_ids
        )
    candidates = tuple(waiting["candidate"] for waiting in waiting_items) + (
        dependency_candidates
    )
    document_ids = {candidate.source_document_id for candidate in candidates}
    documents = {
        document.id: document
        for document in session.scalars(
            select(Document).where(Document.id.in_(document_ids))
        ).all()
    }
    leads: list[tuple[int, date, int, WorkItem]] = []
    remaining: list[tuple[date, int, WorkItem]] = []
    for waiting in waiting_items:
        candidate = waiting["candidate"]
        document = documents[candidate.source_document_id]
        fields = (candidate.payload_json or {}).get("fields", {})
        event_type = fields.get("event_type")
        tier = None
        if candidate.citations_verified and event_type in (
            "slip",
            "committed_date_change",
        ):
            tier = 1
        elif (
            candidate.citations_verified
            and event_type == "commitment"
            and waiting["reason"] == "no_conflict_reference"
        ):
            tier = 2
        source = _candidate_source(candidate, document, fields)
        item = WorkItem(
            kind="candidate",
            commitment_lineage_id=None,
            statement_event_id=None,
            dependency_id=None,
            candidate_id=candidate.id,
            candidate_kind=candidate.kind,
            source_candidate_id=None,
            # Candidate timing is not yet a Ledger fact.  Its exact source
            # wording belongs on the card; a normalized date never does.
            timing_text=None,
            attention_reason_codes=("unplaced_statement",),
            past_due=None,
            candidate_source=source,
            candidate_decision=(
                "Review a possible timing change."
                if tier == 1
                else (
                    "Review who spoke, what timing is supported, and where this "
                    "statement belongs."
                )
                if tier == 2
                else "Review this extracted statement."
            ),
        )
        if tier is None:
            remaining.append(
                (
                    _candidate_context_date(
                        fields.get("event_date"), document.doc_date
                    ),
                    candidate.id,
                    item,
                )
            )
            continue
        leads.append(
            (
                tier,
                _candidate_context_date(fields.get("event_date"), document.doc_date),
                candidate.id,
                item,
            )
        )

    dependency_rows: list[tuple[date, int, WorkItem]] = []
    for candidate in dependency_candidates:
        document = documents[candidate.source_document_id]
        fields = (candidate.payload_json or {}).get("fields", {})
        dependency_rows.append(
            (
                document.doc_date or date.min,
                candidate.id,
                WorkItem(
                    kind="candidate",
                    commitment_lineage_id=None,
                    statement_event_id=None,
                    dependency_id=None,
                    candidate_id=candidate.id,
                    candidate_kind=candidate.kind,
                    source_candidate_id=None,
                    timing_text=None,
                    attention_reason_codes=("unplaced_statement",),
                    past_due=None,
                    candidate_source=_candidate_source(candidate, document, fields),
                    candidate_decision="Review this proposed constraint.",
                ),
            )
        )

    leads.sort(key=lambda row: (row[0], -row[1].toordinal(), row[2]))
    remaining.sort(key=lambda row: (-row[0].toordinal(), row[1]))
    dependency_rows.sort(key=lambda row: (-row[0].toordinal(), row[1]))
    lead_items = tuple(row[3] for row in leads)
    all_items = (
        lead_items
        + tuple(row[2] for row in remaining)
        + tuple(row[2] for row in dependency_rows)
    )
    return (
        lead_items,
        all_items,
        len(waiting_items),
        len(dependency_candidates),
    )


def _candidate_source(
    candidate: Candidate, document: Document, fields: dict
) -> CandidateSource:
    """Read wording and registry context without blessing extracted fields."""
    citations = (candidate.payload_json or {}).get("citations") or ()
    citation = next(
        (
            value
            for value in citations
            if value.get("document_id") == candidate.source_document_id
            and value.get("quote")
        ),
        None,
    )
    page = citation.get("page") if citation is not None else None
    return CandidateSource(
        context_external_org=(
            str(fields.get("external_org")).strip()
            if fields.get("external_org")
            else None
        ),
        context_reference=(
            str(fields.get("conflict_ref")).strip()
            if fields.get("conflict_ref")
            else None
        ),
        context_description=(
            str(fields.get("description")).strip()
            if fields.get("description")
            else None
        ),
        quote=str(citation.get("quote")).strip() if citation is not None else None,
        document_name=document.filename,
        document_date=document.doc_date,
        page=page if isinstance(page, int) and page > 0 else None,
    )


def _candidate_context_date(value: object, document_date: date | None) -> date:
    """Order only by the source event context, never a proposed timing."""
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return document_date or date.min


def _candidate_matches(item: WorkItem, query: str) -> bool:
    """Search human-readable source context, never policy or record ids."""
    source = item.candidate_source
    if source is None:
        return False
    values = (
        source.context_external_org,
        source.context_reference,
        source.context_description,
        source.quote,
        source.document_name,
        source.document_date.isoformat() if source.document_date else None,
        f"page {source.page}" if source.page else None,
    )
    haystack = " ".join(value for value in values if value).casefold()
    return query.casefold() in haystack


def _work_item_matches(item: WorkItem, query: str) -> bool:
    """Search accepted project-language identity, never database identifiers."""
    values = (item.display_name, item.description, item.timing_text)
    haystack = " ".join(value for value in values if value).casefold()
    return query.casefold() in haystack
