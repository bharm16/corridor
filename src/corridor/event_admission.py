"""Mechanical attachment of what the minutes say to the record.

An event Candidate enters through a named, versioned policy — or through
Adjudication — and through nothing else (ADR-0026, with the human
authorization removed by ADR-0029). Every check here is a replayable
computation: the quote verified on its page, the type inside the policy,
a parseable date, a reference resolving to exactly one Dependency, the
party matching that Dependency's External Party, and the actor not being
the project's own side. Nothing consults a model. A model may order the
residue or flag an event into it, and may never put one on the record.

The machine acts only where it can prove eligibility, and everything it
cannot prove abstains — left pending for Adjudication rather than forced
onto the record. What differs from the dependency family is the act:
this writes a DependencyEvent, which carries an External Party's
statement, so the actor boundary is a check rather than an afterthought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
import subprocess

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corridor import audit
from corridor import dependency_events
from corridor import identity
from corridor import policy
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementRefusal,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
    validate_external_party_statement_draft,
)
from corridor.models import (
    Candidate,
    CandidateDisposition,
    Dependency,
    DependencyEvent,
    DependencyEventScopeDecision,
    Document,
    EventAdmissionAcceptanceReceipt,
    EventAdmissionActivation,
    EventAdmissionOutcome,
    ExternalOrg,
    PolicyRun,
    Project,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.verify import normalize

EVENT_ADMISSION_POLICY_VERSION = "event-admission-v2"
UNKNOWN_SCOPE_POLICY_VERSION = "event-admission-v3-unknown-scope"
FAMILY = "event-admission"
ABSTENTION_REASON_VERSION = "event-admission-abstentions-v2"
UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION = "event-admission-abstentions-v3"
MACHINE_ACTOR = "corridor:event-admission"

OUTCOME_ADMITTED = "admitted"
OUTCOME_ABSTAINED = "abstained"

# The types the policy will admit. A response or a status change says
# something happened without stating a date anyone is held to, and the
# date lanes read only these three.
ADMISSIBLE_EVENT_TYPES = ("commitment", "committed_date_change")

ABSTENTION_REASONS = frozenset(
    {
        "citations_unverified",
        "event_type_outside_policy",
        "no_date",
        "unparseable_date",
        "no_conflict_reference",
        "reference_resolves_to_no_dependency",
        "reference_resolves_to_many",
        "party_unstated",
        "stated_party_unresolved",
        "party_mismatch",
        "project_side_actor",
        "statement_draft_invalid",
    }
)

UNKNOWN_SCOPE_ABSTENTION_REASONS = frozenset(
    {
        "citations_unverified",
        "event_type_outside_policy",
        "description_missing",
        "description_not_in_evidence",
        "timing_missing",
        "timing_invalid",
        "timing_not_in_evidence",
        "previous_timing_present",
        "conflict_reference_present",
        "party_unstated",
        "stated_party_unresolved",
        "stated_party_ambiguous",
        "stated_party_not_in_evidence",
        "affected_party_unresolved",
        "affected_party_ambiguous",
        "affected_party_disagreement",
        "project_side_actor",
        "statement_draft_invalid",
        "stale_active_run_or_candidate",
        "cross_project_association",
        "write_integrity_failure",
    }
)


@dataclass(frozen=True)
class EventAdmissionAbstention:
    candidate_id: int
    reason: str
    reason_version: str = ABSTENTION_REASON_VERSION


@dataclass(frozen=True)
class EventAdmissionResult:
    run_id: int
    admitted_count: int
    abstained_count: int
    abstentions: list[EventAdmissionAbstention] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedStatementPlacement:
    """Target-independent facts proved before one Unplaced Statement attaches."""

    event_type: str
    event_date: date | None
    new_timing: StatementTiming
    previous_timing: StatementTiming | None
    affected_party: str
    stated_party: str
    stated_external_org_id: int
    description: str
    evidence: CitedStatementEvidence


@dataclass(frozen=True)
class UnknownScopeAdmission:
    candidate: Candidate
    fields: dict
    event_date: date | None
    new_timing: StatementTiming
    stated_party: str
    stated_external_org_id: int
    evidence: CitedStatementEvidence
    input_receipt: dict


class UnknownScopeWriteIntegrity(RuntimeError):
    """An eligible row could not produce its exact protected write set."""


def run_event_admission(
    session: Session,
    project_id: int,
    *,
    policy_version: str | None = None,
) -> EventAdmissionResult:
    """Attach what the minutes say to the conflicts they name.

    No authorization stands in front of this (ADR-0029); the run records
    the policy version, the project-side parties it read, and the
    deployed bytes of these checks, so the receipt still says what ran.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} does not exist")
    selected_version = policy_version or _normal_policy_version(session, project_id)
    if policy_version is None and selected_version == UNKNOWN_SCOPE_POLICY_VERSION:
        predecessor = run_event_admission(
            session,
            project_id,
            policy_version=EVENT_ADMISSION_POLICY_VERSION,
        )
        extension = run_event_admission(
            session,
            project_id,
            policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
        )
        return EventAdmissionResult(
            run_id=extension.run_id,
            admitted_count=predecessor.admitted_count + extension.admitted_count,
            abstained_count=extension.abstained_count,
            abstentions=extension.abstentions,
        )
    if selected_version == UNKNOWN_SCOPE_POLICY_VERSION:
        return _run_unknown_scope_admission(session, project)
    if selected_version != EVENT_ADMISSION_POLICY_VERSION:
        raise ValueError(f"unsupported Event Admission policy {selected_version!r}")
    lock_project(session, project_id)
    policy_json = _canonical_policy(project, EVENT_ADMISSION_POLICY_VERSION)

    # Only candidates from declared Active Runs of current documents —
    # the same scope the dependency policy, the pile, and the human
    # attach all enforce. This was the one writer still reading every
    # pending event, so a re-extracted or replaced minutes document had
    # its stale candidates machine-admitted while every other door
    # refused them.
    from corridor.supersession import actionable_candidate_query

    candidates = session.scalars(
        actionable_candidate_query(project_id)
        .where(Candidate.kind == "event", Candidate.state == "pending")
        .order_by(Candidate.id)
    ).all()

    # Every verdict first, then one receipt written with its final counts:
    # the receipt table is immutable, so a run row is never updated after
    # it exists — which is the property that makes it a receipt.
    admissible: list[
        tuple[
            Candidate,
            Dependency,
            dict,
            date | None,
            StatementTiming,
            StatementTiming | None,
            str,
            int,
            CitedStatementEvidence,
        ]
    ] = []
    abstentions: list[EventAdmissionAbstention] = []
    for candidate in candidates:
        verdict = _evaluate(session, project, candidate)
        if isinstance(verdict, str):
            abstentions.append(
                EventAdmissionAbstention(
                    candidate_id=candidate.id, reason=verdict
                )
            )
        else:
            (
                dependency,
                fields,
                event_date,
                new_timing,
                previous_timing,
                stated_party,
                stated_external_org_id,
                evidence,
            ) = verdict
            admissible.append(
                (
                    candidate,
                    dependency,
                    fields,
                    event_date,
                    new_timing,
                    previous_timing,
                    stated_party,
                    stated_external_org_id,
                    evidence,
                )
            )

    # A policy receipt, its outcomes, the accepted Candidate, the event, and
    # its audit all name one machine act. Keep the whole act behind a
    # savepoint so a late database refusal cannot leave a receipt for an event
    # that never reached the Ledger.
    with session.begin_nested():
        run = PolicyRun(
            project_id=project_id,
            family=FAMILY,
            policy_approval_id=None,
            policy_version=EVENT_ADMISSION_POLICY_VERSION,
            policy_sha256=policy.canonical_sha256(policy_json),
            abstention_reason_version=ABSTENTION_REASON_VERSION,
            applied_count=len(admissible),
            abstained_count=len(abstentions),
        )
        session.add(run)
        session.flush([run])

        for abstention in abstentions:
            session.add(
                EventAdmissionOutcome(
                    policy_run_id=run.id,
                    candidate_id=abstention.candidate_id,
                    outcome=OUTCOME_ABSTAINED,
                    reason=abstention.reason,
                )
            )

        for (
            candidate,
            dependency,
            fields,
            event_date,
            new_timing,
            previous_timing,
            stated_party,
            stated_external_org_id,
            evidence,
        ) in admissible:
            try:
                event = record_external_party_statement(
                    session,
                    project_id=project.id,
                    affected_external_org_id=dependency.external_org_id,
                    stated_party=stated_party,
                    stated_external_org_id=stated_external_org_id,
                    source_kind="cited",
                    event_date=event_date,
                    description=str(fields.get("description") or ""),
                    new_timing=new_timing,
                    previous_timing=previous_timing,
                    scope=StatementScope.selected((dependency.id,)),
                    created_by=MACHINE_ACTOR,
                    evidence=evidence,
                )
            except StatementRefusal as exc:
                raise RuntimeError(
                    f"candidate {candidate.id} passed admission but statement recording refused: {exc}"
                ) from exc

            candidate.state = "accepted"
            candidate.adjudicated_at = datetime.now(timezone.utc)
            session.add(
                EventAdmissionOutcome(
                    policy_run_id=run.id,
                    candidate_id=candidate.id,
                    outcome=OUTCOME_ADMITTED,
                    dependency_event_id=event.id,
                )
            )
            audit.record(
                session,
                actor=MACHINE_ACTOR,
                action=audit.ADMIT_EVENT,
                entity_type=audit.DEPENDENCY,
                entity_id=dependency.id,
                after={
                    "policy_run_id": run.id,
                    "candidate_id": candidate.id,
                    "dependency_event_id": event.id,
                    "policy_sha256": run.policy_sha256,
                },
            )
        session.flush()
    return EventAdmissionResult(
        run_id=run.id,
        admitted_count=len(admissible),
        abstained_count=len(abstentions),
        abstentions=abstentions,
    )


