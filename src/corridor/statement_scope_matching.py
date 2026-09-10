"""Replay-gated exact Record Inclusion for statement identifying language.

ADR-0054's exact tier, in front of the existing unknown-scope policy.  Three
outcomes leave this module:

- Exactly one active Constraint survives the full evidence stack: the
  statement records itself with that scope, attributed to this rule and its
  matcher fingerprint, retaining every deciding observation on the receipt.
  A statement whose sole survivor already sits inside exactly one open
  Commitment of the same organization records on that chain as a Change to
  Promised Timing rather than as a new unplaced statement.
- Several survive: one abstention receipt carries the surviving candidates,
  the matched details, and the evidence applied, so the coordination screen
  can render the narrowed-set card (each survivor, or "both/all listed").
  Nothing is auto-selected, and the unknown-scope extension is told to leave
  the Candidate pending rather than swallow it as Applies To not yet known.
- Nothing decides: the Candidate flows to the existing policies, unchanged.

This is an expansion of an automatic Record Inclusion class, so ADR-0050
gates it: it writes nothing until a regression replay of the project's own
recorded human scope decisions passes with at least one real case and no
contradiction.  A new project has zero answer-key cases and is therefore
inactive — exactly the ship-inactive posture of #371's schedule link rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit, policy, replay_gate
from corridor.candidate_statement_facts import prepare_candidate_statement_facts
from corridor.dependency_events import (
    COMMITTED_EVENT_TYPES,
    closed_party_commitment_lineages,
    current_scope_decision_filter,
)
from corridor.external_statements import (
    StatementScope,
    record_external_party_statement,
)
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
    StatementTimingRecord,
)
from corridor.statement_lifecycle import current_statement_event_filter
from corridor.statement_matcher import (
    MATCHER_EVIDENCE_VERSIONS,
    MATCHER_VERSION,
    StatementMatchContext,
    match_statement_scope,
    matcher_fingerprint,
)
from corridor.statement_values import StatementRefusal, StatementTiming


POLICY_VERSION = "event-admission-v4-identifying-language"
MACHINE_ACTOR = "corridor:statement-scope-matcher"
REASON_VERSION = "event-admission-identifying-language-abstentions-v1"
NARROWED_SET_REASON = "identifying_language_narrowed_set"


@dataclass(frozen=True)
class StatementScopeRun:
    admitted_count: int
    run_id: int | None
    replay: replay_gate.ReplayOutcome
    # Candidates whose identifying language narrowed to several survivors.
    # They carry the narrowed-set abstention and must stay visibly pending:
    # the unknown-scope extension skips them instead of recording them with
    # Applies To not yet known.
    withheld_candidate_ids: tuple[int, ...] = ()


def _rule_source_bytes() -> tuple[tuple[str, bytes], ...]:
    """The deployed bytes that decide a scope — the fingerprint's ground truth."""
    from corridor import merge as merge_module
    from corridor import statement_matcher as matcher_module
    from corridor import verify as verify_module

    return (
        ("corridor.statement_scope_matching", Path(__file__).read_bytes()),
        ("corridor.statement_matcher", Path(matcher_module.__file__).read_bytes()),
        ("corridor.merge", Path(merge_module.__file__).read_bytes()),
        ("corridor.verify", Path(verify_module.__file__).read_bytes()),
    )


def canonical_policy() -> dict:
    """The exact rule a passing replay stands on; either digest voids the pass."""
    return {
        "policy_version": POLICY_VERSION,
        "matcher_version": MATCHER_VERSION,
        "matcher_fingerprint": matcher_fingerprint(),
        "evidence_versions": dict(MATCHER_EVIDENCE_VERSIONS),
        "activation": "adr-0050-recorded-human-scope-replay-v1",
        "exact_result": "one-survivor-of-full-stack",
        "rules_digest_method": "sha256-rule-source-files-v1",
        "rules_digest": policy.digest_of_sources(_rule_source_bytes),
    }


# --------------------------------------------------------------------------- #
# Derived context — recorded associations only, computed from current records #
# --------------------------------------------------------------------------- #


