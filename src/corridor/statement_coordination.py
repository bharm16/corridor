"""Atomically accept one evidence-bound External Party statement and its plan.

An Unplaced Statement needs a human to settle supported statement facts and a
project response, but the two are not the same fact.  This command keeps the
External Party statement, scope decision, Candidate disposition, and separate
Work Decision chains attributable while committing the guided Save as one act.

The earlier attach-to-Dependency path was intentionally narrow: it forced one
known Dependency scope and could not collect a second verified quote for
attribution context.  Reusing it here would invent scope for the SH 99 cases,
so this module owns the grouped command while preserving the shared statement
and Work Decision writers beneath it.

A mechanically admitted Commitment enters with its accepted facts already
fixed. This module also owns the smaller human residue on that path: reading
the current fact, then recording only its Internal Owner and Next Action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corridor import refusals
from corridor import audit, notifications, presentation
from corridor.presentation import (
    CoordinationPlan,
    CoordinationResidue,
    RESIDUAL_NEXT_ACTION,
    RESIDUAL_OWNER,
    read_admitted_statement_residue,
)
from corridor.coordination_history import coordination_operation, sync_coordination_reversals
from corridor.candidate_statement_facts import prepare_candidate_statement_facts
from corridor.statement_spine import (
    correct_statement_scope_on_spine,
    mark_statement_do_not_add_on_spine,
    record_cited_statement_on_spine,
    restore_statement_do_not_add_on_spine,
)
from corridor.external_statements import (
    CitedStatementEvidence,
    EvidenceBoundPartyResolution,
    StatementRefusal,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
    record_statement_scope_decision,
)
from corridor.models import (
    Candidate,
    CandidateDisposition,
    CommitmentLineage,
    AuditLog,
    Dependency,
    ExternalPartyStatement,
    StatementEvidence,
    CommitmentScopeMembership,
    CommitmentScopeDecision,
    Document,
    DependencyAdmissionOutcome,
    EvidenceLink,
    ExternalParty,
    EventAdmissionOutcome,
    PolicyRun,
    ProjectRosterEntry,
    ReportRun,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
    StatementCoordinationReversalEffect,
    WorkDecisionMilestoneImpact,
)
from corridor.principals import (
    HumanPrincipal,
    InvalidHumanPrincipal,
    require_human_principal,
)
from corridor.measurement_cases import (
    record_do_not_add_case,
    record_do_not_add_reversal_case,
    record_statement_fact_correction_case,
    record_statement_scope_correction_case,
)
from corridor.project_lock import lock_project
from corridor.supersession import actionable_candidate_query
from corridor.supersession_review import ordinary_candidate_ids
from corridor.statement_lifecycle import (
    current_candidate_disposition,
    current_lineage_statement,
    current_scope_decision_filter,
    current_statement_event_filter,
    observe_current_statement,
)
from corridor.verify import normalize
from corridor.work_decisions import (
    CoordinationDecisionRefusal,
    CoordinationSubject,
    StaleNextAction,
    WorkDecision,
    assign_internal_owner,
    cancel_next_action,
    complete_next_action,
    current_deferral_decision,
    current_internal_owner_decision,
    current_next_action_decision,
    defer_work,
    set_milestone_impact,
    set_next_action,
)


class StatementCoordinationRefusal(refusals.Refusal, ValueError):
    """The requested guided Save would not make an honest complete record."""

    refusal_kind = refusals.MALFORMED_INPUT


class StaleStatementCoordination(StatementCoordinationRefusal):
    """The screen's expected predecessors are no longer current."""

    refusal_kind = refusals.STALE


class StatementCoordinationUndoRefusal(StatementCoordinationRefusal):
    """Undo would reverse a result that later work now depends on."""

    refusal_kind = refusals.CONFLICT


NOT_RELEVANT_REASONS = frozenset(
    {
        "not_an_external_party_statement",
        "outside_project_scope",
        "duplicate_statement",
        "insufficient_source_context",
    }
)

STATEMENT_NEXT_ACTION_CHOICES = (
    "Confirm the organization and which constraints the statement applies to",
    "Confirm the stated timing with the organization",
    "Coordinate the selected constraints",
    "Obtain additional supporting documents for this statement",
)

# Existing submitted forms and recorded decisions keep their exact wording.
# New forms offer only the current terms above; accepting an old value must
# never rewrite a historical action or its receipt (ADR-0048).
_LEGACY_STATEMENT_NEXT_ACTION_CHOICES = (
    "Confirm the External Party and Commitment Scope",
    "Confirm the stated timing with the External Party",
    "Coordinate the selected Dependencies",
    "Obtain additional Evidence for this statement",
)

# The codes and their customer words are minted together in
# `corridor.presentation`; these names stay the ones callers already import.
CLOSURE_TARGET_GAP = presentation.CLOSURE_TARGET_GAP
CLOSURE_TARGET_RELATIONSHIP_GAP = presentation.CLOSURE_TARGET_RELATIONSHIP_GAP
CLOSURE_TARGET_AMBIGUOUS_GAP = presentation.CLOSURE_TARGET_AMBIGUOUS_GAP
CLOSURE_PARTY_GAP = presentation.CLOSURE_PARTY_GAP


@dataclass(frozen=True)
class CandidateAuthorityGap:
    """One current Candidate whose named authority gap remains unresolved."""

    code: str
    title: str
    detail: str
    source_family: str
    abstention_reason: str | None = None
    outcome_id: int | None = None
    policy_run_id: int | None = None
    affected_external_org_id: int | None = None
    matching_commitment_lineage_ids: tuple[int, ...] = ()


PendingStatementAuthorityGap = CandidateAuthorityGap


@dataclass(frozen=True)
class AdmittedStatementEvidence:
    """One verified Evidence quote supporting an admitted statement."""

    filename: str
    page_no: int
    quote: str


@dataclass(frozen=True)
class AdmittedStatementCoordination:
    """Accepted statement facts plus exactly one residual human decision."""

    event: ExternalPartyStatement
    affected_party_name: str
    lineage: CommitmentLineage
    scope: CommitmentScopeDecision
    outcome: EventAdmissionOutcome
    policy_run: PolicyRun
    evidence: tuple[AdmittedStatementEvidence, ...]
    scope_dependencies: tuple[Dependency, ...]
    roster: tuple[ProjectRosterEntry, ...]
    # The one coordination authority reading (`corridor.presentation`).  The two
    # published names below are views of it, so no caller and no entry-point
    # guard restates the owner-then-Next-Action ordering.
    residue: CoordinationResidue
    # The live Next Action a coordinator may now complete, cancel, or defer,
    # and the current deferral if immediate work was already delayed.  These
    # are the exact Coordination Decision tails the close and deferral commands
    # bind to, never the lineage projection alone (ADR-0038).
    next_action_decision: WorkDecision | None = None
    deferral_decision: WorkDecision | None = None

    @property
    def next_decision(self) -> str | None:
        """The one residual decision this accepted Commitment still needs."""
        return self.residue.decision

    @property
    def authority_gap(self) -> str | None:
        """Why the residual decision cannot be recorded, in customer words."""
        return self.residue.authority_gap

    @property
    def next_action_choices(self) -> tuple[str, ...]:
        return STATEMENT_NEXT_ACTION_CHOICES

    @property
    def can_close_next_action(self) -> bool:
        """True once a live Next Action exists to complete, cancel, or defer."""
        return (
            self.next_action_decision is not None
            and self.next_action_decision.after_value is not None
        )


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
    event: ExternalPartyStatement
    internal_owner_decision: WorkDecision
    next_action_decision: WorkDecision
    milestone_impact_decision: WorkDecision | None


