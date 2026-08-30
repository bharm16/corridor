"""Replay-gated exact Record Inclusion for statement identifying language.

This is deliberately a small extension in front of the existing unknown-scope
policy.  It writes only an exact one-row scope after replaying the project's
recorded human scope choices; every other Candidate remains for the older
policy/residual workflow.  A new project has zero answer-key cases and is
therefore inactive (ADR-0050), not optimistically enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit, policy
from corridor.candidate_statement_facts import prepare_candidate_statement_facts
from corridor.external_statements import StatementScope, record_external_party_statement
from corridor.models import (
    Candidate,
    CandidateDisposition,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    EventAdmissionOutcome,
    EvidenceLink,
    PolicyRun,
    Project,
)
from corridor.statement_lifecycle import current_statement_event_filter
from corridor.statement_matcher import MATCHER_VERSION, match_statement_scope, matcher_fingerprint


POLICY_VERSION = "event-admission-v4-identifying-language"
MACHINE_ACTOR = "corridor:statement-scope-matcher"
REASON_VERSION = "event-admission-identifying-language-abstentions-v1"


@dataclass(frozen=True)
class StatementScopeReplay:
    case_count: int
    contradictions: tuple[int, ...]

    @property
    def passed(self) -> bool:
        return self.case_count > 0 and not self.contradictions


@dataclass(frozen=True)
class StatementScopeRun:
    admitted_count: int
    run_id: int | None
    replay: StatementScopeReplay


def canonical_policy() -> dict:
    return {
        "policy_version": POLICY_VERSION,
        "matcher_version": MATCHER_VERSION,
        "matcher_fingerprint": matcher_fingerprint(),
        "activation": "adr-0050-recorded-human-scope-replay-v1",
        "exact_result": "one-survivor-of-full-stack",
    }


def replay_matches_human_scope_decisions(
    session: Session, project_id: int
) -> StatementScopeReplay:
    """Compare the new exact rule to actual human selected-scope history."""
    rows = session.execute(
        select(DependencyEventScopeDecision, DependencyEvent, EvidenceLink)
        .join(DependencyEvent, DependencyEvent.id == DependencyEventScopeDecision.event_id)
        .join(DependencyEventEvidence, DependencyEventEvidence.event_id == DependencyEvent.id)
        .join(EvidenceLink, EvidenceLink.id == DependencyEventEvidence.evidence_link_id)
        .where(
            DependencyEvent.project_id == project_id,
            DependencyEventScopeDecision.scope_mode == "selected",
            DependencyEventScopeDecision.decided_by.not_in((MACHINE_ACTOR,)),
            current_statement_event_filter(DependencyEvent.id),
        )
        .order_by(DependencyEventScopeDecision.id, EvidenceLink.id)
    ).all()
    latest: dict[int, tuple[DependencyEventScopeDecision, DependencyEvent, EvidenceLink]] = {}
    for decision, event, evidence in rows:
        latest[decision.event_id] = (decision, event, evidence)
    contradictions: list[int] = []
    for event_id, (decision, event, evidence) in latest.items():
        if event.affected_external_org_id is None:
            continue
        expected = tuple(
            sorted(
                session.scalars(
                    select(DependencyEventScope.dependency_id).where(
                        DependencyEventScope.scope_decision_id == decision.id
                    )
                ).all()
            )
        )
        dependencies = session.scalars(
            select(Dependency).where(
                Dependency.project_id == project_id,
                Dependency.external_org_id == event.affected_external_org_id,
                Dependency.dismissed_at.is_(None),
            )
        ).all()
        match = match_statement_scope(
            dependencies,
            organization_id=event.affected_external_org_id,
            wording=f"{event.description} {evidence.quote}",
        )
        if match.kind == "exact" and match.dependency_ids != expected:
            contradictions.append(event_id)
    return StatementScopeReplay(len(latest), tuple(sorted(contradictions)))


def run_identifying_language_admission(
    session: Session,
    project: Project,
    *,
    prepare_candidate: Callable[[Candidate], object],
) -> StatementScopeRun:
    """Record exact scopes only after this rule's own real-history replay."""
    replay = replay_matches_human_scope_decisions(session, project.id)
    if not replay.passed:
        return StatementScopeRun(0, None, replay)

    from corridor.supersession import actionable_candidate_query

    placements = []
    candidates = session.scalars(
        actionable_candidate_query(project.id)
        .where(Candidate.kind == "event", Candidate.state == "pending")
        .order_by(Candidate.id)
    ).all()
    for candidate in candidates:
        # Explicit references remain the predecessor's unchanged tier.
        facts = prepare_candidate_statement_facts(session, candidate)
        if str(facts.fields.get("conflict_ref") or "").strip():
            continue
        verdict = prepare_candidate(candidate)
        if not hasattr(verdict, "candidate"):
            continue
        dependencies = session.scalars(
            select(Dependency).where(
                Dependency.project_id == project.id,
                Dependency.external_org_id == verdict.stated_external_org_id,
                Dependency.dismissed_at.is_(None),
            )
        ).all()
        match = match_statement_scope(
            dependencies,
            organization_id=verdict.stated_external_org_id,
            wording=f"{verdict.fields.get('description') or ''} {verdict.evidence.quote}",
            station_text=str(facts.fields.get("station") or "") or None,
        )
        if match.kind == "exact":
            placements.append((verdict, match))
    if not placements:
        return StatementScopeRun(0, None, replay)

    policy_json = canonical_policy()
    with session.begin_nested():
        run = PolicyRun(
            project_id=project.id,
            family="event-admission",
            policy_approval_id=None,
            policy_version=POLICY_VERSION,
            policy_sha256=policy.canonical_sha256(policy_json),
            abstention_reason_version=REASON_VERSION,
            applied_count=len(placements),
            abstained_count=0,
        )
        session.add(run)
        session.flush([run])
        for placement, match in placements:
            event = record_external_party_statement(
                session,
                project_id=project.id,
                affected_external_org_id=placement.stated_external_org_id,
                stated_party=placement.stated_party,
                stated_external_org_id=placement.stated_external_org_id,
                source_kind="cited",
                event_date=placement.event_date,
                description=str(placement.fields.get("description") or ""),
                new_timing=placement.new_timing,
                previous_timing=None,
                scope=StatementScope.selected(match.dependency_ids),
                created_by=MACHINE_ACTOR,
                evidence=placement.evidence,
            )
            placement.candidate.state = "accepted"
            placement.candidate.adjudicated_at = datetime.now(timezone.utc)
            disposition = CandidateDisposition(
                candidate_id=placement.candidate.id,
                disposition="accepted",
                reason=None,
                recorded_by=MACHINE_ACTOR,
            )
            session.add(disposition)
            outcome = EventAdmissionOutcome(
                policy_run_id=run.id,
                candidate_id=placement.candidate.id,
                outcome="admitted",
                dependency_event_id=event.id,
                eligibility_json={"match": match.evidence, "replay_case_count": replay.case_count},
                eligibility_sha256=policy.canonical_sha256(match.evidence),
            )
            session.add(outcome)
            audit.record(
                session, actor=MACHINE_ACTOR, action=audit.ADMIT_EVENT,
                entity_type=audit.DEPENDENCY, entity_id=match.dependency_ids[0],
                after={"policy_run_id": run.id, "candidate_id": placement.candidate.id,
                       "dependency_event_id": event.id, "matcher_fingerprint": matcher_fingerprint()},
            )
    return StatementScopeRun(len(placements), run.id, replay)
