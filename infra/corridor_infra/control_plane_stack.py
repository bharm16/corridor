"""Retain environment routing and receipt custody outside customer destruction.

A second database on the customer RDS instance would disappear when that
instance is removed. This stack uses the same network and deployment system,
but owns a separate RDS instance, backups, and credentials. Customer tasks get
only its lookup capability; it stores no source or Project Record content.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_logs as logs,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from corridor_infra.data_stack import POSTGRES_VERSION


class CorridorControlPlaneStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, vpc: ec2.Vpc,
        database_security_group: ec2.SecurityGroup, **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)
        instance_identifier = "corridor-nonprod-control"
        postgres_logs = logs.LogGroup(
            self, "ControlPostgresLogGroup",
            log_group_name=f"/aws/rds/instance/{instance_identifier}/postgresql",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.RETAIN,
        )
        subnet_group = rds.SubnetGroup(
            self, "ControlDatabaseSubnetGroup", vpc=vpc,
            description="Independent Corridor control-plane PostgreSQL.",
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.database = rds.DatabaseInstance(
            self, "ControlDatabase", instance_identifier=instance_identifier,
            engine=rds.DatabaseInstanceEngine.postgres(version=POSTGRES_VERSION),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO,
            ),
            vpc=vpc, subnet_group=subnet_group,
            security_groups=[database_security_group],
            credentials=rds.Credentials.from_generated_secret(
                "corridor_control_owner", secret_name="corridor/nonprod/control-owner",
            ),
            database_name="corridor_control", allocated_storage=20,
            storage_type=rds.StorageType.GP3, storage_encrypted=True,
            multi_az=False, publicly_accessible=False,
            backup_retention=Duration.days(7), delete_automated_backups=False,
            deletion_protection=True, auto_minor_version_upgrade=True,
            removal_policy=RemovalPolicy.RETAIN,
            cloudwatch_logs_exports=["postgresql"],
        )
        self.database.node.add_dependency(postgres_logs)
        self.operations_secret = self._login_secret(
            "ControlOperationsSecret", "corridor_control_operator",
        )
        self.resolver_secret = self._login_secret(
            "ControlResolverSecret", "corridor_control_runtime",
        )
        NagSuppressions.add_resource_suppressions(
            self.database,
            [
                {"id": "AwsSolutions-RDS3", "reason": (
                    "Single-AZ synthetic environment follows the existing #489 "
                    "cost posture. This independent instance retains its own "
                    "7-day PITR window and must pass a separate restore rehearsal."
                )},
                {"id": "AwsSolutions-RDS11", "reason": (
                    "5432 is reachable only from the named web, batch and migration "
                    "security groups in isolated subnets, with no CIDR ingress."
                )},
            ],
            apply_to_children=True,
        )
        for secret in (
            self.database.node.try_find_child("Secret"),
            self.operations_secret, self.resolver_secret,
        ):
            if secret is not None:
                NagSuppressions.add_resource_suppressions(secret, [{
                    "id": "AwsSolutions-SMG4", "reason": (
                        "Automatic database secret rotation remains a #535 activation "
                        "prerequisite alongside the existing customer credentials. "
                        "This NAT-free deployment holds synthetic identifiers only; "
                        "bootstrap reapplies the injected login passwords on release."
                    ),
                }])
        CfnOutput(self, "ControlPlaneDatabaseEndpoint",
                  value=self.database.db_instance_endpoint_address)
        CfnOutput(self, "ControlPlaneDatabaseName", value="corridor_control")

    def _login_secret(self, construct_id: str, login: str) -> secretsmanager.Secret:
        return secretsmanager.Secret(
            self, construct_id, secret_name=f"corridor/nonprod/{login}",
            description=f"Separate control-plane PostgreSQL login: {login}.",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=f'{{"username":"{login}"}}',
                generate_string_key="password", password_length=48,
                exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
