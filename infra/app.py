#!/usr/bin/env python3
"""Corridor nonproduction environment (us-east-2).

Stack order is a dependency order, not a preference:

    AccountFoundation   audit trail and the CI entry identity. Deployed first,
                        locally, so that everything after it is recorded and
                        so GitHub has an identity to deploy with.
    Network             the VPC and every security-group edge.
    Data                database, artifact bucket, credentials. Stateful.
    Application         registry, cluster, tasks, load balancer. Disposable.

Nothing here creates AWS resources. `cdk synth` renders templates; `cdk deploy`
is a separate, deliberate act.
"""

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from corridor_infra.account_foundation_stack import CorridorAccountFoundationStack
from corridor_infra.application_stack import CorridorApplicationStack
from corridor_infra.data_stack import CorridorDataStack
from corridor_infra.network_stack import CorridorNetworkStack

app = cdk.App()


def ctx(key: str, default=None):
    return app.node.try_get_context(f"corridor:{key}") or default


env = cdk.Environment(account=ctx("account"), region=ctx("region", "us-east-2"))

foundation = CorridorAccountFoundationStack(
    app,
    "CorridorAccountFoundation",
    env=env,
    github_repo=ctx("githubRepo", "bharm16/corridor"),
    github_environment=ctx("githubEnvironment", "nonproduction"),
)

network = CorridorNetworkStack(app, "CorridorNetwork", env=env)

data = CorridorDataStack(
    app,
    "CorridorData",
    env=env,
    vpc=network.vpc,
    database_security_group=network.db_sg,
)

CorridorApplicationStack(
    app,
    "CorridorApplication",
    env=env,
    vpc=network.vpc,
    alb_security_group=network.alb_sg,
    web_security_group=network.web_sg,
    batch_security_group=network.batch_sg,
    migration_security_group=network.migration_sg,
    database=data.database,
    artifact_bucket=data.artifact_bucket,
    web_db_secret=data.web_db_secret,
    worker_db_secret=data.worker_db_secret,
    image_tag=ctx("imageTag", "bootstrap"),
    web_desired_count=int(app.node.try_get_context("corridor:webDesiredCount") or 0),
    certificate_arn=ctx("certificateArn", ""),
)

for key, value in {
    "Application": "Corridor",
    "Environment": "nonproduction",
    "ManagedBy": "CDK",
}.items():
    cdk.Tags.of(app).add(key, value)

cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
