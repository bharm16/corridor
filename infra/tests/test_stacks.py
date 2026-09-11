"""Assertions on the synthesized templates.

Each test pins a property that would be a security or cost regression if it
silently changed. They assert on the synthesized CloudFormation, so they fail
on the template that would actually be deployed rather than on the Python.
"""

import json
import pathlib
import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from corridor_infra.account_foundation_stack import CorridorAccountFoundationStack
from corridor_infra.application_stack import CorridorApplicationStack
from corridor_infra.control_plane_stack import CorridorControlPlaneStack
from corridor_infra.data_stack import CorridorDataStack
from corridor_infra.network_stack import CorridorNetworkStack

ENV = cdk.Environment(account="111111111111", region="us-east-2")

# Syntactically valid, deliberately not a real certificate. Deployment supplies
# the real ARN through the protected GitHub environment.
DUMMY_CERT = (
    "arn:aws:acm:us-east-2:111111111111:certificate/"
    "00000000-0000-0000-0000-000000000000"
)


CDK_JSON = pathlib.Path(__file__).parents[1] / "cdk.json"


def _app_context() -> dict:
    """The same context cdk.json gives the real synthesis.

    Building the test App bare meant the permissions-boundary context was
    absent, so the boundary assertions passed against roles that had no
    boundary in the fixture but did in the real template -- and would equally
    have passed if the context were deleted outright.
    """
    return json.loads(CDK_JSON.read_text())["context"]


def _build(**overrides) -> dict[str, Template]:
    """Synthesize every stack, keyed by name.

    Returns a template per stack rather than a tuple. The previous four-item
    tuple built the control-plane stack and then dropped it from the return
    value, so `test_bootstrap_policies`'s generic boundary sweeps -- the ones
    that exist because a hand-written action comparison missed two real grants
    -- ran against four of the five stacks. Nothing decided that; a discarded
    tuple element did. Returning a mapping means a caller takes the stack it
    wants by name and a sweep iterates whatever this builds, so a sixth stack
    joins those sweeps the day it is added.

    `**overrides` still reaches the application stack's keyword arguments only.
    """
    app = cdk.App(context=_app_context())
    foundation = CorridorAccountFoundationStack(
        app, "F", env=ENV,
        github_repo="bharm16/corridor", github_environment="nonproduction",
    )
    network = CorridorNetworkStack(app, "N", env=ENV)
    control = CorridorControlPlaneStack(
        app, "C", env=ENV, vpc=network.vpc,
        database_security_group=network.control_db_sg,
    )
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
        control_database=control.database,
        control_operations_secret=control.operations_secret,
        control_resolver_secret=control.resolver_secret,
        customer_routing_secret=data.customer_routing_secret,
        customer_id="synthetic-a", customer_environment_id="synthetic-nonproduction",
        deployment_id="corridor-nonproduction", data_class="synthetic",
        image_tag="0123456789abcdef0123456789abcdef01234567",
        web_desired_count=0,
        certificate_arn=DUMMY_CERT,
        public_hostname="pilot.example.com",
        sign_in_sender="no-reply@example.com",
    )
    kwargs.update(overrides)
    application = CorridorApplicationStack(app, "A", env=ENV, **kwargs)
    return {
        "foundation": Template.from_stack(foundation),
        "network": Template.from_stack(network),
        "control": Template.from_stack(control),
        "data": Template.from_stack(data),
        "application": Template.from_stack(application),
    }


@pytest.fixture(scope="module")
def stacks():
    """The same five templates `_build` produces, synthesized once per module.

    This used to repeat `_build`'s twenty keyword arguments forty lines below
    it, so the two constructions could drift apart silently.
    """
    return _build()


# --- cost -------------------------------------------------------------
def test_no_nat_gateway(stacks):
    stacks["network"].resource_count_is("AWS::EC2::NatGateway", 0)


def test_s3_gateway_endpoint_present(stacks):
    stacks["network"].resource_count_is("AWS::EC2::VPCEndpoint", 1)


def test_web_service_starts_at_zero(stacks):
    stacks["application"].has_resource_properties(
        "AWS::ECS::Service", {"DesiredCount": 0}
    )


