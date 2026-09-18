"""ECR, the ECS cluster, the load balancer, and the three task shapes.

One image, not two. `workers/render/render_worker.py` is not a service: it is a
subprocess that `src/corridor/render_profiles.py` shells out to with
`uv run --project workers/render`, from inside the same source tree. The batch
commands (`make carry-forward`, `retention`, `due-work`, `ledger-archive`) are
entry points in the same package. Nothing in the repository has a separate
build context, so two repositories would publish the same bytes twice and add
the risk that web and batch drift onto different revisions.

The web service and the Due Work supervisor run separately. The supervisor
uses the existing Batch task's database capability, durable claims, deadlines,
and recovery receipts. Its process generates a fresh runtime owner identity;
replicas never share a claim owner. Both services start at zero until the
release workflow has migrated and bound them to the same image digest.

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

import re

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
    aws_events as events,
    aws_events_targets as targets,
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

# The rule `corridor.control_plane.identifier` owns, restated because this
# project deliberately excludes the application's dependencies. The synthesis
# guard below and the container entrypoint's start-up guard must refuse the
# same shapes, so `infra/tests/test_stacks.py` asserts this copy equals the
# entrypoint's, which `tests/test_vocabulary_owners.py` pins to the owner.
STABLE_IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}"


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
        control_database: rds.DatabaseInstance,
        control_operations_secret: secretsmanager.Secret,
        control_resolver_secret: secretsmanager.Secret,
        customer_routing_secret: secretsmanager.Secret,
        customer_id: str,
        customer_environment_id: str,
        deployment_id: str,
        data_class: str,
        image_tag: str,
        web_desired_count: int,
        worker_desired_count: int = 0,
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
        control_owner_secret = control_database.secret
        assert control_owner_secret is not None, "control-plane owner credential is required"
        if data_class != "synthetic":
            raise ValueError("corridor:dataClass must explicitly be synthetic for #489")
        for name, value in (
            ("customerId", customer_id), ("customerEnvironmentId", customer_environment_id),
            ("deploymentId", deployment_id),
        ):
            if not value or not re.fullmatch(STABLE_IDENTIFIER_PATTERN, value):
                raise ValueError(f"corridor:{name} must be an explicit stable identifier")

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
            "CORRIDOR_CONTROL_DB_HOST": control_database.db_instance_endpoint_address,
            "CORRIDOR_CONTROL_DB_PORT": control_database.db_instance_endpoint_port,
            "CORRIDOR_CONTROL_DB_NAME": "corridor_control",
            "CORRIDOR_CUSTOMER_ID": customer_id,
            "CORRIDOR_CUSTOMER_ENVIRONMENT_ID": customer_environment_id,
            "CORRIDOR_DEPLOYMENT_ID": deployment_id,
            "CORRIDOR_DEPLOYMENT_DATA_CLASS": data_class,
        }
        resolver_secrets = {
            f"CORRIDOR_CONTROL_RESOLVER_DB_{field.upper()}": ecs.Secret.from_secrets_manager(
                control_resolver_secret, field,
            )
            for field in ("username", "password")
        }

        # --- web ---------------------------------------------------------
        web_task, web_role, web_exec = self._task(
            "Web",
            cpu=256,
            memory=512,
            image=image,
            environment=common_env,
            secrets={
                **resolver_secrets,
                "CORRIDOR_WEB_DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    web_db_secret, "password"
                ),
                "CORRIDOR_CUSTOMER_ROUTING_KEY": ecs.Secret.from_secrets_manager(
                    customer_routing_secret,
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
            # ECS release proof requires container health as well as the ALB
            # target check. Without this ECS reports UNKNOWN even when HTTP
            # is reachable, so the exact-task verification cannot pass.
            health_check=ecs.HealthCheck(
                command=[
                    "CMD", "curl", "--fail", "--silent", "--show-error",
                    f"http://127.0.0.1:{CORRIDOR_APP_PORT}/readyz",
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(15),
                retries=3,
                start_period=Duration.seconds(60),
            ),
        )
        # Read and write, never delete. grant_read_write includes
        # s3:DeleteObject*, which would let a compromised internet-facing
        # process bypass S3ObjectStore.delete_under_policy, its DeletionPermit
        # and any retention hold. Versioning keeps the bytes recoverable but
        # every record reference to that key stops resolving, which is the part
        # that matters. Deletion stays with the batch retention capability,
        # which is the thing that actually runs the policy.
        artifact_bucket.grant_read(web_role)

        # Write-once, enforced by IAM rather than by the application.
        # `S3ObjectStore.put` already sends `IfNoneMatch="*"`, but that is the
        # web process's own code: a compromised one simply omits it and
        # overwrites an existing key. Versioning does not save the reader --
        # a reader that names no version gets the new bytes, fails digest
        # verification, and the artifact becomes unreadable despite any
        # retention hold.
        #
        # `Null: {"s3:if-none-match": "false"}` means the header must be
        # present, so this role can create a key and can never replace one.
        # grant_put is not used because it also carries PutObjectLegalHold and
        # PutObjectRetention, and an internet-facing process has no business
        # setting or clearing a retention control.
        web_role.add_to_policy(
            iam.PolicyStatement(
                sid="WebMayCreateAnArtifactButNeverReplaceOne",
                actions=["s3:PutObject"],
                resources=[artifact_bucket.arn_for_objects("*")],
                conditions={"Null": {"s3:if-none-match": "false"}},
            )
        )

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

        # --- worker (retain the existing Batch task and role identities) --
        batch_task, batch_role, batch_exec = self._task(
            "Batch",
            cpu=1024,
            memory=2048,
            image=image,
            environment=common_env,
            secrets={
                **resolver_secrets,
                "CORRIDOR_WORKER_DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    worker_db_secret, "password"
                )
            },
            # The same supervisor consumes preparation requests and scheduled
            # Due Work. A task definition alone would never start either.
            command=[
                "python", "-m", "corridor.due_work_cli", "supervise",
                "--poll-seconds=5",
            ],
            health_check=ecs.HealthCheck(
                # ECS execs health commands with the task definition's env,
                # not PID 1's composed URL. Reuse the credential-scrubbing
                # entrypoint so this probe also connects as corridor_worker.
                command=[
                    "CMD", "python", "/opt/corridor/scripts/container_entrypoint.py",
                    "python", "-m", "corridor.due_work_cli", "health",
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(15),
                retries=3,
                start_period=Duration.seconds(60),
            ),
            stop_timeout=Duration.seconds(120),
        )
        # The retention capability. This is the identity that runs Corridor's
        # deletion policy, so it is the one that may delete.
        artifact_bucket.grant_read_write(batch_role)
        artifact_bucket.grant_delete(batch_role)

        self.worker_service = ecs.FargateService(
            self,
            "WorkerService",
            cluster=cluster,
            task_definition=batch_task,
            desired_count=worker_desired_count,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[batch_security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=100,
            max_healthy_percent=200,
            enable_execute_command=False,
        )
        cloudwatch.Alarm(
            self,
            "WorkerMissingTasksAlarm",
            metric=cloudwatch.MathExpression(
                expression="FILL(desired, 0) - FILL(running, 0)",
                using_metrics={
                    key: cloudwatch.Metric(
                        namespace="ECS/ContainerInsights",
                        metric_name=metric_name,
                        dimensions_map={
                            "ClusterName": cluster.cluster_name,
                            "ServiceName": self.worker_service.service_name,
                        },
                        statistic="Average",
                        period=Duration.minutes(1),
                    )
                    for key, metric_name in (
                        ("desired", "DesiredTaskCount"),
                        ("running", "RunningTaskCount"),
                    )
                },
                period=Duration.minutes(1),
            ),
            threshold=1,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "Corridor has fewer running Due Work supervisors than requested. "
                "Inspect ECS task health and retained Due Work receipts."
            ),
        )

        # --- scheduled sign-in record expiry (ADR-0102, #943) ------------
        # The one thing that makes the expiry pass happen without a person.
        # #927 built `expire-sign-in-records`, the policy and the receipt, and
        # it is runnable by hand; nothing ran it on a schedule, which is the
        # defect #907 named and ADR-0102 assigns to this deployment task.
        #
        # It reuses the batch task definition above -- the same image, under
        # the same corridor_worker credentials -- and overrides only the
        # command. corridor_worker already holds DELETE on web_sessions,
        # sign_in_tokens and sign_in_attempts and INSERT on audit_log
        # (ADR-0102 s.2), so this adds no grant and no new application role.
        # The only new identity is EventBridge's, which may run this one task
        # definition and pass its roles. Its network identity is the batch
        # task's own: the batch security group (already admitted on 5432) in a
        # public subnet with a public IP, because this VPC has no NAT and the
        # image is pulled over the internet gateway.
        #
        # One schedule for the whole customer environment, never one per
        # project and never a placeholder project: the three relations are
        # keyed to a person, not a project, so a per-project schedule would run
        # the same environment-wide delete once per project (ADR-0102, and the
        # rejected "extend Due Work's scope model" alternative -- Due Work's
        # project foreign keys stay NOT NULL).
        #
        # Daily, at a fixed low-traffic hour. None of the three relations
        # carries a time-only index, so every pass scans all three; that is
        # cheap while they are small and is ADR-0102's reason not to run it
        # hourly. The interval is also the deletion lag the ADR states as its
        # own term -- an eligible row goes at the next pass, so up to roughly a
        # further day after it becomes eligible -- not a 24-hour deadline.
        expiry_schedule_role = iam.Role(
            self,
            "SignInExpiryScheduleRole",
            path=CORRIDOR_ROLE_PATH,
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
            description=(
                "EventBridge identity that runs the daily sign-in record "
                "expiry pass on the existing batch task definition. It may run "
                "that one task definition and pass its roles; nothing more."
            ),
        )
        expiry_schedule = events.Rule(
            self,
            "SignInRecordExpirySchedule",
            description=(
                "Daily environment-wide sign-in record expiry pass "
                "(corridor.retention_cli expire-sign-in-records); ADR-0102, "
                "#943."
            ),
            # cron(0 8 * * ? *): every day at 08:00 UTC. A fixed hour rather
            # than rate(1 day) so the pass runs at a predictable wall-clock
            # time; the exact hour is not load-bearing, because the cadence is
            # the deletion lag, not a deadline.
            schedule=events.Schedule.cron(minute="0", hour="8"),
        )
        expiry_schedule.add_target(
            targets.EcsTask(
                cluster=cluster,
                task_definition=batch_task,
                task_count=1,
                role=expiry_schedule_role,
                retry_attempts=0,
                max_event_age=Duration.seconds(60),
                security_groups=[batch_security_group],
                subnet_selection=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PUBLIC
                ),
                assign_public_ip=True,
                launch_type=ecs.LaunchType.FARGATE,
                # Override only the command. The entrypoint still composes
                # WORKER_DATABASE_URL because CORRIDOR_TASK_ROLE=batch, and
                # `expire-sign-in-records` takes no argument: --as-of defaults
                # to the process start, which is what a fixed command line
                # needs (ADR-0102). An operator reproducing a past pass still
                # passes --as-of by hand through `make retention`.
                container_overrides=[
                    targets.ContainerOverride(
                        container_name=batch_task.default_container.container_name,
                        command=[
                            "python",
                            "-m",
                            "corridor.retention_cli",
                            "expire-sign-in-records",
                        ],
                    )
                ],
            )
        )
        # Application-only releases register new revisions in this same family.
        # The target's generated grant covers the initial revision; the family
        # grant lets the identical invocation role run each verified release.
        expiry_schedule_role.add_to_policy(iam.PolicyStatement(
            actions=["ecs:RunTask"],
            resources=[self.format_arn(service="ecs", resource="task-definition",
                                       resource_name=f"{batch_task.family}:*")],
            conditions={"ArnEquals": {"ecs:cluster": cluster.cluster_arn}},
        ))
        NagSuppressions.add_resource_suppressions(expiry_schedule_role, [{
            "id": "AwsSolutions-IAM5", "reason": "Only revisions of the existing Batch family, restricted to its cluster.",
            "appliesTo": [{"regex": "/^Resource::.*:task-definition\\/.*:\\*$/g"}],
        }], apply_to_children=True)
        # The application owns the schedule and grants the already-created
        # release role access to this exact rule and invocation role. No
        # cross-stack reference back into the account foundation is needed.
        release_role = iam.Role.from_role_arn(self, "ScheduleReleaseIdentity",
            f"arn:aws:iam::{Aws.ACCOUNT_ID}:role{CORRIDOR_ROLE_PATH}corridor-nonprod-app-release")
        release_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["events:DescribeRule", "events:DisableRule", "events:EnableRule",
                     "events:ListTagsForResource", "events:ListTargetsByRule", "events:PutTargets",
                     "events:TagResource", "events:UntagResource"],
            resources=[expiry_schedule.rule_arn],
        ))
        release_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["iam:PassRole"], resources=[expiry_schedule_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "events.amazonaws.com"}},
        ))
        CfnOutput(self, "SignInExpiryRuleName", value=expiry_schedule.rule_name)

        # The only wildcard in EventBridge's generated policy is
        # ecs:TagResource on arn:...:task/<cluster>/*. ECS mints the task id
        # when the run starts, so it cannot be named at synthesis; the
        # wildcard is that runtime id, scoped to this one cluster. RunTask is
        # pinned to the batch task definition (conditioned on the cluster) and
        # PassRole to that task's own roles, so neither of those is wild --
        # test_the_expiry_schedule_role_is_scoped_and_grants_no_new_authority
        # holds that line. This role adds no authority beyond running that one
        # task (ADR-0102).
        NagSuppressions.add_resource_suppressions(
            expiry_schedule_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "ecs:TagResource applies to the task ECS creates at run "
                        "time, whose id is not knowable at synthesis; the "
                        "wildcard is that runtime task id under this one "
                        "cluster. RunTask is scoped to the batch task "
                        "definition and PassRole to that task's own roles."
                    ),
                    "appliesTo": [
                        {
                            "regex": "/^Resource::arn:<AWS::Partition>:ecs:.*:task\\/<Cluster.*>\\/\\*$/g"
                        },
                    ],
                }
            ],
            apply_to_children=True,
        )

        # --- migration ---------------------------------------------------
        migration_task, migration_role, migration_exec = self._task(
            "Migration",
            cpu=512,
            memory=1024,
            image=image,
            environment=common_env,
            secrets={
                **resolver_secrets,
                **{
                    f"CORRIDOR_CONTROL_{capability}_DB_{field.upper()}":
                        ecs.Secret.from_secrets_manager(secret, field)
                    for capability, secret in (
                        ("OWNER", control_owner_secret),
                        ("OPERATIONS", control_operations_secret),
                    )
                    for field in ("username", "password")
                },
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
            command=["python", "-m", "corridor.deployment_bootstrap", "configure"],
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
                # Worker trouble belongs in the operational health report;
                # it must not make the coordinator UI unreachable too.
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

        if web_desired_count > 0 and worker_desired_count == 0:
            raise ValueError(
                "corridor:workerDesiredCount must be positive while web serves "
                "traffic; preparation requests require the Due Work supervisor."
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
        # Disposition reads exact stack outputs; generic CDK tags do not bind
        # an application stack to a registered customer environment (#514).
        CfnOutput(self, "DispositionCustomerId", value=customer_id)
        CfnOutput(self, "DispositionEnvironmentId", value=customer_environment_id)
        CfnOutput(self, "DispositionDeploymentId", value=deployment_id)
        CfnOutput(self, "DispositionAlbLogsBucket", value=alb_logs.bucket_name)
        CfnOutput(self, "WebServiceName", value=self.web_service.service_name)
        CfnOutput(self, "WorkerServiceName", value=self.worker_service.service_name)
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
        health_check: ecs.HealthCheck | None = None,
        stop_timeout: Duration | None = None,
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
            health_check=health_check,
            stop_timeout=stop_timeout,
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
