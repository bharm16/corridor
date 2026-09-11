"""The bootstrap policies, evaluated rather than merely parsed.

An earlier version of these tests checked static properties -- valid JSON, no
wildcard PassRole, denies present -- and passed against a boundary that would
have made deployment impossible. A permissions boundary is a *maximum*
permission policy, not a deny list: an operation must be allowed by both the
identity policy and the boundary, so an action the boundary simply fails to
mention is an implicit deny. The boundary allowed the application services and
never mentioned IAM or CloudTrail, so CreateRole, PassRole, the OIDC provider
and the whole CorridorAccountFoundation trail would have been refused.

So the central test here is `test_the_execution_boundary_permits_everything_
the_execution_policy_grants`, which evaluates one document against the other
instead of inspecting each alone.
"""

from __future__ import annotations

import json
import pathlib

import pytest

BOOTSTRAP = pathlib.Path(__file__).parents[1] / "bootstrap"
ACCOUNT = "810100779593"
DELEGATED_ARN = f"arn:aws:iam::{ACCOUNT}:policy/CorridorDelegatedRoleBoundary"
ROLE_PATH = f"arn:aws:iam::{ACCOUNT}:role/corridor/nonproduction/*"


def _load(name: str) -> dict:
    return json.loads((BOOTSTRAP / name).read_text())


@pytest.fixture(scope="module")
def execution_policy() -> dict:
    return _load("cloudformation-execution-policy.json")


@pytest.fixture(scope="module")
def execution_boundary() -> dict:
    return _load("corridor-cdk-execution-boundary.json")


@pytest.fixture(scope="module")
def delegated_boundary() -> dict:
    return _load("corridor-delegated-role-boundary.json")


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else action


def _matches(pattern: str, action: str) -> bool:
    """Whether a policy `pattern` covers `action`.

    `action` may itself be a pattern, because CDK's grants emit families like
    `s3:GetObject*`. A ceiling entry only covers a granted family if it is that
    family or broader: `s3:GetObject` does not admit a grant of
    `s3:GetObject*`, since the grant includes actions the ceiling never named.
    """
    if pattern == "*":
        return True
    if action.endswith("*"):
        return pattern.endswith("*") and action.lower().startswith(
            pattern[:-1].lower()
        )
    if pattern.endswith("*"):
        return action.lower().startswith(pattern[:-1].lower())
    return pattern.lower() == action.lower()


def permits(policy: dict, action: str) -> bool:
    """Whether `policy` allows `action`, with an explicit Deny winning.

    Every statement in these boundaries is `Resource: "*"` or a single ARN, and
    the question asked here is only about the action, so resources are not
    modelled. That is sufficient to catch the failure mode that matters: an
    action the boundary never mentions at all.
    """
    for statement in policy["Statement"]:
        if statement["Effect"] != "Deny":
            continue
        if any(_matches(pattern, action) for pattern in _actions(statement)):
            return False
    for statement in policy["Statement"]:
        if statement["Effect"] != "Allow":
            continue
        if any(_matches(pattern, action) for pattern in _actions(statement)):
            return True
    return False


# --- the finding this file exists for ---------------------------------
def test_the_execution_boundary_permits_everything_the_execution_policy_grants(
    execution_policy, execution_boundary
):
    """Every action the identity policy allows must also be within the boundary.

    An omission here is an implicit deny that only surfaces partway through a
    real deployment, leaving a stack in UPDATE_ROLLBACK.
    """
    refused = sorted(
        {
            action
            for statement in execution_policy["Statement"]
            if statement["Effect"] == "Allow"
            for action in _actions(statement)
            if not permits(execution_boundary, action)
        }
    )
    assert not refused, f"the execution boundary silently denies: {refused}"


def test_the_execution_boundary_permits_the_foundation_stacks_trail(
    execution_boundary,
):
    """CorridorAccountFoundation asks CloudFormation to create and manage the
    trail, so the execution role's boundary cannot refuse those calls."""
    for action in (
        "cloudtrail:CreateTrail",
        "cloudtrail:UpdateTrail",
        "cloudtrail:PutEventSelectors",
        "cloudtrail:StartLogging",
        "cloudtrail:AddTags",
    ):
        assert permits(execution_boundary, action), action


def test_the_execution_boundary_permits_role_and_provider_creation(
    execution_boundary,
):
    for action in (
        "iam:CreateRole",
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:PassRole",
        "iam:PutRolePermissionsBoundary",
        "iam:CreateOpenIDConnectProvider",
        "iam:CreateServiceLinkedRole",
    ):
        assert permits(execution_boundary, action), action


