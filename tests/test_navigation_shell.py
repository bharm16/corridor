"""The navigation shell, on every customer page and pointing somewhere (#843).

`tests/test_architecture.py` owns *membership* -- which manifest pages render
the shell and which two deliberately do not -- because that is a property of
the templates and costs no database. This file owns what membership cannot
say: that each item goes to a route the deployment serves, that the markup and
`corridor.web.navigation` cannot drift apart, that exactly one item is marked
on every page a coordinator lands on, and that the account item works from a
page other than the projects list, which is the one place it used to live.

The rendered half drives the real sign-in flow and submits the form the shell
itself printed. Nothing overrides `get_human_principal`: a sign-out form that
omitted the request-forgery field would not be "unprotected" but inoperable,
and a test that replaced the dependency doing the checking could not tell the
two apart (#821).
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from corridor import access, web_boundary
from corridor.principals import HumanPrincipal
from corridor.web import auth, navigation
from corridor.web.app import app, get_session

from browser_session_support import form_fields, sign_in, submit_form


REPO_ROOT = Path(__file__).resolve().parents[1]
SHELL_TEMPLATE = REPO_ROOT / "src/corridor/web/templates/_shell.html"

COORDINATOR = HumanPrincipal("local:shell-coordinator")
EMAIL = "shell-coordinator@example.test"
OPERATOR = HumanPrincipal("local:operations")

_ARIA_CURRENT = re.compile(r'aria-current="(?P<value>[^"]*)"')


# --- Where the items go, and that the markup still says so ------------------


def test_every_shell_destination_is_a_route_the_deployment_serves():
    """An item pointing outside the manifest is a 404 printed on every page."""
    served = {path for _method, path in web_boundary.PILOT_ROUTES}

    unserved = {
        name: destination
        for name, destination in navigation.DESTINATIONS.items()
        if destination.format(slug="{slug}") not in served
    }

    assert unserved == {}, (
        f"{unserved}: a navigation item goes somewhere an enforcing "
        "deployment answers the way it answers a missing project"
    )
    assert ("POST", navigation.SIGN_OUT_DESTINATION) in web_boundary.PILOT_ROUTES


def test_the_shell_writes_every_destination_the_module_declares():
    """The module decides the destinations; the markup writes them literally.

    `_shell.html` spells each path out rather than printing `item.href`, so
    `tests/test_manifest_page_links.py` can read the link from the template's
    own source instead of recording it as one it cannot resolve. That only
    stays true while the two agree, which is what this asserts.
    """
    markup = SHELL_TEMPLATE.read_text(encoding="utf-8")

    missing = [
        destination
        for destination in list(navigation.DESTINATIONS.values())
        + [navigation.SIGN_OUT_DESTINATION]
        if f'"{destination.format(slug="{{ project.slug }}")}"' not in markup
    ]

    assert missing == [], (
        f"{missing}: `corridor.web.navigation` declares a destination the "
        "shell's markup does not write, so the link ratchet cannot read it"
    )


# --- Which item is marked, and how ------------------------------------------


@pytest.mark.parametrize(
    "path,marked",
    [
        ("/", navigation.PROJECTS),
        ("/portfolio", navigation.PROJECTS),
        ("/work/acme", navigation.WORK),
        ("/work/acme/issue/prepare", navigation.WORK),
        ("/issue-configuration/acme", navigation.WORK),
        ("/review/acme", navigation.WORK),
        ("/review/acme/packet/7", navigation.WORK),
        ("/record/acme", navigation.RECORD),
        ("/projects/acme/sources", navigation.SOURCES),
        ("/projects/acme/sources/upload", navigation.SOURCES),
        ("/sources/acme/passage/12", navigation.SOURCES),
    ],
)
def test_one_item_is_marked_on_every_page_the_manifest_renders(path, marked):
    """"Where am I" is answered on every customer page, not only on four.

    Marking an exact path alone would leave the Review screen, the upload
    form, the exact source and the issue configuration saying nothing at all.
    """
    shell = navigation.shell(path, slug="acme", project_name="Acme")

    current = [item for item in shell.items if item.aria_current]

    assert [item.name for item in current] == [marked]


def test_the_destination_itself_is_announced_differently_from_a_page_inside_it():
    """ARIA already separates the two readings; the shell uses both."""
    at = navigation.shell("/record/acme", slug="acme", project_name="Acme")
    within = navigation.shell("/review/acme", slug="acme", project_name="Acme")

    assert at.record.aria_current == "page"
    assert within.work.aria_current == "true"
    # The mark is decorative reinforcement in both, never the announcement.
    assert at.record.mark == navigation.HERE_MARK
    assert within.work.mark == navigation.HERE_MARK


def test_across_projects_the_shell_offers_no_item_it_cannot_point_at():
    """There is no current project on the projects list, so it has no items."""
    shell = navigation.shell("/", slug="", project_name="")

    assert [item.name for item in shell.items] == [navigation.PROJECTS]
    assert shell.project_name == ""


# --- The shell as a coordinator receives it ---------------------------------


@pytest.fixture
def sender():
    return auth.RecordingEmailSender()


@pytest.fixture
def client(session, sender):
    # Only the plumbing is overridden. Identity stays on the real cookie path,
    # so the sign-out form below is proved rather than bypassed.
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    with TestClient(app, base_url="https://testserver") as opened:
        yield opened
    app.dependency_overrides.clear()


@pytest.fixture
def enrolled(session, project, client, sender):
    """One project this coordinator is on, reached by a consumed magic link."""
    access.enroll_member(
        session,
        project_id=project.id,
        email=EMAIL,
        principal=COORDINATOR,
        display_name="Shell Coordinator",
        designations=[access.COORDINATION],
        operator=OPERATOR,
    )
    sign_in(client, sender, EMAIL)
    return project


def _shell_of(body: str) -> str:
    opening = body.index('<nav class="shell"')
    return body[opening : body.index("</nav>", opening)]


@pytest.mark.parametrize(
    "path,marked",
    [
        ("/", "Projects"),
        ("/work/{slug}", "Work"),
        ("/record/{slug}", "Record"),
        ("/projects/{slug}/sources", "Sources"),
    ],
)
def test_the_same_shell_is_on_every_page_a_coordinator_lands_on(
    client, enrolled, path, marked
):
    """One shell, the same items, and this page's own item marked.

    A page across projects has no current project, so the project's own items
    do not exist there and are not drawn; Projects and the account are on
    every page either way.
    """
    page = client.get(path.format(slug=enrolled.slug))
    assert page.status_code == 200, page.text[:400]

    shell = _shell_of(page.text)
    expected = ["Projects", "Sign out"]
    if "{slug}" in path:
        expected = ["Projects", "Sources", "Work", "Record", "Sign out"]
    for word in expected:
        assert f">{word}</" in shell, (word, shell)
    assert f'action="{navigation.SIGN_OUT_DESTINATION}"' in shell

    marks = _ARIA_CURRENT.findall(shell)
    assert marks == ["page"], shell
    here = shell[shell.index('aria-current="page"') :]
    assert marked in here[: here.index("</a>")]


def test_the_project_the_reader_is_inside_is_named_by_the_shell(client, enrolled):
    """The shell says which project, not only which section of one."""
    page = client.get(f"/record/{enrolled.slug}")

    assert enrolled.name in _shell_of(page.text)


def test_signing_out_works_from_a_page_that_is_not_the_projects_list(
    client, enrolled, session
):
    """The account control used to exist on one page of thirteen.

    The payload is the one the shell itself printed, so a shell whose form
    omitted the request-forgery field would fail here with the 403 a
    coordinator would have received.
    """
    page = client.get(f"/work/{enrolled.slug}")
    fields = form_fields(page.text, navigation.SIGN_OUT_DESTINATION)
    assert fields and auth.CSRF_FIELD in fields, page.text[:400]

    signed_out = submit_form(client, navigation.SIGN_OUT_DESTINATION, fields)
    assert signed_out.status_code == 303
    assert signed_out.headers["location"] == "/sign-in"

    session.expire_all()
    assert client.get("/portfolio", follow_redirects=False).status_code == 401


def test_the_pages_reached_signed_out_carry_no_shell(client):
    """The pre-authentication pair: no project, no account, nothing to leave."""
    form = client.get("/sign-in")

    assert form.status_code == 200
    assert '<nav class="shell"' not in form.text
    assert navigation.SIGN_OUT_DESTINATION not in form.text