def test_worker_service_runs_the_existing_supervisor_with_its_own_health_check(stacks):
    template = stacks["application"].to_json()
    services = {
        key: value["Properties"]
        for key, value in template["Resources"].items()
        if value["Type"] == "AWS::ECS::Service"
    }
    worker = [value for key, value in services.items() if key.startswith("WorkerService")]
    assert len(worker) == 1, "preparation requests need a resident worker service"
    assert worker[0]["DesiredCount"] == 0
    assert worker[0]["EnableExecuteCommand"] is False
    task_id = worker[0]["TaskDefinition"]["Ref"]
    task = template["Resources"][task_id]["Properties"]
    container = task["ContainerDefinitions"][0]
    assert container["Command"] == [
        "python", "-m", "corridor.due_work_cli", "supervise", "--poll-seconds=5"
    ]
    assert container["HealthCheck"]["Command"] == [
        "CMD", "python", "/opt/corridor/scripts/container_entrypoint.py",
        "python", "-m", "corridor.due_work_cli", "health",
    ]
    assert container["StopTimeout"] == 120
    assert template["Outputs"]["WorkerServiceName"]["Value"]
    for service in services.values():
        runtime_task = template["Resources"][service["TaskDefinition"]["Ref"]]["Properties"]
        assert all(item.get("HealthCheck") for item in runtime_task["ContainerDefinitions"]
                   if item.get("Essential", True)), "runtime health cannot remain UNKNOWN"


def test_serving_requires_a_worker_and_the_worker_has_an_operational_alarm():
    with pytest.raises(ValueError, match="workerDesiredCount"):
        _build(web_desired_count=1, worker_desired_count=0)
    template = _build(web_desired_count=1, worker_desired_count=1)["application"]
    template.resource_count_is("AWS::ECS::Service", 2)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    worker_alarms = [value["Properties"] for key, value in alarms.items()
                     if key.startswith("WorkerMissingTasksAlarm")]
    assert len(worker_alarms) == 1
    alarm = worker_alarms[0]
    assert alarm["Threshold"] == 1
    assert alarm["EvaluationPeriods"] == 3
    metrics = [item["MetricStat"]["Metric"] for item in alarm["Metrics"] if "MetricStat" in item]
    assert {metric["MetricName"] for metric in metrics} == {"DesiredTaskCount", "RunningTaskCount"}
    assert all({d["Name"] for d in metric["Dimensions"]} == {"ClusterName", "ServiceName"}
               for metric in metrics)


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


# AWS defines these with no resource type, so `Resource: "*"` is the only
# form they can take. Anything else on a wildcard resource must carry a
# Condition that scopes it instead.
UNSCOPABLE_ACTIONS = {
    "ecr:GetAuthorizationToken",
    "ecs:DescribeTaskDefinition",
    # Registration is account-level in IAM; what a registered revision may
    # reference is bounded by the PassRole statement instead.
    "ecs:RegisterTaskDefinition",
    "ecs:TagResource",
}


def test_wildcard_resources_are_unscopable_actions_or_conditioned(stacks):
    """A blunt "no Resource: *" rule is unenforceable -- a few AWS actions
    genuinely admit no ARN. So the rule is narrower and actually checkable:
    every wildcard statement is either entirely made of those actions, or is
    constrained by a Condition."""
    offenders = []
    for name, template in stacks.items():
        for logical_id, policy in template.find_resources("AWS::IAM::Policy").items():
            if logical_id.startswith("LogRetention"):
                # CDK's own LogRetention helper. It sets retention on a log
                # group RDS names at runtime, so the name is not knowable at
                # synthesis time. Excluded by construct, not by action, so the
                # same wildcard appearing on a Corridor role still fails.
                continue
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                resource = statement.get("Resource")
                if resource != "*":
                    continue
                if "Condition" in statement:
                    continue
                actions = statement.get("Action")
                actions = [actions] if isinstance(actions, str) else actions
                extra = set(actions) - UNSCOPABLE_ACTIONS
                if extra:
                    offenders.append((name, logical_id, sorted(extra)))
    assert not offenders, (
        "unconditioned wildcard resources on scopable actions: " + repr(offenders)
    )


def test_the_cdk_role_holds_no_wildcard_resource_at_all(stacks):
    """The CDK entry role assumes bootstrap roles and reads one SSM parameter.
    Unlike the release role it touches no account-level API, so it has no
    excuse for a wildcard."""
    for logical_id, policy in (
        stacks["foundation"].find_resources("AWS::IAM::Policy").items()
    ):
        if "Release" in logical_id:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert statement.get("Resource") != "*", (logical_id, statement)


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


