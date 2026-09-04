# Corridor nonproduction infrastructure

AWS CDK v2 (Python) for Corridor's nonproduction environment in `us-east-2`.
Isolated from the application: `aws-cdk-lib` is **not** in Corridor's
`pyproject.toml`, and nothing in `src/corridor` imports from here.

Nothing in this directory has been deployed. No AWS resource has been created.

## Stacks

| Stack | Holds | Deployed by |
|---|---|---|
| `CorridorAccountFoundation` | CloudTrail trail + audit buckets, GitHub OIDC provider, `corridor-nonprod-deploy` role | locally, once |
| `CorridorNetwork` | VPC, subnets, flow logs, all security groups | GitHub Actions |
| `CorridorData` | RDS PostgreSQL 16, artifact bucket, secrets. **Stateful** | GitHub Actions |
| `CorridorApplication` | ECR, ECS cluster, three task shapes, ALB | GitHub Actions |

`CorridorData` sets `termination_protection=True`, the database is
`RemovalPolicy.SNAPSHOT` with deletion protection, and every bucket is
`RETAIN`. Destroying the application stack cannot take project data with it.

**Do not rename constructs in `data_stack.py`.** CloudFormation derives logical
IDs from construct IDs; a rename after the first deploy reads as delete-and-
recreate, which for the database means losing it.

## Local commands

```bash
cd infra
uv sync --frozen                          # its own locked project, not Corridor's
npm ci                                    # the CDK CLI, pinned in package-lock.json
export PATH="$PWD/.venv/bin:$PATH"

uv run python -m pytest tests -q          # 77 assertions
./node_modules/.bin/cdk synth --strict --quiet \
  --context corridor:certificateArn=<acm-arn> \
  --context corridor:imageTag=<commit-sha>
```

`infra/` is its own uv project with its own `uv.lock`, and the CDK CLI is
pinned in `package.json`. Both are installed with a frozen resolve
(`uv sync --frozen`, `npm ci`) and both happen *before* any workflow assumes
an AWS role: a dependency resolution while a credential that can deploy stacks
is active would let a compromised release use it.

`cdk diff` and `cdk deploy` need credentials and a bootstrapped account; see
[the runbook](../docs/deployment/nonproduction-aws.md).

## What the tests pin

They assert on synthesized CloudFormation, not on the Python, so they fail on
the template that would actually deploy:

- no NAT Gateway; the S3 gateway endpoint exists; web service starts at 0
- RDS is not public, is encrypted, retains on delete, has 7-day backups
- nothing opens 5432 to a CIDR; only ports 80/443 accept `0.0.0.0/0`
- every bucket blocks all public access; the artifact bucket is versioned
- the trail is multi-region with log-file validation
- the GitHub trust subject is exactly `repo:bharm16/corridor:environment:nonproduction`, with no wildcard
- no policy anywhere grants `iam:PassRole` on `*`
- each task has its **own** execution role, and only the migration role can
  read the RDS admin credential
- every environment variable a task sets is a name `config.py` actually reads
  (or a documented entrypoint input)
- each task declares its role, and the stack never lowers database TLS
- the load balancer checks `/readyz`, not `/health`
- the migration receives all three credentials; web and batch receive only
  their own
- absence of a certificate is refused rather than silently serving plaintext
- the image tag is immutable and never the `bootstrap` placeholder
- the bootstrap policies scope `PassRole`, confine roles to Corridor's path,
  and refuse to create a role without the permissions boundary
- the web role cannot delete an artifact; deletion stays with the batch
  retention capability that runs the deletion policy
- the web command trusts forwarded headers only from the VPC, so one caller
  cannot spend the sign-in allowance for everybody
- a certificate requires the hostname it covers, and serving requires a
  verified sign-in sender

Those were mutation-tested: collapsing the three execution roles back
into one makes both fail.

## Deliberate choices

**No NAT Gateway.** $0.045/hr (~$32.85/mo) plus per-GB, more than the database.
Interface endpoints for ECR/Secrets/Logs would be ~$29/mo and no better. Tasks
run in public subnets with public IPs for egress only; every inbound path is
closed by security group. The database is in isolated subnets with no route to
the internet gateway at all.

**One ECR repository.** `workers/render/render_worker.py` is a subprocess that
`src/corridor/render_profiles.py` shells out to from inside the same tree, and
the batch commands are entry points in the same package. No separate build
context exists, so two repositories would publish identical bytes twice and let
web and batch drift onto different revisions.

**No worker service.** There is no long-running worker in the repository: no
`make worker`, no consumer loop, and `worker_database_url` is read only in
`src/corridor/db.py`. The worker is a *database capability*, so it is a task
definition run on demand. #489's replica-safe leases are the prerequisite for
promoting it to a service.

**No `OPENAI_API_KEY` secret, and no application/session secret.**
`openai_api_key` defaults to empty in `config.py` and the deterministic UCM
path calls no model. Corridor's `Settings` has no session or signing-secret
field at all -- there is no `app_secret`, `session_secret` or `secret_key` --
so creating one would invent a contract the application does not have.

**Environment variable names are asserted against `config.py`.** `Settings`
has no `env_prefix`: a field with a `validation_alias` uses that alias, and a
field without one uses its own name uppercased. So the passwords are
`CORRIDOR_WEB_DB_PASSWORD` and `CORRIDOR_WORKER_DB_PASSWORD`, but the URLs are
`DATABASE_URL`, `WEB_DATABASE_URL` and `WORKER_DATABASE_URL`. The stack injects
connection *parts* and the image entrypoint composes the URLs, because a
password is only a secret reference at task-definition time and the
RDS-managed secret is JSON, not a URL. See the runbook.

**Four cdk-nag suppressions**, each with a written, resource-specific reason in
the source: terminal access-log buckets (S3 cannot log into itself), the ALB's
`0.0.0.0/0` ingress (that is what internet-facing means), Single-AZ RDS
(nonproduction cost decision), the default database port (nothing can reach it
to scan), plaintext env vars (all four are non-secret), and secret rotation
(needs NAT or a paid endpoint this environment deliberately lacks; a #535
prerequisite). No blanket suppressions.
