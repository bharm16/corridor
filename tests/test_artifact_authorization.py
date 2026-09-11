"""Every route that hands out artifact bytes refuses the wrong person (#848).

The customer-journey audit's last guarantee is that an output the product
calls approved can be fetched back *through the product* -- which means the
product grows routes that return bytes rather than pages, and a route that
returns bytes is the one kind of route where a missing authorization check
hands over the thing itself rather than a description of it.

**Which routes those are is declared, not sniffed.** The list lives in
``corridor.web.artifact_responses`` and is reconciled here against the source
of ``corridor.web`` and against the routes the application actually registers,
the way #909 reconciles the frontend receipt registry against the router. The
reading this replaces judged a response by the media type written beside it
and called ``text/plain`` and ``application/json`` pages -- which they are not:
``release_candidate.ARTIFACT_SUFFIXES`` configures ``.txt`` for the accepted
change summary and the weekly coordination report and ``.json`` for the chase
list, so three of the four artifact types a customer receives were media types
that reading called pages. Nothing here is skipped: an unclassified response,
a stale declaration, a route the reader cannot resolve, and a derived set that
disagrees with the declaration are each a named failure.

**Which deployment this proves.** Not the enforced live-pilot boundary: under
that boundary a route outside the manifest is refused before its handler, and
a guard that ran only there would prove the manifest rather than the check.
The membership gate has to hold on every deployment that serves these routes
at all, so this runs the way the rest of the web suite runs -- and the
boundary's own refusals are proved by ``tests/test_project_partition_and_
offboarding.py``, where they belong.

**Why there are three parts, and what each one is for.** Driving a route over
HTTP with an identifier no row has is not enough on its own, and that was
measured rather than assumed: removing the membership gate from
``download_released_report`` outright and re-running those cases changed
nothing, because the release the request named did not exist and the ungated
route answered "no such release" instead of handing over bytes. So:

* the **static** guard reads every declared route's call path and requires it
  to resolve a signed-in person *and* reach the one project gate. That is the
  half that fails when somebody deletes a check from a route whose fixture is
  expensive;
* the **sweep** drives every declared route over the wire with no session and
  then as somebody enrolled on a different project, so the gate is proved to
  be on the wire rather than only in the source;
* the **families** below stand up one real artifact per supported family --
  real retained bytes, retrievable by the person entitled to them -- and prove
  that the same two refusals refuse *that*, that a member's own project slug
  will not reach another project's artifact id, and that the bytes a member
  receives are exactly the bytes that were retained. Deleting a family's
  authorization call fails its family case with bytes in the wrong hands,
  which is the failure the sweep alone could not produce.

One transaction may declare one project-authorization scope (#662), so the
"their slug, our identifier" case is a separate test from the three refusals
above it: the first declares the artifact's project, the second declares the
requester's own.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.routing import Route

from corridor import access
from corridor.frontend_request_receipts import served_route_identity
from corridor.models import (
    DeltaFollowUpPlan,
    Document,
    DocPage,
    Project,
    ReleasePackage,
)
from corridor.object_storage import store_bytes
from corridor.operating_mode import adopt_project_baseline
from corridor.outgoing_requests import retain_outgoing_request
from corridor.packet_review import FocusedAnswer, focused_request, read_review_items
from corridor.principals import HumanPrincipal
from corridor.release_authorization import (
    authorize_release_package,
    candidate_set,
    package_set,
)
from corridor.report_release import (
    RenderedExternalReport,
    prepare_external_report,
    release_external_report,
)
from corridor.report import build_report
from corridor.review_packets import NEEDS_COORDINATION, resolve_review_packet
from corridor.web import auth
from corridor.web.app import app, get_session
from corridor.web.artifact_downloads import bundle_bytes
from corridor.web.artifact_responses import (
    ARTIFACT_BYTE_ROUTES,
    ARTIFACT_RESPONSE_BUILDERS,
    FRAMEWORK_ROUTES,
    PAGE_RESPONSE_BUILDERS,
    UNAMBIGUOUS_RESPONSE_KINDS,
)

from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    modify,
    register_baseline,
    register_source_row,
    subject,
    support,
)

from test_issue_section import (  # noqa: F401 -- fixtures used by name
    adopted,
    configure,
    prepare,
    store,
)


WEB_SOURCE = Path(__file__).resolve().parents[1] / "src/corridor/web"
APP_SOURCE = WEB_SOURCE / "app.py"

OPERATOR = HumanPrincipal("local:operations")
MEMBER = HumanPrincipal("local:artifact-member")
OUTSIDER = HumanPrincipal("local:artifact-outsider")
MEMBER_EMAIL = "member@example.test"
OUTSIDER_EMAIL = "outsider@example.test"
MEMBERSHIP = [access.COORDINATION, access.EXTERNAL_RELEASE]

#: An id no row has. The sweep uses it everywhere, which is exactly why the
#: sweep is not the whole guard: an ungated route answers "no such thing" to a
#: stranger and to a member alike.
NO_SUCH_ID = "999999"

#: The one project gate in ``app.py``. Everything slug-addressed resolves its
#: project through ``_project``, which calls this; ``page_image`` reaches the
#: project through the document and calls it directly.
PROJECT_GATE = "_authorize"

#: The dependency that resolves the signed-in person and, on an unsafe
#: method, checks the request-forgery token.
PRINCIPAL_DEPENDENCY = "get_human_principal"

#: How the sweep addresses each path parameter an artifact route declares.
#: A parameter missing from here fails ``test_every_artifact_route_is_
#: addressable`` by name, which is the instruction to teach the guard rather
#: than the licence to skip the route.
PARAMETER_VALUES = {
    "slug": "the project the artifact belongs to",
    "artifact_id": NO_SUCH_ID,
    "release_id": NO_SUCH_ID,
    "document_id": "a document of that project",
    "page_no": "1",
    "candidate_id": NO_SUCH_ID,
    "issue_number": NO_SUCH_ID,
    "artifact_type": "updated_ucm",
    "request_id": NO_SUCH_ID,
}


# --- reconciling the declaration with the routes ----------------------------


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    """Every bare name this body calls, through an attribute or not."""

    names: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if isinstance(call.func, ast.Name):
            names.add(call.func.id)
        elif isinstance(call.func, ast.Attribute):
            names.add(call.func.attr)
    return names


def _response_constructions(tree: ast.Module):
    """``(owning top-level function, response class, line)`` for every one.

    Attributed to the *outermost* enclosing function, because that is what the
    declaration names. A construction owned by no function is reported rather
    than classified.
    """

    found = []

    def walk(node: ast.AST, owner) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, owner or child)
                continue
            if isinstance(child, ast.Call):
                called = child.func
                name = (
                    called.id
                    if isinstance(called, ast.Name)
                    else called.attr
                    if isinstance(called, ast.Attribute)
                    else None
                )
                if name is not None and name.endswith("Response"):
                    found.append((owner, name, child.lineno))
            walk(child, owner)

    walk(tree, None)
    return found


def artifact_route_reading() -> tuple[tuple[tuple[str, str, str], ...], tuple[str, ...]]:
    """The declared artifact routes as the router serves them, and every fault.

    The second element is the explicit-failure half of the contract, in #909's
    shape: a response nothing classifies, a declaration naming a function that
    builds none, a name two modules define, a registered route whose handler
    this reader cannot find, and a disagreement between the declared route set
    and the one the source derives are each reported rather than skipped.
    """

    problems: list[str] = []
    modules = {
        path: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(WEB_SOURCE.rglob("*.py"))
    }
    repository = WEB_SOURCE.parents[2]
    definitions: dict[str, list[ast.AST]] = {}
    for tree in modules.values():
        for name, node in _functions(tree).items():
            definitions.setdefault(name, []).append(node)

    declared_builders = set(ARTIFACT_RESPONSE_BUILDERS) | set(PAGE_RESPONSE_BUILDERS)
    emitters: set[str] = set()
    classified: set[str] = set()
    for path, tree in modules.items():
        for owner, response, line in _response_constructions(tree):
            where = f"{path.relative_to(repository)}:{line}"
            if owner is None:
                problems.append(
                    f"{where}: {response} is built outside any function, so "
                    "nothing declares whether it carries artifact bytes"
                )
                continue
            carries = UNAMBIGUOUS_RESPONSE_KINDS.get(response)
            if carries is None:
                classified.add(owner.name)
                if owner.name in ARTIFACT_RESPONSE_BUILDERS:
                    carries = True
                elif owner.name in PAGE_RESPONSE_BUILDERS:
                    carries = False
                else:
                    problems.append(
                        f"{where}: {owner.name} builds a {response}, which may "
                        "carry artifact bytes or a page, and neither "
                        "ARTIFACT_RESPONSE_BUILDERS nor PAGE_RESPONSE_BUILDERS "
                        "in corridor.web.artifact_responses says which"
                    )
                    continue
            if carries:
                emitters.add(owner.name)
    problems += [
        f"{name}: declared in corridor.web.artifact_responses as a response "
        "builder and builds no response whose class leaves the question open"
        for name in sorted(declared_builders - classified)
    ]

    while True:
        grown = {
            name
            for name, nodes in definitions.items()
            if name not in emitters
            and any(_called_names(node) & emitters for node in nodes)
        }
        if not grown:
            break
        emitters |= grown
    problems += [
        f"{name}: reaches an artifact response and is defined "
        f"{len(definitions[name])} times under src/corridor/web, so which "
        "definition a caller reaches cannot be read here"
        for name in sorted(emitters)
        if len(definitions[name]) > 1
    ]

    handlers = _functions(modules[APP_SOURCE])
    registered = [route for route in app.routes if isinstance(route, Route)]
    derived: set[str] = set()
    for route in registered:
        if route.name != route.endpoint.__name__:
            problems.append(
                f"{route.path} is registered as {route.name!r} and handled by "
                f"{route.endpoint.__name__}, so a declaration naming one names "
                "neither reliably"
            )
            continue
        if route.name in FRAMEWORK_ROUTES:
            continue
        if route.name not in handlers:
            problems.append(
                f"{route.path} ({route.name}) is registered and is not a "
                "function of src/corridor/web/app.py, so whether it answers "
                "with artifact bytes cannot be read"
            )
            continue
        if route.name in emitters:
            derived.add(route.name)

    served = {route.name for route in registered}
    problems += [
        f"{name}: declared in FRAMEWORK_ROUTES and the application registers "
        "no route of that name"
        for name in sorted(FRAMEWORK_ROUTES - served)
    ]
    problems += [
        f"{name}: answers with artifact bytes and is not in "
        "corridor.web.artifact_responses.ARTIFACT_BYTE_ROUTES"
        for name in sorted(derived - ARTIFACT_BYTE_ROUTES)
    ]
    problems += [
        f"{name}: declared in ARTIFACT_BYTE_ROUTES and nothing its call path "
        "builds carries artifact bytes"
        for name in sorted(ARTIFACT_BYTE_ROUTES - derived)
    ]

    routes = []
    for name in sorted(ARTIFACT_BYTE_ROUTES):
        identity = served_route_identity(app.routes, name)
        if identity is None:
            problems.append(
                f"{name}: declared in ARTIFACT_BYTE_ROUTES and the application "
                "serves no route of that name with one GET or POST method"
            )
            continue
        template, method = identity
        routes.append((method, template, name))
    return tuple(sorted(routes)), tuple(problems)


ARTIFACT_ROUTES, RECONCILIATION_PROBLEMS = artifact_route_reading()


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


def enrolled(session, slug: str, principal, email: str, name: str) -> Project:
    """One synthetic project this person is a full member of."""

    row = Project(
        slug=f"{slug}-{uuid4().hex[:8]}", name=name.title(), is_synthetic=True
    )
    session.add(row)
    session.flush()
    join(session, row, principal, email, name)
    return row


def join(session, project: Project, principal, email: str, name: str) -> None:
    access.enroll_member(
        session,
        project_id=project.id,
        email=email,
        principal=principal,
        display_name=name.title(),
        designations=MEMBERSHIP,
        operator=OPERATOR,
    )


@pytest.fixture
def theirs(session) -> Project:
    """The project the refused requester really is a member of."""

    return enrolled(session, "artifact-theirs", OUTSIDER, OUTSIDER_EMAIL, "outsider")


@pytest.fixture
def two_projects(session, theirs):
    """One project holding an artifact-bearing document, and one that does not."""

    ours = enrolled(session, "artifact-ours", MEMBER, MEMBER_EMAIL, "member")
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


# --- the guard ---------------------------------------------------------------


def test_the_declaration_and_the_registered_routes_agree():
    """The discovery is the guard, so it fails loudly rather than finding none.

    A refactor that moved a response helper, or a new route that starts
    answering with a file, changes one of the two sides of this and fails here
    with the name of what changed.
    """

    assert RECONCILIATION_PROBLEMS == (), "\n".join(RECONCILIATION_PROBLEMS)
    assert ARTIFACT_ROUTES, (
        "no route in src/corridor/web was found to answer with artifact bytes, "
        "which cannot be true while the product offers downloads"
    )
    served = {(method, path) for method, path, _handler in ARTIFACT_ROUTES}
    assert ("GET", "/reports/{slug}/prepared/{artifact_id}/download") in served
    assert ("GET", "/page-image/{document_id}/{page_no}") in served
    assert (
        "GET",
        "/work/{slug}/issue/packages/{issue_number}/artifacts/{artifact_type}",
    ) in served
    assert ("GET", "/work/{slug}/issue/packages/{issue_number}/bundle") in served
    # The two the media-type reading called pages: the retained content of a
    # sent request is served as `text/plain`, and a chase list as
    # `application/json`, through the same builder as the workbook.
    assert ("GET", "/work/{slug}/follow-up/sent/{request_id}") in served


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
    """No session, the wrong project, and then the member who may have it.

    Every identifier here is one no row has, so this proves the status
    contract over *every* declared route and nothing about bytes. The families
    below are where bytes exist to be leaked.
    """

    project, document_id = two_projects
    url = address(path, project, document_id)

    anonymous = client.request(method, url, follow_redirects=False)
    assert anonymous.status_code == 401, (
        f"{handler} answered {anonymous.status_code} with no session at all"
    )
    assert not anonymous.headers.get("content-disposition"), (
        f"{handler} offered a caller with no session a file to save"
    )

    sign_in(client, sender, OUTSIDER_EMAIL)
    outsider = client.request(method, url, follow_redirects=False)
    assert outsider.status_code in (403, 404), (
        f"{handler} answered {outsider.status_code} to somebody enrolled on a "
        "different project"
    )
    assert not outsider.headers.get("content-disposition"), (
        f"{handler} offered somebody enrolled on a different project a file"
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


# --- one real artifact per family -------------------------------------------


@dataclass(frozen=True)
class RealArtifact:
    """One family's retained bytes, and the requests that name them.

    ``elsewhere`` is the same artifact's identifiers under the *requester's*
    own project slug, which is the request a member of another project can
    actually make: their own slug passes the roster gate, so only the row
    filter behind it stands between them and these bytes. ``None`` where the
    route carries no slug to substitute.
    """

    url: str
    content: bytes
    elsewhere: str | None = None
    discloses: tuple[str, ...] = field(default_factory=tuple)


def refused_except_for_its_member(client, sender, artifact: RealArtifact) -> None:
    """Points 1 to 3: nobody, the wrong project, then the person entitled.

    The member comes last because the project gate declares this transaction's
    one authorization scope (#662) and the two refusals above it declare none:
    the anonymous request never reaches a handler, and the outsider is refused
    by the roster before the partition is asked for.
    """

    client.cookies.clear()
    anonymous = client.get(artifact.url, follow_redirects=False)
    assert anonymous.status_code == 401, anonymous.text
    assert artifact.content not in anonymous.content
    assert not anonymous.headers.get("content-disposition")

    sign_in(client, sender, OUTSIDER_EMAIL)
    outsider = client.get(artifact.url, follow_redirects=False)
    assert outsider.status_code in (403, 404), outsider.text
    assert artifact.content not in outsider.content
    assert not outsider.headers.get("content-disposition")
    for disclosure in artifact.discloses:
        assert disclosure not in outsider.text, (
            f"a refusal told somebody enrolled on a different project {disclosure!r}"
        )

    sign_in(client, sender, MEMBER_EMAIL)
    served = client.get(artifact.url, follow_redirects=False)
    assert served.status_code == 200, served.text
    assert served.content == artifact.content


def refused_under_the_requesters_own_slug(client, sender, artifact: RealArtifact) -> None:
    """Point 4: their own project, this artifact's identifiers.

    A separate test from the three above, because this request passes the
    roster gate and therefore declares the requester's project as the
    transaction's authorization scope, which the artifact's own project cannot
    also be.
    """

    assert artifact.elsewhere is not None
    client.cookies.clear()
    sign_in(client, sender, OUTSIDER_EMAIL)

    answered = client.get(artifact.elsewhere, follow_redirects=False)

    # Not one status, because the row filter is not one command: the spine
    # routes answer a foreign identifier as missing, and the legacy report
    # family answers with the retrieval command's own refusal, which the
    # refusal adapter renders as a conflict. What every one of them has in
    # common is the part that matters here.
    assert answered.status_code in (403, 404, 409), answered.text
    assert artifact.content not in answered.content
    assert not answered.headers.get("content-disposition")


# --- the internal working workbook ------------------------------------------


def test_the_internal_workbook_is_served_to_its_member_and_nobody_else(
    session, client, sender, theirs
):
    """The one artifact a project produces with no fixture at all.

    Its bytes are rendered on request rather than retained, so what is
    asserted is that a member receives a real workbook and that the refusals
    refuse one. The slug is this route's only identifier, so a requester's own
    slug reaches their own workbook and there is no fourth case to write.
    """

    ours = enrolled(session, "artifact-ours", MEMBER, MEMBER_EMAIL, "member")
    url = f"/internal-report/{ours.slug}/workbook.xlsx"

    anonymous = client.get(url, follow_redirects=False)
    assert anonymous.status_code == 401
    assert not anonymous.headers.get("content-disposition")

    sign_in(client, sender, OUTSIDER_EMAIL)
    outsider = client.get(url, follow_redirects=False)
    assert outsider.status_code == 404, outsider.text
    assert not outsider.headers.get("content-disposition")

    sign_in(client, sender, MEMBER_EMAIL)
    served = client.get(url, follow_redirects=False)
    assert served.status_code == 200, served.text
    # A real workbook, not an empty body: the zip container every xlsx is.
    assert served.content.startswith(b"PK")
    assert served.headers["content-disposition"].startswith("attachment;")


# --- the registered original of a source document (#831) --------------------


ORIGINAL = b"%PDF-1.7\nthe registered original\n%%EOF"


def registered_original(session, theirs) -> RealArtifact:
    """One source document whose bytes really are in the store."""

    ours = enrolled(session, "artifact-ours", MEMBER, MEMBER_EMAIL, "member")
    digest = sha256(ORIGINAL).hexdigest()
    store_bytes(ORIGINAL, sha256=digest, suffix=".pdf")
    document = Document(
        project_id=ours.id,
        sha256=digest,
        filename="registered-original.pdf",
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    return RealArtifact(
        url=f"/sources/{ours.slug}/document/{document.id}/original",
        content=ORIGINAL,
        elsewhere=f"/sources/{theirs.slug}/document/{document.id}/original",
        discloses=(digest, "registered-original.pdf"),
    )


def test_the_registered_original_answers_only_its_own_member(
    session, client, sender, theirs
):
    """#831 hands back the bytes a customer registered, to that project only."""

    refused_except_for_its_member(client, sender, registered_original(session, theirs))


def test_the_registered_original_is_not_reachable_under_another_slug(
    session, client, sender, theirs
):
    """The document is resolved inside the gate, never by id alone."""

    refused_under_the_requesters_own_slug(
        client, sender, registered_original(session, theirs)
    )


# --- one rendered source page image -----------------------------------------

#: A 1x1 PNG, so the bytes served are a real image rather than a marker.
PAGE_IMAGE = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d76360606000000004000151b3b2130000000049454e"
    "44ae426082"
)