def test_control_plane_state_survives_the_customer_destruction_unit(stacks):
    control = stacks["control"].find_resources("AWS::RDS::DBInstance")
    customer = stacks["data"].find_resources("AWS::RDS::DBInstance")
    assert len(control) == len(customer) == 1
    control_database = next(iter(control.values()))
    customer_database = next(iter(customer.values()))
    assert control_database["Properties"]["DBInstanceIdentifier"] == "corridor-nonprod-control"
    assert control_database["Properties"]["DBInstanceIdentifier"] != customer_database["Properties"]["DBInstanceIdentifier"]
    assert control_database["DeletionPolicy"] == "Retain"
    assert control_database["UpdateReplacePolicy"] == "Retain"
    assert control_database["Properties"]["DeletionProtection"] is True
    assert control_database["Properties"]["BackupRetentionPeriod"] == 7
    assert control_database["Properties"]["DeleteAutomatedBackups"] is False
    assert control_database["Properties"]["PubliclyAccessible"] is False
    assert control_database["Properties"]["StorageEncrypted"] is True
    assert "ArtifactBucket" not in json.dumps(stacks["control"].to_json())


def test_runtime_has_only_control_resolver_and_its_own_customer_secret(stacks):
    roles = {}
    for task in stacks["application"].find_resources("AWS::ECS::TaskDefinition").values():
        container = task["Properties"]["ContainerDefinitions"][0]
        environment = {entry["Name"]: entry["Value"] for entry in container["Environment"]}
        roles[environment["CORRIDOR_TASK_ROLE"]] = {entry["Name"] for entry in container["Secrets"]}
        assert environment["CORRIDOR_CUSTOMER_ID"] == "synthetic-a"
        assert environment["CORRIDOR_CUSTOMER_ENVIRONMENT_ID"] == "synthetic-nonproduction"
        assert environment["CORRIDOR_DEPLOYMENT_ID"] == "corridor-nonproduction"
        assert environment["CORRIDOR_DEPLOYMENT_DATA_CLASS"] == "synthetic"
    resolver = {"CORRIDOR_CONTROL_RESOLVER_DB_USERNAME", "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD"}
    assert roles["web"] == resolver | {"CORRIDOR_WEB_DB_PASSWORD", "CORRIDOR_CUSTOMER_ROUTING_KEY"}
    assert roles["batch"] == resolver | {"CORRIDOR_WORKER_DB_PASSWORD"}
    assert roles["migration"] == resolver | {
        "CORRIDOR_DB_ADMIN_USERNAME", "CORRIDOR_DB_ADMIN_PASSWORD",
        "CORRIDOR_WEB_DB_PASSWORD", "CORRIDOR_WORKER_DB_PASSWORD",
        "CORRIDOR_CONTROL_OWNER_DB_USERNAME", "CORRIDOR_CONTROL_OWNER_DB_PASSWORD",
        "CORRIDOR_CONTROL_OPERATIONS_DB_USERNAME", "CORRIDOR_CONTROL_OPERATIONS_DB_PASSWORD",
    }


def test_only_migration_may_fetch_control_owner_and_operations_credentials(stacks):
    for policy in stacks["application"].find_resources("AWS::IAM::Policy").values():
        statement = json.dumps(policy["Properties"]["PolicyDocument"])
        if "ControlDatabaseSecret" in statement or "ControlOperationsSecret" in statement:
            assert "MigrationExecutionRole" in json.dumps(policy["Properties"]["Roles"])


def test_migration_runs_bounded_configuration_before_runtime_activation(stacks):
    tasks = stacks["application"].find_resources("AWS::ECS::TaskDefinition")
    migration = next(task for name, task in tasks.items() if name.startswith("Migration"))
    assert migration["Properties"]["ContainerDefinitions"][0]["Command"] == [
        "python", "-m", "corridor.deployment_bootstrap", "configure",
    ]


@pytest.mark.parametrize("field", ["customer_id", "customer_environment_id", "deployment_id"])
def test_deployment_requires_explicit_stable_customer_identity(field):
    with pytest.raises(ValueError, match="explicit stable identifier"):
        _build(**{field: ""})


