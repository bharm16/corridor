"""Whole-environment export-and-destroy disposition, proven on synthetic data (#514).

Every DB-backed test drives a disposable control-plane PostgreSQL database (the
harness fixtures), a fake ``EnvironmentDestroyer`` and an in-process object
store; the one resume proof also runs the AWS adapter against SDK response
stubs. Nothing here touches AWS, the customer Alembic history, or the 149
Project Record tables: disposition operates in whole-environment units only,
and its receipts and plan live in the separate control-plane store (ADR-0083).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from corridor.control_plane import ControlPlane, EnvironmentRegistration
from corridor.control_plane_schema import (
    CONTROL_PLANE_METADATA,
    initialize_control_plane,
)
from corridor.principals import HumanPrincipal
from corridor import environment_disposition as disposition
from corridor.environment_disposition import (
    DESTRUCTION_COMPONENTS,
    DispositionContext,
    DispositionRefused,
    ReferentialRetention,
    RetentionSchedule,
    SyntheticEnvironmentDestroyer,
    check_referential_retention,
    execute_environment_disposition,
    manifest_digest,
    plan_environment_disposition,
    resolve_retention,
)


class FixedClock:
    def __init__(self, moment: datetime):
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _registration(environment_id: str, *, hold: bool = False) -> EnvironmentRegistration:
    # The module-scoped control-plane DB is shared, so every environment needs a
    # distinct (customer, deployment) and (host, port, database) to satisfy the
    # registry's unique constraints.
    slug = environment_id.replace("-", "_")
    return EnvironmentRegistration(
        customer_id=f"cust-{environment_id}",
        environment_id=environment_id,
        deployment_id=f"deploy-{environment_id}",
        database_host="localhost",
        database_port=5433,
        database_name=f"db_{slug}",
        web_credential_ref="env:DISPOSITION_WEB",
        worker_credential_ref="env:DISPOSITION_WORKER",
        object_namespace_ref=f"namespace:{environment_id}",
        connector_configuration_ref="configuration:none",
        enabled=True,
        hold=hold,
    )


# --------------------------------------------------------------------------
# Pure units (no database)
# --------------------------------------------------------------------------


def test_overlapping_schedules_resolve_to_the_longest_retention_and_record_precedence():
    schedules = (
        RetentionSchedule("contract", _utc(2030, 1, 1)),
        RetentionSchedule("statutory", _utc(2032, 6, 1)),
        RetentionSchedule("engagement", _utc(2028, 1, 1)),
    )
    resolved, precedence = resolve_retention(schedules)
    assert resolved == _utc(2032, 6, 1)
    # Longest first: the winning obligation leads the recorded precedence.
    assert precedence == ("statutory", "contract", "engagement")
    # No declared retention is a valid input: nothing floors the disposition.
    assert resolve_retention(()) == (None, ())


def test_destruction_is_whole_environment_units_never_a_per_table_engine():
    assert DESTRUCTION_COMPONENTS == (
        "postgresql",
        "object_namespace",
        "encryption_key",
        "backups",
        "environment",
    )
    assert len(DESTRUCTION_COMPONENTS) == 5
    # A row-by-row engine across the 149 Project Record tables is forbidden:
    # the disposition module never reaches a customer model.
    import inspect

    source = inspect.getsource(disposition)
    assert "corridor.models" not in source
    assert "import models" not in source


def test_manifest_digest_is_deterministic_and_binding_sensitive():
    binding = {
        "database_host": "localhost",
        "database_port": 5433,
        "database_name": "disposition_synth",
        "object_namespace_ref": "namespace:disposition",
    }
    first = manifest_digest(
        environment_id="env-a", binding=binding, resolved_retain_until=_utc(2030, 1, 1)
    )
    assert first == manifest_digest(
        environment_id="env-a", binding=binding, resolved_retain_until=_utc(2030, 1, 1)
    )
    moved = manifest_digest(
        environment_id="env-a",
        binding={**binding, "database_name": "somewhere_else"},
        resolved_retain_until=_utc(2030, 1, 1),
    )
    assert moved != first
    assert len(first) == 64


def test_referential_retention_guard_refuses_then_allows_the_disclosed_exception():
    # A retained decision or released artifact still promises dereference.
    with pytest.raises(DispositionRefused, match="dereference"):
        check_referential_retention(ReferentialRetention(open_dereference_promises=2))
    # Custody transferred but unavailability not disclosed is still refused.
    with pytest.raises(DispositionRefused):
        check_referential_retention(
            ReferentialRetention(open_dereference_promises=2, custody_transferred=True)
        )
    # The disclosed-exception path: custody transferred AND record discloses
    # the source is unavailable.
    check_referential_retention(
        ReferentialRetention(
            open_dereference_promises=2,
            custody_transferred=True,
            unavailability_disclosed=True,
        )
    )
    # No open promises: nothing to guard.
    check_referential_retention(ReferentialRetention())


def test_the_orchestrator_is_an_effectful_shaped_registered_handler():
    from corridor import due_work

    assert disposition.HANDLER_KEY == due_work.HANDLER_ENVIRONMENT_DISPOSITION
    assert disposition.IDEMPOTENCY_CONTRACT == "at_least_once_reconcilable"
    # The effectful entrypoint takes exactly one context argument, mirroring
    # ``HandlerContract.run_effectful``.
    assert callable(disposition._environment_disposition_effectful)


def test_the_disposition_plan_is_a_control_plane_table_not_customer_alembic():
    from corridor.migrations import policy
    from corridor.models import Base

    assert "control_plane.disposition_plans" in CONTROL_PLANE_METADATA.tables
    # It is not a Project Record model, so no customer Alembic revision defines
    # it and the migration window is untouched.
    assert "disposition_plans" not in Base.metadata.tables
    assert "control_plane.disposition_plans" not in Base.metadata.tables
    assert policy.CURRENT_HEAD == "b2d5f8a1c4e7"


def test_the_pitr_rehearsal_is_a_definition_with_a_validated_receipt_shape():
    definition = disposition.PITR_REHEARSAL
    ordinals = [step.ordinal for step in definition.steps]
    assert ordinals == sorted(ordinals) and ordinals[0] == 1
    # A definition is implementation evidence, not evidence a deployment
    # occurred: it never claims a real restore happened.
    assert definition.performed is False

    receipt = {field: "recorded" for field in definition.required_receipt_fields}
    definition.validate_rehearsal_receipt(receipt)
    with pytest.raises(DispositionRefused, match="rehearsal receipt"):
        definition.validate_rehearsal_receipt({})


def test_every_destroyer_implements_the_interface_the_executor_calls():
    from corridor.aws_environment_disposition import AwsStackEnvironmentDestroyer
    from corridor.environment_disposition import (
        AwsEnvironmentDestroyer,
        EnvironmentDestroyer,
    )

    declared = set(EnvironmentDestroyer.__protocol_attrs__)
    # Plan verification, the operator's binding and freeze guards, hold
    # cancellation, execution-boundary preparation, the four provider
    # components and the whole-environment verification: nothing the executor
    # or the CLI calls is outside the declared interface.
    assert declared == {
        "verify_plan",
        "require_bound",
        "require_frozen",
        "cancel_pending_deletions_for_hold",
        "prepare_execution",
        "delete_database",
        "delete_object_namespace",
        "destroy_encryption_key",
        "expire_backups",
        "delete_environment",
    }
    for adapter in (
        SyntheticEnvironmentDestroyer(),
        AwsEnvironmentDestroyer(),
        AwsStackEnvironmentDestroyer(),
    ):
        assert isinstance(adapter, EnvironmentDestroyer)


def test_the_real_aws_destroyer_is_human_gated_and_never_runs_here():
    from corridor.environment_disposition import AwsEnvironmentDestroyer

    destroyer = AwsEnvironmentDestroyer()
    # #514 defines the adapter; #535 activates it. Any provider call refuses
    # without explicit live activation, so tests can never reach AWS.
    with pytest.raises(DispositionRefused, match="human-gated"):
        destroyer.delete_database(_registration("env-live"))


# --------------------------------------------------------------------------
# Database-backed behavior (disposable control-plane PostgreSQL)
# --------------------------------------------------------------------------


def test_dry_run_manifest_persists_a_plan_and_matches_its_digest(
    customer_environment_databases,
):
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registration = _registration("env-dryrun")
    registry.register(registration)

    manifest = plan_environment_disposition(
        registry,
        environment_id="env-dryrun",
        schedules=(
            RetentionSchedule("contract", _utc(2030, 1, 1)),
            RetentionSchedule("statutory", _utc(2031, 1, 1)),
        ),
        referential=ReferentialRetention(),
        principal=HumanPrincipal("local:operator"),
        as_of=_utc(2026, 9, 8),
    )
    assert manifest.status == "dry_run"
    assert manifest.components == DESTRUCTION_COMPONENTS
    assert manifest.resolved_retain_until == _utc(2031, 1, 1)
    assert manifest.retention_precedence == ("statutory", "contract")

    stored = registry.disposition_plan(manifest.plan_id)
    assert stored is not None
    assert stored.status == "dry_run"
    assert stored.manifest_sha256 == manifest.content_sha256
    assert stored.resolved_retain_until == _utc(2031, 1, 1)
    assert stored.created_by == "local:operator"


def test_a_hold_refuses_disposition_at_plan_time(customer_environment_databases):
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registry.register(_registration("env-held", hold=True))

    with pytest.raises(DispositionRefused, match="hold"):
        plan_environment_disposition(
            registry,
            environment_id="env-held",
            schedules=(),
            referential=ReferentialRetention(),
            principal=HumanPrincipal("local:operator"),
            as_of=_utc(2026, 9, 8),
        )
    assert registry.disposition_plans("env-held") == ()


def test_execution_destroys_in_order_and_receipts_land_in_the_control_plane(
    customer_environment_databases,
):
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registration = _registration("env-execute")
    registry.register(registration)

    manifest = plan_environment_disposition(
        registry,
        environment_id="env-execute",
        schedules=(RetentionSchedule("contract", _utc(2026, 1, 1)),),
        referential=ReferentialRetention(),
        principal=HumanPrincipal("local:operator"),
        as_of=_utc(2026, 9, 8),
    )
    destroyer = SyntheticEnvironmentDestroyer(
        objects={"a", "b"}, backup_expires_at=_utc(2027, 1, 1)
    )
    outcome = execute_environment_disposition(
        registry,
        plan_id=manifest.plan_id,
        expected_sha256=manifest.content_sha256,
        destroyer=destroyer,
        operation_id="op-execute",
        recorded_by="local:operator",
        referential=ReferentialRetention(),
        clock=FixedClock(_utc(2026, 9, 8, 12)),
    )
    assert outcome.status == "executed"
    assert outcome.completed_components == DESTRUCTION_COMPONENTS
    # Provider-native whole-environment destruction actually happened on the fake.
    assert destroyer.database_present is False
    assert destroyer.objects == set()
    assert destroyer.encryption_key_present is False
    assert destroyer.backup_expiration == _utc(2027, 1, 1)

    receipts = registry.destruction_receipts("env-execute")
    assert tuple(r.component for r in receipts) == DESTRUCTION_COMPONENTS
    assert all(r.outcome == "completed" for r in receipts)
    assert all(r.operation_id == "op-execute" for r in receipts)
    assert registry.disposition_plan(manifest.plan_id).status == "executed"


def test_execution_refuses_a_stale_plan_whose_digest_no_longer_matches(
    customer_environment_databases,
):
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registry.register(_registration("env-stale"))
    manifest = plan_environment_disposition(
        registry,
        environment_id="env-stale",
        schedules=(),
        referential=ReferentialRetention(),
        principal=HumanPrincipal("local:operator"),
        as_of=_utc(2026, 9, 8),
    )
    destroyer = SyntheticEnvironmentDestroyer()
    with pytest.raises(DispositionRefused, match="changed"):
        execute_environment_disposition(
            registry,
            plan_id=manifest.plan_id,
            expected_sha256="0" * 64,
            destroyer=destroyer,
            operation_id="op-stale",
            recorded_by="local:operator",
            referential=ReferentialRetention(),
            clock=FixedClock(_utc(2026, 9, 8, 12)),
        )
    assert destroyer.attempts == []
    assert registry.destruction_receipts("env-stale") == ()
    assert registry.disposition_plan(manifest.plan_id).status == "refused"


def test_a_hold_placed_mid_sequence_suspends_and_the_disposition_resumes(
    customer_environment_databases,
):
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registry.register(_registration("env-hold-mid"))
    manifest = plan_environment_disposition(
        registry,
        environment_id="env-hold-mid",
        schedules=(),
        referential=ReferentialRetention(),
        principal=HumanPrincipal("local:operator"),
        as_of=_utc(2026, 9, 8),
    )

    # A destroyer that places a legal hold immediately after the first
    # component, so the next per-component hold recheck must suspend.
    class HoldingDestroyer(SyntheticEnvironmentDestroyer):
        def delete_database(self, registration):
            evidence = super().delete_database(registration)
            registry.set_state("env-hold-mid", enabled=True, hold=True)
            return evidence

    holding = HoldingDestroyer()
    with pytest.raises(DispositionRefused, match="hold"):
        execute_environment_disposition(
            registry,
            plan_id=manifest.plan_id,
            expected_sha256=manifest.content_sha256,
            destroyer=holding,
            operation_id="op-hold-mid",
            recorded_by="local:operator",
            referential=ReferentialRetention(),
            clock=FixedClock(_utc(2026, 9, 8, 12)),
        )
    # Only the first component was destroyed; the plan is partial, not executed.
    receipts = registry.destruction_receipts("env-hold-mid")
    assert tuple(r.component for r in receipts) == ("postgresql",)
    assert registry.disposition_plan(manifest.plan_id).status == "partial"

    # Lift the hold and resume: the completed component is skipped, the rest run.
    registry.set_state("env-hold-mid", enabled=True, hold=False)
    resume = SyntheticEnvironmentDestroyer()
    outcome = execute_environment_disposition(
        registry,
        plan_id=manifest.plan_id,
        expected_sha256=manifest.content_sha256,
        destroyer=resume,
        operation_id="op-hold-mid",
        recorded_by="local:operator",
        referential=ReferentialRetention(),
        clock=FixedClock(_utc(2026, 9, 8, 13)),
    )
    assert outcome.status == "executed"
    assert "postgresql" not in resume.attempts
    assert tuple(
        r.component for r in registry.destruction_receipts("env-hold-mid")
    ) == DESTRUCTION_COMPONENTS
    assert registry.disposition_plan(manifest.plan_id).status == "executed"


class _SyntheticResume:
    """The hermetic adapter's side of the resume proof."""

    name = "synthetic"
    resumed = ("executed", None)

    def __init__(self):
        self.registration = _registration("env-resume-synthetic")
        self.provider_resources = None
        self.destroyer = SyntheticEnvironmentDestroyer(fail_components={"backups"})

    def clear_failure(self):
        self.destroyer.fail_components = set()

    def evidence(self, component, manifest, operation_id):
        environment_id = self.registration.environment_id
        return {
            "postgresql": f"synthetic:{environment_id}/postgresql-removed",
            "object_namespace": f"synthetic:{environment_id}/object-namespace-removed",
            "encryption_key": f"synthetic:{environment_id}/encryption-key-destroyed",
            "backups": f"synthetic:{environment_id}/backups-expire-scheduled",
            "environment": f"receipt:{operation_id}/environment-destroyed",
        }[component]

    def close(self):
        # The three completed components were not destroyed a second time.
        assert self.destroyer.attempts.count("postgresql") == 1
        assert self.destroyer.attempts.count("object_namespace") == 1
        assert self.destroyer.attempts.count("encryption_key") == 1