def page_image(session, tmp_path) -> RealArtifact:
    """One document page whose rendered image really is on disk."""

    ours = enrolled(session, "artifact-ours", MEMBER, MEMBER_EMAIL, "member")
    document = Document(
        project_id=ours.id,
        sha256=sha256(b"page-image-source").hexdigest(),
        filename="scanned.pdf",
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    rendered = tmp_path / "page-1.png"
    rendered.write_bytes(PAGE_IMAGE)
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text="a scanned page",
            image_path=str(rendered),
        )
    )
    session.flush()
    return RealArtifact(
        url=f"/page-image/{document.id}/1", content=PAGE_IMAGE, discloses=("scanned.pdf",)
    )


def test_the_page_image_answers_only_a_member_of_its_documents_project(
    session, client, sender, tmp_path, theirs
):
    """Addressed by document id alone, so the gate reaches the project (#331).

    There is no slug on this route to combine with a foreign id, which makes
    the outsider's request here both the third case and the fourth: they name
    this project's document from their own session and are refused.
    """

    refused_except_for_its_member(client, sender, page_image(session, tmp_path))


# --- the prepared and released External Report ------------------------------


PREPARED_PDF = b"%PDF-1.7\nthe prepared report\n%%EOF"


def prepared_report(session, theirs):
    """One prepared External Report artifact, with its exact retained bytes."""

    ours = enrolled(session, "artifact-ours", MEMBER, MEMBER_EMAIL, "member")
    today = date(2026, 8, 13)
    artifact = prepare_external_report(
        session,
        project_id=ours.id,
        rendered=RenderedExternalReport(
            artifact_name=f"{ours.slug}-{today.isoformat()}.pdf",
            pdf_bytes=PREPARED_PDF,
            report=build_report(session, ours.id, today=today),
        ),
    )
    session.flush()
    return ours, artifact