def test_nonproduction_stack_cannot_silently_become_a_customer_activation():
    with pytest.raises(ValueError, match="explicitly be synthetic"):
        _build(data_class="customer")


def test_customer_signing_key_and_control_logins_are_generated_separately(stacks):
    customer_secrets = stacks["data"].find_resources("AWS::SecretsManager::Secret")
    routing = next(secret for name, secret in customer_secrets.items() if name.startswith("CustomerRoutingSecret"))
    assert routing["Properties"]["GenerateSecretString"]["PasswordLength"] >= 32
    logins = {
        json.loads(secret["Properties"]["GenerateSecretString"].get("SecretStringTemplate", "{}")).get("username")
        for secret in stacks["control"].find_resources("AWS::SecretsManager::Secret").values()
    }
    assert logins == {"corridor_control_owner", "corridor_control_operator", "corridor_control_runtime"}


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
        "CORRIDOR_CONTROL_DB_HOST", "CORRIDOR_CONTROL_DB_PORT", "CORRIDOR_CONTROL_DB_NAME",
        "CORRIDOR_CONTROL_OWNER_DB_USERNAME", "CORRIDOR_CONTROL_OWNER_DB_PASSWORD",
        "CORRIDOR_CONTROL_OPERATIONS_DB_USERNAME", "CORRIDOR_CONTROL_OPERATIONS_DB_PASSWORD",
        "CORRIDOR_CONTROL_RESOLVER_DB_USERNAME", "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD",
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
        "CORRIDOR_CONTROL_DB_HOST", "CORRIDOR_CONTROL_DB_PORT", "CORRIDOR_CONTROL_DB_NAME",
        "CORRIDOR_CONTROL_OWNER_DB_USERNAME", "CORRIDOR_CONTROL_OWNER_DB_PASSWORD",
        "CORRIDOR_CONTROL_OPERATIONS_DB_USERNAME", "CORRIDOR_CONTROL_OPERATIONS_DB_PASSWORD",
        "CORRIDOR_CONTROL_RESOLVER_DB_USERNAME", "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD",
        "CORRIDOR_DEPLOYMENT_DATA_CLASS",
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
def test_serving_without_a_certificate_is_refused():
    """The web service may not carry traffic over plaintext."""
    with pytest.raises(ValueError, match="certificateArn is required"):
        _build(certificate_arn="", web_desired_count=1)


def test_without_a_certificate_the_stack_synthesises_but_has_no_listener():
    """The network and data stacks still need to be deployable before a
    certificate exists. The service can exist at zero; it just has no way in."""
    template = _build(certificate_arn="", web_desired_count=0)["application"]

    template.resource_count_is("AWS::ElasticLoadBalancingV2::Listener", 0)
    template.has_resource_properties("AWS::ECS::Service", {"DesiredCount": 0})


def test_there_is_no_plaintext_listener_anywhere(stacks):
    """An earlier version fell back to HTTP when no certificate was supplied,
    gated by a flag read with bool() -- and CDK context arrives from the command
    line as a string, so bool("false") is True. The path is gone entirely."""
    for listener in stacks["application"].find_resources(
        "AWS::ElasticLoadBalancingV2::Listener"
    ).values():
        props = listener["Properties"]
        if props["Protocol"] == "HTTP":
            actions = props.get("DefaultActions", [])
            assert all(action.get("Type") == "redirect" for action in actions), (
                "an HTTP listener forwards traffic instead of redirecting"
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
    """Worker failure must not deregister the UI that shows its retained status."""
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
        "CORRIDOR_CONTROL_OWNER_DB_USERNAME",
        "CORRIDOR_CONTROL_OWNER_DB_PASSWORD",
        "CORRIDOR_CONTROL_OPERATIONS_DB_USERNAME",
        "CORRIDOR_CONTROL_OPERATIONS_DB_PASSWORD",
        "CORRIDOR_CONTROL_RESOLVER_DB_USERNAME",
        "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD",
    }


