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
protocol, and that protocol is the whole surface the executor and the operator
CLI call: plan verification, the binding and freeze guards, hold cancellation,
execution-boundary preparation (which hands the adapter the executor's
per-mutation guard), the four provider components and the terminal
whole-environment verification, each returning its own evidence reference. The
first version declared four methods and then branched on the adapter's class
for everything else, spelling the AWS evidence format in the executor and
setting the mutation guard on the adapter from outside; the synthetic double
therefore ran a different path from the AWS adapter, and its resume proof did
not prove the AWS one. Each adapter now implements every call, answering "not
applicable" honestly where a step has nothing to do for it, so one executor
path serves both. ``SyntheticEnvironmentDestroyer`` is the hermetic test double
that operates on in-process state and a local object store.
``AwsEnvironmentDestroyer`` is exercised with SDK response stubs and requires a
persisted, approved provider inventory before any real call. Its S3 namespace
is one dedicated bucket, never a key prefix inside a shared one: the first
version emptied a slash-bounded prefix that the export step refused and the
stack census could not address, so the same registration could never be
disposed (#813); ``control_plane.s3_object_namespace_bucket`` now decides the
shape once, at registration, and every seam reads that parsed bucket. Pending
deletion never means completion. As the
non-production runbook states, definitions and synthetic tests are
implementation evidence, not evidence that a deployment occurred; actually
destroying a running AWS environment, and the live point-in-time-restore
rehearsal that produces a real receipt, are human/live-AWS-gated and out of
scope here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from corridor.control_plane import (
    ControlPlane,
    DestructionReceipt,
    DispositionPlan,
    EnvironmentRegistration,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.disposition_contracts import (
    AwsDispositionResources,
    DispositionRefused,
    EnvironmentDestructionError,
    require_no_rds_replicas,
    automated_backup_rows,
)


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
    "environment": "delete_environment",
}
_PLAN_STATUSES = frozenset({"dry_run", "executed", "refused", "partial"})


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
    provider_resources_sha256: str | None = None,
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
    if provider_resources_sha256 is not None:
        payload["provider_resources_sha256"] = provider_resources_sha256
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
    provider_resources_sha256: str | None = None


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


@runtime_checkable
class EnvironmentDestroyer(Protocol):
    """Provider-native whole-environment destruction, as the executor calls it.

    Every method has a defined not-applicable behaviour so an adapter with
    nothing to do for a step still implements it honestly, and the executor
    never inspects the adapter's class. Each destruction call returns the
    adapter's own bounded evidence reference for the resulting
    ``DestructionReceipt``; once ``prepare_execution`` has bound a plan, that
    reference binds the plan too.
    """

    def verify_plan(self, registration: EnvironmentRegistration) -> None:
        """Re-observe the provider before the dry run is persisted; refuse drift."""

    def require_bound(self, registration: EnvironmentRegistration) -> None:
        """Refuse unless this adapter is activated and bound to ``registration``."""

    def require_frozen(self, registration: EnvironmentRegistration) -> None:
        """The operator's freeze guard: bound, quiescent, and unchanged inventory."""

    def cancel_pending_deletions_for_hold(
        self, control_plane: ControlPlane, plan: DispositionPlan, *, observed_at: datetime
    ) -> Any:
        """Under a fresh hold, cancel cancellable recovery-window deletions and
        return the recorded observation, or ``None`` when nothing is cancellable."""

    def prepare_execution(
        self,
        control_plane: ControlPlane,
        plan: DispositionPlan,
        referential: ReferentialRetention,
        operation_id: str,
        *,
        observed_at: datetime,
        before_delete: Any,
    ) -> None:
        """Bind this adapter to one persisted, unchanged plan before any deletion.

        ``before_delete`` is the executor's guard, run by the adapter before
        every provider mutation. The adapter also checks that the receipts it
        would resume from bind this plan and inventory."""

    def delete_database(self, registration: EnvironmentRegistration) -> str: ...

    def delete_object_namespace(
        self, registration: EnvironmentRegistration
    ) -> str: ...

    def destroy_encryption_key(
        self, registration: EnvironmentRegistration
    ) -> str: ...

    def expire_backups(self, registration: EnvironmentRegistration) -> str: ...

    def delete_environment(self, registration: EnvironmentRegistration) -> str:
        """The terminal marker: verify the whole environment is gone, or raise
        ``EnvironmentDestructionError`` while any declared population remains."""


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
    operation_id: str | None = None

    def _run(self, component: str) -> None:
        self.attempts.append(component)
        if component in self.fail_components:
            raise EnvironmentDestructionError(
                f"synthetic provider failed destroying {component}"
            )

    def verify_plan(self, registration: EnvironmentRegistration) -> None:
        """Not applicable: there is no provider inventory to re-observe."""

    def require_bound(self, registration: EnvironmentRegistration) -> None:
        """Not applicable: in-process state binds any registration."""

    def require_frozen(self, registration: EnvironmentRegistration) -> None:
        """Not applicable: nothing runs against the in-process state but this."""

    def cancel_pending_deletions_for_hold(
        self, control_plane: ControlPlane, plan: DispositionPlan, *, observed_at: datetime
    ) -> None:
        """Not applicable: synthetic destruction has no recovery window."""
        return None

    def prepare_execution(
        self,
        control_plane: ControlPlane,
        plan: DispositionPlan,
        referential: ReferentialRetention,
        operation_id: str,
        *,
        observed_at: datetime,
        before_delete: Any,
    ) -> None:
        """A synthetic destroyer never executes a plan pinned to a provider
        inventory. Its destruction is one in-process step per component with
        no provider call between the executor's per-component guard and the
        effect, so ``before_delete`` has nothing to guard here."""
        if plan.provider_resources_sha256 is not None:
            raise DispositionRefused("a provider-bound plan cannot be executed by a synthetic destroyer")
        self.operation_id = operation_id

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

    def delete_environment(self, registration: EnvironmentRegistration) -> str:
        # The terminal marker: the whole environment is gone once the four
        # provider components are. Its evidence is the control-plane operation.
        self._run("environment")
        return f"receipt:{self.operation_id}/environment-destroyed"


class AwsEnvironmentDestroyer:
    """Verified AWS deletion, with pending work returned as resumable failure.

    Every mutation requires the caller's fresh hold/retention guard. A provider
    acknowledging a deletion request is not evidence the resource is gone.
    There is no implicit credential lookup; #535 supplies scoped clients and
    the exact resource inventory it approved.
    """

    _LIVE_ACTIVATION = "live-aws-535"
    # The completion evidence kind per component. The reference format is this
    # adapter's own; the executor records what the adapter returns.
    _EVIDENCE = {
        "postgresql": "rds-absent",
        "object_namespace": "s3-namespace-empty",
        "encryption_key": "declared-customer-keys-absent",
        "backups": "declared-rds-backups-absent",
        "environment": "whole-environment-absent",
    }

    def __init__(self, *, live_activation: str | None = None, clients: Any = None,
                 resources: AwsDispositionResources | None = None,
                 approved_resource_sha256: str | None = None, before_delete=None):
        self._activated = live_activation == self._LIVE_ACTIVATION
        self._clients = clients
        self.resources = resources
        self.approved_resource_sha256 = approved_resource_sha256
        self.before_delete = before_delete
        self._plan: DispositionPlan | None = None

    def _require_activation(self, component: str) -> None:
        if not self._activated:
            raise DispositionRefused(
                f"AWS {component} destruction is human-gated (#535) and is not "
                "activated by #514; run the operator's live-activation step"
            )

    def _binding(self, registration: EnvironmentRegistration):
        self._require_activation("environment")
        resources = self.resources
        if resources is None or self._clients is None or self.approved_resource_sha256 != resources.sha256:
            raise DispositionRefused("AWS disposition requires its exact approved provider resource inventory and scoped clients")
        if registration.enabled or registration.hold:
            raise DispositionRefused("AWS disposition requires a disabled environment without a legal hold")
        resources.require_registration(registration)
        if self._clients["sts"].get_caller_identity()["Account"] != resources.account_id:
            raise DispositionRefused("AWS account differs from the approved resource inventory")
        for service in ("rds", "s3", "kms"):
            if self._clients[service].meta.region_name != resources.region:
                raise DispositionRefused("AWS client region differs from the approved resource inventory")
        return resources

    def _mutate(self, method, **parameters):
        if self.before_delete is None:
            raise DispositionRefused("AWS deletion requires a fresh hold and retention guard")
        self.before_delete()
        return method(**parameters)

    def _evidence(self, component: str) -> str:
        reference = f"aws:{self.resources.environment_id}/{self._EVIDENCE[component]}/{self.resources.sha256}"
        if self._plan is None:
            return reference
        return f"{reference}/{self._plan.manifest_sha256}"

    def verify_plan(self, registration: EnvironmentRegistration) -> None:
        """The declared-component adapter has no inventory to re-observe; the
        approved resource digest binds its plan."""
        self._binding(registration)

    def require_bound(self, registration: EnvironmentRegistration) -> None:
        self._binding(registration)

    def require_frozen(self, registration: EnvironmentRegistration) -> None:
        """Binding is the whole freeze check here: no application tasks or
        stack membership are declared to this adapter."""
        self._binding(registration)

    def cancel_pending_deletions_for_hold(
        self, control_plane: ControlPlane, plan: DispositionPlan, *, observed_at: datetime
    ) -> None:
        """Not applicable: this adapter holds no inventory of cancellable
        recovery windows; the stack adapter does."""
        return None

    def prepare_execution(
        self,
        control_plane: ControlPlane,
        plan: DispositionPlan,
        referential: ReferentialRetention,
        operation_id: str,
        *,
        observed_at: datetime,
        before_delete: Any,
    ) -> None:
        self._require_persisted_plan(plan)
        self._prepare_execution_boundary(
            control_plane, plan, referential, operation_id, observed_at=observed_at
        )
        self._plan = plan
        self.before_delete = before_delete
        self._verify_resume_receipts(
            control_plane.destruction_receipts(plan.environment_id), plan, operation_id
        )

    def _require_persisted_plan(self, plan: DispositionPlan) -> None:
        if self.resources is None or plan.provider_resources_sha256 != self.resources.sha256:
            raise DispositionRefused("AWS resource inventory differs from the persisted dry-run plan")
        if self.resources.whole_environment is not None:
            if plan.provider_resources != json.loads(json.dumps(asdict(self.resources))):
                raise DispositionRefused("provider inventory bytes differ from the persisted plan")

    def _prepare_execution_boundary(self, control_plane, plan, referential, operation_id, *, observed_at) -> None:
        """No boundary to persist without a whole-environment inventory."""

    def _verify_resume_receipts(self, receipts, plan: DispositionPlan, operation_id: str) -> None:
        for receipt in receipts:
            if receipt.operation_id != operation_id:
                continue
            expected = (self._evidence(receipt.component) if receipt.outcome == "completed"
                        else failure_evidence(operation_id, receipt.component, plan))
            if receipt.evidence_ref != expected or (receipt.component == "environment" and receipt.outcome == "completed" and self.resources.whole_environment is None):
                raise DispositionRefused("AWS resume receipts do not bind this plan and provider inventory")

    def delete_environment(self, registration: EnvironmentRegistration) -> str:
        """Declared components alone never prove the whole environment is gone."""
        raise EnvironmentDestructionError("declared AWS components are gone; complete stack, logs, secrets and remote-copy disposal still require verified inventory coverage")

    def delete_database(self, registration: EnvironmentRegistration) -> str:
        resource = self._binding(registration)
        client = self._clients["rds"]
        rows = [row for page in client.get_paginator("describe_db_instances").paginate(
            Filters=[{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}])
            for row in page.get("DBInstances", [])]
        if not rows:
            return self._evidence("postgresql")
        if len(rows) != 1:
            raise EnvironmentDestructionError("RDS did not identify exactly one approved instance")
        row = rows[0]
        if (row.get("DBInstanceArn"), row.get("DbiResourceId")) != (resource.db_instance_arn, resource.db_resource_id):
            raise DispositionRefused("RDS identifier now names a different physical instance")
        require_no_rds_replicas(row)
        if row.get("DBInstanceStatus") == "deleting":
            raise EnvironmentDestructionError("RDS deletion is still pending")
        endpoint = row.get("Endpoint", {})
        if (endpoint.get("Address"), endpoint.get("Port"), row.get("DBName")) != (
                resource.database_host, resource.database_port, resource.database_name):
            raise DispositionRefused("RDS endpoint does not match the customer environment")
        self._mutate(client.delete_db_instance, DBInstanceIdentifier=resource.db_instance_identifier,
            SkipFinalSnapshot=False, FinalDBSnapshotIdentifier=resource.final_snapshot_identifier,
            DeleteAutomatedBackups=True)
        raise EnvironmentDestructionError("RDS deletion requested; verify absence on retry")

    def delete_object_namespace(self, registration: EnvironmentRegistration) -> str:
        resource = self._binding(registration)
        bucket = resource.object_namespace_bucket
        client = self._clients["s3"]
        parameters = {"Bucket": bucket, "ExpectedBucketOwner": resource.account_id}
        for page in client.get_paginator("list_object_versions").paginate(**parameters):
            objects = [{"Key": row["Key"], "VersionId": row["VersionId"]}
                       for row in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            for offset in range(0, len(objects), 1000):
                result = self._mutate(client.delete_objects, Bucket=bucket,
                    ExpectedBucketOwner=resource.account_id, Delete={"Objects": objects[offset:offset+1000], "Quiet": True})
                if result.get("Errors"):
                    raise EnvironmentDestructionError("S3 reported object-version deletion failures")
        for page in client.get_paginator("list_multipart_uploads").paginate(**parameters):
            for upload in page.get("Uploads", []):
                self._mutate(client.abort_multipart_upload, Bucket=bucket, Key=upload["Key"],
                    UploadId=upload["UploadId"], ExpectedBucketOwner=resource.account_id)
        for operation, fields in (("list_object_versions", ("Versions", "DeleteMarkers")),
                                  ("list_objects_v2", ("Contents",)),
                                  ("list_multipart_uploads", ("Uploads",))):
            for page in client.get_paginator(operation).paginate(**parameters):
                if any(page.get(field) for field in fields):
                    raise EnvironmentDestructionError("S3 namespace is not yet empty")
        return self._evidence("object_namespace")

    def destroy_encryption_key(self, registration: EnvironmentRegistration) -> str:
        resource = self._binding(registration)
        client = self._clients["kms"]
        pending = False
        for arn in resource.kms_key_arns:
            if not arn.startswith(f"arn:aws:kms:{resource.region}:{resource.account_id}:key/"):
                raise DispositionRefused("KMS key lies outside the approved account and region")
            try:
                key = client.describe_key(KeyId=arn)["KeyMetadata"]
            except client.exceptions.NotFoundException:
                continue
            if key.get("Arn") != arn or key.get("KeyManager") != "CUSTOMER" or key.get("MultiRegion"):
                raise DispositionRefused("only declared dedicated single-region customer keys may be deleted")
            pending = True
            if key.get("KeyState") != "PendingDeletion":
                self._mutate(client.schedule_key_deletion, KeyId=arn, PendingWindowInDays=30)
        if pending:
            raise EnvironmentDestructionError("KMS deletion is pending; scheduled deletion is not completion")
        return self._evidence("encryption_key")

    def expire_backups(self, registration: EnvironmentRegistration) -> str:
        resource = self._binding(registration)
        client = self._clients["rds"]
        pending = False
        for page in client.get_paginator("describe_db_snapshots").paginate(
                SnapshotType="manual", Filters=[{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]):
            for snapshot in page.get("DBSnapshots", []):
                if snapshot.get("DbiResourceId") != resource.db_resource_id:
                    raise DispositionRefused("RDS snapshot does not belong to the approved physical instance")
                pending = True
                if snapshot.get("Status") != "deleting":
                    self._mutate(client.delete_db_snapshot, DBSnapshotIdentifier=snapshot["DBSnapshotIdentifier"])
        for backup in automated_backup_rows(client, resource.db_resource_id):
            if backup.get("DbiResourceId") != resource.db_resource_id:
                raise DispositionRefused("RDS automated backup differs from the approved instance")
            pending = True
            self._mutate(client.delete_db_instance_automated_backup, DbiResourceId=resource.db_resource_id)
        if pending:
            raise EnvironmentDestructionError("RDS backup expiration requested; verify absence on retry")
        return self._evidence("backups")


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
    provider_resources: AwsDispositionResources | None = None,
    provider_observer: Any = None,
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
    if provider_resources is not None:
        provider_resources.require_registration(registration)
        if provider_resources.whole_environment is not None:
            if provider_observer is None or provider_observer.resources != provider_resources:
                raise DispositionRefused("whole-environment planning requires fresh provider observation")
            provider_observer.verify_plan(registration)
    resource_digest = provider_resources.sha256 if provider_resources is not None else None
    digest = manifest_digest(
        environment_id=environment_id,
        binding=environment_binding(registration),
        resolved_retain_until=resolved,
        provider_resources_sha256=resource_digest,
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
        provider_resources_sha256=resource_digest,
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
            provider_resources_sha256=resource_digest,
            provider_resources=json.loads(json.dumps(asdict(provider_resources))) if provider_resources else None,
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
        provider_resources_sha256=plan.provider_resources_sha256,
    )
    if expected_sha256 != plan.manifest_sha256 or recomputed != plan.manifest_sha256:
        control_plane.set_disposition_plan_status(plan_id, "refused")
        raise DispositionRefused(
            "the environment or plan changed after the dry run; re-plan before executing"
        )
    if registration.hold:
        destroyer.cancel_pending_deletions_for_hold(control_plane, plan, observed_at=now)
    _guard_before_step(control_plane, plan, referential, now)

    def before_delete():
        # The adapter runs this before every provider mutation: a fresh hold,
        # retention and referential guard, and an unchanged, disabled binding.
        current = control_plane.inspect(plan.environment_id)
        _guard_before_step(control_plane, plan, referential, _aware_utc(clock.now()), current)
        if current.enabled or manifest_digest(environment_id=plan.environment_id,
                binding=environment_binding(current), resolved_retain_until=plan.resolved_retain_until,
                provider_resources_sha256=plan.provider_resources_sha256) != expected_sha256:
            raise DispositionRefused("AWS environment was enabled or its disposition binding changed")

    destroyer.prepare_execution(
        control_plane, plan, referential, operation_id, observed_at=now, before_delete=before_delete
    )

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
            evidence = _destroy(component, destroyer, registration)
        except DispositionRefused:
            control_plane.set_disposition_plan_status(plan_id, "partial")
            raise
        except EnvironmentDestructionError as exc:
            failure = DestructionReceipt(
                receipt_id=receipt_id,
                environment_id=plan.environment_id,
                operation_id=operation_id,
                component=component,
                outcome="failed",
                evidence_ref=failure_evidence(operation_id, component, plan),
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
) -> str:
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        return getattr(destroyer, _PROVIDER_METHOD[component])(registration)
    except (BotoCoreError, ClientError) as exc:
        if component == "environment":
            raise EnvironmentDestructionError("AWS final verification failed; no completion is established") from exc
        raise EnvironmentDestructionError("AWS provider operation failed; no completion is established") from exc


def failure_evidence(operation_id: str, component: str, plan: DispositionPlan) -> str:
    # The executor's own reference for a failed component; the adapters check
    # resume receipts against the same spelling.
    return f"failure:{operation_id}/{component}/{plan.manifest_sha256}"


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