def test_the_prepared_report_answers_only_its_own_member(
    session, client, sender, theirs
):
    """Both readings of the same retained bytes: the download and the preview."""

    ours, artifact = prepared_report(session, theirs)

    for suffix in ("download", "preview"):
        refused_except_for_its_member(
            client,
            sender,
            RealArtifact(
                url=f"/reports/{ours.slug}/prepared/{artifact.id}/{suffix}",
                content=PREPARED_PDF,
                discloses=(artifact.pdf_sha256, artifact.artifact_name),
            ),
        )


def test_the_prepared_report_is_not_reachable_under_another_slug(
    session, client, sender, theirs
):
    """An artifact id is a table-wide sequence; the project filter is the gate."""

    _ours, artifact = prepared_report(session, theirs)

    refused_under_the_requesters_own_slug(
        client,
        sender,
        RealArtifact(
            url="",
            content=PREPARED_PDF,
            elsewhere=f"/reports/{theirs.slug}/prepared/{artifact.id}/download",
        ),
    )


def released_report(session, theirs):
    """One released External Report receipt over those same retained bytes."""

    ours, artifact = prepared_report(session, theirs)
    receipt = release_external_report(
        session, project_id=ours.id, artifact_id=artifact.id, principal=MEMBER
    )
    session.flush()
    return ours, receipt


