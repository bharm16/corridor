"""ECR, the ECS cluster, the load balancer, and the three task shapes.

One image, not two. `workers/render/render_worker.py` is not a service: it is a
subprocess that `src/corridor/render_profiles.py` shells out to with
`uv run --project workers/render`, from inside the same source tree. The batch
commands (`make carry-forward`, `retention`, `due-work`, `ledger-archive`) are
entry points in the same package. Nothing in the repository has a separate
build context, so two repositories would publish the same bytes twice and add
the risk that web and batch drift onto different revisions.

One service, not two. There is no long-running worker in the repository today:
no `make worker` target, no queue-consumer loop, and `worker_database_url` is
read only in `src/corridor/db.py` to build a session factory. The worker is a
*database capability* used by on-demand commands. Modelling it as an always-on
Fargate service would deploy an idle container with no entry point to run, so
it is a task definition that is started when needed. #489's replica-safe
worker leases are the prerequisite for turning this into a service.

Roles are separated because they have genuinely different authority:

    execution role  pulls the image, creates log streams, and injects secrets.
                    This is the ECS agent's identity, not the application's.
    web task role   reads and writes the artifact bucket. Reads its own DB
                    login secret. Cannot reach the admin credential.
    batch task role same bucket access, its own DB login secret.
    migration role  the only identity that may read the RDS admin secret.

The application task roles are deliberately not granted logs:PutLogEvents. The
awslogs driver delivers container output using the *execution* role; the
application does not call CloudWatch itself.
"""

