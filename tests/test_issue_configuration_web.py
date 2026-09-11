"""The Issue configuration page, and the approval it carries (#828).

The page is driven the way a coordinator drives it: a real session established
by the magic-link flow, the selection prepared through the form the page
rendered, and the approval submitted with exactly the hidden fields that form
emitted. Nothing here replaces ``get_human_principal`` — the dependency that
checks the request-forgery token — because a form that omits the token is not
"unprotected" but inoperable, and a test that replaced the check could not tell
the two apart (#821).

Nothing reads a clock: the request instant is declared through
``get_review_clock``, and it is the instant an approved version takes effect.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import html
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from corridor.access import COORDINATION, EXTERNAL_RELEASE, enroll_member
from corridor.issue_profile import (
    ArtifactEntry,
    effective_issue_inventory,
    issue_profile_history,
)
from corridor.models import Project
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import app, get_review_clock, get_session

from browser_session_support import form_fields, sign_in, submit_form
from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes
from packet_review_support import CHASE_RENDERER, REPORT_RENDERER, configure_issue


COORDINATOR = HumanPrincipal("local:coordinator")
RELEASER = HumanPrincipal("local:releaser")
OPERATOR = HumanPrincipal("local:operator")

JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
NOW = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)

WEEKLY = "weekly_coordination_report"
CHASE = "chase_list"
SIDECAR = "provenance_sidecar"


@pytest.fixture
def adopted(session, tmp_path):
    """One adopted project with a coordinator and a designated releaser."""

    row = Project(
        slug=f"issue-config-{uuid4().hex[:8]}",
        name="Issue Configuration",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    for principal, email, name, designations in (
        (COORDINATOR, "coordinator@example.test", "Coordinator", [COORDINATION]),
        (RELEASER, "releaser@example.test", "Releaser", [EXTERNAL_RELEASE]),
    ):
        enroll_member(
            session,
            project_id=row.id,
            email=email,
            principal=principal,
            display_name=name,
            designations=designations,
            operator=OPERATOR,
        )
    adopt(session, row, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    return row


@pytest.fixture
def sender():
    """The replaceable mail seam, as a non-sending capture: a link is never sent."""

    return auth.RecordingEmailSender()


@pytest.fixture
def browser(session, sender):
    """Signed-in browsers, with identity left to the real session-cookie path.

    Only the plumbing is replaced — the transaction, the mail seam and the
    declared instant — so the request-forgery check every write passes is the
    deployed one. `https` so the Secure cookies round-trip.
    """

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with ExitStack() as open_browsers:

        def signed_in(email: str) -> TestClient:
            client = open_browsers.enter_context(
                TestClient(app, base_url="https://testserver")
            )
            sign_in(client, sender, email)
            return client

        yield signed_in
    app.dependency_overrides.clear()


def checked_artifacts(body: str) -> set[str]:
    """Which artifact boxes the selection form opens already ticked."""

    return {
        found.group(1)
        for found in re.finditer(
            r'name="artifact" value="([^"]+)"\s*checked', body
        )
    }


def prose(body: str) -> str:
    """The page as a reader receives it, with markup escaping undone."""

    return html.unescape(body)


def page(client, project, *, artifacts=None) -> str:
    """Read the configuration page, optionally with a prepared selection."""

    params = (
        {} if artifacts is None else {"propose": "1", "artifact": list(artifacts)}
    )
    response = client.get(f"/issue-configuration/{project.slug}", params=params)
    assert response.status_code == 200, response.text
    return response.text


def approve(client, project, body):
    """Submit exactly the hidden fields the approval form rendered."""

    fields = form_fields(body, "/approve")
    assert fields is not None, "the page rendered no approval form"
    return submit_form(
        client, f"/issue-configuration/{project.slug}/approve", fields
    )


def test_the_page_reads_what_is_configured_and_who_approved_it(
    session, adopted, browser
):
    """#828's first criterion: the effective version, its history, its approver."""

    configure_issue(
        session,
        adopted,
        principal=COORDINATOR,
        effective_from=JANUARY,
        artifacts=(ArtifactEntry(artifact_type=WEEKLY, renderer=REPORT_RENDERER),),
    )
    rendered = page(browser("coordinator@example.test"), adopted)
    body = prose(rendered)

    # The selection form opens on what is configured, so preparing without
    # touching a control proposes no change rather than proposing to stop
    # issuing everything.
    assert checked_artifacts(rendered) == {WEEKLY}
    assert "the customer's updated UCM workbook" in body
    assert "the weekly Coordination Report" in body
    # The optional artifacts this project does not issue are still readable as
    # choices, and the one nothing can render says so rather than offering.
    assert "the chase list" in body
    assert "no renderer revision this release registers" in body
    # The approved template and mapping, the coverage expectation and the
    # pre-issue decision policy all have their own readings.
    assert "Output template" in body and "Field mapping" in body
    assert "Source coverage expected before an issue" in body
    assert "Decisions this customer requires before an issue" in body
    # Every version, and who approved each.
    assert "Every version, and who approved it" in body
    assert COORDINATOR.subject in body