def test_web_and_batch_still_receive_only_their_own_login(stacks):
    template = stacks["application"].to_json()["Resources"]
    expected = {
        "Web": {
            "CORRIDOR_WEB_DB_PASSWORD", "CORRIDOR_CUSTOMER_ROUTING_KEY",
            "CORRIDOR_CONTROL_RESOLVER_DB_USERNAME", "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD",
        },
        "Batch": {
            "CORRIDOR_WORKER_DB_PASSWORD", "CORRIDOR_CONTROL_RESOLVER_DB_USERNAME",
            "CORRIDOR_CONTROL_RESOLVER_DB_PASSWORD",
        },
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
    """16.4 is deprecated in RDS. 16.15 carries CVE fixes but the pinned
    aws-cdk-lib's CloudFormation spec refuses it under --strict, so 16.14 is
    the synthetic-environment version with an upgrade gate before #535."""
    stacks["data"].has_resource_properties(
        "AWS::RDS::DBInstance",
        {"Engine": "postgres", "EngineVersion": "16.14"},
    )


def test_the_postgres_upgrade_gate_is_written_down():
    """A temporary version needs a recorded reason to stop being temporary."""
    source = (
        pathlib.Path(__file__).parents[1] / "corridor_infra" / "data_stack.py"
    ).read_text()

    assert "UPGRADE GATE" in source
    assert "#535" in source
    assert "auto_minor_version_upgrade" in source


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

    template = _build()["data"].to_json()["Resources"]
    secrets = [
        resource
        for resource in template.values()
        if resource["Type"] == "AWS::SecretsManager::Secret"
        and "db-admin" in json.dumps(resource["Properties"].get("Name", ""))
    ]
    assert len(secrets) == 1
    generated = secrets[0]["Properties"]["GenerateSecretString"]
    assert json.loads(generated["SecretStringTemplate"]) == {"username": "corridor"}


# --- permissions boundary and role path --------------------------------
def test_every_explicit_role_sits_under_corridors_path(stacks):
    """The CloudFormation execution policy only writes roles on this path, so a
    role created anywhere else would be denied at deploy time."""
    for name in ("foundation", "network", "application"):
        for logical_id, role in stacks[name].find_resources("AWS::IAM::Role").items():
            if "CustomResourceProviderRole" in logical_id:
                # Created by CDK's custom-resource framework; its path is not
                # ours to set. It is still covered by the delegated boundary.
                continue
            assert role["Properties"].get("Path") == "/corridor/nonproduction/", (
                f"{name}/{logical_id} has path {role['Properties'].get('Path')!r}"
            )


def test_every_role_carries_the_delegated_boundary(stacks):
    """Applied app-wide through the @aws-cdk/core:permissionsBoundary context.
    A role without it is refused by the execution policy's Deny."""
    for name in ("foundation", "network", "application"):
        roles = stacks[name].find_resources("AWS::IAM::Role")
        assert roles, name
        for logical_id, role in roles.items():
            boundary = role["Properties"].get("PermissionsBoundary")
            assert boundary is not None, f"{name}/{logical_id} has no boundary"
            assert "CorridorDelegatedRoleBoundary" in json.dumps(boundary), (
                f"{name}/{logical_id} carries {boundary!r}"
            )


def test_the_two_github_roles_are_separate_identities(stacks):
    names = set()
    for role in stacks["foundation"].find_resources("AWS::IAM::Role").values():
        role_name = role["Properties"].get("RoleName")
        if role_name:
            names.add(role_name)
    assert names == {"corridor-nonprod-cdk-deploy", "corridor-nonprod-app-release"}


def test_the_release_role_cannot_deploy_a_stack(stacks):
    """It moves existing ECS resources; it must not be able to create
    infrastructure or assume the CDK bootstrap roles.

    It does read one stack's outputs, so the rule is about CloudFormation
    *writes* rather than the word cloudformation.
    """
    for logical_id, policy in (
        stacks["foundation"].find_resources("AWS::IAM::Policy").items()
    ):
        if "Release" not in logical_id:
            continue
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        document = json.dumps(statements)
        for forbidden in ("cdk-hnb659fds", "rds:", "iam:CreateRole", "iam:PutRole"):
            assert forbidden not in document, (logical_id, forbidden)

        for statement in statements:
            action = statement.get("Action")
            actions = [action] if isinstance(action, str) else action
            for entry in actions:
                if not entry.startswith("cloudformation:"):
                    continue
                assert entry == "cloudformation:DescribeStacks", entry
                # And only the one stack.
                assert "CorridorApplication" in json.dumps(statement["Resource"])


def test_the_cdk_role_cannot_touch_application_resources(stacks):
    for logical_id, policy in (
        stacks["foundation"].find_resources("AWS::IAM::Policy").items()
    ):
        if "Release" in logical_id:
            continue
        document = json.dumps(policy["Properties"]["PolicyDocument"])
        for forbidden in ("ecr:PutImage", "ecs:UpdateService", "ecs:RunTask"):
            assert forbidden not in document, (logical_id, forbidden)


def test_the_permissions_boundary_context_is_actually_configured():
    """The boundary reaches every role through cdk.json context rather than a
    per-role argument, so deleting that key would silently unbind all of them.
    Asserted directly so the fixture cannot be the only thing that notices."""
    context = _app_context()
    assert context["@aws-cdk/core:permissionsBoundary"] == {
        "name": "CorridorDelegatedRoleBoundary"
    }


# --- no hidden custom resources ----------------------------------------
def test_no_stack_synthesizes_a_lambda(stacks):
    """Every CDK helper that quietly adds a Lambda also adds a role that both
    permissions boundaries would have to admit. The execution boundary does not
    permit Lambda creation and the delegated boundary refuses the actions those
    helpers need, so a Lambda appearing here means a deployment that fails
    partway through. Removing the helpers is cheaper than widening the ceiling
    for every delegated role in the account."""
    for name, template in stacks.items():
        template.resource_count_is("AWS::Lambda::Function", 0)


def test_no_stack_synthesizes_a_custom_resource_provider_role(stacks):
    for name, template in stacks.items():
        for logical_id in template.find_resources("AWS::IAM::Role"):
            assert "CustomResourceProvider" not in logical_id, f"{name}/{logical_id}"


def test_no_stack_synthesizes_a_custom_resource(stacks):
    for name, template in stacks.items():
        for logical_id, resource in template.to_json()["Resources"].items():
            assert not resource["Type"].startswith("Custom::"), (
                f"{name}/{logical_id} is {resource['Type']}"
            )


def test_nothing_uses_the_default_security_group(stacks):
    """The default group is left as AWS creates it and never attached, which is
    what the removed restrictDefaultSecurityGroup custom resource was for. This
    proves the half that matters without a Lambda."""
    for name in ("application", "data"):
        body = json.dumps(stacks[name].to_json())
        assert "DefaultSecurityGroup" not in body, name

    network = stacks["network"].to_json()["Resources"]
    vpc_ids = [k for k, v in network.items() if v["Type"] == "AWS::EC2::VPC"]
    assert len(vpc_ids) == 1
    for logical_id, resource in network.items():
        if resource["Type"] != "AWS::EC2::SecurityGroup":
            continue
        # Every group in the VPC is one Corridor declared and named.
        assert resource["Properties"].get("GroupDescription"), logical_id


def test_the_postgres_log_group_is_named_for_the_instance(stacks):
    """RDS exports to a group derived from the instance identifier, so the
    identifier has to be stable and the group has to exist first."""
    stacks["data"].has_resource_properties(
        "AWS::RDS::DBInstance", {"DBInstanceIdentifier": "corridor-nonprod"}
    )
    stacks["data"].has_resource_properties(
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": "/aws/rds/instance/corridor-nonprod/postgresql",
            "RetentionInDays": 14,
        },
    )


