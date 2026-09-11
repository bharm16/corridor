"""The navigation shell every customer page carries, decided in one place (#843).

Why this exists: each screen grew its own header link. The project's week
offered "Source history", the Record offered "This project's work", the Review
offered "project work", and the sign-out control existed on the projects list
and nowhere else. Where a coordinator could go next therefore depended on
which page they had landed on, and leaving the product was reachable from one
of thirteen. The customer-journey audit recorded exactly that
(`docs/research/customer-journey-audit-2026-09-10.md`, the navigation row of
the cross-cutting table): "Work links to source/history, but lacks a
consistent project/portfolio/account shell."

What was tried before: nothing shared. A shell each template writes for itself
is a shell that drifts, which is the same failure `ui_primitives.py` records
for state labels. So the items, their order, their words and which one the
reader is inside are decided here; `_shell.html` draws what this module
returns and decides nothing.

**Every destination is a route in `corridor.web_boundary.PILOT_ROUTES`.** An
enforcing deployment answers an unlisted route the way it answers a missing
project, so a shell item pointing outside the manifest would be a 404 printed
on every page at once. `DESTINATIONS` records each one and
`tests/test_navigation_shell.py` holds it against the manifest and against the
markup `_shell.html` actually writes.

**The words are the ones already on the screens they name**: "Work" is
`project_workflow.html`'s own heading, "Record" what its own link calls the
Record history, "Projects" the person's own project list, "Sources" the
register of documents delivered to the project, "Sign out" the control the
projects list already carried. This module coins no domain term. A different
navigation word is a terminology decision (`docs/agents/domain.md`), not an
edit here.

**Two shapes of page, and the difference is real rather than a special case.**
Across projects -- the person's own list, and the coordinator's week -- there
is no current project, so the project's own items do not exist and are not
drawn. Inside a project the shell names the project and offers its sources,
its work and its record. The account item is the same on both.

**Where the reader is, is a section rather than a path.** Marking only an
exact match would leave the Review screen, the intake upload form, the exact
source and the issue configuration with nothing marked at all, which is the
question "where am I" going unanswered on four of the pages that most need it.
So a page is *inside* an item when it is reached from that item and returns to
it, and ARIA already separates the two readings: `aria-current="page"` for the
destination itself, `aria-current="true"` for a page within it.
"""

from __future__ import annotations

from dataclasses import dataclass


PROJECTS = "projects"
SOURCES = "sources"
WORK = "work"
RECORD = "record"

#: The word each item prints, and the accessible name of the shell itself.
TEXT = {
    PROJECTS: "Projects",
    SOURCES: "Sources",
    WORK: "Work",
    RECORD: "Record",
}
SHELL_LABEL = "Corridor"
SIGN_OUT = "Sign out"

#: Where each item goes, as the manifest spells the route. `{slug}` is the
#: current project's. `_shell.html` writes these paths literally so the link
#: ratchet (`tests/test_manifest_page_links.py`) can read them from its source.
DESTINATIONS = {
    PROJECTS: "/",
    SOURCES: "/projects/{slug}/sources",
    WORK: "/work/{slug}",
    RECORD: "/record/{slug}",
}
SIGN_OUT_DESTINATION = "/sign-out"

#: The mark beside the item the reader is on. Decorative and `aria-hidden`,
#: exactly as `ui_primitives.MARKS` is: the announcement comes from
#: `aria-current`, so a reader in print or in greyscale is told by the glyph
#: what a listening reader is told by ARIA, and neither is told by colour.
HERE_MARK = "\N{BLACK RIGHT-POINTING SMALL TRIANGLE}"

#: The first path segment that puts a page inside an item's section. A page
#: whose section is none marks nothing rather than guessing; there is no such
#: page in the manifest today.
_SECTIONS = {
    "": PROJECTS,
    "portfolio": PROJECTS,
    "work": WORK,
    "review": WORK,
    "issue-configuration": WORK,
    "record": RECORD,
    "sources": SOURCES,
}


@dataclass(frozen=True)
class ShellItem:
    """One item of the shell, and how this page stands to it."""

    name: str
    text: str
    href: str
    #: This page is the item's own destination.
    here: bool
    #: This page is inside the item's section without being its destination.
    within: bool

    @property
    def aria_current(self) -> str:
        """The ARIA token a screen reader announces, or nothing."""
        if self.here:
            return "page"
        return "true" if self.within else ""

    @property
    def mark(self) -> str:
        """The decorative glyph printed beside the words, where it applies."""
        return HERE_MARK if self.aria_current else ""


@dataclass(frozen=True)
class Shell:
    """The shell one page renders: its items, and the project it is inside."""

    label: str
    sign_out: str
    project_name: str
    projects: ShellItem
    sources: ShellItem | None
    work: ShellItem | None
    record: ShellItem | None

    @property
    def items(self) -> tuple[ShellItem, ...]:
        """Every item drawn, in the order they are read."""
        return tuple(
            item
            for item in (self.projects, self.sources, self.work, self.record)
            if item is not None
        )


def current_section(path: str) -> str | None:
    """Which item the reader is inside, from the path they asked for."""
    head = path.strip("/").split("/")[0] if path.strip("/") else ""
    if head == "projects":
        # `/projects/{slug}/sources...` is the source register and what it
        # leads to; the project's other `/projects/` routes render no page.
        return SOURCES if "/sources" in path else None
    return _SECTIONS.get(head)


def shell(path: str, slug: str = "", project_name: str = "") -> Shell:
    """The shell for one page: `path` is the request's, `slug` its project's."""
    section = current_section(path)

    def item(name: str, href: str) -> ShellItem:
        return ShellItem(
            name=name,
            text=TEXT[name],
            href=href,
            here=path == href,
            within=section == name and path != href,
        )

    def scoped(name: str) -> ShellItem | None:
        if not slug:
            return None
        return item(name, DESTINATIONS[name].format(slug=slug))

    return Shell(
        label=SHELL_LABEL,
        sign_out=SIGN_OUT,
        project_name=project_name,
        projects=item(PROJECTS, DESTINATIONS[PROJECTS]),
        sources=scoped(SOURCES),
        work=scoped(WORK),
        record=scoped(RECORD),
    )


def register(env) -> None:
    """Give every template the shell reading under one stable name."""
    env.globals.update(navigation_shell=shell)
