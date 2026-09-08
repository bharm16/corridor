# Corridor nonproduction on AWS — runbook

Account `810100779593`, region `us-east-2`. Serves #601 (account and deployment
identity) and #489 (synthetic environment foundation).

**Status: deployment and restore acceptance remain open.** The CDK in
[`infra/`](../../infra) defines the environment; synthesis is not evidence that
it is running. #489 closes only after the synthetic deployment and the restore
rehearsal below have retained receipts.

## Prerequisites that block the first deploy

The 2026-09-08 read-only refresh authenticated as
`arn:aws:iam::810100779593:user/bryceharmon` using the local `corridor` profile.
It found no Corridor/CDKToolkit stacks, Corridor RDS instance, OIDC provider,
deployment roles under `/corridor/nonproduction/`, ACM certificate, SES
identity, or Route 53 hosted zone in the intended scope. GitHub returned no
environments for `bharm16/corridor`. The existing
`corridor-textract-rung-9593` bucket is historical provider work, not the
application environment. Do not recreate the account or repurpose that bucket.

| Prerequisite | State | Who |
|---|---|---|
| Root MFA enabled | done | — |
| No root or IAM access keys | done | — |
| MFA on `bryceharmon` | done; enabled 2026-09-04 | — |
| **Delete unused `bryce` user** | **exists, console access** | you, console |
| **Account password policy** | **not set** | you, console |
| **Cost Explorer enabled** | **not enabled** | you, console (root must first activate IAM billing access) |
| **Budget + alerts** | `Corridor-Nonproduction-Monthly`: USD 150/month, **HEALTHY**, created and read back 2026-09-08 | #601 records owner-selected email verification |
| **ACM certificate for the ALB** | **none** | you |
| **Public hostname** (`corridor:publicHostname`) | **none** | you, Route 53 or your DNS |
| **SES sender identity** (`corridor:signInSender`) | **none** | you, SES console |
| **Bootstrap policies created** | written, not created | you, one `aws iam create-policy` each |
| Container image | built and smoke-tested in CI | done |
| RDS minor version still offered | 16.14 returned available in `us-east-2` | verified 2026-09-08 |

The 2026-09-04 review recorded `AdministratorAccess` through the `Admin` group
for `bryceharmon`; that group's policies were not re-audited in this refresh.
MFA is now configured, and both IAM users and root have no access keys. Use
the MFA-protected identity only for bootstrap; deployed work uses the separate
short-lived workload and GitHub identities.

### The image

`Dockerfile` builds one image that all three roles run, because nothing in the
repository has a separate build context. It carries Python 3.12, a pinned `uv`
(the render subprocess invokes it at run time), both uv projects synced at
build time, WeasyPrint's native stack, the RDS trust bundle, and a non-root
`corridor` user.

It carried `tesseract-ocr` until #741 retired the engine (ADR-0094). The image
recorded before that removal was 3179 MB uncompressed and held PyMuPDF 1.28.0
in the application environment, PyMuPDF 1.28.2 in the render worker's,
pytesseract 0.3.13, and `tesseract-ocr` 5.3.0-2 at `/usr/bin/tesseract`;
`make image-engine-audit` builds the image and reports what is in it now, and
its receipts are under `artifacts/pdf-engine-retirement/`.

`scripts/container_entrypoint.py` is the entrypoint. It composes exactly one
database URL for the role in `CORRIDOR_TASK_ROLE`, percent-encodes the
credentials, requires `sslmode=verify-full` against the bundled trust store,
drops the raw passwords the role no longer needs, and `execvpe`s the command
from the task definition without a shell.

Which URL it sets is the database capability boundary. `Settings` reads `DATABASE_URL`,
`WEB_DATABASE_URL` and `WORKER_DATABASE_URL` separately, and
`db.capability_url` derives a capability URL from the owner's only when that
capability's own URL is empty. Leaking `DATABASE_URL` into a runtime container
would silently reconnect it as the schema owner, so web and batch never receive
it.

The `image` CI job builds it and, against a real PostgreSQL 16, runs the
migration to head, proves the `corridor_web` and `corridor_worker` logins it
created can authenticate, confirms web answers `/livez` and `/readyz`, checks
that batch holds no other role's password, and resolves the render environment
with the network disabled.

