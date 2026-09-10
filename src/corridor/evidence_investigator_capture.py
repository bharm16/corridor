"""Delayed, cutoff-correct association of independent outcomes with frozen cases.

The one-time ``capture_shadow_outcome`` in ``evidence_investigator_shadow`` reads
current state at the instant a human happens to run it. That is wrong for a
scheduled regression measurement: a late run must associate the human outcome as
it stood at a *declared cutoff*, not as it stands when the scheduler finally
fires. Reconstructing "now" and backdating it would silently label execution-time
state as cutoff-time truth, so this module instead reads the outcome as of the
cutoff through ``evidence_investigator_human_outcome.read_human_outcome``, the
one reading both captures share with a time parameter, and, when the retained
history cannot support an exact answer, keeps an ``incomplete`` result with its
reason rather than inventing one. This module used to carry its own copy of that
reading, filtered by ``created_at <= cutoff``; the copy drifted from the one-time
capture's, so it was retired.

This module owns two records only: the declared gate-7 observation contract and
the per-case cutoff association. It never writes a human Project Record decision,
never releases packet or ranking protection, and never rewrites the frozen case,
its run, or the immutable one-time capture. Production timing, durable occurrence
identity, claims, and crash recovery belong to the one supervised Due Work
runtime (``due_work``), which drives the bounded pass here; this module adds no
timer, worker, or queue of its own. ADR-0024 governs the boundary the whole
design turns on: an acceptance capture verifies behavior and never mints run
lineage, so a captured association is bound to the frozen case's *own* execution
run and refuses to borrow a fresh v3 run to stand in for the frozen v2 cohort.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor.evidence_investigator_human_outcome import (
    HumanOutcomeUnreadable,
    read_human_outcome,
)
from corridor.evidence_investigator_runtime import sha256_json
from corridor.models import (
    EvidenceInvestigationCaptureContract,
    EvidenceInvestigationCaptureResult,
    EvidenceInvestigationShadowCase,
    EvidenceInvestigationShadowExecution,
    Project,
)


CAPTURE_RESULT_SCHEMA_VERSION = "evidence-outcome-capture-result-v1"
MISSING_LABEL_POLICY = "remain_missing"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CaptureContractRefusal(ValueError):
    """A capture observation contract is incomplete, unverifiable, or unsafe."""


@dataclass(frozen=True)
class CaptureObservationContract:
    """The domain half of a gate-7 capture declaration.

    The operational envelope (cadence, timezone, budgets, retention, missed-run,
    lease, and deadline) is validated separately by the Due Work schedule this
    contract is bound to. Here we pin exactly which frozen cases may be captured,
    the sealed configuration identities they must match, the baseline identity and
    its collection contract, the observation window and its intended cutoff, the
    protection end, the retained-history coverage the reconstruction may trust, the
    eligibility and exclusions, the required strata, and the missing-label policy.
    """

    project_id: int
    cohort_id: str
    model: str
    prompt_version: str
    prompt_sha256: str
    adapter_contract_version: str
    tool_contract_version: str
    validator_version: str
    baseline_identity: str
    baseline_collection_contract: str
    window_start: datetime
    cutoff_at: datetime
    protection_end: datetime
    history_retained_from: datetime
    eligibility: str
    missing_label_policy: str
    member_case_public_ids: tuple[str, ...]
    required_strata: tuple[str, ...] = ()
    exclusions: tuple[Mapping[str, str], ...] = ()
    declared_by: str = ""


@dataclass(frozen=True)
class ReconstructedOutcome:
    """One frozen case's outcome as of the cutoff, or why it is incomplete."""

    completeness: str
    incomplete_reason: str | None
    run_id: int | None
    candidate_disposition: str | None = None
    scope_mode: str | None = None
    selected_dependency_ids: tuple[int, ...] = ()
    correction: bool = False
    undo: bool = False
    unresolved: bool = False
    human_outcome_identity: str | None = None
    outcome_identities: Mapping[str, object] = field(default_factory=dict)
    strata: tuple[str, ...] = ()
    review_seconds: float | None = None


