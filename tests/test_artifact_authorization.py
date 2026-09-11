"""Every route that hands out artifact bytes refuses the wrong person (#848).

The customer-journey audit's last guarantee is that an output the product
calls approved can be fetched back *through the product* -- which means the
product grows routes that return bytes rather than pages, and a route that
returns bytes is the one kind of route where a missing authorization check
hands over the thing itself rather than a description of it.

This guard is mechanical in the sense that matters: **it does not hold a list
of the routes to check.** It reads ``src/corridor/web/app.py`` and finds every
route whose handler reaches a response carrying artifact bytes -- a PDF, a
workbook, a page image -- by following the helpers that build one. A route
added next year that returns a new kind of file is covered the day it is
written, and a route that returns a new kind of file through a new helper is
covered as soon as that helper constructs a response with a media type that is
not a page. The only thing a new route must teach this file is how to address
it: a path parameter nothing here knows how to fill fails the guard by name
rather than being skipped.

**Which deployment this proves.** Not the enforced live-pilot boundary: under
that boundary a route outside the manifest is refused before its handler, and
a guard that ran only there would prove the manifest rather than the check.
The membership gate has to hold on every deployment that serves these routes
at all, so this runs the way the rest of the web suite runs -- and the
boundary's own refusals are proved by ``tests/test_project_partition_and_
offboarding.py``, where they belong.

**Why there are two halves, and what each one is for.** Driving the routes
over HTTP is not enough on its own, and that was measured rather than
assumed: removing the membership gate from
``download_released_report`` outright and re-running the HTTP cases changed
nothing, because the release the request named did not exist and the
ungated route answered "no such release" instead of handing over bytes. So
the primary guard is static -- every route that reaches artifact bytes
resolves a signed-in person *and* reaches the one project gate, in its own
call path -- and that is the half that fails when somebody deletes a check.
The HTTP half then proves the gate is really on the wire: no session is
refused, and somebody enrolled on a different project is refused, with no
bytes in either answer. One route serves real bytes to its own member on an
empty project, and it is asserted below, so the refusals are refusals of
something rather than of nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from corridor import access
from corridor.models import Document, Project
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import app, get_session


APP_SOURCE = Path(__file__).resolve().parents[1] / "src/corridor/web/app.py"

OPERATOR = HumanPrincipal("local:operations")
MEMBER = HumanPrincipal("local:artifact-member")
OUTSIDER = HumanPrincipal("local:artifact-outsider")
MEMBER_EMAIL = "member@example.test"
OUTSIDER_EMAIL = "outsider@example.test"

#: Media types a *page* is served with. A response carrying anything else is
#: an artifact: the product handing over a file rather than describing one.
PAGE_MEDIA_TYPES = frozenset({"text/html", "application/json", "text/plain"})

#: An id no row has. The HTTP half cannot mint a real rendered PDF cheaply,
#: which is exactly why the static half below exists rather than instead of
#: it.
NO_SUCH_ID = "999999"

#: The one project gate in ``app.py``. Everything slug-addressed resolves its
#: project through ``_project``, which calls this; ``page_image`` reaches the
#: project through the document and calls it directly.
PROJECT_GATE = "_authorize"

#: The dependency that resolves the signed-in person and, on an unsafe
#: method, checks the request-forgery token.
PRINCIPAL_DEPENDENCY = "get_human_principal"

#: An artifact route that serves real bytes to its own member without any
#: fixture: proof that the refusals below are refusals of something.
BYTES_WITHOUT_A_FIXTURE = "/internal-report/{slug}/workbook.xlsx"

#: Modules beside ``app.py`` whose whole purpose is to build a response
#: carrying artifact bytes, and whose builders ``app.py`` imports by name.
#: The reading above judges a response by the media type *written in the
#: call*, and a builder that chooses one from a table writes none, so the
#: closure is seeded with every response these modules build instead (#830).
#: A module here is a module that may not build a page.
ARTIFACT_RESPONSE_MODULES = ("artifact_downloads",)

#: How this guard addresses each path parameter an artifact route declares.
#: A parameter missing from here fails ``test_every_artifact_route_is_
#: addressable`` by name, which is the instruction to teach the guard rather
#: than the licence to skip the route.
PARAMETER_VALUES = {
    "slug": "the project the artifact belongs to",
    "artifact_id": NO_SUCH_ID,
    "release_id": NO_SUCH_ID,
    "document_id": "a document of that project",
    "page_no": "1",
    # #830: a prepared candidate's row id, an approved issue's number in its
    # project's release chain, and one member of the configured set. The
    # first two are ids no row has, for the reason NO_SUCH_ID exists; the
    # artifact type is the one every issue must contain, so a route that
    # answered a stranger would be answering about a real member of a real
    # set rather than about a type nothing configures.
    "candidate_id": NO_SUCH_ID,
    "issue_number": NO_SUCH_ID,
    "artifact_type": "updated_ucm",
}


# --- finding the routes that hand out bytes ---------------------------------


def _media_type_of(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg != "media_type":
            continue
        try:
            value = ast.literal_eval(keyword.value)
        except (ValueError, SyntaxError):
            return None
        return value if isinstance(value, str) else None
    return None


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _builds_a_response(node: ast.AST) -> bool:
    """Whether this body constructs any response at all.

    Used only for the modules in ``ARTIFACT_RESPONSE_MODULES``, which build
    nothing else, so "a response" and "a response carrying artifact bytes"
    are the same statement there.
    """

    return any(name.endswith("Response") for name in _called_names(node))


def _helper_emitters() -> set[str]:
    """Response builders defined beside ``app.py`` and imported into it."""

    found: set[str] = set()
    for module in ARTIFACT_RESPONSE_MODULES:
        path = APP_SOURCE.parent / f"{module}.py"
        functions = _functions(ast.parse(path.read_text(encoding="utf-8")))
        found |= {
            name
            for name, node in functions.items()
            if _emits_artifact_bytes(node) or _builds_a_response(node)
        }
    return found


def _emits_artifact_bytes(node: ast.AST) -> bool:
    """Whether this body constructs a response that is a file, not a page.

    ``FileResponse`` is one by definition. Everything else is judged by the
    media type it declares, so a helper that starts returning a new kind of
    file is found without anybody remembering to add it here.
    """

    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.func.id == "FileResponse":
            return True
        media_type = _media_type_of(call)
        if media_type is not None and media_type not in PAGE_MEDIA_TYPES:
            return True
    return False


def _route_paths(node: ast.AST) -> tuple[tuple[str, str], ...]:
    found = []
    for decorator in getattr(node, "decorator_list", ()):
        if not isinstance(decorator, ast.Call):
            continue
        function = decorator.func
        if not isinstance(function, ast.Attribute):
            continue
        if not isinstance(function.value, ast.Name) or function.value.id != "app":
            continue
        if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
            continue
        found.append((function.attr.upper(), decorator.args[0].value))
    return tuple(found)


def artifact_byte_routes() -> tuple[tuple[str, str, str], ...]:
    """Every ``(method, path, handler)`` that answers with artifact bytes.

    Transitive, because the emitters are helpers: ``_pdf_download`` calls
    ``_pdf_response``, and the routes call one or the other. The closure runs
    until it stops growing rather than to a fixed depth, so another layer of
    helper changes nothing here.
    """

    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    functions = _functions(tree)
    emitters = {name for name, node in functions.items() if _emits_artifact_bytes(node)}
    emitters |= _helper_emitters()
    while True:
        grown = {
            name
            for name, node in functions.items()
            if name not in emitters and _called_names(node) & emitters
        }
        if not grown:
            break
        emitters |= grown
    routes = []
    # Intersected with ``app.py``'s own functions, because the seed above
    # names builders that live in another module and decorate no route.
    for name in sorted(emitters & functions.keys()):
        for method, path in _route_paths(functions[name]):
            routes.append((method, path, name))
    return tuple(sorted(routes))


def _reaching(target: str, functions) -> set[str]:
    """Every function whose call path reaches ``target``, transitively."""

    reaching = {target}
    while True:
        grown = {
            name
            for name, node in functions.items()
            if name not in reaching and _called_names(node) & reaching
        }
        if not grown:
            break
        reaching |= grown
    return reaching


def _resolves_a_principal(node: ast.AST) -> bool:
    """Whether this handler takes the signed-in person as a dependency."""

    arguments = getattr(node, "args", None)
    if arguments is None:
        return False
    for default in list(arguments.defaults) + list(arguments.kw_defaults):
        if not isinstance(default, ast.Call) or not isinstance(default.func, ast.Name):
            continue
        if default.func.id != "Depends":
            continue
        named = default.args[0] if default.args else None
        if isinstance(named, ast.Name) and named.id == PRINCIPAL_DEPENDENCY:
            return True
    return False


def ungated_artifact_routes() -> tuple[str, ...]:
    """Artifact routes that resolve no person, or reach no project gate.

    The sentence a failure prints, rather than a set difference, because the
    two halves are different defects: a route nobody has to sign in for, and a
    route a signed-in stranger may call.
    """

    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    functions = _functions(tree)
    gated = _reaching(PROJECT_GATE, functions)
    faults = []
    for _method, path, handler in ARTIFACT_ROUTES:
        node = functions[handler]
        if not _resolves_a_principal(node):
            faults.append(
                f"{path} ({handler}) takes no {PRINCIPAL_DEPENDENCY} "
                "dependency, so it answers without anybody being signed in"
            )
        if handler not in gated:
            faults.append(
                f"{path} ({handler}) never reaches {PROJECT_GATE}, so a "
                "signed-in stranger reaches its bytes"
            )
    return tuple(faults)


ARTIFACT_ROUTES = artifact_byte_routes()


# --- the deployment under test ----------------------------------------------


@pytest.fixture
def sender():
    return auth.RecordingEmailSender()


@pytest.fixture
def client(session, sender):
    """The application on this test's transaction, with nobody overridden.

    Identity is the real magic-link path, so "signed in" below means a cookie
    a consumed link established, and "not signed in" means no cookie at all.
    """

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    with TestClient(app, base_url="https://testserver") as opened:
        yield opened
    app.dependency_overrides.clear()


@pytest.fixture
def two_projects(session):
    """One project holding an artifact-bearing document, and one that does not."""

    theirs = Project(slug="artifact-theirs", name="Theirs", is_synthetic=True)
    ours = Project(slug="artifact-ours", name="Ours", is_synthetic=True)
    session.add_all([theirs, ours])
    session.flush()
    document = Document(
        project_id=ours.id,
        sha256="a" * 64,
        filename="ours.pdf",
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    access.enroll_member(
        session,
        project_id=ours.id,
        email=MEMBER_EMAIL,
        principal=MEMBER,
        display_name="Member",
        designations=[access.COORDINATION, access.EXTERNAL_RELEASE],
        operator=OPERATOR,
    )
    access.enroll_member(
        session,
        project_id=theirs.id,
        email=OUTSIDER_EMAIL,
        principal=OUTSIDER,
        display_name="Outsider",
        designations=[access.COORDINATION, access.EXTERNAL_RELEASE],
        operator=OPERATOR,
    )
    return ours, int(document.id)


def sign_in(client, sender, email: str) -> None:
    """Establish a session the way a person does, through the real link."""

    client.cookies.clear()
    requested = client.post(
        "/sign-in/request", data={"email": email}, follow_redirects=False
    )
    assert requested.status_code == 200, requested.text
    token = parse_qs(urlsplit(sender.sent[-1][1]).query)["token"][0]
    consumed = client.get(
        "/sign-in/consume", params={"token": token}, follow_redirects=False
    )
    assert consumed.status_code == 303, consumed.text


def address(path: str, project: Project, document_id: int) -> str:
    """Fill one route's path parameters, or say which one is unknown."""

    filled = path
    for name in _parameters(path):
        if name == "slug":
            value = project.slug
        elif name == "document_id":
            value = str(document_id)
        elif name in PARAMETER_VALUES:
            value = PARAMETER_VALUES[name]
        else:
            raise AssertionError(
                f"{path} takes a path parameter {{{name}}} this guard does not "
                "know how to address; teach PARAMETER_VALUES how to fill it "
                "rather than leaving the route unproved"
            )
        filled = filled.replace("{" + name + "}", value)
    return filled


