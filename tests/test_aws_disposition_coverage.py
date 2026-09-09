"""Whole-stack disposal refuses omissions and observes asynchronous provider work."""
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from corridor.aws_environment_disposition import (
    AwsStackEnvironmentDestroyer, digest, observe_stack_inventory,
)
from corridor.environment_disposition import AwsDispositionResources, DispositionRefused, EnvironmentDestructionError
from corridor.environment_export import export_environment_archive, verify_export_archive
from corridor.environment_rehearsal import AwsRestoreRehearsal, RestoreRehearsal, result_digest
from corridor.principals import HumanPrincipal


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
ACCOUNT = "123456789012"
APP = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/application/one"
DATA = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/data/two"


class ScriptedClient:
    """A strict injected SDK seam: unexpected calls fail without a network."""
    class NotFound(Exception):
        pass

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.meta = SimpleNamespace(region_name="us-east-1")
        self.exceptions = SimpleNamespace(DBInstanceNotFoundFault=self.NotFound,
            DBInstanceAutomatedBackupNotFoundFault=self.NotFound,
            ResourceNotFoundException=self.NotFound, NotFoundException=self.NotFound,
            DBSubnetGroupNotFoundFault=self.NotFound, RepositoryNotFoundException=self.NotFound)

    def __getattr__(self, operation):
        def call(**parameters):
            assert self.script, f"unexpected {operation}: {parameters}"
            name, expected, result = self.script.pop(0)
            assert (operation, parameters) == (name, expected)
            self.calls.append((operation, parameters))
            if isinstance(result, BaseException):
                raise result
            return result
        return call

    def get_paginator(self, operation):
        return SimpleNamespace(paginate=lambda **parameters: [getattr(self, operation)(**parameters)])


def fixture_restore_parser(command, **options):
    assert command[:5] == ["pg_restore", "--file", "/dev/null", "--no-owner", "--no-privileges"]
    assert options["check"] is True
    assert Path(command[-1]).read_bytes().startswith(b"PGDMP")


def resource(**changes):
    value = AwsDispositionResources("customer", "environment", "deployment", ACCOUNT, "us-east-1",
        "db.example", 5432, "corridor", "customer-db", f"arn:aws:rds:us-east-1:{ACCOUNT}:db:customer-db",
        "db-CUSTOMER", "s3:artifact-bucket", "final-customer", ())
    return replace(value, **changes)


def inventory():
    return {"schema": "aws-corridor-environment-v1", "application_stack_id": APP, "data_stack_id": DATA,
            "resources": [], "implicit_log_groups": []}


def test_inventory_refuses_unknown_store_before_it_can_be_approved():
    outputs = [{"OutputKey": "Disposition" + key, "OutputValue": value} for key, value in (
        ("CustomerId", "customer"), ("EnvironmentId", "environment"), ("DeploymentId", "deployment"))]
    cf = ScriptedClient([
        ("describe_stacks", {"StackName": APP}, {"Stacks": [{"StackId": APP, "StackStatus": "CREATE_COMPLETE", "Outputs": outputs}]}),
        ("list_stack_resources", {"StackName": APP}, {"StackResourceSummaries": [{"ResourceType": "AWS::DynamoDB::Table", "ResourceStatus": "CREATE_COMPLETE", "LogicalResourceId": "new-store", "PhysicalResourceId": "data"}]}),
    ])
    with pytest.raises(DispositionRefused, match="uncovered"):
        observe_stack_inventory({"cloudformation": cf}, resource(), application_stack_id=APP, data_stack_id=DATA)
    assert not cf.script


def test_inventory_rejects_wrong_customer_stack_outputs():
    cf = ScriptedClient([("describe_stacks", {"StackName": APP}, {"Stacks": [{"StackId": APP,
        "StackStatus": "CREATE_COMPLETE", "Outputs": []}]})])
    with pytest.raises(DispositionRefused, match="outputs"):
        observe_stack_inventory({"cloudformation": cf}, resource(), application_stack_id=APP, data_stack_id=DATA)


def test_export_runs_bound_pg_dump_and_checks_every_retained_version(tmp_path):
    source = b"source bytes"
    params = {"Bucket": "artifact-bucket", "ExpectedBucketOwner": ACCOUNT}
    listing = {"Versions": [{"Key": "source", "VersionId": "version-one"}], "DeleteMarkers": [{"Key": "old", "VersionId": "deleted"}]}
    s3 = ScriptedClient([
        ("get_bucket_versioning", params, {"Status": "Enabled"}),
        ("list_object_versions", params, listing),
        ("get_object", {**params, "Key": "source", "VersionId": "version-one"}, {"VersionId": "version-one", "Body": BytesIO(source)}),
        ("list_object_versions", params, listing),
    ])
    pgpass = tmp_path / "pgpass"
    pgpass.write_text("fixture credentials never exported")
    calls = []
    def runner(command, **options):
        assert command[:3] == ["pg_dump", "--format=custom", "--no-password"]
        assert command[command.index("--host")+1] == "db.example"
        assert command[command.index("--username")+1] == "export_reader"
        assert options["check"] and options["env"]["PGPASSFILE"] == str(pgpass)
        Path(command[command.index("--file")+1]).write_bytes(b"PGDMPsynthetic custom dump")
    output = export_environment_archive(resources=resource(), inventory=inventory(), clients={"s3": s3},
        output_path=tmp_path / "export.tar", pgpass_file=pgpass, database_username="export_reader",
        principal=HumanPrincipal("local:operator"), before_export=lambda: calls.append("freeze"),
        unavailability_disclosure=b"The retained record discloses transferred source custody.", run=runner, restore_run=fixture_restore_parser)
    assert calls == ["freeze", "freeze"]
    assert len(output["manifest"]["members"]) == 3
    assert not s3.script
    with open(output["path"], "rb") as stream:
        manifest = verify_export_archive(stream, expected_sha256=output["sha256"], environment_id="environment", inventory_sha256=digest(inventory()), restore_run=fixture_restore_parser)
    assert manifest["object_versions"] == [{"key": "old", "version_id": "deleted", "delete_marker": True}, {"key": "source", "version_id": "version-one", "delete_marker": False}]
    assert b"fixture credentials" not in Path(output["path"]).read_bytes()
    with open(output["path"], "rb") as stream, pytest.raises(DispositionRefused, match="digest"):
        verify_export_archive(stream, expected_sha256="0" * 64, environment_id="environment", inventory_sha256=digest(inventory()), restore_run=fixture_restore_parser)