def test_the_web_boundary_is_declared_wherever_reads_run_as_corridor_web(stacks):
    """#694 refuses every gated route when the declared flag is off but the
    reading login is `corridor_web`. The web task connects as exactly that, so
    omitting the declaration answers 503 on /readyz and the load balancer
    deregisters a task that is otherwise fine."""
    template = stacks["application"].to_json()["Resources"]
    for logical_id, resource in template.items():
        if resource["Type"] != "AWS::ECS::TaskDefinition":
            continue
        for container in resource["Properties"]["ContainerDefinitions"]:
            env = {
                entry["Name"]: entry["Value"]
                for entry in container.get("Environment", [])
            }
            assert env.get("CORRIDOR_LIVE_PILOT_WEB_BOUNDARY") == "true", (
                f"{logical_id} does not declare the live-pilot web boundary"
            )


# --- least privilege on the artifact bucket ----------------------------
def test_the_web_role_cannot_delete_an_artifact(stacks):
    """grant_read_write includes s3:DeleteObject*. An internet-facing process
    holding it can bypass S3ObjectStore.delete_under_policy, its DeletionPermit
    and any retention hold; versioning keeps the bytes but every record
    reference to that key stops resolving."""
    template = stacks["application"].to_json()["Resources"]
    for logical_id, policy in template.items():
        if policy.get("Type") != "AWS::IAM::Policy":
            continue
        roles = json.dumps(policy["Properties"].get("Roles"))
        if "WebTaskRole" not in roles:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            actions = [action] if isinstance(action, str) else action
            for entry in actions:
                assert not entry.lower().startswith("s3:deleteobject"), (
                    f"{logical_id} grants the web role {entry}"
                )