@dataclass(frozen=True)
class StatementScopeCorrection:
    """The exact scope tail a coordinator read before correcting it."""

    candidate_id: int
    event_id: int
    expected_scope_decision_id: int
    scope: StatementScope


@dataclass(frozen=True)
class StatementFactCorrectionDraft:
    """One supported factual successor in an existing Commitment Lineage."""

    candidate_id: int
    expected_statement_event_id: int
    affected_external_org_id: int
    stated_party: str
    stated_external_org_id: int
    event_date: date | None
    description: str
    new_timing: StatementTiming
    previous_timing: StatementTiming | None
    evidence: tuple[CitedStatementEvidence, ...]


def read_admitted_statement_coordination(
    session: Session,
    project_id: int,
    candidate_id: int,
) -> AdmittedStatementCoordination | None:
    """Read accepted facts and the one remaining human statement decision."""
    from corridor.event_admission import UNKNOWN_SCOPE_POLICY_VERSION

    candidate = session.get(Candidate, candidate_id, populate_existing=True)
    if (
        candidate is None
        or candidate.project_id != project_id
        or candidate.kind != "event"
        or candidate.state != "accepted"
    ):
        return None
    row = session.execute(
        select(EventAdmissionOutcome, PolicyRun)
        .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project_id,
            PolicyRun.policy_version == UNKNOWN_SCOPE_POLICY_VERSION,
            EventAdmissionOutcome.candidate_id == candidate.id,
            EventAdmissionOutcome.outcome == "admitted",
            EventAdmissionOutcome.dependency_event_id.is_not(None),
        )
        .order_by(EventAdmissionOutcome.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    outcome, policy_run = row
    if outcome.commitment_lineage_id is None:
        return None
    observation = observe_current_statement(session, outcome.commitment_lineage_id)
    if (
        observation is None
        or observation.event.project_id != project_id
        or observation.scope_decision is None
    ):
        return None
    event = observation.event
    admitted_event = session.get(ExternalPartyStatement, outcome.dependency_event_id)
    if (
        admitted_event is None
        or admitted_event.project_id != project_id
        or admitted_event.commitment_lineage_id != event.commitment_lineage_id
    ):
        return None
    lineage = session.get(
        CommitmentLineage,
        event.commitment_lineage_id,
        populate_existing=True,
    )
    if lineage is None or lineage.project_id != project_id:
        return None
    scope = observation.scope_decision
    affected_party = session.get(ExternalParty, event.affected_external_org_id)
    scope_dependency_ids = tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id)
            .where(CommitmentScopeMembership.scope_decision_id == scope.id)
            .order_by(CommitmentScopeMembership.dependency_id)
        ).all()
    )
    scope_dependencies = tuple(
        session.scalars(
            select(Dependency)
            .where(
                Dependency.project_id == project_id,
                Dependency.id.in_(scope_dependency_ids),
            )
            .order_by(Dependency.ref_code)
        ).all()
    )
    evidence = tuple(
        AdmittedStatementEvidence(document.filename, link.page_no, link.quote)
        for _membership, link, document in session.execute(
            select(StatementEvidence, EvidenceLink, Document)
            .join(
                EvidenceLink,
                EvidenceLink.id == StatementEvidence.evidence_link_id,
            )
            .join(Document, Document.id == EvidenceLink.document_id)
            .where(
                StatementEvidence.event_id == event.id,
                EvidenceLink.verified.is_(True),
                Document.project_id == project_id,
            )
            .order_by(StatementEvidence.evidence_link_id)
        ).all()
    )
    roster = tuple(
        session.scalars(
            select(ProjectRosterEntry)
            .where(
                ProjectRosterEntry.project_id == project_id,
                ProjectRosterEntry.active.is_(True),
            )
            .order_by(ProjectRosterEntry.display_name)
            .execution_options(populate_existing=True)
        ).all()
    )
    residue = read_admitted_statement_residue(
        CoordinationPlan(lineage.internal_owner, lineage.next_action),
        evidence_available=bool(evidence),
        roster_available=bool(roster),
    )
    subject = CoordinationSubject.statement(lineage.id)
    action_tail = current_next_action_decision(session, subject)
    next_action_decision = (
        action_tail if action_tail is not None and action_tail.after_value is not None
        else None
    )
    deferral_tail = current_deferral_decision(session, subject)
    deferral_decision = (
        deferral_tail
        if deferral_tail is not None and deferral_tail.after_value is not None
        else None
    )
    return AdmittedStatementCoordination(
        event=event,
        affected_party_name=(
            affected_party.name
            if affected_party is not None
            else "External Party not resolved"
        ),
        lineage=lineage,
        scope=scope,
        outcome=outcome,
        policy_run=policy_run,
        evidence=evidence,
        scope_dependencies=scope_dependencies,
        roster=roster,
        residue=residue,
        next_action_decision=next_action_decision,
        deferral_decision=deferral_decision,
    )


def assign_admitted_statement_owner(
    session: Session,
    project_id: int,
    candidate_id: int,
    roster_entry_id: int,
    *,
    principal: HumanPrincipal,
) -> WorkDecision:
    """Record the Internal Owner only when it is the current residual decision."""
    lock_project(session, project_id)
    coordination = read_admitted_statement_coordination(
        session, project_id, candidate_id
    )
    if coordination is None:
        raise StatementCoordinationRefusal(
            "no mechanically admitted statement exists in this project"
        )
    if coordination.residue.decision != RESIDUAL_OWNER:
        raise StatementCoordinationRefusal(
            "Internal Owner is no longer the next unresolved decision"
        )
    roster_entry = next(
        (entry for entry in coordination.roster if entry.id == roster_entry_id),
        None,
    )
    if roster_entry is None:
        raise StatementCoordinationRefusal(
            "Internal Owner must come from the active project roster"
        )
    decision = assign_internal_owner(
        session,
        CoordinationSubject.statement(coordination.lineage.id),
        roster_entry.display_name,
        principal=principal,
    )
    # This is the first Internal Owner for the admitted statement, so the
    # committed assignment registers one new-assignment notification (#351).
    notifications.register_new_assignment_notification(
        session,
        assignment_decision=decision,
        roster_entry=roster_entry,
        principal=principal,
    )
    return decision