def test_the_released_report_answers_only_its_own_member(
    session, client, sender, theirs
):
    """The immutable bytes one historical release names."""

    ours, receipt = released_report(session, theirs)

    refused_except_for_its_member(
        client,
        sender,
        RealArtifact(
            url=f"/reports/{ours.slug}/releases/{receipt.id}/download",
            content=PREPARED_PDF,
            discloses=(receipt.pdf_sha256, receipt.artifact_name),
        ),
    )


def test_the_released_report_is_not_reachable_under_another_slug(
    session, client, sender, theirs
):
    """A release id names a receipt of one project, resolved with its id."""

    _ours, receipt = released_report(session, theirs)

    refused_under_the_requesters_own_slug(
        client,
        sender,
        RealArtifact(
            url="",
            content=PREPARED_PDF,
            elsewhere=f"/reports/{theirs.slug}/releases/{receipt.id}/download",
        ),
    )


# --- the exact content of a sent follow-up request (#837) -------------------


SENT_CONTENT = (
    "Dear City Water,\n\nOur record holds 21 September for U-042. Your "
    "September workbook says 15 December. Which date do you hold to?\n"
).encode("utf-8")

COORDINATION_AT = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
RETURNS_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def retained_request(session, theirs) -> RealArtifact:
    """One retained outgoing request whose sent content is in the store.

    The plan it advances is built the way the product builds one -- an
    accepted date, a source disagreeing with it, and a coordinator answering
    that child "Needs coordination" -- because ``append_outgoing_request``
    refuses a request that advances no Follow-up Plan of its own project.
    """

    ours = enrolled(session, "artifact-ours", MEMBER, MEMBER_EMAIL, "member")
    source = Rendition(session, ours, "ucm-2026-08.xlsx")
    organization, _ = source.capture(
        fact_type="external_org", value="City Water", subject_key=subject(42)
    )
    accept_baseline_fact(session, ours, organization)
    promised, _ = source.capture(
        fact_type="committed_date", value="2026-09-21", subject_key=subject(42)
    )
    revision = accept_baseline_fact(session, ours, promised)
    baseline = register_baseline(session, ours, source.document, revision)
    register_source_row(
        session, ours, baseline, row_number=42, business_identity="U-042"
    )
    # Two sources disagreeing with the accepted date and with each other, which
    # is the shape that focuses the item a coordinator answers.
    for name, family, source_revision, value in (
        ("ucm-2026-09.xlsx", "ucm-workbook", "2026-09", "2026-12-15"),
        ("minutes-2026-09-02.pdf", "meeting-minutes", "2026-09-02", "2027-01-20"),
    ):
        later = Rendition(session, ours, name)
        fact, segment = later.capture(
            fact_type="committed_date", value=value, subject_key=subject(42)
        )
        support(session, ours, fact, segment)
        append_deltas(
            session,
            ours,
            later,
            source_revision=source_revision,
            source_family=family,
            values=[
                modify(
                    subject_key=subject(42),
                    field_name="committed_date",
                    accepted_value="2026-09-21",
                    proposed_value=value,
                    baseline_revision=revision,
                )
            ],
            is_complete_enumerative_source=False,
            row_accounting_sealed=False,
        )
    adopt_project_baseline(
        session,
        project_id=ours.id,
        adopted_by_principal=MEMBER.subject,
        baseline_source_sha256=source.document.sha256,
        importer_identity="artifact_authorization_fixture",
        importer_version="v1",
        idempotency_key=f"adopt:{uuid4().hex[:10]}",
    )
    session.expire_all()
    reading = read_review_items(session, project_id=ours.id, as_of=COORDINATION_AT)
    item = next(row for row in reading.items if row.focused)
    outcome = resolve_review_packet(
        session,
        focused_request(
            reading,
            item,
            principal=MEMBER,
            decided_at=COORDINATION_AT,
            answers=[
                FocusedAnswer(
                    delta_id=child.delta_id,
                    outcome=NEEDS_COORDINATION,
                    question="Which date does the utility actually hold to?",
                    responsible_organization="City Water",
                    return_date=RETURNS_AT,
                )
                for child in item.children
            ],
        ),
    )
    assert outcome.status == "saved", outcome
    session.expire_all()
    plans = tuple(
        session.scalars(
            select(DeltaFollowUpPlan.id).where(DeltaFollowUpPlan.project_id == ours.id)
        )
    )
    assert plans
    retained = retain_outgoing_request(
        session,
        project_id=ours.id,
        follow_up_plan_ids=plans,
        external_organization="City Water",
        question="Which date do you hold to?",
        covered_subject_keys=(subject(42),),
        sent_content=SENT_CONTENT,
        sent_on=date(2026, 9, 1),
        sent_by_principal=MEMBER.subject,
        recorded_by_principal=MEMBER.subject,
        expected_response_by=date(2026, 9, 20),
        idempotency_key=f"sent:{uuid4().hex[:10]}",
    )
    session.flush()
    return RealArtifact(
        url=f"/work/{ours.slug}/follow-up/sent/{retained.id}",
        content=SENT_CONTENT,
        elsewhere=f"/work/{theirs.slug}/follow-up/sent/{retained.id}",
        discloses=(retained.content_sha256,),
    )


