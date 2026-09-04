"""No dependency is installed or resolved once AWS credentials exist.

A `pip install` or `npm install` after `configure-aws-credentials` runs
unreviewed bytes while a role that can deploy stacks, push images and run the
migration is active. The lockfiles make *what* is installed reproducible; this
asserts *when*.

Checked structurally rather than by reading the YAML by eye, because the
ordering is easy to break with an innocuous-looking edit.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

WORKFLOWS = pathlib.Path(__file__).parents[2] / ".github" / "workflows"

# Anything that resolves or fetches a dependency.
INSTALLERS = (
    "pip install",
    "npm install",
    "npm ci",
    "uv sync",
    "uv lock",
    "pipx install",
)

CREDENTIAL_ACTION = "aws-actions/configure-aws-credentials"


def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


def _step_text(step: dict) -> str:
    return " ".join(str(step.get(key, "")) for key in ("uses", "run", "name"))


@pytest.mark.parametrize(
    "workflow", ["infra-deploy.yml", "app-release.yml"]
)
def test_nothing_installs_after_credentials_are_configured(workflow):
    for job_name, job in _jobs(workflow).items():
        steps = job.get("steps") or []
        credential_at = next(
            (
                index
                for index, step in enumerate(steps)
                if CREDENTIAL_ACTION in str(step.get("uses", ""))
            ),
            None,
        )
        if credential_at is None:
            continue
        for index, step in enumerate(steps[credential_at + 1 :], credential_at + 1):
            text = _step_text(step)
            for installer in INSTALLERS:
                assert installer not in text, (
                    f"{workflow}:{job_name} step {index} runs '{installer}' "
                    "after AWS credentials are configured"
                )


@pytest.mark.parametrize(
    "workflow", ["infra-deploy.yml", "app-release.yml"]
)
def test_every_credentialed_job_installs_before_it_authenticates(workflow):
    """The converse: a job that assumes a role must already have its
    toolchain, or it will be tempted to install one afterwards."""
    for job_name, job in _jobs(workflow).items():
        steps = job.get("steps") or []
        credential_at = next(
            (
                index
                for index, step in enumerate(steps)
                if CREDENTIAL_ACTION in str(step.get("uses", ""))
            ),
            None,
        )
        if credential_at is None:
            continue
        before = " ".join(_step_text(step) for step in steps[:credential_at])
        assert any(installer in before for installer in INSTALLERS), (
            f"{workflow}:{job_name} authenticates without having installed "
            "its toolchain first"
        )


def test_the_pull_request_jobs_never_authenticate():
    """`plan` and `image` create nothing and must hold no id-token."""
    for job_name, job in _jobs("infra-deploy.yml").items():
        if job_name not in ("plan", "image"):
            continue
        permissions = job.get("permissions") or {}
        assert "id-token" not in permissions, job_name
        for step in job.get("steps") or []:
            assert CREDENTIAL_ACTION not in str(step.get("uses", "")), job_name


def test_both_dispatch_jobs_pass_every_required_context():
    """A deploy missing these synthesises a stack with no listener, no
    application URL and an empty sender: it succeeds and is unusable."""
    required = (
        "corridor:certificateArn",
        "corridor:publicHostname",
        "corridor:signInSender",
        "corridor:imageTag",
    )
    for job_name in ("diff", "deploy"):
        job = _jobs("infra-deploy.yml")[job_name]
        commands = " ".join(str(step.get("run", "")) for step in job["steps"])
        for context in required:
            assert context in commands, f"{job_name} does not pass {context}"


def test_both_dispatch_jobs_validate_before_authenticating():
    for job_name in ("diff", "deploy"):
        steps = _jobs("infra-deploy.yml")[job_name]["steps"]
        validate_at = next(
            index
            for index, step in enumerate(steps)
            if "validate-deployment-config" in _step_text(step)
        )
        credential_at = next(
            index
            for index, step in enumerate(steps)
            if CREDENTIAL_ACTION in str(step.get("uses", ""))
        )
        assert validate_at < credential_at, job_name


def test_every_external_image_is_pinned_by_digest():
    """A tag is a mutable pointer, and these bytes run with the database
    credential and the task roles.

    Covers `FROM` as well as `COPY --from`: an earlier version of this test
    checked only the latter, so it passed while the base image itself was
    still on a floating tag.
    """
    import re

    dockerfile = (pathlib.Path(__file__).parents[2] / "Dockerfile").read_text()

    references = [
        match
        for match in re.findall(r"^FROM\s+(\S+)", dockerfile, re.M)
        # A later stage may build on an earlier one by name; only external
        # references need a digest.
        if "/" in match or ":" in match
    ]
    references += re.findall(r"^COPY --from=(\S+)", dockerfile, re.M)

    assert references, "no external image references found"
    unpinned = [
        reference for reference in references if "@sha256:" not in reference
    ]
    assert not unpinned, f"pinned by tag rather than digest: {unpinned}"


@pytest.mark.parametrize(
    "workflow", ["infra-deploy.yml", "app-release.yml"]
)
def test_no_credentialed_job_runs_from_an_unreviewed_ref(workflow):
    """`workflow_dispatch` runs the *selected ref's* YAML, so a job that
    assumes a role hands that role to whatever the branch says. Checking out a
    reviewed commit inside the job does not help: the steps around the checkout
    are the branch's own.

    A read-only-sounding job is not exempt. `corridor-nonprod-cdk-deploy` can
    assume the CDK deploy bootstrap role, so a `diff` dispatch from a branch
    could call deployment APIs directly instead of the advertised command.
    """
    for job_name, job in _jobs(workflow).items():
        steps = job.get("steps") or []
        if not any(
            CREDENTIAL_ACTION in str(step.get("uses", "")) for step in steps
        ):
            continue
        condition = str(job.get("if", ""))
        assert "refs/heads/main" in condition, (
            f"{workflow}:{job_name} assumes an AWS role without a main-only "
            "guard, so an unreviewed branch's YAML can use it"
        )


def test_the_runbook_names_the_setting_the_ref_guard_cannot_enforce():
    """The `github.ref` guard lives in the workflow file, which the requester
    controls: a branch can delete it, and after environment approval its OIDC
    token still carries the trusted `...:environment:nonproduction` subject.
    Both role trusts check only that subject, so AWS never learns which ref
    supplied the YAML.

    The actual boundary is the environment's deployment-branch policy, which
    is a GitHub setting rather than anything in this repository. The one thing
    the repository can do is refuse to let that go undocumented.
    """
    runbook = (
        pathlib.Path(__file__).parents[2]
        / "docs"
        / "deployment"
        / "nonproduction-aws.md"
    ).read_text()

    assert "Deployment branches and tags" in runbook
    assert "Selected branches" in runbook
    assert "load-bearing" in runbook.lower()
    assert "Required reviewers" in runbook
    # And it must say why the in-repo guard is not enough.
    assert "defence in depth" in runbook or "defense in depth" in runbook
