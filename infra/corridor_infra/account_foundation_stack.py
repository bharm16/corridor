"""Account-level audit and the CI entry identity.

This stack exists because two things must be true before any Corridor
infrastructure is deployed, and neither belongs to the application: there must
be a durable record of what changed the account, and GitHub must be able to
deploy without a long-lived AWS key.

CloudTrail Event History is not that record. It is a 90-day, per-region
management-event view that cannot be extended and carries no integrity proof.
A trail with log-file validation is the durable, tamper-evident record, so the
trail is modelled here rather than clicked once in the console.

The GitHub role is deliberately an *entry* identity, not a deployment
administrator. It may assume the CDK bootstrap roles and read the bootstrap
version, and nothing else. CloudFormation, running as the CDK execution role,
is what actually creates resources. Granting the GitHub role direct ECS, RDS
and IAM authority in addition would build a second, parallel deployment path
with a far larger blast radius than the one it duplicates.
"""

from aws_cdk import (
    Aws,
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_cloudtrail as cloudtrail,
    aws_iam as iam,
    aws_s3 as s3,
)
from cdk_nag import NagSuppressions
from constructs import Construct

# The default CDK bootstrap qualifier. If the account is bootstrapped with a
# custom qualifier, override it here and in the bootstrap command together.
BOOTSTRAP_QUALIFIER = "hnb659fds"

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"


class CorridorAccountFoundationStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_repo: str,
        github_environment: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Audit -------------------------------------------------------
        # A bucket cannot deliver server access logs to itself, so the access
        # log bucket terminates the chain. That is the one suppression below.
        access_logs = s3.Bucket(
            self,
            "AuditAccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        trail_bucket = s3.Bucket(
            self,
            "CloudTrailBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            server_access_logs_bucket=access_logs,
            server_access_logs_prefix="cloudtrail-bucket-access/",
        )

        self.trail = cloudtrail.Trail(
            self,
            "AccountTrail",
            bucket=trail_bucket,
            is_multi_region_trail=True,
            include_global_service_events=True,
            enable_file_validation=True,
            # Management events only. S3 and Lambda data events are billed per
            # event and would be dominated by Corridor's own artifact reads;
            # they are added later, scoped to one bucket, if the pilot's audit
            # contract asks for them.
            management_events=cloudtrail.ReadWriteType.ALL,
        )

        # --- CI entry identity -------------------------------------------
        oidc_provider = iam.OpenIdConnectProvider(
            self,
            "GitHubOidcProvider",
            url=GITHUB_OIDC_URL,
            client_ids=["sts.amazonaws.com"],
        )

        # The subject is pinned to the repository *and* the deployment
        # environment. `repo:owner/name:*` would let any branch or pull request
        # assume this role; the environment form additionally requires the
        # GitHub environment's own protection rules to admit the run.
        subject = f"repo:{github_repo}:environment:{github_environment}"

        self.github_role = iam.Role(
            self,
            "GitHubDeployRole",
            role_name="corridor-nonprod-cdk-deploy",
            description=(
                "GitHub Actions CDK entry identity. Assumes the CDK bootstrap "
                "roles; holds no direct ECS, RDS, S3 or IAM authority of its "
                "own. Named for CDK so it is not later mistaken for a direct "
                "application-release role -- if a non-CDK image/ECS release "
                "workflow is ever added, it gets its own role."
            ),
            assumed_by=iam.WebIdentityPrincipal(
                oidc_provider.open_id_connect_provider_arn,
                {
                    "StringEquals": {
                        f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                        f"{GITHUB_OIDC_HOST}:sub": subject,
                    }
                },
            ),
        )

        bootstrap_roles = [
            f"arn:aws:iam::{Aws.ACCOUNT_ID}:role/cdk-{BOOTSTRAP_QUALIFIER}-{name}"
            f"-role-{Aws.ACCOUNT_ID}-{Aws.REGION}"
            for name in ("deploy", "file-publishing", "image-publishing", "lookup")
        ]

        self.github_role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles",
                actions=["sts:AssumeRole"],
                resources=bootstrap_roles,
            )
        )
        self.github_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadBootstrapVersion",
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter"
                    f"/cdk-bootstrap/{BOOTSTRAP_QUALIFIER}/version"
                ],
            )
        )

        NagSuppressions.add_resource_suppressions(
            access_logs,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": (
                        "This is the terminal server-access-log bucket. S3 "
                        "cannot deliver a bucket's access logs into itself, so "
                        "the chain has to end at a bucket without them."
                    ),
                }
            ],
        )

        CfnOutput(self, "GitHubDeployRoleArn", value=self.github_role.role_arn)
        CfnOutput(self, "CloudTrailBucketName", value=trail_bucket.bucket_name)
