"""AWS stack coverage for whole-environment disposition (#514).

The first provider handled one database and one prefix. The selected CDK platform
also retains access-log buckets and secrets, and ECS creates a Container Insights
log group outside CloudFormation. This provider inventories those resources from
AWS before planning, persists their physical identities outside the customer DB,
and closes the environment only after checking their absence. Unknown stack
resource types and replication are refused; a stack deletion acknowledgement is
never a completion receipt. Clients are injected; importing this module creates
no session, credentials, network connection, or resource.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace
import json

from corridor.environment_disposition import AwsEnvironmentDestroyer
from corridor.disposition_contracts import (
    DispositionRefused, EnvironmentDestructionError,
    json_digest as digest, provider_rows, require_no_rds_replicas, automated_backup_rows,
    retained_object_versions,
)
from corridor.release_contract import DISPOSITION_STACK_OUTPUTS

# Explicitly reviewed #489 resource kinds. Any new kind needs a disposition
# implementation before it can be silently included in this profile.
_DATA_TYPES = frozenset({"AWS::RDS::DBInstance", "AWS::RDS::DBSubnetGroup",
    "AWS::S3::Bucket", "AWS::SecretsManager::Secret", "AWS::Logs::LogGroup",
    "AWS::ECR::Repository", "AWS::KMS::Key"})
_STATELESS_TYPES = frozenset({"AWS::S3::BucketPolicy", "AWS::IAM::Role", "AWS::IAM::Policy",
    "AWS::ECS::Cluster", "AWS::ECS::Service", "AWS::ECS::TaskDefinition",
    "AWS::EC2::SecurityGroup", "AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress",
    "AWS::ElasticLoadBalancingV2::LoadBalancer", "AWS::ElasticLoadBalancingV2::Listener",
    "AWS::ElasticLoadBalancingV2::TargetGroup", "AWS::ElasticLoadBalancingV2::ListenerRule",
    "AWS::SecretsManager::SecretTargetAttachment", "AWS::CloudWatch::Alarm",
    "AWS::ApplicationAutoScaling::ScalableTarget", "AWS::ApplicationAutoScaling::ScalingPolicy",
    "AWS::CDK::Metadata"})


def observe_stack_inventory(clients, resources, *, application_stack_id, data_stack_id):
    """Read the selected deployment's physical resources; never accept a list of names.

    Stack IDs must be immutable ARNs. The application outputs bind customer,
    environment and deployment; data outputs bind the registered DB and bucket.
    Shared network, account audit and control-plane stacks are never selected.
    """
    cf = clients["cloudformation"]
    # Which outputs bind a stack to the registered environment is the release
    # contract's declaration, not a second copy of it here. Every binding is
    # read before the first provider call, so a namespace no registration can
    # express refuses without a describe (#813).
    expected_by_stack = {
        stack: {key: getattr(resources, attribute) for key, attribute in binding.items()}
        for stack, binding in DISPOSITION_STACK_OUTPUTS.items()
    }
    result = []
    for stack, stack_id in (
        ("CorridorApplication", application_stack_id),
        ("CorridorData", data_stack_id),
    ):
        prefix = f"arn:aws:cloudformation:{resources.region}:{resources.account_id}:stack/"
        if not stack_id.startswith(prefix) or len(stack_id.removeprefix(prefix).split("/")) != 2:
            raise DispositionRefused("inventory needs exact same-account stack ARNs")
        described = cf.describe_stacks(StackName=stack_id)["Stacks"][0]
        if described["StackId"] != stack_id or described["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}:
            raise DispositionRefused("stack identity or stable deployment state differs")
        outputs = {row["OutputKey"]: row["OutputValue"] for row in described.get("Outputs", [])}
        if any(outputs.get(key) != value for key, value in expected_by_stack[stack].items()):
            raise DispositionRefused("stack outputs do not bind this registered environment")
        rows = provider_rows(cf, "list_stack_resources", "StackResourceSummaries", StackName=stack_id)
        for row in rows:
            if row["ResourceType"] not in _DATA_TYPES | _STATELESS_TYPES:
                raise DispositionRefused(f"uncovered stack resource type: {row['ResourceType']}")
            if row["ResourceStatus"] == "DELETE_COMPLETE":
                continue
            if not row.get("PhysicalResourceId"):
                raise DispositionRefused("a stack resource has no observed physical identity")
            result.append({"stack_id": stack_id, "logical_id": row["LogicalResourceId"],
                           "type": row["ResourceType"], "physical_id": row["PhysicalResourceId"]})
    result.sort(key=lambda row: (row["stack_id"], row["logical_id"]))
    databases = [r["physical_id"] for r in result if r["type"] == "AWS::RDS::DBInstance"]
    if databases != [resources.db_instance_identifier]:
        raise DispositionRefused("whole-environment inventory must contain exactly the bound customer database")
    clusters = [r["physical_id"] for r in result if r["type"] == "AWS::ECS::Cluster"]
    if len(clusters) != 1:
        raise DispositionRefused("selected platform requires exactly one application cluster")
    bucket_names = {r["physical_id"] for r in result if r["type"] == "AWS::S3::Bucket"}
    buckets = {r["Name"]: r["CreationDate"].isoformat() for r in provider_rows(clients["s3"], "list_buckets", "Buckets") if r["Name"] in bucket_names}
    if set(buckets) != bucket_names:
        raise DispositionRefused("declared buckets are not all positively observed in the customer account")
    source_rows = provider_rows(clients["rds"], "describe_db_instances", "DBInstances",
                         Filters=[{"Name": "dbi-resource-id", "Values": [resources.db_resource_id]}])
    if len(source_rows) != 1 or source_rows[0].get("DBInstanceArn") != resources.db_instance_arn:
        raise DispositionRefused("replica inventory must observe the approved physical source database")
    replica_relationships = require_no_rds_replicas(source_rows[0])
    return {"schema": "aws-corridor-environment-v1", "application_stack_id": application_stack_id,
            "data_stack_id": data_stack_id, "resources": result, "bucket_creation_dates": buckets,
            "rds_replica_relationships": replica_relationships,
            "implicit_log_groups": [f"/aws/ecs/containerinsights/{clusters[0].split('/')[-1]}/performance"]}


def verify_custody(clients, resources, inventory):
    """Read and hash the externally held version, including its archive manifest.

    The archive validator streams the actual retained bytes. Metadata, an ETag,
    a caller-supplied 'exported' flag, or request acceptance proves nothing.
    """
    from corridor.environment_export import verify_export_archive
    custody = inventory.get("custody")
    if not isinstance(custody, Mapping):
        raise DispositionRefused("whole-environment destruction requires external export custody")
    bucket_names = {r["physical_id"] for r in inventory["resources"] if r["type"] == "AWS::S3::Bucket"}
    if custody.get("bucket") in bucket_names or custody.get("owner") == resources.account_id:
        raise DispositionRefused("custody must be in a separately owned destination outside destruction")
    if not custody.get("accepted_by") or not custody.get("version_id") or custody["version_id"] == "null":
        raise DispositionRefused("custody needs a named human acceptance and immutable object version")
    source = dict(inventory)
    source.pop("custody", None)
    response = clients["custody_s3"].get_object(Bucket=custody["bucket"], Key=custody["key"],
        VersionId=custody["version_id"], ExpectedBucketOwner=custody["owner"])
    if response.get("VersionId") != custody["version_id"]:
        response["Body"].close()
        raise DispositionRefused("custody object version differs")
    with response["Body"] as body:
        manifest = verify_export_archive(body, expected_sha256=custody["sha256"],
            environment_id=resources.environment_id, inventory_sha256=digest(source))
    return manifest


class AwsStackEnvironmentDestroyer(AwsEnvironmentDestroyer):
    """Provider-native stack disposal with separately observed retained resources."""

    # The persisted execution boundary this adapter mutates under; ``None``
    # until ``prepare_execution`` has verified custody, census and quiescence.
    _execution_boundary = None

    def _binding(self, registration):
        resources = super()._binding(registration)
        for name in ("cloudformation", "ecs", "logs", "secretsmanager", "ecr", "ec2"):
            if name not in self._clients or self._clients[name].meta.region_name != resources.region:
                raise DispositionRefused("whole-environment client region differs from the approved inventory")
        return resources

    def _inventory(self):
        inventory = self.resources.whole_environment if self.resources else None
        if not inventory or inventory.get("schema") != "aws-corridor-environment-v1":
            raise DispositionRefused("whole-environment inventory is missing")
        return inventory

    def _ids(self, resource_type):
        return [r["physical_id"] for r in self._inventory()["resources"] if r["type"] == resource_type]

    def verify_plan(self, registration):
        resources = self._binding(registration)
        inv = self._inventory()
        observed = observe_stack_inventory(self._clients, resources,
            application_stack_id=inv["application_stack_id"], data_stack_id=inv["data_stack_id"])
        expected = dict(inv)
        expected.pop("custody", None)
        if observed != expected:
            raise DispositionRefused("provider inventory changed before the dry run")
        self._verify_copies()
        self._require_quiescent()
        manifest = verify_custody(self._clients, resources, inv)
        if manifest.get("db_resource_id") != resources.db_resource_id:
            raise DispositionRefused("export dump does not bind the approved physical database")
        self._verify_export_census(manifest, allow_disposed_subset=False)
        declared_keys = set(self._ids("AWS::KMS::Key"))
        expected_keys = {arn.rsplit("/", 1)[-1] for arn in resources.kms_key_arns}
        if declared_keys != expected_keys:
            raise DispositionRefused("KMS destruction inventory differs from the stack's dedicated keys")

    def _verify_export_census(self, manifest, *, allow_disposed_subset):
        bucket = self.resources.object_namespace_bucket
        if bucket not in self._owned_buckets():
            if allow_disposed_subset:
                return {"bucket_absent": True}
            raise DispositionRefused("source bucket disappeared before the execution boundary")
        versions = retained_object_versions(self._clients["s3"], bucket, self.resources.account_id)
        expected = manifest["object_versions"]
        actual_set = {(r["key"], r["version_id"], r["delete_marker"]) for r in versions}
        expected_set = {(r["key"], r["version_id"], r["delete_marker"]) for r in expected}
        if (not allow_disposed_subset and versions != expected) or not actual_set <= expected_set:
            raise DispositionRefused("source versions changed after export custody was established")
        return {"source_versions_sha256": digest(versions), "version_count": len(versions)}

    def require_frozen(self, registration):
        """The operator's freeze guard before export, custody and rehearsal
        mutations: bound, application drained, stack membership unchanged."""
        self._binding(registration)
        self._require_quiescent()
        self._verify_stack_membership()

    def _prepare_execution_boundary(self, control_plane, plan, referential, operation_id, *, observed_at):
        from uuid import uuid4
        if self.resources.whole_environment is None:
            return super()._prepare_execution_boundary(
                control_plane, plan, referential, operation_id, observed_at=observed_at)
        # A persisted boundary distinguishes the first pass (exact export
        # equality) from a resumed partial disposal (only disappearance allowed).
        previous = control_plane.disposition_rehearsal_receipts(plan.environment_id, operation_id)
        boundaries = [row for row in previous if row["phase"] == "execution_boundary"]
        expected = {"inventory_sha256": self.resources.sha256, "manifest_sha256": plan.manifest_sha256,
                    "custody_sha256": self._inventory()["custody"]["sha256"]}
        if any(any(row["evidence"].get(key) != value for key, value in expected.items()) for row in boundaries):
            raise DispositionRefused("execution boundary belongs to another plan, inventory or export")
        manifest = verify_custody(self._clients, self.resources, self._inventory())
        if referential.open_dereference_promises and not manifest.get("unavailability_disclosure"):
            raise DispositionRefused("retained references require exported unavailability disclosure")
        self._verify_stack_membership()
        replicas = self._verify_copies()
        census = self._verify_export_census(manifest, allow_disposed_subset=bool(boundaries))
        if not boundaries:
            self._require_quiescent()
            control_plane.record_disposition_rehearsal(receipt_id=f"execution-{uuid4().hex}",
                environment_id=plan.environment_id, operation_id=operation_id, phase="execution_boundary",
                outcome="completed", evidence={**expected, **census, "rds_replica_relationships": replicas,
                    "database_validation": manifest["database_validation"]}, observed_at=observed_at)
        self._execution_boundary = expected
        self._control_plane = control_plane
        self._referential = referential

    def _mutate(self, method, **parameters):
        if self._execution_boundary is None:
            raise DispositionRefused("whole-environment deletion needs a persisted disposition plan")
        return super()._mutate(method, **parameters)

    def _require_quiescent(self):
        ecs = self._clients["ecs"]
        for cluster in self._ids("AWS::ECS::Cluster"):
            # Both running and pending are required: a deployment at desired=0
            # may still be draining a writer, and an already submitted task may
            # become runnable after an export is frozen.
            for desired in ("RUNNING", "PENDING"):
                if provider_rows(ecs, "list_tasks", "taskArns", cluster=cluster, desiredStatus=desired):
                    raise DispositionRefused("application tasks must be drained before export or deletion")
            services = self._ids("AWS::ECS::Service")
            for start in range(0, len(services), 10):
                result = ecs.describe_services(cluster=cluster, services=services[start:start+10])
                if result.get("failures") or any(s.get("desiredCount") or s.get("runningCount") or s.get("pendingCount") for s in result.get("services", [])):
                    raise DispositionRefused("application services are not quiescent")

    def _verify_stack_membership(self):
        """Reject added/replaced resources; allow only disappearance on resume."""
        cf = self._clients["cloudformation"]
        inv = self._inventory()
        known = {(r["stack_id"], r["logical_id"]): (r["type"], r["physical_id"]) for r in inv["resources"]}
        for stack_id in (inv["application_stack_id"], inv["data_stack_id"]):
            rows = provider_rows(cf, "list_stack_resources", "StackResourceSummaries", StackName=stack_id)
            for row in rows:
                if row["ResourceStatus"] == "DELETE_COMPLETE":
                    continue
                if known.get((stack_id, row["LogicalResourceId"])) != (row["ResourceType"], row.get("PhysicalResourceId")):
                    raise DispositionRefused("stack inventory added or replaced a resource after planning")

    def _verify_copies(self):
        """This #489 profile has no replication: verify the provider agrees.

        Cross-region clients cover every enabled region. AWS Backup recovery
        points, snapshot sharing and remote replication fail closed pending an
        explicitly expanded custody inventory; they are never inferred absent
        from the source region's empty list.
        """
        resources = self.resources
        source_rows = provider_rows(self._clients["rds"], "describe_db_instances", "DBInstances",
                             Filters=[{"Name": "dbi-resource-id", "Values": [resources.db_resource_id]}])
        relationships = {"db_resource_id": resources.db_resource_id, "source_absent": True}
        if source_rows:
            if len(source_rows) != 1 or source_rows[0].get("DBInstanceArn") != resources.db_instance_arn:
                raise DispositionRefused("replica inventory source identity differs")
            relationships = require_no_rds_replicas(source_rows[0])
        regions = {r["RegionName"] for r in self._clients["ec2"].describe_regions(AllRegions=False)["Regions"]}
        regional = self._clients.get("regional", {})
        if set(regional) != regions:
            raise DispositionRefused("backup inventory requires clients for every enabled AWS region")
        for region, clients in regional.items():
            if clients["sts"].get_caller_identity()["Account"] != resources.account_id:
                raise DispositionRefused("backup client account differs from inventory")
            for name in ("rds", "backup"):
                if clients[name].meta.region_name != region:
                    raise DispositionRefused("backup client region differs from inventory")
            snapshots = provider_rows(clients["rds"], "describe_db_snapshots", "DBSnapshots",
                SnapshotType="manual", Filters=[{"Name": "dbi-resource-id", "Values": [resources.db_resource_id]}])
            backups = automated_backup_rows(clients["rds"], resources.db_resource_id)
            if region != resources.region and (snapshots or backups):
                raise DispositionRefused("remote RDS copies require expanded custody and disposition inventory")
            for snapshot in snapshots:
                attributes = clients["rds"].describe_db_snapshot_attributes(DBSnapshotIdentifier=snapshot["DBSnapshotIdentifier"])["DBSnapshotAttributesResult"]
                if any(a.get("AttributeValues") for a in attributes.get("DBSnapshotAttributes", []) if a["AttributeName"] == "restore"):
                    raise DispositionRefused("shared snapshot requires external copy custody inventory")
            if provider_rows(clients["backup"], "list_recovery_points_by_resource", "RecoveryPoints", ResourceArn=resources.db_instance_arn):
                raise DispositionRefused("AWS Backup recovery points require expanded disposition inventory")
        s3 = self._clients["s3"]
        for bucket in self._ids("AWS::S3::Bucket"):
            # list_buckets supplies a positive account inventory; unlike a HEAD
            # 403 it does not conflate inaccessible with absent.
            if bucket not in self._owned_buckets():
                continue
            if s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=resources.account_id).get("Status") != "Enabled":
                raise DispositionRefused("declared customer buckets must retain versioned identities")
            try:
                replication = s3.get_bucket_replication(Bucket=bucket, ExpectedBucketOwner=resources.account_id)
            except s3.exceptions.ClientError as exc:
                if exc.response["Error"]["Code"] != "ReplicationConfigurationNotFoundError":
                    raise
            else:
                if replication.get("ReplicationConfiguration", {}).get("Rules"):
                    raise DispositionRefused("S3 replicas require expanded external custody inventory")
        sm = self._clients["secretsmanager"]
        for secret in self._ids("AWS::SecretsManager::Secret"):
            try:
                result = sm.describe_secret(SecretId=secret)
            except sm.exceptions.ResourceNotFoundException:
                continue
            if result.get("ReplicationStatus"):
                raise DispositionRefused("replicated secrets require expanded inventory")
        return relationships

    def _owned_buckets(self):
        rows = provider_rows(self._clients["s3"], "list_buckets", "Buckets")
        expected = self._inventory().get("bucket_creation_dates", {})
        for row in rows:
            if row["Name"] in self._ids("AWS::S3::Bucket") and (
                    row["Name"] not in expected or row.get("CreationDate").isoformat() != expected[row["Name"]]):
                raise DispositionRefused("bucket identity changed or lacks its persisted provider observation")
        return {r["Name"] for r in rows}

    def _delete_stack(self, stack_id):
        cf = self._clients["cloudformation"]
        stack = cf.describe_stacks(StackName=stack_id)["Stacks"][0]
        if stack["StackId"] != stack_id:
            raise DispositionRefused("stack physical identity changed")
        if stack["StackStatus"] == "DELETE_COMPLETE":
            return
        if stack["StackStatus"] == "DELETE_IN_PROGRESS":
            raise EnvironmentDestructionError("stack deletion is pending")
        if stack.get("EnableTerminationProtection"):
            self._mutate(cf.update_termination_protection, EnableTerminationProtection=False, StackName=stack_id)
        self._mutate(cf.delete_stack, StackName=stack_id)
        raise EnvironmentDestructionError("stack deletion requested; verify DELETE_COMPLETE on retry")

    def delete_database(self, registration):
        self._binding(registration)
        # Delete the application first. This ends providers that can recreate
        # data/logs and removes the cross-stack references to the data stack.
        status = self._clients["cloudformation"].describe_stacks(StackName=self._inventory()["application_stack_id"])["Stacks"][0]["StackStatus"]
        if status != "DELETE_COMPLETE":
            self._require_quiescent()
            self._delete_stack(self._inventory()["application_stack_id"])
        rds = self._clients["rds"]
        rows = provider_rows(rds, "describe_db_instances", "DBInstances", Filters=[{"Name": "dbi-resource-id", "Values": [self.resources.db_resource_id]}])
        for row in rows:
            if row.get("DbiResourceId") != self.resources.db_resource_id or row.get("DBInstanceArn") != self.resources.db_instance_arn:
                raise DispositionRefused("database physical identity changed")
            require_no_rds_replicas(row)
            if row.get("DeletionProtection"):
                self._mutate(rds.modify_db_instance, DBInstanceIdentifier=self.resources.db_instance_identifier, DeletionProtection=False, ApplyImmediately=True)
                raise EnvironmentDestructionError("database protection change pending; observe before deletion")
        return super().delete_database(registration)

    def delete_object_namespace(self, registration):
        original = self.resources
        try:
            for bucket in self._ids("AWS::S3::Bucket"):
                if bucket not in self._owned_buckets():
                    continue
                # Reuse the exact version/multipart deletion algorithm for all
                # three dedicated buckets, never a user-supplied arbitrary name.
                scoped = replace(original, object_namespace_ref=f"s3:{bucket}")
                self.resources = scoped
                self.approved_resource_sha256 = scoped.sha256
                super().delete_object_namespace(replace(registration, object_namespace_ref=f"s3:{bucket}"))
                self.resources = original
                self.approved_resource_sha256 = original.sha256
                self._mutate(self._clients["s3"].delete_bucket, Bucket=bucket, ExpectedBucketOwner=original.account_id)
            if set(self._ids("AWS::S3::Bucket")) & self._owned_buckets():
                raise EnvironmentDestructionError("environment buckets are not yet absent")
        finally:
            self.resources = original
            self.approved_resource_sha256 = original.sha256
        return self._evidence("object_namespace")

    def delete_environment(self, registration):
        if self.resources is None or self.resources.whole_environment is None:
            return super().delete_environment(registration)
        self._binding(registration)
        self._verify_stack_membership()
        self._verify_copies()
        logs = self._clients["logs"]
        pending = False
        for group in self._ids("AWS::Logs::LogGroup") + self._inventory()["implicit_log_groups"]:
            rows = provider_rows(logs, "describe_log_groups", "logGroups", logGroupNamePrefix=group)
            if any(row["logGroupName"] == group for row in rows):
                self._mutate(logs.delete_log_group, logGroupName=group)
                pending = True
        secrets = self._clients["secretsmanager"]
        for secret in self._ids("AWS::SecretsManager::Secret"):
            try:
                row = secrets.describe_secret(SecretId=secret)
            except secrets.exceptions.ResourceNotFoundException:
                continue
            if row["ARN"] != secret:
                raise DispositionRefused("secret physical identity changed")
            if not row.get("DeletedDate"):
                self._mutate(secrets.delete_secret, SecretId=secret, RecoveryWindowInDays=30)
            pending = True
        if pending:
            raise EnvironmentDestructionError("logs or secrets still exist; deletion request is not absence")
        # CDK retains the subnet group and image repository. Neither contains
        # customer records, but both are owned resources of the declared stacks.
        rds = self._clients["rds"]
        for name in self._ids("AWS::RDS::DBSubnetGroup"):
            try:
                rds.describe_db_subnet_groups(DBSubnetGroupName=name)
            except rds.exceptions.DBSubnetGroupNotFoundFault:
                continue
            self._mutate(rds.delete_db_subnet_group, DBSubnetGroupName=name)
            pending = True
        ecr = self._clients["ecr"]
        for name in self._ids("AWS::ECR::Repository"):
            try:
                ecr.describe_repositories(repositoryNames=[name], registryId=self.resources.account_id)
            except ecr.exceptions.RepositoryNotFoundException:
                continue
            self._mutate(ecr.delete_repository, repositoryName=name, registryId=self.resources.account_id, force=True)
            pending = True
        if pending:
            raise EnvironmentDestructionError("retained stack resources deletion requested; verify absence on retry")
        self._delete_stack(self._inventory()["data_stack_id"])
        # Reobserve data populations even if earlier component receipts exist;
        # a restore or late log delivery must not inherit stale completion.
        super().delete_database(registration)
        if set(self._ids("AWS::S3::Bucket")) & self._owned_buckets():
            raise EnvironmentDestructionError("a disposed bucket exists again")
        super().expire_backups(registration)
        super().destroy_encryption_key(registration)
        return self._evidence("environment")

    def cancel_pending_deletions_for_hold(self, control_plane, plan, *, observed_at):
        """Cancel provider recovery-window deletions while a fresh hold is active.

        Already completed deletions and non-cancellable in-flight RDS/stack
        operations cannot be undone. The receipt names only cancellations AWS
        subsequently confirmed, never a claim that earlier data was restored.
        """
        from uuid import uuid4
        if self.resources is None or self.resources.whole_environment is None:
            return super().cancel_pending_deletions_for_hold(control_plane, plan, observed_at=observed_at)
        self._require_activation("hold cancellation")
        if self.resources.sha256 != plan.provider_resources_sha256 or plan.provider_resources != json.loads(json.dumps(asdict(self.resources))):
            raise DispositionRefused("hold cancellation requires the persisted provider inventory")
        if self._clients["sts"].get_caller_identity()["Account"] != self.resources.account_id:
            raise DispositionRefused("hold cancellation account differs")
        for service in ("kms", "secretsmanager"):
            if self._clients[service].meta.region_name != self.resources.region:
                raise DispositionRefused("hold cancellation region differs")
        def require_hold():
            if not control_plane.inspect(plan.environment_id).hold:
                raise DispositionRefused("hold cancellation requires a current legal hold")
        require_hold()
        observations = []
        kms = self._clients["kms"]
        for arn in self.resources.kms_key_arns:
            try:
                key = kms.describe_key(KeyId=arn)["KeyMetadata"]
            except kms.exceptions.NotFoundException:
                observations.append({"resource": arn, "state": "already_absent"})
                continue
            if key["Arn"] != arn:
                raise DispositionRefused("hold key identity differs")
            if key["KeyState"] == "PendingDeletion":
                require_hold()
                kms.cancel_key_deletion(KeyId=arn)
                key = kms.describe_key(KeyId=arn)["KeyMetadata"]
            observations.append({"resource": arn, "state": key["KeyState"]})
        sm = self._clients["secretsmanager"]
        for secret in self._ids("AWS::SecretsManager::Secret"):
            try:
                row = sm.describe_secret(SecretId=secret)
            except sm.exceptions.ResourceNotFoundException:
                observations.append({"resource": secret, "state": "already_absent"})
                continue
            if row["ARN"] != secret:
                raise DispositionRefused("hold secret identity differs")
            if row.get("DeletedDate"):
                require_hold()
                sm.restore_secret(SecretId=secret)
                row = sm.describe_secret(SecretId=secret)
            observations.append({"resource": secret, "state": "PendingDeletion" if row.get("DeletedDate") else "retained"})
        return control_plane.record_disposition_rehearsal(receipt_id=f"hold-{uuid4().hex}",
            environment_id=plan.environment_id, operation_id=plan.plan_id, phase="hold_cancellation",
            outcome="pending" if any(r["state"] == "PendingDeletion" for r in observations) else "completed",
            evidence={"inventory_sha256": self.resources.sha256, "observations": observations}, observed_at=observed_at)