def set_admitted_statement_next_action(
    session: Session,
    project_id: int,
    candidate_id: int,
    action: str,
    *,
    due_date: date | None,
    due_date_unknown_reason: str | None,
    principal: HumanPrincipal,
) -> WorkDecision:
    """Record the Next Action only when it is the current residual decision."""
    lock_project(session, project_id)
    coordination = read_admitted_statement_coordination(
        session, project_id, candidate_id
    )
    if coordination is None:
        raise StatementCoordinationRefusal(
            "no mechanically admitted statement exists in this project"
        )
    if coordination.residue.decision != RESIDUAL_NEXT_ACTION:
        raise StatementCoordinationRefusal(
            "Next Action is no longer the next unresolved decision"
        )
    normalized_action = str(action or "").strip()
    if (
        normalized_action not in STATEMENT_NEXT_ACTION_CHOICES
        and normalized_action not in _LEGACY_STATEMENT_NEXT_ACTION_CHOICES
    ):
        raise StatementCoordinationRefusal(
            "Next Action must be one structured project-language choice"
        )
    return set_next_action(
        session,
        CoordinationSubject.statement(coordination.lineage.id),
        normalized_action,
        due_date=due_date,
        due_date_unknown_reason=due_date_unknown_reason,
        principal=principal,
    )


def _read_admitted_with_live_action(
    session: Session, project_id: int, candidate_id: int
) -> AdmittedStatementCoordination:
    """Read the accepted commitment or refuse if it has no closable action."""
    lock_project(session, project_id)
    coordination = read_admitted_statement_coordination(
        session, project_id, candidate_id
    )
    if coordination is None:
        raise StatementCoordinationRefusal(
            "no mechanically admitted statement exists in this project"
        )
    if not coordination.can_close_next_action:
        raise StatementCoordinationRefusal(
            "this statement has no current Next Action to close or defer"
        )
    return coordination


def complete_admitted_statement_next_action(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    expected_next_action_decision_id: int,
    principal: HumanPrincipal,
    successor_action: str | None = None,
    successor_due_date: date | None = None,
    successor_due_date_unknown_reason: str | None = None,
    no_follow_up_reason: str | None = None,
    note: str | None = None,
) -> WorkDecision:
    """Complete an accepted commitment's Next Action without touching the fact.

    Completion leaves the External Party statement, Completion Reported, Applies
    To, and documentation judgments unchanged (ADR-0035, ADR-0038); it records
    only the project's internal response.
    """
    coordination = _read_admitted_with_live_action(session, project_id, candidate_id)
    try:
        return complete_next_action(
            session,
            CoordinationSubject.statement(coordination.lineage.id),
            principal=principal,
            successor_action=successor_action,
            successor_due_date=successor_due_date,
            successor_due_date_unknown_reason=successor_due_date_unknown_reason,
            no_follow_up_reason=no_follow_up_reason,
            note=note,
            expected_next_action_decision_id=expected_next_action_decision_id,
            permitted_successor_actions=STATEMENT_NEXT_ACTION_CHOICES,
        )
    except StaleNextAction as exc:
        raise StaleStatementCoordination(str(exc)) from exc
    except (CoordinationDecisionRefusal, ValueError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc


def cancel_admitted_statement_next_action(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    expected_next_action_decision_id: int,
    principal: HumanPrincipal,
    cancellation_reason: str | None = None,
    successor_action: str | None = None,
    successor_due_date: date | None = None,
    successor_due_date_unknown_reason: str | None = None,
    no_follow_up_reason: str | None = None,
    note: str | None = None,
) -> WorkDecision:
    """Cancel an accepted commitment's Next Action with a structured reason."""
    coordination = _read_admitted_with_live_action(session, project_id, candidate_id)
    try:
        return cancel_next_action(
            session,
            CoordinationSubject.statement(coordination.lineage.id),
            principal=principal,
            cancellation_reason=cancellation_reason,
            successor_action=successor_action,
            successor_due_date=successor_due_date,
            successor_due_date_unknown_reason=successor_due_date_unknown_reason,
            no_follow_up_reason=no_follow_up_reason,
            note=note,
            expected_next_action_decision_id=expected_next_action_decision_id,
            permitted_successor_actions=STATEMENT_NEXT_ACTION_CHOICES,
        )
    except StaleNextAction as exc:
        raise StaleStatementCoordination(str(exc)) from exc
    except (CoordinationDecisionRefusal, ValueError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc


def defer_admitted_statement(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    expected_next_action_decision_id: int,
    reason: str,
    return_date: date,
    principal: HumanPrincipal,
) -> WorkDecision:
    """Defer an accepted commitment's immediate work to a stated return date."""
    coordination = _read_admitted_with_live_action(session, project_id, candidate_id)
    try:
        return defer_work(
            session,
            CoordinationSubject.statement(coordination.lineage.id),
            reason=reason,
            return_date=return_date,
            principal=principal,
            expected_next_action_decision_id=expected_next_action_decision_id,
        )
    except StaleNextAction as exc:
        raise StaleStatementCoordination(str(exc)) from exc
    except (CoordinationDecisionRefusal, ValueError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc


@coordination_operation
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

            candidate_evidence = _candidate_evidence(session, candidate)
            all_evidence = _deduplicate_evidence((*candidate_evidence, *draft.evidence))
            _require_evidence_support(draft, all_evidence)
            _require_plan_shape(draft)
            party_resolution = _guided_party_resolution(
                session, candidate.project_id, draft, all_evidence, recorder
            )

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
                party_resolution=party_resolution,
            )
            scope_decision = _current_scope_decision(session, event.id)
            if scope_decision is None:
                raise RuntimeError(
                    "the accepted statement did not receive a scope decision"
                )
            # Dual-write the accepted statement onto the spine in the same act
            # (#451, ADR-0074): the legacy event stays the source of truth until
            # cutover, and the spine gains the attributable inclusion decision.
            record_cited_statement_on_spine(
                session,
                event=event,
                candidate_id=candidate.id,
                description=draft.description,
                new_timing=draft.new_timing,
                previous_timing=draft.previous_timing,
                recorder=recorder,
                command_type="coordinate_statement",
                evidence=all_evidence[0],
            )

            subject = CoordinationSubject.statement(event.commitment_lineage_id)
            internal_owner_decision = assign_internal_owner(
                session, subject, roster_entry.display_name, principal=recorder
            )
            # The committed assignment registers exactly one new-assignment
            # notification, atomically inside this accepted-statement Save (#351).
            notifications.register_new_assignment_notification(
                session,
                assignment_decision=internal_owner_decision,
                roster_entry=roster_entry,
                principal=recorder,
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
            disposition = CandidateDisposition(
                candidate_id=candidate.id,
                disposition="accepted",
                reason=None,
                recorded_by=recorder.subject,
            )
            session.add(disposition)
            session.flush([disposition])
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
                    **_party_resolution_audit(party_resolution),
                },
            )
            receipt = StatementCoordinationReceipt(
                candidate_id=candidate.id,
                candidate_disposition_id=disposition.id,
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
                    draft,
                    event,
                    scope_decision.id,
                    roster_entry.id,
                    all_evidence,
                    party_resolution,
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


def undo_statement_coordination(
    session: Session,
    receipt_id: int,
    *,
    principal: HumanPrincipal,
) -> StatementCoordinationReversal:
    """Append the exact compensation for one immediately undoable guided Save."""
    recorder = require_human_principal(principal)
    receipt = session.get(StatementCoordinationReceipt, receipt_id)
    if receipt is None:
        raise StatementCoordinationUndoRefusal(
            "the guided Save receipt no longer exists"
        )
    candidate = session.get(Candidate, receipt.candidate_id)
    if candidate is None:
        raise StatementCoordinationUndoRefusal(
            "the guided Save Candidate no longer exists"
        )

    try:
        with session.begin_nested():
            lock_project(session, candidate.project_id)
            _require_undoable_save(session, receipt, candidate)
            disposition = session.get(
                CandidateDisposition, receipt.candidate_disposition_id
            )
            if disposition is None:
                raise StatementCoordinationUndoRefusal(
                    "this historical guided Save has no reversible Candidate disposition"
                )

            audit_entry = audit.record(
                session,
                principal=recorder,
                action=audit.UNDO_COORDINATED_STATEMENT,
                entity_type=audit.CANDIDATE,
                entity_id=candidate.id,
                before={"receipt_id": receipt.id, "candidate_state": candidate.state},
                after={
                    "receipt_id": receipt.id,
                    "candidate_state": "pending",
                    "statement_event_id": receipt.dependency_event_id,
                },
            )
            reversal = StatementCoordinationReversal(
                receipt_id=receipt.id,
                candidate_id=candidate.id,
                audit_log_id=audit_entry.id,
                recorded_by=recorder.subject,
            )
            session.add(reversal)
            session.flush([reversal])
            _record_undo_effects(session, reversal, receipt)
            sync_coordination_reversals(session, candidate.project_id)

            candidate.state = "pending"
            candidate.adjudicated_at = None
            lineage = session.get(CommitmentLineage, receipt.commitment_lineage_id)
            if lineage is None:
                raise StatementCoordinationUndoRefusal(
                    "the grouped Commitment Lineage no longer exists"
                )
            lineage.internal_owner = None
            lineage.next_action = None
            lineage.action_due_date = None
            lineage.action_due_date_reason = None
            lineage.milestone_impact = None
            lineage.milestone_ids = []
            lineage.plan_needs_review = False

            from corridor.dependency_events import project_committed_dates

            dependency_ids = tuple(
                session.scalars(
                    select(CommitmentScopeMembership.dependency_id).where(
                        CommitmentScopeMembership.scope_decision_id
                        == receipt.scope_decision_id
                    )
                ).all()
            )
            project_committed_dates(session, dependency_ids)
            session.flush()
    except StatementCoordinationUndoRefusal:
        raise
    except (ValueError, IntegrityError) as exc:
        raise StatementCoordinationUndoRefusal(str(exc)) from exc
    return reversal


def _require_undoable_save(
    session: Session,
    receipt: StatementCoordinationReceipt,
    candidate: Candidate,
) -> None:
    if (
        session.scalar(
            select(StatementCoordinationReversal.id).where(
                StatementCoordinationReversal.receipt_id == receipt.id
            )
        )
        is not None
    ):
        raise StatementCoordinationUndoRefusal("this guided Save was already undone")
    disposition = current_candidate_disposition(session, candidate.id)
    if (
        candidate.state != "accepted"
        or disposition is None
        or disposition.id != receipt.candidate_disposition_id
    ):
        raise StatementCoordinationUndoRefusal(
            "the Candidate changed after this Save; use Correct instead"
        )
    observation = observe_current_statement(session, receipt.commitment_lineage_id)
    if observation is None:
        raise StatementCoordinationUndoRefusal(
            "the statement changed after this Save; use Correct instead"
        )
    if observation.statement_event_id != receipt.dependency_event_id:
        raise StatementCoordinationUndoRefusal(
            "the statement changed after this Save; use Correct instead"
        )
    if observation.scope_decision_id != receipt.scope_decision_id:
        raise StatementCoordinationUndoRefusal(
            "the statement scope changed after this Save; use Correct instead"
        )
    subject = CoordinationSubject.statement(receipt.commitment_lineage_id)
    current_decisions = (
        _decision_id(current_internal_owner_decision(session, subject)),
        _decision_id(current_next_action_decision(session, subject)),
        observation.milestone_impact_decision_id,
    )
    receipt_decisions = (
        receipt.internal_owner_decision_id,
        receipt.next_action_decision_id,
        receipt.milestone_impact_decision_id,
    )
    if current_decisions != receipt_decisions:
        raise StatementCoordinationUndoRefusal(
            "later Coordination Plan work depends on this Save; use Correct instead"
        )
    _require_no_later_downstream_act(session, receipt)


def _require_no_later_downstream_act(
    session: Session, receipt: StatementCoordinationReceipt
) -> None:
    """Refuse Undo once a later closure, publication, or lineage act used it."""
    lineage = session.get(CommitmentLineage, receipt.commitment_lineage_id)
    if lineage is None:
        raise StatementCoordinationUndoRefusal(
            "the grouped Commitment Lineage no longer exists"
        )
    dependency_ids = tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id).where(
                CommitmentScopeMembership.scope_decision_id == receipt.scope_decision_id
            )
        ).all()
    )
    later_closure = (
        session.scalar(
            select(ExternalPartyStatement.id)
            .join(
                CommitmentScopeMembership,
                CommitmentScopeMembership.event_id == ExternalPartyStatement.id,
            )
            .where(
                ExternalPartyStatement.project_id == lineage.project_id,
                ExternalPartyStatement.event_type == "closure",
                CommitmentScopeMembership.dependency_id.in_(dependency_ids),
                ExternalPartyStatement.id > receipt.dependency_event_id,
            )
            .limit(1)
        )
        if dependency_ids
        else None
    )
    if later_closure is not None:
        raise StatementCoordinationUndoRefusal(
            "a later closure depends on this Save; use Correct instead"
        )
    if _report_published_scoped_dependency(
        session, receipt, lineage.project_id, dependency_ids
    ):
        raise StatementCoordinationUndoRefusal(
            "a later Report publication depends on this Save; use Correct instead"
        )
    _require_no_later_audited_save_reference(session, receipt)


