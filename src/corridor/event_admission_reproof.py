"""Restore a stale unknown-scope Event Admission class through scheduled re-proof.

ADR-0050 shrank ticket #358 to one thing: when a *previously proved* deterministic
Record Inclusion class goes stale — its rule fingerprint or its schema changed, never
a redeploy alone — replay the policy's own recorded history and surface the honest
outcome. This module owns that recovery decision and orchestration; the shared
supervised runtime (``corridor.due_work``) owns the occurrence, claim, retry, and
receipt lifecycle and calls in here from one bounded effectful handler.

Two invariants shape the code:

* Staleness is read from the *same* effective-policy selection ordinary processing
  uses (``read_event_admission_policy_status``). ``proof_status`` is
  ``"stale_bound_identities"`` exactly when the newest proof is a passing proof whose
  bound source revision, migration head, or policy fingerprint no longer match what
  is deployed. A never-proved class (``no_applicable_proof``) is therefore never
  recovered here — first-activation authority stays with the explicit operator path.
* The replay itself is ``run_event_admission_acceptance``: it copies the database,
  erases only the rule's own signed answers, re-runs the new rule version on the same
  cases, records one immutable pass-or-fail receipt, and reactivates only through the
  suspension-safe interface. Recovery reuses it unchanged rather than re-deriving proof.

Provisioning disposable clones and checking out a clean revision is operational work
that a deployment wires explicitly and a human gates; it is intentionally deferred
behind :func:`set_live_reproof_replay`. Until it is wired, a stale class produces an
honest ``recovery_attention_required`` result — the predecessor rules stay effective
as the safe fallback and nothing is silently reactivated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.event_admission import (
    UNKNOWN_SCOPE_POLICY_VERSION,
    read_event_admission_policy_status,
)
from corridor.due_work_contract import (
    DECLARED_IDENTITY,
    DueWorkRefusal,
    DueWorkScheduling,
    HandlerRegistration,
    ResolvedSchedule,
    ValidatedDeclaration,
    gate7_configuration,
    validate_scheduling,
)
from corridor.event_admission_acceptance import EventAdmissionAcceptanceResult
from corridor.models import Project

RESULT_SCHEMA_VERSION = "event-admission-reproof-result-v1"

# The one server-owned handler key this module's work runs under. It matches
# ``due_work.HANDLER_EVENT_ADMISSION_REPROOF``; the constant lives here because
# this module is the lower layer and the runtime imports its declaration and
# execution, never the reverse.
HANDLER_KEY = "event_admission_reproof"
HEALTH_OK = "healthy"
HEALTH_ATTENTION = "recovery_attention_required"

# The one proof state that means "a class we already proved has gone stale". Every
# other state is either fine (current / suspended / never-proved) or a durable
# failure a human inspects — none of them is re-run automatically, so an unchanged
# redeployment and a standing failure both settle without an unbounded retry loop.
STALE_PROOF_STATUS = "stale_bound_identities"


@dataclass(frozen=True)
class ReproofRequest:
    """What a live replay executor needs to re-prove one project's stale class.

    It carries only durable identities — the project, the bound policy/reason and the
    approved replay population (selection rule). Operational secrets (the source and
    admin database URLs, the clean revision to check out) are supplied by the wired
    executor from its own deployment configuration, never persisted on the append-only
    schedule and never handled here.
    """

    project_id: int
    project_slug: str
    policy_version: str
    reason_version: str
    selection_rule: str


ReproofReplay = Callable[[ReproofRequest], EventAdmissionAcceptanceResult]

# The live clone-provisioning executor. ``None`` means live recovery is not wired in
# this deployment: staleness is surfaced honestly and the safe predecessor fallback
# stays effective. A deployment enables real recovery by installing an executor that
# builds the acceptance config from its own operational configuration and calls
# ``run_event_admission_acceptance`` — a deferred, human-gated step.
_LIVE_REPROOF_REPLAY: ReproofReplay | None = None


def set_live_reproof_replay(replay: ReproofReplay | None) -> None:
    """Install (or clear) the deferred live clone-provisioning replay executor."""

    global _LIVE_REPROOF_REPLAY
    _LIVE_REPROOF_REPLAY = replay


def configured_live_reproof_replay() -> ReproofReplay | None:
    """The installed live replay executor, or ``None`` while recovery is deferred."""

    return _LIVE_REPROOF_REPLAY


@dataclass(frozen=True)
class ReproofNeed:
    """Whether one project's proved class is stale, read from effective selection."""

    project_id: int
    proof_status: str
    is_stale: bool
    effective_status: str
    effective_policy_version: str
    latest_receipt_id: int | None


def evaluate_reproof_need(session: Session, project_id: int) -> ReproofNeed:
    """Decide staleness from the same status ordinary processing selects on.

    Stale means the newest proof is a *passing* proof whose bound identities drifted
    from what is deployed. Any other proof state — current, suspended, failed, corrupt,
    or never proved — is not recovered automatically.
    """

    status = read_event_admission_policy_status(session, project_id)
    return ReproofNeed(
        project_id=project_id,
        proof_status=status.proof_status,
        is_stale=status.proof_status == STALE_PROOF_STATUS,
        effective_status=status.status,
        effective_policy_version=status.effective_policy_version,
        latest_receipt_id=status.latest_receipt_id,
    )


