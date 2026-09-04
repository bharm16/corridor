"""Assertions on the synthesized templates.

Each test pins a property that would be a security or cost regression if it
silently changed. They assert on the synthesized CloudFormation, so they fail
on the template that would actually be deployed rather than on the Python.
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from corridor_infra.account_foundation_stack import CorridorAccountFoundationStack
from corridor_infra.application_stack import CorridorApplicationStack
from corridor_infra.data_stack import CorridorDataStack
from corridor_infra.network_stack import CorridorNetworkStack

ENV = cdk.Environment(account="111111111111", region="us-east-2")


@pytest.fixture(scope="module")
def stacks():
    app = cdk.App(context={"corridor:webDesiredCount": 0})
    foundation = CorridorAccountFoundationStack(
        app,
        "F",
        env=ENV,
        github_repo="bharm16/corridor",
        github_environment="nonproduction",
    )
    network = CorridorNetworkStack(app, "N", env=ENV)
    data = CorridorDataStack(
        app, "D", env=ENV, vpc=network.vpc, database_security_group=network.db_sg
    )
    application = CorridorApplicationStack(
        app,
        "A",
        env=ENV,
        vpc=network.vpc,
        alb_security_group=network.alb_sg,
        web_security_group=network.web_sg,
        batch_security_group=network.batch_sg,
        migration_security_group=network.migration_sg,
        database=data.database,
        artifact_bucket=data.artifact_bucket,
        web_db_secret=data.web_db_secret,
        worker_db_secret=data.worker_db_secret,
        app_secret=data.app_secret,
        image_tag="test",
        web_desired_count=0,
    )
    return {
        "foundation": Template.from_stack(foundation),
        "network": Template.from_stack(network),
        "data": Template.from_stack(data),
        "application": Template.from_stack(application),
    }


# --- cost -------------------------------------------------------------
def test_no_nat_gateway(stacks):
    stacks["network"].resource_count_is("AWS::EC2::NatGateway", 0)


def test_s3_gateway_endpoint_present(stacks):
    stacks["network"].resource_count_is("AWS::EC2::VPCEndpoint", 1)


def test_web_service_starts_at_zero(stacks):
    stacks["application"].has_resource_properties(
        "AWS::ECS::Service", {"DesiredCount": 0}
    )


# --- database exposure -------------------------------------------------
def test_database_is_not_public(stacks):
    stacks["data"].has_resource_properties(
        "AWS::RDS::DBInstance",
        {"PubliclyAccessible": False, "StorageEncrypted": True, "MultiAZ": False},
    )


def test_database_retains_and_is_protected(stacks):
    stacks["data"].has_resource(
        "AWS::RDS::DBInstance",
        {"DeletionPolicy": "Snapshot", "UpdateReplacePolicy": "Snapshot"},
    )
    stacks["data"].has_resource_properties(
        "AWS::RDS::DBInstance",
        {"DeletionProtection": True, "BackupRetentionPeriod": 7},
    )


def test_no_database_ingress_from_the_internet(stacks):
    """5432 must never be reachable from a CIDR, only from a security group."""
    ingress = stacks["network"].find_resources("AWS::EC2::SecurityGroupIngress")
    for logical_id, resource in ingress.items():
        props = resource["Properties"]
        if props.get("FromPort") == 5432:
            assert "CidrIp" not in props, f"{logical_id} opens 5432 to a CIDR"
            assert "SourceSecurityGroupId" in props, logical_id


def test_only_the_alb_accepts_internet_traffic(stacks):
    """Any 0.0.0.0/0 ingress must be on 80 or 443."""
    template = stacks["network"].to_json()
    for logical_id, resource in template["Resources"].items():
        if resource["Type"] != "AWS::EC2::SecurityGroup":
            continue
        for rule in resource["Properties"].get("SecurityGroupIngress", []):
            if rule.get("CidrIp") == "0.0.0.0/0":
                assert rule.get("FromPort") in (80, 443), (
                    f"{logical_id} exposes port {rule.get('FromPort')} to the internet"
                )


# --- storage -----------------------------------------------------------
def test_every_bucket_blocks_public_access(stacks):
    for name in ("data", "foundation", "application"):
        for logical_id, bucket in stacks[name].find_resources("AWS::S3::Bucket").items():
            config = bucket["Properties"]["PublicAccessBlockConfiguration"]
            assert all(
                config[k]
                for k in (
                    "BlockPublicAcls",
                    "BlockPublicPolicy",
                    "IgnorePublicAcls",
                    "RestrictPublicBuckets",
                )
            ), f"{name}/{logical_id} does not block all public access"


def test_artifact_bucket_is_versioned(stacks):
    stacks["data"].has_resource_properties(
        "AWS::S3::Bucket",
        {"VersioningConfiguration": {"Status": "Enabled"}},
    )


# --- audit -------------------------------------------------------------
def test_trail_is_multi_region_and_validated(stacks):
    stacks["foundation"].has_resource_properties(
        "AWS::CloudTrail::Trail",
        {
            "IsMultiRegionTrail": True,
            "EnableLogFileValidation": True,
            "IncludeGlobalServiceEvents": True,
        },
    )


# --- identity ----------------------------------------------------------
def test_github_trust_is_pinned_to_the_environment(stacks):
    roles = stacks["foundation"].find_resources("AWS::IAM::Role")
    subjects = []
    for role in roles.values():
        for statement in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            condition = statement.get("Condition", {}).get("StringEquals", {})
            for key, value in condition.items():
                if key.endswith(":sub"):
                    subjects.append(value)
    assert subjects, "no OIDC subject condition found"
    for subject in subjects:
        assert subject == "repo:bharm16/corridor:environment:nonproduction"
        assert "*" not in subject


def test_github_role_has_no_wildcard_resource(stacks):
    for policy in stacks["foundation"].find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert statement.get("Resource") != "*", statement


def test_no_wildcard_passrole_anywhere(stacks):
    for name, template in stacks.items():
        for policy in template.find_resources("AWS::IAM::Policy").values():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action")
                actions = [actions] if isinstance(actions, str) else (actions or [])
                if any("PassRole" in str(a) for a in actions):
                    assert statement.get("Resource") != "*", f"{name}: {statement}"


def test_each_task_has_its_own_execution_role(stacks):
    """Secrets are injected *as the execution role*, so sharing one across
    tasks would let the web task's agent read the migration credential."""
    template = stacks["application"].to_json()["Resources"]
    arns = []
    for resource in template.values():
        if resource["Type"] == "AWS::ECS::TaskDefinition":
            arns.append(str(resource["Properties"]["ExecutionRoleArn"]))
    assert len(arns) == 3, arns
    assert len(set(arns)) == 3, f"execution role shared across tasks: {arns}"


