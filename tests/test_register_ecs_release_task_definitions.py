"""The release helper that makes a release actually release the built image.

The failure this guards against is quiet: pushing a tag to ECR does not change
an existing task definition, so a release could push the right image, run the
migration, restart the service, report success, and have run the previous
commit throughout. Nothing in the workflow output would say so.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "register_release",
    pathlib.Path(__file__).parents[1]
    / "scripts"
    / "register_ecs_release_task_definitions.py",
)
release = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(release)


REPO = "810100779593.dkr.ecr.us-east-2.amazonaws.com/corridor"
DIGEST = "sha256:" + "ab" * 32


def _described(role: str, image: str) -> dict:
    """A DescribeTaskDefinition response, including the response-only fields
    that RegisterTaskDefinition rejects."""
    return {
        "taskDefinition": {
            "taskDefinitionArn": f"arn:aws:ecs:us-east-2:810100779593:task-definition/{role}:7",
            "revision": 7,
            "status": "ACTIVE",
            "registeredAt": "2026-09-04T00:00:00Z",
            "registeredBy": "arn:aws:iam::810100779593:role/whoever",
            "requiresAttributes": [{"name": "ecs.capability.execution-role-awslogs"}],
            "compatibilities": ["EC2", "FARGATE"],
            "family": f"corridor-{role}",
            "cpu": "1024",
            "memory": "2048",
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "executionRoleArn": f"arn:aws:iam::810100779593:role/corridor/nonproduction/{role}-exec",
            "taskRoleArn": f"arn:aws:iam::810100779593:role/corridor/nonproduction/{role}-task",
            "runtimePlatform": {
                "cpuArchitecture": "X86_64",
                "operatingSystemFamily": "LINUX",
            },
            "containerDefinitions": [
                {
                    "name": f"{role}Container",
                    "image": image,
                    "command": ["alembic", "upgrade", "head"],
                    "secrets": [{"name": "CORRIDOR_WEB_DB_PASSWORD", "valueFrom": "arn:secret"}],
                    "environment": [{"name": "CORRIDOR_TASK_ROLE", "value": role}],
                    "logConfiguration": {"logDriver": "awslogs"},
                }
            ],
        }
    }


class FakeEcs:
    def __init__(self, images: dict[str, str] | None = None):
        self.registered: list[dict] = []
        self._images = images or {}

    def describe_task_definition(self, taskDefinition: str):  # noqa: N803
        role = taskDefinition.rsplit("/", 1)[-1].split(":")[0]
        return _described(role, self._images.get(role, f"{REPO}:oldsha"))

    def register_task_definition(self, **kwargs):
        self.registered.append(kwargs)
        family = kwargs["family"]
        return {
            "taskDefinition": {
                "taskDefinitionArn": f"arn:aws:ecs:us-east-2:810100779593:task-definition/{family}:8"
            }
        }


BASE = {role: f"arn:aws:ecs:us-east-2:810100779593:task-definition/{role}:7"
        for role in ("web", "migration", "batch")}


def test_every_role_is_repointed_at_the_released_digest():
    client = FakeEcs()

    released = release.register(
        client, base_arns=BASE, repository_uri=REPO, digest=DIGEST, tags={}
    )

    assert set(released) == {"web", "migration", "batch"}
    assert len(client.registered) == 3
    for registered in client.registered:
        images = [c["image"] for c in registered["containerDefinitions"]]
        assert images == [f"{REPO}@{DIGEST}"], images


def test_the_digest_is_used_rather_than_the_tag():
    """A tag is a mutable pointer: two tasks started seconds apart can resolve
    it to different images."""
    client = FakeEcs()

    release.register(
        client, base_arns=BASE, repository_uri=REPO, digest=DIGEST, tags={}
    )

    for registered in client.registered:
        for container in registered["containerDefinitions"]:
            assert "@sha256:" in container["image"]
            assert not container["image"].endswith(":oldsha")


def test_response_only_fields_are_stripped():
    """Copying a DescribeTaskDefinition response straight back is the obvious
    implementation and RegisterTaskDefinition rejects it."""
    client = FakeEcs()

    release.register(
        client, base_arns=BASE, repository_uri=REPO, digest=DIGEST, tags={}
    )

    for registered in client.registered:
        for field in release.RESPONSE_ONLY_FIELDS:
            assert field not in registered, field


def test_everything_cloudformation_defined_is_carried_forward():
    """Only the image changes. Roles, secrets and platform stay whatever the
    stack says, so a release cannot quietly re-scope a task."""
    client = FakeEcs()

    release.register(
        client, base_arns=BASE, repository_uri=REPO, digest=DIGEST, tags={}
    )

    web = next(r for r in client.registered if r["family"] == "corridor-web")
    assert web["cpu"] == "1024"
    assert web["memory"] == "2048"
    assert web["networkMode"] == "awsvpc"
    assert "nonproduction/web-exec" in web["executionRoleArn"]
    assert "nonproduction/web-task" in web["taskRoleArn"]
    assert web["containerDefinitions"][0]["secrets"] == [
        {"name": "CORRIDOR_WEB_DB_PASSWORD", "valueFrom": "arn:secret"}
    ]
    assert web["runtimePlatform"]["cpuArchitecture"] == "X86_64"


def test_the_release_is_tagged_with_its_commit_and_digest():
    client = FakeEcs()

    release.register(
        client,
        base_arns=BASE,
        repository_uri=REPO,
        digest=DIGEST,
        tags={"CommitSha": "a" * 40, "ImageDigest": DIGEST},
    )

    for registered in client.registered:
        tags = {tag["key"]: tag["value"] for tag in registered["tags"]}
        assert tags["CommitSha"] == "a" * 40
        assert tags["ImageDigest"] == DIGEST


def test_a_tag_shaped_digest_is_refused():
    with pytest.raises(release.ReleaseError, match="expected an image digest"):
        release.digest_reference(REPO, "latest")


def test_a_container_from_another_registry_is_left_alone():
    """Corridor has no sidecar today, but adding one must not silently repoint
    it at Corridor's image."""
    definition = _described("web", f"{REPO}:oldsha")["taskDefinition"]
    definition["containerDefinitions"].append(
        {"name": "sidecar", "image": "public.ecr.aws/somebody/agent:1.2.3"}
    )

    prepared = release.redefine(
        definition, repository_uri=REPO, image=f"{REPO}@{DIGEST}"
    )

    images = {c["name"]: c["image"] for c in prepared["containerDefinitions"]}
    assert images["webContainer"] == f"{REPO}@{DIGEST}"
    assert images["sidecar"] == "public.ecr.aws/somebody/agent:1.2.3"


def test_a_definition_referencing_no_corridor_image_is_refused():
    """Registering a revision that would run an unrelated image is worse than
    failing the release."""
    definition = _described("web", "public.ecr.aws/somebody/other:1")["taskDefinition"]

    with pytest.raises(release.ReleaseError, match="no container referenced"):
        release.redefine(definition, repository_uri=REPO, image=f"{REPO}@{DIGEST}")


def test_a_missing_base_task_definition_stops_the_release():
    client = FakeEcs()
    incomplete = dict(BASE)
    del incomplete["batch"]

    with pytest.raises(release.ReleaseError, match="no base task definition"):
        release.register(
            client,
            base_arns=incomplete,
            repository_uri=REPO,
            digest=DIGEST,
            tags={},
        )
