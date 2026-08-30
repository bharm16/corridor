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

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.event_admission import (
    UNKNOWN_SCOPE_POLICY_VERSION,
    read_event_admission_policy_status,
)
from corridor.event_admission_acceptance import EventAdmissionAcceptanceResult
from corridor.models import DueWorkSchedule, Project

RESULT_SCHEMA_VERSION = "event-admission-reproof-result-v1"
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
    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise ValueError("Event Admission re-proof schedule disappeared")
        project_id = int(schedule.project_id)
        configuration_version = str(schedule.configuration_version)
        scope = dict(schedule.scope_json or {})
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