from aws_cdk import (
    Annotations,
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_rds as rds,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from .network_stack import CORRIDOR_APP_PORT


class CorridorApplicationStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.Vpc,
        alb_security_group: ec2.SecurityGroup,
        web_security_group: ec2.SecurityGroup,
        batch_security_group: ec2.SecurityGroup,
        migration_security_group: ec2.SecurityGroup,
        database: rds.DatabaseInstance,
        artifact_bucket: s3.Bucket,
        web_db_secret: secretsmanager.Secret,
        worker_db_secret: secretsmanager.Secret,
        image_tag: str,
        web_desired_count: int,
        certificate_arn: str = "",
        allow_insecure_http: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.repository = ecr.Repository(
            self,
            "Repository",
            repository_name="corridor",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Keep the 10 most recent images.",
                    max_image_count=10,
                )
            ],
        )

        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            cluster_name="corridor-nonprod",
            container_insights=True,
        )

        db_admin_secret = database.secret
        assert db_admin_secret is not None, "RDS generated credential is required"

        if not image_tag or image_tag == "bootstrap":
            raise ValueError(
                "corridor:imageTag must be an immutable tag -- the commit SHA "
                "the image was built from. The repository is configured "
                "IMMUTABLE, and a placeholder like 'bootstrap' would pin every "
                "deployment to whatever happened to be pushed first."
            )
        image = ecs.ContainerImage.from_ecr_repository(self.repository, image_tag)

        # Only names src/corridor/config.py actually reads. Settings has no
        # env_prefix, so a field with a validation_alias uses that alias
        # (CORRIDOR_*) and a field without one uses its own name uppercased
        # (DATABASE_URL, WEB_DATABASE_URL, WORKER_DATABASE_URL).
        common_env = {
            "CORRIDOR_ENVIRONMENT": "nonproduction",
            "CORRIDOR_STORAGE_BACKEND": "s3",
            "CORRIDOR_S3_BUCKET": artifact_bucket.bucket_name,
            "CORRIDOR_S3_REGION": Aws.REGION,
            # Connection parts. The image entrypoint composes these plus the
            # injected password into the SQLAlchemy URL its role needs --
            # DATABASE_URL for migration, WEB_DATABASE_URL for web,
            # WORKER_DATABASE_URL for batch. They cannot be composed here: a
            # password only exists as a secret reference at task-definition
            # time, and the RDS-managed secret is a JSON document rather than
            # a URL.
            "CORRIDOR_DB_HOST": database.db_instance_endpoint_address,
            "CORRIDOR_DB_PORT": database.db_instance_endpoint_port,
            "CORRIDOR_DB_NAME": "corridor",
        }

        # --- web ---------------------------------------------------------
        web_task, web_role, web_exec = self._task(
            "Web",
            cpu=256,
            memory=512,
            image=image,
            environment=common_env,
            secrets={
                "CORRIDOR_WEB_DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    web_db_secret, "password"
                ),
            },
            command=[
                "uvicorn",
                "corridor.web.app:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(CORRIDOR_APP_PORT),
            ],
            port=CORRIDOR_APP_PORT,
        )
        artifact_bucket.grant_read_write(web_role)

        self.web_service = ecs.FargateService(
            self,
            "WebService",
            cluster=cluster,
            task_definition=web_task,
            desired_count=web_desired_count,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[web_security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            health_check_grace_period=Duration.seconds(90),
            enable_execute_command=False,
        )

        # --- batch (the "worker" capability, started on demand) ----------
        batch_task, batch_role, batch_exec = self._task(
            "Batch",
            cpu=1024,
            memory=2048,
            image=image,
            environment=common_env,
            secrets={
                "CORRIDOR_WORKER_DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    worker_db_secret, "password"
                )
            },
            # 1 vCPU / 2 GB: this container runs PyMuPDF and shells out to
            # the OpenCV render subprocess, and it is billed only while a task
            # is actually running. Reduce it once CloudWatch shows real usage.
            # Overridden per RunTask; carry-forward is the routine one.
            command=["python", "-m", "corridor.automatic_carry_forward_cli"],
        )
        artifact_bucket.grant_read_write(batch_role)

        # --- migration ---------------------------------------------------
        migration_task, migration_role, migration_exec = self._task(
            "Migration",
            cpu=512,
            memory=1024,
            image=image,
            environment=common_env,
            secrets={
                # Discrete fields, not the whole JSON document.
                "CORRIDOR_DB_ADMIN_USERNAME": ecs.Secret.from_secrets_manager(
                    db_admin_secret, "username"
                ),
                "CORRIDOR_DB_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(
                    db_admin_secret, "password"
                ),
                # The migration *creates* the corridor_web and corridor_worker
                # logins, and the baseline reads exactly these two variables to
                # set their passwords (baseline_versions/a1c4e7b0d2f3, lines
                # 59-60). Without them it falls back to predictable role-name
                # passwords, which would not match the randomized secrets the
                # web and batch tasks are given -- so neither could
                # authenticate after a successful migration. This widens the
                # migration task only; web and batch still cannot read each
                # other's login or the admin credential.
                "CORRIDOR_WEB_DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    web_db_secret, "password"
                ),
                "CORRIDOR_WORKER_DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    worker_db_secret, "password"
                ),
            },
            command=["alembic", "upgrade", "head"],
        )
        # No explicit grant: attaching the admin credential to the migration
        # task definition grants it to *that* task's execution role alone.
        # The web and batch execution roles never see it.

        # --- load balancer -----------------------------------------------
        alb_logs = s3.Bucket(
            self,
            "AlbAccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_security_group,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )
        self.alb.log_access_logs(alb_logs, prefix="alb")

        target_group = elbv2.ApplicationTargetGroup(
            self,
            "WebTargetGroup",
            vpc=vpc,
            port=CORRIDOR_APP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            targets=[self.web_service],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                # /readyz, not /health: /health is the aggregate operational
                # answer and returns 503 on a stale worker heartbeat. This
                # environment runs no resident worker, so /health would
                # deregister a web task that is serving perfectly well.
                path="/readyz",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
            ),
        )

        # Fail closed. Absence of a certificate previously selected plaintext
        # silently, which is the wrong default for an externally reachable
        # environment: the quiet path should be the safe one, and choosing
        # plaintext should cost a deliberate flag.
        if not certificate_arn and not allow_insecure_http:
            raise ValueError(
                "corridor:certificateArn is required. Issue an ACM certificate "
                "and pass it through the protected GitHub environment. To run "
                "an internal, disposable HTTP-only stack instead, set "
                "corridor:allowInsecureHttp=true explicitly -- it is disabled "
                "by default and must not be used for the nonproduction "
                "environment."
            )

        if certificate_arn:
            self.alb.add_listener(
                "HttpsListener",
                port=443,
                protocol=elbv2.ApplicationProtocol.HTTPS,
                certificates=[
                    elbv2.ListenerCertificate.from_arn(certificate_arn)
                ],
                ssl_policy=elbv2.SslPolicy.TLS13_RES,
                default_target_groups=[target_group],
            )
            self.alb.add_redirect(
                source_port=80,
                target_port=443,
                target_protocol=elbv2.ApplicationProtocol.HTTPS,
            )
        else:
            # Reached only when allow_insecure_http was set explicitly; the
            # guard above rejects the default path.
            Annotations.of(self).add_warning(
                "corridor:allowInsecureHttp is set. This load balancer serves "
                "plaintext HTTP and must not be used for the nonproduction "
                "environment or any externally reachable demo."
            )
            self.alb.add_listener(
                "HttpListener",
                port=80,
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_target_groups=[target_group],
            )

        self._alarms(target_group)
        self._suppressions(
            [web_exec, batch_exec, migration_exec,
             web_role, batch_role, migration_role],
            alb_logs,
            task_definitions=(web_task, batch_task, migration_task),
        )

        CfnOutput(self, "LoadBalancerDns", value=self.alb.load_balancer_dns_name)
        CfnOutput(self, "RepositoryUri", value=self.repository.repository_uri)

    # ------------------------------------------------------------------
    def _task(
        self,
        name: str,
        *,
        cpu: int,
        memory: int,
        image: ecs.ContainerImage,
        environment: dict,
        secrets: dict,
        command: list,
        port: int | None = None,
    ):
        execution_role = iam.Role(
            self,
            f"{name}ExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=(
                f"ECS agent identity for the {name.lower()} task: image pull, "
                "log delivery, and injection of only this task's secrets."
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )
        role = iam.Role(
            self,
            f"{name}TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=f"Corridor {name.lower()} application identity.",
        )
        task = ecs.FargateTaskDefinition(
            self,
            f"{name}TaskDefinition",
            cpu=cpu,
            memory_limit_mib=memory,
            execution_role=execution_role,
            task_role=role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        container = task.add_container(
            f"{name}Container",
            image=image,
            command=command,
            # The entrypoint selects which single database URL to compose from
            # this. It refuses to start without it.
            environment={**environment, "CORRIDOR_TASK_ROLE": name.lower()},
            secrets=secrets,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=name.lower(),
                log_group=logs.LogGroup(
                    self,
                    f"{name}LogGroup",
                    log_group_name=f"/ecs/corridor-nonprod/{name.lower()}",
                    retention=logs.RetentionDays.TWO_WEEKS,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
            ),
        )
        if port is not None:
            container.add_port_mappings(ecs.PortMapping(container_port=port))
        return task, role, execution_role

    def _alarms(self, target_group: elbv2.ApplicationTargetGroup) -> None:
        cloudwatch.Alarm(
            self,
            "WebUnhealthyHostsAlarm",
            metric=target_group.metrics.unhealthy_host_count(
                period=Duration.minutes(1)
            ),
            threshold=1,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Corridor web has an unhealthy target.",
        )

    def _suppressions(self, roles, alb_logs, task_definitions=()) -> None:
        for task in task_definitions:
            NagSuppressions.add_resource_suppressions(
                task,
                [
                    {
                        "id": "AwsSolutions-ECS2",
                        "reason": (
                            "The only direct environment variables are "
                            "non-secret deployment facts: CORRIDOR_ENVIRONMENT, "
                            "CORRIDOR_STORAGE_BACKEND, the artifact bucket name "
                            "and its region. Every credential -- database "
                            "logins and the application secret -- is injected "
                            "through ecs.Secret from Secrets Manager, never as "
                            "a plaintext variable."
                        ),
                    }
                ],
                apply_to_children=True,
            )
        for role in roles:
            NagSuppressions.add_resource_suppressions(
                role,
                [
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": (
                            "Object-level grants resolve to <bucket>/* and log "
                            "delivery to <log-group>:*. Both are scoped to a "
                            "single named resource; the wildcard is the object "
                            "or stream key, not the resource. The bare "
                            "Resource::* is ecr:GetAuthorizationToken, which "
                            "AWS defines as an account-level action that admits "
                            "no resource ARN."
                        ),
                        "appliesTo": [
                            "Resource::*",
                            "Action::s3:GetBucket*",
                            "Action::s3:GetObject*",
                            "Action::s3:List*",
                            "Action::s3:DeleteObject*",
                            "Action::s3:Abort*",
                            {
                                "regex": "/^Resource::<ArtifactBucket.*\\.Arn>\\/\\*$/g"
                            },
                        ],
                    },
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": (
                            "AmazonECSTaskExecutionRolePolicy is the AWS-managed "
                            "policy for the ECS agent's own image-pull and log "
                            "path. Replacing it with a hand-written copy would "
                            "drift from AWS's updates without reducing scope."
                        ),
                    },
                ],
                apply_to_children=True,
            )
        NagSuppressions.add_resource_suppressions(
            alb_logs,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": "Terminal access-log bucket; S3 cannot log into itself.",
                }
            ],
        )
