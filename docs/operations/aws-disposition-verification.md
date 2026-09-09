# Verified AWS disposition components

The earlier #514 AWS adapter returned success references without removing S3
objects, KMS keys, or backups. RDS also returned success as soon as it accepted
a deletion request. Those paths now perform provider calls and require an
absence check before returning a completion reference.

`AwsDispositionResources` pins the customer, environment, deployment, account,
region, SQL endpoint/name, RDS instance ARN and immutable resource ID, object
namespace, final snapshot identity, and dedicated customer-managed key ARNs.
Pass it to `plan_environment_disposition(provider_resources=...)`; the separate
control-plane plan retains its digest. Execution refuses a different inventory
or a synthetic destroyer for that provider-bound plan. Existing plans without
this digest cannot drive the AWS adapter. The control-plane owner bootstrap
performs the additive column upgrade; customer Alembic is unchanged.

The caller supplies scoped SDK clients and the approved inventory digest.
No credentials are discovered implicitly. The account and client regions must
match the inventory, and the environment must be disabled. The orchestrator
rechecks legal hold, retention, and the registered binding before every
destructive API call, including each S3 batch.

- RDS deletion uses the physical instance identity, not the SQL database name.
  Request acceptance is partial; retry verifies absence. Identifier reuse
  refuses. The final snapshot is retained until the backup-disposition step.
- S3 removes versions and delete markers in the exact bucket/prefix and aborts
  multipart uploads. Per-object errors refuse completion. Final checks cover
  versions, current objects, and multipart uploads. Governance retention is
  never bypassed.
- KMS scheduling remains partial until `DescribeKey` reports absence. Only
  declared single-region customer-managed keys are supported. An empty key
  inventory means no declared customer-managed keys, not that AWS-managed keys
  were destroyed.
- RDS snapshot and automated-backup deletion remains partial until both
  declared same-region populations are absent. Physical resource identity is
  checked before deletion.

This is a component profile, not proof of complete customer-environment
destruction. Stack resources, customer logs, secrets, replicas, cross-region
copies, and other backup services still need complete inventory coverage and
verified disposition. Consequently the AWS adapter refuses the final
`environment` completion receipt even after its declared components are absent.
#514 stays open for that implementation, export/custody evidence, and the live
restore/expiration rehearsal. No AWS resources were changed to validate this
code; tests use SDK response stubs.

Provider contracts:
[RDS deletion](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DeleteDBInstance.html),
[S3 version deletion and per-object errors](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObjects.html),
[KMS waiting period](https://docs.aws.amazon.com/kms/latest/APIReference/API_ScheduleKeyDeletion.html),
[RDS automated-backup deletion](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DeleteDBInstanceAutomatedBackup.html).
