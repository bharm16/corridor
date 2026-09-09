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
        unavailability_disclosure=b"The retained record discloses transferred source custody.", run=runner)
    assert calls == ["freeze", "freeze"]
    assert len(output["manifest"]["members"]) == 3
    assert not s3.script
    with open(output["path"], "rb") as stream:
        manifest = verify_export_archive(stream, expected_sha256=output["sha256"], environment_id="environment", inventory_sha256=digest(inventory()))
    assert manifest["object_versions"] == [{"key": "old", "version_id": "deleted", "delete_marker": True}, {"key": "source", "version_id": "version-one", "delete_marker": False}]
    assert b"fixture credentials" not in Path(output["path"]).read_bytes()
    with open(output["path"], "rb") as stream, pytest.raises(DispositionRefused, match="digest"):
        verify_export_archive(stream, expected_sha256="0" * 64, environment_id="environment", inventory_sha256=digest(inventory()))


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
            pgpass_file=pgpass, database_username="export_reader", principal=HumanPrincipal("local:operator"), before_export=lambda: None, run=runner)
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
            "PubliclyAccessible": False, "DeletionProtection": False, "CopyTagsToSnapshot": True, "Tags": [{"Key": "corridor:rehearsal", "Value": spec.sha256}]}, {}),
    ])
    sts = ScriptedClient([("get_caller_identity", {}, {"Account": ACCOUNT})])
    store = ReceiptStore()
    destroyer = AwsStackEnvironmentDestroyer(live_activation="live-aws-535", resources=r, clients={"sts": sts, "rds": rds})
    runner = AwsRestoreRehearsal(destroyer=destroyer, control_plane=store, specification=spec,
        clock=SimpleNamespace(now=lambda: NOW), before_mutation=lambda: None)
    receipt = runner.restore()
    assert receipt["outcome"] == "pending"
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
        ("describe_db_snapshots", snapshots, {}),
        ("describe_db_instance_automated_backups", {"DbiResourceId": r.db_resource_id}, {}),
        ("describe_db_subnet_groups", {"DBSubnetGroupName": "subnets"}, ScriptedClient.NotFound()),
        ("describe_db_instances", db_filter, {}),
        ("describe_db_snapshots", snapshots, {}),
        ("describe_db_instance_automated_backups", {"DbiResourceId": r.db_resource_id}, {}),
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
    from corridor.object_storage import LocalFilesystemStore
    storage = LocalFilesystemStore(tmp_path / "objects")
    checksum = sha256(b"retained source").hexdigest()
    storage.put("source", b"retained source", sha256=checksum)
    statements = []
    class Connection:
        info = SimpleNamespace(host="restore.example", port=5432, dbname="corridor")
        def transaction(self):
            return nullcontext()
        def execute(self, sql):
            statements.append(sql)
            return SimpleNamespace(fetchall=lambda: [("earlier",)])
    observed = sql_restore_probe(Connection(), target={"Endpoint": {"Address": "restore.example", "Port": 5432}, "DBName": "corridor"},
        queries={"earlier_state": "select fixture from synthetic_rehearsal"}, object_store=storage, object_digests={"source": checksum})
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert observed == {"query_digests": {"earlier_state": result_digest([("earlier",)])}, "object_digests": {"source": checksum}}


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
            principal=HumanPrincipal("local:operator"), before_export=before_export, run=runner)
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
            principal=HumanPrincipal("local:operator"), before_export=lambda: None, run=runner)
    assert output.read_bytes() == b"previous verified export"
    assert output.stat().st_ino == previous_stat.st_ino
    assert output.stat().st_mode == previous_stat.st_mode
    assert list(tmp_path.glob(".export.tar.*.partial")) == []
    assert not s3.script