def _parameters(path: str) -> tuple[str, ...]:
    return tuple(
        piece.split("}", 1)[0] for piece in path.split("{")[1:] if "}" in piece
    )


def carries_artifact_bytes(response) -> bool:
    media_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    return bool(media_type) and media_type not in PAGE_MEDIA_TYPES


# --- the guard ---------------------------------------------------------------


def test_the_scan_finds_the_routes_that_hand_out_bytes():
    """The discovery is the guard, so it fails loudly when it finds nothing.

    A refactor that moved the response helpers, or renamed ``FileResponse``
    behind an alias, would otherwise leave every case below passing over an
    empty list.
    """

    assert ARTIFACT_ROUTES, (
        "no route in src/corridor/web/app.py was found to answer with artifact "
        "bytes, which cannot be true while the product offers downloads"
    )
    served = {
        (method, path) for method, path, _handler in ARTIFACT_ROUTES
    }
    assert ("GET", "/reports/{slug}/prepared/{artifact_id}/download") in served
    assert ("GET", "/page-image/{document_id}/{page_no}") in served
    # #830's downloads build their response in a helper module, which is the
    # case ``ARTIFACT_RESPONSE_MODULES`` exists for.
    assert (
        "GET",
        "/work/{slug}/issue/packages/{issue_number}/artifacts/{artifact_type}",
    ) in served
    assert ("GET", "/work/{slug}/issue/packages/{issue_number}/bundle") in served


