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

There is deliberately no OPENAI_API_KEY secret. `openai_api_key` defaults to
empty in config.py and the deterministic UCM path does not call a model, so
creating an empty model secret would add access surface that nothing reads.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_rds as rds,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from cdk_nag import NagSuppressions
from constructs import Construct

# Verify the exact minor version is still offered before the first deploy:
#   aws rds describe-db-engine-versions --engine postgres --engine-version 16
# auto_minor_version_upgrade keeps it current afterwards.
POSTGRES_VERSION = rds.PostgresEngineVersion.of("16.4", "16")


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

        self.database = rds.DatabaseInstance(
            self,
            "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=POSTGRES_VERSION),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO
            ),
            vpc=vpc,
            subnet_group=subnet_group,
            security_groups=[database_security_group],
            # The DDL owner. config.py's `database_url`; only the migration
            # task is granted read access to this secret.
            credentials=rds.Credentials.from_generated_secret(
                "corridor_admin", secret_name="corridor/nonprod/db-admin"
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

        # Session/signing secret for the web application.
        self.app_secret = secretsmanager.Secret(
            self,
            "AppSecret",
            secret_name="corridor/nonprod/app-secret",
            description="Corridor web application session/signing secret.",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64, exclude_punctuation=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

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
        for secret in (generated, self.web_db_secret,
                       self.worker_db_secret, self.app_secret):
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
