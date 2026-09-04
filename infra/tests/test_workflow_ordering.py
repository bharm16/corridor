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
    "workflow", ["infra-nonproduction.yml", "app-release.yml"]
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
    "workflow", ["infra-nonproduction.yml", "app-release.yml"]
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
    for job_name, job in _jobs("infra-nonproduction.yml").items():
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
        job = _jobs("infra-nonproduction.yml")[job_name]
        commands = " ".join(str(step.get("run", "")) for step in job["steps"])
        for context in required:
            assert context in commands, f"{job_name} does not pass {context}"


def test_both_dispatch_jobs_validate_before_authenticating():
    for job_name in ("diff", "deploy"):
        steps = _jobs("infra-nonproduction.yml")[job_name]["steps"]
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
