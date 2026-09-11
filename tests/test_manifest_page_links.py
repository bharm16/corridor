"""A link printed on an enabled page has to go somewhere the pilot serves (#848).

On an enforcing deployment the live pilot answers the routes in
``corridor.web_boundary`` and answers everything else exactly as it answers a
missing project. The customer-journey audit found the consequence on the
screen: **Source history** is printed on the Work page and on the Record page,
and the Review rows print a link to the exact source, and on an enforcing
deployment all five of those answer 404. Nothing failed, because nothing was
checking that a link a page prints is a route the deployment serves.

This is that check, kept as a ratchet: the offenders are recorded, they may
leave, and none may join. #824 is the ticket that empties it, by admitting
upload, preview, confirmation, the source register and the Review source link
to the manifest; when it does, it deletes the names from the list below in the
same change.

**What this reads, said plainly, because it is narrower than the sentence
#824 will finish.** It reads the ``href`` and ``action`` attributes written in
the templates a manifest route renders, and follows the templates those
import. It does not render pages and read the links off a rendered state, so a
link whose whole target is an expression -- ``href="{{ view.open_url }}"`` --
is a target this cannot resolve. Those are not skipped: they are recorded in
their own list, with the same rule, so the hole is countable and #824 closes
it by testing representative rendered states rather than by discovering it.

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
from pathlib import Path
import re

from corridor import web_boundary

from ratchet_support import assert_ratchet


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = REPO_ROOT / "src/corridor/web/app.py"
TEMPLATE_ROOT = REPO_ROOT / "src/corridor/web/templates"


#: Links on a manifest page that go to a route the live pilot does not serve.
#: **This list may fall and may never rise.** #824 empties it by admitting
#: these routes to the boundary; nothing else may be added to it.
UNADMITTED_LINKS: frozenset[str] = frozenset(
    {
        "project_workflow.html: GET /projects/{}/sources",
        "record_history.html: GET /projects/{}/sources",
        "review.html: GET /review/{}/source",
        "work_list.html: GET /internal-report/{}",
        "work_list.html: GET /ledger/{}",
        "work_list.html: GET /reports/{}",
    }
)

#: Links on a manifest page whose whole target is a template expression, which
#: a reading of the template cannot resolve to a route. **This list may fall
#: and may never rise**, and #824 empties it by reading links off rendered
#: states instead of out of the source. Two are URLs a view object carries;
#: the third is a shared macro's parameter, whose real targets are written at
#: the call sites and read there.
COMPUTED_LINKS: frozenset[str] = frozenset(
    {
        "_primitives.html: GET {{ href }}",
        "review.html: GET {{ view.open_url }}",
        "work_list.html: GET {{ view.action_url }}",
    }
)

#: External destinations a manifest page is allowed to offer, and why.
#: **Empty, deliberately**: sending a coordinator off the product mid-workflow
#: is a decision, and no page in the pilot set makes it today.
APPROVED_EXTERNAL_LINKS: dict[str, str] = {}


_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)
_ATTRIBUTE = re.compile(r'(?P<kind>href|action)="(?P<value>[^"]*)"')
_FORM_METHOD = re.compile(r'<form[^>]*\bmethod="(?P<method>[a-zA-Z]+)"', re.I)
_IMPORTS = re.compile(r'\{%-?\s*(?:import|include|extends|from)\s+"(?P<name>[^"]+)"')
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


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
    adopted project's week through ``_project_workflow_response`` and the
    legacy list directly, and every Review route renders through
    ``_render_review_response``.
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
    to spell both the same way.
    """

    path = _EXPRESSION.sub("{}", target)
    path = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", "{}", path)
    return path.split("?", 1)[0].split("#", 1)[0]


def _method_of(body: str, position: int, kind: str) -> str:
    """GET for a link; a form's own declared method for a form action."""

    if kind == "href":
        return "GET"
    opening = body.rfind("<form", 0, position)
    if opening < 0:
        return "GET"
    match = _FORM_METHOD.match(body, opening)
    return match.group("method").upper() if match else "GET"


def links_on_manifest_pages() -> tuple[tuple[str, str, str], ...]:
    """``(template, "METHOD path", kind)`` for every link a manifest page writes.

    ``kind`` is one of ``internal``, ``fragment``, ``external`` or
    ``computed``, which is the distinction #824 asks for.
    """

    found = []
    for template in sorted(manifest_page_templates()):
        path = TEMPLATE_ROOT / template
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8")
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
            found.append((template, f"{method} {target}", kind))
    return tuple(sorted(set(found)))


def _manifest_routes() -> frozenset[str]:
    return frozenset(
        f"{method} {_normalize(template)}"
        for method, template in web_boundary.PILOT_ROUTES
    )


def unadmitted_links() -> frozenset[str]:
    """Internal links a manifest page prints that the pilot does not serve."""

    served = _manifest_routes()
    return frozenset(
        f"{template}: {link}"
        for template, link, kind in links_on_manifest_pages()
        if kind == "internal" and link not in served
    )


def computed_links() -> frozenset[str]:
    """Links a manifest page prints whose target this reading cannot resolve."""

    return frozenset(
        f"{template}: {link}"
        for template, link, kind in links_on_manifest_pages()
        if kind == "computed"
    )


def unapproved_external_links() -> frozenset[str]:
    """External destinations a manifest page offers with no recorded decision."""

    return frozenset(
        f"{template}: {link}"
        for template, link, kind in links_on_manifest_pages()
        if kind == "external" and link.split(" ", 1)[1] not in APPROVED_EXTERNAL_LINKS
    )


def test_the_scan_reads_the_pages_the_pilot_actually_serves():
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
    } <= templates, sorted(templates)
    assert any(
        kind == "internal" for _template, _link, kind in links_on_manifest_pages()
    )


def test_every_internal_link_on_a_manifest_page_resolves_to_a_manifest_route():
    """#824's ratchet, started with today's offenders and allowed to shrink."""

    assert_ratchet(
        "tests/test_manifest_page_links.py:UNADMITTED_LINKS",
        measured=unadmitted_links(),
        recorded=UNADMITTED_LINKS,
    )


def test_every_link_a_manifest_page_prints_can_be_read_from_its_template():
    """The hole in this reading, counted rather than left implicit."""

    assert_ratchet(
        "tests/test_manifest_page_links.py:COMPUTED_LINKS",
        measured=computed_links(),
        recorded=COMPUTED_LINKS,
    )


def test_no_manifest_page_sends_a_coordinator_somewhere_undecided():
    """An external destination needs a decision, and today there are none."""

    stranded = unapproved_external_links()
    assert not stranded, (
        "a page the live pilot serves links out of the product with no "
        "recorded reason: " + ", ".join(sorted(stranded))
    )
