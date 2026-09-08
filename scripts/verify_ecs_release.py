"""Drain old Corridor tasks and prove the exact release ECS is running.

A stable service can be the previous deployment after circuit-breaker rollback.
Service counts alone also miss containers still stopping, and a missing image
digest proves nothing. The release uses these checks for both its web service
and its resident worker; the worker needs no HTTP endpoint.

Only the existing web/worker services and the managed Batch task family are
drained. The latter includes older ad hoc workers that a zero service count
cannot stop. No schema change may begin until their actual task status is
STOPPED, including tasks whose desired status was already STOPPED.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from typing import Any

TERMINAL_TASK_STATES = frozenset({"STOPPED", "DELETED"})
WAIT_CONFIG = {"Delay": 5, "MaxAttempts": 120}


class ReleaseVerificationError(RuntimeError):
    """ECS has not proved that the release can safely continue."""


def _service(client, *, cluster: str, name: str) -> dict[str, Any]:
    response = client.describe_services(cluster=cluster, services=[name])
    services = response.get("services") or []
    if response.get("failures") or len(services) != 1:
        raise ReleaseVerificationError(f"could not describe service {name}: {response}")
    service = services[0]
    if service.get("serviceName") != name or service.get("status") != "ACTIVE":
        raise ReleaseVerificationError(f"service {name} is missing or inactive")
    return service


def _task_arns(client, *, cluster: str, **filters: str) -> set[str]:
    arns = set()
    paginator = client.get_paginator("list_tasks")
    # Desired STOPPED can still mean actual STOPPING or DEACTIVATING. PENDING
    # is only an actual state; ECS never sets desiredStatus to PENDING.
    for status in ("RUNNING", "STOPPED"):
        for page in paginator.paginate(
            cluster=cluster, desiredStatus=status, **filters
        ):
            arns.update(page["taskArns"])
    return arns


def _tasks(client, *, cluster: str, arns: set[str]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    ordered = sorted(arns)
    for start in range(0, len(ordered), 100):
        batch = ordered[start:start + 100]
        response = client.describe_tasks(cluster=cluster, tasks=batch)
        described = response.get("tasks") or []
        if (
            response.get("failures")
            or len(described) != len(batch)
            or {task.get("taskArn") for task in described} != set(batch)
        ):
            raise ReleaseVerificationError(
                f"could not describe every listed task: {response}"
            )
        tasks.extend(described)
    return tasks


def verify_service(
    client,
    *,
    cluster: str,
    service_name: str,
    task_definition_arn: str,
    repository_uri: str,
    digest: str,
    desired_count: int,
) -> dict[str, Any]:
    """Require a completed deployment and healthy tasks on the exact image.

    Returns the observed task ARNs for the release receipt. At desired count
    zero the receipt proves the configured revision and absence of live tasks,
    not a running capability. Missing ECS evidence always refuses the release.
    """

    if desired_count not in (0, 1):
        raise ReleaseVerificationError("desired count must be 0 or 1")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ReleaseVerificationError("expected a complete sha256 image digest")
    image = f"{repository_uri}@{digest}"
    definition = client.describe_task_definition(
        taskDefinition=task_definition_arn
    )["taskDefinition"]
    if definition.get("taskDefinitionArn") != task_definition_arn:
        raise ReleaseVerificationError("described a different task definition")
    own_names = set()
    for container in definition.get("containerDefinitions") or []:
        reference = container.get("image", "")
        if (
            reference == repository_uri
            or reference.startswith(f"{repository_uri}:")
            or reference.startswith(f"{repository_uri}@")
        ):
            if reference != image or not container.get("name"):
                raise ReleaseVerificationError(
                    "task definition does not bind every Corridor container "
                    f"to {image}"
                )
            own_names.add(container["name"])
    if not own_names:
        raise ReleaseVerificationError("task definition has no Corridor container")

    service = _service(client, cluster=cluster, name=service_name)
    if service.get("taskDefinition") != task_definition_arn:
        raise ReleaseVerificationError(
            f"{service_name} is running {service.get('taskDefinition')}, "
            f"expected {task_definition_arn}: the deployment may have rolled back"
        )
    deployments = service.get("deployments") or []
    if len(deployments) != 1 or deployments[0].get("status") != "PRIMARY":
        raise ReleaseVerificationError("service has no single PRIMARY deployment")
    deployment = deployments[0]
    if deployment.get("taskDefinition") != task_definition_arn:
        raise ReleaseVerificationError("PRIMARY deployment has a different revision")
    # Both Corridor services use ECS rolling deployments, which report this
    # field. An absent rolloutState is not proof of completed rollout.
    if deployment.get("rolloutState") != "COMPLETED":
        raise ReleaseVerificationError(
            f"rollout is not COMPLETED: {deployment.get('rolloutState')} "
            f"({deployment.get('rolloutStateReason')})"
        )
    for label, state in (("service", service), ("deployment", deployment)):
        if (
            state.get("desiredCount") != desired_count
            or state.get("runningCount") != desired_count
            or state.get("pendingCount") != 0
        ):
            raise ReleaseVerificationError(
                f"{label} counts do not match {desired_count} running and none pending"
            )

    tasks = [
        task for task in _tasks(
            client, cluster=cluster,
            arns=_task_arns(client, cluster=cluster, serviceName=service_name),
        )
        if task.get("lastStatus") not in TERMINAL_TASK_STATES
    ]
    if len(tasks) != desired_count:
        raise ReleaseVerificationError(
            f"{len(tasks)} live tasks, expected {desired_count}"
        )
    for task in tasks:
        if task.get("taskDefinitionArn") != task_definition_arn:
            raise ReleaseVerificationError("running task has a different revision")
        if (
            task.get("lastStatus") != "RUNNING"
            or task.get("desiredStatus") != "RUNNING"
            or task.get("healthStatus") != "HEALTHY"
        ):
            raise ReleaseVerificationError("task is not RUNNING and HEALTHY")
        containers = [
            container for container in task.get("containers") or []
            if container.get("name") in own_names
        ]
        if (
            len(containers) != len(own_names)
            or {container["name"] for container in containers} != own_names
        ):
            raise ReleaseVerificationError("running task is missing a Corridor container")
        for container in containers:
            if container.get("image") != image or container.get("imageDigest") != digest:
                raise ReleaseVerificationError(
                    f"{container['name']} does not prove the released image digest {digest}"
                )
            if (
                container.get("lastStatus") != "RUNNING"
                or container.get("healthStatus") != "HEALTHY"
            ):
                raise ReleaseVerificationError(
                    f"{container['name']} is not RUNNING and HEALTHY"
                )
    return {
        "service": service_name,
        "task_definition": task_definition_arn,
        "digest": digest,
        "desired_count": desired_count,
        "task_arns": sorted(task["taskArn"] for task in tasks),
    }


def drain_services(
    client,
    *,
    cluster: str,
    web_service: str,
    worker_service: str,
    worker_task_definition_arn: str,
) -> dict[str, Any]:
    """Stop both services and every live task in the managed worker family.

    Read both services before changing either, then stop replacement scheduling
    before stopping tasks. The workflow's shared environment concurrency keeps
    its other deployments out of this maintenance window; an unexpected task
    appearing during the final recheck refuses migration.
    """

    if web_service == worker_service:
        raise ReleaseVerificationError("web and worker must be distinct services")
    family = worker_task_definition_arn.rsplit("/", 1)[-1].rsplit(":", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", family):
        raise ReleaseVerificationError("worker task definition has no valid family")
    names = (web_service, worker_service)
    previous = {
        name: _service(client, cluster=cluster, name=name)["desiredCount"]
        for name in names
    }

    def managed_arns() -> set[str]:
        arns = _task_arns(client, cluster=cluster, family=family)
        for name in names:
            arns.update(_task_arns(client, cluster=cluster, serviceName=name))
        return arns

    arns = managed_arns()
    for name in names:
        client.update_service(cluster=cluster, service=name, desiredCount=0)
    client.get_waiter("services_stable").wait(
        cluster=cluster, services=list(names), WaiterConfig=WAIT_CONFIG,
    )
    arns.update(managed_arns())
    live = [
        task for task in _tasks(client, cluster=cluster, arns=arns)
        if task.get("lastStatus") not in TERMINAL_TASK_STATES
    ]
    for task in live:
        if task.get("desiredStatus") != "STOPPED":
            client.stop_task(
                cluster=cluster, task=task["taskArn"],
                reason="Corridor maintenance-window release before schema migration",
            )
    stopped = sorted(task["taskArn"] for task in live)
    for start in range(0, len(stopped), 100):
        client.get_waiter("tasks_stopped").wait(
            cluster=cluster, tasks=stopped[start:start + 100], WaiterConfig=WAIT_CONFIG,
        )
    arns.update(managed_arns())
    remaining = [
        task["taskArn"] for task in _tasks(client, cluster=cluster, arns=arns)
        if task.get("lastStatus") not in TERMINAL_TASK_STATES
    ]
    if remaining:
        raise ReleaseVerificationError(f"managed tasks are still live: {remaining}")
    for name in names:
        service = _service(client, cluster=cluster, name=name)
        if any(service.get(field) != 0 for field in (
            "desiredCount", "runningCount", "pendingCount"
        )):
            raise ReleaseVerificationError(f"{name} did not remain drained")
    return {"previous_desired_counts": previous, "stopped_task_arns": stopped}


def main(argv: Iterable[str] | None = None) -> int:
    """Run the release check with the workflow's existing ECS identity."""

    import boto3

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--cluster", required=True)
    verify.add_argument("--service", required=True)
    verify.add_argument("--task-definition", required=True)
    verify.add_argument("--repository-uri", required=True)
    verify.add_argument("--digest", required=True)
    verify.add_argument("--desired-count", required=True, type=int, choices=(0, 1))
    drain = commands.add_parser("drain")
    drain.add_argument("--cluster", required=True)
    drain.add_argument("--web-service", required=True)
    drain.add_argument("--worker-service", required=True)
    drain.add_argument("--worker-task-definition", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    client = boto3.client("ecs")
    try:
        if args.command == "drain":
            result = drain_services(
                client, cluster=args.cluster, web_service=args.web_service,
                worker_service=args.worker_service,
                worker_task_definition_arn=args.worker_task_definition,
            )
        else:
            result = verify_service(
                client, cluster=args.cluster, service_name=args.service,
                task_definition_arn=args.task_definition,
                repository_uri=args.repository_uri, digest=args.digest,
                desired_count=args.desired_count,
            )
    except ReleaseVerificationError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