def test_the_retained_sent_content_answers_only_its_own_member(
    session, client, sender, theirs
):
    """#837 serves the message itself, as ``text/plain``, to that project only.

    This is the family the media-type reading could not see: the response the
    member receives declares ``text/plain``, which the old guard classified as
    a page, so a leak of the whole retained message would have passed its
    "handed bytes" assertion.
    """

    artifact = retained_request(session, theirs)

    refused_except_for_its_member(client, sender, artifact)


def test_the_retained_sent_content_is_not_reachable_under_another_slug(
    session, client, sender, theirs
):
    """The request row is resolved with its project, never by id alone."""

    refused_under_the_requesters_own_slug(
        client, sender, retained_request(session, theirs)
    )


# --- the prepared candidate, the approved issue, and its bundle (#830) ------


def issue_member(session, adopted) -> None:
    """Enrol this guard's own member and outsider on the adopted project."""

    join(session, adopted.project, MEMBER, MEMBER_EMAIL, "member")


def approved(session, adopted, store):
    """One approved package, authorized by #533's command rather than a route.

    The approval route would declare this project as the transaction's one
    authorization scope, which the "their own slug" case below needs to spend
    on the requester's project instead.
    """

    candidate = prepare(session, adopted, store)
    authorize_release_package(
        session,
        project_id=adopted.project.id,
        candidate_id=candidate.id,
        releaser=MEMBER,
        authorized_at=datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc),
        store=store,
    )
    session.flush()
    package = session.scalars(
        select(ReleasePackage).where(ReleasePackage.project_id == adopted.project.id)
    ).one()
    return candidate, package


