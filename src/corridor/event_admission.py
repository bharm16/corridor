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

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
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
from corridor.candidate_statement_facts import prepare_candidate_statement_facts
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
    EvidenceLink,
    EventAdmissionAcceptanceReceipt,
    EventAdmissionActivation,
    EventAdmissionOutcome,
    ExternalOrg,
    PolicyRun,
    Project,
    StatementEvidence,
    StatementTimingRecord,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.statement_lifecycle import current_statement_event_filter
from corridor.verify import normalize

EVENT_ADMISSION_POLICY_VERSION = "event-admission-v2"
UNKNOWN_SCOPE_POLICY_VERSION = "event-admission-v3-unknown-scope"
FAMILY = "event-admission"
ABSTENTION_REASON_VERSION = "event-admission-abstentions-v3"
UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION = "event-admission-abstentions-v5"
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
        "non_commitment_language",
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
        "matching_open_commitment_exists",
        "commitment_evidence_already_recorded",
        "matching_closed_commitment_not_post_closure",
    }
)

type CommitmentSignature = tuple[
    int,
    int,
    str,
    str,
    str,
    date | None,
    date | None,
]
type EvidenceSourceIdentity = tuple[str, int, str]


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


@dataclass(frozen=True)
class CommitmentMatch:
    commitment_lineage_id: int
    statement_event_id: int
    basis: str
    closure_event_id: int | None = None
    closure_event_date: date | None = None

    def receipt(self) -> dict:
        return {
            "basis": self.basis,
            "commitment_lineage_id": self.commitment_lineage_id,
            "statement_event_id": self.statement_event_id,
            "closure_event_id": self.closure_event_id,
            "closure_event_date": (
                self.closure_event_date.isoformat()
                if self.closure_event_date is not None
                else None
            ),
        }


@dataclass(frozen=True)
class UnknownScopeDuplicate:
    reason: str
    matches: tuple[CommitmentMatch, ...]


@dataclass
class CommitmentRegistry:
    open_facts: dict[CommitmentSignature, list[CommitmentMatch]]
    historical_evidence: dict[EvidenceSourceIdentity, list[CommitmentMatch]]
    closed_facts: dict[CommitmentSignature, list[CommitmentMatch]]

    def matches(
        self, placement: UnknownScopeAdmission, document: Document
    ) -> tuple[CommitmentMatch, ...]:
        evidence_identity = _evidence_source_identity(
            document.sha256, placement.evidence
        )
        historical_matches = list(
            self.historical_evidence.get(evidence_identity, ())
        )
        if historical_matches:
            return _distinct_commitment_matches(historical_matches)
        signature = _unknown_scope_signature(placement)
        open_matches = list(self.open_facts.get(signature, ()))
        if open_matches:
            return _distinct_commitment_matches(open_matches)
        closed_matches: list[CommitmentMatch] = []
        for closed_match in self.closed_facts.get(signature, ()):
            if (
                document.doc_date is None
                or placement.event_date is None
                or closed_match.closure_event_date is None
                or document.doc_date <= closed_match.closure_event_date
                or placement.event_date <= closed_match.closure_event_date
            ):
                closed_matches.append(closed_match)
        return _distinct_commitment_matches(closed_matches)

    def add_admission(
        self,
        placement: UnknownScopeAdmission,
        event: DependencyEvent,
        document_sha256: str,
    ) -> None:
        if event.commitment_lineage_id is None:
            raise UnknownScopeWriteIntegrity(
                "unknown-scope admission did not create a Commitment Lineage"
            )
        open_match = CommitmentMatch(
            commitment_lineage_id=event.commitment_lineage_id,
            statement_event_id=event.id,
            basis="open_facts",
        )
        _append_commitment_match(
            self.open_facts, _unknown_scope_signature(placement), open_match
        )
        evidence_match = CommitmentMatch(
            commitment_lineage_id=event.commitment_lineage_id,
            statement_event_id=event.id,
            basis="historical_evidence",
        )
        _append_commitment_match(
            self.historical_evidence,
            _evidence_source_identity(document_sha256, placement.evidence),
            evidence_match,
        )

    def sha256(self) -> str:
        return policy.canonical_sha256(_commitment_registry_receipt(self))


