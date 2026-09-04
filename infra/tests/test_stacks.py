"""Assertions on the synthesized templates.

Each test pins a property that would be a security or cost regression if it
silently changed. They assert on the synthesized CloudFormation, so they fail
on the template that would actually be deployed rather than on the Python.
"""

import pathlib
import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from corridor_infra.account_foundation_stack import CorridorAccountFoundationStack
from corridor_infra.application_stack import CorridorApplicationStack
from corridor_infra.data_stack import CorridorDataStack
from corridor_infra.network_stack import CorridorNetworkStack

ENV = cdk.Environment(account="111111111111", region="us-east-2")

# Syntactically valid, deliberately not a real certificate. Deployment supplies
# the real ARN through the protected GitHub environment.
DUMMY_CERT = (
    "arn:aws:acm:us-east-2:111111111111:certificate/"
    "00000000-0000-0000-0000-000000000000"
)


def _build(**overrides):
    """Build the four stacks, so a test can vary one input and assert on it."""
    app = cdk.App()
    foundation = CorridorAccountFoundationStack(
        app, "F", env=ENV,
        github_repo="bharm16/corridor", github_environment="nonproduction",
    )
    network = CorridorNetworkStack(app, "N", env=ENV)
    data = CorridorDataStack(
        app, "D", env=ENV, vpc=network.vpc, database_security_group=network.db_sg
    )
    kwargs = dict(
        vpc=network.vpc,
        alb_security_group=network.alb_sg,
        web_security_group=network.web_sg,
        batch_security_group=network.batch_sg,
        migration_security_group=network.migration_sg,
        database=data.database,
        artifact_bucket=data.artifact_bucket,
        web_db_secret=data.web_db_secret,
        worker_db_secret=data.worker_db_secret,
        image_tag="0123456789abcdef0123456789abcdef01234567",
        web_desired_count=0,
        certificate_arn=DUMMY_CERT,
    )
    kwargs.update(overrides)
    application = CorridorApplicationStack(app, "A", env=ENV, **kwargs)
    return foundation, network, data, application


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
        image_tag="0123456789abcdef0123456789abcdef01234567",
        web_desired_count=0,
        certificate_arn=DUMMY_CERT,
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
            assert "DatabaseSecret" not in document, logical_id
        if "BatchExecutionRole" in roles:
            assert "WebDbSecret" not in document, logical_id


# --- runtime shape -----------------------------------------------------
def test_single_ecr_repository(stacks):
    stacks["application"].resource_count_is("AWS::ECR::Repository", 1)
    stacks["application"].has_resource_properties(
        "AWS::ECR::Repository",
        {"ImageScanningConfiguration": {"ScanOnPush": True}},
    )


def test_log_retention_is_bounded(stacks):
    for group in stacks["application"].find_resources("AWS::Logs::LogGroup").values():
        assert group["Properties"]["RetentionInDays"] == 14


def test_env_and_secret_names_exist_in_corridor_config():
    """Every name the task definitions set must be one config.py reads.

    Settings has no env_prefix: a field with a validation_alias uses that
    alias, and a field without one uses its own name uppercased. Inventing a
    name here produces a variable the application silently ignores.
    """
    config = pathlib.Path(__file__).parents[2] / "src/corridor/config.py"
    source = config.read_text()

    aliases = set(re.findall(r'validation_alias="([A-Z0-9_]+)"', source))
    plain = {
        f.upper()
        for f in re.findall(r"^    ([a-z_]+):", source, re.MULTILINE)
    }
    readable = aliases | plain

    # Names the image entrypoint consumes to compose the URLs config.py reads.
    entrypoint_inputs = {
        "CORRIDOR_DB_HOST",
        "CORRIDOR_DB_PORT",
        "CORRIDOR_DB_NAME",
        "CORRIDOR_DB_ADMIN_USERNAME",
        "CORRIDOR_DB_ADMIN_PASSWORD",
        "CORRIDOR_TASK_ROLE",
    }

    assert "CORRIDOR_WEB_DB_PASSWORD" in readable
    assert "CORRIDOR_WORKER_DB_PASSWORD" in readable
    assert "DATABASE_URL" in readable
    assert "WEB_DATABASE_URL" in readable
    assert "WORKER_DATABASE_URL" in readable
    # These must NOT be treated as settings; they are entrypoint inputs only.
    assert not (entrypoint_inputs & readable), (
        "an entrypoint input collides with a real setting name"
    )