def derived_statement_context(
    session: Session, project_id: int, organization_id: int
) -> tuple[StatementMatchContext, dict[int, tuple[int, ...]]]:
    """Build the layer-2 context ADR-0054 allows, from recorded facts only.

    Returns the context plus the organization's open Commitment scopes
    (lineage id -> scoped Constraint ids), which the writer reuses to place a
    timing update on its chain.  Thread and sender evidence stay empty until
    email-borne statements (#372) supply them; the matcher's tiers for them
    are live and null-safe either way.
    """
    closed_lineages = closed_party_commitment_lineages(session, project_id)
    scope_rows = session.execute(
        select(DependencyEvent.commitment_lineage_id, DependencyEventScope.dependency_id)
        .join(DependencyEventScope, DependencyEventScope.event_id == DependencyEvent.id)
        .join(
            DependencyEventScopeDecision,
            DependencyEventScopeDecision.id == DependencyEventScope.scope_decision_id,
        )
        .where(
            DependencyEvent.project_id == project_id,
            DependencyEvent.event_type.in_(COMMITTED_EVENT_TYPES),
            DependencyEvent.attribution_state == "resolved",
            DependencyEvent.affected_external_org_id == organization_id,
            DependencyEvent.stated_external_org_id == organization_id,
            DependencyEvent.commitment_lineage_id.is_not(None),
            current_statement_event_filter(DependencyEvent.id),
            current_scope_decision_filter(),
        )
        .order_by(DependencyEvent.commitment_lineage_id, DependencyEventScope.dependency_id)
    ).all()
    open_lineage_scopes: dict[int, tuple[int, ...]] = {}
    closed_dependency_ids: set[int] = set()
    for lineage_id, dependency_id in scope_rows:
        if lineage_id in closed_lineages:
            closed_dependency_ids.add(dependency_id)
        else:
            open_lineage_scopes[lineage_id] = tuple(
                sorted({*open_lineage_scopes.get(lineage_id, ()), dependency_id})
            )
    # An open recorded follow-up ask names a Constraint through the row's
    # current Next Action projection (ADR-0054's "call the organization and
    # ask" task).  Only an exactly-one open ask narrows, decided in the
    # matcher itself.
    open_ask_ids = tuple(
        session.scalars(
            select(Dependency.id)
            .where(
                Dependency.project_id == project_id,
                Dependency.external_org_id == organization_id,
                Dependency.dismissed_at.is_(None),
                Dependency.next_action.is_not(None),
            )
            .order_by(Dependency.id)
        ).all()
    )
    promise_ids = tuple(
        sorted({dep for ids in open_lineage_scopes.values() for dep in ids})
    )
    context = StatementMatchContext(
        open_ask_dependency_ids=open_ask_ids,
        promise_dependency_ids=promise_ids,
        closed_dependency_ids=tuple(sorted(closed_dependency_ids)),
    )
    return context, open_lineage_scopes


# --------------------------------------------------------------------------- #
# Regression replay (ADR-0050)                                                #
# --------------------------------------------------------------------------- #


def replay_matches_human_scope_decisions(
    session: Session, project_id: int
) -> replay_gate.ReplayOutcome:
    """Compare the exact rule to actual human selected-scope history.

    The cases a person decided are the answer key.  A contradiction is the
    stack producing an exact answer *different* from the person's selection;
    the stack abstaining on a case a person decided is not a contradiction.
    Machine-selected scopes (any ``corridor:*`` actor) are the rule's own or
    a sibling rule's answers, never the answer key.  The comparison, the pass
    rule and the ledger are ``corridor.replay_gate``'s (ADR-0050); this family
    contributes the answer key and the recomputation.
    """
    decisions = session.execute(
        select(DependencyEventScopeDecision, DependencyEvent)
        .join(DependencyEvent, DependencyEvent.id == DependencyEventScopeDecision.event_id)
        .where(
            DependencyEvent.project_id == project_id,
            DependencyEventScopeDecision.scope_mode == "selected",
            DependencyEventScopeDecision.decided_by.not_like("corridor:%"),
            current_statement_event_filter(DependencyEvent.id),
        )
        .order_by(DependencyEventScopeDecision.id)
    ).all()
    latest: dict[int, tuple[DependencyEventScopeDecision, DependencyEvent]] = {}
    for decision, event in decisions:
        latest[decision.event_id] = (decision, event)

    def expected(event_id: int) -> frozenset[int]:
        decision, _ = latest[event_id]
        return frozenset(
            session.scalars(
                select(DependencyEventScope.dependency_id).where(
                    DependencyEventScope.scope_decision_id == decision.id
                )
            ).all()
        )

    def recompute(event_id: int) -> object:
        _, event = latest[event_id]
        if event.affected_external_org_id is None:
            return replay_gate.ABSTAINED
        quotes = session.scalars(
            select(EvidenceLink.quote)
            .join(
                DependencyEventEvidence,
                DependencyEventEvidence.evidence_link_id == EvidenceLink.id,
            )
            .where(DependencyEventEvidence.event_id == event_id)
            .order_by(EvidenceLink.id)
        ).all()
        dependencies = session.scalars(
            select(Dependency).where(
                Dependency.project_id == project_id,
                Dependency.external_org_id == event.affected_external_org_id,
                Dependency.dismissed_at.is_(None),
            )
        ).all()
        context, _unused = derived_statement_context(
            session, project_id, event.affected_external_org_id
        )
        match = match_statement_scope(
            dependencies,
            organization_id=event.affected_external_org_id,
            wording=" ".join((event.description or "", *quotes)),
            context=context,
        )
        if match.kind != "exact":
            return replay_gate.ABSTAINED
        return frozenset(match.dependency_ids)

    return replay_gate.replay(
        family=replay_gate.FAMILY_STATEMENT_SCOPE,
        human_decisions=[
            (event_id, expected(event_id)) for event_id in sorted(latest)
        ],
        recompute=recompute,
    )