def _run_unknown_scope_admission(
    session: Session, project: Project
) -> EventAdmissionResult:
    """Apply only ADR-0042's exact party-level Commitment class."""
    lock_project(session, project.id)
    policy_json = _canonical_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
    policy_sha256 = policy.canonical_sha256(policy_json)

    from corridor.supersession import actionable_candidate_query

    candidates = session.scalars(
        actionable_candidate_query(project.id)
        .where(Candidate.kind == "event", Candidate.state == "pending")
        .order_by(Candidate.id)
    ).all()
    prior_abstentions: dict[int, EventAdmissionOutcome] = {}
    for outcome in session.scalars(
        select(EventAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            PolicyRun.policy_version == UNKNOWN_SCOPE_POLICY_VERSION,
            EventAdmissionOutcome.outcome == OUTCOME_ABSTAINED,
        )
        .order_by(EventAdmissionOutcome.id)
    ):
        prior_abstentions[outcome.candidate_id] = outcome
    registry_sha256 = policy.canonical_sha256(
        [
            {"id": org.id, "name": org.name, "aliases": sorted(org.aliases or [])}
            for org in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id))
        ]
    )

    prepared: list[UnknownScopeAdmission] = []
    abstentions: list[EventAdmissionAbstention] = []
    abstention_inputs: dict[int, dict] = {}
    for candidate in candidates:
        input_receipt = _unknown_scope_input_receipt(
            candidate,
            policy_sha256=policy_sha256,
            external_org_registry_sha256=registry_sha256,
        )
        prior = prior_abstentions.get(candidate.id)
        if (
            prior is not None
            and isinstance(prior.eligibility_json, dict)
            and prior.eligibility_json.get("input") == input_receipt
        ):
            continue
        verdict = _evaluate_unknown_scope(
            session, project, candidate, input_receipt=input_receipt
        )
        if isinstance(verdict, str):
            abstention_inputs[candidate.id] = input_receipt
            abstentions.append(
                EventAdmissionAbstention(
                    candidate_id=candidate.id,
                    reason=verdict,
                    reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
                )
            )
        else:
            prepared.append(verdict)

    admitted: list[
        tuple[
            UnknownScopeAdmission,
            DependencyEvent,
            DependencyEventScopeDecision,
            CandidateDisposition,
        ]
    ] = []
    with session.begin_nested():
        for placement in prepared:
            try:
                with session.begin_nested():
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
                        scope=StatementScope.unknown(),
                        created_by=MACHINE_ACTOR,
                        evidence=placement.evidence,
                    )
                    scope_decision = session.scalar(
                        select(DependencyEventScopeDecision).where(
                            DependencyEventScopeDecision.event_id == event.id
                        )
                    )
                    if scope_decision is None or scope_decision.scope_mode != "unknown":
                        raise UnknownScopeWriteIntegrity(
                            "unknown-scope admission did not create its exact scope decision"
                        )
                    disposition = CandidateDisposition(
                        candidate_id=placement.candidate.id,
                        disposition="accepted",
                        reason=None,
                        recorded_by=MACHINE_ACTOR,
                    )
                    session.add(disposition)
                    placement.candidate.state = "accepted"
                    placement.candidate.adjudicated_at = datetime.now(timezone.utc)
                    session.flush([disposition, placement.candidate])
                admitted.append((placement, event, scope_decision, disposition))
            except (StatementRefusal, IntegrityError, UnknownScopeWriteIntegrity):
                abstention_inputs[placement.candidate.id] = placement.input_receipt
                abstentions.append(
                    EventAdmissionAbstention(
                        candidate_id=placement.candidate.id,
                        reason="write_integrity_failure",
                        reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
                    )
                )

        run = PolicyRun(
            project_id=project.id,
            family=FAMILY,
            policy_approval_id=None,
            policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
            policy_sha256=policy_sha256,
            abstention_reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
            applied_count=len(admitted),
            abstained_count=len(abstentions),
        )
        session.add(run)
        session.flush([run])

        for abstention in abstentions:
            eligibility = {
                "input": abstention_inputs[abstention.candidate_id],
                "verdict": abstention.reason,
                "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
            }
            session.add(
                EventAdmissionOutcome(
                    policy_run_id=run.id,
                    candidate_id=abstention.candidate_id,
                    outcome=OUTCOME_ABSTAINED,
                    reason=abstention.reason,
                    eligibility_json=eligibility,
                    eligibility_sha256=policy.canonical_sha256(eligibility),
                )
            )

        for placement, event, scope_decision, disposition in admitted:
            audit_entry = audit.record(
                session,
                actor=MACHINE_ACTOR,
                action=audit.ADMIT_EVENT,
                entity_type=audit.COMMITMENT_LINEAGE,
                entity_id=event.commitment_lineage_id,
                after={
                    "policy_run_id": run.id,
                    "candidate_id": placement.candidate.id,
                    "commitment_lineage_id": event.commitment_lineage_id,
                    "dependency_event_id": event.id,
                    "scope_decision_id": scope_decision.id,
                    "candidate_disposition_id": disposition.id,
                    "policy_sha256": run.policy_sha256,
                },
            )
            eligibility = _unknown_scope_eligibility_receipt(
                placement,
                event=event,
                scope_decision=scope_decision,
                disposition=disposition,
                audit_log_id=audit_entry.id,
                policy_json=policy_json,
                policy_sha256=policy_sha256,
            )
            session.add(
                EventAdmissionOutcome(
                    policy_run_id=run.id,
                    candidate_id=placement.candidate.id,
                    outcome=OUTCOME_ADMITTED,
                    dependency_event_id=event.id,
                    commitment_lineage_id=event.commitment_lineage_id,
                    scope_decision_id=scope_decision.id,
                    candidate_disposition_id=disposition.id,
                    audit_log_id=audit_entry.id,
                    eligibility_json=eligibility,
                    eligibility_sha256=policy.canonical_sha256(eligibility),
                )
            )
        session.flush()

    return EventAdmissionResult(
        run_id=run.id,
        admitted_count=len(admitted),
        abstained_count=len(abstentions),
        abstentions=abstentions,
    )