class UnknownScopeWriteIntegrity(RuntimeError):
    """An eligible row could not produce its exact protected write set."""


def _validated_write_candidate_ids(
    session: Session,
    *,
    project_id: int,
    write_candidate_ids: Sequence[int] | None,
) -> tuple[int, ...] | None:
    """Normalize an optional exact event mutation boundary."""
    if write_candidate_ids is None:
        return None
    values = tuple(write_candidate_ids)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in values
    ) or len(set(values)) != len(values):
        raise ValueError(
            "write_candidate_ids must name unique positive event Candidate ids"
        )

    from corridor.supersession import actionable_candidate_query

    observed_ids = set(
        session.scalars(
            actionable_candidate_query(project_id)
            .where(Candidate.kind == "event", Candidate.id.in_(values))
            .with_only_columns(Candidate.id)
        ).all()
    )
    if observed_ids != set(values):
        raise ValueError(
            "write_candidate_ids must name only current actionable pending "
            f"event Candidates in project {project_id}"
        )
    return tuple(sorted(values))


def run_event_admission(
    session: Session,
    project_id: int,
    *,
    policy_version: str | None = None,
    write_candidate_ids: Sequence[int] | None = None,
) -> EventAdmissionResult:
    """Attach what the minutes say to the conflicts they name.

    No authorization stands in front of this (ADR-0029); the run records
    the policy version, the project-side parties it read, and the
    deployed bytes of these checks, so the receipt still says what ran.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} does not exist")
    lock_project(session, project_id)
    write_scope = _validated_write_candidate_ids(
        session,
        project_id=project_id,
        write_candidate_ids=write_candidate_ids,
    )
    selected_version = policy_version or normal_event_admission_policy_version(
        session, project_id
    )
    if policy_version is None and selected_version == UNKNOWN_SCOPE_POLICY_VERSION:
        predecessor = run_event_admission(
            session,
            project_id,
            policy_version=EVENT_ADMISSION_POLICY_VERSION,
            write_candidate_ids=write_scope,
        )
        extension_scope = write_scope
        if write_scope is not None:
            from corridor.supersession import actionable_candidate_query

            extension_scope = tuple(
                session.scalars(
                    actionable_candidate_query(project_id)
                    .where(
                        Candidate.kind == "event",
                        Candidate.id.in_(write_scope),
                    )
                    .with_only_columns(Candidate.id)
                    .order_by(Candidate.id)
                ).all()
            )
        extension = run_event_admission(
            session,
            project_id,
            policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
            write_candidate_ids=extension_scope,
        )
        return EventAdmissionResult(
            run_id=extension.run_id,
            admitted_count=predecessor.admitted_count + extension.admitted_count,
            abstained_count=extension.abstained_count,
            abstentions=extension.abstentions,
        )
    if selected_version == UNKNOWN_SCOPE_POLICY_VERSION:
        return _run_unknown_scope_admission(
            session,
            project,
            write_candidate_ids=write_scope,
        )
    if selected_version != EVENT_ADMISSION_POLICY_VERSION:
        raise ValueError(f"unsupported Event Admission policy {selected_version!r}")
    policy_json = canonical_event_admission_policy(
        project,
        EVENT_ADMISSION_POLICY_VERSION,
        write_candidate_ids=write_scope,
    )

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
    if write_scope is not None:
        writable_ids = set(write_scope)
        candidates = [
            candidate for candidate in candidates if candidate.id in writable_ids
        ]
    policy_sha256 = policy.canonical_sha256(policy_json)
    registry_sha256 = _predecessor_registry_sha256(session, project.id)
    prior_abstentions: dict[int, list[EventAdmissionOutcome]] = {}
    for outcome in session.scalars(
        select(EventAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            PolicyRun.policy_version == EVENT_ADMISSION_POLICY_VERSION,
            EventAdmissionOutcome.outcome == OUTCOME_ABSTAINED,
        )
        .order_by(EventAdmissionOutcome.id)
    ):
        prior_abstentions.setdefault(outcome.candidate_id, []).append(outcome)

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
    abstention_inputs: dict[int, dict] = {}
    for candidate in candidates:
        input_receipt = _predecessor_input_receipt(
            candidate,
            policy_sha256=policy_sha256,
            registry_sha256=registry_sha256,
        )
        verdict = _evaluate(session, project, candidate)
        if isinstance(verdict, str):
            if policy.has_matching_abstention(
                prior_abstentions.get(candidate.id, []),
                input_receipt=input_receipt,
                verdict=verdict,
                reason_version=ABSTENTION_REASON_VERSION,
            ):
                continue
            abstention_inputs[candidate.id] = input_receipt
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
            policy_sha256=policy_sha256,
            abstention_reason_version=ABSTENTION_REASON_VERSION,
            applied_count=len(admissible),
            abstained_count=len(abstentions),
        )
        session.add(run)
        session.flush([run])

        for abstention in abstentions:
            eligibility = {
                "input": abstention_inputs[abstention.candidate_id],
                "verdict": abstention.reason,
                "reason_version": ABSTENTION_REASON_VERSION,
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


def _predecessor_registry_sha256(session: Session, project_id: int) -> str:
    """Fingerprint every registered fact the predecessor resolver can consult."""
    dependencies = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project_id)
        .order_by(Dependency.id)
    ).all()
    organizations = session.scalars(select(ExternalOrg).order_by(ExternalOrg.id)).all()
    return policy.canonical_sha256(
        {
            "dependencies": [
                {
                    "id": dependency.id,
                    "source_ref": dependency.source_ref,
                    "external_org_id": dependency.external_org_id,
                }
                for dependency in dependencies
            ],
            "organizations": [
                {
                    "id": organization.id,
                    "name": organization.name,
                    "aliases": sorted(organization.aliases or []),
                }
                for organization in organizations
            ],
        }
    )


def _predecessor_input_receipt(
    candidate: Candidate,
    *,
    policy_sha256: str,
    registry_sha256: str,
) -> dict:
    """Exact unchanged input that makes one predecessor Abstention reusable."""
    return {
        "receipt_version": "event-admission-v2-input-v1",
        "project_id": candidate.project_id,
        "candidate_id": candidate.id,
        "source_document_id": candidate.source_document_id,
        "active_extraction_run_id": candidate.extraction_run_id,
        "candidate_payload_sha256": policy.canonical_sha256(candidate.payload_json),
        "citations_verified": candidate.citations_verified,
        "candidate_state": candidate.state,
        "policy_sha256": policy_sha256,
        "registry_sha256": registry_sha256,
    }


def _run_unknown_scope_admission(
    session: Session,
    project: Project,
    *,
    write_candidate_ids: Sequence[int] | None = None,
) -> EventAdmissionResult:
    """Apply only ADR-0042's exact party-level Commitment class."""
    lock_project(session, project.id)
    policy_json = canonical_event_admission_policy(
        project,
        UNKNOWN_SCOPE_POLICY_VERSION,
        write_candidate_ids=write_candidate_ids,
    )
    policy_sha256 = policy.canonical_sha256(policy_json)

    from corridor.supersession import actionable_candidate_query

    candidates = session.scalars(
        actionable_candidate_query(project.id)
        .where(Candidate.kind == "event", Candidate.state == "pending")
        .order_by(Candidate.id)
    ).all()
    if write_candidate_ids is not None:
        writable_ids = set(write_candidate_ids)
        candidates = [
            candidate for candidate in candidates if candidate.id in writable_ids
        ]
    prior_abstentions: dict[int, list[EventAdmissionOutcome]] = {}
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
        prior_abstentions.setdefault(outcome.candidate_id, []).append(outcome)
    external_org_registry_sha256 = policy.canonical_sha256(
        [
            {"id": org.id, "name": org.name, "aliases": sorted(org.aliases or [])}
            for org in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id))
        ]
    )
    commitment_registry = _commitment_registry(session, project.id)
    abstentions: list[EventAdmissionAbstention] = []
    abstention_inputs: dict[int, dict] = {}
    admitted: list[
        tuple[
            UnknownScopeAdmission,
            DependencyEvent,
            DependencyEventScopeDecision,
            CandidateDisposition,
        ]
    ] = []
    with session.begin_nested():
        for candidate in candidates:
            input_receipt = _unknown_scope_input_receipt(
                candidate,
                policy_sha256=policy_sha256,
                external_org_registry_sha256=external_org_registry_sha256,
            )
            verdict = _evaluate_unknown_scope(
                session,
                project,
                candidate,
                input_receipt=input_receipt,
                commitment_registry=commitment_registry,
            )
            if isinstance(verdict, UnknownScopeDuplicate):
                input_receipt = _unknown_scope_input_receipt(
                    candidate,
                    policy_sha256=policy_sha256,
                    external_org_registry_sha256=external_org_registry_sha256,
                    matching_commitments=verdict.matches,
                )
                reason = verdict.reason
                if policy.has_matching_abstention(
                    prior_abstentions.get(candidate.id, []),
                    input_receipt=input_receipt,
                    verdict=reason,
                    reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
                ):
                    continue
                abstention_inputs[candidate.id] = input_receipt
                abstentions.append(
                    EventAdmissionAbstention(
                        candidate_id=candidate.id,
                        reason=reason,
                        reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
                    )
                )
                continue
            if isinstance(verdict, str):
                if policy.has_matching_abstention(
                    prior_abstentions.get(candidate.id, []),
                    input_receipt=input_receipt,
                    verdict=verdict,
                    reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
                ):
                    continue
                abstention_inputs[candidate.id] = input_receipt
                abstentions.append(
                    EventAdmissionAbstention(
                        candidate_id=candidate.id,
                        reason=verdict,
                        reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
                    )
                )
                continue

            placement = replace(
                verdict,
                input_receipt=_unknown_scope_input_receipt(
                    candidate,
                    policy_sha256=policy_sha256,
                    external_org_registry_sha256=external_org_registry_sha256,
                    commitment_registry_sha256=commitment_registry.sha256(),
                ),
            )
            source_document = session.get(Document, placement.evidence.document_id)
            if source_document is None or source_document.project_id != project.id:
                abstention_inputs[placement.candidate.id] = placement.input_receipt
                abstentions.append(
                    EventAdmissionAbstention(
                        candidate_id=placement.candidate.id,
                        reason="write_integrity_failure",
                        reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
                    )
                )
                continue
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
                    if event.commitment_lineage_id is None:
                        raise UnknownScopeWriteIntegrity(
                            "unknown-scope admission did not create a Commitment Lineage"
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
                commitment_registry.add_admission(
                    placement, event, source_document.sha256
                )
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
    facts = prepare_candidate_statement_facts(session, candidate)
    if not facts.evidence_is_complete:
        return "citations_unverified"

    fields = facts.fields
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
    event_date = facts.event_date
    new_timing = facts.new_timing.timing
    if not facts.event_date_is_valid or new_timing is None:
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
    evidence = facts.cited_evidence[0]
    previous_timing = facts.previous_timing.timing
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
    commitment_registry: CommitmentRegistry,
) -> str | UnknownScopeAdmission | UnknownScopeDuplicate:
    """Prove ADR-0042's class without consulting model-derived confidence."""
    document = session.get(Document, candidate.source_document_id)
    if (
        candidate.project_id != project.id
        or document is None
        or document.project_id != project.id
    ):
        return "cross_project_association"
    facts = prepare_candidate_statement_facts(session, candidate)
    if not facts.evidence_is_complete:
        return "citations_unverified"

    fields = facts.fields
    if not isinstance(fields, dict) or fields.get("event_type") != "commitment":
        return "event_type_outside_policy"
    description = facts.description
    if not description:
        return "description_missing"
    if _has_non_commitment_language(description):
        return "non_commitment_language"
    if fields.get("previous_timing") is not None:
        return "previous_timing_present"
    if str(fields.get("conflict_ref") or "").strip():
        return "conflict_reference_present"

    raw_timing = fields.get("committed_date")
    if raw_timing is None or raw_timing == "":
        return "timing_missing"
    new_timing = facts.new_timing.timing
    if new_timing is None:
        return "timing_invalid"
    event_date = facts.event_date
    if not facts.event_date_is_valid:
        return "timing_invalid"

    stated_party = str(fields.get("stated_party") or "").strip()
    affected_party = str(fields.get("external_org") or "").strip()
    if not stated_party or not affected_party:
        return "party_unstated"
    if identity.is_project_side_party(project, stated_party):
        return "project_side_actor"
    stated_ids = facts.stated_party.registered_external_org_ids
    if not stated_ids:
        return "stated_party_unresolved"
    if len(stated_ids) != 1:
        return "stated_party_ambiguous"
    affected_ids = facts.affected_party.registered_external_org_ids
    if not affected_ids:
        return "affected_party_unresolved"
    if len(affected_ids) != 1:
        return "affected_party_ambiguous"
    [stated_external_org_id] = stated_ids
    [affected_external_org_id] = affected_ids
    if stated_external_org_id != affected_external_org_id:
        return "affected_party_disagreement"

    evidence = facts.cited_evidence[0]
    if not facts.description_is_supported:
        return "description_not_in_evidence"
    if not facts.new_timing.is_supported:
        return "timing_not_in_evidence"
    if not facts.stated_party.is_supported:
        return "stated_party_not_in_evidence"
    try:
        validate_external_party_statement_draft(
            session,
            project_id=project.id,
            stated_party=stated_party,
            stated_external_org_id=stated_external_org_id,
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
    placement = UnknownScopeAdmission(
        candidate=candidate,
        fields=fields,
        event_date=event_date,
        new_timing=new_timing,
        stated_party=stated_party,
        stated_external_org_id=stated_external_org_id,
        evidence=evidence,
        input_receipt=input_receipt,
    )
    matches = commitment_registry.matches(placement, document)
    if matches:
        return UnknownScopeDuplicate(_duplicate_reason(matches), matches)
    return placement


def _has_non_commitment_language(description: str) -> bool:
    """Fail closed on exact phrases that describe plans, invitations, or meetings.

    These phrases do not prove an attributable External Party Commitment even
    when a model labels the Candidate as one. False positives remain pending
    for human placement; they never delete or rewrite the Candidate.
    """
    text = f" {normalize(description)} "
    markers = (
        " after ",
        " provided this ",
        " subject to ",
        " if ",
        " invited ",
        " invitation ",
        " scheduled a meeting ",
        " meeting is scheduled ",
        " meeting was scheduled ",
    )
    return any(marker in text for marker in markers)


def _commitment_registry(session: Session, project_id: int) -> CommitmentRegistry:
    """Index current facts and full Evidence history by authoritative identity."""
    commitment_rows = session.execute(
        select(DependencyEvent, StatementTimingRecord)
        .join(
            StatementTimingRecord,
            StatementTimingRecord.event_id == DependencyEvent.id,
        )
        .where(
            DependencyEvent.project_id == project_id,
            DependencyEvent.event_type.in_(dependency_events.COMMITTED_EVENT_TYPES),
            DependencyEvent.commitment_lineage_id.is_not(None),
            DependencyEvent.affected_external_org_id.is_not(None),
            DependencyEvent.stated_external_org_id.is_not(None),
            DependencyEvent.attribution_state == "resolved",
            StatementTimingRecord.kind == "new",
        )
        .order_by(DependencyEvent.commitment_lineage_id, DependencyEvent.id)
    ).all()
    current_event_ids = frozenset(
        session.scalars(
            select(DependencyEvent.id).where(
                DependencyEvent.project_id == project_id,
                DependencyEvent.event_type.in_(
                    dependency_events.COMMITTED_EVENT_TYPES
                ),
                current_statement_event_filter(DependencyEvent.id),
            )
        )
    )
    closures_by_lineage: dict[int, list[DependencyEvent]] = {}
    for closure in _authoritative_closures(session, project_id):
        if closure.closes_commitment_lineage_id is not None:
            closures_by_lineage.setdefault(
                closure.closes_commitment_lineage_id, []
            ).append(closure)

    registry = CommitmentRegistry(
        open_facts={}, historical_evidence={}, closed_facts={}
    )
    open_lineage_ids = frozenset(
        event.commitment_lineage_id
        for event, _ in commitment_rows
        if event.id in current_event_ids
        and event.commitment_lineage_id is not None
        and not closures_by_lineage.get(event.commitment_lineage_id)
    )
    events_by_id: dict[int, DependencyEvent] = {}
    for event, timing in commitment_rows:
        if (
            event.commitment_lineage_id is None
            or event.affected_external_org_id is None
            or event.stated_external_org_id is None
        ):
            continue
        events_by_id[event.id] = event
        signature = _commitment_signature(
            affected_external_org_id=event.affected_external_org_id,
            stated_external_org_id=event.stated_external_org_id,
            description=event.description,
            timing=timing,
        )
        if event.commitment_lineage_id in open_lineage_ids:
            _append_commitment_match(
                registry.open_facts,
                signature,
                CommitmentMatch(
                    commitment_lineage_id=event.commitment_lineage_id,
                    statement_event_id=event.id,
                    basis=(
                        "open_facts"
                        if event.id in current_event_ids
                        else "open_lineage_history"
                    ),
                ),
            )
        for closure in closures_by_lineage.get(event.commitment_lineage_id, ()):
            _append_commitment_match(
                registry.closed_facts,
                signature,
                CommitmentMatch(
                    commitment_lineage_id=event.commitment_lineage_id,
                    statement_event_id=event.id,
                    basis="closed_facts_not_post_closure",
                    closure_event_id=closure.id,
                    closure_event_date=closure.event_date,
                ),
            )

    evidence_rows = session.execute(
        select(StatementEvidence.event_id, EvidenceLink, Document)
        .join(EvidenceLink, EvidenceLink.id == StatementEvidence.evidence_link_id)
        .join(Document, Document.id == EvidenceLink.document_id)
        .where(StatementEvidence.event_id.in_(tuple(events_by_id) or (0,)))
        .order_by(StatementEvidence.event_id, EvidenceLink.id)
    ).all()
    for event_id, evidence, document in evidence_rows:
        event = events_by_id.get(event_id)
        if event is None or event.commitment_lineage_id is None:
            continue
        _append_commitment_match(
            registry.historical_evidence,
            _evidence_source_identity(
                document.sha256,
                CitedStatementEvidence(
                    document_id=evidence.document_id,
                    page_no=evidence.page_no,
                    quote=evidence.quote,
                ),
            ),
            CommitmentMatch(
                commitment_lineage_id=event.commitment_lineage_id,
                statement_event_id=event.id,
                basis="historical_evidence",
            ),
        )
    return registry


def _authoritative_closures(
    session: Session, project_id: int
) -> tuple[DependencyEvent, ...]:
    closures = session.scalars(
        select(DependencyEvent)
        .where(
            DependencyEvent.project_id == project_id,
            DependencyEvent.event_type == "closure",
            DependencyEvent.closes_commitment_lineage_id.is_not(None),
            DependencyEvent.attribution_state == "resolved",
            DependencyEvent.stated_external_org_id.is_not(None),
            current_statement_event_filter(DependencyEvent.id),
        )
        .order_by(DependencyEvent.id)
    ).all()
    cited = dependency_events._verified_party_statement_provenance(
        session, (closure.id for closure in closures), project_id=project_id
    )
    return tuple(
        closure
        for closure in closures
        if (closure.source_kind == "verbal" and closure.event_date is not None)
        or (closure.source_kind == "cited" and closure.id in cited)
    )


def _unknown_scope_signature(placement: UnknownScopeAdmission) -> CommitmentSignature:
    return _commitment_signature(
        affected_external_org_id=placement.stated_external_org_id,
        stated_external_org_id=placement.stated_external_org_id,
        description=str(placement.fields.get("description") or ""),
        timing=placement.new_timing,
    )


def _commitment_signature(
    *,
    affected_external_org_id: int,
    stated_external_org_id: int,
    description: str,
    timing: StatementTiming | StatementTimingRecord,
) -> CommitmentSignature:
    return (
        affected_external_org_id,
        stated_external_org_id,
        normalize(description),
        timing.precision,
        timing.text.strip(),
        timing.start_date,
        timing.end_date,
    )


def _evidence_source_identity(
    document_sha256: str, evidence: CitedStatementEvidence
) -> EvidenceSourceIdentity:
    return (
        document_sha256,
        evidence.page_no,
        policy.canonical_sha256(evidence.quote.strip()),
    )


def _append_commitment_match(registry: dict, key, match: CommitmentMatch) -> None:
    matches = registry.setdefault(key, [])
    if match not in matches:
        matches.append(match)


def _distinct_commitment_matches(
    matches: list[CommitmentMatch],
) -> tuple[CommitmentMatch, ...]:
    distinct = {
        (
            match.commitment_lineage_id,
            match.statement_event_id,
            match.basis,
            match.closure_event_id,
            match.closure_event_date,
        ): match
        for match in matches
    }
    return tuple(
        distinct[key]
        for key in sorted(
            distinct,
            key=lambda value: (
                value[0],
                value[1],
                value[2],
                value[3] if value[3] is not None else -1,
                value[4].isoformat() if value[4] is not None else "",
            ),
        )
    )


def _duplicate_reason(matches: tuple[CommitmentMatch, ...]) -> str:
    bases = {match.basis for match in matches}
    if "historical_evidence" in bases:
        return "commitment_evidence_already_recorded"
    if bases.intersection({"open_facts", "open_lineage_history"}):
        return "matching_open_commitment_exists"
    return "matching_closed_commitment_not_post_closure"


def _commitment_registry_receipt(registry: CommitmentRegistry) -> dict:
    def fact_rows(mapping: dict[CommitmentSignature, list[CommitmentMatch]]) -> list:
        return [
            {
                "facts": _commitment_signature_receipt(signature),
                "matches": [
                    match.receipt()
                    for match in _distinct_commitment_matches(matches)
                ],
            }
            for signature, matches in sorted(
                mapping.items(), key=lambda item: repr(item[0])
            )
        ]

    evidence_rows = [
        {
            "source": {
                "document_sha256": identity[0],
                "page": identity[1],
                "quote_sha256": identity[2],
            },
            "matches": [
                match.receipt() for match in _distinct_commitment_matches(matches)
            ],
        }
        for identity, matches in sorted(
            registry.historical_evidence.items(), key=lambda item: repr(item[0])
        )
    ]
    return {
        "registry_version": "event-admission-commitment-registry-v2",
        "open_facts": fact_rows(registry.open_facts),
        "historical_evidence": evidence_rows,
        "closed_facts": fact_rows(registry.closed_facts),
    }


def _commitment_signature_receipt(signature: CommitmentSignature) -> dict:
    (
        affected_external_org_id,
        stated_external_org_id,
        description,
        precision,
        timing_text,
        start_date,
        end_date,
    ) = signature
    return {
        "affected_external_org_id": affected_external_org_id,
        "stated_external_org_id": stated_external_org_id,
        "normalized_description": description,
        "timing": {
            "precision": precision,
            "text": timing_text,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
    }


def _unknown_scope_input_receipt(
    candidate: Candidate,
    *,
    policy_sha256: str,
    external_org_registry_sha256: str,
    commitment_registry_sha256: str | None = None,
    matching_commitments: tuple[CommitmentMatch, ...] = (),
) -> dict:
    """Exact deterministic inputs that make an unchanged Abstention idempotent."""
    receipt = {
        "receipt_version": "event-admission-unknown-scope-input-v3",
        "project_id": candidate.project_id,
        "candidate_id": candidate.id,
        "source_document_id": candidate.source_document_id,
        "active_extraction_run_id": candidate.extraction_run_id,
        "candidate_payload_sha256": policy.canonical_sha256(candidate.payload_json),
        "citations_verified": candidate.citations_verified,
        "candidate_state": candidate.state,
        "policy_sha256": policy_sha256,
        "external_org_registry_sha256": external_org_registry_sha256,
        "matching_commitments": [
            match.receipt() for match in matching_commitments
        ],
    }
    if commitment_registry_sha256 is not None:
        receipt["commitment_registry_sha256"] = commitment_registry_sha256
    return receipt


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
    from corridor import statement_lifecycle as statement_lifecycle_module
    from corridor import verify as verify_module

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
            "corridor.statement_lifecycle",
            Path(statement_lifecycle_module.__file__),
        ),
        ("corridor.verify", Path(verify_module.__file__)),
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


def acceptance_receipt_is_current(
    session: Session, receipt: EventAdmissionAcceptanceReceipt
) -> bool:
    """Require the runtime source and schema identities proved by the receipt."""
    return (
        receipt.source_revision == _current_source_revision()
        and receipt.migration_head == _current_migration_head(session)
    )


def normal_event_admission_policy_version(
    session: Session, project_id: int
) -> str:
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
        and acceptance_receipt_is_current(session, receipt)
        and receipt.policy_sha256
        == policy.canonical_sha256(
            canonical_event_admission_policy(
                project, UNKNOWN_SCOPE_POLICY_VERSION
            )
        )
    ):
        return UNKNOWN_SCOPE_POLICY_VERSION
    return EVENT_ADMISSION_POLICY_VERSION


