"""Repoint Corridor's task definitions at one exact image digest.

Pushing a tag to ECR does not change an existing ECS task definition. A task
definition stores the image reference it was registered with, so a release that
pushes `corridor:<sha>` and then runs the task definition CloudFormation
created runs whatever image *that* was registered with -- the previous release.
The push succeeds, the migration succeeds, the service restarts, and none of it
is the commit that was asked for.

So a release registers a new revision of each task definition with the image
replaced by an immutable digest reference, and runs those revisions. The digest
rather than the tag because a tag is a mutable pointer: two tasks started
seconds apart can otherwise resolve it differently.

Everything else in the definition is CloudFormation's. This copies it forward
untouched and changes exactly one field, so CPU, memory, roles, secrets,
environment, logging, networking and platform stay whatever the stack says they
are.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Iterable

# Present in a DescribeTaskDefinition response, rejected by
# RegisterTaskDefinition. Copying a response straight back is the obvious
# implementation and it fails with a parameter-validation error.
RESPONSE_ONLY_FIELDS = frozenset(
    {
        "compatibilities",
        "deregisteredAt",
        "registeredAt",
        "registeredBy",
        "requiresAttributes",
        "revision",
        "status",
        "taskDefinitionArn",
    }
)

ROLES = ("web", "migration", "batch")


class ReleaseError(RuntimeError):
    """The release cannot proceed and must not be reported as successful."""


def digest_reference(repository_uri: str, digest: str) -> str:
    """`<repo>@sha256:...`, the only form that names one exact image."""

    if not digest.startswith("sha256:"):
        raise ReleaseError(f"expected an image digest, got {digest!r}")
    return f"{repository_uri}@{digest}"


def redefine(
    task_definition: dict[str, Any], *, repository_uri: str, image: str
) -> dict[str, Any]:
    """One task definition, ready to register, pointing at `image`."""

    prepared = {
        key: value
        for key, value in task_definition.items()
        if key not in RESPONSE_ONLY_FIELDS
    }

    containers = prepared.get("containerDefinitions") or []
    if not containers:
        raise ReleaseError("task definition has no container definitions")

    repointed = 0
    for container in containers:
        current = container.get("image", "")
        # Only Corridor's own image moves. A sidecar from another registry --
        # there is none today, but adding one should not silently repoint it --
        # keeps whatever the stack gave it.
        if current.split("@")[0].split(":")[0] == repository_uri:
            container["image"] = image
            repointed += 1

    if repointed == 0:
        raise ReleaseError(
            f"no container referenced {repository_uri}; refusing to register a "
            "revision that would run an unrelated image"
        )
    return prepared


def register(
    client,
    *,
    base_arns: dict[str, str],
    repository_uri: str,
    digest: str,
    tags: dict[str, str],
) -> dict[str, str]:
    """Register one new revision per role. Returns role -> new ARN."""

    missing = [role for role in ROLES if not base_arns.get(role)]
    if missing:
        raise ReleaseError(f"no base task definition for: {', '.join(missing)}")

    image = digest_reference(repository_uri, digest)
    released: dict[str, str] = {}
    for role in ROLES:
        described = client.describe_task_definition(taskDefinition=base_arns[role])
        prepared = redefine(
            described["taskDefinition"], repository_uri=repository_uri, image=image
        )
        if tags:
            prepared["tags"] = [
                {"key": key, "value": value} for key, value in sorted(tags.items())
            ]
        response = client.register_task_definition(**prepared)
        released[role] = response["taskDefinition"]["taskDefinitionArn"]
    return released


def _parse(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-uri", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--commit-sha", required=True)
    for role in ROLES:
        parser.add_argument(f"--{role}-task-definition", required=True)
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:  # pragma: no cover - exercised in CI
    import boto3

    args = _parse(argv)
    released = register(
        boto3.client("ecs"),
        base_arns={
            role: getattr(args, f"{role}_task_definition") for role in ROLES
        },
        repository_uri=args.repository_uri,
        digest=args.digest,
        tags={
            "Application": "Corridor",
            "Environment": "nonproduction",
            "CommitSha": args.commit_sha,
            "ImageDigest": args.digest,
        },
    )
    json.dump(released, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main(sys.argv[1:]))
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        sys.exit(1)