def _evaluate(
    session: Session, project: Project, candidate: Candidate
) -> str | tuple[
    Dependency,
    dict,
    date | None,
    StatementTiming,
    StatementTiming | None,
    str,
    int,
    CitedStatementEvidence,
]:
    """Every check, in order. A string is the abstention reason."""
    if not candidate.citations_verified:
        return "citations_unverified"

    fields = (candidate.payload_json or {}).get("fields", {})
    if fields.get("event_type") not in ADMISSIBLE_EVENT_TYPES:
        return "event_type_outside_policy"

    # Two dates, kept two: when the party spoke, and what they promised.
    # A missing meeting date is recorded as missing — never filled in
    # from the promised date, because the promised date is not evidence
    # of when anything was said, and the Committed Date projection
    # orders by exactly that.
    raw_event_date = fields.get("event_date")
    raw_committed_date = fields.get("committed_date")
    if not raw_committed_date:
        return "no_date"
    event_date = _parse_date(raw_event_date) if raw_event_date else None
    new_timing = _timing_from_candidate(raw_committed_date)
    if (raw_event_date and event_date is None) or new_timing is None:
        return "unparseable_date"

    ref = fields.get("conflict_ref")
    if not ref:
        return "no_conflict_reference"

    matches = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project.id,
            Dependency.source_ref == str(ref),
            # A record nobody is working takes no statements (ADR-0032).
            # The human attach refuses this; the machine must not be the
            # looser door — an event written here would be filed where
            # the list, the engine, and the pile never look again.
            Dependency.dismissed_at.is_(None),
        )
    ).all()
    if not matches:
        return "reference_resolves_to_no_dependency"
    if len(matches) > 1:
        # Under a per-party numbering scheme (ADR-0030) one number names
        # a row on several parties' lists, and the statement's stated
        # party is the other half of the name. Narrowing by it is the
        # same alias-bounded match the party check below applies — not a
        # second, looser rule.
        affected = str(fields.get("external_org") or "").strip()
        matches = [d for d in matches if affected and identity.party_matches(session, d, affected)]
        if len(matches) != 1:
            return "reference_resolves_to_many"
    [dependency] = matches

    affected_party = str(fields.get("external_org") or "").strip()
    if not affected_party:
        return "party_unstated"
    if dependency.external_org_id is None or not identity.party_matches(
        session, dependency, affected_party
    ):
        return "party_mismatch"
    stated_party = str(fields.get("stated_party") or "").strip()
    if not stated_party:
        # ``external_org`` is affected-party context from old extractors.  It
        # is never proof of who spoke.
        return "party_unstated"
    if identity.is_project_side_party(project, stated_party):
        # The project's own engineer taking an action item is a Next
        # Action's territory, never an External Party's commitment
        # (ADR-0026). Adjudication may still record it by hand.
        return "project_side_actor"
    stated_external_org = _external_org_for_party(session, stated_party)
    if stated_external_org is None:
        return "stated_party_unresolved"
    evidence = _candidate_evidence(candidate)
    if evidence is None:
        return "citations_unverified"
    previous_timing = _previous_timing_from_candidate(fields.get("previous_timing"))
    if fields.get("event_type") == "committed_date_change" and previous_timing is None:
        return "no_date"
    if fields.get("event_type") == "commitment" and previous_timing is not None:
        return "event_type_outside_policy"
    try:
        validate_external_party_statement_draft(
            session,
            project_id=project.id,
            stated_party=stated_party,
            stated_external_org_id=stated_external_org.id,
            source_kind="cited",
            event_date=event_date,
            description=str(fields.get("description") or ""),
            new_timing=new_timing,
            previous_timing=previous_timing,
            evidence=evidence,
        )
    except StatementRefusal as exc:
        message = str(exc)
        if "timing" in message or "calendar bounds" in message:
            return "unparseable_date"
        if "Evidence" in message:
            return "citations_unverified"
        return "statement_draft_invalid"
    return (
        dependency,
        fields,
        event_date,
        new_timing,
        previous_timing,
        stated_party,
        stated_external_org.id,
        evidence,
    )


