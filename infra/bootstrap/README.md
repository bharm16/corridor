# Bootstrap policies

`cdk bootstrap` creates five IAM roles, a staging bucket, a staging ECR
repository, and an SSM version parameter. The one that matters here is the
**CloudFormation execution role**: it is what actually creates every resource,
and by default it is given `AdministratorAccess`.

These two policies exist so that default is never taken. They are deliberately
a **v1**: broad enough to deploy the four known stacks, materially narrower
than administrator. CloudTrail and IAM Access Analyzer evidence from the first
deployment is what refines them. Waiting for that evidence before writing any
policy would mean bootstrapping unrestricted, which is the thing to avoid.

| File | Becomes |
|---|---|
| `cloudformation-execution-policy.json` | `CorridorCdkIamProvisioning`, attached to the CloudFormation execution role alongside `PowerUserAccess` |
| `corridor-permissions-boundary.json` | `CorridorNonproductionBoundary`, attached to every role CDK creates |

`PowerUserAccess` covers every non-IAM service and already refuses IAM writes,
so the custom policy supplies exactly the IAM and OIDC operations the
synthesized stacks need and nothing else.

## What the split enforces

- Corridor's roles live under the path `/corridor/nonproduction/`, and the
  execution role can only write roles on that path. It cannot touch the
  bootstrap roles, or anything a person created.
- **Every role it creates must carry the boundary.** The `Deny` on `CreateRole`
  without `iam:PermissionsBoundary` is what makes that unavoidable rather than
  merely intended, so a role cannot be created with more authority than the
  boundary allows.
- `iam:PassRole` is scoped to that same path *and* conditioned on
  `iam:PassedToService`. Unscoped `PassRole` next to compute-creation
  permissions is a privilege-escalation path: it lets the holder run a task as
  any role in the account.
- No IAM users, access keys, login profiles or service-specific credentials,
  under any circumstances. The account has no long-lived keys today and this
  keeps it that way.
- No Organizations, account or billing mutation.
- The boundary additionally denies deleting itself and denies stopping or
  altering the CloudTrail trail, so a Corridor task cannot disable the record
  of what it did.

## Creating them

Substitute nothing; the account id is already the dedicated nonproduction
account. Run as the MFA-protected bootstrap administrator.

```bash
aws iam create-policy \
  --policy-name CorridorNonproductionBoundary \
  --policy-document file://infra/bootstrap/corridor-permissions-boundary.json \
  --profile corridor
```

```bash
aws iam create-policy \
  --policy-name CorridorCdkIamProvisioning \
  --policy-document file://infra/bootstrap/cloudformation-execution-policy.json \
  --profile corridor
```

Then bootstrap with both, and with the boundary applied to the roles bootstrap
itself creates:

```bash
cd infra && npx cdk bootstrap aws://810100779593/us-east-2 \
  --cloudformation-execution-policies "arn:aws:iam::aws:policy/PowerUserAccess,arn:aws:iam::810100779593:policy/CorridorCdkIamProvisioning" \
  --custom-permissions-boundary CorridorNonproductionBoundary \
  --profile corridor
```

Omitting `--cloudformation-execution-policies` is what silently produces the
`AdministratorAccess` execution role. It is not a default worth inheriting.

## After the first deployment

1. Read CloudTrail for the calls CloudFormation actually made, filtered to the
   execution role.
2. Run IAM Access Analyzer policy generation against that activity.
3. Replace `PowerUserAccess` with the generated, service-scoped policy.
4. Re-bootstrap with the narrowed list and confirm `cdk diff` is empty.

Do not narrow it by guessing before the stacks have run once. A policy that is
too tight fails halfway through a stack and leaves it in `UPDATE_ROLLBACK`,
which is worse than one that is briefly too broad in an account holding no
customer data.