def test_the_web_role_can_still_read_and_write_artifacts(stacks):
    """Removing deletion must not remove the store's actual work."""
    template = stacks["application"].to_json()["Resources"]
    granted: set[str] = set()
    for policy in template.values():
        if policy.get("Type") != "AWS::IAM::Policy":
            continue
        if "WebTaskRole" not in json.dumps(policy["Properties"].get("Roles")):
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            granted.update([action] if isinstance(action, str) else action)
    assert any(a.startswith("s3:GetObject") for a in granted), granted
    assert any(a.startswith("s3:PutObject") for a in granted), granted
    assert any(a.startswith("s3:List") for a in granted), granted


def test_deletion_stays_with_the_batch_retention_capability(stacks):
    """Corridor's deletion policy runs in the batch capability, so that is the
    identity that may delete."""
    template = stacks["application"].to_json()["Resources"]
    granted: set[str] = set()
    for policy in template.values():
        if policy.get("Type") != "AWS::IAM::Policy":
            continue
        if "BatchTaskRole" not in json.dumps(policy["Properties"].get("Roles")):
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            granted.update([action] if isinstance(action, str) else action)
    assert any(a.lower().startswith("s3:deleteobject") for a in granted), granted


# --- client identity behind the load balancer --------------------------
def test_the_web_command_trusts_only_the_vpc_for_forwarded_headers(stacks):
    """Without this the ASGI client address is the ALB node and every caller
    shares one rate-limit identity, so one anonymous caller can exhaust the
    sign-in allowance for everybody."""
    template = stacks["application"].to_json()["Resources"]
    web = [
        resource
        for logical_id, resource in template.items()
        if resource["Type"] == "AWS::ECS::TaskDefinition" and "Web" in logical_id
    ]
    assert len(web) == 1
    command = web[0]["Properties"]["ContainerDefinitions"][0]["Command"]
    assert "--proxy-headers" in command
    index = command.index("--forwarded-allow-ips")
    assert command[index + 1] == "10.20.0.0/16", command
    assert "*" not in command, "forwarded headers must not be trusted from anywhere"


# --- sign-in delivery ---------------------------------------------------
def test_the_web_task_selects_a_real_delivery_adapter(stacks):
    """The logging sender delivers nothing, and /sign-in/request reports
    success either way, so a deployment without an adapter looks healthy while
    nobody can authenticate."""
    template = stacks["application"].to_json()["Resources"]
    for logical_id, resource in template.items():
        if resource["Type"] != "AWS::ECS::TaskDefinition" or "Web" not in logical_id:
            continue
        env = {
            entry["Name"]: entry["Value"]
            for entry in resource["Properties"]["ContainerDefinitions"][0]["Environment"]
        }
        assert env["CORRIDOR_EMAIL_BACKEND"] == "ses"
        assert env["CORRIDOR_SIGN_IN_SENDER"] == "no-reply@example.com"


def test_serving_without_a_sign_in_sender_is_refused():
    with pytest.raises(ValueError, match="signInSender is required"):
        _build(sign_in_sender="", web_desired_count=1)


# --- the hostname a release can actually verify ------------------------
def test_a_certificate_requires_the_hostname_it_covers():
    """An ACM certificate covers a domain, never the generated ELB name, so a
    release verifying the load balancer's own hostname fails certificate
    validation after every otherwise-successful deployment."""
    with pytest.raises(ValueError, match="publicHostname is required"):
        _build(public_hostname="")


def test_the_application_url_is_the_certificate_covered_hostname(stacks):
    outputs = stacks["application"].to_json()["Outputs"]
    assert outputs["ApplicationUrl"]["Value"] == "https://pilot.example.com"
    # The raw name is still reported, for operators; it is never probed.
    assert "LoadBalancerDns" in outputs


