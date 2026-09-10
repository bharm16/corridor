"""A healthy old deployment or an unproved digest cannot pass a release.

The ECS API is the public boundary: fixtures represent its service, task, and
task-definition responses without starting AWS resources or a database.
"""

from __future__ import annotations

import copy

import pytest

from scripts import verify_ecs_release as release

REPO = "810100779593.dkr.ecr.us-east-2.amazonaws.com/corridor"
DIGEST = "sha256:" + "ab" * 32
IMAGE = f"{REPO}@{DIGEST}"
CLUSTER = "corridor-nonprod"
PREFIX = "arn:aws:ecs:us-east-2:810100779593:"
DEFINITIONS = {
    "web": f"{PREFIX}task-definition/corridor-web:8",
    "worker": f"{PREFIX}task-definition/corridor-batch:8",
}


def _service(role: str, desired: int = 1) -> dict:
    return {
        "serviceName": f"corridor-{role}",
        "status": "ACTIVE",
        "taskDefinition": DEFINITIONS[role],
        "desiredCount": desired,
        "runningCount": desired,
        "pendingCount": 0,
        "deployments": [{
            "status": "PRIMARY",
            "taskDefinition": DEFINITIONS[role],
            "rolloutState": "COMPLETED",
            "desiredCount": desired,
            "runningCount": desired,
            "pendingCount": 0,
        }],
    }


def _task(role: str) -> dict:
    return {
        "taskArn": f"{PREFIX}task/{CLUSTER}/{role}",
        "taskDefinitionArn": DEFINITIONS[role],
        "group": f"service:corridor-{role}",
        "desiredStatus": "RUNNING",
        "lastStatus": "RUNNING",
        "healthStatus": "HEALTHY",
        "containers": [{
            "name": f"{role}Container",
            "image": IMAGE,
            "imageDigest": DIGEST,
            "lastStatus": "RUNNING",
            "healthStatus": "HEALTHY",
        }],
    }


class FakeEcs:
    def __init__(self, desired: int = 1):
        self.services = {
            f"corridor-{role}": _service(role, desired)
            for role in DEFINITIONS
        }
        self.tasks = {
            task["taskArn"]: task
            for role in DEFINITIONS
            for task in ([_task(role)] if desired else [])
        }
        self.definitions = {
            arn: {
                "taskDefinitionArn": arn,
                "containerDefinitions": [{
                    "name": f"{role}Container",
                    "image": IMAGE,
                }],
            }
            for role, arn in DEFINITIONS.items()
        }
        self.describe_failures: list[dict] = []

    def describe_services(self, *, cluster, services):
        assert cluster == CLUSTER
        return {
            "services": [copy.deepcopy(self.services[name]) for name in services],
            "failures": [],
        }

    def describe_task_definition(self, *, taskDefinition):  # noqa: N803
        return {"taskDefinition": copy.deepcopy(self.definitions[taskDefinition])}

    def get_paginator(self, operation):
        assert operation == "list_tasks"
        return self

    def paginate(self, *, cluster, desiredStatus, serviceName=None, family=None):  # noqa: N803
        assert cluster == CLUSTER
        arns = [
            arn for arn, task in self.tasks.items()
            if task["desiredStatus"] == desiredStatus
            and (serviceName is None or task["group"] == f"service:{serviceName}")
            and (family is None or task["taskDefinitionArn"].rsplit("/", 1)[-1]
                 .rsplit(":", 1)[0] == family)
        ]
        # Real pagination matters even though the deployed service is bounded.
        return [{"taskArns": [arn]} for arn in arns] or [{"taskArns": []}]

    def describe_tasks(self, *, cluster, tasks):
        assert cluster == CLUSTER
        return {
            "tasks": [copy.deepcopy(self.tasks[arn]) for arn in tasks],
            "failures": self.describe_failures,
        }


@pytest.mark.parametrize("role", ["web", "worker"])
def test_release_proves_its_actual_service_revision_and_own_image(role):
    client = FakeEcs()
    # A sidecar has a different digest. Only Corridor's named containers move
    # when the release definition is registered.
    definition = client.definitions[DEFINITIONS[role]]
    definition["containerDefinitions"].append({
        "name": "sidecar", "image": "public.ecr.aws/other/agent:1",
    })
    client.tasks[_task(role)["taskArn"]]["containers"].append({
        "name": "sidecar", "imageDigest": "sha256:" + "cd" * 32,
    })

    result = release.verify_service(
        client, cluster=CLUSTER, service_name=f"corridor-{role}",
        task_definition_arn=DEFINITIONS[role], repository_uri=REPO,
        digest=DIGEST, desired_count=1,
    )

    assert result == {
        "service": f"corridor-{role}",
        "task_definition": DEFINITIONS[role],
        "digest": DIGEST,
        "desired_count": 1,
        "task_arns": [f"{PREFIX}task/{CLUSTER}/{role}"],
    }


def _verify_worker(client, desired=1):
    return release.verify_service(
        client, cluster=CLUSTER, service_name="corridor-worker",
        task_definition_arn=DEFINITIONS["worker"], repository_uri=REPO,
        digest=DIGEST, desired_count=desired,
    )


