"""Import key dates and link them to a Constraint's Required By date.

A Constraint's **Required By** date comes from the key date it serves — when
the project needs its requirement met. **Promised For** is the timing stated
by the external organization. Those are different claims by different parties, and the
gap between them is the entire signal this tool exists to surface, so they
are never merged into one field.

CSV is the v0 stopgap. P6 XER import is M9.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import Dependency, Milestone, MilestoneRegistration

REQUIRED_COLUMNS = ("code", "name", "need_date")


class MalformedMilestoneCsv(Exception):
    """Raised rather than importing a partial schedule.

    Every dependency's need date derives from these rows, so a silently
    half-loaded schedule would put `DUE_SOON` and `ORPHAN` quietly wrong
    across the whole ledger.
    """


@dataclass
class ImportResult:
    created: list[Milestone] = field(default_factory=list)
    updated: list[Milestone] = field(default_factory=list)
    skipped_blank: int = 0


def import_csv(
    session: Session,
    *,
    project_id: int,
    path: Path | str,
    source: str | None = None,
    actor: str = "import",
) -> ImportResult:
    path = Path(path)
    source_bytes = path.read_bytes()
    source_sha256 = sha256(source_bytes).hexdigest()
    rows = list(csv.DictReader(source_bytes.decode().splitlines()))
    if not rows:
        raise MalformedMilestoneCsv(f"{path.name}: no rows")

    headers = {(h or "").strip().lower() for h in rows[0]}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise MalformedMilestoneCsv(
            f"{path.name}: missing column(s) {', '.join(missing)}; "
            f"found {', '.join(sorted(headers))}"
        )

    result = ImportResult()
    for number, raw in enumerate(rows, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        code = row.get("code")
        if not code:
            result.skipped_blank += 1
            continue

        need_date = _parse_date(row.get("need_date"), path.name, number)
        existing = session.scalars(
            select(Milestone).where(
                Milestone.project_id == project_id, Milestone.code == code
            )
        ).first()

        if existing is None:
            milestone = Milestone(
                project_id=project_id,
                code=code,
                name=row.get("name") or code,
                need_date=need_date,
                source=source or path.name,
            )
            session.add(milestone)
            session.flush()
            _register_milestone(
                session,
                milestone,
                source_name=source or path.name,
                source_sha256=source_sha256,
                source_row=_source_row(code, milestone.name, need_date),
                recorded_by=actor,
            )
            # Recorded like a revision. Only revisions were, so the first
            # Need Date every linked Dependency inherits — the one that
            # decides whether it is overdue — entered the record with
            # nobody's name on it.
            audit.record(
                session,
                actor=actor,
                action=audit.CREATE_MILESTONE,
                entity_type=audit.MILESTONE,
                entity_id=milestone.id,
                after={
                    "code": code,
                    "need_date": need_date.isoformat() if need_date else None,
                    "source": milestone.source,
                },
            )
            result.created.append(milestone)
        else:
            # Re-import updates in place: a schedule revision is the normal
            # case, and creating a second row for one milestone code would
            # split every dependency linked to it.
            was = existing.need_date
            existing.name = row.get("name") or existing.name
            existing.need_date = need_date
            existing.source = source or path.name
            _register_milestone(
                session,
                existing,
                source_name=existing.source,
                source_sha256=source_sha256,
                source_row=_source_row(existing.code, existing.name, need_date),
                recorded_by=actor,
            )
            if was != need_date:
                # A schedule revision moves every linked Dependency's need
                # date on the next relink, so who moved it is part of the
                # record rather than a fact about a CSV nobody kept.
                audit.record(
                    session,
                    actor=actor,
                    action=audit.REVISE_MILESTONE,
                    entity_type=audit.MILESTONE,
                    entity_id=existing.id,
                    before={"need_date": was.isoformat() if was else None},
                    after={
                        "need_date": need_date.isoformat() if need_date else None,
                        "source": existing.source,
                    },
                )
            result.updated.append(existing)

    session.flush()
    return result


def _source_row(code: str, name: str, need_date: date | None) -> dict[str, str | None]:
    return {
        "code": code,
        "name": name,
        "need_date": need_date.isoformat() if need_date is not None else None,
    }


def _register_milestone(
    session: Session,
    milestone: Milestone,
    *,
    source_name: str,
    source_sha256: str,
    source_row: dict[str, str | None],
    recorded_by: str,
) -> MilestoneRegistration:
    """Append one exact source registration, or reuse the identical current one."""
    if not isinstance(recorded_by, str) or not recorded_by.strip():
        raise ValueError("a Milestone Registration needs a recording identity")
    current = (
        session.get(MilestoneRegistration, milestone.current_registration_id)
        if milestone.current_registration_id is not None
        else None
    )
    if (
        current is not None
        and current.source_name == source_name
        and current.source_sha256 == source_sha256
        and current.source_row_json == source_row
    ):
        return current
    registration = MilestoneRegistration(
        milestone_id=milestone.id,
        source_name=source_name,
        source_sha256=source_sha256,
        source_row_json=source_row,
        recorded_by=recorded_by.strip(),
        predecessor_registration_id=current.id if current is not None else None,
    )
    session.add(registration)
    session.flush([registration])
    milestone.current_registration_id = registration.id
    return registration


def _parse_date(value: str | None, filename: str, line: int) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise MalformedMilestoneCsv(
        f"{filename} line {line}: cannot read need_date {value!r}"
    )


def link_dependency(
    session: Session,
    dependency: Dependency,
    milestone: Milestone,
    *,
    actor: str,
) -> Dependency:
    """Point a dependency at the milestone it must be ready for.

    `need_date` is copied onto the dependency rather than joined at read
    time, because a schedule revision must not silently rewrite history on
    records already reported against the old date. Re-import updates the
    milestone; re-linking is what pushes a new date onto a dependency.

    `actor` is required because this *is* a ledger mutation: `need_date`
    drives DUE_SOON and ORPHAN and appears in every recorded run, so a
    relink moves published numbers. It used to write no audit entry at
    all — the silent overwrite this module's own docstring is about.
    """
    if milestone.project_id != dependency.project_id:
        raise ValueError("milestone belongs to a different project")

    before = {
        "milestone_id": dependency.milestone_id,
        "need_date": dependency.need_date.isoformat() if dependency.need_date else None,
    }
    dependency.milestone_id = milestone.id
    dependency.milestone_registration_id = milestone.current_registration_id
    dependency.need_date = milestone.need_date
    audit.record(
        session,
        actor=actor,
        action=audit.LINK_MILESTONE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        before=before,
        after={
            "milestone_id": milestone.id,
            "milestone_registration_id": milestone.current_registration_id,
            "milestone_code": milestone.code,
            "need_date": milestone.need_date.isoformat()
            if milestone.need_date
            else None,
        },
    )
    session.flush()
    return dependency


def link_all(
    session: Session,
    *,
    project_id: int,
    milestone_code: str,
    dep_type: str | None = None,
    actor: str,
) -> int:
    """Bulk-link every unlinked dependency to one milestone.

    The v0 stopgap: most projects have a single utility-clearance milestone
    that everything is measured against. Per-dependency linkage is the
    reviewer's job in the ledger UI.
    """
    milestone = session.scalars(
        select(Milestone).where(
            Milestone.project_id == project_id, Milestone.code == milestone_code
        )
    ).first()
    if milestone is None:
        raise LookupError(f"no milestone {milestone_code!r} in this project")

    query = select(Dependency).where(
        Dependency.project_id == project_id, Dependency.milestone_id.is_(None)
    )
    if dep_type:
        query = query.where(Dependency.dep_type == dep_type)

    count = 0
    for dependency in session.scalars(query):
        link_dependency(session, dependency, milestone, actor=actor)
        count += 1
    return count


def main(argv: list[str]) -> int:
    """`make milestones ARGS="<slug> <csv> [milestone-code-to-link]"`"""
    import sys

    from corridor.db import Session as SessionFactory
    from corridor.models import Project

    if len(argv) < 2:
        print("usage: <project-slug> <csv> [milestone-code]", file=sys.stderr)
        print(
            "Import key dates from a schedule CSV; the optional code links constraints.",
            file=sys.stderr,
        )
        return 1
    slug, csv_path = argv[0], argv[1]
    link_code = argv[2] if len(argv) > 2 else None

    with SessionFactory() as session:
        project = session.scalars(select(Project).where(Project.slug == slug)).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        result = import_csv(session, project_id=project.id, path=csv_path)
        print(
            f"{len(result.created)} created, {len(result.updated)} updated"
            + (
                f", {result.skipped_blank} blank rows skipped"
                if result.skipped_blank
                else ""
            ),
            flush=True,
        )
        for milestone in result.created + result.updated:
            print(f"  {milestone.code:<16} {milestone.need_date}  {milestone.name}")

        if link_code:
            linked = link_all(
                session,
                project_id=project.id,
                milestone_code=link_code,
                actor="import",
            )
            print(
                f"linked {linked} previously unlinked constraints to key date {link_code}",
                flush=True,
            )

        session.commit()
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
