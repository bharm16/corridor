"""Which stack outputs a deployment reads back, and what reads each one.

The release workflow used to spell the nine output keys it needs in an inline
heredoc, and nothing related that list to the stacks that emit them. Renaming a
`CfnOutput` passed `make check`, passed every infrastructure assertion, passed
`cdk synth --strict`, passed `release-gate`, and failed for the first time in
the middle of a release -- after the `nonproduction` environment approval had
been spent and, past the drain step, with both services already at zero.

The names live here so that the run-time reader in `app-release.yml`, the
synthesis-time assertion in `infra/tests/test_stacks.py`, and the disposition
provider read one declaration rather than three copies of it. That test imports
this module by path, because `infra/` is a separate uv project that
deliberately excludes the application's dependencies, so this module imports
the standard library only and nothing from `corridor`.

It lives under `src/corridor/` for the third of those readers. It was in
`scripts/`, and `aws_environment_disposition.py` therefore could not import it
at all: `Dockerfile` copies only `container_entrypoint.py` out of `scripts/`,
so inside the deployed image the module was simply not there, and the provider
spelled its five output keys itself. A rename failed at synthesis for the
release path and still refused a correctly requested destruction.

Every authored output is declared here, including the ones no program reads.
`infra-deploy.yml` puts the whole outputs document in the deployment's run
summary, so an operator reading one there during an incident is a reader; an
output that appears in no list below is a decision nobody has made.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from types import MappingProxyType

# The stack the release resolves its outputs from. Stacks are named below as
# CloudFormation knows them, which is how `describe-stacks`, `infra/app.py` and
# the runbook name them too.
RELEASE_STACK = "CorridorApplication"

# The container the release names in `--overrides` when it runs the migration
# task. `application_stack.py` builds it as f"{name}Container" for "Migration";
# ECS refuses a `containerOverrides` entry that names no container in the task
# definition, so a rename here is a release that stops at the schema step.
MIGRATION_CONTAINER_NAME = "MigrationContainer"

# CloudFormation still emits an output whose value the deployment left unset.
UNSET_OUTPUT_VALUE = "not-configured"

# Application-stack output -> the environment variable the release exports it as.
#
# `ClusterName` is here because the workflow used to carry `CLUSTER:
# corridor-nonprod` in its own `env:` block while the stack emitted the cluster
# it actually created and nothing related the two. Every `aws ecs` call in the
# release names that cluster, so a stack that renamed it would have run the
# drain, the migration and both service updates against a cluster this
# repository invented.
RELEASE_STACK_OUTPUTS: Mapping[str, str] = MappingProxyType({
    "RepositoryUri": "REPOSITORY_URI",
    "ClusterName": "CLUSTER",
    "SignInExpiryRuleName": "EXPIRY_RULE",
    "WebServiceName": "WEB_SERVICE",
    "WorkerServiceName": "WORKER_SERVICE",
    "TaskSubnetIds": "TASK_SUBNETS",
    "MigrationSecurityGroupId": "MIGRATION_SG",
    "MigrationTaskDefinitionArn": "BASE_MIGRATION_TD",
    "BatchTaskDefinitionArn": "BASE_BATCH_TD",
    "WebTaskDefinitionArn": "BASE_WEB_TD",
    "ApplicationUrl": "APPLICATION_URL",
})

RELEASE_WORKFLOW = "app-release.yml, through resolve()"
DISPOSITION_PROVIDER = "src/corridor/aws_environment_disposition.py"
OPERATOR_SUMMARY = "an operator, from the infra-deploy run summary; no program reads it"

# The outputs the disposition provider reads back to prove that the stacks it
# is about to destroy are the registered environment's, by the stack that emits
# them. Each names the attribute of the registered resources it must equal, so
# a renamed output and a renamed registration field both fail here rather than
# leaving a comparison that silently never matches.
DISPOSITION_STACK_OUTPUTS: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "CorridorApplication": MappingProxyType({
        "DispositionCustomerId": "customer_id",
        "DispositionEnvironmentId": "environment_id",
        "DispositionDeploymentId": "deployment_id",
    }),
    "CorridorData": MappingProxyType({
        "DatabaseEndpoint": "database_host",
        "ArtifactBucketName": "object_namespace_bucket",
    }),
})

# Every authored output, by the stack that emits it and what reads it.
STACK_OUTPUT_READERS: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "CorridorAccountFoundation": MappingProxyType({
        "GitHubDeployRoleArn": OPERATOR_SUMMARY,
        "GitHubReleaseRoleArn": OPERATOR_SUMMARY,
        "CloudTrailBucketName": OPERATOR_SUMMARY,
    }),
    "CorridorNetwork": MappingProxyType({}),
    "CorridorControlPlane": MappingProxyType({
        "ControlPlaneDatabaseEndpoint": OPERATOR_SUMMARY,
        "ControlPlaneDatabaseName": OPERATOR_SUMMARY,
    }),
    "CorridorData": MappingProxyType({
        **dict.fromkeys(DISPOSITION_STACK_OUTPUTS["CorridorData"], DISPOSITION_PROVIDER),
        "DispositionArtifactLogsBucket": OPERATOR_SUMMARY,
        "DispositionDatabaseArn": OPERATOR_SUMMARY,
    }),
    "CorridorApplication": MappingProxyType({
        **dict.fromkeys(RELEASE_STACK_OUTPUTS, RELEASE_WORKFLOW),
        **dict.fromkeys(
            DISPOSITION_STACK_OUTPUTS["CorridorApplication"], DISPOSITION_PROVIDER
        ),
        "LoadBalancerDns": OPERATOR_SUMMARY,
        "ClusterArn": OPERATOR_SUMMARY,
        "BatchSecurityGroupId": OPERATOR_SUMMARY,
        "DispositionAlbLogsBucket": OPERATOR_SUMMARY,
    }),
})


class ReleaseContractError(RuntimeError):
    """The deployed stack does not carry what the release reads off it."""


def resolve(outputs: Iterable[Mapping[str, str]]) -> list[str]:
    """Turn `describe-stacks` output rows into the release's `NAME=value` lines.

    Every missing key is reported together: the release is about to spend an
    environment approval, so one round trip should say everything that is wrong.
    """
    values = {row["OutputKey"]: row["OutputValue"] for row in outputs}
    missing = sorted(
        key
        for key in RELEASE_STACK_OUTPUTS
        if not values.get(key) or values[key] == UNSET_OUTPUT_VALUE
    )
    if missing:
        raise ReleaseContractError(
            f"{RELEASE_STACK} is missing or has not set {', '.join(missing)}; "
            "the release reads these outputs by name"
        )
    return [f"{name}={values[key]}" for key, name in RELEASE_STACK_OUTPUTS.items()]


# Explicitly reviewed #489 resource kinds. Any new kind needs a disposition
# implementation before it can be silently included in this profile.
_DISPOSITION_DATA_TYPES = frozenset({"AWS::RDS::DBInstance", "AWS::RDS::DBSubnetGroup",
    "AWS::S3::Bucket", "AWS::SecretsManager::Secret", "AWS::Logs::LogGroup",
    "AWS::ECR::Repository", "AWS::KMS::Key"})
_DISPOSITION_STATELESS_TYPES = frozenset({"AWS::S3::BucketPolicy", "AWS::IAM::Role", "AWS::IAM::Policy",
    "AWS::ECS::Cluster", "AWS::ECS::Service", "AWS::ECS::TaskDefinition",
    "AWS::EC2::SecurityGroup", "AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress",
    "AWS::ElasticLoadBalancingV2::LoadBalancer", "AWS::ElasticLoadBalancingV2::Listener",
    "AWS::ElasticLoadBalancingV2::TargetGroup", "AWS::ElasticLoadBalancingV2::ListenerRule",
    "AWS::SecretsManager::SecretTargetAttachment", "AWS::CloudWatch::Alarm",
    "AWS::ApplicationAutoScaling::ScalableTarget", "AWS::ApplicationAutoScaling::ScalingPolicy",
    "AWS::CDK::Metadata", "AWS::Events::Rule"})


DISPOSITION_RESOURCE_TYPES = _DISPOSITION_DATA_TYPES | _DISPOSITION_STATELESS_TYPES


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve_command = commands.add_parser(
        "resolve", help="print the release's NAME=value assignments"
    )
    resolve_command.add_argument(
        "outputs", help="a describe-stacks Stacks[0].Outputs document"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    with open(args.outputs, encoding="utf-8") as handle:
        rows = json.load(handle)
    try:
        assignments = resolve(rows)
    except ReleaseContractError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    print("\n".join(assignments))
    return 0


if __name__ == "__main__":
    sys.exit(main())