def test_only_migration_can_read_the_schema_owner_credential(stacks):
    """#489: the migration job is the only schema writer.

    That holds only if the web and batch execution roles cannot fetch the RDS
    admin credential. Asserted against the synthesized grants, not intent.
    """
    template = stacks["application"].to_json()["Resources"]
    grants = {}
    for logical_id, resource in template.items():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        document = str(resource["Properties"]["PolicyDocument"])
        if "secretsmanager:GetSecretValue" not in document:
            continue
        roles = str(resource["Properties"].get("Roles"))
        grants[logical_id] = (roles, document)

    assert grants, "no secret grants found; the test would pass vacuously"

    for logical_id, (roles, document) in grants.items():
        reads_admin = "DatabaseSecret" in document
        is_migration = "Migration" in roles
        if reads_admin:
            assert is_migration, (
                f"{logical_id} reads the RDS admin credential but is attached "
                f"to {roles}"
            )
        if not is_migration:
            assert not reads_admin, logical_id


def test_web_and_batch_receive_only_their_own_login(stacks):
    template = stacks["application"].to_json()["Resources"]
    for logical_id, resource in template.items():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        document = str(resource["Properties"]["PolicyDocument"])
        roles = str(resource["Properties"].get("Roles"))
        if "secretsmanager:GetSecretValue" not in document:
            continue
        if "WebExecutionRole" in roles:
            assert "WorkerDbSecret" not in document, logical_id
        if "BatchExecutionRole" in roles:
            assert "WebDbSecret" not in document, logical_id


# --- runtime shape -----------------------------------------------------
def test_single_ecr_repository(stacks):
    stacks["application"].resource_count_is("AWS::ECR::Repository", 1)
    stacks["application"].has_resource_properties(
        "AWS::ECR::Repository",
        {"ImageScanningConfiguration": {"ScanOnPush": True}},
    )


def test_health_check_uses_the_application_route_and_port(stacks):
    stacks["application"].has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {"HealthCheckPath": "/health", "Port": 8412},
    )


def test_log_retention_is_bounded(stacks):
    for group in stacks["application"].find_resources("AWS::Logs::LogGroup").values():
        assert group["Properties"]["RetentionInDays"] == 14