# --- the delegated boundary is the one that refuses ---------------------
def test_the_delegated_boundary_refuses_identity_mutation(delegated_boundary):
    """A running task must not be able to grant itself anything."""
    for action in (
        "iam:CreateRole",
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:CreateUser",
        "iam:CreateAccessKey",
        "iam:UpdateAssumeRolePolicy",
    ):
        assert not permits(delegated_boundary, action), action


def test_the_delegated_boundary_refuses_audit_mutation(delegated_boundary):
    for action in (
        "cloudtrail:StopLogging",
        "cloudtrail:DeleteTrail",
        "cloudtrail:UpdateTrail",
        "logs:DeleteLogGroup",
    ):
        assert not permits(delegated_boundary, action), action


def test_the_delegated_boundary_permits_what_the_tasks_actually_do(
    delegated_boundary,
):
    for action in (
        "ecr:GetAuthorizationToken",
        "ecr:PutImage",
        "ecs:RunTask",
        "ecs:StopTask",
        "ecs:UpdateService",
        "s3:GetObject",
        "s3:PutObject",
        "logs:PutLogEvents",
        "secretsmanager:GetSecretValue",
        "iam:PassRole",
    ):
        assert permits(delegated_boundary, action), action


def test_passrole_survives_the_delegated_denies(delegated_boundary):
    """iam:Put*/Create*/... deny families must not swallow iam:PassRole, which
    the release role legitimately needs. A broader deny pattern here would
    break ECS RunTask in a way no static check would notice."""
    assert permits(delegated_boundary, "iam:PassRole")


# --- structural guarantees --------------------------------------------
def test_all_three_documents_are_valid_policy_documents(
    execution_policy, execution_boundary, delegated_boundary
):
    for policy in (execution_policy, execution_boundary, delegated_boundary):
        assert policy["Version"] == "2012-10-17"
        assert policy["Statement"]
        for statement in policy["Statement"]:
            assert statement["Effect"] in {"Allow", "Deny"}
            assert "Sid" in statement, statement


def test_no_document_permits_a_long_lived_identity(
    execution_policy, execution_boundary, delegated_boundary
):
    for policy in (execution_policy, execution_boundary, delegated_boundary):
        for action in ("iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile"):
            assert not permits(policy, action), action


def test_no_document_permits_account_or_organization_changes(
    execution_policy, execution_boundary, delegated_boundary
):
    for policy in (execution_policy, execution_boundary, delegated_boundary):
        for action in ("organizations:LeaveOrganization", "account:PutContactInformation"):
            assert not permits(policy, action), action


def test_passrole_in_the_execution_policy_is_path_and_service_scoped(
    execution_policy,
):
    for statement in execution_policy["Statement"]:
        if statement["Effect"] != "Allow" or "iam:PassRole" not in _actions(statement):
            continue
        assert statement["Resource"] == ROLE_PATH
        passed_to = statement["Condition"]["StringEquals"]["iam:PassedToService"]
        assert "ecs-tasks.amazonaws.com" in passed_to


def test_a_role_cannot_be_created_without_the_delegated_boundary(execution_policy):
    denies = [
        statement
        for statement in execution_policy["Statement"]
        if statement["Effect"] == "Deny" and "iam:CreateRole" in _actions(statement)
    ]
    assert len(denies) == 1
    assert (
        denies[0]["Condition"]["StringNotEquals"]["iam:PermissionsBoundary"]
        == DELEGATED_ARN
    )


def test_the_execution_policy_can_attach_the_boundary_it_requires(execution_policy):
    """Denying CreateRole without a boundary does not grant the operation that
    attaches one; both are needed or every role creation fails."""
    allowed = any(
        statement["Effect"] == "Allow"
        and "iam:PutRolePermissionsBoundary" in _actions(statement)
        for statement in execution_policy["Statement"]
    )
    assert allowed


def test_the_readme_documents_both_boundaries(execution_policy):
    readme = (BOOTSTRAP / "README.md").read_text()

    assert "--cloudformation-execution-policies" in readme
    assert "CorridorCdkExecutionBoundary" in readme
    assert "CorridorDelegatedRoleBoundary" in readme
    assert "arn:aws:iam::aws:policy/PowerUserAccess" in readme


