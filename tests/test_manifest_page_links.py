"""A link printed on an enabled page has to go somewhere the pilot serves (#848).

On an enforcing deployment the live pilot answers the routes in
``corridor.web_boundary`` and answers everything else exactly as it answers a
missing project. The customer-journey audit found the consequence on the
screen: **Source history** is printed on the Work page and on the Record page,
and the Review rows print a link to the exact source, and on an enforcing
deployment all five of those answered 404. Nothing failed, because nothing was
checking that a link a page prints is a route the deployment serves.

This is that check, kept as a ratchet: the offenders are recorded, they may
leave, and none may join. #824 admitted upload, preview, confirmation, the
source register and the Review source link, so those five names are gone from
the list below.

**What this reads, and why it now reads two things.** #848 read the ``href``
and ``action`` attributes written in the templates a manifest route renders.
That reading is conservative -- it covers a template whatever state it would be
rendered in -- and it is also blind twice over. A link whose whole target is an
expression, ``href="{{ view.open_url }}"``, is a target it cannot resolve. And
a control a page prints only in *some* states is read as printed in all of
them, which is the opposite error: the intake preview page carries the markup
for the optional model-assisted draft, a route the pilot deliberately does not
serve, and prints it only where the deployment would answer it.

So #824 added the second reading the ticket asked for: representative states,
rendered through the application, with the links read off the page a person
would actually receive. The two compose by one rule, stated once:

    A template is read from its source unless this guard renders a
    representative state of it. Where it does, what that state printed is what
    is measured.

``RENDERED_STATES`` names the templates covered that way and why each needs to
be. Everything else stays on the source reading, which is the conservative
answer and the right default: a page nobody here renders is held to every
string it writes.

The four kinds #824 asks to be distinguished are distinguished here:

- an **internal route**, which must be a route the manifest serves;
- a **fragment**, which goes nowhere and is left alone;
- an **approved external link**, which needs a decision and a reason, and of
  which there are none on a manifest page today;
- a **download endpoint**, which is an internal route like any other and must
  be in the manifest before a page may offer it. There are none today either,
  and #830 is the ticket that adds the first.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from corridor import web_boundary
from corridor.config import settings
from corridor.models import Project
from corridor.packet_review import read_review_items
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import SOURCE_REVISION
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)

from access_support import seed_membership
from later_revision_support import BASELINE_ROWS, workbook_bytes
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
from ratchet_support import assert_ratchet


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = REPO_ROOT / "src/corridor/web/app.py"
TEMPLATE_ROOT = REPO_ROOT / "src/corridor/web/templates"


#: Links on a manifest page that go to a route the live pilot does not serve.
#: **This list may fall and may never rise.** What remains is the legacy
#: item-per-record Work List (ADR-0035), which an enforcing deployment refuses
#: before the template rather than rendering -- so these three are read from
#: source, conservatively, rather than from a state this guard builds.
UNADMITTED_LINKS: frozenset[str] = frozenset(
    {
        "work_list.html: GET /internal-report/{}",
        "work_list.html: GET /ledger/{}",
        "work_list.html: GET /reports/{}",
    }
)

#: Links on a manifest page whose whole target is a template expression, which
#: a reading of the template cannot resolve to a route. **This list may fall
#: and may never rise.** Two of #848's three are gone: ``review.html``'s is
#: resolved by rendering the Review page, and ``_primitives.html``'s belongs to
#: a macro no template calls, so no page can print it. The third is on the
#: legacy Work List, which has no enforcing-deployment state to render.
COMPUTED_LINKS: frozenset[str] = frozenset(
    {
        "work_list.html: GET {{ view.action_url }}",
    }
)

#: External destinations a manifest page is allowed to offer, and why.
#: **Empty, deliberately**: sending a coordinator off the product mid-workflow
#: is a decision, and no page in the pilot set makes it today.
APPROVED_EXTERNAL_LINKS: dict[str, str] = {}


#: Why each rendered template needs a state rather than its source, so a later
#: reader can tell a deliberate rendering from an accumulating exemption. A
#: template named here must be rendered by the ``rendered`` fixture below, and
#: one rendered but not named is an unexplained narrowing; both are asserted.
RENDERED_STATES: dict[str, str] = {
    "review.html": (
        "the Review reading's own links: the item's `{{ view.open_url }}` is "
        "a whole expression, and the exact-source links only exist for an "
        "opened item, so both states are rendered"
    ),
    "source_preview.html": (
        "the intake preview offers the optional model-assisted draft only "
        "where the deployment serves that route (#824). The markup is in the "
        "template either way, so the source reading cannot tell an offered "
        "control from a withheld one"
    ),
}


_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)
_ATTRIBUTE = re.compile(r'(?P<kind>href|action)="(?P<value>[^"]*)"')
_FORM_METHOD = re.compile(r'<form[^>]*\bmethod="(?P<method>[a-zA-Z]+)"', re.I)
_IMPORTS = re.compile(r'\{%-?\s*(?:import|include|extends|from)\s+"(?P<name>[^"]+)"')
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_MACRO = re.compile(r"\{%-?\s*macro\s+(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
_ENDMACRO = re.compile(r"\{%-?\s*endmacro\s*-?%\}")


def _functions(tree: ast.Module):
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


def _templates_named(node: ast.AST) -> set[str]:
    named = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr != "TemplateResponse":
            continue
        for argument in call.args:
            if isinstance(argument, ast.Constant) and str(argument.value).endswith(
                ".html"
            ):
                named.add(argument.value)
    return named


def manifest_page_templates() -> frozenset[str]:
    """Every template a route in the live-pilot manifest can render.

    Transitive through the module's own helpers, because the two pages this
    check exists for are rendered that way: ``/work/{slug}`` renders the
    adopted project's week through ``_project_workflow_response``, and every
    Review route renders through ``_render_review_response``.
    """

    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    functions = _functions(tree)
    reached: dict[str, set[str]] = {
        name: _templates_named(node) for name, node in functions.items()
    }
    while True:
        grown = False
        for name, node in functions.items():
            for called in _called_names(node) & set(functions):
                if not reached[called] <= reached[name]:
                    reached[name] |= reached[called]
                    grown = True
        if not grown:
            break

    enabled = {template for _method, template in web_boundary.PILOT_ROUTES}
    named = set()
    for name, node in functions.items():
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            if not isinstance(function, ast.Attribute):
                continue
            if not isinstance(function.value, ast.Name) or function.value.id != "app":
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                continue
            if decorator.args[0].value in enabled:
                named |= reached[name]
    return frozenset(_with_imports(named))


def _with_imports(templates: set[str]) -> set[str]:
    """The templates themselves and every partial they pull in, transitively."""

    seen: set[str] = set()
    pending = list(templates)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = TEMPLATE_ROOT / name
        if not path.exists():
            continue
        pending.extend(_IMPORTS.findall(path.read_text(encoding="utf-8")))
    return seen


def _normalize(target: str) -> str:
    """One link's path, with its parameters and its query taken off.

    ``/work/{{ project.slug }}?from=portfolio`` and the manifest's
    ``/work/{slug}`` are the same route, and the only way to compare them is
    to spell both the same way. A *rendered* page carries the slug itself
    rather than an expression, so the path it printed is matched against the
    application's own routes and spelled back as that route -- which is how
    one reading and the other end up comparable.
    """

    path = _EXPRESSION.sub("{}", target)
    path = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", "{}", path)
    path = path.split("?", 1)[0].split("#", 1)[0]
    matched = _served_route(path)
    if matched is None:
        return path
    return re.sub(r"\{[^}]*\}", "{}", matched)


def _served_route(path: str) -> str | None:
    """The route template the application serves this path under, if any."""

    from starlette.routing import Route

    for route in app.routes:
        if isinstance(route, Route) and route.path_regex.match(path):
            return route.path
    return None


def _method_of(body: str, position: int, kind: str) -> str:
    """GET for a link; a form's own declared method for a form action."""

    if kind == "href":
        return "GET"
    opening = body.rfind("<form", 0, position)
    if opening < 0:
        return "GET"
    match = _FORM_METHOD.match(body, opening)
    return match.group("method").upper() if match else "GET"


