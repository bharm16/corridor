"""Observe every ECS task before a release or export calls a cluster drained.

Disposition once listed RUNNING/PENDING desired states, missing STOPPING tasks
whose desired state was STOPPED. Release already knew that distinction. Both
now use this dependency-free module; callers keep their own mutation authority.
"""
from __future__ import annotations
import re
from typing import Any

TERMINAL_TASK_STATES = frozenset({"STOPPED", "DELETED"})


class TaskObservationError(RuntimeError):
    """An incomplete task census cannot prove stopped writers."""


def task_arns(client, *, cluster: str, **filters: str) -> set[str]:
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


def describe_tasks(client, *, cluster: str, arns: set[str]) -> list[dict[str, Any]]:
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
            raise TaskObservationError(
                f"could not describe every listed task: {response}"
            )
        tasks.extend(described)
    return tasks



def live_tasks(client, *, cluster: str, **filters: str) -> list[dict[str, Any]]:
    """Return every nonterminal task; missing state is nonterminal."""
    return [task for task in describe_tasks(client, cluster=cluster,
                arns=task_arns(client, cluster=cluster, **filters))
            if task.get("lastStatus") not in TERMINAL_TASK_STATES]


def verify_task_definition(client, *, task_definition_arn: str, repository_uri: str, digest: str) -> set[str]:
    """Prove every Corridor container in a definition names the released image."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise TaskObservationError("expected a complete sha256 image digest")
    image = f"{repository_uri}@{digest}"
    definition = client.describe_task_definition(
        taskDefinition=task_definition_arn
    )["taskDefinition"]
    if definition.get("taskDefinitionArn") != task_definition_arn:
        raise TaskObservationError("described a different task definition")
    own_names = set()
    for container in definition.get("containerDefinitions") or []:
        reference = container.get("image", "")
        if (
            reference == repository_uri
            or reference.startswith(f"{repository_uri}:")
            or reference.startswith(f"{repository_uri}@")
        ):
            if reference != image or not container.get("name"):
                raise TaskObservationError(
                    "task definition does not bind every Corridor container "
                    f"to {image}"
                )
            own_names.add(container["name"])
    if not own_names:
        raise TaskObservationError("task definition has no Corridor container")

    return own_names
