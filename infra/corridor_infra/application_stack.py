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

from .network_stack import (
    CORRIDOR_APP_PORT,
    CORRIDOR_ROLE_PATH,
    CORRIDOR_VPC_CIDR,
)


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
        public_hostname: str = "",
        sign_in_sender: str = "",
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
            container_insights_v2=ecs.ContainerInsights.ENABLED,
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
            # #694's two halves have to agree. The web task reads as
            # `corridor_web`, which is the login the revoke was aimed at, so a
            # deployment that does not also declare the boundary reads as
            # INCONSISTENT and `refuse_routes_the_boundary_cannot_serve`
            # answers 503 on every gated route -- /readyz included, which would
            # make the load balancer deregister the task and take the whole
            # environment down. Found by running the container, not by reading
            # the stack.
            "CORRIDOR_LIVE_PILOT_WEB_BOUNDARY": "true",
            # Without a real adapter /sign-in/request reports success and
            # delivers nothing, and the application refuses to start rather
            # than accept sign-ins it cannot fulfil.
            "CORRIDOR_EMAIL_BACKEND": "ses",
            "CORRIDOR_SIGN_IN_SENDER": sign_in_sender,
            # The origin every sign-in link is built from. Derived from the
            # public hostname rather than read from the request: base_url is
            # the caller's own Host header, so a forged host would make
            # Corridor email the real user a live token pointing elsewhere.
            "CORRIDOR_PUBLIC_ORIGIN": (
                f"https://{public_hostname}" if public_hostname else ""
            ),
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
                # Without these the ASGI client address is the ALB node, so
                # auth.client_scope throttles every caller under one of a
                # handful of private addresses: one anonymous caller can
                # exhaust the sign-in allowance for everybody. client_scope
                # deliberately ignores raw X-Forwarded-For -- forging it would
                # let a caller reset its own backoff -- so the header has to be
                # resolved here, and only from the VPC the ALB sits in.
                "--proxy-headers",
                "--forwarded-allow-ips",
                CORRIDOR_VPC_CIDR,
            ],
            port=CORRIDOR_APP_PORT,
        )
        # Read and write, never delete. grant_read_write includes
        # s3:DeleteObject*, which would let a compromised internet-facing
        # process bypass S3ObjectStore.delete_under_policy, its DeletionPermit
        # and any retention hold. Versioning keeps the bytes recoverable but
        # every record reference to that key stops resolving, which is the part
        # that matters. Deletion stays with the batch retention capability,
        # which is the thing that actually runs the policy.
        artifact_bucket.grant_read(web_role)
        artifact_bucket.grant_put(web_role)

        # The sign-in link is delivered by the web process itself, so this is
        # the one role that needs SES. Scoped by the from-address rather than
        # only the identity ARN: a verified *domain* identity would otherwise
        # let this role send as any address under it.
        web_role.add_to_policy(
            iam.PolicyStatement(
                sid="SendTheSignInLinkAsTheVerifiedSender",
                actions=["ses:SendEmail"],
                resources=[
                    f"arn:aws:ses:{Aws.REGION}:{Aws.ACCOUNT_ID}:identity/*"
                ],
                conditions={
                    "StringEquals": {"ses:FromAddress": sign_in_sender}
                },
            )
        )

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
            # The default 50% would take a one-task service to zero healthy
            # tasks during a deployment: the environment would be down for the
            # length of every rollout. 100/200 starts the replacement before
            # retiring the incumbent.
            min_healthy_percent=100,
            max_healthy_percent=200,
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
        # The retention capability. This is the identity that runs Corridor's
        # deletion policy, so it is the one that may delete.
        artifact_bucket.grant_read_write(batch_role)
        artifact_bucket.grant_delete(batch_role)

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

        # There is no plaintext path at all. An earlier version fell back to an
        # HTTP listener when no certificate was supplied, which is the wrong
        # default for an externally reachable environment; the flag that was
        # meant to gate it was also read with bool(), and CDK context arrives
        # from the command line as a string, so bool("false") would have been
        # True.
        #
        # Without a certificate the stack still synthesises, so the network and
        # data stacks can be deployed and the service can exist at zero, but it
        # gets no listener and therefore no way in.
        if certificate_arn and not public_hostname:
            raise ValueError(
                "corridor:publicHostname is required alongside a certificate. "
                "An ACM certificate covers a domain, never the generated "
                "*.elb.amazonaws.com name, so a release that verified the load "
                "balancer's own hostname would fail certificate validation "
                "after every otherwise-successful deployment."
            )
        if not sign_in_sender and web_desired_count > 0:
            raise ValueError(
                "corridor:signInSender is required before the web service can "
                "serve traffic. Without a verified SES identity no sign-in "
                "link can be delivered and nobody can authenticate."
            )
        if not certificate_arn and web_desired_count > 0:
            raise ValueError(
                "corridor:certificateArn is required before the web service "
                "can serve traffic. Issue an ACM certificate and pass it "
                f"through the protected GitHub environment; got "
                f"webDesiredCount={web_desired_count} with no certificate."
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
            # No certificate: no listener. The load balancer exists so the
            # stack is deployable and its DNS name is stable, but nothing can
            # reach the service until a certificate is supplied.
            Annotations.of(self).add_warning(
                "No corridor:certificateArn was supplied, so the load balancer "
                "has no listener and the web service is unreachable. Supply a "
                "certificate before raising corridor:webDesiredCount above 0."
            )

        # The unhealthy-host metric only exists once the target group is
        # attached to a listener, so there is nothing to alarm on until a
        # certificate makes one.
        if certificate_arn:
            self._alarms(target_group)
        self._suppressions(
            [web_exec, batch_exec, migration_exec,
             web_role, batch_role, migration_role],
            alb_logs,
            task_definitions=(web_task, batch_task, migration_task),
        )

        CfnOutput(self, "LoadBalancerDns", value=self.alb.load_balancer_dns_name)
        # What a release actually verifies. The load balancer's own name is not
        # covered by the certificate, so it is reported but never probed.
        CfnOutput(
            self,
            "ApplicationUrl",
            value=(
                f"https://{public_hostname}"
                if public_hostname
                else "not-configured"
            ),
        )
        CfnOutput(self, "RepositoryUri", value=self.repository.repository_uri)
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "ClusterArn", value=cluster.cluster_arn)
        CfnOutput(self, "WebServiceName", value=self.web_service.service_name)
        CfnOutput(
            self, "WebTaskDefinitionArn", value=web_task.task_definition_arn
        )
        CfnOutput(
            self,
            "MigrationTaskDefinitionArn",
            value=migration_task.task_definition_arn,
        )
        CfnOutput(self, "BatchTaskDefinitionArn", value=batch_task.task_definition_arn)
        CfnOutput(
            self,
            "TaskSubnetIds",
            value=",".join(
                vpc.select_subnets(subnet_type=ec2.SubnetType.PUBLIC).subnet_ids
            ),
        )
        CfnOutput(
            self,
            "BatchSecurityGroupId",
            value=batch_security_group.security_group_id,
        )
        CfnOutput(
            self,
            "MigrationSecurityGroupId",
            value=migration_security_group.security_group_id,
        )

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
            path=CORRIDOR_ROLE_PATH,
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
            path=CORRIDOR_ROLE_PATH,
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
                            "non-secret deployment facts: the environment "
                            "name, the storage backend, the artifact bucket and "
                            "its region, the task role, and the database host, "
                            "port and name. Every credential -- the schema "
                            "owner's and the two runtime logins -- is injected "
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
                            {
                                # A verified identity may be a domain or an
                                # address, so the identity ARN is not knowable
                                # at synthesis. The ses:FromAddress condition
                                # is the real bound and it names one address.
                                "regex": "/^Resource::arn:aws:ses:.*:identity\\/\\*$/g"
                            },
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
