"""The bootstrap policies, asserted rather than trusted.

These are plain JSON files a person pastes into `aws iam create-policy`, so
nothing else would catch a typo that widens them. Each test pins one property
that the policy exists in order to have.
"""

from __future__ import annotations

import json
import pathlib

import pytest

BOOTSTRAP = pathlib.Path(__file__).parents[1] / "bootstrap"
ACCOUNT = "810100779593"
BOUNDARY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/CorridorNonproductionBoundary"


def _load(name: str) -> dict:
    return json.loads((BOOTSTRAP / name).read_text())


@pytest.fixture(scope="module")
def execution() -> dict:
    return _load("cloudformation-execution-policy.json")


@pytest.fixture(scope="module")
def boundary() -> dict:
    return _load("corridor-permissions-boundary.json")


def _statements(policy: dict) -> list[dict]:
    return policy["Statement"]


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else action


def test_both_policies_are_valid_policy_documents(execution, boundary):
    for policy in (execution, boundary):
        assert policy["Version"] == "2012-10-17"
        assert _statements(policy)
        for statement in _statements(policy):
            assert statement["Effect"] in {"Allow", "Deny"}
            assert "Sid" in statement, statement


def test_passrole_is_never_unscoped(execution):
    """PassRole on * beside compute creation lets the holder run a task as any
    role in the account."""
    for statement in _statements(execution):
        if statement["Effect"] != "Allow":
            continue
        if not any("PassRole" in action for action in _actions(statement)):
            continue
        assert statement["Resource"] != "*"
        assert statement["Resource"].startswith(
            f"arn:aws:iam::{ACCOUNT}:role/corridor/nonproduction/"
        )
        passed_to = statement["Condition"]["StringEquals"]["iam:PassedToService"]
        assert "ecs-tasks.amazonaws.com" in passed_to


def test_role_creation_is_confined_to_corridors_own_path(execution):
    for statement in _statements(execution):
        if statement["Effect"] != "Allow":
            continue
        if "iam:CreateRole" not in _actions(statement):
            continue
        assert statement["Resource"] == (
            f"arn:aws:iam::{ACCOUNT}:role/corridor/nonproduction/*"
        )


def test_a_role_cannot_be_created_without_the_boundary(execution):
    """The Deny is what makes the boundary unavoidable rather than intended."""
    denies = [
        statement
        for statement in _statements(execution)
        if statement["Effect"] == "Deny"
        and "iam:CreateRole" in _actions(statement)
    ]
    assert len(denies) == 1
    condition = denies[0]["Condition"]["StringNotEquals"]["iam:PermissionsBoundary"]
    assert condition == BOUNDARY_ARN


def test_neither_policy_permits_a_long_lived_identity(execution, boundary):
    forbidden = {"iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile"}
    for policy in (execution, boundary):
        denied: set[str] = set()
        for statement in _statements(policy):
            if statement["Effect"] == "Deny":
                denied.update(_actions(statement))
            else:
                assert not (forbidden & set(_actions(statement))), statement["Sid"]
        assert forbidden <= denied, f"missing denies: {forbidden - denied}"


def test_service_linked_roles_are_constrained_to_named_services(execution):
    statements = [
        statement
        for statement in _statements(execution)
        if "iam:CreateServiceLinkedRole" in _actions(statement)
    ]
    assert len(statements) == 1
    services = statements[0]["Condition"]["StringEquals"]["iam:AWSServiceName"]
    assert set(services) == {
        "ecs.amazonaws.com",
        "elasticloadbalancing.amazonaws.com",
        "rds.amazonaws.com",
    }


def test_the_boundary_refuses_account_and_organization_changes(boundary):
    denied: set[str] = set()
    for statement in _statements(boundary):
        if statement["Effect"] == "Deny":
            denied.update(_actions(statement))
    assert {"organizations:*", "account:*"} <= denied


def test_the_boundary_protects_the_audit_trail(boundary):
    """A task must not be able to stop the record of what it did."""
    denied: set[str] = set()
    for statement in _statements(boundary):
        if statement["Effect"] == "Deny":
            denied.update(_actions(statement))
    assert {"cloudtrail:StopLogging", "cloudtrail:DeleteTrail"} <= denied


def test_the_boundary_cannot_be_removed_from_a_role(boundary):
    denied: set[str] = set()
    for statement in _statements(boundary):
        if statement["Effect"] == "Deny":
            denied.update(_actions(statement))
    assert "iam:DeleteRolePermissionsBoundary" in denied


def test_the_readme_documents_the_bootstrap_command(execution):
    """The point of these files is the command that consumes them."""
    readme = (BOOTSTRAP / "README.md").read_text()

    assert "--cloudformation-execution-policies" in readme
    assert "--custom-permissions-boundary CorridorNonproductionBoundary" in readme
    assert "CorridorCdkIamProvisioning" in readme
    assert "arn:aws:iam::aws:policy/PowerUserAccess" in readme