def links_in(body: str) -> tuple[tuple[str, str, int], ...]:
    """``("METHOD path", kind, offset)`` for every link this markup writes.

    One reader for both halves, because the question is the same one: a
    rendered page is markup with no expressions left in it, so ``computed``
    simply never comes back from one.
    """

    found = []
    for match in _ATTRIBUTE.finditer(body):
        value = match.group("value").strip()
        method = _method_of(body, match.start(), match.group("kind"))
        if not value or value.startswith("#"):
            kind = "fragment"
        elif value.startswith("/"):
            kind = "internal"
        elif _SCHEME.match(value):
            kind = "external"
        else:
            kind = "computed"
        target = _normalize(value) if kind == "internal" else value
        found.append((f"{method} {target}", kind, match.start()))
    return tuple(found)


def _uncalled_macro_attributes(body: str) -> set[int]:
    """Offsets of attributes inside a macro of this file nothing ever calls.

    A macro's body is markup no page sends until a call site sends it. The
    one attribute #848 could not resolve is a macro *parameter* --
    ``evidence_reference(..., href="")`` -- and the honest answer is not that
    its target is unknown but that no page prints it at all, because no
    template calls the macro. Proved rather than assumed: every template is
    searched for the call.
    """

    everywhere = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TEMPLATE_ROOT.glob("*.html"))
    )
    uncalled: set[int] = set()
    for opening in _MACRO.finditer(body):
        name = opening.group("name")
        closing = _ENDMACRO.search(body, opening.end())
        end = closing.end() if closing else len(body)
        # Every mention of the name, less the `{% macro name(` that defines
        # it -- a macro defined and called in the same template is called, and
        # one mentioned only by its own definition is not.
        mentions = len(re.findall(rf"\b{re.escape(name)}\s*\(", everywhere))
        definitions = len(
            re.findall(rf"\{{%-?\s*macro\s+{re.escape(name)}\s*\(", everywhere)
        )
        if mentions > definitions:
            continue
        uncalled.update(range(opening.start(), end))
    return uncalled