@pytest.mark.parametrize(
    "artifact_type",
    ("updated_ucm", "weekly_coordination_report", "chase_list"),
)
def test_a_prepared_candidate_artifact_answers_only_its_own_member(
    session, client, sender, adopted, store, theirs, artifact_type
):
    """Every configured type, including the two the media type called pages.

    ``weekly_coordination_report`` is served as ``text/plain`` and
    ``chase_list`` as ``application/json``; both are a customer's own data, and
    both were outside the old reading's idea of a file.
    """

    configure(session, adopted)
    issue_member(session, adopted)
    candidate = prepare(session, adopted, store)
    (sealed,) = [
        one for one in candidate_set(session, candidate)
        if one.artifact_type == artifact_type
    ]

    refused_except_for_its_member(
        client,
        sender,
        RealArtifact(
            url=(
                f"/work/{adopted.project.slug}/issue/candidates/{candidate.id}"
                f"/artifacts/{artifact_type}"
            ),
            content=store.get(sealed.storage_key, sha256=sealed.content_sha256),
            discloses=(sealed.content_sha256, sealed.storage_key),
        ),
    )


def test_a_prepared_candidate_artifact_is_not_reachable_under_another_slug(
    session, client, sender, adopted, store, theirs
):
    """A candidate id is a table-wide sequence; the project filter is the gate."""

    configure(session, adopted)
    issue_member(session, adopted)
    candidate = prepare(session, adopted, store)
    sealed = candidate_set(session, candidate)[0]

    refused_under_the_requesters_own_slug(
        client,
        sender,
        RealArtifact(
            url="",
            content=store.get(sealed.storage_key, sha256=sealed.content_sha256),
            elsewhere=(
                f"/work/{theirs.slug}/issue/candidates/{candidate.id}"
                f"/artifacts/{sealed.artifact_type}"
            ),
        ),
    )