def declare_capture_contract(
    session: Session,
    declaration: CaptureObservationContract,
    *,
    now: datetime,
) -> EvidenceInvestigationCaptureContract:
    """Validate and retain one approved, content-addressed capture contract.

    Missing, unverifiable, or unapproved configuration raises and writes nothing,
    so the capture stays disabled. No retrospective default is invented: every
    identity, the window and cutoff, the protection end, and the retained-history
    coverage must be declared. Re-declaring the identical contract returns the
    existing row rather than minting a second identity.
    """

    now = _aware(now)
    if session.get(Project, declaration.project_id) is None:
        raise CaptureContractRefusal(
            f"capture contract project {declaration.project_id} does not exist"
        )
    _validate_declaration_shapes(declaration)

    member_public_ids = tuple(declaration.member_case_public_ids)
    if not member_public_ids or len(set(member_public_ids)) != len(member_public_ids):
        raise CaptureContractRefusal(
            "capture cohort membership must be a non-empty set of frozen case ids"
        )
    members: list[EvidenceInvestigationShadowCase] = []
    for public_id in member_public_ids:
        case = session.scalar(
            select(EvidenceInvestigationShadowCase).where(
                EvidenceInvestigationShadowCase.public_id == public_id
            )
        )
        if case is None:
            raise CaptureContractRefusal(
                f"frozen case {public_id} does not exist for capture"
            )
        if case.project_id != declaration.project_id:
            raise CaptureContractRefusal(
                f"frozen case {public_id} is outside the contract project"
            )
        if not _case_identity_matches(case, declaration):
            raise CaptureContractRefusal(
                f"frozen case {public_id} does not match the declared configuration "
                "identity; a fresh run may not satisfy the frozen cohort"
            )
        members.append(case)

    member_id_set = set(member_public_ids)
    for exclusion in declaration.exclusions:
        excluded_id = exclusion.get("shadow_case_public_id")
        if excluded_id not in member_id_set:
            raise CaptureContractRefusal(
                "an exclusion must name a declared cohort member"
            )
        if not str(exclusion.get("reason", "")).strip():
            raise CaptureContractRefusal("an exclusion must carry a reason")
        if not str(exclusion.get("excluded_by", "")).strip():
            raise CaptureContractRefusal(
                "an exclusion must name the person who declared it"
            )

    content = _contract_content(declaration, now=now)
    contract_sha256 = sha256_json(content)
    existing = session.scalar(
        select(EvidenceInvestigationCaptureContract).where(
            EvidenceInvestigationCaptureContract.contract_sha256 == contract_sha256
        )
    )
    if existing is not None:
        return existing

    contract = EvidenceInvestigationCaptureContract(
        public_id=str(uuid4()),
        project_id=declaration.project_id,
        cohort_id=declaration.cohort_id,
        contract_sha256=contract_sha256,
        model=declaration.model,
        prompt_version=declaration.prompt_version,
        prompt_sha256=declaration.prompt_sha256,
        adapter_contract_version=declaration.adapter_contract_version,
        tool_contract_version=declaration.tool_contract_version,
        validator_version=declaration.validator_version,
        baseline_identity=declaration.baseline_identity,
        window_start=_aware(declaration.window_start),
        cutoff_at=_aware(declaration.cutoff_at),
        protection_end=_aware(declaration.protection_end),
        history_retained_from=_aware(declaration.history_retained_from),
        missing_label_policy=declaration.missing_label_policy,
        member_case_public_ids_json=list(member_public_ids),
        contract_json=content,
        declared_by=declaration.declared_by,
    )
    session.add(contract)
    session.flush([contract])
    return contract


