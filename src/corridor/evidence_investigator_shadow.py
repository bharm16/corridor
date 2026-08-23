"""Prospective hidden shadow cohort and independent human outcomes.

Cases are frozen before the runtime sees them. Later coordinator decisions are
associated by exact Candidate identity and never copied into the original case
or packet.

Replay was rejected as the default because later adjudicated rows leak answers
into model input. This module therefore freezes current state prospectively and
keeps immutable case, execution, review, and outcome identities separate.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.evidence_investigator import (
    TOOL_CONTRACT_VERSION,
    InvestigationAbstention,
    InvestigationBudget,
    InvestigationRuntime,
    prepare_investigation,
)
from corridor.evidence_investigator_runtime import (
    ReceiptedInvestigation,
    RuntimeIdentity,
    configured_runtime_identity,
    sha256_json,
    run_receipted_investigation,
)
from corridor.models import (
    Candidate,
    CandidateDisposition,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventScopeDecision,
    EvidenceInvestigationPacketReceipt,
    EvidenceInvestigationCandidateReviewStart,
    EvidenceInvestigationReviewObservation,
    EvidenceInvestigationShadowCase,
    EvidenceInvestigationShadowExecution,
    EvidenceInvestigationShadowOutcome,
    Project,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.statement_lifecycle import (
    current_candidate_disposition,
    current_lineage_statement,
)
from corridor.work_list import build_work_list


@dataclass(frozen=True)
class ShadowExecutionResult:
    case: EvidenceInvestigationShadowCase
    execution: EvidenceInvestigationShadowExecution
    investigation: ReceiptedInvestigation


@dataclass(frozen=True)
class V2ShadowCohortManifest:
    """Sealed identity of one prospective dataset; never model or Ledger input."""

    schema_version: str
    cohort_id: str
    project: dict
    selected_at: str
    selection_rule: str
    candidate_ids: tuple[int, ...]
    model: str
    prompt_version: str
    prompt_sha256: str
    adapter_contract_version: str
    tool_contract_version: str
    transport_gate_sha256: str
    budget: dict
    active_run_ids: tuple[int, ...]
    read_fingerprints: tuple[str, ...]
    dataset_membership: tuple[dict, ...]
    execution_summary: dict
    manifest_sha256: str

    def __post_init__(self) -> None:
        size = len(self.candidate_ids)
        if self.schema_version != "evidence-investigator-v2-shadow-cohort-v2":
            raise ValueError("unknown v2 shadow cohort manifest schema")
        if not size or len(set(self.candidate_ids)) != size:
            raise ValueError("cohort Candidate identities must be non-empty and unique")
        if not all(
            len(values) == size
            for values in (
                self.active_run_ids,
                self.read_fingerprints,
                self.dataset_membership,
            )
        ):
            raise ValueError("cohort manifest identity lists must have equal membership")
        if any(
            len(value) != 64
            for value in (
                self.prompt_sha256,
                self.transport_gate_sha256,
                self.manifest_sha256,
            )
        ):
            raise ValueError("cohort configuration and receipt hashes must be SHA-256")


@dataclass(frozen=True)
class V2ShadowCohort:
    """One exact hidden prospective dataset and its immutable local manifest."""

    manifest: V2ShadowCohortManifest
    results: tuple[ShadowExecutionResult, ...]


async def run_shadow_batch(
    session: Session,
    project_id: int,
    *,
    runtime_factory: Callable[[], InvestigationRuntime],
    identity: RuntimeIdentity,
    budget: InvestigationBudget,
    limit: int = 25,
    candidate_ids: tuple[int, ...] | None = None,
) -> tuple[ShadowExecutionResult, ...]:
    """Freeze and invisibly run current unplaced Candidate Work Items."""
    work = build_work_list(session, project_id)
    eligible_candidate_ids: list[int] = []
    for item in (*work.immediate, *work.candidate_backlog):
        if (
            item.kind == "candidate"
            and item.candidate_id is not None
            and "unplaced_statement" in item.attention_reason_codes
            and item.candidate_id not in eligible_candidate_ids
        ):
            eligible_candidate_ids.append(item.candidate_id)
        if candidate_ids is None and len(eligible_candidate_ids) == limit:
            break
    if candidate_ids is None:
        selected_candidate_ids = eligible_candidate_ids
    else:
        if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("explicit shadow Candidate ids must be non-empty and unique")
        if set(candidate_ids) - set(eligible_candidate_ids):
            raise ValueError(
                "every selected Candidate must be a current Unplaced Statement "
                "Work Item in the bound project"
            )
        selected_candidate_ids = list(candidate_ids)
    results: list[ShadowExecutionResult] = []
    for candidate_id in selected_candidate_ids:
        prepared = prepare_investigation(session, candidate_id)
        if isinstance(prepared, InvestigationAbstention):
            continue
        candidate = session.get(Candidate, candidate_id)
        assert candidate is not None
        existing = session.scalar(
            select(EvidenceInvestigationShadowCase).where(
                EvidenceInvestigationShadowCase.candidate_id == candidate_id,
                EvidenceInvestigationShadowCase.read_fingerprint
                == prepared.case.read_fingerprint,
                EvidenceInvestigationShadowCase.model == identity.model,
                EvidenceInvestigationShadowCase.prompt_version
                == identity.prompt_version,
                EvidenceInvestigationShadowCase.prompt_sha256
                == identity.prompt_sha256,
                EvidenceInvestigationShadowCase.adapter_contract_version
                == identity.adapter_contract_version,
                EvidenceInvestigationShadowCase.tool_contract_version
                == TOOL_CONTRACT_VERSION,
            )
        )
        if existing is not None:
            continue
        option_population = {
            "project_id": project_id,
            "party_ids": sorted(prepared.bound.party_ref_by_id),
            "dependency_ids": sorted(prepared.bound.dependency_ref_by_id),
            "statement_ids": sorted(prepared.bound.statement_ref_by_id),
            "party_refs": {
                reference: party_id
                for party_id, reference in sorted(prepared.bound.party_ref_by_id.items())
            },
            "dependency_refs": {
                reference: dependency_id
                for dependency_id, reference in sorted(
                    prepared.bound.dependency_ref_by_id.items()
                )
            },
        }
        evidence = [
            {
                "evidence_ref": reference,
                "document_id": binding.document_id,
                "page_no": binding.page_no,
                "quote_sha256": hashlib.sha256(binding.candidate_quote.encode()).hexdigest(),
            }
            for reference, binding in sorted(prepared.bound.evidence_by_ref.items())
        ]
        frozen_at = datetime.now(timezone.utc)
        shadow_case = EvidenceInvestigationShadowCase(
            public_id=str(uuid.uuid4()),
            project_id=project_id,
            candidate_id=candidate_id,
            extraction_run_id=candidate.extraction_run_id,
            candidate_payload_sha256=sha256_json(candidate.payload_json),
            read_fingerprint=prepared.case.read_fingerprint,
            model=identity.model,
            prompt_version=identity.prompt_version,
            prompt_sha256=identity.prompt_sha256,
            adapter_contract_version=identity.adapter_contract_version,
            tool_contract_version=TOOL_CONTRACT_VERSION,
            transport_gate_sha256=identity.transport_gate_sha256,
            budget_json=asdict(budget),
            case_json=asdict(prepared.case),
            registered_evidence_json=evidence,
            option_population_json=option_population,
            option_population_sha256=sha256_json(option_population),
            frozen_at=frozen_at,
        )
        session.add(shadow_case)
        session.flush([shadow_case])
        investigation = await run_receipted_investigation(
            session,
            candidate_id,
            runtime=runtime_factory(),
            identity=identity,
            budget=budget,
            prepared=prepared,
        )
        status = investigation.run.terminal_status
        if (
            investigation.run.reason == "stale_input"
            or investigation.run.read_fingerprint != shadow_case.read_fingerprint
        ):
            status = "stale"
        execution = EvidenceInvestigationShadowExecution(
            shadow_case_id=shadow_case.id,
            run_id=investigation.run.id,
            execution_status=status,
        )
        session.add(execution)
        session.flush([execution])
        results.append(ShadowExecutionResult(shadow_case, execution, investigation))
    return tuple(results)


async def run_v2_shadow_cohort(
    session: Session,
    project_id: int,
    *,
    candidate_ids: tuple[int, ...],
    selection_rule: str,
    runtime_factory: Callable[[], InvestigationRuntime],
    identity: RuntimeIdentity,
    budget: InvestigationBudget,
) -> V2ShadowCohort:
    """Freeze and execute one explicit, configuration-verifiable v2 cohort."""
    if not selection_rule.startswith("operator-declared:"):
        raise ValueError("the v2 cohort requires an operator-declared selection rule")
    if identity != configured_runtime_identity(identity.model):
        raise ValueError("the cohort must use the sealed Evidence Investigator v2")
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError("shadow cohort project does not exist")
    prior_case_ids = select(EvidenceInvestigationShadowCase.id).where(
        EvidenceInvestigationShadowCase.candidate_id.in_(candidate_ids)
    )
    prior_outcome = session.scalar(
        select(EvidenceInvestigationShadowOutcome.id)
        .where(EvidenceInvestigationShadowOutcome.shadow_case_id.in_(prior_case_ids))
        .limit(1)
    )
    prior_review = session.scalar(
        select(EvidenceInvestigationReviewObservation.id)
        .where(
            EvidenceInvestigationReviewObservation.shadow_case_id.in_(prior_case_ids)
        )
        .limit(1)
    )
    candidate_review = session.scalar(
        select(EvidenceInvestigationCandidateReviewStart.id)
        .where(EvidenceInvestigationCandidateReviewStart.candidate_id.in_(candidate_ids))
        .limit(1)
    )
    if (
        prior_outcome is not None
        or prior_review is not None
        or candidate_review is not None
    ):
        raise ValueError(
            "a prospective v2 case must be frozen before any prior human review "
            "or outcome exists"
        )
    selected_at = datetime.now(timezone.utc)
    results = await run_shadow_batch(
        session,
        project_id,
        runtime_factory=runtime_factory,
        identity=identity,
        budget=budget,
        limit=len(candidate_ids),
        candidate_ids=candidate_ids,
    )
    if tuple(item.case.candidate_id for item in results) != candidate_ids:
        raise ValueError(
            "one or more selected frozen cases already exists or could not execute"
        )
    summary: dict[str, int] = {}
    semantic_reasons = {
        "insufficient_evidence",
        "ambiguous_scope",
        "ambiguous_party",
    }
    membership = []
    for item in results:
        reason = item.investigation.run.reason
        if item.investigation.result.packet is not None:
            category = "validated_packet"
        elif reason in semantic_reasons:
            category = "semantic_abstention"
        else:
            category = "harness_failure"
        summary[category] = summary.get(category, 0) + 1
        membership.append(
            {
                "shadow_case_id": item.case.public_id,
                "run_id": item.investigation.run.public_id,
                "candidate_id": item.case.candidate_id,
                "category": category,
                "terminal_status": item.investigation.run.terminal_status,
                "reason": reason,
                "hidden": True,
                "non_authoritative": True,
            }
        )
    content = {
        "schema_version": "evidence-investigator-v2-shadow-cohort-v2",
        "cohort_id": str(uuid.uuid4()),
        "project": {"id": project.id, "slug": project.slug},
        "selected_at": selected_at.isoformat(),
        "selection_rule": selection_rule,
        "candidate_ids": candidate_ids,
        "model": identity.model,
        "prompt_version": identity.prompt_version,
        "prompt_sha256": identity.prompt_sha256,
        "adapter_contract_version": identity.adapter_contract_version,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "transport_gate_sha256": identity.transport_gate_sha256,
        "budget": asdict(budget),
        "active_run_ids": tuple(item.case.extraction_run_id for item in results),
        "read_fingerprints": tuple(item.case.read_fingerprint for item in results),
        "dataset_membership": tuple(membership),
        "execution_summary": summary,
    }
    return V2ShadowCohort(
        manifest=V2ShadowCohortManifest(
            **content, manifest_sha256=sha256_json(content)
        ),
        results=results,
    )


def write_shadow_cohort_manifest(cohort: V2ShadowCohort, path: Path) -> None:
    """Create one local manifest without overwriting an earlier cohort receipt."""
    manifest = asdict(cohort.manifest)
    content = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if sha256_json(content) != cohort.manifest.manifest_sha256:
        raise ValueError("shadow cohort manifest changed after it was sealed")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")


def observe_shadow_review(
    session: Session,
    candidate_id: int,
    *,
    boundary: str,
    principal: HumanPrincipal,
) -> EvidenceInvestigationReviewObservation | EvidenceInvestigationCandidateReviewStart | None:
    """Append one server-observed human review boundary, if shadowed."""
    if boundary not in {"start", "end"}:
        raise ValueError("review boundary must be start or end")
    actor = require_human_principal(principal)
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise ValueError("reviewed Candidate does not exist")
    candidate_start = None
    if boundary == "start":
        candidate_start = session.scalar(
            select(EvidenceInvestigationCandidateReviewStart).where(
                EvidenceInvestigationCandidateReviewStart.candidate_id == candidate_id
            )
        )
        if candidate_start is None:
            candidate_start = EvidenceInvestigationCandidateReviewStart(
                project_id=candidate.project_id,
                candidate_id=candidate.id,
                principal=actor.subject,
                observed_at=datetime.now(timezone.utc),
            )
            session.add(candidate_start)
            session.flush([candidate_start])
    shadow_case = session.scalar(
        select(EvidenceInvestigationShadowCase)
        .where(EvidenceInvestigationShadowCase.candidate_id == candidate_id)
        .order_by(EvidenceInvestigationShadowCase.id.desc())
        .limit(1)
    )
    if shadow_case is None:
        return candidate_start
    existing = session.scalar(
        select(EvidenceInvestigationReviewObservation).where(
            EvidenceInvestigationReviewObservation.shadow_case_id == shadow_case.id,
            EvidenceInvestigationReviewObservation.boundary == boundary,
        )
    )
    if existing is not None:
        return existing
    observation = EvidenceInvestigationReviewObservation(
        shadow_case_id=shadow_case.id,
        boundary=boundary,
        principal=actor.subject,
        observed_at=datetime.now(timezone.utc),
    )
    session.add(observation)
    session.flush([observation])
    return observation


def capture_shadow_outcome(
    session: Session, shadow_public_id: str
) -> EvidenceInvestigationShadowOutcome:
    """Associate current independent human facts with one frozen case once."""
    shadow_case = session.scalar(
        select(EvidenceInvestigationShadowCase).where(
            EvidenceInvestigationShadowCase.public_id == shadow_public_id
        )
    )
    if shadow_case is None:
        raise ValueError("shadow case does not exist")
    existing = session.scalar(
        select(EvidenceInvestigationShadowOutcome).where(
            EvidenceInvestigationShadowOutcome.shadow_case_id == shadow_case.id
        )
    )
    if existing is not None:
        return existing
    candidate = session.get(Candidate, shadow_case.candidate_id)
    if candidate is None or candidate.project_id != shadow_case.project_id:
        raise ValueError("shadow Candidate is outside its frozen project")
    disposition = current_candidate_disposition(session, candidate.id)
    any_disposition = session.scalar(
        select(CandidateDisposition)
        .where(CandidateDisposition.candidate_id == candidate.id)
        .order_by(CandidateDisposition.id.desc())
        .limit(1)
    )
    receipt = None
    scope = None
    selected_dependency_ids: list[int] = []
    correction = False
    if disposition is not None and disposition.disposition == "accepted":
        receipt = session.scalar(
            select(StatementCoordinationReceipt).where(
                StatementCoordinationReceipt.candidate_disposition_id == disposition.id
            )
        )
        if receipt is None:
            raise ValueError("accepted shadow outcome has no grouped Save receipt")
        event = session.get(DependencyEvent, receipt.dependency_event_id)
        scope = session.get(DependencyEventScopeDecision, receipt.scope_decision_id)
        if event is None or scope is None or event.project_id != shadow_case.project_id:
            raise ValueError("shadow outcome crosses its frozen project")
        selected_dependency_ids = list(
            session.scalars(
                select(DependencyEventScope.dependency_id)
                .where(DependencyEventScope.scope_decision_id == scope.id)
                .order_by(DependencyEventScope.dependency_id)
            ).all()
        )
        current = current_lineage_statement(session, receipt.commitment_lineage_id)
        correction = current is not None and current.id != receipt.dependency_event_id
    reversal_ids = list(
        session.scalars(
            select(StatementCoordinationReversal.id).where(
                StatementCoordinationReversal.candidate_id == candidate.id
            )
        ).all()
    )
    undo = bool(reversal_ids)
    unresolved = disposition is None
    observations = {
        item.boundary: item
        for item in session.scalars(
            select(EvidenceInvestigationReviewObservation).where(
                EvidenceInvestigationReviewObservation.shadow_case_id == shadow_case.id
            )
        ).all()
    }
    review_seconds = None
    if "start" in observations and "end" in observations:
        elapsed = (
            observations["end"].observed_at - observations["start"].observed_at
        ).total_seconds()
        if 0 <= elapsed <= 14_400:
            review_seconds = elapsed
    strata: set[str] = set()
    if disposition is not None and disposition.disposition == "not_relevant":
        strata.add("not_relevant")
    if scope is not None:
        if scope.scope_mode == "unknown":
            strata.add("unknown_scope")
        elif len(selected_dependency_ids) == 1:
            strata.add("single_dependency")
        elif len(selected_dependency_ids) > 1:
            strata.add("multiple_dependencies")
    if correction:
        strata.add("later_corrected")
    if unresolved:
        strata.add("unresolved")
    packet = session.scalar(
        select(EvidenceInvestigationPacketReceipt)
        .join(
            EvidenceInvestigationShadowExecution,
            EvidenceInvestigationShadowExecution.run_id
            == EvidenceInvestigationPacketReceipt.run_id,
        )
        .where(EvidenceInvestigationShadowExecution.shadow_case_id == shadow_case.id)
    )
    if packet is not None:
        if len(packet.packet_json.get("possible_parties") or []) > 1:
            strata.add("party_ambiguity")
        questions = " ".join(packet.packet_json.get("human_questions") or []).casefold()
        if "timing" in questions or "date" in questions:
            strata.add("timing_ambiguity")
    identities = {
        "candidate_disposition_id": disposition.id if disposition else None,
        "last_candidate_disposition_id": any_disposition.id if any_disposition else None,
        "statement_coordination_receipt_id": receipt.id if receipt else None,
        "scope_decision_id": scope.id if scope else None,
        "reversal_ids": reversal_ids,
    }
    human_outcome_identity = sha256_json(
        {
            "candidate_id": candidate.id,
            "candidate_disposition_id": identities["candidate_disposition_id"],
            "statement_coordination_receipt_id": identities[
                "statement_coordination_receipt_id"
            ],
            "reversal_ids": reversal_ids,
            "unresolved": unresolved,
        }
    )
    duplicate = session.scalar(
        select(EvidenceInvestigationShadowOutcome).where(
            EvidenceInvestigationShadowOutcome.human_outcome_identity
            == human_outcome_identity
        )
    )
    if duplicate is not None and duplicate.shadow_case_id != shadow_case.id:
        raise ValueError("human outcome already captured for another shadow case")
    duplicate = session.scalar(
        select(EvidenceInvestigationShadowOutcome)
        .join(
            EvidenceInvestigationShadowCase,
            EvidenceInvestigationShadowCase.id
            == EvidenceInvestigationShadowOutcome.shadow_case_id,
        )
        .where(
            EvidenceInvestigationShadowCase.candidate_id == candidate.id,
            EvidenceInvestigationShadowOutcome.shadow_case_id != shadow_case.id,
            EvidenceInvestigationShadowOutcome.outcome_identities_json == identities,
        )
    )
    if duplicate is not None:
        raise ValueError(
            "human outcome already captured for another shadow case on this Candidate"
        )
    content = {
        "candidate_disposition": disposition.disposition if disposition else None,
        "scope_mode": scope.scope_mode if scope else None,
        "selected_dependency_ids": selected_dependency_ids,
        "correction": correction,
        "undo": undo,
        "unresolved": unresolved,
        "outcome_identities": identities,
        "strata": sorted(strata),
        "review_seconds": review_seconds,
    }
    outcome = EvidenceInvestigationShadowOutcome(
        shadow_case_id=shadow_case.id,
        human_outcome_identity=human_outcome_identity,
        candidate_disposition=content["candidate_disposition"],
        scope_mode=content["scope_mode"],
        selected_dependency_ids_json=selected_dependency_ids,
        correction=correction,
        undo=undo,
        unresolved=unresolved,
        outcome_identities_json=identities,
        strata_json=sorted(strata),
        review_seconds=review_seconds,
        outcome_sha256=sha256_json(content),
        captured_at=datetime.now(timezone.utc),
    )
    session.add(outcome)
    session.flush([outcome])
    return outcome