def test_export_refuses_versions_arriving_during_database_dump(tmp_path):
    params = {"Bucket": "artifact-bucket", "ExpectedBucketOwner": ACCOUNT}
    s3 = ScriptedClient([("get_bucket_versioning", params, {"Status": "Enabled"}), ("list_object_versions", params, {}), ("list_object_versions", params,
        {"Versions": [{"Key": "arrived", "VersionId": "new"}]})])
    pgpass = tmp_path / "pgpass"
    pgpass.touch()
    def runner(command, **options):
        Path(command[command.index("--file")+1]).write_bytes(b"PGDMPfixture")
    output = tmp_path / "export.tar"
    with pytest.raises(DispositionRefused, match="changed during export"):
        export_environment_archive(resources=resource(), inventory=inventory(), clients={"s3": s3}, output_path=output,
            pgpass_file=pgpass, database_username="export_reader", principal=HumanPrincipal("local:operator"), before_export=lambda: None, run=runner, restore_run=fixture_restore_parser)
    assert not output.exists()


def test_stack_request_never_reports_completion_and_guard_precedes_both_mutations():
    inv = inventory()
    cf = ScriptedClient([
        ("describe_stacks", {"StackName": DATA}, {"Stacks": [{"StackId": DATA, "StackStatus": "CREATE_COMPLETE", "EnableTerminationProtection": True}]}),
        ("update_termination_protection", {"EnableTerminationProtection": False, "StackName": DATA}, {}),
        ("delete_stack", {"StackName": DATA}, {}),
        ("describe_stacks", {"StackName": DATA}, {"Stacks": [{"StackId": DATA, "StackStatus": "DELETE_COMPLETE"}]}),
    ])
    calls = []
    destroyer = AwsStackEnvironmentDestroyer(resources=resource(whole_environment=inv), clients={"cloudformation": cf}, before_delete=lambda: calls.append("guard"))
    destroyer._plan = SimpleNamespace()
    with pytest.raises(EnvironmentDestructionError, match="requested"):
        destroyer._delete_stack(DATA)
    assert calls == ["guard", "guard"]
    destroyer._delete_stack(DATA)
    assert not cf.script


def test_scheduled_secret_recovery_window_is_still_partial():
    inv = inventory()
    secret = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:customer-secret"
    inv["resources"] = [{"type": "AWS::SecretsManager::Secret", "physical_id": secret, "logical_id": "secret", "stack_id": DATA}]
    sm = ScriptedClient([
        ("describe_secret", {"SecretId": secret}, {"ARN": secret}),
        ("delete_secret", {"SecretId": secret, "RecoveryWindowInDays": 30}, {"ARN": secret, "DeletionDate": NOW}),
    ])
    destroyer = AwsStackEnvironmentDestroyer(resources=resource(whole_environment=inv), clients={"secretsmanager": sm, "logs": ScriptedClient()}, before_delete=lambda: None)
    destroyer._plan = SimpleNamespace()
    destroyer._binding = lambda registration: destroyer.resources
    destroyer._verify_stack_membership = lambda: None
    destroyer._verify_copies = lambda: None
    with pytest.raises(EnvironmentDestructionError, match="still exist"):
        destroyer.delete_environment(None)
    assert not sm.script


class ReceiptStore:
    def __init__(self):
        self.rows = []
    def inspect(self, environment_id):
        return SimpleNamespace(hold=False)
    def record_disposition_rehearsal(self, **row):
        self.rows.append(row)
        return row
    def disposition_rehearsal_receipts(self, environment_id, operation_id):
        return tuple(r for r in self.rows if r["operation_id"] == operation_id)


def rehearsal_spec():
    return RestoreRehearsal("restore-one", "environment", "db-CUSTOMER", resource().db_instance_arn,
        NOW, "isolated-restore", "isolated-subnets", ("sg-isolated",),
        {"earlier_state": result_digest([["known"]]), "later_mutation": result_digest([]), "workflow_receipts": result_digest([["receipt"]])},
        {"object": "1" * 64})