def canonical_event_admission_policy(
    project: Project,
    policy_version: str,
    *,
    write_candidate_ids: Sequence[int] | None = None,
) -> dict:
    """The exact rules a real-state acceptance receipt proves.

    Three things move this digest, and each suspends the extension until a new
    passing receipt and append-only activation exist: the stated configuration,
    the project's project-side parties (they decide which events may carry a
    Commitment), and the deployed bytes of the deciding code.
    """
    unknown_scope = policy_version == UNKNOWN_SCOPE_POLICY_VERSION
    if not unknown_scope and policy_version != EVENT_ADMISSION_POLICY_VERSION:
        raise ValueError(f"unsupported Event Admission policy {policy_version!r}")
    receipt = {
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
                "no_matching_open_commitment",
                "no_historical_evidence_replay",
                "closed_facts_require_distinct_post_closure_evidence",
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
    if write_candidate_ids is not None:
        receipt["write_candidate_ids"] = sorted(write_candidate_ids)
    return receipt




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
    facts = prepare_candidate_statement_facts(session, candidate)
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
    if not facts.evidence_is_complete:
        raise StatementUnplaceable(
            "this statement's quote was not found on its page — check the "
            "page before placing it"
        )
    fields = facts.fields
    event_type = fields.get("event_type")
    if event_type not in ADMISSIBLE_EVENT_TYPES:
        raise StatementUnplaceable(
            f"{event_type!r} is not one of {', '.join(ADMISSIBLE_EVENT_TYPES)}"
        )
    raw_committed_date = fields.get("committed_date")
    new_timing = facts.new_timing.timing
    if new_timing is None:
        if facts.new_timing.invalid_reason == "invalid_calendar_bounds":
            raise StatementUnplaceable("a month timing has invalid calendar bounds")
        raise StatementUnplaceable("a statement must carry a date")
    raw_event_date = fields.get("event_date")
    event_date = facts.event_date
    if not facts.event_date_is_valid:
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
    evidence = facts.cited_evidence[0]
    previous_timing = facts.previous_timing.timing
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
    from corridor.adjudicate import require_candidate_action_scope

    try:
        require_candidate_action_scope(
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