def reconstruct_cutoff_outcome(
    session: Session,
    shadow_case: EvidenceInvestigationShadowCase,
    contract: EvidenceInvestigationCaptureContract,
    *,
    executed_at: datetime,
) -> ReconstructedOutcome:
    """Reconstruct one frozen case's independent outcome as of the cutoff.

    The outcome is ``read_human_outcome`` at the cutoff. If the retained history
    does not provably cover the case, an identity cannot be verified, or the
    reading refuses, the result is incomplete with its reason and carries no
    fabricated disposition.
    """

    cutoff = _aware(contract.cutoff_at)
    run_id = session.scalar(
        select(EvidenceInvestigationShadowExecution.run_id).where(
            EvidenceInvestigationShadowExecution.shadow_case_id == shadow_case.id
        )
    )

    if shadow_case.project_id != contract.project_id:
        return _incomplete(run_id, "cross_project_or_missing_candidate")
    if not _case_identity_matches(shadow_case, contract):
        return _incomplete(run_id, "configuration_identity_mismatch")
    if _aware(contract.history_retained_from) > _aware(shadow_case.frozen_at):
        # The case was frozen before complete human history was retained, so an
        # earlier decision could have existed and not been kept. We cannot prove
        # the cutoff state, so we never claim it.
        return _incomplete(run_id, "case_predates_retained_history")

    try:
        read = read_human_outcome(session, shadow_case, as_of=cutoff)
    except HumanOutcomeUnreadable as refusal:
        return _incomplete(run_id, refusal.reason)
    identities = {
        "shadow_case_id": shadow_case.id,
        "run_id": run_id,
        "candidate_id": read.candidate_id,
        "candidate_disposition_id": read.candidate_disposition_id,
        "statement_coordination_receipt_id": read.statement_coordination_receipt_id,
        "scope_decision_id": read.scope_decision_id,
        "reversal_ids": list(read.reversal_ids),
    }
    return ReconstructedOutcome(
        completeness="complete",
        incomplete_reason=None,
        run_id=run_id,
        candidate_disposition=read.candidate_disposition,
        scope_mode=read.scope_mode,
        selected_dependency_ids=read.selected_dependency_ids,
        correction=read.correction,
        undo=read.undo,
        unresolved=read.unresolved,
        human_outcome_identity=read.human_outcome_identity,
        outcome_identities=identities,
        strata=read.strata,
        review_seconds=read.review_seconds,
    )


def run_outcome_capture(
    session_factory,
    *,
    project_id: int,
    contract_sha256: str,
    configuration_version: str,
    clock,
) -> dict[str, object]:
    """Run one bounded, idempotent, cutoff-correct capture pass over a cohort.

    Called only by the Due Work effectful handler for a claimed occurrence. It
    commits its association records durably so the runtime's finalize is safe to
    recover: a repeated tick or a competing worker converges on the same one
    association per case. A case whose cutoff has not arrived is left pending,
    never captured; capturing releases no protection.
    """

    executed_at = _aware(clock.now())
    with session_factory() as session:
        with session.begin():
            contract = session.scalar(
                select(EvidenceInvestigationCaptureContract).where(
                    EvidenceInvestigationCaptureContract.project_id == project_id,
                    EvidenceInvestigationCaptureContract.contract_sha256
                    == contract_sha256,
                )
            )
            if contract is None:
                raise CaptureContractRefusal(
                    "capture occurrence names no approved contract"
                )
            cutoff = _aware(contract.cutoff_at)
            excluded = {
                str(item.get("shadow_case_public_id"))
                for item in (contract.contract_json.get("exclusions") or [])
            }
            members = session.scalars(
                select(EvidenceInvestigationShadowCase)
                .where(
                    EvidenceInvestigationShadowCase.public_id.in_(
                        contract.member_case_public_ids_json
                    )
                )
                .order_by(EvidenceInvestigationShadowCase.id)
            ).all()

            summary = {
                "cases_in_scope": 0,
                "captured_complete": 0,
                "captured_incomplete": 0,
                "before_cutoff_pending": 0,
                "excluded": 0,
                "already_captured": 0,
            }
            for case in members:
                summary["cases_in_scope"] += 1
                if case.public_id in excluded:
                    summary["excluded"] += 1
                    continue
                if executed_at < cutoff:
                    summary["before_cutoff_pending"] += 1
                    continue
                already = session.scalar(
                    select(EvidenceInvestigationCaptureResult.id).where(
                        EvidenceInvestigationCaptureResult.capture_contract_id
                        == contract.id,
                        EvidenceInvestigationCaptureResult.shadow_case_id == case.id,
                    )
                )
                if already is not None:
                    summary["already_captured"] += 1
                    continue
                outcome = reconstruct_cutoff_outcome(
                    session, case, contract, executed_at=executed_at
                )
                outcome = _refuse_duplicate_human_outcome(session, contract, outcome)
                written = _write_result(
                    session, contract, case, outcome, executed_at=executed_at
                )
                if written and outcome.completeness == "complete":
                    summary["captured_complete"] += 1
                elif written:
                    summary["captured_incomplete"] += 1
                else:
                    summary["already_captured"] += 1

            health = (
                "capture_attention_required"
                if summary["captured_incomplete"] > 0
                else "healthy"
            )
            return {
                "schema_version": CAPTURE_RESULT_SCHEMA_VERSION,
                "project_id": project_id,
                "configuration_version": configuration_version,
                "contract_sha256": contract_sha256,
                "cutoff_at": _iso(cutoff),
                "observed_at": _iso(executed_at),
                "health": health,
                **summary,
            }