def test_restore_request_uses_observed_pitr_window_and_persists_pending():
    r = resource()
    spec = rehearsal_spec()
    target_query = {"DBInstanceIdentifier": spec.target_identifier}
    rds = ScriptedClient([
        ("describe_db_instances", target_query, ScriptedClient.NotFound()),
        ("describe_db_instances", target_query, ScriptedClient.NotFound()),
        ("describe_db_instances", {"Filters": [{"Name": "dbi-resource-id", "Values": [r.db_resource_id]}]}, {"DBInstances": [{"DBInstanceArn": r.db_instance_arn}]}),
        ("describe_db_instance_automated_backups", {"DbiResourceId": r.db_resource_id}, {"DBInstanceAutomatedBackups": [{"DbiResourceId": r.db_resource_id, "RestoreWindow": {"EarliestTime": NOW, "LatestTime": NOW}}]}),
        ("restore_db_instance_to_point_in_time", {"SourceDbiResourceId": r.db_resource_id, "TargetDBInstanceIdentifier": spec.target_identifier,
            "RestoreTime": NOW, "UseLatestRestorableTime": False, "DBSubnetGroupName": "isolated-subnets", "VpcSecurityGroupIds": ["sg-isolated"],
            "PubliclyAccessible": False, "DeletionProtection": False, "CopyTagsToSnapshot": True, "Tags": [{"Key": "corridor:rehearsal", "Value": spec.sha256}]},
            {"DBInstance": {"DbiResourceId": "db-RESTORED", "DBInstanceIdentifier": spec.target_identifier}}),
    ])
    sts = ScriptedClient([("get_caller_identity", {}, {"Account": ACCOUNT})])
    store = ReceiptStore()
    destroyer = AwsStackEnvironmentDestroyer(live_activation="live-aws-535", resources=r, clients={"sts": sts, "rds": rds})
    runner = AwsRestoreRehearsal(destroyer=destroyer, control_plane=store, specification=spec,
        clock=SimpleNamespace(now=lambda: NOW), before_mutation=lambda: None)
    receipt = runner.restore()
    assert receipt["outcome"] == "pending"
    assert receipt["evidence"]["target_resource_id"] == "db-RESTORED"
    assert all(row["outcome"] == "pending" for row in store.rows)
    assert not rds.script and not sts.script


def test_rehearsal_cannot_replace_probes_with_boolean_claims():
    spec = rehearsal_spec()
    bad = replace(spec, expected_query_digests={"earlier_state": True, "later_mutation": True, "workflow_receipts": True})
    with pytest.raises(DispositionRefused, match="excluding"):
        bad.validate(resource())


def test_whole_environment_receipt_requires_final_observation_of_every_population():
    from corridor.control_plane import EnvironmentRegistration
    inv = inventory()
    secret = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:customer-secret"
    ids = [("AWS::RDS::DBInstance", "customer-db"), ("AWS::RDS::DBSubnetGroup", "subnets"),
           ("AWS::S3::Bucket", "artifact-bucket"), ("AWS::S3::Bucket", "artifact-logs"), ("AWS::S3::Bucket", "alb-logs"),
           ("AWS::SecretsManager::Secret", secret), ("AWS::Logs::LogGroup", "customer-logs"),
           ("AWS::ECR::Repository", "customer-image")]
    inv["resources"] = [{"type": kind, "physical_id": name, "logical_id": str(index), "stack_id": DATA} for index, (kind, name) in enumerate(ids)]
    inv["implicit_log_groups"] = ["/aws/ecs/containerinsights/customer/performance"]
    r = resource(whole_environment=inv)
    registration = EnvironmentRegistration("customer", "environment", "deployment", "db.example", 5432, "corridor",
        "env:WEB", "env:WORKER", "s3:artifact-bucket", "configuration:none", enabled=False)
    db_filter = {"Filters": [{"Name": "dbi-resource-id", "Values": [r.db_resource_id]}]}
    snapshots = {"SnapshotType": "manual", **db_filter}
    rds = ScriptedClient([
        ("describe_db_instances", db_filter, {}),
        ("describe_db_snapshots", snapshots, {}),
        ("describe_db_instance_automated_backups", {"DbiResourceId": r.db_resource_id}, ScriptedClient.NotFound()),
        ("describe_db_subnet_groups", {"DBSubnetGroupName": "subnets"}, ScriptedClient.NotFound()),
        ("describe_db_instances", db_filter, {}),
        ("describe_db_snapshots", snapshots, {}),
        ("describe_db_instance_automated_backups", {"DbiResourceId": r.db_resource_id}, ScriptedClient.NotFound()),
    ])
    sts = ScriptedClient([("get_caller_identity", {}, {"Account": ACCOUNT})] * 5)
    backup = ScriptedClient([("list_recovery_points_by_resource", {"ResourceArn": r.db_instance_arn}, {})])
    clients = {
        "sts": sts, "rds": rds, "kms": ScriptedClient(), "ecs": ScriptedClient(),
        "ec2": ScriptedClient([("describe_regions", {"AllRegions": False}, {"Regions": [{"RegionName": "us-east-1"}]})]),
        "s3": ScriptedClient([("list_buckets", {}, {})] * 4),
        "secretsmanager": ScriptedClient([("describe_secret", {"SecretId": secret}, ScriptedClient.NotFound())] * 2),
        "logs": ScriptedClient([("describe_log_groups", {"logGroupNamePrefix": name}, {}) for name in ("customer-logs", inv["implicit_log_groups"][0])]),
        "ecr": ScriptedClient([("describe_repositories", {"repositoryNames": ["customer-image"], "registryId": ACCOUNT}, ScriptedClient.NotFound())]),
        "cloudformation": ScriptedClient([
            ("list_stack_resources", {"StackName": APP}, {}), ("list_stack_resources", {"StackName": DATA}, {}),
            ("describe_stacks", {"StackName": DATA}, {"Stacks": [{"StackId": DATA, "StackStatus": "DELETE_COMPLETE"}]}),
        ]),
    }
    clients["regional"] = {"us-east-1": {"rds": rds, "sts": sts, "backup": backup}}
    destroyer = AwsStackEnvironmentDestroyer(live_activation="live-aws-535", resources=r,
        approved_resource_sha256=r.sha256, clients=clients)
    assert "/whole-environment-absent/" in destroyer.delete_environment(registration)
    assert all(not client.script for key, client in clients.items() if key != "regional")
    assert not backup.script