def execute_scheduled_reproof(
    context,
    *,
    replay: ReproofReplay | None = None,
) -> dict:
    """Run one bounded staleness-recovery attempt and return an honest receipt.

    Called by the supervised runtime's effectful handler outside any runtime-held
    transaction. It reads staleness in its own short session, and only when the class
    is stale does it hand off to the replay executor — which owns the copy-erase-replay
    proof and the suspension-safe reactivation. Proof execution and activation are kept
    as separately observable fields: a passing proof whose reactivation a suspension
    vetoes still reports ``proof_outcome="passed"`` with ``activated=False``.
    """

    observed_at = _aware(context.clock.now())
    # The runtime resolved the claimed schedule before its transaction closed,
    # so the project, the configuration version and the validated scope arrive
    # with the claim instead of being re-read here.
    project_id = int(context.schedule.project_id)
    configuration_version = str(context.schedule.configuration_version)
    scope = dict(context.schedule.scope or {})
    with context.session_factory() as reading:
        project = reading.get(Project, project_id)
        if project is None:
            raise ValueError("Event Admission re-proof project no longer exists")
        project_slug = str(project.slug)
        need = evaluate_reproof_need(reading, project_id)

    policy_version = str(scope.get("policy_version") or UNKNOWN_SCOPE_POLICY_VERSION)
    reason_version = str(scope.get("reason_version") or "")
    selection_rule = str(scope.get("selection_rule") or "")

    base = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "project_id": project_id,
        "configuration_version": configuration_version,
        "observed_at": observed_at.isoformat(),
        "policy_version": policy_version,
        "proof_status": need.proof_status,
        "was_stale": need.is_stale,
    }

    if not need.is_stale:
        health = (
            HEALTH_ATTENTION
            if need.proof_status in {"failed_newest_proof", "corrupt_newest_proof"}
            else HEALTH_OK
        )
        return _result(
            base,
            health=health,
            trigger="not_required",
            did_reprove=False,
            proof_outcome="not_run",
            activated=False,
            acceptance_receipt_id=None,
            effective_status=need.effective_status,
            effective_policy_version=need.effective_policy_version,
            detail=need.proof_status,
        )

    executor = replay if replay is not None else configured_live_reproof_replay()
    if executor is None:
        # The mechanism is shipped but live clone provisioning is a deferred,
        # human-gated step. Surface the stale class honestly; the predecessor rules
        # remain the safe fallback and nothing is reactivated on an older receipt.
        return _result(
            base,
            health=HEALTH_ATTENTION,
            trigger=STALE_PROOF_STATUS,
            did_reprove=False,
            proof_outcome="not_run",
            activated=False,
            acceptance_receipt_id=None,
            effective_status=need.effective_status,
            effective_policy_version=need.effective_policy_version,
            detail="live_recovery_execution_deferred",
        )

    result = executor(
        ReproofRequest(
            project_id=project_id,
            project_slug=project_slug,
            policy_version=policy_version,
            reason_version=reason_version,
            selection_rule=selection_rule,
        )
    )
    if not isinstance(result, EventAdmissionAcceptanceResult):
        raise ValueError("Event Admission re-proof replay returned an invalid result")

    # Re-read effective status so the receipt reports the state ordinary processing
    # now sees, whatever the replay's activation decision was.
    with context.session_factory() as after:
        settled = evaluate_reproof_need(after, project_id)

    passed = result.status == "passed"
    if passed and result.activated:
        health, detail = HEALTH_OK, "reactivated_after_stale_proof"
    elif passed:
        # A current passing proof whose reactivation a standing suspension vetoed.
        # The passing-but-unactivated receipt is retained honestly; the human act
        # governs and nothing here impersonates a lift.
        health, detail = HEALTH_OK, "reproved_passing_activation_vetoed"
    else:
        # A failed proof (zero eligible, a person's contrary decision, a gate) is
        # durable evidence and the safe predecessor fallback stays effective.
        health, detail = HEALTH_ATTENTION, "reproved_failed_safe_fallback"

    return _result(
        base,
        health=health,
        trigger=STALE_PROOF_STATUS,
        did_reprove=True,
        proof_outcome=result.status,
        activated=bool(result.activated),
        acceptance_receipt_id=int(result.receipt_id),
        effective_status=settled.effective_status,
        effective_policy_version=settled.effective_policy_version,
        detail=detail,
    )