# --- generic: the boundaries against what is actually synthesized -------
#
# The action-only test above compares two handwritten documents. That is not
# enough on its own: it missed the delegated boundary refusing
# logs:PutRetentionPolicy, which a CDK helper needed, and it missed the release
# role's cloudformation:DescribeStacks, which the boundary never mentioned. Both
# were found by a human reading the templates. These tests read the templates
# instead.

# Actions the AWS-managed execution policy contributes, which do not appear in
# any inline document but still have to be within the ceiling.
ECS_TASK_EXECUTION_MANAGED_ACTIONS = (
    "ecr:GetAuthorizationToken",
    "ecr:BatchCheckLayerAvailability",
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchGetImage",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
)


def _synthesized_stacks():
    """Import the stack fixtures lazily so this file stays runnable alone.

    Whatever `_build` synthesizes is what the sweeps below read. Listing the
    stacks here instead is how the control plane came to be exempt from them:
    `_build` returned a four-item tuple that dropped it, and this copy of the
    list never noticed.
    """
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_stacks import _build

    return _build()


def _inline_actions(template) -> dict[str, set[str]]:
    """role logical id -> every action its inline policies grant."""
    by_role: dict[str, set[str]] = {}
    for policy in template.find_resources("AWS::IAM::Policy").values():
        roles = policy["Properties"].get("Roles") or []
        names = [json.dumps(role) for role in roles]
        actions: set[str] = set()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement["Effect"] != "Allow":
                continue
            action = statement.get("Action")
            actions.update([action] if isinstance(action, str) else action)
        for name in names:
            by_role.setdefault(name, set()).update(actions)
    return by_role


def test_the_delegated_boundary_permits_every_action_every_role_is_granted(
    delegated_boundary,
):
    """For each synthesized role, every action its identity policy grants must
    also be within the delegated ceiling -- otherwise the grant is an implicit
    deny that only appears at run time."""
    refused: list[tuple[str, str]] = []
    for name, template in _synthesized_stacks().items():
        for role, actions in _inline_actions(template).items():
            for action in sorted(actions):
                if not permits(delegated_boundary, action):
                    refused.append((f"{name}:{role[:60]}", action))
    assert not refused, f"the delegated boundary silently denies: {refused}"


def test_the_delegated_boundary_permits_the_managed_execution_policy(
    delegated_boundary,
):
    """The ECS execution roles attach an AWS-managed policy whose actions never
    appear in an inline document, so nothing else in this file would notice the
    boundary refusing them."""
    for action in ECS_TASK_EXECUTION_MANAGED_ACTIONS:
        assert permits(delegated_boundary, action), action


def test_the_execution_boundary_covers_every_service_in_the_templates(
    execution_boundary,
):
    """CloudFormation creates whatever the templates contain, so every service
    that appears must be within the execution ceiling. A CDK helper that
    quietly adds a Lambda fails here rather than during a deployment."""
    seen: set[str] = set()
    for template in _synthesized_stacks().values():
        for resource in template.to_json()["Resources"].values():
            kind = resource["Type"]
            if not kind.startswith("AWS::"):
                continue
            seen.add(kind.split("::")[1].lower())

    # CloudFormation service names to the IAM prefix that governs them.
    prefixes = {
        "ec2": "ec2", "ecs": "ecs", "ecr": "ecr", "rds": "rds", "s3": "s3",
        "iam": "iam", "logs": "logs", "cloudtrail": "cloudtrail",
        "cloudwatch": "cloudwatch", "secretsmanager": "secretsmanager",
        "elasticloadbalancingv2": "elasticloadbalancing", "lambda": "lambda",
        "sns": "sns", "ssm": "ssm", "kms": "kms",
    }
    def covered(prefix: str) -> bool:
        """Whether the ceiling mentions this service at all.

        A synthetic probe action does not work: the boundary names CloudTrail
        operations individually rather than with a wildcard, so
        `cloudtrail:CreateSomething` would read as uncovered while
        `cloudtrail:CreateTrail` is right there.
        """
        for statement in execution_boundary["Statement"]:
            if statement["Effect"] != "Allow":
                continue
            for pattern in _actions(statement):
                if pattern == "*" or pattern.lower().startswith(f"{prefix}:"):
                    return True
        return False

    uncovered = []
    for service in sorted(seen):
        prefix = prefixes.get(service)
        if prefix is None:
            continue
        if not covered(prefix):
            uncovered.append(service)
    assert not uncovered, (
        f"templates contain {uncovered} but the execution boundary does not "
        "permit those services"
    )
