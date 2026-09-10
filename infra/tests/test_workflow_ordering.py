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
import subprocess

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

# Every workflow in the repository, so a new one is covered the day it lands
# rather than the day someone remembers to add it to a list. GitHub reads both
# suffixes; collecting only one would leave the same silent hole as a job-name
# list. An empty list would parametrize the credential guard into nothing, so
# it fails collection rather than reporting a sweep it never ran.
WORKFLOW_FILES = sorted(
    path.name for path in WORKFLOWS.iterdir() if path.suffix in (".yml", ".yaml")
)
assert WORKFLOW_FILES, f"no workflows found under {WORKFLOWS}"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _jobs(name: str) -> dict:
    return _workflow(name)["jobs"]


def _step_text(step: dict) -> str:
    return " ".join(str(step.get(key, "")) for key in ("uses", "run", "name"))


def _is_main_guarded(job: dict) -> bool:
    """Whether the job's own `if` restricts it to `refs/heads/main`.

    The two tests that care about credentials read the guard through this one
    function, so they cannot come to disagree about what counts as guarded.
    """
    return "refs/heads/main" in str(job.get("if", ""))


def _declared_permissions(workflow: dict, job: dict):
    """The `GITHUB_TOKEN` scopes this repository declares for a job.

    A job without a block of its own inherits the workflow's top-level block.
    With neither -- `full-suite.yml` is the case in point -- the job takes the
    repository's default, which is a GitHub setting rather than anything here.
    That default cannot grant `id-token: write`: the scope is `write|none` and
    is granted only where a `permissions` block names it. So an absent block
    cannot hide the thing the rule below forbids, and the rule does not demand
    one. Least privilege for the *other* scopes is a separate question that
    reading this repository cannot settle either way.
    """
    if "permissions" in job:
        return job["permissions"]
    return workflow.get("permissions", {})


def _grants_id_token(permissions) -> bool:
    # `permissions:` may be the string `read-all` or `write-all` instead of a
    # mapping, and `write-all` grants every scope, `id-token` among them.
    if isinstance(permissions, str):
        return permissions == "write-all"
    return bool((permissions or {}).get("id-token"))


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


@pytest.mark.parametrize("workflow", WORKFLOW_FILES)
def test_no_unguarded_job_holds_an_aws_credential_or_the_token_to_get_one(workflow):
    """A job an unreviewed ref can run must not be able to reach AWS.

    This replaces a test that named `plan` and `image` in `infra-deploy.yml`.
    Both jobs had moved -- `image` to `full-suite.yml`, the pull-request work to
    `release-gate.yml` -- leaving `diff` and `deploy` as that file's only jobs,
    so the loop body never executed and the test passed by asserting nothing.
    A guard written against a list of job names goes vacuous the moment the
    jobs are renamed, and says nothing at all about a job added later.

    So the rule is a property of every job in every workflow instead: without a
    `refs/heads/main` guard, a job may neither run
    `configure-aws-credentials` nor hold `id-token`. The credential half is the
    contrapositive of `test_no_credentialed_job_runs_from_an_unreviewed_ref`,
    which covers only the two dispatch workflows; sweeping every file extends
    it to the three it never opens. The `id-token` half is new to this test:
    nothing else notices a job that holds the OIDC token but reaches AWS
    through something other than the pinned action.
    """
    document = _workflow(workflow)
    for job_name, job in (document.get("jobs") or {}).items():
        if _is_main_guarded(job):
            continue
        assert not _grants_id_token(_declared_permissions(document, job)), (
            f"{workflow}:{job_name} holds id-token without a main-only guard, "
            "so an unreviewed branch's YAML can mint an AWS session"
        )
        for step in job.get("steps") or []:
            assert CREDENTIAL_ACTION not in str(step.get("uses", "")), (
                f"{workflow}:{job_name} configures AWS credentials without a "
                "main-only guard"
            )


