"""Stateful resources, separated so the application stack stays disposable.

Everything here outlives a deployment: the database, the artifact bucket, and
the credentials. They are in their own stack with termination protection and
retaining removal policies so that destroying or replacing the application
never destroys project data.

Construct IDs in this file are load-bearing. CloudFormation derives logical IDs
from them, and renaming a construct after the first deploy is read as
"delete and create a new one" -- which for a database means losing it. Do not
rename these; add new ones alongside instead.

Corridor already models three database identities (src/corridor/config.py):

    database_url          the migration/DDL owner, which owns the schema.
                          No application process connects with it (#492).
    web_database_url      the corridor_web login.
    worker_database_url   the corridor_worker login.

So this stack creates the RDS master secret for the migration path only, plus
one secret per runtime login. The web and batch tasks are never given the
master credential -- that is what keeps "the migration job is the only schema
writer" (#489) a real boundary rather than a convention.

There is deliberately no OPENAI_API_KEY secret, and no application/session
secret either. `openai_api_key` defaults to empty in config.py and the
deterministic UCM path calls no model; and Corridor's Settings has no session
or signing-secret field at all -- grep finds no app_secret, session_secret or
secret_key. Creating either would be inventing a contract the application does
not have.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_rds as rds,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from cdk_nag import NagSuppressions
from constructs import Construct

# 16.4 is deprecated in RDS. 16.15 is available and carries CVE fixes, but the
# CloudFormation spec bundled with the pinned aws-cdk-lib knows 16.x only up to
# 16.14, so `cdk synth --strict` refuses it. 16.14 is what synthesises clean.
#
# UPGRADE GATE: this is acceptable for #489's synthetic environment, which
# holds no customer data. Move to 16.15 or later before #535 activates live
# data -- either when a newer aws-cdk-lib ships an updated spec, or by
# upgrading the instance in place after deployment. auto_minor_version_upgrade
# is on, so RDS will also move it during a maintenance window.
#   aws rds describe-db-engine-versions --engine postgres --engine-version 16
POSTGRES_VERSION = rds.PostgresEngineVersion.of("16.14", "16")


class CorridorDataStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.Vpc,
        database_security_group: ec2.SecurityGroup,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)

        access_logs = s3.Bucket(
            self,
            "ArtifactAccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # The content-addressed store behind CORRIDOR_STORAGE_BACKEND=s3
        # (corridor.object_storage, ADR-0079). Versioned because a
        # content-addressed store must never silently lose bytes a record cites.
        self.artifact_bucket = s3.Bucket(
            self,
            "ArtifactBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            server_access_logs_bucket=access_logs,
            server_access_logs_prefix="artifact-bucket-access/",
        )

        subnet_group = rds.SubnetGroup(
            self,
            "DatabaseSubnetGroup",
            vpc=vpc,
            description="Corridor PostgreSQL, isolated subnets only.",
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # A stable identifier, because the log group name is derived from it
        # and has to be knowable at synthesis time.
        instance_identifier = "corridor-nonprod"

        # Created explicitly rather than through `cloudwatch_logs_retention`.
        # That property makes CDK synthesize a LogRetention custom resource --
        # a Lambda, its own role, and a policy needing logs:PutRetentionPolicy
        # on *. Both boundaries would then have to admit Lambda creation and
        # that wildcard, widening the ceiling for every delegated role in the
        # account to save one property. RDS writes to this group by convention,
        # so declaring it directly costs nothing and creates no Lambda.
        postgres_logs = logs.LogGroup(
            self,
            "DatabasePostgresLogGroup",
            log_group_name=f"/aws/rds/instance/{instance_identifier}/postgresql",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.database = rds.DatabaseInstance(
            self,
            "Database",
            instance_identifier=instance_identifier,
            engine=rds.DatabaseInstanceEngine.postgres(version=POSTGRES_VERSION),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO
            ),
            vpc=vpc,
            subnet_group=subnet_group,
            security_groups=[database_security_group],
            # The DDL owner. config.py's `database_url`; only the migration
            # task is granted read access to this secret.
            # The master username must be exactly "corridor". The consolidated
            # baseline is a pg_dump carrying 408 `OWNER TO corridor;`
            # statements, so any other master login makes `alembic upgrade
            # head` fail on the first one with: role "corridor" does not exist.
            credentials=rds.Credentials.from_generated_secret(
                "corridor", secret_name="corridor/nonprod/db-admin"
            ),
            database_name="corridor",
            allocated_storage=20,
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            multi_az=False,
            publicly_accessible=False,
            backup_retention=Duration.days(7),
            delete_automated_backups=False,
            deletion_protection=True,
            auto_minor_version_upgrade=True,
            removal_policy=RemovalPolicy.SNAPSHOT,
            cloudwatch_logs_exports=["postgresql"],
        )

        # Runtime logins. The migration creates the roles themselves; these
        # hold the passwords config.py reads as CORRIDOR_WEB_DB_PASSWORD and
        # CORRIDOR_WORKER_DB_PASSWORD.
        self.web_db_secret = self._login_secret("WebDbSecret", "corridor_web")
        self.worker_db_secret = self._login_secret("WorkerDbSecret", "corridor_worker")

        NagSuppressions.add_resource_suppressions(
            access_logs,
            [{"id": "AwsSolutions-S1",
              "reason": "Terminal access-log bucket; S3 cannot log into itself."}],
        )
        NagSuppressions.add_resource_suppressions(
            self.database,
            [
                {
                    "id": "AwsSolutions-RDS3",
                    "reason": (
                        "Single-AZ is a deliberate nonproduction cost decision. "
                        "Multi-AZ doubles the instance charge to protect an "
                        "environment that holds no customer data and whose "
                        "recovery contract is the 7-day PITR window plus the "
                        "restore rehearsal #489 requires. Revisit before #535 "
                        "activates live data."
                    ),
                },
                {
                    "id": "AwsSolutions-RDS11",
                    "reason": (
                        "Port obfuscation defends against untargeted scanning. "
                        "This instance sits in isolated subnets with no route "
                        "to the internet gateway and accepts 5432 only from "
                        "three named security groups, so there is no scannable "
                        "surface for a non-default port to hide from. Changing "
                        "it would only desynchronise the port from every "
                        "DATABASE_URL the application builds."
                    ),
                },
            ],
            apply_to_children=True,
        )
        # `database.secret` is the *attachment*; the generated secret itself is
        # a child construct of the instance, and that is what cdk-nag scans.
        generated = self.database.node.try_find_child("Secret")
        for secret in (generated, self.web_db_secret, self.worker_db_secret):
            if secret is None:
                continue
            NagSuppressions.add_resource_suppressions(
                secret,
                [
                    {
                        "id": "AwsSolutions-SMG4",
                        "reason": (
                            "Rotation is deferred, not rejected. A rotation "
                            "Lambda must reach both Secrets Manager and the "
                            "database; from the isolated subnets that needs "
                            "either a NAT Gateway (~$33/mo) or a Secrets "
                            "Manager interface endpoint (~$7.30/mo), and this "
                            "environment is deliberately NAT-free. These "
                            "credentials guard synthetic data only. Rotation "
                            "and the endpoint to support it are a prerequisite "
                            "of #535, when live customer data is activated."
                        ),
                    }
                ],
            )

        # The group must exist before RDS starts exporting into it.
        self.database.node.add_dependency(postgres_logs)

        CfnOutput(self, "ArtifactBucketName", value=self.artifact_bucket.bucket_name)
        CfnOutput(self, "DatabaseEndpoint",
                  value=self.database.db_instance_endpoint_address)

    def _login_secret(self, construct_id: str, login: str) -> secretsmanager.Secret:
        return secretsmanager.Secret(
            self,
            construct_id,
            secret_name=f"corridor/nonprod/{login}",
            description=f"Password for the {login} PostgreSQL login.",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=f'{{"username":"{login}"}}',
                generate_string_key="password",
                password_length=48,
                exclude_characters=' %+~`#$&*()|[]{}:;<>?!\'/"\\@',
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
