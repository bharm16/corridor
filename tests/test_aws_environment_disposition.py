"""AWS responses, never live resources, prove deletion receipts do not get ahead of evidence."""

from dataclasses import replace
from datetime import datetime, timezone

import boto3
from botocore.stub import Stubber
import pytest

from corridor.control_plane import EnvironmentRegistration
from corridor.environment_disposition import (
    AwsDispositionResources, AwsEnvironmentDestroyer, DispositionRefused, EnvironmentDestructionError,
)


@pytest.fixture
def aws():
    registration = EnvironmentRegistration("customer", "environment", "deployment",
        "customer.abc.us-east-1.rds.amazonaws.com", 5432, "corridor",
        "env:WEB", "env:WORKER", "s3:customer-bucket/data/", "configuration:none", enabled=False)
    resource = AwsDispositionResources("customer", "environment", "deployment", "123456789012", "us-east-1",
        registration.database_host, 5432, "corridor", "customer-instance",
        "arn:aws:rds:us-east-1:123456789012:db:customer-instance", "db-CUSTOMERRESOURCE",
        registration.object_namespace_ref, "customer-final", ("arn:aws:kms:us-east-1:123456789012:key/customer-key",))
    clients = {name: boto3.client(name, region_name="us-east-1", aws_access_key_id="fixture",
        aws_secret_access_key="fixture", endpoint_url="http://127.0.0.1:9") for name in ("sts", "rds", "s3", "kms")}
    stubs = {name: Stubber(client) for name, client in clients.items()}
    for stub in stubs.values():
        stub.activate()
    calls = []
    destroyer = AwsEnvironmentDestroyer(live_activation="live-aws-535", clients=clients,
        resources=resource, approved_resource_sha256=resource.sha256, before_delete=lambda: calls.append("guard"))
    yield registration, resource, destroyer, stubs, calls
    for stub in stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()
    for client in clients.values():
        client.close()


def identity(stubs):
    stubs["sts"].add_response("get_caller_identity", {"Account": "123456789012", "UserId": "fixture",
        "Arn": "arn:aws:iam::123456789012:role/fixture"}, {})


def test_magic_activation_string_cannot_report_unperformed_deletions():
    destroyer = AwsEnvironmentDestroyer(live_activation="live-aws-535")
    for operation in (destroyer.delete_database, destroyer.delete_object_namespace,
                      destroyer.destroy_encryption_key, destroyer.expire_backups):
        with pytest.raises(DispositionRefused, match="inventory"):
            operation(None)


