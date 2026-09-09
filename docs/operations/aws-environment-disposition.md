# AWS whole-environment disposition and recovery tooling

Issue #514 now has an AWS implementation for the stacks selected by #489. This
is software and hermetic validation; it is not evidence of a live export,
destruction, recovery rehearsal, or customer authorization. #535 must supply
scoped clients and an explicitly approved execution. The control-plane store
survives every step.

## Supported resource inventory

`AwsStackEnvironmentDestroyer` consumes `AwsDispositionResources.whole_environment`.
Create that inventory with `observe_stack_inventory`, using the immutable ARN
of the customer **application** stack and **data** stack. Generic stack names,
generic CDK tags, and handwritten resource lists are insufficient. The application
stack publishes customer, environment and deployment outputs; the data stack
publishes the database endpoint and artifact bucket. These outputs must agree
with the registered environment. Existing deployments must publish the new
outputs before this provider can plan their disposal.

The observer enumerates every CloudFormation resource and rejects unknown
resource types. The selected platform includes:

- The RDS instance, its manual snapshots and automated/PITR backups, and retained
  subnet group.
- The versioned artifact bucket, artifact access-log bucket and ALB access-log
  bucket, including delete markers and unfinished multipart uploads.
- Customer database administrator, web, worker, and routing secrets.
- RDS/ECS log groups, plus the implicit Container Insights performance log group.
- Application services, task definitions, roles, load balancer and retained image
  repository through their owning stack and explicit retained-resource checks.
- Dedicated customer-managed KMS keys when declared. The current #489 stacks use
  AWS-managed encryption and therefore declare no customer key for destruction.

The network, account audit stack and separate control plane are shared operations
infrastructure, not customer destruction units. Their operational audit receipts
remain under their own retention. No application code or operator receipt should
claim that deleting a customer environment erases account audit history.

The current profile has no replication. Supply read clients for **every enabled
AWS region**; the provider checks same-account RDS snapshots and automated backups,
AWS Backup recovery points, snapshot sharing, S3 replication and secret replicas.
A detected remote copy, shared snapshot, AWS Backup recovery point, or replication
configuration refuses this profile and requires an expanded, explicitly approved
inventory. An empty source-region list cannot establish remote-copy absence.
Arbitrary historical copies made outside this deployment are not discoverable
from CloudFormation: the operator must establish their custody before choosing
this profile. The software does not interpret a blanket completeness boolean.

## Export, custody and planning

1. Disable the registered environment and freeze deployment/maintenance writes.
   Set service desired counts to zero and wait for all running and pending ECS
   tasks to finish. Inventory and export guards check this provider state.
2. Build the provider inventory with `observe_stack_inventory`. No customer bytes
   enter the control-plane inventory.
3. Call `export_environment_archive` with the inventory, an explicit read-capable
   PostgreSQL username and pgpass file, a named `HumanPrincipal`, and a fresh
   freeze guard. It runs `pg_dump --format=custom` against the bound host, port and
   database, captures every artifact object version and delete marker, checks
   that the source version inventory did not change, and writes a tar archive.
   A retained record that will lose dereference needs an explicit source
   unavailability disclosure in the archive. This archive exports the whole
   Project Record database and its raw-source/artifact namespace; operational
   logs and authentication credentials are disposed rather than exported as
   part of that customer record.
4. `transfer_export_custody` uploads the archive to a separately owned, versioned
   destination. It reads the exact retained version back and checks the archive
   digest, every member digest and size, database dump, object-version coverage,
   and disclosure. Attach the returned custody reference to the inventory.
5. Call `plan_environment_disposition(..., provider_resources=resources,
   provider_observer=destroyer)`. It reobserves stack membership, copy/replication
   posture, task quiescence, current source versions and the external archive.
   The full provider inventory and custody reference are persisted in the
   control plane alongside their digest and the disposition manifest digest.

The pgpass file and its contents never enter an archive or receipt. The export
function inherits no ambient libpq `PG*` configuration other than the explicitly
provided pgpass path. The caller must keep the deployment frozen between export
and disposal; a new deployment or writer invalidates the plan.

## Destruction and resume

`execute_environment_disposition` checks the persisted manifest and provider
inventory bytes before calling the destroyer. Every provider mutation receives a
fresh hold, retention and registration guard. The provider first removes the
quiescent application stack, disables RDS deletion protection under that guard,
and requests database deletion with an explicit final snapshot. A later pass
must observe the instance absent. It empties and deletes every dedicated bucket,
checks customer key absence, and removes snapshots and PITR backups. It then
checks logs, secrets, retained resources and the data stack, and rechecks the
actual database, bucket, key and backup populations before final completion.