### The hostname, the certificate and the sender

Three deployment inputs have no sensible default and the stack refuses without
them. The recommendation is one domain for all three: `pilot.<domain>` as the
public hostname, an ACM certificate in `us-east-2` covering it, and
`no-reply@<domain>` as the SES sender.

**`corridor:publicHostname`** is what a release actually verifies. An ACM
certificate covers a domain, never the generated `*.elb.amazonaws.com` name, so
probing the load balancer's own hostname fails certificate validation after
every otherwise-successful deployment. The stack outputs `ApplicationUrl` from
this value and the release workflow curls that; `LoadBalancerDns` is still
reported for operators but never probed. Point a Route 53 alias (or your own
DNS) at the load balancer.

**`corridor:signInSender`** must be an SES identity you have verified. Without
a delivery adapter `/sign-in/request` reports success and sends nothing, and
because that endpoint answers identically whether or not an address is enrolled
-- which is correct, it must not disclose enrolment -- the failure is invisible
from outside. The web application therefore refuses to start in any deployed
environment configured with the logging sender.

SES sandbox is enough for an internal rehearsal provided both the sender
identity and your own recipient address are verified. Request production access
before inviting anyone else, or their links will be rejected.

### The ALB certificate

Without `corridor:certificateArn`, the stack creates **no listener** and
refuses a positive web desired count. The application stays unreachable.
Supply a certificate and its public hostname to enable 443 with an 80→443
redirect and `TLS13_RES`. The protected deployment workflow validates those
inputs and the SES sender before obtaining AWS credentials.

## The deployment-branch policy is load-bearing

Both OIDC roles trust exactly one subject:

```
repo:bharm16/corridor:environment:nonproduction
```

That subject says which *environment* the run was admitted to. It says nothing
about which ref supplied the workflow file. So the `github.ref == 'refs/heads/main'`
guard in the workflows is defence in depth and not the boundary: a collaborator
with write access can dispatch from a branch that simply deletes the guard, and
after environment approval its token carries the same trusted subject. AWS
grants the role without ever learning where the YAML came from.

The boundary is a GitHub setting, and it must be configured before the first
deployment:

**Settings → Environments → `nonproduction`**

| Setting | Value | Why |
|---|---|---|
| Deployment branches and tags | **Selected branches**, `main` only | The only thing that stops an unreviewed branch obtaining the role |
| Required reviewers | at least one person | A human sees the dispatch before the token is issued |
| Environment variables | `CORRIDOR_CERTIFICATE_ARN`, `CORRIDOR_PUBLIC_HOSTNAME`, `CORRIDOR_SIGN_IN_SENDER` | Deployment configuration, not secrets |
| Synthetic identity variables | `CORRIDOR_CUSTOMER_ID`, `CORRIDOR_CUSTOMER_ENVIRONMENT_ID`, `CORRIDOR_DEPLOYMENT_ID`, `CORRIDOR_DEPLOYMENT_DATA_CLASS=synthetic` | Stable registry/database identities; deployment ID is never the commit SHA |
| Environment secrets | none | The account holds no long-lived AWS credential |

"Selected branches: `main`" is the load-bearing one. Without it, every other
control in this document is enforced by a file the requester can edit.

## Deployment order

Complete and retain each step's actual outcome. These instructions are not
execution receipts.

1. Complete every prerequisite above, including the hostname, certificate and
   verified SES sender.
2. Review the PR; run `make test-infra` and `cdk synth --strict` using the
   locked toolchain described in [`infra/README.md`](../../infra/README.md).
3. Create the two bootstrap policies, then bootstrap with them. See
   [`infra/bootstrap/README.md`](../../infra/bootstrap/README.md) for the exact
   commands. Bootstrap creates an asset bucket, an ECR repository, an SSM
   version parameter, and **five** IAM roles (`deploy`, `lookup`,
   `file-publishing`, `image-publishing`, and the CloudFormation execution
   role). Omitting `--cloudformation-execution-policies` is what silently gives
   that execution role `AdministratorAccess`, so it is passed explicitly along
   with `--custom-permissions-boundary`.
4. Deploy `CorridorAccountFoundation` locally. This creates the OIDC provider,
   `corridor-nonprod-cdk-deploy` and `corridor-nonprod-app-release`.