def test_rehearsal_receipts_survive_customer_disposition_and_are_immutable(customer_environment_databases):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from corridor.control_plane import ControlPlane, EnvironmentRegistration
    from corridor.control_plane_schema import initialize_control_plane
    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    registry = ControlPlane(owner)
    registry.register(EnvironmentRegistration("customer", "rehearsal-environment", "deployment", "db.example", 5432,
        "corridor_rehearsal", "env:WEB", "env:WORKER", "s3:artifact-rehearsal", "configuration:none", enabled=False))
    receipt = dict(receipt_id="restore-observed", environment_id="rehearsal-environment", operation_id="restore-one",
        phase="restore", outcome="pending", evidence={"spec_sha256": "1"*64, "target_resource_id": "db-TEMP"}, observed_at=NOW)
    registry.record_disposition_rehearsal(**receipt)
    registry.record_disposition_rehearsal(**receipt)
    assert registry.disposition_rehearsal_receipts("rehearsal-environment", "restore-one") == (receipt,)
    with pytest.raises(ValueError, match="another observation"):
        registry.record_disposition_rehearsal(**{**receipt, "outcome": "completed"})
    with pytest.raises(DBAPIError, match="immutable"), owner.begin() as connection:
        connection.execute(text("delete from control_plane.disposition_rehearsal_receipts where receipt_id='restore-observed'"))


def test_restore_probe_uses_corridor_object_store_digest_contract(tmp_path):
    from contextlib import nullcontext
    from hashlib import sha256
    from corridor.environment_rehearsal import sql_restore_probe
    from corridor.object_storage import LocalFilesystemStore, content_key
    storage = LocalFilesystemStore(tmp_path / "objects")
    checksum = sha256(b"retained source").hexdigest()
    key = content_key(checksum, ".txt")
    storage.put(key, b"retained source", sha256=checksum)
    statements = []
    class Connection:
        info = SimpleNamespace(host="restore.example", port=5432, dbname="corridor")
        def transaction(self):
            return nullcontext()
        def execute(self, sql):
            statements.append(sql)
            return SimpleNamespace(fetchall=lambda: [("earlier",)])
    observed = sql_restore_probe(Connection(), target={"Endpoint": {"Address": "restore.example", "Port": 5432}, "DBName": "corridor"},
        queries={"earlier_state": "select fixture from synthetic_rehearsal"}, object_store=storage, object_digests={key: checksum})
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert observed == {"query_digests": {"earlier_state": result_digest([("earlier",)])}, "object_digests": {key: checksum}}