def test_no_task_sets_an_unreadable_corridor_variable(stacks):
    config = pathlib.Path(__file__).parents[2] / "src/corridor/config.py"
    source = config.read_text()
    aliases = set(re.findall(r'validation_alias="([A-Z0-9_]+)"', source))
    plain = {f.upper() for f in re.findall(r"^    ([a-z_]+):", source, re.MULTILINE)}
    entrypoint_inputs = {
        "CORRIDOR_DB_HOST", "CORRIDOR_DB_PORT", "CORRIDOR_DB_NAME",
        "CORRIDOR_DB_ADMIN_USERNAME", "CORRIDOR_DB_ADMIN_PASSWORD",
        "CORRIDOR_TASK_ROLE",
    }
    allowed = aliases | plain | entrypoint_inputs

    template = stacks["application"].to_json()["Resources"]
    for logical_id, resource in template.items():
        if resource["Type"] != "AWS::ECS::TaskDefinition":
            continue
        for container in resource["Properties"]["ContainerDefinitions"]:
            for entry in container.get("Environment", []) + container.get("Secrets", []):
                name = entry["Name"]
                assert name in allowed, (
                    f"{logical_id} sets {name}, which config.py does not read "
                    f"and the entrypoint does not consume"
                )


# --- fail-closed TLS ---------------------------------------------------
def test_no_certificate_and_no_explicit_opt_in_is_refused():
    """Absence of a certificate must not quietly select plaintext."""
    with pytest.raises(ValueError, match="certificateArn is required"):
        _build(certificate_arn="")


def test_insecure_http_requires_an_explicit_flag():
    _, _, _, application = _build(certificate_arn="", allow_insecure_http=True)
    template = Template.from_stack(application)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener", {"Port": 80, "Protocol": "HTTP"}
    )


def test_the_normal_path_is_https_with_a_redirect(stacks):
    template = stacks["application"]
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"Port": 443, "Protocol": "HTTPS"},
    )
    listeners = template.find_resources("AWS::ElasticLoadBalancingV2::Listener")
    redirects = [
        listener
        for listener in listeners.values()
        if any(
            action.get("Type") == "redirect"
            for action in listener["Properties"].get("DefaultActions", [])
        )
    ]
    assert redirects, "port 80 does not redirect to HTTPS"


# --- immutable image tag ----------------------------------------------
def test_an_empty_image_tag_is_refused():
    with pytest.raises(ValueError, match="immutable tag"):
        _build(image_tag="")


def test_the_bootstrap_placeholder_tag_is_refused():
    with pytest.raises(ValueError, match="immutable tag"):
        _build(image_tag="bootstrap")


# --- readiness ---------------------------------------------------------
def test_the_load_balancer_checks_readyz_not_health(stacks):
    """/health returns 503 on a stale worker heartbeat and this environment
    runs no resident worker, so checking it would deregister a healthy task."""
    stacks["application"].has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {"HealthCheckPath": "/readyz", "Port": 8412},
    )


# --- migration credentials --------------------------------------------
def test_migration_receives_both_runtime_passwords(stacks):
    """The baseline migration creates the corridor_web and corridor_worker
    logins from CORRIDOR_WEB_DB_PASSWORD and CORRIDOR_WORKER_DB_PASSWORD. If it
    does not get them it invents predictable ones, and neither runtime task can
    then authenticate with its randomized secret."""
    template = stacks["application"].to_json()["Resources"]
    migration = [
        resource
        for logical_id, resource in template.items()
        if resource["Type"] == "AWS::ECS::TaskDefinition" and "Migration" in logical_id
    ]
    assert len(migration) == 1
    names = {
        entry["Name"]
        for container in migration[0]["Properties"]["ContainerDefinitions"]
        for entry in container.get("Secrets", [])
    }
    assert names == {
        "CORRIDOR_DB_ADMIN_USERNAME",
        "CORRIDOR_DB_ADMIN_PASSWORD",
        "CORRIDOR_WEB_DB_PASSWORD",
        "CORRIDOR_WORKER_DB_PASSWORD",
    }