def _result(
    base: dict,
    *,
    health: str,
    trigger: str,
    did_reprove: bool,
    proof_outcome: str,
    activated: bool,
    acceptance_receipt_id: int | None,
    effective_status: str,
    effective_policy_version: str,
    detail: str,
) -> dict:
    return {
        **base,
        "health": health,
        "trigger": trigger,
        "did_reprove": did_reprove,
        "proof_outcome": proof_outcome,
        "activated": activated,
        "acceptance_receipt_id": acceptance_receipt_id,
        "effective_status": effective_status,
        "effective_policy_version": effective_policy_version,
        "detail": detail,
    }


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Event Admission re-proof clock must supply an aware datetime")
    return value


# --- The Due Work declaration this recovery runs under ---------------------
#
# The three identities a replay is pinned to and the clones one attempt may
# consume are what this recovery *is*, so they are declared here rather than in
# the runtime; the runtime keeps the lease and the retries (card 6).

# The upper ceiling for the disposable clones one re-proof attempt may consume;
# the ADR-0050 replay provisions two policy clones plus a migration rehearsal.
_CLONE_BUDGET_CEILING = 8


@dataclass(frozen=True)
class EventAdmissionReproofDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables stale-class re-proof recovery.

    Re-proof replays the policy's own recorded history (ADR-0050): it consults no
    model, so ``model_token_budget`` must be a declared zero, never a silent one, and
    it delivers nothing outward, so ``notification_budget`` is zero too. Scope names
    the exact project and the three bound identities the replay is pinned to — the
    unknown-scope ``policy_version``, its ``reason_version``, and the approved replay
    population ``selection_rule`` (#324). ``clone_budget`` bounds the disposable clones
    one attempt may consume. The lease and deadline are generous because a real replay
    copies the database and rehearses migrations; concurrency stays one.
    """

    handler_key: ClassVar[str] = HANDLER_KEY

    policy_version: str
    reason_version: str
    selection_rule: str
    clone_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        policy_version: str,
        reason_version: str,
        selection_rule: str,
        starts_at: datetime,
    ) -> "EventAdmissionReproofDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            policy_version=policy_version,
            reason_version=reason_version,
            selection_rule=selection_rule,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=2,
            backoff_seconds=300,
            claim_ttl_seconds=1800,
            deadline_seconds=1800,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
            clone_budget=3,
        )


def _validated_declaration(
    declaration: EventAdmissionReproofDeclaration,
) -> ValidatedDeclaration:
    """Validate one stale-class re-proof declaration.

    Neither a prior activation nor deployed credentials enable recovery
    automatically: an authorized operator declares the three identities the
    replay is pinned to, or the handler stays refused.
    """

    if not DECLARED_IDENTITY.fullmatch(declaration.policy_version):
        raise DueWorkRefusal("event-admission-reproof policy version is invalid")
    if not DECLARED_IDENTITY.fullmatch(declaration.reason_version):
        raise DueWorkRefusal("event-admission-reproof reason version is invalid")
    if not DECLARED_IDENTITY.fullmatch(declaration.selection_rule):
        raise DueWorkRefusal("event-admission-reproof selection rule is invalid")
    starts_at = validate_scheduling(
        declaration,
        subject="event-admission-reproof",
        resources_valid=1 <= declaration.clone_budget <= _CLONE_BUDGET_CEILING,
    )
    scope = {
        "project_id": declaration.project_id,
        "policy_version": declaration.policy_version,
        "reason_version": declaration.reason_version,
        "selection_rule": declaration.selection_rule,
    }
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_KEY,
            scope=scope,
            input_identity={
                "kind": "one_project_event_admission_class-v1",
                **scope,
            },
            idempotency_contract="at_least_once_reconcilable",
            starts_at=starts_at,
            extra_resources={"clone_budget": declaration.clone_budget},
        ),
        input_identity={"handler": HANDLER_KEY, **scope},
    )


def _stored_declaration(
    stored: ResolvedSchedule,
) -> EventAdmissionReproofDeclaration:
    return EventAdmissionReproofDeclaration(
        **stored.scheduling_fields(),
        policy_version=stored.scope.get("policy_version", ""),
        reason_version=stored.scope.get("reason_version", ""),
        selection_rule=stored.scope.get("selection_rule", ""),
        # The clone budget is retained with the other resource bounds rather
        # than in the scope, so it is rebuilt from the configuration.
        clone_budget=(stored.configuration.get("resources") or {}).get(
            "clone_budget", 0
        ),
    )


def _run_due_work(context) -> dict[str, Any]:
    """Run one bounded stale-class re-proof for a claimed occurrence.

    The recovery decision, the ADR-0050 replay, and the suspension-safe
    reactivation all live in this module; the runtime holds no transaction and no
    project mutation lock across the replay — the replay owns its own short
    final mutation.
    """

    return execute_scheduled_reproof(context)


DUE_WORK_REGISTRATION = HandlerRegistration(
    key=HANDLER_KEY,
    scope_kind="one_project_event_admission_class",
    idempotency_contract="at_least_once_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=0,
    declaration_type=EventAdmissionReproofDeclaration,
    validate=_validated_declaration,
    stored_declaration=_stored_declaration,
    run_effectful=_run_due_work,
)
