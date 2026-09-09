"""Resumable AWS PITR and backup-expiration rehearsal software (#514).

A checklist and 'earlier_state_present=True' cannot prove a restore. This module
pins a restore point, isolated destination, expected query-result digests and
object bytes before requesting RDS restoration. Every asynchronous observation
is retained in the separate control plane. Successful restoration, semantic
checks and eventual temporary-backup absence are separate phases. The module
creates no clients; callers supply explicitly activated scoped SDK clients.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from uuid import uuid4

from corridor.environment_disposition import DispositionRefused
from corridor.aws_environment_disposition import _pages, digest


def result_digest(rows) -> str:
    """Digest exact ordered rows without persisting their customer content."""
    return sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class RestoreRehearsal:
    operation_id: str
    environment_id: str
    source_resource_id: str
    source_instance_arn: str
    restore_time: datetime
    target_identifier: str
    subnet_group: str
    security_group_ids: tuple[str, ...]
    expected_query_digests: dict[str, str]
    expected_object_digests: dict[str, str]

    @property
    def sha256(self):
        value = asdict(self)
        value["restore_time"] = self.restore_time.isoformat()
        return digest(value)

    def validate(self, resources):
        if (self.environment_id, self.source_resource_id, self.source_instance_arn) != (
                resources.environment_id, resources.db_resource_id, resources.db_instance_arn):
            raise DispositionRefused("restore source differs from the registered environment inventory")
        if self.restore_time.tzinfo is None or self.target_identifier == resources.db_instance_identifier:
            raise DispositionRefused("rehearsal needs an aware restore point and a different target")
        if not self.subnet_group or not self.security_group_ids:
            raise DispositionRefused("restore target needs explicitly approved isolated networking")
        expected = self.expected_query_digests
        if not {"earlier_state", "later_mutation", "workflow_receipts"} <= set(expected):
            raise DispositionRefused("rehearsal needs earlier state, later mutation, and workflow observations")
        if expected["earlier_state"] == result_digest([]) or expected["workflow_receipts"] == result_digest([]) or expected["later_mutation"] != result_digest([]):
            raise DispositionRefused("rehearsal must preserve nonempty earlier state and receipts while excluding the later mutation")
        if not self.expected_object_digests:
            raise DispositionRefused("rehearsal must verify retained object bytes")


def sql_restore_probe(connection, *, target, queries, object_store, object_keys):
    """Read exact test observations from the observed restored DB and object store.

    The operator supplies explicit queries for a known synthetic fixture. No
    production table guesses or boolean attestations enter the receipt. psycopg
    connection identity must match the RDS-described destination before any SQL.
    """
    endpoint = target["Endpoint"]
    if (connection.info.host, connection.info.port, connection.info.dbname) != (
            endpoint["Address"], endpoint["Port"], target["DBName"]):
        raise DispositionRefused("probe connection does not reach the restored target")
    with connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        query_results = {name: result_digest(connection.execute(sql).fetchall()) for name, sql in queries.items()}
    objects = {key: sha256(object_store.read_bytes(key)).hexdigest() for key in object_keys}
    return {"query_digests": query_results, "object_digests": objects}


class AwsRestoreRehearsal:
    def __init__(self, *, destroyer, control_plane, specification, clock, before_mutation):
        self.destroyer = destroyer
        self.control_plane = control_plane
        self.spec = specification
        self.clock = clock
        self.before_mutation = before_mutation

    def _record(self, phase, outcome, **evidence):
        return self.control_plane.record_disposition_rehearsal(
            receipt_id=f"rehearsal-{uuid4().hex}", environment_id=self.spec.environment_id,
            operation_id=self.spec.operation_id, phase=phase, outcome=outcome,
            evidence={"spec_sha256": self.spec.sha256, **evidence}, observed_at=self.clock.now())

    def _mutate(self, operation, **parameters):
        if self.control_plane.inspect(self.spec.environment_id).hold:
            raise DispositionRefused("legal hold suspends rehearsal mutations and cleanup")
        self.before_mutation()
        return operation(**parameters)

    def _binding(self):
        self.spec.validate(self.destroyer.resources)
        self.destroyer._require_activation("restore rehearsal")
        resource = self.destroyer.resources
        clients = self.destroyer._clients
        if clients["sts"].get_caller_identity()["Account"] != resource.account_id or clients["rds"].meta.region_name != resource.region:
            raise DispositionRefused("restore clients differ from the approved AWS account and region")
        previous = self.control_plane.disposition_rehearsal_receipts(self.spec.environment_id, self.spec.operation_id)
        if any(row["evidence"].get("spec_sha256") != self.spec.sha256 for row in previous):
            raise DispositionRefused("rehearsal changed after its first persisted observation")
        return clients["rds"], previous

    def _target(self, client):
        try:
            rows = client.describe_db_instances(DBInstanceIdentifier=self.spec.target_identifier)["DBInstances"]
        except client.exceptions.DBInstanceNotFoundFault:
            return None
        if len(rows) != 1:
            raise DispositionRefused("restore target is not unique")
        target = rows[0]
        expected = {"corridor:rehearsal": self.spec.sha256}
        tags = {r["Key"]: r["Value"] for r in client.list_tags_for_resource(ResourceName=target["DBInstanceArn"])["TagList"]}
        if any(tags.get(key) != value for key, value in expected.items()):
            raise DispositionRefused("target identifier belongs to a different resource")
        return target

    def restore(self):
        client, previous = self._binding()
        if not previous:
            # Check collision before writing intent. On crash/retry the intent
            # and the provider tag together identify our own async request.
            try:
                client.describe_db_instances(DBInstanceIdentifier=self.spec.target_identifier)
            except client.exceptions.DBInstanceNotFoundFault:
                pass
            else:
                raise DispositionRefused("restore target identifier already exists")
            self._record("restore", "pending", target_identifier=self.spec.target_identifier)
        target = self._target(client)
        if target is not None:
            if target["DBInstanceStatus"] != "available":
                return self._record("restore", "pending", target_resource_id=target["DbiResourceId"], provider_status=target["DBInstanceStatus"])
            if target.get("PubliclyAccessible") or target.get("DBSubnetGroup", {}).get("DBSubnetGroupName") != self.spec.subnet_group or {
                    s["VpcSecurityGroupId"] for s in target.get("VpcSecurityGroups", [])} != set(self.spec.security_group_ids):
                raise DispositionRefused("restored target networking differs from the approved isolated target")
            return self._record("restore", "completed", target_resource_id=target["DbiResourceId"], target_arn=target["DBInstanceArn"], restore_time=self.spec.restore_time.isoformat())
        if any(row["phase"] == "cleanup" for row in previous):
            raise DispositionRefused("a cleaned rehearsal cannot silently create another restore")
        rows = _pages(client, "describe_db_instances", "DBInstances", Filters=[{"Name": "dbi-resource-id", "Values": [self.spec.source_resource_id]}])
        if len(rows) != 1 or rows[0]["DBInstanceArn"] != self.spec.source_instance_arn:
            raise DispositionRefused("source physical database differs from rehearsal")
        source_backups = _pages(client, "describe_db_instance_automated_backups", "DBInstanceAutomatedBackups",
                                DbiResourceId=self.spec.source_resource_id)
        windows = [row.get("RestoreWindow", {}) for row in source_backups
                   if row.get("DbiResourceId") == self.spec.source_resource_id]
        if not any(window.get("EarliestTime") and window.get("LatestTime") and
                   window["EarliestTime"] <= self.spec.restore_time <= window["LatestTime"] for window in windows):
            raise DispositionRefused("requested point lies outside the observed PITR window")
        self._mutate(client.restore_db_instance_to_point_in_time,
            SourceDbiResourceId=self.spec.source_resource_id, TargetDBInstanceIdentifier=self.spec.target_identifier,
            RestoreTime=self.spec.restore_time, UseLatestRestorableTime=False,
            DBSubnetGroupName=self.spec.subnet_group, VpcSecurityGroupIds=list(self.spec.security_group_ids),
            PubliclyAccessible=False, DeletionProtection=False, CopyTagsToSnapshot=True,
            Tags=[{"Key": "corridor:rehearsal", "Value": self.spec.sha256}])
        return self._record("restore", "pending", target_identifier=self.spec.target_identifier, provider_status="requested")

    def verify(self, probe):
        client, previous = self._binding()
        completed = [r for r in previous if r["phase"] == "restore" and r["outcome"] == "completed"]
        if not completed:
            raise DispositionRefused("state checks require an observed available restore")
        target = self._target(client)
        if target is None or target["DBInstanceStatus"] != "available" or target["DbiResourceId"] != completed[-1]["evidence"]["target_resource_id"]:
            raise DispositionRefused("restored target changed before verification")
        observed = probe(target)
        if observed != {"query_digests": self.spec.expected_query_digests, "object_digests": self.spec.expected_object_digests}:
            self._record("state_verification", "refused", observation_sha256=digest(observed))
            raise DispositionRefused("restored state, absent later mutation, objects, or workflow receipts differ")
        return self._record("state_verification", "completed", target_resource_id=target["DbiResourceId"], observation_sha256=digest(observed))

    def cleanup(self):
        client, previous = self._binding()
        verified = [r for r in previous if r["phase"] == "state_verification" and r["outcome"] == "completed"]
        if not verified:
            raise DispositionRefused("cleanup requires persisted state verification")
        resource_id = verified[-1]["evidence"]["target_resource_id"]
        target = self._target(client)
        if target is not None:
            if target["DbiResourceId"] != resource_id or resource_id == self.spec.source_resource_id:
                raise DispositionRefused("cleanup target differs from the verified temporary instance")
            if target["DBInstanceStatus"] != "deleting":
                self._mutate(client.delete_db_instance, DBInstanceIdentifier=self.spec.target_identifier,
                             SkipFinalSnapshot=True, DeleteAutomatedBackups=True)
            return self._record("cleanup", "pending", target_resource_id=resource_id)
        self._record("cleanup", "completed", target_resource_id=resource_id)
        snapshots = _pages(client, "describe_db_snapshots", "DBSnapshots", SnapshotType="manual",
                           Filters=[{"Name": "dbi-resource-id", "Values": [resource_id]}])
        backups = _pages(client, "describe_db_instance_automated_backups", "DBInstanceAutomatedBackups", DbiResourceId=resource_id)
        for snapshot in snapshots:
            if snapshot["DbiResourceId"] != resource_id:
                raise DispositionRefused("temporary snapshot ownership differs")
            if snapshot["Status"] != "deleting":
                self._mutate(client.delete_db_snapshot, DBSnapshotIdentifier=snapshot["DBSnapshotIdentifier"])
        for backup in backups:
            if backup["DbiResourceId"] != resource_id:
                raise DispositionRefused("temporary backup ownership differs")
            self._mutate(client.delete_db_instance_automated_backup, DbiResourceId=resource_id)
        return self._record("backup_expiration", "pending" if snapshots or backups else "completed",
                            target_resource_id=resource_id, snapshot_count=len(snapshots), automated_backup_count=len(backups))
