"""Atomically accept one evidence-bound External Party statement and its plan.

An Unplaced Statement needs a human to settle supported statement facts and a
project response, but the two are not the same fact.  This command keeps the
External Party statement, scope decision, Candidate disposition, and separate
Work Decision chains attributable while committing the guided Save as one act.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corridor import audit
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementRefusal,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import (
    Candidate,
    CommitmentLineage,
    DependencyEvent,
    DependencyEventScopeDecision,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.supersession import actionable_candidate_query
from corridor.verify import normalize
from corridor.work_decisions import (
    CoordinationSubject,
    WorkDecision,
    assign_internal_owner,
    current_internal_owner_decision,
    current_milestone_impact_decision,
    current_next_action_decision,
    set_milestone_impact,
    set_next_action,
)


class StatementCoordinationRefusal(ValueError):
    """The requested guided Save would not make an honest complete record."""


class StaleStatementCoordination(StatementCoordinationRefusal):
    """The screen's expected predecessors are no longer current."""


@dataclass(frozen=True)
class StatementCoordinationPredecessors:
    """Identities the coordinator read before pressing Save.

    First-time statement placement expects every lineage, scope, and plan
    predecessor to be absent.  Keeping those absence checks explicit means a
    later extension for Correct has one complete optimistic-concurrency shape.
    """

    candidate_state: str = "pending"
    commitment_lineage_id: int | None = None
    statement_event_id: int | None = None
    scope_decision_id: int | None = None
    internal_owner_decision_id: int | None = None
    next_action_decision_id: int | None = None
    milestone_impact_decision_id: int | None = None

    def as_json(self) -> dict[str, int | str | None]:
        return {
            "candidate_state": self.candidate_state,
            "commitment_lineage_id": self.commitment_lineage_id,
            "statement_event_id": self.statement_event_id,
            "scope_decision_id": self.scope_decision_id,
            "internal_owner_decision_id": self.internal_owner_decision_id,
            "next_action_decision_id": self.next_action_decision_id,
            "milestone_impact_decision_id": self.milestone_impact_decision_id,
        }


@dataclass(frozen=True)
class StatementCoordinationDraft:
    """The one public command input for the ordinary guided Save."""

    candidate_id: int
    affected_external_org_id: int
    stated_party: str
    stated_external_org_id: int
    event_date: date | None
    description: str
    new_timing: StatementTiming
    previous_timing: StatementTiming | None
    evidence: tuple[CitedStatementEvidence, ...]
    scope: StatementScope
    internal_owner_roster_entry_id: int
    next_action: str
    action_due_date: date | None
    action_due_date_unknown_reason: str | None
    milestone_impact: str | None
    milestone_ids: tuple[int, ...] = ()
    expected: StatementCoordinationPredecessors = StatementCoordinationPredecessors()


@dataclass(frozen=True)
class StatementCoordinationResult:
    """The separately attributable rows created by one grouped Save."""

    receipt: StatementCoordinationReceipt
    event: DependencyEvent
    internal_owner_decision: WorkDecision
    next_action_decision: WorkDecision
    milestone_impact_decision: WorkDecision | None