# --------------------------------------------------------------------------- #
# The admission run                                                           #
# --------------------------------------------------------------------------- #


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

    policy_json = canonical_policy()
    policy_sha256 = policy.canonical_sha256(policy_json)
    prior_abstentions: dict[int, list[EventAdmissionOutcome]] = {}
    for outcome in session.scalars(
        select(EventAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            PolicyRun.policy_version == POLICY_VERSION,
            EventAdmissionOutcome.outcome == "abstained",
        )
        .order_by(EventAdmissionOutcome.id)
    ):
        prior_abstentions.setdefault(outcome.candidate_id, []).append(outcome)

    candidates = session.scalars(
        actionable_candidate_query(project.id)
        .where(Candidate.kind == "event", Candidate.state == "pending")
        .order_by(Candidate.id)
    ).all()
    context_cache: dict[int, tuple[StatementMatchContext, dict[int, tuple[int, ...]]]] = {}
    placements: list[tuple[object, object, int | None, StatementTiming | None]] = []
    narrowed: list[tuple[Candidate, object, dict]] = []
    withheld: list[int] = []
    for candidate in candidates:
        verdict = prepare_candidate(candidate)
        if not hasattr(verdict, "candidate"):
            # Explicit references stay the predecessor's unchanged tier;
            # abstention strings and duplicate verdicts stay the existing
            # policies' outcomes.
            continue
        organization_id = verdict.stated_external_org_id
        if organization_id not in context_cache:
            context_cache[organization_id] = derived_statement_context(
                session, project.id, organization_id
            )
        context, open_lineage_scopes = context_cache[organization_id]
        facts = prepare_candidate_statement_facts(session, candidate)
        dependencies = session.scalars(
            select(Dependency).where(
                Dependency.project_id == project.id,
                Dependency.external_org_id == organization_id,
                Dependency.dismissed_at.is_(None),
            )
        ).all()
        match = match_statement_scope(
            dependencies,
            organization_id=organization_id,
            wording=" ".join(
                (
                    str(verdict.fields.get("description") or ""),
                    verdict.evidence.quote,
                )
            ),
            station_text=str(facts.fields.get("station") or "") or None,
            context=context,
        )
        if match.kind == "exact":
            [survivor_id] = match.dependency_ids
            lineage_ids = tuple(
                sorted(
                    lineage_id
                    for lineage_id, dependency_ids in open_lineage_scopes.items()
                    if survivor_id in dependency_ids
                )
            )
            lineage_id: int | None = None
            previous_timing: StatementTiming | None = None
            if len(lineage_ids) == 1:
                # Same organization, overlapping identifying language, one
                # open Commitment holding the survivor: this is a Change to
                # Promised Timing on that chain, not a new unplaced statement.
                lineage_id = lineage_ids[0]
                previous_timing = _lineage_current_timing(session, lineage_id)
            placements.append((verdict, match, lineage_id, previous_timing))
        elif match.kind == "ambiguous":
            input_receipt = _narrowed_input_receipt(
                candidate, policy_sha256=policy_sha256, match_evidence=match.evidence
            )
            withheld.append(candidate.id)
            if policy.has_matching_abstention(
                prior_abstentions.get(candidate.id, []),
                input_receipt=input_receipt,
                verdict=NARROWED_SET_REASON,
                reason_version=REASON_VERSION,
            ):
                continue
            narrowed.append((candidate, match, input_receipt))

    if not placements and not narrowed:
        return StatementScopeRun(0, None, replay, tuple(withheld))

    with session.begin_nested():
        run = PolicyRun(
            project_id=project.id,
            family="event-admission",
            policy_approval_id=None,
            policy_version=POLICY_VERSION,
            policy_sha256=policy_sha256,
            abstention_reason_version=REASON_VERSION,
            applied_count=len(placements),
            abstained_count=len(narrowed),
        )
        session.add(run)
        session.flush([run])

        for candidate, match, input_receipt in narrowed:
            eligibility = {
                "input": input_receipt,
                "verdict": NARROWED_SET_REASON,
                "reason_version": REASON_VERSION,
                "card": match.card,
            }
            session.add(
                EventAdmissionOutcome(
                    policy_run_id=run.id,
                    candidate_id=candidate.id,
                    outcome="abstained",
                    reason=NARROWED_SET_REASON,
                    eligibility_json=eligibility,
                    eligibility_sha256=policy.canonical_sha256(eligibility),
                )
            )

        for placement, match, lineage_id, previous_timing in placements:
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
                previous_timing=previous_timing,
                scope=StatementScope.selected(match.dependency_ids),
                created_by=MACHINE_ACTOR,
                evidence=placement.evidence,
                commitment_lineage_id=lineage_id,
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
            eligibility = {
                "match": match.evidence,
                "replay_case_count": replay.case_count,
                "commitment_lineage_id": event.commitment_lineage_id,
            }
            session.add(
                EventAdmissionOutcome(
                    policy_run_id=run.id,
                    candidate_id=placement.candidate.id,
                    outcome="admitted",
                    dependency_event_id=event.id,
                    commitment_lineage_id=event.commitment_lineage_id,
                    eligibility_json=eligibility,
                    eligibility_sha256=policy.canonical_sha256(eligibility),
                )
            )
            audit.record(
                session,
                actor=MACHINE_ACTOR,
                action=audit.ADMIT_EVENT,
                entity_type=audit.DEPENDENCY,
                entity_id=match.dependency_ids[0],
                after={
                    "policy_run_id": run.id,
                    "candidate_id": placement.candidate.id,
                    "dependency_event_id": event.id,
                    "commitment_lineage_id": event.commitment_lineage_id,
                    "matcher_fingerprint": matcher_fingerprint(),
                    "policy_sha256": policy_sha256,
                },
            )
        session.flush()
    return StatementScopeRun(len(placements), run.id, replay, tuple(withheld))