def capture_status(
    session: Session,
    *,
    project_id: int | None = None,
    contract_sha256: str | None = None,
) -> dict[str, object]:
    """Read back contracts and cutoff associations for operations and review.

    The readback keeps the intended cutoff and the actual execution time
    distinct, and it never restates an incomplete association as a success.
    """

    contract_query = select(EvidenceInvestigationCaptureContract)
    if project_id is not None:
        contract_query = contract_query.where(
            EvidenceInvestigationCaptureContract.project_id == project_id
        )
    if contract_sha256 is not None:
        contract_query = contract_query.where(
            EvidenceInvestigationCaptureContract.contract_sha256 == contract_sha256
        )
    contracts = session.scalars(
        contract_query.order_by(EvidenceInvestigationCaptureContract.id)
    ).all()
    contract_ids = [contract.id for contract in contracts]
    results = (
        session.scalars(
            select(EvidenceInvestigationCaptureResult)
            .where(
                EvidenceInvestigationCaptureResult.capture_contract_id.in_(contract_ids)
            )
            .order_by(EvidenceInvestigationCaptureResult.id)
        ).all()
        if contract_ids
        else []
    )
    return {
        "contracts": [
            {
                "contract_id": contract.public_id,
                "project_id": contract.project_id,
                "cohort_id": contract.cohort_id,
                "contract_sha256": contract.contract_sha256,
                "cutoff_at": _iso(contract.cutoff_at),
                "window_start": _iso(contract.window_start),
                "protection_end": _iso(contract.protection_end),
                "history_retained_from": _iso(contract.history_retained_from),
                "member_case_count": len(contract.member_case_public_ids_json),
            }
            for contract in contracts
        ],
        "results": [
            {
                "result_id": result.public_id,
                "contract_id": next(
                    contract.public_id
                    for contract in contracts
                    if contract.id == result.capture_contract_id
                ),
                "shadow_case_id": result.shadow_case_id,
                "run_id": result.run_id,
                "cutoff_at": _iso(result.cutoff_at),
                "executed_at": _iso(result.executed_at),
                "on_time": result.executed_at <= result.cutoff_at,
                "completeness": result.completeness,
                "incomplete_reason": result.incomplete_reason,
                "candidate_disposition": result.candidate_disposition,
                "scope_mode": result.scope_mode,
                "correction": result.correction,
                "undo": result.undo,
                "unresolved": result.unresolved,
                "strata": result.strata_json,
                "review_seconds": result.review_seconds,
                "protection_active": _aware(result.executed_at)
                < _aware(
                    next(
                        contract.protection_end
                        for contract in contracts
                        if contract.id == result.capture_contract_id
                    )
                ),
            }
            for result in results
        ],
    }


