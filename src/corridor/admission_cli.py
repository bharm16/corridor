"""Record Inclusion backfill for a project extracted before ADR-0029.

There is nothing to authorize any more, and nothing an operator has to
run in the ordinary course: extraction loads the project on its way out.
This exists for the projects whose documents were read before that was
true, and as the way to finish a load after a human declares the Current
Production Run of a document that held several readings. The command remains
``admission`` for compatibility with existing scripts and receipts.
"""

from __future__ import annotations

import sys

from sqlalchemy import select


def _usage() -> int:
    print("usage: admission load <project-slug>", file=sys.stderr)
    print(
        "Record Inclusion: add eligible Extracted Proposals under the applicable rules.",
        file=sys.stderr,
    )
    return 2


def _project(session, slug: str):
    from corridor.models import Project

    project = session.scalars(
        select(Project).where(Project.slug == slug)
    ).first()
    if project is None:
        print(f"no project with slug {slug!r}", file=sys.stderr)
    return project


def main(argv: list[str], *, session_factory=None) -> int:
    if len(argv) != 2 or argv[0] != "load":
        return _usage()
    _, slug = argv

    if session_factory is None:
        from corridor.db import Session as session_factory

    from corridor.admission import load_and_report

    with session_factory() as session:
        project = _project(session, slug)
        if project is None:
            return 2
        print(load_and_report(session, project))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