def test_both_dispatch_jobs_pass_every_required_context():
    """A deploy missing these synthesises a stack with no listener, no
    application URL and an empty sender: it succeeds and is unusable."""
    required = (
        "corridor:certificateArn",
        "corridor:publicHostname",
        "corridor:signInSender",
        "corridor:imageTag",
        "corridor:customerId",
        "corridor:customerEnvironmentId",
        "corridor:deploymentId",
        "corridor:dataClass",
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
        assert _is_main_guarded(job), (
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


@pytest.mark.parametrize(
    ("web", "worker", "allowed"),
    [("0", "0", True), ("0", "1", True), ("1", "1", True), ("1", "0", False)],
)
def test_release_refuses_web_without_a_worker_before_authenticating(web, worker, allowed):
    """The deployed UI must have a supervisor to execute its preparation work."""
    steps = _jobs("app-release.yml")["release"]["steps"]
    counts_at = next(index for index, step in enumerate(steps) if step.get("id") == "counts")
    credential_at = next(
        index for index, step in enumerate(steps)
        if CREDENTIAL_ACTION in str(step.get("uses", ""))
    )
    assert counts_at < credential_at
    result = subprocess.run(
        ["bash", "-e", "-c", steps[counts_at]["run"]],
        env={"WEB_DESIRED_COUNT": web, "WORKER_DESIRED_COUNT": worker},
        capture_output=True, text=True, check=False,
    )
    assert (result.returncode == 0) is allowed


def test_release_drain_migration_and_both_rollout_proofs_are_ordered_and_fail_closed():
    """Migration cannot race an old worker; web starts only after worker proof."""
    steps = _jobs("app-release.yml")["release"]["steps"]
    required = ("drain", "migration", "start_worker", "verify_worker", "start_web", "verify_web")
    ordered = [(index, step) for index, step in enumerate(steps) if step.get("id") in required]
    assert tuple(step["id"] for _, step in ordered) == required
    by_id = {step["id"]: step for _, step in ordered}
    for _, step in ordered:
        assert "if" not in step, "release safety steps must run only after preceding success"
        assert not step.get("continue-on-error"), step["id"]

    drain = by_id["drain"]["run"]
    assert "scripts/verify_ecs_release.py drain" in drain
    for argument in (
        '--web-service "$WEB_SERVICE"', '--worker-service "$WORKER_SERVICE"',
        '--worker-task-definition "$BASE_BATCH_TD"',
    ):
        assert argument in drain
    assert '--task-definition "$MIGRATION_TD"' in by_id["migration"]["run"]
    assert '"corridor.deployment_bootstrap", "configure"' in by_id["migration"]["run"]
    assert 'os.environ["WORKER_DESIRED_COUNT"] != "0"' in by_id["migration"]["run"]
    assert 'command.append("--require-enabled")' in by_id["migration"]["run"]
    for role, task_definition in (("worker", "BATCH_TD"), ("web", "WEB_TD")):
        start = by_id[f"start_{role}"]["run"]
        verify = by_id[f"verify_{role}"]["run"]
        assert f'--service "${role.upper()}_SERVICE"' in start
        assert f'--task-definition "${task_definition}"' in start
        assert f'--desired-count "${role.upper()}_DESIRED_COUNT"' in start
        assert "scripts/verify_ecs_release.py verify" in verify
        assert f'--service "${role.upper()}_SERVICE"' in verify
        assert f'--task-definition "${task_definition}"' in verify
        assert '--digest "$IMAGE_DIGEST"' in verify


def test_a_release_failure_after_drain_keeps_both_services_stopped_for_recovery():
    steps = _jobs("app-release.yml")["release"]["steps"]
    recovery = next(
        step for step in steps
        if "failure()" in str(step.get("if", ""))
        and "scripts/verify_ecs_release.py drain" in str(step.get("run", ""))
    )
    assert "steps.drain.outcome" in recovery["if"]
    assert '--web-service "$WEB_SERVICE"' in recovery["run"]
    assert '--worker-service "$WORKER_SERVICE"' in recovery["run"]
    assert "Do not restart an old task revision" in recovery["run"]
    assert not recovery.get("continue-on-error")


@pytest.mark.parametrize("missing", ["CUSTOMER_ID", "CUSTOMER_ENVIRONMENT_ID", "DEPLOYMENT_ID", "DATA_CLASS"])
def test_deployment_configuration_requires_an_explicit_synthetic_binding(missing):
    env = {
        "CERTIFICATE_ARN": "arn:aws:acm:us-east-2:111111111111:certificate/00000000-0000-0000-0000-000000000000",
        "PUBLIC_HOSTNAME": "pilot.example.com", "SIGN_IN_SENDER": "signin@example.com",
        "CUSTOMER_ID": "synthetic-a", "CUSTOMER_ENVIRONMENT_ID": "synthetic-nonproduction",
        "DEPLOYMENT_ID": "corridor-nonproduction", "DATA_CLASS": "synthetic",
    }
    script = WORKFLOWS.parent / "scripts" / "validate-deployment-config.sh"
    accepted = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    assert accepted.returncode == 0, accepted.stdout
    env.pop(missing)
    refused = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    assert refused.returncode != 0