def _write_result(
    session: Session,
    contract: EvidenceInvestigationCaptureContract,
    case: EvidenceInvestigationShadowCase,
    outcome: ReconstructedOutcome,
    *,
    executed_at: datetime,
) -> bool:
    content = {
        "schema_version": CAPTURE_RESULT_SCHEMA_VERSION,
        "contract_sha256": contract.contract_sha256,
        "shadow_case_id": case.id,
        "run_id": outcome.run_id,
        "cutoff_at": _iso(contract.cutoff_at),
        "completeness": outcome.completeness,
        "incomplete_reason": outcome.incomplete_reason,
        "candidate_disposition": outcome.candidate_disposition,
        "scope_mode": outcome.scope_mode,
        "selected_dependency_ids": list(outcome.selected_dependency_ids),
        "correction": outcome.correction,
        "undo": outcome.undo,
        "unresolved": outcome.unresolved,
        "human_outcome_identity": outcome.human_outcome_identity,
        "outcome_identities": dict(outcome.outcome_identities),
        "strata": list(outcome.strata),
        "review_seconds": outcome.review_seconds,
    }
    association_sha256 = sha256_json(content)
    statement = (
        insert(EvidenceInvestigationCaptureResult)
        .values(
            public_id=str(uuid4()),
            capture_contract_id=contract.id,
            shadow_case_id=case.id,
            project_id=contract.project_id,
            candidate_id=case.candidate_id,
            run_id=outcome.run_id,
            cutoff_at=_aware(contract.cutoff_at),
            executed_at=executed_at,
            completeness=outcome.completeness,
            incomplete_reason=outcome.incomplete_reason,
            candidate_disposition=outcome.candidate_disposition,
            scope_mode=outcome.scope_mode,
            selected_dependency_ids_json=list(outcome.selected_dependency_ids),
            correction=outcome.correction,
            undo=outcome.undo,
            unresolved=outcome.unresolved,
            human_outcome_identity=outcome.human_outcome_identity,
            outcome_identities_json=dict(outcome.outcome_identities),
            strata_json=list(outcome.strata),
            review_seconds=outcome.review_seconds,
            association_sha256=association_sha256,
        )
        .on_conflict_do_nothing(
            index_elements=["capture_contract_id", "shadow_case_id"]
        )
        .returning(EvidenceInvestigationCaptureResult.id)
    )
    # RETURNING distinguishes an inserted row from one a competing worker already
    # wrote (ON CONFLICT DO NOTHING yields no row), which ``rowcount`` cannot do
    # reliably across drivers.
    return session.execute(statement).scalar() is not None


def _refuse_duplicate_human_outcome(
    session: Session,
    contract: EvidenceInvestigationCaptureContract,
    outcome: ReconstructedOutcome,
) -> ReconstructedOutcome:
    if outcome.completeness != "complete" or outcome.human_outcome_identity is None:
        return outcome
    duplicate = session.scalar(
        select(EvidenceInvestigationCaptureResult.id).where(
            EvidenceInvestigationCaptureResult.capture_contract_id == contract.id,
            EvidenceInvestigationCaptureResult.completeness == "complete",
            EvidenceInvestigationCaptureResult.human_outcome_identity
            == outcome.human_outcome_identity,
        )
    )
    if duplicate is None:
        return outcome
    return _incomplete(outcome.run_id, "human_outcome_claimed_by_another_case")