def _evaluate_unknown_scope(
    session: Session,
    project: Project,
    candidate: Candidate,
    *,
    input_receipt: dict,
) -> str | UnknownScopeAdmission:
    """Prove ADR-0042's class without consulting model-derived confidence."""
    document = session.get(Document, candidate.source_document_id)
    if (
        candidate.project_id != project.id
        or document is None
        or document.project_id != project.id
    ):
        return "cross_project_association"
    if not candidate.citations_verified:
        return "citations_unverified"

    fields = (candidate.payload_json or {}).get("fields", {})
    if not isinstance(fields, dict) or fields.get("event_type") != "commitment":
        return "event_type_outside_policy"
    if not str(fields.get("description") or "").strip():
        return "description_missing"
    if fields.get("previous_timing") is not None:
        return "previous_timing_present"
    if str(fields.get("conflict_ref") or "").strip():
        return "conflict_reference_present"

    raw_timing = fields.get("committed_date")
    if raw_timing is None or raw_timing == "":
        return "timing_missing"
    new_timing = _timing_from_candidate(raw_timing)
    if new_timing is None:
        return "timing_invalid"
    raw_event_date = fields.get("event_date")
    event_date = _parse_date(raw_event_date) if raw_event_date else None
    if raw_event_date and event_date is None:
        return "timing_invalid"

    stated_party = str(fields.get("stated_party") or "").strip()
    affected_party = str(fields.get("external_org") or "").strip()
    if not stated_party or not affected_party:
        return "party_unstated"
    if identity.is_project_side_party(project, stated_party):
        return "project_side_actor"
    stated_matches = _external_orgs_for_party(session, stated_party)
    if not stated_matches:
        return "stated_party_unresolved"
    if len(stated_matches) != 1:
        return "stated_party_ambiguous"
    affected_matches = _external_orgs_for_party(session, affected_party)
    if not affected_matches:
        return "affected_party_unresolved"
    if len(affected_matches) != 1:
        return "affected_party_ambiguous"
    [stated_external_org] = stated_matches
    [affected_external_org] = affected_matches
    if stated_external_org.id != affected_external_org.id:
        return "affected_party_disagreement"

    evidence = _candidate_evidence(candidate)
    if evidence is None:
        return "citations_unverified"
    evidence_text = normalize(evidence.quote)
    if normalize(str(fields.get("description") or "")) not in evidence_text:
        return "description_not_in_evidence"
    if normalize(new_timing.text) not in evidence_text:
        return "timing_not_in_evidence"
    if normalize(stated_party) not in evidence_text:
        return "stated_party_not_in_evidence"
    try:
        validate_external_party_statement_draft(
            session,
            project_id=project.id,
            stated_party=stated_party,
            stated_external_org_id=stated_external_org.id,
            source_kind="cited",
            event_date=event_date,
            description=str(fields.get("description") or ""),
            new_timing=new_timing,
            previous_timing=None,
            evidence=evidence,
        )
    except StatementRefusal as exc:
        message = str(exc)
        if "Evidence" in message or "quote" in message or "page" in message:
            return "citations_unverified"
        if "timing" in message or "calendar bounds" in message:
            return "timing_invalid"
        return "statement_draft_invalid"
    return UnknownScopeAdmission(
        candidate=candidate,
        fields=fields,
        event_date=event_date,
        new_timing=new_timing,
        stated_party=stated_party,
        stated_external_org_id=stated_external_org.id,
        evidence=evidence,
        input_receipt=input_receipt,
    )