`DELETE_IN_PROGRESS`, RDS deletion acceptance, KMS `PendingDeletion`, a secret's
recovery-window `DeletedDate`, and requested backup deletion all leave the plan
**partial**. They are never completed receipts. SDK failures likewise establish
no completion. Retry the same plan and operation ID: provider calls are
reconciled against observed state, completed component receipts must bind the
same inventory and manifest, and additions or replacements in the stack cause
refusal. Never silently create a new plan for a partially destroyed environment.

Keys and secrets use 30-day waiting/recovery windows. When a legal hold arrives,
`cancel_pending_deletions_for_hold` verifies the persisted inventory and cancels
pending KMS/secret deletion, recording the subsequently observed state in the
control plane. Held execution attempts invoke this cancellation path as well.
The operator must arrange prompt hold reconciliation while a deletion is
waiting. This library is not itself a deployed monitor. AWS operations that have
already completed, or accepted RDS/stack deletions that cannot be cancelled,
cannot be undone by a later hold; receipts do not claim restoration of those
resources.

## PITR and backup-expiration rehearsal

`RestoreRehearsal` binds the source physical database, restore timestamp,
isolated target identifier/networking, and expected exact query-result/object
byte digests. It requires nonempty earlier state and workflow receipts, and an
empty later-mutation result. `AwsRestoreRehearsal` has three resumable operations:

- `restore()` verifies the source identity and the provider's automated-backup
  `RestoreWindow`, records intent externally, then requests a tagged isolated
  PITR instance. Only a later `available` observation with the pinned networking
  produces a completed restore receipt.
- `verify(probe)` executes an injected probe against that exact physical target.
  `sql_restore_probe` checks the psycopg connection endpoint/database, runs
  explicit synthetic-fixture queries in a read-only transaction and hashes the
  returned rows and actual object bytes. All digests must match the pinned
  earlier state, absent later mutation, workflow receipts and object inventory.
- `cleanup()` requires persisted state verification, deletes only the observed
  temporary physical instance, then separately observes and expires its manual
  snapshots and automated backups. It reports pending until both are absent.

Every phase appends an immutable `disposition_rehearsal_receipts` row in the
separate control plane. Restore availability alone does not establish state
verification, and successful state verification alone does not establish cleanup
or backup expiration. A live rehearsal receipt requires actual provider and
probe observations; no `performed=True` or synthetic receipt activates a customer.

Provider behavior was checked against the AWS API references for
[DeleteDBInstance](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DeleteDBInstance.html),
[DeleteStack](https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_DeleteStack.html),
[DeleteSecret](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_DeleteSecret.html),
[RestoreDBInstanceToPointInTime](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_RestoreDBInstanceToPointInTime.html),
and [S3 versioned GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html).
S3 access logs have [best-effort, delayed delivery](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html);
therefore finalization deletes the owning buckets and checks their absence rather
than claiming that an earlier empty listing permanently ended log delivery.

## Execution boundary and archive validation

Immediately before the first destructive provider operation, execution reads the
external archive again and compares its artifact-version census with the current
source bucket. It records an immutable `execution_boundary` observation binding
the plan, inventory, export digest, database-validation evidence and observed RDS
replica relationships. A resumed execution accepts only disappearance of exported
versions; an uploaded or replaced version that was never exported causes refusal.
The nonreplicated profile also checks live RDS instance and cluster replica links,
not merely backups associated with the source's physical ID.

Both export creation and custody verification run `pg_restore --file /dev/null`
over the complete PostgreSQL custom archive. This emits SQL to a discarded file;
it never connects to a database or executes the archive's SQL. Header recognition
and `pg_restore --list` alone are insufficient because the data blocks can be
truncated while the table of contents remains readable. The manifest retains the
full-parser method, database-byte digest and size, and custody verification must
reproduce that evidence. Operators need compatible `pg_dump` and `pg_restore`
clients on their execution host; missing or incompatible binaries refuse the
operation. The library does not install packages or fetch tools at runtime.

The completed local export is published with mode `0600`: a private temporary
file in the destination directory is verified and synced before atomic
replacement. A failed dump, read, archive check or sync leaves an existing
verified destination intact and removes the partial file.

Rehearsal cleanup locates the restored database by its persisted `DbiResourceId`.
Renaming the temporary instance does not make it disappear: cleanup checks its
rehearsal tags and requests deletion using the currently observed identifier.

## Operator CLI

Use `make environment-disposition ARGS="..."`. `--help` creates no clients and
makes no connections. Each command requires `--configuration <private-json>` and
`--output <private-json>`. The output is a new configuration/result artifact;
feed it to the next command. The original configuration is preserved unless the
operator explicitly names it as the output destination.

The initial configuration contains:

- `resources`: the `AwsDispositionResources` identity fields from the registered
  deployment: customer/environment/deployment IDs, AWS account/region, database
  host/port/name, instance identifier/ARN/physical `DbiResourceId`, dedicated
  `s3:<bucket>` namespace, chosen final-snapshot identifier and `kms_key_arns`.
  `whole_environment` starts as null and is populated by `inventory`.
- `application_stack_id` and `data_stack_id`: immutable deployment stack ARNs.
- `retention_schedules`: explicit `{label, retain_until}` entries, or an
  explicitly empty list when no retention obligation applies.
- `referential_retention`: explicit `open_dereference_promises`,
  `custody_transferred` and `unavailability_disclosed` declarations. These do not
  substitute for verified export/disclosure bytes.
- `custody_destination`: a separately owned `bucket`, `key` and AWS `owner`.

Database URLs are read only through named environment variables. The default
control-plane reference is `CONTROL_PLANE_OPERATIONS_DATABASE_URL`, matching
`make control-plane`; override the **variable name**, not its value, using
`--control-plane-url-env`. Provider commands require `--aws-profile <name>`,
`--principal <human-subject>` and `--authorize-aws-535`. Planning, custody transfer
and execution additionally require `--custody-profile <separate-profile>` so the
archive can be read using the custodian's explicit access. No credential values
belong in configuration JSON, command arguments, stdout or result artifacts.

For example, with those operator-owned environment/profile references already
configured:

```bash
make environment-disposition ARGS="inventory --configuration input.json --output inventory.json --aws-profile disposition --principal local:operator --authorize-aws-535"
make environment-disposition ARGS="export --configuration inventory.json --output exported.json --archive-output customer-export.tar --pgpass-file /private/operator/export.pgpass --database-username export_reader --postgres-ca-file /private/operator/rds-ca.pem --aws-profile disposition --principal local:operator --authorize-aws-535"
make environment-disposition ARGS="custody --configuration exported.json --output custody.json --aws-profile disposition --custody-profile archive-custodian --principal local:operator --authorize-aws-535"
make environment-disposition ARGS="plan --configuration custody.json --output plan.json --aws-profile disposition --custody-profile archive-custodian --principal local:operator --authorize-aws-535"
make environment-disposition ARGS="execute --configuration plan.json --output execution.json --operation-id approved-disposition-001 --aws-profile disposition --custody-profile archive-custodian --principal local:operator --authorize-aws-535"
make environment-disposition ARGS="status --configuration plan.json --output status.json --operation-id approved-disposition-001"
```

Export uses explicit RDS certificate verification (`sslmode=verify-full`) and a
pgpass reference. Add `--disclosure-file <path>` where retained dereference needs
the exported disclosure. No command disables services or edits the registry on
the operator's behalf; freeze the deployment using its existing deployment and
`make control-plane state` commands before planning.

`rehearsal-restore`, `rehearsal-verify` and `rehearsal-cleanup` consume a `rehearsal`
entry matching `RestoreRehearsal`. Verification additionally needs explicit
`rehearsal_queries` for the synthetic fixture and
`--restored-database-url-env <variable-name>`; the probe verifies that connection
against the RDS-described isolated target before reading. Re-run the same
configuration and operation identity to observe asynchronous progress.

The CLI returns **3** for a recorded partial/pending/refused execution or rehearsal
observation, **2** for a policy refusal and **1** for another failure. It returns
0 for a completed requested operation or a successful read/plan. A written
pending artifact does not mean destruction or restoration is complete. `status`
and `gate` read the control plane and never construct AWS clients.

For activation evidence, `gate` requires the executed `disposition_plan`,
`--operation-id`, the `rehearsal.operation_id`, the exact
`activation_configuration_identity` and `disposition_inventory_digest`.
`disposition_gate_payload` verifies the precise final AWS receipt and the latest
completed restore, state-verification, cleanup and backup-expiration observations
for one rehearsal specification. Its freshness timestamp comes from the retained
observations; regenerating a JSON artifact cannot freshen old provider evidence.
The result is evidence for the separate activation gate, never an activation.

The automated-backup collector recognizes the provider's documented
[`DBInstanceAutomatedBackupNotFound` response](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DescribeDBInstanceAutomatedBackups.html)
as an empty population. Permission failures and other errors remain failures;
a not-found response after an earlier page already returned backups also refuses
that inconsistent observation. Source-window checks, regional inventory,
disposition expiration and rehearsal cleanup share this collector.

A rehearsal binds the first observed target `DbiResourceId`, including an identity
returned with an asynchronous restore request. Subsequent restore and verification
passes use that physical identity. Renaming the instance does not create another
database under the original name, and disappearance or a conflicting physical
identity requires operator reconciliation instead of silently recreating it.