class _AwsResume:
    """The declared-component AWS adapter driven by SDK response stubs, never a
    live resource. Its stubs queue exactly the calls each pass may make, so a
    component destroyed twice, or a mutation without the guard, fails the stub."""

    name = "aws"
    # Declared components alone never prove the whole environment is gone.
    resumed = ("partial", "environment")

    def __init__(self):
        import boto3
        from botocore.stub import Stubber
        from corridor.environment_disposition import (
            AwsDispositionResources, AwsEnvironmentDestroyer,
        )

        self.registration = replace(
            _registration("env-resume-aws"),
            enabled=False,
            object_namespace_ref="s3:customer-bucket/data/",
        )
        registration = self.registration
        self.provider_resources = AwsDispositionResources(
            registration.customer_id, registration.environment_id, registration.deployment_id,
            "123456789012", "us-east-1", registration.database_host, registration.database_port,
            registration.database_name, "customer-instance",
            "arn:aws:rds:us-east-1:123456789012:db:customer-instance", "db-CUSTOMERRESOURCE",
            registration.object_namespace_ref, "customer-final",
            ("arn:aws:kms:us-east-1:123456789012:key/customer-key",),
        )
        self.clients = {name: boto3.client(name, region_name="us-east-1", aws_access_key_id="fixture",
            aws_secret_access_key="fixture", endpoint_url="http://127.0.0.1:9") for name in ("sts", "rds", "s3", "kms")}
        self.stubs = {name: Stubber(client) for name, client in self.clients.items()}
        for stub in self.stubs.values():
            stub.activate()
        self.destroyer = AwsEnvironmentDestroyer(live_activation="live-aws-535", clients=self.clients,
            resources=self.provider_resources, approved_resource_sha256=self.provider_resources.sha256)
        self._queue_first_pass()

    def _identity(self):
        self.stubs["sts"].add_response("get_caller_identity", {"Account": "123456789012", "UserId": "fixture",
            "Arn": "arn:aws:iam::123456789012:role/fixture"}, {})

    def _queue_first_pass(self):
        resource = self.provider_resources
        by_instance = {"Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]}
        self._identity()
        self.stubs["rds"].add_response("describe_db_instances", {"DBInstances": []}, by_instance)
        self._identity()
        namespace = {"Bucket": "customer-bucket", "Prefix": "data/", "ExpectedBucketOwner": "123456789012"}
        for operation in ("list_object_versions", "list_multipart_uploads", "list_object_versions",
                          "list_objects_v2", "list_multipart_uploads"):
            self.stubs["s3"].add_response(operation, {}, namespace)
        self._identity()
        self.stubs["kms"].add_client_error("describe_key", "NotFoundException",
            expected_params={"KeyId": resource.kms_key_arns[0]})
        # backups: a manual snapshot is still present, so the pass requests its
        # deletion under the freeze guard and reports the component pending.
        self._identity()
        self.stubs["rds"].add_response("describe_db_snapshots", {"DBSnapshots": [{"DBSnapshotIdentifier": "customer-final",
            "DbiResourceId": resource.db_resource_id, "Status": "available"}]}, {"SnapshotType": "manual", **by_instance})
        self.stubs["rds"].add_response("delete_db_snapshot", {}, {"DBSnapshotIdentifier": "customer-final"})
        self.stubs["rds"].add_response("describe_db_instance_automated_backups", {"DBInstanceAutomatedBackups": []},
            {"DbiResourceId": resource.db_resource_id})

    def clear_failure(self):
        resource = self.provider_resources
        by_instance = {"Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]}
        self._identity()
        self.stubs["rds"].add_response("describe_db_snapshots", {"DBSnapshots": []}, {"SnapshotType": "manual", **by_instance})
        self.stubs["rds"].add_response("describe_db_instance_automated_backups", {"DBInstanceAutomatedBackups": []},
            {"DbiResourceId": resource.db_resource_id})

    def evidence(self, component, manifest, operation_id):
        kind = {"postgresql": "rds-absent", "object_namespace": "s3-namespace-empty",
                "encryption_key": "declared-customer-keys-absent", "backups": "declared-rds-backups-absent"}[component]
        return (f"aws:{self.registration.environment_id}/{kind}/{self.provider_resources.sha256}"
                f"/{manifest.content_sha256}")

    def close(self):
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()
            stub.deactivate()
        for client in self.clients.values():
            client.close()


@pytest.mark.parametrize("make_case", [_SyntheticResume, _AwsResume], ids=["synthetic", "aws"])
def test_a_partial_component_failure_is_reported_and_resumable_through_the_destroyer_interface(
    customer_environment_databases, make_case
):
    """One resume proof, one executor path, both adapters.

    The executor never inspects the adapter's class: it calls the declared
    interface, and each adapter answers honestly, including the terminal
    ``environment`` marker, which only a whole-environment inventory can
    complete on AWS.
    """
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    case = make_case()
    try:
        registry.register(case.registration)
        environment_id = case.registration.environment_id
        manifest = plan_environment_disposition(
            registry,
            environment_id=environment_id,
            schedules=(),
            referential=ReferentialRetention(),
            principal=HumanPrincipal("local:operator"),
            as_of=_utc(2026, 9, 8),
            provider_resources=case.provider_resources,
        )
        # Receipt ids derive from the operation id, and both cases share the
        # module's control-plane store, so each case owns its operation.
        operation_id = f"op-resume-{case.name}"
        arguments = dict(
            plan_id=manifest.plan_id,
            expected_sha256=manifest.content_sha256,
            destroyer=case.destroyer,
            operation_id=operation_id,
            recorded_by="local:operator",
            referential=ReferentialRetention(),
        )
        partial = execute_environment_disposition(
            registry, clock=FixedClock(_utc(2026, 9, 8, 12)), **arguments
        )
        assert partial.status == "partial"
        assert partial.failed_component == "backups"
        assert partial.completed_components == (
            "postgresql",
            "object_namespace",
            "encryption_key",
        )
        receipts = registry.destruction_receipts(environment_id)
        assert receipts[-1].component == "backups"
        assert receipts[-1].outcome == "failed"
        assert registry.disposition_plan(manifest.plan_id).status == "partial"

        # Resume with the failure cleared: completed components are skipped and
        # the sequence continues. The plan is never silently re-planned.
        case.clear_failure()
        resumed = execute_environment_disposition(
            registry, clock=FixedClock(_utc(2026, 9, 8, 13)), **arguments
        )
        assert (resumed.status, resumed.failed_component) == case.resumed
        receipts = registry.destruction_receipts(environment_id)
        terminal = "completed" if case.resumed[0] == "executed" else "failed"
        assert [(r.component, r.outcome) for r in receipts] == [
            ("postgresql", "completed"),
            ("object_namespace", "completed"),
            ("encryption_key", "completed"),
            ("backups", "failed"),
            ("backups", "completed"),
            ("environment", terminal),
        ]
        # Every evidence reference is the adapter's own, bound to this plan;
        # the failure reference is the executor's.
        for receipt in receipts:
            if receipt.outcome == "completed":
                assert receipt.evidence_ref == case.evidence(receipt.component, manifest, operation_id)
            else:
                assert receipt.evidence_ref == f"failure:{operation_id}/{receipt.component}/{manifest.content_sha256}"
        assert registry.disposition_plan(manifest.plan_id).status == case.resumed[0]
    finally:
        case.close()


def test_execution_refuses_before_the_retention_obligation_ends(
    customer_environment_databases,
):
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registry.register(_registration("env-retained"))
    manifest = plan_environment_disposition(
        registry,
        environment_id="env-retained",
        schedules=(RetentionSchedule("statutory", _utc(2035, 1, 1)),),
        referential=ReferentialRetention(),
        principal=HumanPrincipal("local:operator"),
        as_of=_utc(2026, 9, 8),
    )
    destroyer = SyntheticEnvironmentDestroyer()
    with pytest.raises(DispositionRefused, match="retention"):
        execute_environment_disposition(
            registry,
            plan_id=manifest.plan_id,
            expected_sha256=manifest.content_sha256,
            destroyer=destroyer,
            operation_id="op-retained",
            recorded_by="local:operator",
            referential=ReferentialRetention(),
            clock=FixedClock(_utc(2026, 9, 8, 12)),
        )
    assert destroyer.attempts == []


def test_the_effectful_entrypoint_returns_a_bounded_receipt_summary(
    customer_environment_databases,
):
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registry.register(_registration("env-effectful"))
    manifest = plan_environment_disposition(
        registry,
        environment_id="env-effectful",
        schedules=(),
        referential=ReferentialRetention(),
        principal=HumanPrincipal("local:operator"),
        as_of=_utc(2026, 9, 8),
    )
    context = DispositionContext(
        control_plane=registry,
        plan_id=manifest.plan_id,
        expected_sha256=manifest.content_sha256,
        destroyer=SyntheticEnvironmentDestroyer(),
        operation_id="op-effectful",
        recorded_by="local:operator",
        referential=ReferentialRetention(),
        clock=FixedClock(_utc(2026, 9, 8, 12)),
    )
    result = disposition._environment_disposition_effectful(context)
    assert result["environment_id"] == "env-effectful"
    assert result["status"] == "executed"
    assert result["plan_id"] == manifest.plan_id
    assert result["idempotency_contract"] == "at_least_once_reconcilable"


def test_the_operations_role_persists_a_plan_the_resolver_cannot_read(
    customer_environment_databases, control_plane_capabilities
):
    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError
    from corridor.control_plane_schema import DISPOSITION_PLANS

    operations, resolver = control_plane_capabilities
    registry = ControlPlane(operations)
    registry.register(_registration("env-scoped"))
    manifest = plan_environment_disposition(
        registry,
        environment_id="env-scoped",
        schedules=(),
        referential=ReferentialRetention(),
        principal=HumanPrincipal("local:operator"),
        as_of=_utc(2026, 9, 8),
    )
    # Operations can update the mutable status column ...
    registry.set_disposition_plan_status(manifest.plan_id, "refused")
    assert registry.disposition_plan(manifest.plan_id).status == "refused"
    # ... but not rebind the plan's identity columns.
    with pytest.raises(DBAPIError), operations.begin() as connection:
        connection.execute(
            text(
                "update control_plane.disposition_plans set manifest_sha256 = :d"
            ),
            {"d": "0" * 64},
        )
    # The resolver login has no read path to the operations-owned plan table.
    with pytest.raises(DBAPIError), resolver.connect() as connection:
        connection.execute(select(DISPOSITION_PLANS))