def links_on_manifest_pages(
    rendered: dict[str, str],
) -> tuple[tuple[str, str, str], ...]:
    """``(template, "METHOD path", kind)`` for every link a manifest page prints.

    Rendered where a representative state exists, read from the template's own
    source everywhere else.
    """

    found = []
    for template in sorted(manifest_page_templates()):
        if template in rendered:
            for link, kind, _offset in links_in(rendered[template]):
                found.append((template, link, kind))
            continue
        path = TEMPLATE_ROOT / template
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8")
        uncalled = _uncalled_macro_attributes(body)
        for link, kind, offset in links_in(body):
            if offset in uncalled:
                continue
            found.append((template, link, kind))
    return tuple(sorted(set(found)))


def _manifest_routes() -> frozenset[str]:
    return frozenset(
        f"{method} {_normalize(template)}"
        for method, template in web_boundary.PILOT_ROUTES
    )


def unadmitted_links(rendered: dict[str, str]) -> frozenset[str]:
    """Internal links a manifest page prints that the pilot does not serve."""

    served = _manifest_routes()
    return frozenset(
        f"{template}: {link}"
        for template, link, kind in links_on_manifest_pages(rendered)
        if kind == "internal" and link not in served
    )


def computed_links(rendered: dict[str, str]) -> frozenset[str]:
    """Links a manifest page prints whose target this reading cannot resolve."""

    return frozenset(
        f"{template}: {link}"
        for template, link, kind in links_on_manifest_pages(rendered)
        if kind == "computed"
    )


def unapproved_external_links(rendered: dict[str, str]) -> frozenset[str]:
    """External destinations a manifest page offers with no recorded decision."""

    return frozenset(
        f"{template}: {link}"
        for template, link, kind in links_on_manifest_pages(rendered)
        if kind == "external" and link.split(" ", 1)[1] not in APPROVED_EXTERNAL_LINKS
    )


# --- The representative rendered states ------------------------------------

COORDINATOR = HumanPrincipal("local:link-ratchet-coordinator")
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def enforcing(monkeypatch, tmp_path):
    """The deployment these states belong to: the boundary declared."""

    monkeypatch.setattr(settings, "live_pilot_web_boundary", True)
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))


@pytest.fixture
def linked_project(session) -> Project:
    row = Project(
        slug=f"link-ratchet-{uuid4().hex[:8]}",
        name="Link Ratchet",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR)
    return row


@pytest.fixture
def client(session, enforcing):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with TestClient(app) as opened:
        yield opened
    app.dependency_overrides.clear()


def _one_reviewable_change(session, project: Project) -> None:
    """One adopted row, one incoming change to it, and its external record.

    The smallest reading that prints both links this guard has to resolve: an
    unopened item carries ``open_url``, and an opened one carries the exact
    source links whose route #824 admitted.
    """

    adopted = Rendition(session, project, "ucm-2026-08.xlsx")
    incoming = Rendition(session, project, "ucm-2026-09.xlsx")
    fact, _segment = adopted.capture(
        fact_type="station_from", value="1001+00", subject_key=subject(1)
    )
    revision = accept_baseline_fact(session, project, fact)
    baseline = register_baseline(session, project, adopted.document, revision)
    register_source_row(
        session,
        project,
        baseline,
        row_number=1,
        business_identity="UC-001",
        external_system_id="UCM-00001",
        source_url="https://records.example.gov/conflict/1",
    )
    changed, changed_segment = incoming.capture(
        fact_type="station_from", value="2001+00", subject_key=subject(1)
    )
    support(session, project, changed, changed_segment)
    append_deltas(
        session,
        project,
        incoming,
        source_revision="2026-09",
        values=[
            modify(
                subject_key=subject(1),
                field_name="station_from",
                accepted_value="1001+00",
                proposed_value="2001+00",
                baseline_revision=revision,
            )
        ],
    )