def test_every_artifact_route_is_addressable(two_projects):
    """A new artifact route must teach this guard how to reach it."""

    project, document_id = two_projects
    for method, path, _handler in ARTIFACT_ROUTES:
        address(path, project, document_id)


@pytest.mark.parametrize(
    ("method", "path", "handler"),
    ARTIFACT_ROUTES,
    ids=[f"{method} {path}" for method, path, _ in ARTIFACT_ROUTES],
)
def test_an_artifact_route_refuses_everyone_but_a_member(
    client, sender, two_projects, method, path, handler
):
    """No session, the wrong project, and then the member who may have it."""

    project, document_id = two_projects
    url = address(path, project, document_id)

    anonymous = client.request(method, url, follow_redirects=False)
    assert anonymous.status_code == 401, (
        f"{handler} answered {anonymous.status_code} with no session at all"
    )
    assert not carries_artifact_bytes(anonymous), (
        f"{handler} handed bytes to a caller with no session"
    )

    sign_in(client, sender, OUTSIDER_EMAIL)
    outsider = client.request(method, url, follow_redirects=False)
    assert outsider.status_code in (403, 404), (
        f"{handler} answered {outsider.status_code} to somebody enrolled on a "
        "different project"
    )
    assert not carries_artifact_bytes(outsider), (
        f"{handler} handed bytes to somebody enrolled on a different project"
    )

    sign_in(client, sender, MEMBER_EMAIL)
    member = client.request(method, url, follow_redirects=False)
    assert member.status_code != 401, f"{handler} refused a signed-in member"


def test_every_artifact_route_resolves_a_person_and_reaches_the_project_gate():
    """The guard that catches a deleted check, and the one that was measured.

    Written against the call path rather than the response, because a route
    whose artifact does not happen to exist answers a stranger and a member
    identically, and an HTTP case alone would pass over a route with no gate
    at all.
    """

    faults = ungated_artifact_routes()
    assert not faults, "\n".join(faults)


def test_a_member_really_does_receive_artifact_bytes(client, sender, two_projects):
    """The refusals above refuse something: this member gets the file.

    The internal workbook is the one artifact a project can produce with no
    fixture at all, so it is the case that proves an artifact route on this
    deployment serves bytes to the person entitled to them.
    """

    project, _document_id = two_projects
    sign_in(client, sender, MEMBER_EMAIL)

    served = client.get(
        BYTES_WITHOUT_A_FIXTURE.replace("{slug}", project.slug),
        follow_redirects=False,
    )

    assert served.status_code == 200, served.text
    assert carries_artifact_bytes(served), served.headers.get("content-type")
    assert served.content, "the workbook downloaded as no bytes"