def test_rds_request_is_pending_and_only_verified_absence_completes(aws):
    registration, resource, destroyer, stubs, calls = aws
    identity(stubs)
    row = {"DBInstanceArn": resource.db_instance_arn, "DbiResourceId": resource.db_resource_id,
        "DBInstanceStatus": "available", "DBName": "corridor",
        "Endpoint": {"Address": resource.database_host, "Port": 5432}}
    query = {"Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]}
    stubs["rds"].add_response("describe_db_instances", {"DBInstances": [row]}, query)
    stubs["rds"].add_response("delete_db_instance", {}, {"DBInstanceIdentifier": "customer-instance",
        "SkipFinalSnapshot": False, "FinalDBSnapshotIdentifier": "customer-final", "DeleteAutomatedBackups": True})
    with pytest.raises(EnvironmentDestructionError, match="requested"):
        destroyer.delete_database(registration)
    assert calls == ["guard"]
    identity(stubs)
    stubs["rds"].add_response("describe_db_instances", {"DBInstances": []}, query)
    assert "/rds-absent/" in destroyer.delete_database(registration)


def test_s3_per_object_error_never_becomes_a_completion_receipt(aws):
    registration, _, destroyer, stubs, calls = aws
    identity(stubs)
    parameters = {"Bucket": "customer-bucket", "Prefix": "data/", "ExpectedBucketOwner": "123456789012"}
    stubs["s3"].add_response("list_object_versions", {"Versions": [{"Key": "data/source", "VersionId": "one"}]}, parameters)
    stubs["s3"].add_response("delete_objects", {"Errors": [{"Key": "data/source", "Code": "AccessDenied"}]},
        {"Bucket": "customer-bucket", "ExpectedBucketOwner": "123456789012",
         "Delete": {"Objects": [{"Key": "data/source", "VersionId": "one"}], "Quiet": True}})
    with pytest.raises(EnvironmentDestructionError, match="failures"):
        destroyer.delete_object_namespace(registration)
    assert calls == ["guard"]


def test_s3_empty_namespace_requires_versions_current_objects_and_multipart_checks(aws):
    registration, _, destroyer, stubs, _ = aws
    identity(stubs)
    parameters = {"Bucket": "customer-bucket", "Prefix": "data/", "ExpectedBucketOwner": "123456789012"}
    for operation in ("list_object_versions", "list_multipart_uploads", "list_object_versions", "list_objects_v2", "list_multipart_uploads"):
        stubs["s3"].add_response(operation, {}, parameters)
    assert "/s3-namespace-empty/" in destroyer.delete_object_namespace(registration)


def test_kms_waiting_period_is_not_key_destruction(aws):
    registration, resource, destroyer, stubs, calls = aws
    arn = resource.kms_key_arns[0]
    identity(stubs)
    stubs["kms"].add_response("describe_key", {"KeyMetadata": {"KeyId": "customer-key", "Arn": arn,
        "KeyManager": "CUSTOMER", "MultiRegion": False, "KeyState": "Enabled"}}, {"KeyId": arn})
    stubs["kms"].add_response("schedule_key_deletion", {"KeyId": arn, "KeyState": "PendingDeletion",
        "DeletionDate": datetime(2026, 10, 9, tzinfo=timezone.utc)}, {"KeyId": arn, "PendingWindowInDays": 30})
    with pytest.raises(EnvironmentDestructionError, match="not completion"):
        destroyer.destroy_encryption_key(registration)
    assert calls == ["guard"]


def test_changed_registration_refuses_before_any_provider_call(aws):
    registration, _, destroyer, _, _ = aws
    with pytest.raises(DispositionRefused, match="differs"):
        destroyer.delete_database(replace(registration, database_name="another_database"))


def test_backups_complete_only_after_both_populations_are_empty(aws):
    registration, resource, destroyer, stubs, _ = aws
    identity(stubs)
    stubs["rds"].add_response("describe_db_snapshots", {"DBSnapshots": []}, {"SnapshotType": "manual", "Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]})
    stubs["rds"].add_response("describe_db_instance_automated_backups", {"DBInstanceAutomatedBackups": []}, {"DbiResourceId": resource.db_resource_id})
    assert "/declared-rds-backups-absent/" in destroyer.expire_backups(registration)


def test_persisted_inventory_cannot_change_and_components_do_not_prove_whole_environment(aws, customer_environment_databases):
    from corridor.control_plane import ControlPlane, DestructionReceipt
    from corridor.control_plane_schema import initialize_control_plane
    from corridor.environment_disposition import plan_environment_disposition, execute_environment_disposition, ReferentialRetention
    from corridor.principals import HumanPrincipal
    from types import SimpleNamespace

    registration, resource, destroyer, stubs, _ = aws
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registry.register(registration)
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    manifest = plan_environment_disposition(registry, environment_id=registration.environment_id,
        schedules=(), referential=ReferentialRetention(), principal=HumanPrincipal("local:operator"),
        as_of=now, provider_resources=resource)
    assert registry.disposition_plan(manifest.plan_id).provider_resources_sha256 == resource.sha256
    changed = replace(resource, db_resource_id="db-ANOTHER")
    arguments = dict(plan_id=manifest.plan_id, expected_sha256=manifest.content_sha256,
        operation_id="aws-inventory-proof", recorded_by="local:operator",
        referential=ReferentialRetention(), clock=SimpleNamespace(now=lambda: now))
    with pytest.raises(DispositionRefused, match="persisted"):
        execute_environment_disposition(registry, destroyer=AwsEnvironmentDestroyer(
            live_activation="live-aws-535", resources=changed, approved_resource_sha256=changed.sha256), **arguments)
    registry.record_destruction(DestructionReceipt(receipt_id="inherited-aws-completion",
        environment_id=registration.environment_id, operation_id="inherited-op", component="environment",
        outcome="completed", evidence_ref="synthetic:environment/gone", recorded_by="local:operator", observed_at=now))
    with pytest.raises(DispositionRefused, match="resume receipts"):
        execute_environment_disposition(registry, destroyer=destroyer, **{**arguments, "operation_id": "inherited-op"})
    for _ in range(4):
        identity(stubs)
    stubs["rds"].add_response("describe_db_instances", {"DBInstances": []}, {"Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]})
    parameters = {"Bucket": "customer-bucket", "Prefix": "data/", "ExpectedBucketOwner": "123456789012"}
    for operation in ("list_object_versions", "list_multipart_uploads", "list_object_versions", "list_objects_v2", "list_multipart_uploads"):
        stubs["s3"].add_response(operation, {}, parameters)
    stubs["kms"].add_client_error("describe_key", "NotFoundException", expected_params={"KeyId": resource.kms_key_arns[0]})
    stubs["rds"].add_response("describe_db_snapshots", {"DBSnapshots": []}, {"SnapshotType": "manual", "Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]})
    stubs["rds"].add_response("describe_db_instance_automated_backups", {"DBInstanceAutomatedBackups": []}, {"DbiResourceId": resource.db_resource_id})
    outcome = execute_environment_disposition(registry, destroyer=destroyer, **arguments)
    assert outcome.status == "partial" and outcome.failed_component == "environment"
    assert not any(r.operation_id == arguments["operation_id"] and r.component == "environment" and r.outcome == "completed" for r in outcome.receipts)


def test_renamed_physical_instance_is_not_mistaken_for_deleted(aws):
    registration, resource, destroyer, stubs, _ = aws
    identity(stubs)
    stubs["rds"].add_response("describe_db_instances", {"DBInstances": [{
        "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:renamed",
        "DbiResourceId": resource.db_resource_id, "DBInstanceIdentifier": "renamed"}]},
        {"Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]})
    with pytest.raises(DispositionRefused, match="physical instance"):
        destroyer.delete_database(registration)


def test_manual_and_automated_backups_use_their_own_delete_operations(aws):
    registration, resource, destroyer, stubs, calls = aws
    identity(stubs)
    stubs["rds"].add_response("describe_db_snapshots", {"DBSnapshots": [{"DBSnapshotIdentifier": "customer-final",
        "DbiResourceId": resource.db_resource_id, "Status": "available"}]},
        {"SnapshotType": "manual", "Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]})
    stubs["rds"].add_response("delete_db_snapshot", {}, {"DBSnapshotIdentifier": "customer-final"})
    stubs["rds"].add_response("describe_db_instance_automated_backups", {"DBInstanceAutomatedBackups": [{
        "DbiResourceId": resource.db_resource_id, "Status": "retained"}]}, {"DbiResourceId": resource.db_resource_id})
    stubs["rds"].add_response("delete_db_instance_automated_backup", {}, {"DbiResourceId": resource.db_resource_id})
    with pytest.raises(EnvironmentDestructionError, match="verify absence"):
        destroyer.expire_backups(registration)
    assert calls == ["guard", "guard"]


def test_modeled_missing_automated_backup_completes_expiration(aws):
    registration, resource, destroyer, stubs, _ = aws
    identity(stubs)
    stubs["rds"].add_response("describe_db_snapshots", {"DBSnapshots": []},
        {"SnapshotType": "manual", "Filters": [{"Name": "dbi-resource-id", "Values": [resource.db_resource_id]}]})
    stubs["rds"].add_client_error("describe_db_instance_automated_backups", "DBInstanceAutomatedBackupNotFound",
        http_status_code=404, expected_params={"DbiResourceId": resource.db_resource_id})
    assert "/declared-rds-backups-absent/" in destroyer.expire_backups(registration)
