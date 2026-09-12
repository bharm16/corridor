# Corridor nonproduction infrastructure

AWS CDK v2 (Python) for Corridor's nonproduction environment in `us-east-2`.
Isolated from the application: `aws-cdk-lib` is **not** in Corridor's
`pyproject.toml`, and nothing in `src/corridor` imports from here.

These definitions do not establish deployment completion. #489 remains open
until the synthetic environment runs and its point-in-time restore receipt is
retained; #601 supplies the verified account and deployment inputs.

## Stacks

| Stack | Holds | Deployed by |
|---|---|---|
| `CorridorAccountFoundation` | CloudTrail trail + audit buckets, GitHub OIDC provider, separate CDK and application-release roles | locally, once |
| `CorridorNetwork` | VPC, subnets, flow logs, all security groups | GitHub Actions |
| `CorridorData` | RDS PostgreSQL 16, artifact bucket, secrets. **Stateful** | GitHub Actions |
| `CorridorControlPlane` | Separate RDS database and owner/operations/resolver credentials. **Stateful, outside customer destruction** | GitHub Actions |
| `CorridorApplication` | ECR, ECS cluster, web and Due Work services, migration task, ALB | GitHub Actions |

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

cd ..
make test-infra                          # synthesized-template assertions
cd infra
./node_modules/.bin/cdk synth --strict --quiet \
  --context corridor:certificateArn=<acm-arn> \
  --context corridor:publicHostname=<hostname> \
  --context corridor:signInSender=<verified-sender> \
  --context corridor:imageTag=<commit-sha> \
  --context corridor:customerId=<synthetic-customer-id> \
  --context corridor:customerEnvironmentId=<synthetic-environment-id> \
  --context corridor:deploymentId=<stable-deployment-id> \
  --context corridor:dataClass=synthetic
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

- no NAT Gateway; the S3 gateway endpoint exists; both services start at 0
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
- the sign-in record expiry pass (`expire-sign-in-records`) runs on a daily
  EventBridge schedule, exactly once per environment, on the existing batch
  task definition, under a scoped EventBridge role that adds no application
  grant (ADR-0102, #943)

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

**The worker service runs the existing Due Work supervisor.** Its command is
`python -m corridor.due_work_cli supervise --poll-seconds=5`. Each process
generates its own `runtime:` owner identity; durable claims and receipts govern
retries and recovery. The existing Batch task definition, credentials, and
roles are retained. The ECS health check runs `make due-work ARGS=health`'s
CLI through the container entrypoint so it receives the worker credential,
checks the database and object store, and reads the durable worker heartbeat.
ECS replaces unhealthy tasks; a CloudWatch alarm also detects fewer running
workers than the service requests, using service and cluster dimensions only.
Both services start at zero. The application release drains both before
migration and verifies both revisions and image digests before reporting a
release. A serving web deployment requires a running worker.

**The customer routing key is a real runtime contract.** #656 requires a
separate random key to bind the browser session to its customer environment.
`CorridorData` generates that secret and only the web task receives it. Both
runtime tasks receive the scoped control-plane resolver login, never its owner
or operations credential. The existing Migration task initializes and binds
both databases through `make deployment-bootstrap ARGS=configure`; a missing
registry entry is created disabled. Explicit control-plane operations enable
the route after inspection. Ordinary releases preserve enabled, hold and
connector state, and refuse a disabled route before starting services.

**No `OPENAI_API_KEY` secret.**
`openai_api_key` defaults to empty in `config.py` and the deterministic UCM
path calls no model. Model-backed source paths remain separately configured
and authorized.

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