def test_web_and_batch_still_receive_only_their_own_login(stacks):
    template = stacks["application"].to_json()["Resources"]
    expected = {
        "Web": {"CORRIDOR_WEB_DB_PASSWORD"},
        "Batch": {"CORRIDOR_WORKER_DB_PASSWORD"},
    }
    for role, wanted in expected.items():
        task = [
            resource
            for logical_id, resource in template.items()
            if resource["Type"] == "AWS::ECS::TaskDefinition"
            and logical_id.startswith(role)
        ]
        assert len(task) == 1, role
        names = {
            entry["Name"]
            for container in task[0]["Properties"]["ContainerDefinitions"]
            for entry in container.get("Secrets", [])
        }
        assert names == wanted, f"{role} sees {names}"


# --- identity and engine ----------------------------------------------
def test_the_cdk_entry_role_is_named_for_cdk(stacks):
    stacks["foundation"].has_resource_properties(
        "AWS::IAM::Role", {"RoleName": "corridor-nonprod-cdk-deploy"}
    )


def test_postgres_is_not_the_deprecated_minor_version(stacks):
    stacks["data"].has_resource_properties(
        "AWS::RDS::DBInstance",
        {"Engine": "postgres", "EngineVersion": "16.14"},
    )


def test_the_stack_never_lowers_database_tls(stacks):
    """CORRIDOR_DB_SSLMODE exists only so a smoke test can run against a plain
    PostgreSQL container. A deployed task must always verify the certificate,
    so the stack must never set it at all."""
    template = stacks["application"].to_json()["Resources"]
    for logical_id, resource in template.items():
        if resource["Type"] != "AWS::ECS::TaskDefinition":
            continue
        for container in resource["Properties"]["ContainerDefinitions"]:
            names = {
                entry["Name"]
                for entry in container.get("Environment", [])
                + container.get("Secrets", [])
            }
            assert "CORRIDOR_DB_SSLMODE" not in names, logical_id


def test_every_task_declares_its_role(stacks):
    """The entrypoint refuses to start without CORRIDOR_TASK_ROLE, and it
    decides which single database URL gets composed."""
    template = stacks["application"].to_json()["Resources"]
    seen = {}
    for logical_id, resource in template.items():
        if resource["Type"] != "AWS::ECS::TaskDefinition":
            continue
        for container in resource["Properties"]["ContainerDefinitions"]:
            role = [
                entry["Value"]
                for entry in container.get("Environment", [])
                if entry["Name"] == "CORRIDOR_TASK_ROLE"
            ]
            assert role, f"{logical_id} declares no task role"
            seen[logical_id] = role[0]
    assert sorted(seen.values()) == ["batch", "migration", "web"]


def test_the_database_master_login_is_the_schema_owner_the_baseline_expects():
    """The consolidated baseline is a pg_dump with 408 `OWNER TO corridor;`
    statements. Any other master username fails the first migration with
    role "corridor" does not exist -- found by running it, not by reading it.
    """
    import json

    _, _, data, _ = _build()
    template = Template.from_stack(data).to_json()["Resources"]
    secrets = [
        resource
        for resource in template.values()
        if resource["Type"] == "AWS::SecretsManager::Secret"
        and "db-admin" in json.dumps(resource["Properties"].get("Name", ""))
    ]
    assert len(secrets) == 1
    generated = secrets[0]["Properties"]["GenerateSecretString"]
    assert json.loads(generated["SecretStringTemplate"]) == {"username": "corridor"}