@pytest.mark.parametrize("digest", [None, "", "sha256:" + "cd" * 32])
def test_missing_or_wrong_runtime_digest_refuses_the_release(digest):
    client = FakeEcs()
    client.tasks[_task("worker")["taskArn"]]["containers"][0]["imageDigest"] = digest

    with pytest.raises(release.ReleaseVerificationError, match="released image digest"):
        _verify_worker(client)


def test_a_sidecar_digest_cannot_stand_in_for_a_missing_corridor_container():
    client = FakeEcs()
    client.tasks[_task("worker")["taskArn"]]["containers"][0]["name"] = "sidecar"

    with pytest.raises(release.ReleaseVerificationError, match="missing a Corridor"):
        _verify_worker(client)


def test_every_corridor_container_must_be_present_and_prove_its_digest():
    client = FakeEcs()
    client.definitions[DEFINITIONS["worker"]]["containerDefinitions"].append({
        "name": "secondCorridorContainer", "image": IMAGE,
    })
    containers = client.tasks[_task("worker")["taskArn"]]["containers"]
    containers.append({
        **containers[0], "name": "secondCorridorContainer", "imageDigest": None,
    })

    with pytest.raises(release.ReleaseVerificationError, match="released image digest"):
        _verify_worker(client)


@pytest.mark.parametrize("reference", [f"{REPO}:oldsha", "public.ecr.aws/other/app:1"])
def test_the_registered_revision_must_bind_the_corridor_image(reference):
    client = FakeEcs()
    client.definitions[DEFINITIONS["worker"]]["containerDefinitions"][0]["image"] = reference

    with pytest.raises(release.ReleaseVerificationError, match="Corridor container"):
        _verify_worker(client)


@pytest.mark.parametrize("where", ["service", "deployment", "task"])
def test_a_healthy_old_revision_cannot_pass_after_rollback(where):
    client = FakeEcs()
    old = f"{PREFIX}task-definition/corridor-batch:7"
    if where == "service":
        client.services["corridor-worker"]["taskDefinition"] = old
    elif where == "deployment":
        client.services["corridor-worker"]["deployments"][0]["taskDefinition"] = old
    else:
        client.tasks[_task("worker")["taskArn"]]["taskDefinitionArn"] = old

    with pytest.raises(release.ReleaseVerificationError, match="rolled back|different revision"):
        _verify_worker(client)


@pytest.mark.parametrize("state", [None, "IN_PROGRESS", "FAILED"])
def test_an_uncompleted_deployment_is_not_a_successful_release(state):
    client = FakeEcs()
    client.services["corridor-worker"]["deployments"][0]["rolloutState"] = state

    with pytest.raises(release.ReleaseVerificationError, match="not COMPLETED"):
        _verify_worker(client)


def test_a_second_deployment_prevents_reporting_a_completed_rollout():
    client = FakeEcs()
    deployments = client.services["corridor-worker"]["deployments"]
    deployments.append({**deployments[0], "status": "ACTIVE"})

    with pytest.raises(release.ReleaseVerificationError, match="single PRIMARY"):
        _verify_worker(client)


@pytest.mark.parametrize("where", ["task", "container"])
@pytest.mark.parametrize("health", [None, "UNKNOWN", "UNHEALTHY"])
def test_the_worker_requires_actual_container_health_without_an_http_endpoint(where, health):
    client = FakeEcs()
    task = client.tasks[_task("worker")["taskArn"]]
    (task if where == "task" else task["containers"][0])["healthStatus"] = health

    with pytest.raises(release.ReleaseVerificationError, match="not RUNNING and HEALTHY"):
        _verify_worker(client)


@pytest.mark.parametrize("where", ["service", "deployment"])
def test_reported_pending_work_prevents_release(where):
    client = FakeEcs()
    service = client.services["corridor-worker"]
    (service if where == "service" else service["deployments"][0])["pendingCount"] = 1

    with pytest.raises(release.ReleaseVerificationError, match="counts do not match"):
        _verify_worker(client)


def test_a_reported_running_count_does_not_replace_task_evidence():
    client = FakeEcs()
    del client.tasks[_task("worker")["taskArn"]]

    with pytest.raises(release.ReleaseVerificationError, match="0 live tasks, expected 1"):
        _verify_worker(client)


def test_describe_task_failures_refuse_the_release():
    client = FakeEcs()
    client.describe_failures = [{"arn": _task("worker")["taskArn"], "reason": "MISSING"}]

    with pytest.raises(release.ReleaseVerificationError, match="every listed task"):
        _verify_worker(client)


def test_zero_desired_count_reports_configuration_without_claiming_a_running_worker():
    client = FakeEcs(desired=0)
    historical = _task("worker")
    historical.update(desiredStatus="STOPPED", lastStatus="STOPPED")
    client.tasks[historical["taskArn"]] = historical

    result = _verify_worker(client, desired=0)

    assert result["desired_count"] == 0
    assert result["task_arns"] == []
    assert result["task_definition"] == DEFINITIONS["worker"]


