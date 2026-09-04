# Bootstrap policies

`cdk bootstrap` creates five IAM roles, a staging bucket, a staging ECR
repository, and an SSM version parameter. The one that matters here is the
**CloudFormation execution role**: it is what actually creates every resource,
and by default it is given `AdministratorAccess`.

These three documents exist so that default is never taken.

| File | Becomes | Attached to |
|---|---|---|
| `cloudformation-execution-policy.json` | `CorridorCdkIamProvisioning` | the CloudFormation execution role, alongside `PowerUserAccess` |
| `corridor-cdk-execution-boundary.json` | `CorridorCdkExecutionBoundary` | the CloudFormation execution role, via `--custom-permissions-boundary` |
| `corridor-delegated-role-boundary.json` | `CorridorDelegatedRoleBoundary` | every role the CDK app creates, via the `@aws-cdk/core:permissionsBoundary` context |

## Why there are two boundaries and not one

A permissions boundary is a **maximum-permission** policy, not a deny list. An
operation must be allowed by the identity policy *and* by the boundary, so an
action the boundary simply does not mention is an implicit deny.

An earlier version of this directory had a single boundary that allowed the
application services and never mentioned IAM or CloudTrail. Applied to the
CloudFormation execution role it would have refused `CreateRole`,
`PutRolePolicy`, `PassRole`, `CreateOpenIDConnectProvider` and every CloudTrail
call, so `cdk bootstrap` would have appeared to succeed and the first
deployment would have failed partway through. It also explicitly denied
CloudTrail updates while `CorridorAccountFoundation` asks CloudFormation to
manage the trail.

The two roles want opposite things, which is why they cannot share a document:

- The **execution role** must be able to create roles, attach boundaries,
  create the OIDC provider and manage the trail. It is the authority that
  builds the environment.
- A **delegated role** -- a task role, an execution role, the CI roles -- must
  never do any of that. It runs inside the environment.

`test_the_execution_boundary_permits_everything_the_execution_policy_grants`
evaluates the first two documents against each other, so a future edit that
grants an action the boundary does not cover fails in CI rather than halfway
through a CloudFormation update.

## What the design enforces

- Corridor's roles live under `/corridor/nonproduction/`, set explicitly in the
  stacks, and the execution policy only writes roles on that path.
- **Every role created must carry the delegated boundary.** The `Deny` on
  `CreateRole` without `iam:PermissionsBoundary` makes that unavoidable, and
  the matching `Allow` on `iam:PutRolePermissionsBoundary` is what lets the
  boundary actually be attached. Denying the absence without granting the
  attachment would fail every role creation.
- `iam:PassRole` is scoped to that path *and* conditioned on
  `iam:PassedToService`. Unscoped `PassRole` beside compute creation lets the
  holder run a task as any role in the account.
- The delegated boundary denies all IAM mutation and all CloudTrail mutation,
  so a running task cannot grant itself authority or stop the record of what it
  did. It still permits `iam:PassRole`, which ECS `RunTask` requires.
- No IAM users, access keys or login profiles in any of the three documents.
- No Organizations, account or billing mutation in any of the three.

## Creating them

Run as the MFA-protected bootstrap administrator. The account id is already the
dedicated nonproduction account.

```bash
aws iam create-policy --policy-name CorridorCdkExecutionBoundary \
  --policy-document file://infra/bootstrap/corridor-cdk-execution-boundary.json \
  --profile corridor
```

```bash
aws iam create-policy --policy-name CorridorDelegatedRoleBoundary \
  --policy-document file://infra/bootstrap/corridor-delegated-role-boundary.json \
  --profile corridor
```

```bash
aws iam create-policy --policy-name CorridorCdkIamProvisioning \
  --policy-document file://infra/bootstrap/cloudformation-execution-policy.json \
  --profile corridor
```

Then bootstrap with the execution policies and the execution boundary:

```bash
cd infra && ./node_modules/.bin/cdk bootstrap aws://810100779593/us-east-2 \
  --cloudformation-execution-policies "arn:aws:iam::aws:policy/PowerUserAccess,arn:aws:iam::810100779593:policy/CorridorCdkIamProvisioning" \
  --custom-permissions-boundary CorridorCdkExecutionBoundary \
  --profile corridor
```

The delegated boundary is not passed here. It is applied to the roles the
*application* creates, through the `@aws-cdk/core:permissionsBoundary` context
already set in `infra/cdk.json`.

Omitting `--cloudformation-execution-policies` is what silently produces the
`AdministratorAccess` execution role. It is not a default worth inheriting.

## After the first deployment

1. Read CloudTrail for the calls CloudFormation actually made, filtered to the
   execution role.
2. Run IAM Access Analyzer policy generation against that activity.
3. Replace `PowerUserAccess` with the generated, service-scoped policy, and
   narrow `CorridorCdkExecutionBoundary` to match.
4. Re-bootstrap with the narrowed list and confirm `cdk diff` is empty.

Do not narrow by guessing before the stacks have run once. A policy that is too
tight fails halfway through and leaves a stack in `UPDATE_ROLLBACK`, which is
worse than one briefly too broad in an account holding no customer data.