def _unknown_scope_input_receipt(
    candidate: Candidate,
    *,
    policy_sha256: str,
    external_org_registry_sha256: str,
) -> dict:
    """Exact deterministic inputs that make an unchanged Abstention idempotent."""
    return {
        "receipt_version": "event-admission-unknown-scope-input-v1",
        "project_id": candidate.project_id,
        "candidate_id": candidate.id,
        "source_document_id": candidate.source_document_id,
        "active_extraction_run_id": candidate.extraction_run_id,
        "candidate_payload_sha256": policy.canonical_sha256(candidate.payload_json),
        "citations_verified": candidate.citations_verified,
        "candidate_state": candidate.state,
        "policy_sha256": policy_sha256,
        "external_org_registry_sha256": external_org_registry_sha256,
    }


def _unknown_scope_eligibility_receipt(
    placement: UnknownScopeAdmission,
    *,
    event: DependencyEvent,
    scope_decision: DependencyEventScopeDecision,
    disposition: CandidateDisposition,
    audit_log_id: int,
    policy_json: dict,
    policy_sha256: str,
) -> dict:
    candidate = placement.candidate
    timing = placement.new_timing
    return {
        "receipt_version": "event-admission-unknown-scope-eligibility-v1",
        "project_id": candidate.project_id,
        "candidate_id": candidate.id,
        "source_document_id": candidate.source_document_id,
        "active_extraction_run_id": candidate.extraction_run_id,
        "candidate_payload_sha256": policy.canonical_sha256(candidate.payload_json),
        "input": placement.input_receipt,
        "evidence": {
            "document_id": placement.evidence.document_id,
            "page": placement.evidence.page_no,
            "quote": placement.evidence.quote.strip(),
            "quote_sha256": policy.canonical_sha256(placement.evidence.quote.strip()),
        },
        "resolved_external_party_id": placement.stated_external_org_id,
        "stated_party": placement.stated_party,
        "timing": {
            "text": timing.text,
            "precision": timing.precision,
            "start_date": timing.start_date.isoformat() if timing.start_date else None,
            "end_date": timing.end_date.isoformat() if timing.end_date else None,
        },
        "policy_version": UNKNOWN_SCOPE_POLICY_VERSION,
        "policy_sha256": policy_sha256,
        "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        "policy": policy_json,
        "created": {
            "commitment_lineage_id": event.commitment_lineage_id,
            "statement_event_id": event.id,
            "scope_decision_id": scope_decision.id,
            "candidate_disposition_id": disposition.id,
            "audit_log_id": audit_log_id,
        },
    }


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _timing_from_candidate(value: object) -> StatementTiming | None:
    """Read only timing precision the Candidate explicitly supplies."""
    if isinstance(value, dict):
        text = str(value.get("text") or "").strip()
        precision = str(value.get("precision") or "").strip()
        if precision == "approximate" and text:
            return StatementTiming.approximate(text)
        start = _parse_date(value.get("start_date"))
        end = _parse_date(value.get("end_date"))
        if precision == "day" and start is not None and end == start and text:
            return StatementTiming.day(text, start)
        if precision == "month" and start is not None and end is not None and text:
            return StatementTiming(text, "month", start, end)
        return None
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return StatementTiming.day(str(value), parsed)


def _previous_timing_from_candidate(value: object) -> StatementTiming | None:
    return _timing_from_candidate(value) if isinstance(value, dict) else None


def _external_org_for_party(session: Session, party: str) -> ExternalOrg | None:
    """Resolve one registered party spelling; ambiguity is not admission proof."""
    wanted = identity.normalize_party(party)
    matches = [
        org
        for org in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id))
        if any(
            identity.normalize_party(name) == wanted
            for name in (org.name, *(org.aliases or []))
            if name
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _external_orgs_for_party(session: Session, party: str) -> list[ExternalOrg]:
    wanted = identity.normalize_party(party)
    return [
        org
        for org in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id))
        if any(
            identity.normalize_party(name) == wanted
            for name in (org.name, *(org.aliases or []))
            if name
        )
    ]