def test_a_coordinator_prepares_a_change_and_approves_it(session, adopted, browser):
    """#828's second and fifth criteria, through the form the page rendered.

    Nothing about the submission is composed here: the selection is prepared
    through the page, and the approval carries exactly the hidden fields —
    including the request-forgery token — that the rendered form emitted.
    """

    client = browser("coordinator@example.test")
    prepared = page(client, adopted, artifacts=[WEEKLY])
    assert "Proposed version 1" in prose(prepared)

    response = approve(client, adopted, prepared)

    assert response.status_code == 201, response.text
    assert "Version 1 is in force" in prose(response.text)
    inventory = effective_issue_inventory(session, adopted.id, NOW)
    assert inventory is not None
    assert set(inventory.artifact_types) == {"updated_ucm", WEEKLY}
    assert inventory.registered_by_principal == COORDINATOR.subject
    assert inventory.effective_from == NOW


def test_the_same_approval_clicked_twice_registers_one_version(
    session, adopted, browser
):
    """A resubmitted approval converges rather than opening a second version."""

    client = browser("coordinator@example.test")
    prepared = page(client, adopted, artifacts=[WEEKLY])

    first = approve(client, adopted, prepared)
    second = approve(client, adopted, prepared)

    assert first.status_code == 201
    assert second.status_code == 200
    assert "Nothing changed" in prose(second.text)
    assert len(issue_profile_history(session, adopted.id)) == 1


def test_an_approval_after_someone_else_changed_it_is_refused(
    session, adopted, browser
):
    """The proposal binds the version it would replace, and that version moved.

    The concurrent approval is a *different* configuration. One that happened
    to be identical would not be a conflict at all: it is already in force, and
    there would be nothing left for this approval to add.
    """

    client = browser("coordinator@example.test")
    prepared = page(client, adopted, artifacts=[WEEKLY])
    configure_issue(
        session,
        adopted,
        principal=COORDINATOR,
        effective_from=JANUARY,
        artifacts=(ArtifactEntry(artifact_type=CHASE, renderer=CHASE_RENDERER),),
    )

    response = approve(client, adopted, prepared)

    assert response.status_code == 409, response.text
    assert "changed by someone else" in prose(response.text)
    assert len(issue_profile_history(session, adopted.id)) == 1


def test_an_artifact_this_release_cannot_render_is_refused_at_approval(
    session, adopted, browser
):
    """#828's third criterion, reached the way a prepared proposal reaches it.

    The page offers no control for the provenance sidecar, because nothing
    registers a renderer for it. A proposal that names it anyway — which is
    what a prepared configuration handed over as a link can do — is refused at
    the approval, by the artifact's own name.
    """

    client = browser("coordinator@example.test")
    prepared = page(client, adopted, artifacts=[SIDECAR])
    assert "the provenance sidecar" in prose(prepared)

    response = approve(client, adopted, prepared)

    assert response.status_code == 409, response.text
    assert "the provenance sidecar" in prose(response.text)
    assert issue_profile_history(session, adopted.id) == ()


def test_a_member_without_the_coordination_designation_is_refused(
    session, adopted, browser
):
    """The registration command owns the rule, and its sentence is what prints."""

    client = browser("releaser@example.test")
    prepared = page(client, adopted, artifacts=[WEEKLY])

    response = approve(client, adopted, prepared)

    assert response.status_code == 409, response.text
    assert "project-coordination decision" in prose(response.text)
    assert issue_profile_history(session, adopted.id) == ()


def test_a_non_member_is_answered_exactly_like_a_missing_project(
    session, adopted, browser
):
    """The partition gate is the same one every slug-addressed surface passes.

    The outsider is a real signed-in person who coordinates a different
    project, so what refuses them here is membership of *this* project and not
    the absence of an identity.
    """

    other = Project(slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    enroll_member(
        session,
        project_id=other.id,
        email="outsider@example.test",
        principal=HumanPrincipal("local:outsider"),
        display_name="Outsider",
        designations=[COORDINATION],
        operator=OPERATOR,
    )
    client = browser("outsider@example.test")

    assert client.get(f"/issue-configuration/{adopted.slug}").status_code == 404
    assert (
        client.post(
            f"/issue-configuration/{adopted.slug}/approve",
            data={"content_sha256": "0" * 64},
            headers={auth.CSRF_HEADER: client.cookies.get(auth.CSRF_COOKIE)},
        ).status_code
        == 404
    )