@pytest.mark.parametrize(
    "artifact_type",
    ("updated_ucm", "weekly_coordination_report", "chase_list"),
)
def test_an_approved_issue_artifact_answers_only_its_own_member(
    session, client, sender, adopted, store, theirs, artifact_type
):
    """The sealed bytes of an approved package, by the issue number a person uses."""

    configure(session, adopted)
    issue_member(session, adopted)
    _candidate, package = approved(session, adopted, store)
    (sealed,) = [
        one for one in package_set(session, package)
        if one.artifact_type == artifact_type
    ]

    refused_except_for_its_member(
        client,
        sender,
        RealArtifact(
            url=(
                f"/work/{adopted.project.slug}/issue/packages/"
                f"{int(package.sequence_number)}/artifacts/{artifact_type}"
            ),
            content=store.get(sealed.storage_key, sha256=sealed.content_sha256),
            discloses=(sealed.content_sha256, sealed.storage_key),
        ),
    )


def test_an_approved_issue_artifact_is_not_reachable_under_another_slug(
    session, client, sender, adopted, store, theirs
):
    """Every project has an issue 1 or none, so the number alone reaches nothing."""

    configure(session, adopted)
    issue_member(session, adopted)
    _candidate, package = approved(session, adopted, store)
    sealed = package_set(session, package)[0]

    refused_under_the_requesters_own_slug(
        client,
        sender,
        RealArtifact(
            url="",
            content=store.get(sealed.storage_key, sha256=sealed.content_sha256),
            elsewhere=(
                f"/work/{theirs.slug}/issue/packages/"
                f"{int(package.sequence_number)}/artifacts/{sealed.artifact_type}"
            ),
        ),
    )


