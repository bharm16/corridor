"""A dedicated VPC, and the security-group edges that make public subnets safe.

The default VPC would work and cost the same, but it is account state CDK does
not own: its subnets, routes and default security group can be changed outside
this repository, and a teardown cannot tell Corridor's networking from
anything else that drifted into it. A dedicated VPC makes the topology
reproducible and gives the environment a clean destruction boundary.

There is no NAT Gateway. NAT costs $0.045/hr (~$32.85/month) plus per-GB
processing -- more than the database this environment exists to exercise. The
alternative, private subnets plus interface endpoints for ECR API, ECR DKR,
Secrets Manager and Logs, is roughly $29/month and no better. So tasks run in
public subnets with public IPs for *egress only*, and every inbound path is
closed by security group rather than by subnet placement:

    internet -> ALB (443/80)          the only thing with an internet ingress
    ALB      -> web (8412)            the app's real port, not 80/443
    web      -> database (5432)
    batch    -> database (5432)       no ingress of its own at all
    migration-> database (5432)       no ingress of its own at all

The database sits in isolated subnets with no route to the internet gateway,
so `publicly_accessible=False` is reinforced by there being no path at all.
"""

from aws_cdk import (
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
)
from cdk_nag import NagSuppressions
from constructs import Construct

# The port `make queue` serves on: uvicorn corridor.web.app:app --port 8412.
CORRIDOR_APP_PORT = 8412

# Every role Corridor creates lives here. The CloudFormation execution policy
# only permits role writes on this path, so a stack cannot quietly create a
# role somewhere the policy does not constrain.
CORRIDOR_ROLE_PATH = "/corridor/nonproduction/"


class CorridorNetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
            max_azs=2,
            nat_gateways=0,
            enable_dns_support=True,
            enable_dns_hostnames=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
            # Free. Keeps artifact reads and writes off the public path.
            gateway_endpoints={
                "S3": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.S3
                )
            },
        )

        flow_log_group = logs.LogGroup(
            self,
            "VpcFlowLogGroup",
            log_group_name="/vpc/corridor-nonprod/flow-logs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        flow_log_role = iam.Role(
            self,
            "VpcFlowLogRole",
            path=CORRIDOR_ROLE_PATH,
            assumed_by=iam.ServicePrincipal("vpc-flow-logs.amazonaws.com"),
            description="Delivers Corridor VPC flow logs to CloudWatch.",
        )
        self.vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                flow_log_group, flow_log_role
            ),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        self.alb_sg = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=self.vpc,
            description="Corridor ALB: the only internet-facing ingress.",
            allow_all_outbound=True,
        )
        self.alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS from the internet"
        )
        self.alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP, redirected to HTTPS"
        )

        self.web_sg = ec2.SecurityGroup(
            self,
            "WebSecurityGroup",
            vpc=self.vpc,
            description="Corridor web tasks. Ingress from the ALB only.",
            allow_all_outbound=True,
        )
        self.web_sg.add_ingress_rule(
            self.alb_sg,
            ec2.Port.tcp(CORRIDOR_APP_PORT),
            "Application port from the ALB security group only",
        )

        # Batch and migration tasks are started on demand and serve nothing.
        # Neither gets an ingress rule of any kind.
        self.batch_sg = ec2.SecurityGroup(
            self,
            "BatchSecurityGroup",
            vpc=self.vpc,
            description="Corridor batch/worker tasks. No inbound access.",
            allow_all_outbound=True,
        )
        self.migration_sg = ec2.SecurityGroup(
            self,
            "MigrationSecurityGroup",
            vpc=self.vpc,
            description="Corridor migration task. No inbound access.",
            allow_all_outbound=True,
        )

        self.db_sg = ec2.SecurityGroup(
            self,
            "DatabaseSecurityGroup",
            vpc=self.vpc,
            description="Corridor PostgreSQL. Reachable only from Corridor tasks.",
            allow_all_outbound=False,
        )
        for source, label in (
            (self.web_sg, "web"),
            (self.batch_sg, "batch"),
            (self.migration_sg, "migration"),
        ):
            self.db_sg.add_ingress_rule(
                source, ec2.Port.tcp(5432), f"PostgreSQL from the {label} tasks"
            )

        NagSuppressions.add_resource_suppressions(
            self.alb_sg,
            [
                {
                    "id": "AwsSolutions-EC23",
                    "reason": (
                        "This is the internet-facing load balancer's security "
                        "group; accepting 0.0.0.0/0 is its purpose. Only 80 "
                        "(redirect) and 443 are open, and it is the only "
                        "security group in the VPC with a CIDR ingress rule -- "
                        "test_only_the_alb_accepts_internet_traffic enforces "
                        "that."
                    ),
                }
            ],
        )
        NagSuppressions.add_resource_suppressions(
            flow_log_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The flow-log delivery role writes to log streams under "
                        "one named log group; the wildcard is the stream name."
                    ),
                    "appliesTo": ["Resource::*"],
                }
            ],
            apply_to_children=True,
        )
