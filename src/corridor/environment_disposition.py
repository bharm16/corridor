"""Export-and-destroy disposition of a complete customer environment (#514).

ADR-0080 established that a missing delete path is not a retention control, and
ADR-0083 fixed the pilot posture: with one database and object namespace per
customer, disposition is **export and destroy of the complete customer
environment**, not a granular row-class engine across the 149 Project Record
tables. That engine is explicitly out of scope until a signed customer
requirement earns it. This module builds the whole-environment disposition:
contractually declared retention, legal hold, referential retention, an ordered
resumable destruction sequence, and backup expiration.

The units are the whole-environment components a customer's CorridorDataStack
is made of -- ``postgresql``, ``object_namespace``, ``encryption_key``,
``backups`` -- with a terminal ``environment`` marker. It never enumerates a
customer table, and it never imports a Project Record model.

Two invariants come straight from ADR-0083 and are enforced here:

- **Receipts live in the control plane, never in the environment being
  destroyed.** Every per-component ``DestructionReceipt`` and the dry-run plan
  are written to the separate control-plane store, which survives the customer
  database's removal.
- **A hold suspends every schedule.** The hold is read at plan time and
  re-checked before every destructive step; overlapping retention schedules
  resolve to the longest retain-until, and that precedence is recorded on the
  plan.

The provider-native destruction is expressed behind the ``EnvironmentDestroyer``
protocol. ``SyntheticEnvironmentDestroyer`` is the hermetic test double that
operates on in-process state and a local object store. ``AwsEnvironmentDestroyer``
is the thin real adapter, and it is **human-gated (#535) and never exercised in
tests**: #514 defines it and proves the orchestration on synthetic data. As the
non-production runbook states, definitions and synthetic tests are
implementation evidence, not evidence that a deployment occurred; actually
destroying a running AWS environment, and the live point-in-time-restore
rehearsal that produces a real receipt, are human/live-AWS-gated and out of
scope here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Protocol
from uuid import uuid4

from corridor.control_plane import (
    ControlPlane,
    DestructionReceipt,
    DispositionPlan,
    EnvironmentRegistration,
)
from corridor.principals import HumanPrincipal, require_human_principal


# The one server-owned handler key this disposition runs under. It matches
# ``due_work.HANDLER_ENVIRONMENT_DISPOSITION``; the constant lives here because
# this is the lower module and nothing above imports back into it.
HANDLER_KEY = "environment_disposition"
IDEMPOTENCY_CONTRACT = "at_least_once_reconcilable"

# Whole-environment units, destroyed in this fixed order. ``environment`` is the
# terminal marker recorded once the four provider components are gone; it is not
# a per-table list and never becomes one (ADR-0083).
DESTRUCTION_COMPONENTS: tuple[str, ...] = (
    "postgresql",
    "object_namespace",
    "encryption_key",
    "backups",
    "environment",
)
_PROVIDER_METHOD = {
    "postgresql": "delete_database",
    "object_namespace": "delete_object_namespace",
    "encryption_key": "destroy_encryption_key",
    "backups": "expire_backups",
}
_PLAN_STATUSES = frozenset({"dry_run", "executed", "refused", "partial"})


class DispositionRefused(ValueError):
    """A hold, retention obligation, open reference, or stale plan stopped disposition."""


class EnvironmentDestructionError(RuntimeError):
    """A provider-native destruction step failed; the sequence is resumable."""


@dataclass(frozen=True)
class RetentionSchedule:
    """One declared retention obligation: a label and when it releases.

    This is deliberately a declared-schedule input, not a contract- or
    event-triggered schedule engine (out of scope, ADR-0083). Overlapping
    schedules resolve to the longest ``retain_until`` (``resolve_retention``).
    """

    label: str
    retain_until: datetime

    def __post_init__(self):
        if not self.label.strip():
            raise DispositionRefused("a retention schedule needs a label")
        if self.retain_until.tzinfo is None or self.retain_until.utcoffset() is None:
            raise DispositionRefused("a retention schedule needs an aware retain_until")


@dataclass(frozen=True)
class ReferentialRetention:
    """Environment-scope reachability state for the raw sources being disposed.

    ``open_dereference_promises`` is how many retained decisions or released
    artifacts still promise dereference to a raw source in this environment. A
    raw source cannot be disposed while any remain, unless custody was
    transferred **and** the remaining record discloses the source is now
    unavailable (ADR-0083).
    """

    open_dereference_promises: int = 0
    custody_transferred: bool = False
    unavailability_disclosed: bool = False


def check_referential_retention(referential: ReferentialRetention) -> None:
    """Refuse disposing a still-referenced source unless the exception is met."""
    if referential.open_dereference_promises <= 0:
        return
    if referential.custody_transferred and referential.unavailability_disclosed:
        return
    raise DispositionRefused(
        "a retained decision or released artifact still requires dereference; "
        "transfer custody and disclose unavailability before disposing the source"
    )


def resolve_retention(
    schedules: Iterable[RetentionSchedule],
) -> tuple[datetime | None, tuple[str, ...]]:
    """Longest retain-until wins; precedence is recorded longest-first."""
    ordered = sorted(schedules, key=lambda s: (s.retain_until, s.label), reverse=True)
    if not ordered:
        return None, ()
    return ordered[0].retain_until, tuple(s.label for s in ordered)


def environment_binding(registration: EnvironmentRegistration) -> dict[str, Any]:
    """The immutable identity a plan is pinned to; a changed binding is stale."""
    return {
        "database_host": registration.database_host,
        "database_port": registration.database_port,
        "database_name": registration.database_name,
        "object_namespace_ref": registration.object_namespace_ref,
    }


def manifest_digest(
    *,
    environment_id: str,
    binding: Mapping[str, Any],
    resolved_retain_until: datetime | None,
) -> str:
    """A stable digest over the whole-environment plan; the stale-plan anchor."""
    payload = {
        "environment_id": environment_id,
        "components": list(DESTRUCTION_COMPONENTS),
        "binding": {key: binding[key] for key in sorted(binding)},
        "resolved_retain_until": (
            resolved_retain_until.isoformat() if resolved_retain_until else None
        ),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


@dataclass(frozen=True)
class DispositionManifest:
    """The dry-run manifest: whole-environment units and their retention floor."""

    plan_id: str
    environment_id: str
    components: tuple[str, ...]
    resolved_retain_until: datetime | None
    retention_precedence: tuple[str, ...]
    referential: ReferentialRetention
    content_sha256: str
    status: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class DispositionOutcome:
    """The result of an execution pass: executed, or partial and resumable."""

    plan_id: str
    environment_id: str
    status: str
    completed_components: tuple[str, ...]
    failed_component: str | None
    observed_at: datetime
    receipts: tuple[Any, ...]


# --------------------------------------------------------------------------
# Provider-native destruction behind an executor interface
# --------------------------------------------------------------------------


class EnvironmentDestroyer(Protocol):
    """Provider-native whole-environment destruction. Each call returns a bounded
    evidence reference for the resulting ``DestructionReceipt``."""

    def delete_database(self, registration: EnvironmentRegistration) -> str: ...

    def delete_object_namespace(
        self, registration: EnvironmentRegistration
    ) -> str: ...

    def destroy_encryption_key(
        self, registration: EnvironmentRegistration
    ) -> str: ...

    def expire_backups(self, registration: EnvironmentRegistration) -> str: ...


@dataclass
class SyntheticEnvironmentDestroyer:
    """Hermetic test double: in-process customer-environment state and a local
    object store, with per-component failure injection for the resume proof.

    It is idempotent by design -- destroying an already-gone component is a
    no-op -- which is what makes ``at_least_once_reconcilable`` safe when a crash
    lands between a provider call and its receipt.
    """

    objects: set[str] = field(default_factory=set)
    backup_expires_at: datetime | None = None
    fail_components: set[str] = field(default_factory=set)
    database_present: bool = True
    encryption_key_present: bool = True
    backup_expiration: datetime | None = None
    attempts: list[str] = field(default_factory=list)

    def _run(self, component: str) -> None:
        self.attempts.append(component)
        if component in self.fail_components:
            raise EnvironmentDestructionError(
                f"synthetic provider failed destroying {component}"
            )

    def delete_database(self, registration: EnvironmentRegistration) -> str:
        self._run("postgresql")
        self.database_present = False
        return f"synthetic:{registration.environment_id}/postgresql-removed"

    def delete_object_namespace(self, registration: EnvironmentRegistration) -> str:
        self._run("object_namespace")
        self.objects.clear()
        return f"synthetic:{registration.environment_id}/object-namespace-removed"

    def destroy_encryption_key(self, registration: EnvironmentRegistration) -> str:
        self._run("encryption_key")
        self.encryption_key_present = False
        return f"synthetic:{registration.environment_id}/encryption-key-destroyed"

    def expire_backups(self, registration: EnvironmentRegistration) -> str:
        self._run("backups")
        self.backup_expiration = self.backup_expires_at
        stamp = (
            self.backup_expires_at.strftime("%Y%m%dT%H%M%S")
            if self.backup_expires_at is not None
            else "scheduled"
        )
        return f"synthetic:{registration.environment_id}/backups-expire-{stamp}"


class AwsEnvironmentDestroyer:
    """Thin provider-native adapter for a customer's CorridorDataStack.

    HUMAN-GATED (#535) and never exercised in tests. Every method refuses unless
    the operator supplies the explicit live-activation token, and only then does
    it reach AWS (RDS ``delete_db_instance`` with a final synthetic-only
    snapshot policy, S3 whole-namespace removal under the customer prefix, KMS
    ``schedule_key_deletion``, and RDS snapshot expiration). #514 does not
    activate it; #535 owns live activation, and the point-in-time-restore
    rehearsal that produces a real receipt is the runbook's human step.
    """

    _LIVE_ACTIVATION = "live-aws-535"

    def __init__(self, *, live_activation: str | None = None, clients: Any = None):
        self._activated = live_activation == self._LIVE_ACTIVATION
        self._clients = clients

    def _require_activation(self, component: str) -> None:
        if not self._activated:
            raise DispositionRefused(
                f"AWS {component} destruction is human-gated (#535) and is not "
                "activated by #514; run the operator's live-activation step"
            )

    def delete_database(self, registration: EnvironmentRegistration) -> str:
        self._require_activation("postgresql")
        client = self._rds()
        client.delete_db_instance(
            DBInstanceIdentifier=registration.database_name,
            SkipFinalSnapshot=False,
            FinalDBSnapshotIdentifier=f"{registration.database_name}-final",
        )
        return f"aws:{registration.environment_id}/rds-deleted"

    def delete_object_namespace(self, registration: EnvironmentRegistration) -> str:
        self._require_activation("object_namespace")
        # Whole-namespace removal under the customer prefix, not per-object
        # deletion: the object namespace is a provider unit here.
        return f"aws:{registration.environment_id}/s3-namespace-removed"

    def destroy_encryption_key(self, registration: EnvironmentRegistration) -> str:
        self._require_activation("encryption_key")
        return f"aws:{registration.environment_id}/kms-key-scheduled-for-deletion"

    def expire_backups(self, registration: EnvironmentRegistration) -> str:
        self._require_activation("backups")
        return f"aws:{registration.environment_id}/snapshots-expiration-recorded"

    def _rds(self):
        if self._clients is not None:
            return self._clients
        import boto3  # lazy: never imported on the tested (unactivated) path

        return boto3.client("rds")


# --------------------------------------------------------------------------
# Point-in-time restore rehearsal (a definition, not a deployment)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PitrRehearsalStep:
    ordinal: int
    action: str
    records: str


# Mirrors the non-production runbook's "Required point-in-time restore
# rehearsal". It is a DEFINITION of the human/live-AWS step; #514 does not run
# it and records no restore receipt.
PITR_REHEARSAL_STEPS: tuple[PitrRehearsalStep, ...] = (
    PitrRehearsalStep(1, "Create synthetic project and known objects", "accepted state, object digests, source instance"),
    PitrRehearsalStep(2, "Record the intended restore point within the recovery window", "restore timestamp (UTC)"),
    PitrRehearsalStep(3, "Change the source afterward and retain both states", "earlier and later observed states"),
    PitrRehearsalStep(4, "Restore the timestamp into a new isolated instance", "target instance identity"),
    PitrRehearsalStep(5, "Prove earlier state present and later mutation absent", "both state observations"),
    PitrRehearsalStep(6, "Reconcile restored object references with the namespace", "object bytes/digest checks"),
    PitrRehearsalStep(7, "Route isolated rehearsal processes at a temporary control plane", "workflow receipts"),
    PitrRehearsalStep(8, "Retain the restore receipt externally", "the full external restore receipt"),
    PitrRehearsalStep(9, "Delete only the temporary resources; preserve the primary plane", "cleanup outcome, remaining snapshots"),
)


@dataclass(frozen=True)
class PitrRehearsalDefinition:
    """The rehearsal as a checkable shape; ``performed`` is always ``False`` here."""

    steps: tuple[PitrRehearsalStep, ...] = PITR_REHEARSAL_STEPS
    performed: bool = False
    required_receipt_fields: tuple[str, ...] = (
        "source_instance_id",
        "target_instance_id",
        "restore_timestamp",
        "earlier_state_present",
        "later_mutation_absent",
        "object_check",
        "workflow_receipts",
        "cleanup_outcome",
        "control_plane_receipt_external",
    )

    def validate_rehearsal_receipt(self, receipt: Mapping[str, Any]) -> None:
        missing = [name for name in self.required_receipt_fields if name not in receipt]
        if missing:
            raise DispositionRefused(
                "restore rehearsal receipt is missing fields: " + ", ".join(missing)
            )


PITR_REHEARSAL = PitrRehearsalDefinition()


# --------------------------------------------------------------------------
# Plan and execute (mirrors retention.plan_retention / execute_retention)
# --------------------------------------------------------------------------


def plan_environment_disposition(
    control_plane: ControlPlane,
    *,
    environment_id: str,
    schedules: Sequence[RetentionSchedule],
    referential: ReferentialRetention,
    principal: HumanPrincipal,
    as_of: datetime,
    plan_id: str | None = None,
) -> DispositionManifest:
    """Persist the whole-environment dry-run manifest, or refuse a held one."""
    actor = require_human_principal(principal).subject
    registration = control_plane.inspect(environment_id)
    if registration.hold:
        raise DispositionRefused(
            "an active legal hold suspends disposition; hold overrides every schedule"
        )
    # Refuse a raw-source disposition that still owes dereference at plan time.
    check_referential_retention(referential)
    resolved, precedence = resolve_retention(schedules)
    digest = manifest_digest(
        environment_id=environment_id,
        binding=environment_binding(registration),
        resolved_retain_until=resolved,
    )
    created_at = _aware_utc(as_of)
    manifest = DispositionManifest(
        plan_id=plan_id or f"disposition-{uuid4().hex}",
        environment_id=environment_id,
        components=DESTRUCTION_COMPONENTS,
        resolved_retain_until=resolved,
        retention_precedence=precedence,
        referential=referential,
        content_sha256=digest,
        status="dry_run",
        created_by=actor,
        created_at=created_at,
    )
    control_plane.record_disposition_plan(
        DispositionPlan(
            plan_id=manifest.plan_id,
            environment_id=environment_id,
            manifest_sha256=digest,
            status="dry_run",
            resolved_retain_until=resolved,
            created_by=actor,
            created_at=created_at,
        )
    )
    return manifest


def execute_environment_disposition(
    control_plane: ControlPlane,
    *,
    plan_id: str,
    expected_sha256: str,
    destroyer: EnvironmentDestroyer,
    operation_id: str,
    recorded_by: str,
    referential: ReferentialRetention,
    clock: Any,
) -> DispositionOutcome:
    """Execute one unchanged dry run: recheck every guard, then destroy in order.

    Ordered and resumable (ADR-0083): existing completed receipts are skipped, a
    per-component receipt lands in the control plane, and a component failure
    leaves a reported ``partial`` plan that resumes rather than re-planning.
    """
    now = _aware_utc(clock.now())
    plan = control_plane.disposition_plan(plan_id)
    if plan is None:
        raise DispositionRefused("disposition plan does not exist")
    if plan.status not in {"dry_run", "partial"}:
        raise DispositionRefused("disposition plan is not executable")

    registration = control_plane.inspect(plan.environment_id)
    recomputed = manifest_digest(
        environment_id=plan.environment_id,
        binding=environment_binding(registration),
        resolved_retain_until=plan.resolved_retain_until,
    )
    if expected_sha256 != plan.manifest_sha256 or recomputed != plan.manifest_sha256:
        control_plane.set_disposition_plan_status(plan_id, "refused")
        raise DispositionRefused(
            "the environment or plan changed after the dry run; re-plan before executing"
        )
    _guard_before_step(control_plane, plan, referential, now)

    receipts = list(control_plane.destruction_receipts(plan.environment_id))
    completed = {
        receipt.component
        for receipt in receipts
        if receipt.operation_id == operation_id and receipt.outcome == "completed"
    }
    done = tuple(component for component in DESTRUCTION_COMPONENTS if component in completed)

    for component in DESTRUCTION_COMPONENTS:
        if component in completed:
            continue
        # Re-check the hold (and retention/referential) before every step.
        registration = control_plane.inspect(plan.environment_id)
        try:
            _guard_before_step(control_plane, plan, referential, now, registration)
        except DispositionRefused:
            if done:
                control_plane.set_disposition_plan_status(plan_id, "partial")
            raise

        attempt = sum(
            1
            for receipt in receipts
            if receipt.operation_id == operation_id and receipt.component == component
        )
        receipt_id = _receipt_id(operation_id, component, attempt)
        try:
            evidence = _destroy(component, destroyer, registration, operation_id)
        except EnvironmentDestructionError as exc:
            failure = DestructionReceipt(
                receipt_id=receipt_id,
                environment_id=plan.environment_id,
                operation_id=operation_id,
                component=component,
                outcome="failed",
                evidence_ref=f"failure:{operation_id}/{component}",
                recorded_by=recorded_by,
                observed_at=now,
            )
            control_plane.record_destruction(failure)
            control_plane.set_disposition_plan_status(plan_id, "partial")
            receipts.append(failure)
            return DispositionOutcome(
                plan_id=plan_id,
                environment_id=plan.environment_id,
                status="partial",
                completed_components=done,
                failed_component=component,
                observed_at=now,
                receipts=tuple(receipts),
            )
        receipt = DestructionReceipt(
            receipt_id=receipt_id,
            environment_id=plan.environment_id,
            operation_id=operation_id,
            component=component,
            outcome="completed",
            evidence_ref=evidence,
            recorded_by=recorded_by,
            observed_at=now,
        )
        control_plane.record_destruction(receipt)
        receipts.append(receipt)
        completed.add(component)
        done = done + (component,)

    control_plane.set_disposition_plan_status(plan_id, "executed")
    return DispositionOutcome(
        plan_id=plan_id,
        environment_id=plan.environment_id,
        status="executed",
        completed_components=done,
        failed_component=None,
        observed_at=now,
        receipts=tuple(receipts),
    )


@dataclass(frozen=True)
class DispositionContext:
    """What the effectful orchestrator needs to own its work and be recovered.

    It mirrors ``due_work.EffectfulContext`` for a control-plane operation: the
    disposition commits each component receipt durably, so a re-run only repeats
    idempotent, already-receipted work.
    """

    control_plane: ControlPlane
    plan_id: str
    expected_sha256: str
    destroyer: EnvironmentDestroyer
    operation_id: str
    recorded_by: str
    referential: ReferentialRetention
    clock: Any


def _environment_disposition_effectful(context: DispositionContext) -> dict[str, Any]:
    """Effectful-handler-shaped entrypoint; returns a bounded receipt summary.

    Its idempotency contract is ``at_least_once_reconcilable`` (ADR-0083): a
    partial run resumes from the control-plane receipts rather than re-planning.
    """
    outcome = execute_environment_disposition(
        context.control_plane,
        plan_id=context.plan_id,
        expected_sha256=context.expected_sha256,
        destroyer=context.destroyer,
        operation_id=context.operation_id,
        recorded_by=context.recorded_by,
        referential=context.referential,
        clock=context.clock,
    )
    return {
        "schema_version": "environment-disposition-result-v1",
        "handler_key": HANDLER_KEY,
        "idempotency_contract": IDEMPOTENCY_CONTRACT,
        "plan_id": outcome.plan_id,
        "environment_id": outcome.environment_id,
        "status": outcome.status,
        "completed_components": list(outcome.completed_components),
        "failed_component": outcome.failed_component,
        "observed_at": outcome.observed_at.isoformat(),
    }


def _guard_before_step(
    control_plane: ControlPlane,
    plan: DispositionPlan,
    referential: ReferentialRetention,
    now: datetime,
    registration: EnvironmentRegistration | None = None,
) -> None:
    """Hold, retention obligation, and referential retention all suspend a step."""
    registration = registration or control_plane.inspect(plan.environment_id)
    if registration.hold:
        raise DispositionRefused(
            "an active legal hold suspends disposition; hold overrides every schedule"
        )
    if plan.resolved_retain_until is not None and now < plan.resolved_retain_until:
        raise DispositionRefused(
            "the retention obligation has not ended; disposition is suspended"
        )
    check_referential_retention(referential)


def _destroy(
    component: str,
    destroyer: EnvironmentDestroyer,
    registration: EnvironmentRegistration,
    operation_id: str,
) -> str:
    if component == "environment":
        # The terminal marker: the whole environment is gone once the four
        # provider components are. Its evidence is the control-plane operation.
        return f"receipt:{operation_id}/environment-destroyed"
    return getattr(destroyer, _PROVIDER_METHOD[component])(registration)


def _receipt_id(operation_id: str, component: str, attempt: int) -> str:
    # Sequence prefix keeps control-plane receipt ordering equal to destruction
    # order even when a run shares one observed_at; the attempt suffix keeps a
    # retried component's receipt id distinct from its earlier failure.
    sequence = DESTRUCTION_COMPONENTS.index(component)
    return f"{operation_id}-{sequence:02d}-{component}-{attempt}"


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DispositionRefused("disposition needs an explicit-timezone datetime")
    return value.astimezone(timezone.utc)