5. Create the GitHub `nonproduction` environment with protection rules. The
   role's trust subject is
   `repo:bharm16/corridor:environment:nonproduction`, so the environment's
   rules are load-bearing, not decorative.
6. Verify OIDC with a workflow that calls only `sts:GetCallerIdentity`.
7. Record account id, region, and role ARN on #601 — **identifiers only, never
   a secret.** That satisfies #601's checklist.
8. From GitHub Actions, dispatch `diff` for each stack and read it, then
   dispatch `deploy`: `CorridorNetwork`, then `CorridorData` and
   `CorridorControlPlane`, then
   `CorridorApplication` (web and worker desired counts 0). They are separate dispatches on
   purpose -- an environment approval granted before a job produces its diff
   approves nothing.
9. Dispatch `app-release` for an exact merged commit with both desired counts
   zero. The existing Migration task runs `make deployment-bootstrap
   ARGS=configure`'s CLI: initialize the separate control plane and its scoped
   logins, run the customer Alembic migration, bind the customer database,
   register or verify its immutable route, and check the supplied credentials.
   A new route stays disabled; retries preserve operator state. Customer
   Project Record data never belongs in the control-plane database.
10. Inspect and explicitly enable the synthetic route using
    [the customer-environment operations commands](../operations/customer-environments.md).
    Run them as a one-off released Migration task with container override
    `MigrationContainer`, environment `CORRIDOR_MIGRATION_MODE=operations`,
    and command `python -m corridor.control_plane_cli inspect <environment-id>`
    (then the explicit `state` command with the intended enabled and hold
    flags). Operations mode composes only the operations URL and strips all
    customer, owner and resolver credentials before executing the command.
    Inspect and preserve any hold; it is independent of serving access.
    Then dispatch `app-release` with both desired counts set to 1.
    It registers digest-bound task revisions, drains web and worker
    (including live tasks from the retained Batch family), runs the migration
    as the schema writer and requires the route already enabled, starts the
    worker, verifies it, then starts web.
    The worker runs the existing Due Work supervisor and receives only its
    own database capability. Its ECS health command checks the database,
    object storage and durable heartbeat. The release refuses rollback,
    missing image-digest evidence, or a different active revision.
11. Confirm `/livez` and `/readyz` through the configured hostname and the
    worker's ECS health result. The restricted web capability cannot serve
    machine routes such as `/health`; do not inject the worker credential
    into web to make that route pass.
    Deliver and use a real sign-in link to the declared synthetic test
    recipient. Before requesting preparation for the synthetic project, run
    its explicit `configure-release-preparation` declaration through the
    worker capability (the retained Batch task). The existing supervisor
    requires a declared handler; a healthy idle process alone does not
    establish that preparation can execute. The complete command is in the
    [Due Work runbook](../operations/due-work-runtime.md).
    Run the synthetic source → Review → preparation → authorized
    package workflow, retain the preparation attempt and Due Work receipt,
    and verify object dereferencing with the runtime roles. Record the image
    digest, task ARNs, customer/environment binding, and exact receipt IDs.
12. Rehearse the RDS point-in-time restore below. A successful migration,
    task rollout or HTTP probe alone does not close #489.

If migration or rollout fails, both services remain stopped. Inspect the
migration task's exit status and retained logs, repair the failure, and rerun
the release of the same commit. An already-published immutable image is reused
only after verifying its revision label. Do not restart an old image against
a changed schema as an automatic recovery step.

### Required point-in-time restore rehearsal

Use synthetic data only and a distinct temporary RDS instance. Keep the
control-plane receipt store outside the customer environment being restored
or destroyed.

1. Create a synthetic project and known content-addressed objects; record
   exact accepted state, object identities and digests, source RDS instance,
   database name, image digest and migration head.
2. Wait until the intended restore point is within RDS's available recovery
   window and record its UTC timestamp.
3. Change the source database afterward and retain both the earlier and
   later observed states. The states must differ: an unchanged source does
   not prove point-in-time recovery.
4. Restore that timestamp into a new instance using
   `aws rds restore-db-instance-to-point-in-time`, with the intended isolated
   subnet group and security groups. Never substitute an in-place restore.
5. Connect to the restored endpoint with TLS verification. Prove the earlier
   state is present and the later mutation is absent; record both results.