def test_the_web_role_can_send_as_the_verified_sender_and_no_other(stacks):
    """Selecting the SES adapter without granting the call produces an
    AccessDenied on the first sign-in, which looks the same from outside as
    having no adapter at all."""
    template = stacks["application"].to_json()["Resources"]
    statements = []
    for policy in template.values():
        if policy.get("Type") != "AWS::IAM::Policy":
            continue
        if "WebTaskRole" not in json.dumps(policy["Properties"].get("Roles")):
            continue
        statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])

    ses = [
        statement
        for statement in statements
        if "ses:SendEmail" in json.dumps(statement.get("Action"))
    ]
    assert len(ses) == 1, "the web role cannot send the sign-in link"
    condition = ses[0]["Condition"]["StringEquals"]["ses:FromAddress"]
    assert condition == "no-reply@example.com"


def test_no_other_role_can_send_mail(stacks):
    """Only the web process issues sign-in links."""
    template = stacks["application"].to_json()["Resources"]
    for logical_id, policy in template.items():
        if policy.get("Type") != "AWS::IAM::Policy":
            continue
        roles = json.dumps(policy["Properties"].get("Roles"))
        if "WebTaskRole" in roles:
            continue
        assert "ses:" not in json.dumps(policy["Properties"]["PolicyDocument"]), (
            logical_id
        )


def test_the_web_task_is_told_its_own_public_origin(stacks):
    """A sign-in link must not be built from the request. The stack derives
    the origin from the hostname the certificate covers."""
    template = stacks["application"].to_json()["Resources"]
    for logical_id, resource in template.items():
        if resource["Type"] != "AWS::ECS::TaskDefinition" or "Web" not in logical_id:
            continue
        env = {
            entry["Name"]: entry["Value"]
            for entry in resource["Properties"]["ContainerDefinitions"][0]["Environment"]
        }
        assert env["CORRIDOR_PUBLIC_ORIGIN"] == "https://pilot.example.com"
        # And it agrees with what a release verifies.
        outputs = stacks["application"].to_json()["Outputs"]
        assert outputs["ApplicationUrl"]["Value"] == env["CORRIDOR_PUBLIC_ORIGIN"]


def test_the_web_role_can_create_an_artifact_but_never_replace_one(stacks):
    """S3ObjectStore.put sends IfNoneMatch="*", but that is the web process's
    own code: a compromised one omits it and overwrites an existing key.
    Versioning does not save the reader -- one that names no version gets the
    new bytes and fails digest verification, so the artifact is unreadable
    despite any retention hold. IAM has to carry the condition."""
    template = stacks["application"].to_json()["Resources"]
    puts = []
    for policy in template.values():
        if policy.get("Type") != "AWS::IAM::Policy":
            continue
        if "WebTaskRole" not in json.dumps(policy["Properties"].get("Roles")):
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            actions = [action] if isinstance(action, str) else action
            if any(a.startswith("s3:PutObject") for a in actions):
                puts.append(statement)

    assert puts, "the web role cannot write an artifact at all"
    for statement in puts:
        condition = statement.get("Condition", {})
        assert condition.get("Null", {}).get("s3:if-none-match") == "false", (
            f"unconditional PutObject on the web role: {statement}"
        )


def test_the_web_role_holds_no_retention_control(stacks):
    """grant_put also carries PutObjectLegalHold and PutObjectRetention. An
    internet-facing process has no business setting or clearing those."""
    template = stacks["application"].to_json()["Resources"]
    for policy in template.values():
        if policy.get("Type") != "AWS::IAM::Policy":
            continue
        if "WebTaskRole" not in json.dumps(policy["Properties"].get("Roles")):
            continue
        document = json.dumps(policy["Properties"]["PolicyDocument"])
        assert "PutObjectLegalHold" not in document
        assert "PutObjectRetention" not in document


def test_the_batch_role_may_still_replace_and_delete(stacks):
    """The retention capability runs the deletion policy, so it keeps the
    authority the web role gives up."""
    template = stacks["application"].to_json()["Resources"]
    granted: set[str] = set()
    for policy in template.values():
        if policy.get("Type") != "AWS::IAM::Policy":
            continue
        if "BatchTaskRole" not in json.dumps(policy["Properties"].get("Roles")):
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            granted.update([action] if isinstance(action, str) else action)
    assert any(a.lower().startswith("s3:deleteobject") for a in granted), granted
    assert any(a.startswith("s3:PutObject") for a in granted), granted