def coordinate_statement(
    session: Session,
    draft: StatementCoordinationDraft,
    *,
    principal: HumanPrincipal,
) -> StatementCoordinationResult:
    """Record an accepted statement, scope, and Coordination Plan as one act."""
    recorder = require_human_principal(principal)
    candidate = session.get(Candidate, draft.candidate_id, populate_existing=True)
    if candidate is None:
        raise StaleStatementCoordination("the statement Candidate no longer exists")

    try:
        with session.begin_nested():
            lock_project(session, candidate.project_id)
            session.refresh(candidate)
            _require_current_candidate(session, candidate, draft.expected)
            roster_entry = _require_roster_entry(
                session, candidate.project_id, draft.internal_owner_roster_entry_id
            )
            _check_expected_predecessors(session, candidate.project_id, draft.expected)

            candidate_evidence = _candidate_evidence(candidate)
            all_evidence = _deduplicate_evidence(
                (*candidate_evidence, *draft.evidence)
            )
            _require_evidence_support(draft, all_evidence)
            _require_plan_shape(draft)

            event = record_external_party_statement(
                session,
                project_id=candidate.project_id,
                affected_external_org_id=draft.affected_external_org_id,
                stated_party=draft.stated_party,
                stated_external_org_id=draft.stated_external_org_id,
                source_kind="cited",
                event_date=draft.event_date,
                description=draft.description,
                new_timing=draft.new_timing,
                previous_timing=draft.previous_timing,
                scope=draft.scope,
                created_by=recorder.subject,
                evidence=all_evidence[0],
                supporting_evidence=all_evidence[1:],
                commitment_lineage_id=draft.expected.commitment_lineage_id,
            )
            scope_decision = _current_scope_decision(session, event.id)
            if scope_decision is None:
                raise RuntimeError("the accepted statement did not receive a scope decision")

            subject = CoordinationSubject.statement(event.commitment_lineage_id)
            internal_owner_decision = assign_internal_owner(
                session, subject, roster_entry.display_name, principal=recorder
            )
            next_action_decision = set_next_action(
                session,
                subject,
                draft.next_action,
                due_date=draft.action_due_date,
                due_date_unknown_reason=draft.action_due_date_unknown_reason,
                principal=recorder,
            )
            milestone_impact_decision = None
            if event.event_type == "committed_date_change":
                milestone_impact_decision = set_milestone_impact(
                    session,
                    subject,
                    draft.milestone_impact or "",
                    milestone_ids=draft.milestone_ids,
                    principal=recorder,
                )

            candidate.state = "accepted"
            candidate.adjudicated_at = datetime.now(timezone.utc)
            audit_entry = audit.record(
                session,
                principal=recorder,
                action=audit.COORDINATE_STATEMENT,
                entity_type=audit.CANDIDATE,
                entity_id=candidate.id,
                after={
                    "candidate_id": candidate.id,
                    "commitment_lineage_id": event.commitment_lineage_id,
                    "dependency_event_id": event.id,
                    "scope_decision_id": scope_decision.id,
                    "internal_owner_decision_id": internal_owner_decision.id,
                    "next_action_decision_id": next_action_decision.id,
                    "milestone_impact_decision_id": (
                        milestone_impact_decision.id
                        if milestone_impact_decision is not None
                        else None
                    ),
                },
            )
            receipt = StatementCoordinationReceipt(
                candidate_id=candidate.id,
                commitment_lineage_id=event.commitment_lineage_id,
                dependency_event_id=event.id,
                scope_decision_id=scope_decision.id,
                internal_owner_roster_entry_id=roster_entry.id,
                internal_owner_decision_id=internal_owner_decision.id,
                next_action_decision_id=next_action_decision.id,
                milestone_impact_decision_id=(
                    milestone_impact_decision.id
                    if milestone_impact_decision is not None
                    else None
                ),
                audit_log_id=audit_entry.id,
                expected_predecessors_json=draft.expected.as_json(),
                accepted_facts_json=_accepted_facts(
                    draft, event, scope_decision.id, roster_entry.id, all_evidence
                ),
                candidate_payload_sha256=_payload_sha256(candidate),
                recorded_by=recorder.subject,
            )
            session.add(receipt)
            session.flush([receipt])
    except StaleStatementCoordination:
        raise
    except (StatementRefusal, ValueError, IntegrityError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc

    return StatementCoordinationResult(
        receipt=receipt,
        event=event,
        internal_owner_decision=internal_owner_decision,
        next_action_decision=next_action_decision,
        milestone_impact_decision=milestone_impact_decision,
    )


def _require_current_candidate(
    session: Session,
    candidate: Candidate,
    expected: StatementCoordinationPredecessors,
) -> None:
    if candidate.kind != "event":
        raise StatementCoordinationRefusal("only an Unplaced Statement can use guided coordination")
    if candidate.state != expected.candidate_state:
        raise StaleStatementCoordination(
            f"the statement Candidate is already {candidate.state}; reload its newer state"
        )
    if expected.candidate_state != "pending":
        raise StatementCoordinationRefusal("a guided Save must begin from a pending Candidate")
    if not candidate.citations_verified:
        raise StatementCoordinationRefusal("the statement Candidate has unverified Evidence")
    actionable = session.scalar(
        actionable_candidate_query(candidate.project_id)
        .where(Candidate.id == candidate.id, Candidate.kind == "event")
        .limit(1)
    )
    if actionable is None:
        raise StaleStatementCoordination(
            "the statement Candidate is no longer in the current actionable scope"
        )


def _require_roster_entry(
    session: Session, project_id: int, roster_entry_id: int
) -> ProjectRosterEntry:
    entry = session.get(ProjectRosterEntry, roster_entry_id)
    if entry is None or entry.project_id != project_id or not entry.active:
        raise StatementCoordinationRefusal(
            "Internal Owner must be selected from this project's active roster"
        )
    try:
        HumanPrincipal(entry.principal_subject)
    except InvalidHumanPrincipal as exc:
        raise StatementCoordinationRefusal(
            "the selected project roster entry has no valid human identity"
        ) from exc
    return entry


def _check_expected_predecessors(
    session: Session,
    project_id: int,
    expected: StatementCoordinationPredecessors,
) -> None:
    """Compare every screen predecessor before the writer appends anything."""
    if expected.commitment_lineage_id is None:
        values = (
            expected.statement_event_id,
            expected.scope_decision_id,
            expected.internal_owner_decision_id,
            expected.next_action_decision_id,
            expected.milestone_impact_decision_id,
        )
        if any(value is not None for value in values):
            raise StaleStatementCoordination(
                "the guided Save names predecessors without a Commitment Lineage"
            )
        return

    lineage = session.get(CommitmentLineage, expected.commitment_lineage_id)
    if lineage is None or lineage.project_id != project_id:
        raise StaleStatementCoordination("the expected Commitment Lineage is no longer current")
    event = _current_statement_event(session, lineage.id)
    if event is None or event.id != expected.statement_event_id:
        raise StaleStatementCoordination("the statement changed; reload the newer state")
    scope_decision = _current_scope_decision(session, event.id)
    if (
        scope_decision is None
        or scope_decision.id != expected.scope_decision_id
    ):
        raise StaleStatementCoordination("the statement scope changed; reload the newer state")
    subject = CoordinationSubject.statement(lineage.id)
    actual = (
        _decision_id(current_internal_owner_decision(session, subject)),
        _decision_id(current_next_action_decision(session, subject)),
        _decision_id(current_milestone_impact_decision(session, subject)),
    )
    expected_ids = (
        expected.internal_owner_decision_id,
        expected.next_action_decision_id,
        expected.milestone_impact_decision_id,
    )
    if actual != expected_ids:
        raise StaleStatementCoordination("the Coordination Plan changed; reload the newer state")


def _current_statement_event(session: Session, lineage_id: int) -> DependencyEvent | None:
    successor = DependencyEvent.__table__.alias("successor")
    return session.scalar(
        select(DependencyEvent)
        .where(
            DependencyEvent.commitment_lineage_id == lineage_id,
            ~select(successor.c.id)
            .where(successor.c.supersedes_event_id == DependencyEvent.id)
            .exists(),
        )
        .order_by(DependencyEvent.id)
    )


def _current_scope_decision(
    session: Session, event_id: int
) -> DependencyEventScopeDecision | None:
    successor = DependencyEventScopeDecision.__table__.alias("successor")
    return session.scalar(
        select(DependencyEventScopeDecision)
        .where(
            DependencyEventScopeDecision.event_id == event_id,
            ~select(successor.c.id)
            .where(
                successor.c.supersedes_scope_decision_id
                == DependencyEventScopeDecision.id
            )
            .exists(),
        )
        .order_by(DependencyEventScopeDecision.id)
    )


def _candidate_evidence(candidate: Candidate) -> tuple[CitedStatementEvidence, ...]:
    citations = (candidate.payload_json or {}).get("citations") or ()
    evidence: list[CitedStatementEvidence] = []
    for citation in citations:
        try:
            item = CitedStatementEvidence(
                document_id=int(citation["document_id"]),
                page_no=int(citation["page"]),
                quote=str(citation["quote"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StatementCoordinationRefusal(
                "the Candidate has no complete verified Evidence identity"
            ) from exc
        evidence.append(item)
    if not evidence:
        raise StatementCoordinationRefusal("the Candidate has no verified Evidence")
    return tuple(evidence)


def _deduplicate_evidence(
    evidence: tuple[CitedStatementEvidence, ...]
) -> tuple[CitedStatementEvidence, ...]:
    deduplicated: list[CitedStatementEvidence] = []
    seen: set[tuple[int, int, str]] = set()
    for item in evidence:
        identity = (item.document_id, item.page_no, item.quote.strip())
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(item)
    if not deduplicated:
        raise StatementCoordinationRefusal("the statement needs verified Evidence")
    return tuple(deduplicated)


def _require_evidence_support(
    draft: StatementCoordinationDraft,
    evidence: tuple[CitedStatementEvidence, ...],
) -> None:
    """Make source evidence, never confirmation, carry party and timing facts."""
    quotes = tuple(normalize(item.quote) for item in evidence)
    party = normalize(draft.stated_party)
    if not party or not any(party in quote for quote in quotes):
        raise StatementCoordinationRefusal(
            "verified Evidence must name the stated External Party"
        )
    for timing in (draft.previous_timing, draft.new_timing):
        if timing is None:
            continue
        phrase = normalize(timing.text)
        if not phrase or not any(phrase in quote for quote in quotes):
            raise StatementCoordinationRefusal(
                "verified Evidence must preserve each stated timing's source wording"
            )


def _require_plan_shape(draft: StatementCoordinationDraft) -> None:
    if draft.previous_timing is None and draft.milestone_impact is not None:
        raise StatementCoordinationRefusal(
            "only a Committed Date Change records Milestone Impact"
        )
    if draft.previous_timing is not None and draft.milestone_impact is None:
        raise StatementCoordinationRefusal(
            "a Committed Date Change needs a Milestone Impact"
        )


def _accepted_facts(
    draft: StatementCoordinationDraft,
    event: DependencyEvent,
    scope_decision_id: int,
    roster_entry_id: int,
    evidence: tuple[CitedStatementEvidence, ...],
) -> dict:
    return {
        "affected_external_org_id": draft.affected_external_org_id,
        "stated_party": draft.stated_party.strip(),
        "stated_external_org_id": draft.stated_external_org_id,
        "event_date": draft.event_date.isoformat() if draft.event_date else None,
        "description": draft.description.strip(),
        "event_type": event.event_type,
        "timing_direction": event.timing_direction,
        "new_timing": _timing_json(draft.new_timing),
        "previous_timing": _timing_json(draft.previous_timing),
        "scope": {
            "mode": draft.scope.mode,
            "scope_decision_id": scope_decision_id,
        },
        "coordination_plan": {
            "internal_owner_roster_entry_id": roster_entry_id,
            "next_action": draft.next_action.strip(),
            "action_due_date": (
                draft.action_due_date.isoformat() if draft.action_due_date else None
            ),
            "action_due_date_unknown_reason": draft.action_due_date_unknown_reason,
            "milestone_impact": draft.milestone_impact,
            "milestone_ids": list(draft.milestone_ids),
        },
        "evidence": [
            {
                "document_id": item.document_id,
                "page_no": item.page_no,
                "quote": item.quote.strip(),
            }
            for item in evidence
        ],
    }


def _timing_json(timing: StatementTiming | None) -> dict | None:
    if timing is None:
        return None
    return {
        "text": timing.text,
        "precision": timing.precision,
        "start_date": timing.start_date.isoformat() if timing.start_date else None,
        "end_date": timing.end_date.isoformat() if timing.end_date else None,
    }


def _payload_sha256(candidate: Candidate) -> str:
    canonical = json.dumps(
        candidate.payload_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _decision_id(decision: WorkDecision | None) -> int | None:
    return decision.id if decision is not None else None