6. Reconcile all restored object references with the retained S3 namespace
   and verify their exact bytes/digests through the normal storage interface.
7. Provision an empty, temporary **rehearsal control-plane database** on the
   retained control-plane instance, with separate operations and resolver
   logins. Point only the rehearsal processes at it. Preserve the three
   customer/environment/deployment IDs in the restored database's immutable
   `customer_environment_binding`; do not invent new IDs, rewrite that row,
   or change the primary registry's endpoint.
   Supply those original IDs, the **restored** customer endpoint and its
   runtime credentials, the temporary control-plane URLs, and the retained
   object namespace to `make deployment-bootstrap ARGS=configure`. Its empty
   registry can register the restored endpoint under the preserved IDs, and
   the bootstrap verifies the existing attestation before migration. Enable
   that rehearsal registry entry through the explicit operations command;
   then use `configure --require-enabled` to prove both routed capabilities.
   Run the synthetic workflow only through these isolated processes and
   retain its receipts. Production processes retain their primary resolver
   URLs and routes throughout. The local bootstrap fixture proves that a
   matching retained attestation works with a fresh registry; it does not
   substitute for this actual RDS restore.
8. Retain the restore receipt externally, including the source and target
   instance IDs, restore timestamp, both state observations, object check
   results, workflow receipts and cleanup outcome.
9. Delete only the named temporary restored instance and the temporary
   rehearsal control-plane database/logins. Preserve the primary control
   plane and externally retained receipt. Record the temporary resources'
   final absence and any remaining snapshots or backup expiration. A failed
   cleanup remains outstanding in the receipt.

No restore receipt has been recorded here. #514 later consumes external
disposition receipts; this rehearsal does not claim customer destruction or
customer activation.

## Cost

Unit prices pulled from the AWS Price List API for `us-east-2` on 2026-09-04.
Public IPv4 is AWS's published `$0.005` per address-hour, not from that pull.
These are historical planning figures. A serving release now requires the
worker as well as web, so it uses the **Both running** column; the web-only
column is no longer a supported release configuration. The separate
control-plane store is not included in this older estimate. The approved
monthly budget is recorded below; refresh the complete topology estimate
under #601 before deployment.

| Item | Unit | Idle (web 1, batch 0) | Both running |
|---|---|---:|---:|
| Fargate web 0.25 vCPU / 0.5 GB | $0.04048 vCPU-hr, $0.004445 GB-hr | $9.01 | $9.01 |
| Fargate batch 1 vCPU / 2 GB | same | $0.00 | $36.04 |
| ALB | $0.0225/hr + $0.008/LCU-hr | $18.43 | $18.43 |
| Public IPv4 | $0.005/addr-hr | $10.95 (3) | $14.60 (4) |
| RDS db.t4g.micro + 20 GB gp3 | $0.016/hr, $0.115/GB-mo | $13.98 | $13.98 |
| Secrets Manager ×3 | $0.40/secret-mo | $1.20 | $1.20 |
| CloudWatch + VPC flow logs | $0.50/GB, $0.03/GB-mo | ~$1.60 | ~$2.10 |
| ECR + S3 | $0.10, $0.023 /GB-mo | $0.32 | $0.32 |
| NAT Gateway | excluded by design | $0.00 | $0.00 |
| **Total** | | **≈ $56** | **≈ $96** |

Scaling web to 0 as well leaves the ALB, its two addresses, RDS, secrets and
storage: **≈ $42/month**. RDS and the ALB are the floor; they do not go away
when the services stop.

On 2026-09-08, the owner approved USD 150/month and
`Corridor-Nonproduction-Monthly` was created and read back **HEALTHY**. Its
actual-spend alerts at 80% and 100%, and forecast-spend alert at 100%, were
verified against the owner-selected email; #601 retains that verification.
The earlier $100 example remains a historical estimate excluding the control
plane. A current estimate for the complete serving topology is still required
before deployment. Budgets alert; they do not cap spend.

## What is deliberately not here

Fargate Spot, IAM Identity Center, CloudTrail data events, WAF, Redis,
Multi-AZ, secret rotation, and interface VPC endpoints. Each is a later
decision, and several are prerequisites of #535 rather than of this
environment.
