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
    """IAM action matching: only a trailing * is meaningful in these documents."""
    if pattern == "*":
        return True
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