def test_export_replaces_destination_atomically_with_private_permissions(tmp_path):
    import os
    import stat
    output = tmp_path / "export.tar"
    output.write_bytes(b"previous verified export")
    output.chmod(0o644)
    pgpass = tmp_path / "pgpass"
    pgpass.touch()
    params = {"Bucket": "artifact-bucket", "ExpectedBucketOwner": ACCOUNT}
    s3 = ScriptedClient([("get_bucket_versioning", params, {"Status": "Enabled"}),
        ("list_object_versions", params, {}), ("list_object_versions", params, {})])
    observations = []
    def before_export():
        assert output.read_bytes() == b"previous verified export"
        staged = list(tmp_path.glob(".export.tar.*.partial"))
        if staged:
            assert len(staged) == 1
            assert stat.S_IMODE(staged[0].stat().st_mode) == 0o600
            observations.append("private staged archive")
    def runner(command, **options):
        Path(command[command.index("--file")+1]).write_bytes(b"PGDMPfixture")
    previous_umask = os.umask(0)
    try:
        result = export_environment_archive(resources=resource(), inventory=inventory(), clients={"s3": s3},
            output_path=output, pgpass_file=pgpass, database_username="export_reader",
            principal=HumanPrincipal("local:operator"), before_export=before_export, run=runner, restore_run=fixture_restore_parser)
    finally:
        os.umask(previous_umask)
    assert result["path"] == str(output)
    assert observations == ["private staged archive"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_bytes() != b"previous verified export"
    assert list(tmp_path.glob(".export.tar.*.partial")) == []


@pytest.mark.parametrize("failure_phase", ["source_read", "archive_verification", "fsync"])
def test_export_failure_preserves_existing_destination_and_removes_partial(tmp_path, monkeypatch, failure_phase):
    import os
    import corridor.environment_export as export_module
    output = tmp_path / "export.tar"
    output.write_bytes(b"previous verified export")
    output.chmod(0o600)
    previous_stat = output.stat()
    pgpass = tmp_path / "pgpass"
    pgpass.touch()
    params = {"Bucket": "artifact-bucket", "ExpectedBucketOwner": ACCOUNT}
    rows = {"Versions": [{"Key": "source", "VersionId": "one"}]} if failure_phase == "source_read" else {}
    script = [("get_bucket_versioning", params, {"Status": "Enabled"}), ("list_object_versions", params, rows)]
    if failure_phase == "source_read":
        script.append(("get_object", {**params, "Key": "source", "VersionId": "one"}, OSError("injected failure")))
    else:
        script.append(("list_object_versions", params, rows))
    s3 = ScriptedClient(script)
    def fail(*args, **kwargs):
        raise OSError("injected failure")
    if failure_phase == "archive_verification":
        monkeypatch.setattr(export_module, "verify_export_archive", fail)
    elif failure_phase == "fsync":
        monkeypatch.setattr(os, "fsync", fail)
    def runner(command, **options):
        Path(command[command.index("--file")+1]).write_bytes(b"PGDMPfixture")
    with pytest.raises(OSError, match="injected failure"):
        export_environment_archive(resources=resource(), inventory=inventory(), clients={"s3": s3},
            output_path=output, pgpass_file=pgpass, database_username="export_reader",
            principal=HumanPrincipal("local:operator"), before_export=lambda: None, run=runner, restore_run=fixture_restore_parser)
    assert output.read_bytes() == b"previous verified export"
    assert output.stat().st_ino == previous_stat.st_ino
    assert output.stat().st_mode == previous_stat.st_mode
    assert list(tmp_path.glob(".export.tar.*.partial")) == []
    assert not s3.script


def test_execution_boundary_rechecks_exports_and_only_allows_disappearance_on_resume(monkeypatch):
    from corridor.environment_disposition import ReferentialRetention
    import corridor.aws_environment_disposition as provider
    inv = inventory()
    inv["custody"] = {"sha256": "f" * 64}
    manifest = {"object_versions": [{"key": "source", "version_id": "one", "delete_marker": False}],
                "database_validation": {"method": "pg-restore-full-stream-v1", "database_sha256": "e" * 64}}
    monkeypatch.setattr(provider, "verify_custody", lambda *args: manifest)
    params = {"Bucket": "artifact-bucket", "ExpectedBucketOwner": ACCOUNT}
    expected = {"Versions": [{"Key": "source", "VersionId": "one"}]}
    changed = {"Versions": [{"Key": "source", "VersionId": "one"}, {"Key": "late", "VersionId": "two"}]}
    s3 = ScriptedClient([("list_object_versions", params, changed),
        ("list_object_versions", params, expected), ("list_object_versions", params, changed),
        ("list_object_versions", params, {})])
    destroyer = AwsStackEnvironmentDestroyer(resources=resource(whole_environment=inv), clients={"s3": s3})
    destroyer._verify_stack_membership = lambda: None
    replica_observation = {"db_resource_id": "db-CUSTOMER", "read_replica_instances": [], "read_replica_clusters": [], "read_replica_source": None}
    destroyer._verify_copies = lambda: replica_observation
    destroyer._require_quiescent = lambda: None
    destroyer._owned_buckets = lambda: {"artifact-bucket"}
    plan = SimpleNamespace(environment_id="environment", manifest_sha256="a" * 64)
    store = ReceiptStore()
    def prepare():
        destroyer.prepare_execution(store, plan, ReferentialRetention(), "dispose-one", observed_at=NOW)
    with pytest.raises(DispositionRefused, match="source versions changed"):
        prepare()
    assert not store.rows and not hasattr(destroyer, "_plan")
    prepare()
    assert len(store.rows) == 1 and store.rows[0]["phase"] == "execution_boundary"
    assert store.rows[0]["evidence"]["rds_replica_relationships"] == replica_observation
    with pytest.raises(DispositionRefused, match="source versions changed"):
        prepare()
    prepare()  # already exported versions may have been removed by a partial pass
    assert len(store.rows) == 1
    assert not s3.script
    plan.manifest_sha256 = "b" * 64
    with pytest.raises(DispositionRefused, match="another plan"):
        prepare()


@pytest.mark.parametrize("field", ["ReadReplicaDBInstanceIdentifiers", "ReadReplicaDBClusterIdentifiers"])
def test_live_rds_replicas_refuse_source_deletion_even_without_backup_copies(field):
    from corridor.control_plane import EnvironmentRegistration
    from corridor.environment_disposition import AwsEnvironmentDestroyer
    r = resource()
    query = {"Filters": [{"Name": "dbi-resource-id", "Values": [r.db_resource_id]}]}
    rds = ScriptedClient([("describe_db_instances", query, {"DBInstances": [{
        "DBInstanceArn": r.db_instance_arn, "DbiResourceId": r.db_resource_id,
        "DBInstanceStatus": "available", field: ["live-replica"]}]})])
    clients = {"rds": rds, "sts": ScriptedClient([("get_caller_identity", {}, {"Account": ACCOUNT})]),
               "s3": ScriptedClient(), "kms": ScriptedClient()}
    registration = EnvironmentRegistration("customer", "environment", "deployment", "db.example", 5432,
        "corridor", "env:WEB", "env:WORKER", "s3:artifact-bucket", "configuration:none", enabled=False)
    destroyer = AwsEnvironmentDestroyer(resources=r, approved_resource_sha256=r.sha256,
        live_activation="live-aws-535", clients=clients, before_delete=lambda: pytest.fail("must not mutate"))
    with pytest.raises(DispositionRefused, match="live RDS replicas"):
        destroyer.delete_database(registration)
    assert not rds.script


def test_rehearsal_cleanup_follows_the_persisted_physical_database_after_rename():
    spec = rehearsal_spec()
    target_arn = f"arn:aws:rds:us-east-1:{ACCOUNT}:db:renamed-rehearsal"
    rds = ScriptedClient([
        ("describe_db_instances", {"Filters": [{"Name": "dbi-resource-id", "Values": ["db-RESTORED"]}]},
            {"DBInstances": [{"DBInstanceIdentifier": "renamed-rehearsal", "DBInstanceArn": target_arn,
                              "DbiResourceId": "db-RESTORED", "DBInstanceStatus": "available"}]}),
        ("list_tags_for_resource", {"ResourceName": target_arn}, {"TagList": [{"Key": "corridor:rehearsal", "Value": spec.sha256}]}),
        ("delete_db_instance", {"DBInstanceIdentifier": "renamed-rehearsal", "SkipFinalSnapshot": True, "DeleteAutomatedBackups": True}, {}),
    ])
    store = ReceiptStore()
    store.record_disposition_rehearsal(environment_id="environment", operation_id=spec.operation_id,
        phase="state_verification", outcome="completed", evidence={"spec_sha256": spec.sha256, "target_resource_id": "db-RESTORED"})
    destroyer = AwsStackEnvironmentDestroyer(resources=resource(), live_activation="live-aws-535",
        clients={"rds": rds, "sts": ScriptedClient([("get_caller_identity", {}, {"Account": ACCOUNT})])})
    runner = AwsRestoreRehearsal(destroyer=destroyer, control_plane=store, specification=spec,
        clock=SimpleNamespace(now=lambda: NOW), before_mutation=lambda: None)
    receipt = runner.cleanup()
    assert receipt["outcome"] == "pending"
    assert receipt["evidence"]["target_identifier"] == "renamed-rehearsal"
    assert not any(row["phase"] == "cleanup" and row["outcome"] == "completed" for row in store.rows)
    assert not rds.script


def _archive_containing_dump(database_bytes):
    from hashlib import sha256
    import tarfile
    checksum = sha256(database_bytes).hexdigest()
    manifest = {"schema": "corridor-environment-export-v1", "environment_id": "environment",
        "inventory_sha256": digest(inventory()), "members": [{"name": "database.dump", "kind": "postgresql", "sha256": checksum, "size": len(database_bytes)}],
        "object_versions": [], "database_validation": {"method": "pg-restore-full-stream-v1", "database_sha256": checksum, "database_size": len(database_bytes)}}
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, body in (("database.dump", database_bytes), ("manifest.json", json.dumps(manifest).encode())):
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            archive.addfile(entry, BytesIO(body))
    data = output.getvalue()
    return data, sha256(data).hexdigest()


def test_custody_rejects_self_consistent_five_byte_pg_dump_archive():
    data, checksum = _archive_containing_dump(b"PGDMP")
    with pytest.raises(DispositionRefused, match="full archive validation failed"):
        verify_export_archive(BytesIO(data), expected_sha256=checksum, environment_id="environment", inventory_sha256=digest(inventory()))


def test_custody_consumes_real_dump_data_blocks_beyond_the_table_of_contents(runtime_database, tmp_path):
    import os
    import subprocess
    from sqlalchemy import text
    engine = runtime_database.session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(text("create table public.disposition_dump_probe (payload text not null)"))
        connection.execute(text("insert into public.disposition_dump_probe values (:payload)"), {"payload": os.urandom(16384).hex()})
    url = engine.url
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment["PGPASSWORD"] = url.password or ""
    output = tmp_path / "valid.dump"
    from corridor.rehearsal_environment import _compose_service_is_running
    # Use the same provider-native PostgreSQL clients as the existing rehearsal
    # harness. macOS's unrelated Homebrew14 client cannot dump the Docker16 DB.
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], check=True, capture_output=True, text=True).stdout.strip()
    compose_root = Path(common).resolve().parent
    use_compose = url.host in {"localhost", "127.0.0.1"} and url.port == 5433 and _compose_service_is_running(compose_root)
    dump_command = (["docker", "compose", "exec", "-T", "postgres", "pg_dump"] if use_compose else ["pg_dump", "--host", url.host, "--port", str(url.port)])
    with output.open("wb") as destination:
        subprocess.run([*dump_command, "--format=custom", "--no-password", "--username", url.username,
            "--dbname", url.database, "--table=public.disposition_dump_probe"], env=environment,
            cwd=compose_root, check=True, stdout=destination, stderr=subprocess.PIPE)
    def restore_runner(command, **options):
        if not use_compose:
            return subprocess.run(command, **options)
        with Path(command[-1]).open("rb") as source:
            return subprocess.run(["docker", "compose", "exec", "-T", "postgres", "pg_restore", *command[1:-1]],
                stdin=source, cwd=compose_root, check=options.get("check", True), capture_output=True)
    data, checksum = _archive_containing_dump(output.read_bytes())
    verified = verify_export_archive(BytesIO(data), expected_sha256=checksum, environment_id="environment", inventory_sha256=digest(inventory()), restore_run=restore_runner)
    assert verified["database_validation"]["method"] == "pg-restore-full-stream-v1"
    truncated = output.read_bytes()[:-32]
    output.write_bytes(truncated)
    # The TOC remains readable while the data block is incomplete. A --list
    # check would incorrectly accept these customer export bytes.
    restore_runner(["pg_restore", "--list", str(output)], check=True, capture_output=True)
    data, checksum = _archive_containing_dump(truncated)
    with pytest.raises(DispositionRefused, match="full archive validation failed"):
        verify_export_archive(BytesIO(data), expected_sha256=checksum, environment_id="environment", inventory_sha256=digest(inventory()), restore_run=restore_runner)