def _candidate_evidence(candidate: Candidate) -> CitedStatementEvidence | None:
    """Select the event's one canonical verified page citation."""
    for citation in (candidate.payload_json or {}).get("citations", []):
        if not isinstance(citation, dict) or citation.get("verified") is not True:
            continue
        document_id = citation.get("document_id")
        page_no = citation.get("page")
        quote = citation.get("quote")
        if isinstance(document_id, int) and isinstance(page_no, int) and isinstance(quote, str):
            if quote.strip():
                return CitedStatementEvidence(document_id, page_no, quote)
    return None


def _rule_source_bytes() -> tuple[tuple[str, bytes], ...]:
    """The deployed bytes of the code that decides admission.

    ADR-0022's guarantee, inherited here: a digest over configuration
    alone would let someone loosen a check — widen a date format, relax
    the party match — and keep running under an authorization no one
    re-read. The authorization covers the rules, and the rules are code.
    """
    from pathlib import Path

    from corridor import models as models_module
    from corridor import principals as principals_module
    from corridor import external_statements as external_statements_module

    paths = (
        ("corridor.event_admission", Path(__file__)),
        ("corridor.dependency_events", Path(dependency_events.__file__)),
        ("corridor.external_statements", Path(external_statements_module.__file__)),
        ("corridor.audit", Path(audit.__file__)),
        ("corridor.identity", Path(identity.__file__)),
        ("corridor.policy", Path(policy.__file__)),
        ("corridor.models", Path(models_module.__file__)),
        ("corridor.principals", Path(principals_module.__file__)),
        (
            "corridor.migrations.c7d2f5a83b46",
            Path(__file__).parent
            / "migrations/versions/c7d2f5a83b46_add_event_admission.py",
        ),
        (
            "corridor.migrations.a217e4f3a2b1",
            Path(__file__).parent
            / "migrations/versions/a217e4f3a2b1_external_party_statement_shape.py",
        ),
        (
            "corridor.migrations.b257d0f7a315",
            Path(__file__).parent
            / "migrations/versions/b257d0f7a315_unknown_scope_event_admission.py",
        ),
        (
            "corridor.migrations.c257e1a8b426",
            Path(__file__).parent
            / "migrations/versions/c257e1a8b426_seal_event_admission_acceptance.py",
        ),
        (
            "corridor.migrations.d257f2b9c537",
            Path(__file__).parent
            / "migrations/versions/d257f2b9c537_guard_event_admission_activation.py",
        ),
    )
    return tuple((name, path.read_bytes()) for name, path in paths)




def _rules_digest() -> str:
    return policy.digest_of_sources(_rule_source_bytes)


def _current_source_revision() -> str | None:
    """Return the deployed checkout revision, or no identity on uncertainty."""
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    revision = completed.stdout.strip()
    if completed.returncode != 0 or len(revision) != 40:
        return None
    return revision


def _current_migration_head(session: Session) -> str | None:
    """Return one exact database revision; ambiguity suspends the extension."""
    revisions = tuple(
        session.scalars(text("select version_num from alembic_version")).all()
    )
    return str(revisions[0]) if len(revisions) == 1 else None


def _acceptance_receipt_is_current(
    session: Session, receipt: EventAdmissionAcceptanceReceipt
) -> bool:
    """Require the runtime source and schema identities proved by the receipt."""
    return (
        receipt.source_revision == _current_source_revision()
        and receipt.migration_head == _current_migration_head(session)
    )