def test_an_approved_issue_bundle_answers_only_its_own_member(
    session, client, sender, adopted, store, theirs
):
    """ADR-0086's unit is the set, so the set is one archive with one gate."""

    configure(session, adopted)
    issue_member(session, adopted)
    _candidate, package = approved(session, adopted, store)
    members = tuple(
        (one, store.get(one.storage_key, sha256=one.content_sha256))
        for one in package_set(session, package)
    )

    refused_except_for_its_member(
        client,
        sender,
        RealArtifact(
            url=(
                f"/work/{adopted.project.slug}/issue/packages/"
                f"{int(package.sequence_number)}/bundle"
            ),
            content=bundle_bytes(members),
            discloses=tuple(one.content_sha256 for one, _data in members),
        ),
    )


def test_an_approved_issue_bundle_is_not_reachable_under_another_slug(
    session, client, sender, adopted, store, theirs
):
    """The bundle is addressed exactly as its members are, and filtered so."""

    configure(session, adopted)
    issue_member(session, adopted)
    _candidate, package = approved(session, adopted, store)
    members = tuple(
        (one, store.get(one.storage_key, sha256=one.content_sha256))
        for one in package_set(session, package)
    )

    refused_under_the_requesters_own_slug(
        client,
        sender,
        RealArtifact(
            url="",
            content=bundle_bytes(members),
            elsewhere=(
                f"/work/{theirs.slug}/issue/packages/"
                f"{int(package.sequence_number)}/bundle"
            ),
        ),
    )