def _require_no_later_audited_save_reference(
    session: Session, receipt: StatementCoordinationReceipt
) -> None:
    """Reject Undo only when a later audit names an exact result of this Save."""
    referenced_ids = {
        "receipt_id": {receipt.id},
        "statement_event_id": {receipt.dependency_event_id},
        "scope_decision_id": {receipt.scope_decision_id},
        "candidate_disposition_id": {receipt.candidate_disposition_id},
        "work_decision_id": {
            receipt.internal_owner_decision_id,
            receipt.next_action_decision_id,
            receipt.milestone_impact_decision_id,
        }
        - {None},
        "milestone_link_id": set(
            session.scalars(
                select(WorkDecisionMilestoneImpact.id).where(
                    WorkDecisionMilestoneImpact.work_decision_id
                    == receipt.milestone_impact_decision_id
                )
            ).all()
        ),
    }
    later_audits = session.scalars(
        select(AuditLog).where(
            AuditLog.id > receipt.audit_log_id,
            AuditLog.action != audit.PRODUCT_PROVING_FRONTEND_REQUEST,
        )
    )
    if any(
        _audit_references_save(entry.after_json, referenced_ids)
        or _audit_references_save(entry.before_json, referenced_ids)
        for entry in later_audits
    ):
        raise StatementCoordinationUndoRefusal(
            "a later recorded act depends on this Save; use Correct instead"
        )