def _normal_policy_version(session: Session, project_id: int) -> str:
    """Read the latest append-only activation act; suspension restores v2."""
    row = session.execute(
        select(EventAdmissionActivation, EventAdmissionAcceptanceReceipt)
        .join(
            EventAdmissionAcceptanceReceipt,
            EventAdmissionAcceptanceReceipt.id
            == EventAdmissionActivation.acceptance_receipt_id,
        )
        .where(EventAdmissionActivation.project_id == project_id)
        .order_by(EventAdmissionActivation.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return EVENT_ADMISSION_POLICY_VERSION
    latest, receipt = row
    newest_receipt_id = session.scalar(
        select(EventAdmissionAcceptanceReceipt.id)
        .where(EventAdmissionAcceptanceReceipt.project_id == project_id)
        .order_by(EventAdmissionAcceptanceReceipt.id.desc())
        .limit(1)
    )
    project = session.get(Project, project_id)
    if (
        project is not None
        and latest.action == "activate"
        and latest.policy_version == UNKNOWN_SCOPE_POLICY_VERSION
        and receipt.id == newest_receipt_id
        and receipt.status == "passed"
        and _acceptance_receipt_is_current(session, receipt)
        and receipt.policy_sha256
        == policy.canonical_sha256(
            _canonical_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
        )
    ):
        return UNKNOWN_SCOPE_POLICY_VERSION
    return EVENT_ADMISSION_POLICY_VERSION


def _canonical_policy(project: Project, policy_version: str) -> dict:
    """What the approval is approving — the rules, exactly.

    Three things move this digest, and each must pause the policy until
    a principal authorizes the replacement: the stated configuration,
    the project's project-side parties (they decide which events may
    carry a commitment), and the deployed bytes of the deciding code.
    """
    unknown_scope = policy_version == UNKNOWN_SCOPE_POLICY_VERSION
    if not unknown_scope and policy_version != EVENT_ADMISSION_POLICY_VERSION:
        raise ValueError(f"unsupported Event Admission policy {policy_version!r}")
    return {
        "policy_version": policy_version,
        "abstention_reason_version": (
            UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION
            if unknown_scope
            else ABSTENTION_REASON_VERSION
        ),
        "admissible_event_types": (
            ["commitment"] if unknown_scope else list(ADMISSIBLE_EVENT_TYPES)
        ),
        "scope_mode": "unknown" if unknown_scope else "selected",
        "abstention_reasons": sorted(
            UNKNOWN_SCOPE_ABSTENTION_REASONS if unknown_scope else ABSTENTION_REASONS
        ),
        "project_side_parties": sorted(
            str(p) for p in (project.project_side_parties or [])
        ),
        "checks": (
            [
                "current_candidate_from_declared_active_run",
                "registered_evidence_quote_verified",
                "one_commitment_only",
                "nonempty_supported_description",
                "one_supported_timing_at_stated_precision",
                "one_registered_non_project_stated_party",
                "affected_party_equals_stated_party",
                "no_previous_timing",
                "no_conflict_reference",
                "ordinary_statement_validators",
            ]
            if unknown_scope
            else [
                "citations_verified",
                "event_type_admissible",
                "source_timing_explicit_and_parseable",
                "reference_resolves_to_exactly_one_dependency",
                "affected_party_matches_scope",
                "stated_party_is_resolved",
                "actor_is_not_project_side",
            ]
        ),
        "rules_digest_method": "sha256-rule-source-files-v1",
        "rules_digest": _rules_digest(),
    }




class StatementUnplaceable(ValueError):
    """This Candidate cannot become an event on the named record."""


def waiting_statements(
    session: Session,
    project_id: int,
    *,
    include_attachability: bool = True,
) -> list[dict]:
    """The statements the machine could not place, and why.

    One visible pile rather than a lane: a dated promise from a meeting
    is exactly what this product exists to catch, and losing it silently
    is worse than showing a short list (ADR-0032). The reason comes from
    the newest run that abstained on the Candidate, in the machine's own
    vocabulary — the caller renders it in the reviewer's.

    The interactive pile requests ``include_attachability`` so each button
    reflects the exact statement-preparation guard. Population-wide readers
    may omit that per-row projection: they still consume this function's
    Active Run scope and latest Admission reason, while the guided mutation
    revalidates attachability before writing.
    """
    reasons = dict(
        session.execute(
            select(
                EventAdmissionOutcome.candidate_id,
                EventAdmissionOutcome.reason,
            )
            .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
            .where(
                PolicyRun.project_id == project_id,
                EventAdmissionOutcome.outcome == OUTCOME_ABSTAINED,
            )
            .order_by(EventAdmissionOutcome.id.asc())
        ).all()
    )
    # Only what a human could actually place. The pile offers an attach
    # button, and offering one the mutation then refuses is the failure
    # the lane rule already names: a Candidate outside its document's
    # declared Active Run, or on a replaced revision, is not actionable
    # by anyone and does not belong on a worklist.
    from corridor.supersession import actionable_candidate_query

    candidates = session.scalars(
        actionable_candidate_query(project_id)
        .where(Candidate.kind == "event", Candidate.state == "pending")
        .order_by(Candidate.id)
    ).all()
    project = session.get(Project, project_id)
    waiting = []
    for candidate in candidates:
        fields = (candidate.payload_json or {}).get("fields", {})
        attachable = None
        if include_attachability:
            try:
                _prepare_statement_placement(session, project, candidate)
                attachable = True
            except StatementUnplaceable:
                attachable = False
        waiting.append(
            {
                "candidate": candidate,
                "reason": reasons.get(candidate.id),
                "event_type": fields.get("event_type"),
                "external_org": fields.get("external_org"),
                "event_date": fields.get("event_date"),
                "committed_date": fields.get("committed_date"),
                "conflict_ref": fields.get("conflict_ref"),
                "description": fields.get("description"),
                # Whether attach could possibly take it: the checks that
                # depend on the statement alone use the same preparation as
                # attach_statement. An enabled button that 409s teaches a
                # reviewer the pile is broken; a disabled one with the
                # reason beside it teaches them what the statement lacks.
                "attachable": attachable,
            }
        )
    return waiting


def _prepare_statement_placement(
    session: Session,
    project: Project,
    candidate: Candidate,
) -> PreparedStatementPlacement:
    """Parse or refuse every placement rule independent of the chosen target."""
    if candidate.kind != "event":
        raise StatementUnplaceable(
            f"candidate {candidate.id} is a {candidate.kind}; only a "
            "statement attaches to a record"
        )
    if candidate.state != "pending":
        raise StatementUnplaceable(
            f"candidate {candidate.id} was already {candidate.state}"
        )
    if candidate.project_id != project.id:
        raise StatementUnplaceable(
            "a statement cannot attach to another project's record"
        )
    if not candidate.citations_verified:
        raise StatementUnplaceable(
            "this statement's quote was not found on its page — check the "
            "page before placing it"
        )
    fields = (candidate.payload_json or {}).get("fields", {})
    event_type = fields.get("event_type")
    if event_type not in ADMISSIBLE_EVENT_TYPES:
        raise StatementUnplaceable(
            f"{event_type!r} is not one of {', '.join(ADMISSIBLE_EVENT_TYPES)}"
        )
    raw_committed_date = fields.get("committed_date")
    new_timing = _timing_from_candidate(raw_committed_date)
    if new_timing is None:
        raise StatementUnplaceable("a statement must carry a date")
    raw_event_date = fields.get("event_date")
    event_date = _parse_date(raw_event_date) if raw_event_date else None
    if raw_event_date and event_date is None:
        raise StatementUnplaceable("a stated date could not be read")
    stated_party = str(fields.get("stated_party") or "").strip()
    if not stated_party:
        raise StatementUnplaceable(
            "the statement must name a registered External Party who spoke"
        )
    if identity.is_project_side_party(project, stated_party):
        raise StatementUnplaceable(
            f"{stated_party} is the project's own side — an action item, "
            "never an External Party commitment"
        )
    stated_external_org = _external_org_for_party(session, stated_party)
    if stated_external_org is None:
        raise StatementUnplaceable(
            "the statement must name a registered External Party who spoke"
        )
    evidence = _candidate_evidence(candidate)
    if evidence is None:
        raise StatementUnplaceable(
            "this statement's quote was not found on its page — check the "
            "page before placing it"
        )
    previous_timing = _previous_timing_from_candidate(fields.get("previous_timing"))
    if event_type == "committed_date_change" and previous_timing is None:
        raise StatementUnplaceable(
            "a Committed Date Change must preserve the earlier stated timing"
        )
    if event_type == "commitment" and previous_timing is not None:
        raise StatementUnplaceable("two stated timings are a Committed Date Change")
    try:
        validate_external_party_statement_draft(
            session,
            project_id=project.id,
            stated_party=stated_party,
            stated_external_org_id=stated_external_org.id,
            source_kind="cited",
            event_date=event_date,
            description=str(fields.get("description") or ""),
            new_timing=new_timing,
            previous_timing=previous_timing,
            evidence=evidence,
        )
    except StatementRefusal as exc:
        raise StatementUnplaceable(str(exc)) from exc
    return PreparedStatementPlacement(
        event_type=event_type,
        event_date=event_date,
        new_timing=new_timing,
        previous_timing=previous_timing,
        affected_party=str(fields.get("external_org") or "").strip(),
        stated_party=stated_party,
        stated_external_org_id=stated_external_org.id,
        description=str(fields.get("description") or ""),
        evidence=evidence,
    )


def attach_statement(
    session: Session,
    candidate: Candidate,
    dependency: Dependency,
    *,
    principal: HumanPrincipal,
) -> DependencyEvent:
    """Put an unplaced statement on the record a human says it belongs to.

    Acceptance builds a Dependency and refuses an event outright; this is
    the gesture that message has always pointed at. What the policy could
    not prove — which record the statement names — a human supplies by
    naming it, and everything the policy would still have refused is
    refused here too: an unparseable date, a type outside the policy, and
    above all the masquerade boundary, because a project-side actor
    stating a delivery date is an internal action item and never an
    External Party's commitment (ADR-0026).
    """
    attacher = require_human_principal(principal)
    if candidate.project_id != dependency.project_id:
        raise StatementUnplaceable(
            "a statement cannot attach to another project's record"
        )
    # The Candidate is mutable reviewer work until this act. Its state,
    # payload, and action scope must all be re-read after the project lock;
    # preparing first could write a stale pre-lock edit.
    from corridor.adjudicate import _require_candidate_action_scope

    try:
        _require_candidate_action_scope(
            session, candidate, historical_document_id=None
        )
    except Exception as exc:
        raise StatementUnplaceable(str(exc)) from exc
    project = session.get(Project, dependency.project_id, populate_existing=True)
    if project is None:
        raise StatementUnplaceable(
            f"dependency {dependency.id} belongs to no registered project"
        )
    # Re-read under the lock — the caller loaded this row before taking
    # it, and a dismissal committed in between must refuse this attach,
    # not race it. The same stale-read the dismiss path re-reads for.
    session.refresh(dependency)
    if dependency.dismissed_at is not None:
        raise StatementUnplaceable(
            f"{dependency.ref_code} was dismissed — a statement cannot "
            "attach to a record nobody is working"
        )
    prepared = _prepare_statement_placement(session, project, candidate)

    if prepared.affected_party and not identity.party_matches(
        session, dependency, prepared.affected_party
    ):
        raise StatementUnplaceable(
            "the statement's affected External Party does not match this record"
        )
    if dependency.external_org_id is None:
        raise StatementUnplaceable("this record has no resolved External Party")

    try:
        # The accepted Candidate and its audit receipt are one human act with
        # the event. A late refusal must not preserve the event while leaving
        # the source Candidate pending or unaudited.
        with session.begin_nested():
            event = record_external_party_statement(
                session,
                project_id=project.id,
                affected_external_org_id=dependency.external_org_id,
                stated_party=prepared.stated_party,
                stated_external_org_id=prepared.stated_external_org_id,
                source_kind="cited",
                event_date=prepared.event_date,
                description=prepared.description,
                new_timing=prepared.new_timing,
                previous_timing=prepared.previous_timing,
                scope=StatementScope.selected((dependency.id,)),
                created_by=attacher.subject,
                evidence=prepared.evidence,
            )
            candidate.state = "accepted"
            candidate.adjudicated_at = datetime.now(timezone.utc)
            audit.record(
                session,
                principal=attacher,
                action=audit.ATTACH_STATEMENT,
                entity_type=audit.DEPENDENCY,
                entity_id=dependency.id,
                after={
                    "candidate_id": candidate.id,
                    "dependency_event_id": event.id,
                    "event_type": prepared.event_type,
                },
            )
            session.flush()
    except StatementRefusal as exc:
        raise StatementUnplaceable(str(exc)) from exc
    return event