@pytest.fixture
def rendered(client, session, linked_project, tmp_path) -> dict[str, str]:
    """Each template in ``RENDERED_STATES``, as an enforcing deployment sends it.

    Where a template has more than one state worth reading, the states are
    concatenated: this guard asks what a page *can* print, so every link any
    of them printed is measured.
    """

    project = linked_project
    _one_reviewable_change(session, project)
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = [
        row
        for row in reading.items
        if row.grouping_key_kind == SOURCE_REVISION and row.held_out_reason is None
    ]

    listed = client.get(f"/review/{project.slug}")
    opened = client.get(f"/review/{project.slug}", params={"item": item.item_key})
    assert listed.status_code == 200
    assert opened.status_code == 200

    previewed = client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={
            "upload": (
                "ucm.xlsx",
                workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS),
                "application/octet-stream",
            )
        },
    )
    assert previewed.status_code == 200, previewed.text[:400]

    return {
        "review.html": listed.text + opened.text,
        "source_preview.html": previewed.text,
    }


def test_the_scan_reads_the_pages_the_pilot_actually_serves(rendered):
    """The ratchet is worth nothing if it is reading an empty set of pages."""

    templates = manifest_page_templates()
    assert {
        "projects.html",
        "portfolio.html",
        "project_workflow.html",
        "record_history.html",
        "review.html",
        "work_list.html",
        "sign_in.html",
        "_primitives.html",
        # #824's admissions bring the deterministic intake path into scope.
        "source_upload.html",
        "source_preview.html",
        "source_uploads.html",
    } <= templates, sorted(templates)
    assert any(
        kind == "internal" for _template, _link, kind in links_on_manifest_pages(rendered)
    )


def test_the_rendered_states_are_the_ones_this_guard_says_it_renders(rendered):
    """A rendering is a narrowing, so each one is declared and each is real.

    Where a state is rendered the template's own source is no longer read, and
    that is exactly how a check quietly stops covering something. So the
    templates named in `RENDERED_STATES` and the templates actually rendered
    are the same set, and each rendered state printed links rather than
    arriving empty.
    """

    assert set(RENDERED_STATES) == set(rendered)
    for template, markup in sorted(rendered.items()):
        assert any(
            kind == "internal" for _link, kind, _offset in links_in(markup)
        ), f"{template} rendered with no internal link; the state proves nothing"


def test_the_review_reading_resolves_the_links_it_computes(rendered):
    """#848's first unresolvable target, read off the page instead.

    `href="{{ view.open_url }}"` is a whole expression, so reading the
    template could only record that it could not tell. The rendered item
    carries the path the route built, and the exact-source link #824 admitted
    is on the opened one.
    """

    printed = {link for link, kind, _offset in links_in(rendered["review.html"])
               if kind == "internal"}

    assert "GET /review/{}" in printed
    assert "GET /review/{}/source" in printed


def test_the_intake_preview_offers_no_control_the_deployment_refuses(rendered):
    """#824's second: a control the page carries and an enforcing pilot withholds.

    The model-assisted draft stays outside the manifest, and the preview page
    asks the boundary before offering it. The template still contains the form,
    which is why this is asserted against the rendered page: the source reading
    would call it printed.
    """

    printed = {link for link, _kind, _offset in links_in(rendered["source_preview.html"])}

    assert "POST /projects/{}/sources/confirm" in printed
    assert "POST /projects/{}/sources/draft" not in printed


def test_every_internal_link_on_a_manifest_page_resolves_to_a_manifest_route(rendered):
    """#824's ratchet, with the offenders it admitted taken off it."""

    assert_ratchet(
        "tests/test_manifest_page_links.py:UNADMITTED_LINKS",
        measured=unadmitted_links(rendered),
        recorded=UNADMITTED_LINKS,
    )


def test_every_link_a_manifest_page_prints_can_be_read_from_its_template(rendered):
    """The hole in this reading, counted rather than left implicit."""

    assert_ratchet(
        "tests/test_manifest_page_links.py:COMPUTED_LINKS",
        measured=computed_links(rendered),
        recorded=COMPUTED_LINKS,
    )


def test_no_manifest_page_sends_a_coordinator_somewhere_undecided(rendered):
    """An external destination needs a decision, and today there are none."""

    stranded = unapproved_external_links(rendered)
    assert not stranded, (
        "a page the live pilot serves links out of the product with no "
        "recorded reason: " + ", ".join(sorted(stranded))
    )