def _audit_references_save(value: object, referenced_ids: dict[str, set[int]]) -> bool:
    """Find a typed exact Save reference in append-only audit detail."""
    return audit.references_typed_ids(value, referenced_ids)


def _report_published_scoped_dependency(
    session: Session,
    receipt: StatementCoordinationReceipt,
    project_id: int,
    dependency_ids: tuple[int, ...],
) -> bool:
    """Whether a later immutable Report snapshot names this Save's scope."""
    if not dependency_ids:
        return False
    reports = session.scalars(
        select(ReportRun).where(
            ReportRun.project_id == project_id,
            ReportRun.ts >= receipt.created_at,
        )
    ).all()
    for report in reports:
        published_ids = {
            value.get("id")
            for value in (
                (report.snapshot_json or {}).get("dependencies") or {}
            ).values()
            if isinstance(value, dict)
        }
        if published_ids.intersection(dependency_ids):
            return True
    return False


def _record_undo_effects(
    session: Session,
    reversal: StatementCoordinationReversal,
    receipt: StatementCoordinationReceipt,
) -> None:
    effects = [
        ("statement", receipt.dependency_event_id),
        ("scope_decision", receipt.scope_decision_id),
        ("work_decision", receipt.internal_owner_decision_id),
        ("work_decision", receipt.next_action_decision_id),
        ("candidate_disposition", receipt.candidate_disposition_id),
        ("candidate_projection", receipt.candidate_id),
        ("lineage_projection", receipt.commitment_lineage_id),
        ("audit_pointer", receipt.audit_log_id),
        ("grouping_receipt", receipt.id),
    ]
    if receipt.milestone_impact_decision_id is not None:
        effects.append(("work_decision", receipt.milestone_impact_decision_id))
        effects.extend(
            ("milestone_link", milestone_link_id)
            for milestone_link_id in session.scalars(
                select(WorkDecisionMilestoneImpact.id).where(
                    WorkDecisionMilestoneImpact.work_decision_id
                    == receipt.milestone_impact_decision_id
                )
            )
        )
    session.add_all(
        StatementCoordinationReversalEffect(
            reversal_id=reversal.id,
            effect_kind=effect_kind,
            target_id=target_id,
        )
        for effect_kind, target_id in effects
    )


def correct_statement_scope(
    session: Session,
    correction: StatementScopeCorrection,
    *,
    principal: HumanPrincipal,
) -> CommitmentScopeDecision:
    """Append one scope correction without changing the External Party fact."""
    recorder = require_human_principal(principal)
    event = session.get(ExternalPartyStatement, correction.event_id)
    if event is None:
        raise StatementCoordinationRefusal("the statement to correct no longer exists")
    try:
        with session.begin_nested():
            lock_project(session, event.project_id)
            _require_candidate_owns_lineage(session, correction.candidate_id, event)
            current = current_lineage_statement(session, event.commitment_lineage_id)
            if current is None or current.id != event.id:
                raise StaleStatementCoordination(
                    "the statement changed; reload the newer state"
                )
            predecessor = _current_scope_decision(session, event.id)
            if (
                predecessor is None
                or predecessor.id != correction.expected_scope_decision_id
            ):
                raise StaleStatementCoordination(
                    "the statement scope changed; reload the newer state"
                )
            if _scope_correction_is_noop(session, event, predecessor, correction.scope):
                raise StatementCoordinationRefusal(
                    "Commitment Scope already has this exact value; "
                    "no correction was recorded"
                )
            decision = record_statement_scope_decision(
                session,
                event_id=event.id,
                scope=correction.scope,
                actor=recorder,
            )
            audit.record(
                session,
                principal=recorder,
                action=audit.CORRECT_STATEMENT_SCOPE,
                entity_type=audit.COMMITMENT_LINEAGE,
                entity_id=event.commitment_lineage_id,
                before={"scope_decision_id": predecessor.id},
                after={
                    "statement_event_id": event.id,
                    "scope_decision_id": decision.id,
                },
            )
            correct_statement_scope_on_spine(
                session,
                event=event,
                scope_decision_id=decision.id,
                recorder=recorder,
            )
            candidate = session.get(Candidate, correction.candidate_id)
            assert candidate is not None  # required by _require_candidate_owns_lineage
            record_statement_scope_correction_case(
                session,
                candidate,
                event,
                decision,
                recorded_by=recorder.subject,
            )
    except StaleStatementCoordination:
        raise
    except (StatementRefusal, ValueError, IntegrityError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc
    return decision


def _scope_correction_is_noop(
    session: Session,
    event: ExternalPartyStatement,
    predecessor: CommitmentScopeDecision,
    requested: StatementScope,
) -> bool:
    """Refuse an attributable scope receipt that changes no scope state."""
    if predecessor.scope_mode != requested.mode:
        return False
    current_ids = tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id)
            .where(CommitmentScopeMembership.scope_decision_id == predecessor.id)
            .order_by(CommitmentScopeMembership.dependency_id)
        ).all()
    )
    if requested.mode == "unknown":
        requested_ids: tuple[int, ...] = ()
    elif requested.mode == "selected":
        requested_ids = tuple(sorted(requested.dependency_ids))
    elif requested.mode == "all_active":
        requested_ids = tuple(
            session.scalars(
                select(Dependency.id)
                .where(
                    Dependency.project_id == event.project_id,
                    Dependency.external_org_id == event.affected_external_org_id,
                    Dependency.dismissed_at.is_(None),
                )
                .order_by(Dependency.id)
            ).all()
        )
    else:
        return False
    return current_ids == requested_ids