def test_disposition_gate_requires_real_final_receipt_and_same_spec_rehearsal():
    from corridor.control_plane import DestructionReceipt
    from corridor.disposition_evidence import disposition_gate_payload
    provider_resources = json.loads(json.dumps(__import__("dataclasses").asdict(resource(whole_environment=inventory()))))
    inventory_sha = digest(provider_resources)
    plan = SimpleNamespace(status="executed", environment_id="environment", manifest_sha256="a" * 64,
        provider_resources_sha256=inventory_sha, provider_resources=provider_resources)
    final = DestructionReceipt("environment-final", "environment", "dispose-one", "environment", "completed",
        f"aws:environment/whole-environment-absent/{inventory_sha}/{plan.manifest_sha256}", "local:operator", NOW)
    rows = [{"receipt_id": f"phase-{phase}", "environment_id": "environment", "operation_id": "rehearsal-one",
             "phase": phase, "outcome": "completed", "observed_at": NOW, "evidence": {"spec_sha256": "b" * 64}}
            for phase in ("restore", "state_verification", "cleanup", "backup_expiration")]
    store = SimpleNamespace(disposition_plan=lambda plan_id: plan, destruction_receipts=lambda env: [final],
                            disposition_rehearsal_receipts=lambda env, op: rows)
    arguments = dict(plan_id="plan-one", operation_id="dispose-one", rehearsal_operation_id="rehearsal-one",
                     configuration_identity="configured-identity", inventory_digest=inventory_sha)
    result = disposition_gate_payload(store, **arguments)
    assert result["gate"] == "disposition" and result["configuration"] == "configured-identity"
    assert result["external_receipt_reference"] == "control-plane:destruction/environment-final"
    assert result["observed_at"] == NOW.isoformat()
    rows[-1]["outcome"] = "pending"
    with pytest.raises(DispositionRefused, match="not completed"):
        disposition_gate_payload(store, **arguments)
    rows[-1]["outcome"] = "completed"
    rows[-1]["evidence"]["spec_sha256"] = "c" * 64
    with pytest.raises(DispositionRefused, match="one pinned"):
        disposition_gate_payload(store, **arguments)
    final = replace(final, evidence_ref="synthetic:environment/gone")
    with pytest.raises(DispositionRefused, match="exact AWS"):
        disposition_gate_payload(store, **arguments)