def _case_identity_matches(
    case: EvidenceInvestigationShadowCase,
    declaration: CaptureObservationContract | EvidenceInvestigationCaptureContract,
) -> bool:
    return (
        case.model == declaration.model
        and case.prompt_version == declaration.prompt_version
        and case.prompt_sha256 == declaration.prompt_sha256
        and case.adapter_contract_version == declaration.adapter_contract_version
        and case.tool_contract_version == declaration.tool_contract_version
    )


def _validate_declaration_shapes(declaration: CaptureObservationContract) -> None:
    if declaration.missing_label_policy != MISSING_LABEL_POLICY:
        raise CaptureContractRefusal(
            "capture missing-label policy must keep missing labels missing"
        )
    for label, value in (
        ("cohort_id", declaration.cohort_id),
        ("model", declaration.model),
        ("prompt_version", declaration.prompt_version),
        ("adapter_contract_version", declaration.adapter_contract_version),
        ("tool_contract_version", declaration.tool_contract_version),
        ("validator_version", declaration.validator_version),
        ("baseline_identity", declaration.baseline_identity),
        ("baseline_collection_contract", declaration.baseline_collection_contract),
        ("eligibility", declaration.eligibility),
        ("declared_by", declaration.declared_by),
    ):
        if not _IDENTITY.fullmatch(value or ""):
            raise CaptureContractRefusal(
                f"capture contract {label} identity is missing or malformed"
            )
    if not _SHA256.fullmatch(declaration.prompt_sha256 or ""):
        raise CaptureContractRefusal("capture contract prompt digest must be SHA-256")
    window_start = _aware(declaration.window_start)
    cutoff = _aware(declaration.cutoff_at)
    if window_start > cutoff:
        raise CaptureContractRefusal(
            "capture observation window must start on or before its cutoff"
        )
    # Touch the remaining declared instants so a naive-datetime slip is refused
    # at declaration time rather than surfacing later as a reconstruction error.
    _aware(declaration.protection_end)
    _aware(declaration.history_retained_from)
    if not declaration.required_strata == tuple(
        dict.fromkeys(declaration.required_strata)
    ):
        raise CaptureContractRefusal("required strata must be declared without repeats")


def _contract_content(
    declaration: CaptureObservationContract, *, now: datetime
) -> dict[str, object]:
    return {
        "schema_version": "evidence-outcome-capture-contract-v1",
        "project_id": declaration.project_id,
        "cohort_id": declaration.cohort_id,
        "configuration_identity": {
            "model": declaration.model,
            "prompt_version": declaration.prompt_version,
            "prompt_sha256": declaration.prompt_sha256,
            "adapter_contract_version": declaration.adapter_contract_version,
            "tool_contract_version": declaration.tool_contract_version,
            "validator_version": declaration.validator_version,
        },
        "baseline": {
            "identity": declaration.baseline_identity,
            "collection_contract": declaration.baseline_collection_contract,
        },
        "observation_window": {
            "window_start": _iso(declaration.window_start),
            "cutoff_at": _iso(declaration.cutoff_at),
            "history_retained_from": _iso(declaration.history_retained_from),
        },
        "protection_end": _iso(declaration.protection_end),
        "eligibility": declaration.eligibility,
        "missing_label_policy": declaration.missing_label_policy,
        "required_strata": list(declaration.required_strata),
        "member_case_public_ids": list(declaration.member_case_public_ids),
        "exclusions": [
            {
                "shadow_case_public_id": item.get("shadow_case_public_id"),
                "reason": item.get("reason"),
                "excluded_by": item.get("excluded_by"),
            }
            for item in declaration.exclusions
        ],
        "declared_by": declaration.declared_by,
    }


def _incomplete(run_id: int | None, reason: str) -> ReconstructedOutcome:
    return ReconstructedOutcome(
        completeness="incomplete",
        incomplete_reason=reason,
        run_id=run_id,
        selected_dependency_ids=(),
        outcome_identities={"run_id": run_id, "incomplete_reason": reason},
        strata=(),
    )


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureContractRefusal("capture timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()