def test_a_stopping_task_is_still_live_despite_zero_service_counts():
    client = FakeEcs(desired=0)
    stopping = _task("worker")
    stopping.update(desiredStatus="STOPPED", lastStatus="STOPPING")
    client.tasks[stopping["taskArn"]] = stopping

    with pytest.raises(release.ReleaseVerificationError, match="1 live tasks, expected 0"):
        _verify_worker(client, desired=0)


class DrainingEcs(FakeEcs):
    def __init__(self):
        super().__init__()
        self.leave_stopping = False
        self.restart_task: dict | None = None

    def update_service(self, *, cluster, service, desiredCount):  # noqa: N803
        assert cluster == CLUSTER
        self.services[service]["desiredCount"] = desiredCount
        for task in self.tasks.values():
            if task["group"] == f"service:{service}" and task["lastStatus"] != "STOPPED":
                task.update(desiredStatus="STOPPED", lastStatus="STOPPING")

    def stop_task(self, *, cluster, task, reason):
        assert cluster == CLUSTER
        # ECS may replace tasks while a service still desires them. A safe
        # drain must disable both schedulers before explicitly stopping tasks.
        assert all(service["desiredCount"] == 0 for service in self.services.values())
        self.tasks[task].update(desiredStatus="STOPPED", lastStatus="STOPPING")

    def get_waiter(self, operation):
        return DrainWaiter(self, operation)


class DrainWaiter:
    def __init__(self, client, operation):
        self.client = client
        self.operation = operation

    def wait(self, *, cluster, WaiterConfig, services=None, tasks=None):  # noqa: N803
        assert cluster == CLUSTER
        if self.operation == "services_stable":
            for name in services:
                self.client.services[name].update(runningCount=0, pendingCount=0)
        elif self.operation == "tasks_stopped":
            if not self.client.leave_stopping:
                for arn in tasks:
                    assert self.client.tasks[arn]["desiredStatus"] == "STOPPED"
                    self.client.tasks[arn]["lastStatus"] = "STOPPED"
            if self.client.restart_task:
                new = self.client.restart_task
                self.client.tasks[new["taskArn"]] = new
        else:
            raise AssertionError(self.operation)


def _drain(client):
    return release.drain_services(
        client, cluster=CLUSTER, web_service="corridor-web",
        worker_service="corridor-worker",
        worker_task_definition_arn=DEFINITIONS["worker"],
    )


def test_drain_stops_both_services_and_older_ad_hoc_workers_before_migration():
    client = DrainingEcs()
    ad_hoc = _task("worker")
    ad_hoc.update(
        taskArn=f"{PREFIX}task/{CLUSTER}/old-worker",
        taskDefinitionArn=f"{PREFIX}task-definition/corridor-batch:7",
        group="family:corridor-batch",
    )
    already_stopping = _task("worker")
    already_stopping.update(
        taskArn=f"{PREFIX}task/{CLUSTER}/stopping-worker",
        group="family:corridor-batch", desiredStatus="STOPPED", lastStatus="STOPPING",
    )
    unrelated = _task("worker")
    unrelated.update(
        taskArn=f"{PREFIX}task/{CLUSTER}/unrelated",
        taskDefinitionArn=f"{PREFIX}task-definition/other:1", group="family:other",
    )
    client.tasks.update({task["taskArn"]: task for task in (
        ad_hoc, already_stopping, unrelated,
    )})

    result = _drain(client)

    assert result["previous_desired_counts"] == {"corridor-web": 1, "corridor-worker": 1}
    assert set(result["stopped_task_arns"]) == {
        f"{PREFIX}task/{CLUSTER}/web", f"{PREFIX}task/{CLUSTER}/worker",
        f"{PREFIX}task/{CLUSTER}/old-worker", f"{PREFIX}task/{CLUSTER}/stopping-worker",
    }
    for arn in result["stopped_task_arns"]:
        assert client.tasks[arn]["lastStatus"] == "STOPPED"
    assert client.tasks[unrelated["taskArn"]]["lastStatus"] == "RUNNING"
    assert all(service["desiredCount"] == 0 for service in client.services.values())


def test_drain_refuses_migration_when_a_worker_is_only_desired_stopped():
    client = DrainingEcs()
    client.leave_stopping = True

    with pytest.raises(release.ReleaseVerificationError, match="managed tasks are still live"):
        _drain(client)


def test_drain_refuses_a_concurrent_worker_restart_in_the_final_check():
    client = DrainingEcs()
    new = _task("worker")
    new.update(taskArn=f"{PREFIX}task/{CLUSTER}/new-worker", group="family:corridor-batch")
    client.restart_task = new

    with pytest.raises(release.ReleaseVerificationError, match="managed tasks are still live"):
        _drain(client)


def test_drain_validates_both_services_before_stopping_either():
    client = DrainingEcs()
    client.services["corridor-worker"]["status"] = "INACTIVE"

    with pytest.raises(release.ReleaseVerificationError, match="missing or inactive"):
        _drain(client)

    assert client.services["corridor-web"]["desiredCount"] == 1
