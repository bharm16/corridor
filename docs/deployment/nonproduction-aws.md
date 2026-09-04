# Corridor nonproduction on AWS — runbook

Account `810100779593`, region `us-east-2`. Serves #601 (account and deployment
identity) and #489 (synthetic environment foundation).

**Status: not deployed.** The CDK in [`infra/`](../../infra) synthesizes and
passes its tests; no AWS resource has been created.

## Prerequisites that block the first deploy

These are account facts, verified read-only on 2026-09-04. Each must be true
before `cdk bootstrap`:

| Prerequisite | State | Who |
|---|---|---|
| Root MFA enabled | done | — |
| No root or IAM access keys | done | — |
| **MFA on `bryceharmon`** | **missing** | you, console |
| **Delete unused `bryce` user** | **exists, console access** | you, console |
| **Account password policy** | **not set** | you, console |
| **Cost Explorer enabled** | **not enabled** | you, console (root must first activate IAM billing access) |
| **Budget + alerts** | **none** | you, console |
| **ACM certificate for the ALB** | **none** | you |
| **Dockerfile** | **does not exist in the repo** | a separate change |
| RDS minor version still offered | verify | `aws rds describe-db-engine-versions --engine postgres --engine-version 16` |

`bryceharmon` holds `AdministratorAccess` through the `Admin` group and has no
MFA device. It is acceptable as a *temporary bootstrap identity* once MFA is
on; it should not remain the permanent administrative path.

### The Dockerfile is a real blocker

No Dockerfile exists — only `docker-compose.yml`, which runs PostgreSQL for
local development. `CorridorApplication` therefore references an image by tag
in ECR rather than building one. The image must provide:

- Python 3.12 (`requires-python = ">=3.12,<3.13"`)
- `uv` **on PATH** — `render_profiles.py` shells out to
  `uv run --project workers/render --frozen python render_worker.py`
- both uv projects synced: the root and `workers/render`
- the `tesseract` binary (`scripts/ci_environment.sh` installs `tesseract-ocr`)
- WeasyPrint's native stack (pango, cairo, gdk-pixbuf, libffi)
- the source tree at a stable path, since the render subprocess resolves
  `workers/render` relative to the repository root

### The ALB certificate

Without `corridor:certificateArn` in context, the stack synthesizes an **HTTP**
listener and no redirect. That is deliberate: an HTTPS listener cannot
synthesize without a certificate, and silently shipping plaintext as though it
were intended would be worse than failing visibly here. Issue an ACM
certificate, set the context value, and the stack switches to 443 with a 80→443
redirect and `TLS13_RES`.

## Deployment order

Nothing below has been run.

1. Complete every prerequisite above.
2. Review the PR; run `pytest infra/tests` and `cdk synth --strict`.
3. `cdk bootstrap aws://810100779593/us-east-2` — locally, as the MFA-protected
   admin. This itself creates a stack: an asset bucket, an ECR repository, an
   SSM version parameter, and **five** IAM roles (`deploy`, `lookup`,
   `file-publishing`, `image-publishing`, and the CloudFormation execution
   role). The execution role defaults to `AdministratorAccess`; pass
   `--cloudformation-execution-policies` deliberately rather than accepting it
   by omission.
4. Deploy `CorridorAccountFoundation` locally. This creates the OIDC provider
   and `corridor-nonprod-deploy`.
5. Create the GitHub `nonproduction` environment with protection rules. The
   role's trust subject is
   `repo:bharm16/corridor:environment:nonproduction`, so the environment's
   rules are load-bearing, not decorative.
6. Verify OIDC with a workflow that calls only `sts:GetCallerIdentity`.
7. Record account id, region, and role ARN on #601 — **identifiers only, never
   a secret.** That satisfies #601's checklist.
8. From GitHub Actions: `CorridorNetwork`, then `CorridorData`, then
   `CorridorApplication` (web desired count 0).
9. Build and push the image; run the migration task (`alembic upgrade head`),
   which creates the `corridor_web` and `corridor_worker` roles from the
   baseline migration.
10. Scale web to 1; confirm `GET /health` through the ALB.
11. Run the batch task for the rehearsal; return it to 0.
12. Rehearse the RDS point-in-time restore #489 requires.

## Cost

Unit prices pulled from the AWS Price List API for `us-east-2` on 2026-09-04.
Public IPv4 is AWS's published `$0.005` per address-hour, not from that pull.

| Item | Unit | Idle (web 1, batch 0) | Both running |
|---|---|---:|---:|
| Fargate web 0.25 vCPU / 0.5 GB | $0.04048 vCPU-hr, $0.004445 GB-hr | $9.01 | $9.01 |
| Fargate batch 1 vCPU / 2 GB | same | $0.00 | $36.04 |
| ALB | $0.0225/hr + $0.008/LCU-hr | $18.43 | $18.43 |
| Public IPv4 | $0.005/addr-hr | $10.95 (3) | $14.60 (4) |
| RDS db.t4g.micro + 20 GB gp3 | $0.016/hr, $0.115/GB-mo | $13.98 | $13.98 |
| Secrets Manager ×4 | $0.40/secret-mo | $1.60 | $1.60 |
| CloudWatch + VPC flow logs | $0.50/GB, $0.03/GB-mo | ~$1.60 | ~$2.10 |
| ECR + S3 | $0.10, $0.023 /GB-mo | $0.32 | $0.32 |
| NAT Gateway | excluded by design | $0.00 | $0.00 |
| **Total** | | **≈ $56** | **≈ $96** |

Scaling web to 0 as well leaves the ALB, its two addresses, RDS, secrets and
storage: **≈ $42/month**. RDS and the ALB are the floor; they do not go away
when the services stop.

A $50 budget would alert immediately at the idle baseline. A **$100 monthly
budget** with alerts at 50%, 80% and 100% actual plus 100% forecast is the
honest shape. Budgets alert; they do not cap spend.

## What is deliberately not here

Fargate Spot, IAM Identity Center, CloudTrail data events, WAF, Redis,
Multi-AZ, secret rotation, and interface VPC endpoints. Each is a later
decision, and several are prerequisites of #535 rather than of this
environment.