def test_disposition_cli_refuses_aws_without_authorization_before_client_creation(tmp_path, monkeypatch):
    from dataclasses import asdict
    import boto3
    import corridor.environment_disposition_cli as cli
    configuration = tmp_path / "configuration.json"
    configuration.write_text(json.dumps({"resources": asdict(resource())}))
    monkeypatch.setenv("DISPOSITION_TEST_CONTROL_URL", "postgresql+psycopg://fixture@127.0.0.1:9/unopened")
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: pytest.fail("must not construct AWS session"))
    output = tmp_path / "output.json"
    assert cli.main(["inventory", "--configuration", str(configuration), "--output", str(output),
        "--control-plane-url-env", "DISPOSITION_TEST_CONTROL_URL", "--aws-profile", "fixture"]) == 2
    assert not output.exists()


def test_disposition_cli_status_reads_control_plane_without_aws_and_writes_private_artifact(tmp_path, monkeypatch):
    from dataclasses import asdict
    import stat
    import sqlalchemy
    import corridor.environment_disposition_cli as cli
    configuration = tmp_path / "configuration.json"
    configuration.write_text(json.dumps({"resources": asdict(resource())}))
    monkeypatch.setenv("DISPOSITION_TEST_CONTROL_URL", "postgresql+psycopg://fixture@127.0.0.1:9/unopened")
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(cli, "ControlPlane", lambda engine: SimpleNamespace(disposition_plans=lambda env: [], destruction_receipts=lambda env: []))
    monkeypatch.setattr(cli, "_provider_clients", lambda *args, **kwargs: pytest.fail("status must not load AWS clients"))
    output = tmp_path / "status.json"
    assert cli.main(["status", "--configuration", str(configuration), "--output", str(output),
        "--control-plane-url-env", "DISPOSITION_TEST_CONTROL_URL"]) == 0
    assert json.loads(output.read_text())["environment_id"] == "environment"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_automated_backup_absence_uses_only_the_modeled_sdk_not_found():
    import boto3
    from botocore.exceptions import ClientError
    from botocore.stub import Stubber
    from corridor.disposition_contracts import automated_backup_rows
    client = boto3.client("rds", region_name="us-east-1", aws_access_key_id="fixture",
                          aws_secret_access_key="fixture", endpoint_url="http://127.0.0.1:9")
    try:
        with Stubber(client) as stub:
            parameters = {"DbiResourceId": "db-RESTORED"}
            stub.add_client_error("describe_db_instance_automated_backups", "DBInstanceAutomatedBackupNotFound",
                                  http_status_code=404, expected_params=parameters)
            assert automated_backup_rows(client, "db-RESTORED") == []
            stub.add_client_error("describe_db_instance_automated_backups", "AccessDenied",
                                  http_status_code=403, expected_params=parameters)
            with pytest.raises(ClientError):
                automated_backup_rows(client, "db-RESTORED")
            stub.add_response("describe_db_instance_automated_backups",
                {"DBInstanceAutomatedBackups": [{"DbiResourceId": "db-RESTORED"}], "Marker": "next"}, parameters)
            stub.add_client_error("describe_db_instance_automated_backups", "DBInstanceAutomatedBackupNotFound",
                                  http_status_code=404, expected_params={**parameters, "Marker": "next"})
            with pytest.raises(DispositionRefused, match="already observed"):
                automated_backup_rows(client, "db-RESTORED")
            stub.assert_no_pending_responses()
    finally:
        client.close()