def correct_statement_facts(
    session: Session,
    draft: StatementFactCorrectionDraft,
    *,
    principal: HumanPrincipal,
) -> ExternalPartyStatement:
    """Append a supported statement successor while retaining the plan lineage."""
    recorder = require_human_principal(principal)
    predecessor = session.get(ExternalPartyStatement, draft.expected_statement_event_id)
    if predecessor is None:
        raise StatementCoordinationRefusal("the statement to correct no longer exists")
    try:
        with session.begin_nested():
            lock_project(session, predecessor.project_id)
            _require_candidate_owns_lineage(session, draft.candidate_id, predecessor)
            current = current_lineage_statement(
                session, predecessor.commitment_lineage_id
            )
            if current is None or current.id != predecessor.id:
                raise StaleStatementCoordination(
                    "the statement changed; reload the newer state"
                )
            scope_decision = _current_scope_decision(session, predecessor.id)
            if scope_decision is None:
                raise StatementCoordinationRefusal(
                    "the statement has no current Commitment Scope"
                )
            scope_ids = tuple(
                session.scalars(
                    select(CommitmentScopeMembership.dependency_id)
                    .where(
                        CommitmentScopeMembership.scope_decision_id == scope_decision.id
                    )
                    .order_by(CommitmentScopeMembership.dependency_id)
                ).all()
            )
            scope = StatementScope(
                "unknown"
                if scope_decision.scope_mode == "unknown"
                else "carried_forward",
                scope_ids,
            )
            evidence = _deduplicate_evidence(draft.evidence)
            _require_fact_evidence_support(draft, evidence)
            party_resolution = _guided_party_resolution(
                session, predecessor.project_id, draft, evidence, recorder
            )
            successor = record_external_party_statement(
                session,
                project_id=predecessor.project_id,
                affected_external_org_id=draft.affected_external_org_id,
                stated_party=draft.stated_party,
                stated_external_org_id=draft.stated_external_org_id,
                source_kind="cited",
                event_date=draft.event_date,
                description=draft.description,
                new_timing=draft.new_timing,
                previous_timing=draft.previous_timing,
                scope=scope,
                created_by=recorder.subject,
                evidence=evidence[0],
                supporting_evidence=evidence[1:],
                commitment_lineage_id=predecessor.commitment_lineage_id,
                allow_party_correction=True,
                party_resolution=party_resolution,
                _scope_snapshot_dependency_ids=scope_ids,
            )
            audit.record(
                session,
                principal=recorder,
                action=audit.CORRECT_STATEMENT_FACTS,
                entity_type=audit.COMMITMENT_LINEAGE,
                entity_id=predecessor.commitment_lineage_id,
                before={"statement_event_id": predecessor.id},
                after={
                    "statement_event_id": successor.id,
                    **_party_resolution_audit(party_resolution),
                },
            )
            record_cited_statement_on_spine(
                session,
                event=successor,
                candidate_id=draft.candidate_id,
                description=draft.description,
                new_timing=draft.new_timing,
                previous_timing=draft.previous_timing,
                recorder=recorder,
                command_type="correct_statement_facts",
                evidence=evidence[0],
            )
            candidate = session.get(Candidate, draft.candidate_id)
            assert candidate is not None  # required by _require_candidate_owns_lineage
            record_statement_fact_correction_case(
                session,
                candidate,
                successor,
                evidence=evidence,
                recorded_by=recorder.subject,
            )
    except StaleStatementCoordination:
        raise
    except (StatementRefusal, ValueError, IntegrityError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc
    return successor




def _closure_gap(code: str, **facts) -> CandidateAuthorityGap:
    """One closure-family gap: its code and its own words, never separately."""
    words = presentation.authority_gap_words(code)
    return CandidateAuthorityGap(
        code=code, title=words.title, detail=words.detail, **facts
    )


def _open_commitment_lineage_ids(
    session: Session,
    project_id: int,
    affected_external_org_id: int,
) -> tuple[int, ...]:
    """Return current open Commitments for one Evidence-supported party."""
    closed = frozenset(
        lineage_id
        for lineage_id in session.scalars(
            select(ExternalPartyStatement.closes_commitment_lineage_id).where(
                ExternalPartyStatement.project_id == project_id,
                ExternalPartyStatement.event_type == "closure",
                ExternalPartyStatement.closes_commitment_lineage_id.is_not(None),
                current_statement_event_filter(ExternalPartyStatement.id),
            )
        ).all()
        if lineage_id is not None
    )
    return tuple(
        lineage_id
        for lineage_id in dict.fromkeys(
            session.scalars(
                select(ExternalPartyStatement.commitment_lineage_id)
                .where(
                    ExternalPartyStatement.project_id == project_id,
                    ExternalPartyStatement.event_type.in_(
                        ("commitment", "committed_date_change")
                    ),
                    ExternalPartyStatement.affected_external_org_id
                    == affected_external_org_id,
                    ExternalPartyStatement.commitment_lineage_id.is_not(None),
                    current_statement_event_filter(ExternalPartyStatement.id),
                )
                .order_by(ExternalPartyStatement.commitment_lineage_id)
            ).all()
        )
        if lineage_id is not None and lineage_id not in closed
    )


def pending_candidate_authority_gap(
    session: Session,
    project_id: int,
    candidate_id: int,
) -> CandidateAuthorityGap | None:
    """Recompute one current structured gap from Evidence or policy receipts."""
    candidate = session.get(Candidate, candidate_id, populate_existing=True)
    if (
        candidate is None
        or candidate.project_id != project_id
        or candidate.state != "pending"
    ):
        return None
    actionable = session.scalar(
        actionable_candidate_query(project_id)
        .where(Candidate.id == candidate.id)
        .limit(1)
    )
    if actionable is None:
        return None

    if candidate.kind == "event":
        facts = prepare_candidate_statement_facts(session, candidate)
        if (
            facts.fields.get("event_type") != "closure"
            or not facts.evidence_is_complete
            or not facts.description_is_supported
        ):
            return None
        affected_external_org_id = facts.affected_party.external_org_id
        if affected_external_org_id is None:
            return _closure_gap(
                CLOSURE_PARTY_GAP,
                source_family="external-party-statement",
            )
        matching_lineage_ids = _open_commitment_lineage_ids(
            session,
            project_id,
            affected_external_org_id,
        )
        if len(matching_lineage_ids) == 1:
            return _closure_gap(
                CLOSURE_TARGET_RELATIONSHIP_GAP,
                source_family="external-party-statement",
                affected_external_org_id=affected_external_org_id,
                matching_commitment_lineage_ids=matching_lineage_ids,
            )
        if len(matching_lineage_ids) > 1:
            return _closure_gap(
                CLOSURE_TARGET_AMBIGUOUS_GAP,
                source_family="external-party-statement",
                affected_external_org_id=affected_external_org_id,
                matching_commitment_lineage_ids=matching_lineage_ids,
            )
        return _closure_gap(
            CLOSURE_TARGET_GAP,
            source_family="external-party-statement",
            affected_external_org_id=affected_external_org_id,
        )

    if candidate.kind != "dependency" or candidate.id not in ordinary_candidate_ids(
        session, project_id
    ):
        return None
    row = session.execute(
        select(DependencyAdmissionOutcome, PolicyRun)
        .join(PolicyRun, PolicyRun.id == DependencyAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project_id,
            PolicyRun.family == "dependency-admission",
            DependencyAdmissionOutcome.candidate_id == candidate.id,
            DependencyAdmissionOutcome.outcome == "abstained",
        )
        .order_by(DependencyAdmissionOutcome.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    outcome, policy_run = row
    words = presentation.dependency_admission_gap_words(outcome.reason or "")
    if words is None:
        # Not an allowed unresolved gap: this abstention reason has no words
        # because it is not work a coordinator may keep.
        return None
    return CandidateAuthorityGap(
        code=f"dependency_admission_{outcome.reason}",
        title=words.title,
        detail=words.detail,
        source_family="dependency-admission",
        abstention_reason=outcome.reason,
        outcome_id=outcome.id,
        policy_run_id=policy_run.id,
    )


def pending_statement_authority_gap(
    session: Session,
    project_id: int,
    candidate_id: int,
) -> PendingStatementAuthorityGap | None:
    """Compatibility read seam for the closure-specific screen."""
    gap = pending_candidate_authority_gap(session, project_id, candidate_id)
    candidate = session.get(Candidate, candidate_id)
    return gap if candidate is not None and candidate.kind == "event" else None


def keep_candidate_unresolved(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    principal: HumanPrincipal,
) -> AuditLog:
    """Acknowledge a recomputed structured gap without disposing of the work."""
    recorder = require_human_principal(principal)
    try:
        with session.begin_nested():
            lock_project(session, project_id)
            gap = pending_candidate_authority_gap(session, project_id, candidate_id)
            if gap is None:
                raise StaleStatementCoordination(
                    "this Candidate no longer has an allowed unresolved authority gap"
                )
            after = {
                "candidate_state": "pending",
                "authority_gap": gap.code,
            }
            if gap.source_family == "dependency-admission":
                after.update(
                    {
                        "source_family": gap.source_family,
                        "abstention_reason": gap.abstention_reason,
                        "dependency_admission_outcome_id": gap.outcome_id,
                        "policy_run_id": gap.policy_run_id,
                    }
                )
            elif gap.source_family == "external-party-statement":
                after.update(
                    {
                        "affected_external_org_id": gap.affected_external_org_id,
                        "matching_open_commitment_lineage_ids": list(
                            gap.matching_commitment_lineage_ids
                        ),
                    }
                )
            latest = session.scalar(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == audit.CANDIDATE,
                    AuditLog.entity_id == candidate_id,
                    AuditLog.action == audit.KEEP_CANDIDATE_UNRESOLVED,
                )
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
            if (
                latest is not None
                and latest.actor == recorder.subject
                and latest.human_principal == recorder.subject
                and latest.before_json == {"candidate_state": "pending"}
                and latest.after_json == after
            ):
                return latest
            return audit.record(
                session,
                principal=recorder,
                action=audit.KEEP_CANDIDATE_UNRESOLVED,
                entity_type=audit.CANDIDATE,
                entity_id=candidate_id,
                before={"candidate_state": "pending"},
                after=after,
            )
    except StaleStatementCoordination:
        raise
    except (ValueError, IntegrityError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc


def keep_statement_unresolved(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    principal: HumanPrincipal,
) -> AuditLog:
    """Compatibility command for the closure-specific route."""
    candidate = session.get(Candidate, candidate_id)
    if candidate is None or candidate.kind != "event":
        raise StatementCoordinationRefusal(
            "only a pending closure can use the statement unresolved action"
        )
    return keep_candidate_unresolved(
        session,
        project_id,
        candidate_id,
        principal=principal,
    )


def mark_statement_not_relevant(
    session: Session,
    candidate_id: int,
    *,
    reason: str,
    confirmed: bool,
    principal: HumanPrincipal,
) -> CandidateDisposition:
    """Record a reversible, reasoned disposition without creating a statement."""
    recorder = require_human_principal(principal)
    if reason not in NOT_RELEVANT_REASONS:
        raise StatementCoordinationRefusal("Not Relevant needs a structured reason")
    if confirmed is not True:
        raise StatementCoordinationRefusal("Not Relevant needs explicit confirmation")
    candidate = session.get(Candidate, candidate_id)
    if candidate is None or candidate.kind != "event":
        raise StatementCoordinationRefusal(
            "only an Unplaced Statement can be marked Not Relevant"
        )
    try:
        with session.begin_nested():
            lock_project(session, candidate.project_id)
            session.refresh(candidate)
            if candidate.state != "pending" or current_candidate_disposition(
                session, candidate.id
            ):
                raise StaleStatementCoordination(
                    "the statement Candidate changed; reload the newer state"
                )
            actionable = session.scalar(
                actionable_candidate_query(candidate.project_id)
                .where(Candidate.id == candidate.id, Candidate.kind == "event")
                .limit(1)
            )
            if actionable is None:
                raise StaleStatementCoordination(
                    "the statement Candidate is no longer in the current actionable scope"
                )
            disposition = CandidateDisposition(
                candidate_id=candidate.id,
                disposition="not_relevant",
                reason=reason,
                recorded_by=recorder.subject,
            )
            session.add(disposition)
            session.flush([disposition])
            candidate.state = "rejected"
            candidate.adjudicated_at = datetime.now(timezone.utc)
            # The disposition row carries the reason, and Not Relevant is
            # only recordable from `pending` with explicit confirmation, so
            # the transition and the confirmation are properties of the act
            # rather than facts to copy. The entry names the disposition and
            # a reader derives the same before/after from it (#604).
            audit.record(
                session,
                principal=recorder,
                action=audit.MARK_STATEMENT_NOT_RELEVANT,
                entity_type=audit.CANDIDATE,
                entity_id=candidate.id,
                decided_by=audit.DecisionIdentity(
                    kind=audit.CANDIDATE_DISPOSITION, identity=disposition.id
                ),
            )
            record_do_not_add_case(
                session,
                candidate,
                reason=reason,
                ruling_type="candidate_disposition",
                ruling_id=disposition.id,
                recorded_by=recorder.subject,
            )
            session.flush()
            mark_statement_do_not_add_on_spine(
                session,
                candidate=candidate,
                recorder=recorder,
                disposition_id=disposition.id,
            )
    except StaleStatementCoordination:
        raise
    except (ValueError, IntegrityError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc
    return disposition


def restore_statement_not_relevant(
    session: Session,
    disposition_id: int,
    *,
    principal: HumanPrincipal,
) -> StatementCoordinationReversal:
    """Append the restoration of one Not Relevant disposition."""
    recorder = require_human_principal(principal)
    disposition = session.get(CandidateDisposition, disposition_id)
    if disposition is None or disposition.disposition != "not_relevant":
        raise StatementCoordinationRefusal(
            "the Not Relevant disposition no longer exists"
        )
    candidate = session.get(Candidate, disposition.candidate_id)
    if candidate is None:
        raise StatementCoordinationRefusal(
            "the Not Relevant Candidate no longer exists"
        )
    try:
        with session.begin_nested():
            lock_project(session, candidate.project_id)
            current = current_candidate_disposition(session, candidate.id)
            if (
                candidate.state != "rejected"
                or current is None
                or current.id != disposition.id
            ):
                raise StaleStatementCoordination(
                    "the Candidate changed after Not Relevant; reload the newer state"
                )
            audit_entry = audit.record(
                session,
                principal=recorder,
                action=audit.RESTORE_STATEMENT_NOT_RELEVANT,
                entity_type=audit.CANDIDATE,
                entity_id=candidate.id,
                before={
                    "candidate_state": "rejected",
                    "candidate_disposition_id": disposition.id,
                },
                after={"candidate_state": "pending"},
            )
            reversal = StatementCoordinationReversal(
                candidate_disposition_id=disposition.id,
                candidate_id=candidate.id,
                audit_log_id=audit_entry.id,
                recorded_by=recorder.subject,
            )
            session.add(reversal)
            session.flush([reversal])
            record_do_not_add_reversal_case(
                session,
                candidate,
                reversal,
                original_ruling_type="candidate_disposition",
                original_ruling_id=disposition.id,
                reason=disposition.reason,
            )
            session.add_all(
                (
                    StatementCoordinationReversalEffect(
                        reversal_id=reversal.id,
                        effect_kind="candidate_disposition",
                        target_id=disposition.id,
                    ),
                    StatementCoordinationReversalEffect(
                        reversal_id=reversal.id,
                        effect_kind="candidate_projection",
                        target_id=candidate.id,
                    ),
                    StatementCoordinationReversalEffect(
                        reversal_id=reversal.id,
                        effect_kind="audit_pointer",
                        target_id=audit_entry.id,
                    ),
                )
            )
            candidate.state = "pending"
            candidate.adjudicated_at = None
            session.flush()
            restore_statement_do_not_add_on_spine(
                session,
                candidate=candidate,
                recorder=recorder,
                reversal_id=reversal.id,
            )
    except StaleStatementCoordination:
        raise
    except (ValueError, IntegrityError) as exc:
        raise StatementCoordinationRefusal(str(exc)) from exc
    return reversal


def _require_current_candidate(
    session: Session,
    candidate: Candidate,
    expected: StatementCoordinationPredecessors,
) -> None:
    if candidate.kind != "event":
        raise StatementCoordinationRefusal(
            "only an Unplaced Statement can use guided coordination"
        )
    if candidate.state != expected.candidate_state:
        raise StaleStatementCoordination(
            f"the statement Candidate is already {candidate.state}; reload its newer state"
        )
    if expected.candidate_state != "pending":
        raise StatementCoordinationRefusal(
            "a guided Save must begin from a pending Candidate"
        )
    if not candidate.citations_verified:
        raise StatementCoordinationRefusal(
            "the statement Candidate has unverified Evidence"
        )
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
        raise StaleStatementCoordination(
            "the expected Commitment Lineage is no longer current"
        )
    observation = observe_current_statement(session, lineage.id)
    if (
        observation is None
        or observation.statement_event_id != expected.statement_event_id
    ):
        raise StaleStatementCoordination(
            "the statement changed; reload the newer state"
        )
    if observation.scope_decision_id != expected.scope_decision_id:
        raise StaleStatementCoordination(
            "the statement scope changed; reload the newer state"
        )
    subject = CoordinationSubject.statement(lineage.id)
    actual = (
        _decision_id(current_internal_owner_decision(session, subject)),
        _decision_id(current_next_action_decision(session, subject)),
        observation.milestone_impact_decision_id,
    )
    expected_ids = (
        expected.internal_owner_decision_id,
        expected.next_action_decision_id,
        expected.milestone_impact_decision_id,
    )
    if actual != expected_ids:
        raise StaleStatementCoordination(
            "the Coordination Plan changed; reload the newer state"
        )


def _require_candidate_owns_lineage(
    session: Session, candidate_id: int, event: ExternalPartyStatement
) -> None:
    """Bind Correct to the Candidate whose accepted receipt owns the lineage."""
    candidate = session.get(Candidate, candidate_id)
    if (
        candidate is None
        or candidate.kind != "event"
        or candidate.state != "accepted"
        or candidate.project_id != event.project_id
    ):
        raise StaleStatementCoordination(
            "the statement Candidate does not own this correction target"
        )
    receipt = session.scalar(
        select(StatementCoordinationReceipt)
        .outerjoin(
            StatementCoordinationReversal,
            StatementCoordinationReversal.receipt_id == StatementCoordinationReceipt.id,
        )
        .where(
            StatementCoordinationReceipt.candidate_id == candidate.id,
            StatementCoordinationReceipt.commitment_lineage_id
            == event.commitment_lineage_id,
            StatementCoordinationReversal.id.is_(None),
        )
        .limit(1)
    )
    admission = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == candidate.id,
            EventAdmissionOutcome.commitment_lineage_id == event.commitment_lineage_id,
            EventAdmissionOutcome.dependency_event_id == event.id,
            EventAdmissionOutcome.outcome == "admitted",
        )
    )
    if receipt is None and admission is None:
        raise StaleStatementCoordination(
            "the statement Candidate does not own this correction target"
        )


def _current_scope_decision(
    session: Session, event_id: int
) -> CommitmentScopeDecision | None:
    successor = CommitmentScopeDecision.__table__.alias("successor")
    return session.scalar(
        select(CommitmentScopeDecision)
        .where(
            CommitmentScopeDecision.event_id == event_id,
            current_scope_decision_filter(CommitmentScopeDecision.id),
            ~select(successor.c.id)
            .where(
                successor.c.supersedes_scope_decision_id == CommitmentScopeDecision.id
            )
            .exists(),
        )
        .order_by(CommitmentScopeDecision.id)
    )


def _candidate_evidence(
    session: Session, candidate: Candidate
) -> tuple[CitedStatementEvidence, ...]:
    facts = prepare_candidate_statement_facts(session, candidate)
    if not facts.evidence_is_complete:
        raise StatementCoordinationRefusal("the Candidate has no verified Evidence")
    return facts.cited_evidence


def _deduplicate_evidence(
    evidence: tuple[CitedStatementEvidence, ...],
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
    _require_fact_evidence_support(draft, evidence)


def _require_fact_evidence_support(
    draft: StatementCoordinationDraft | StatementFactCorrectionDraft,
    evidence: tuple[CitedStatementEvidence, ...],
) -> None:
    """Make each corrected attribution and timing fact point back to a quote."""
    raw_quotes = tuple(item.quote for item in evidence)
    party = draft.stated_party.strip()
    quotes = tuple(normalize(quote) for quote in raw_quotes)
    if not party or not any(party in quote for quote in raw_quotes):
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
    event: ExternalPartyStatement,
    scope_decision_id: int,
    roster_entry_id: int,
    evidence: tuple[CitedStatementEvidence, ...],
    party_resolution: EvidenceBoundPartyResolution | None,
) -> dict:
    facts = {
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
    if party_resolution is not None:
        facts["party_resolution"] = party_resolution.as_json()
    return facts


def _guided_party_resolution(
    session: Session,
    project_id: int,
    draft: StatementCoordinationDraft | StatementFactCorrectionDraft,
    evidence: tuple[CitedStatementEvidence, ...],
    principal: HumanPrincipal,
) -> EvidenceBoundPartyResolution | None:
    selected_party = session.get(ExternalParty, draft.stated_external_org_id)
    if selected_party is None:
        raise StatementCoordinationRefusal(
            "the selected External Party no longer exists"
        )
    stated_party = draft.stated_party.strip()
    registered_spellings = {
        normalize(value)
        for value in (selected_party.name, *(selected_party.aliases or []))
        if value
    }
    if normalize(stated_party) in registered_spellings:
        return None

    candidate = session.get(Candidate, draft.candidate_id)
    prepared = (
        prepare_candidate_statement_facts(session, candidate)
        if candidate is not None and candidate.project_id == project_id
        else None
    )
    source_party = (
        prepared.source_stated_party_wording if prepared is not None else None
    )
    if not isinstance(source_party, str) or source_party.strip() != stated_party:
        raise StatementCoordinationRefusal(
            "guided resolution requires the Candidate's exact source party wording"
        )
    return EvidenceBoundPartyResolution(
        mode="guided_evidence_bound",
        project_id=project_id,
        candidate_id=draft.candidate_id,
        stated_party=stated_party,
        stated_external_org_id=draft.stated_external_org_id,
        principal=principal.subject,
        evidence=evidence,
    )


def _party_resolution_audit(
    party_resolution: EvidenceBoundPartyResolution | None,
) -> dict[str, dict]:
    if party_resolution is None:
        return {}
    return {"party_resolution": party_resolution.as_json()}


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
        candidate.payload_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _decision_id(decision: WorkDecision | None) -> int | None:
    return decision.id if decision is not None else None