def _narrowed_input_receipt(
    candidate: Candidate, *, policy_sha256: str, match_evidence: dict
) -> dict:
    """Exact unchanged input that makes one narrowed-set Abstention reusable."""
    return {
        "receipt_version": "identifying-language-narrowed-input-v1",
        "project_id": candidate.project_id,
        "candidate_id": candidate.id,
        "source_document_id": candidate.source_document_id,
        "active_extraction_run_id": candidate.extraction_run_id,
        "candidate_payload_sha256": policy.canonical_sha256(candidate.payload_json),
        "policy_sha256": policy_sha256,
        "match_evidence_sha256": policy.canonical_sha256(match_evidence),
    }


def _lineage_current_timing(
    session: Session, commitment_lineage_id: int
) -> StatementTiming | None:
    """The chain's current promised timing, preserved as the previous timing."""
    superseding = DependencyEvent.__table__.alias("superseding")
    event = session.scalar(
        select(DependencyEvent)
        .where(
            DependencyEvent.commitment_lineage_id == commitment_lineage_id,
            current_statement_event_filter(DependencyEvent.id),
            ~select(superseding.c.id)
            .where(superseding.c.supersedes_event_id == DependencyEvent.id)
            .exists(),
        )
        .order_by(DependencyEvent.id)
    )
    if event is None:
        return None
    record = session.scalar(
        select(StatementTimingRecord).where(
            StatementTimingRecord.event_id == event.id,
            StatementTimingRecord.kind == "new",
        )
    )
    if record is None:
        return None
    return StatementTiming(
        record.text, record.precision, record.start_date, record.end_date
    )