def test_rehearsal_cleanup_converges_on_modeled_automated_backup_absence():
    spec = rehearsal_spec()
    rds = ScriptedClient([
        ("describe_db_instances", {"Filters": [{"Name": "dbi-resource-id", "Values": ["db-RESTORED"]}]}, {}),
        ("describe_db_snapshots", {"SnapshotType": "manual", "Filters": [{"Name": "dbi-resource-id", "Values": ["db-RESTORED"]}]}, {}),
        ("describe_db_instance_automated_backups", {"DbiResourceId": "db-RESTORED"}, ScriptedClient.NotFound()),
    ])
    store = ReceiptStore()
    store.record_disposition_rehearsal(environment_id="environment", operation_id=spec.operation_id,
        phase="state_verification", outcome="completed", evidence={"spec_sha256": spec.sha256, "target_resource_id": "db-RESTORED"})
    destroyer = AwsStackEnvironmentDestroyer(resources=resource(), live_activation="live-aws-535",
        clients={"rds": rds, "sts": ScriptedClient([("get_caller_identity", {}, {"Account": ACCOUNT})])})
    runner = AwsRestoreRehearsal(destroyer=destroyer, control_plane=store, specification=spec,
        clock=SimpleNamespace(now=lambda: NOW), before_mutation=lambda: pytest.fail("nothing remains to delete"))
    receipt = runner.cleanup()
    assert receipt["phase"] == "backup_expiration" and receipt["outcome"] == "completed"
    assert receipt["evidence"]["automated_backup_count"] == 0
    assert not rds.script


def test_restore_retry_follows_the_first_observed_physical_target_after_rename():
    spec = rehearsal_spec()
    target_arn = f"arn:aws:rds:us-east-1:{ACCOUNT}:db:renamed-rehearsal"
    target = {"DBInstanceArn": target_arn, "DBInstanceIdentifier": "renamed-rehearsal", "DbiResourceId": "db-RESTORED",
              "DBInstanceStatus": "available", "PubliclyAccessible": False,
              "DBSubnetGroup": {"DBSubnetGroupName": spec.subnet_group}, "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-isolated"}]}
    rds = ScriptedClient([
        ("describe_db_instances", {"Filters": [{"Name": "dbi-resource-id", "Values": ["db-RESTORED"]}]}, {"DBInstances": [target]}),
        ("list_tags_for_resource", {"ResourceName": target_arn}, {"TagList": [{"Key": "corridor:rehearsal", "Value": spec.sha256}]}),
    ])
    store = ReceiptStore()
    store.record_disposition_rehearsal(environment_id="environment", operation_id=spec.operation_id,
        phase="restore", outcome="pending", evidence={"spec_sha256": spec.sha256, "target_resource_id": "db-RESTORED"})
    destroyer = AwsStackEnvironmentDestroyer(resources=resource(), live_activation="live-aws-535",
        clients={"rds": rds, "sts": ScriptedClient([("get_caller_identity", {}, {"Account": ACCOUNT})])})
    runner = AwsRestoreRehearsal(destroyer=destroyer, control_plane=store, specification=spec,
        clock=SimpleNamespace(now=lambda: NOW), before_mutation=lambda: pytest.fail("must not create another restore"))
    receipt = runner.restore()
    assert receipt["outcome"] == "completed"
    assert receipt["evidence"]["target_resource_id"] == "db-RESTORED"
    assert receipt["evidence"]["target_arn"] == target_arn
    assert not rds.script


@pytest.mark.parametrize("observed_target", [None, {"DbiResourceId": "db-REPLACEMENT"}])
def test_restore_never_recreates_or_rebinds_an_already_observed_target(observed_target):
    spec = rehearsal_spec()
    rds = ScriptedClient([("describe_db_instances", {"Filters": [{"Name": "dbi-resource-id", "Values": ["db-RESTORED"]}]},
                          {"DBInstances": [] if observed_target is None else [observed_target]})])
    store = ReceiptStore()
    store.record_disposition_rehearsal(environment_id="environment", operation_id=spec.operation_id,
        phase="restore", outcome="pending", evidence={"spec_sha256": spec.sha256, "target_resource_id": "db-RESTORED"})
    destroyer = AwsStackEnvironmentDestroyer(resources=resource(), live_activation="live-aws-535",
        clients={"rds": rds, "sts": ScriptedClient([("get_caller_identity", {}, {"Account": ACCOUNT})])})
    runner = AwsRestoreRehearsal(destroyer=destroyer, control_plane=store, specification=spec,
        clock=SimpleNamespace(now=lambda: NOW), before_mutation=lambda: pytest.fail("must not create another restore"))
    with pytest.raises(DispositionRefused, match="(cannot create another|different physical)"):
        runner.restore()
    assert len(store.rows) == 1
    assert not rds.script
