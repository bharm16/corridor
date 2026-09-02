"""Import key dates and link them to a Constraint's Required By date.

A Constraint's **Required By** date comes from the key date it serves — when
the project needs its requirement met. **Promised For** is the timing stated
by the external organization. Those are different claims by different parties, and the
gap between them is the entire signal this tool exists to surface, so they
are never merged into one field.

Two intake paths, one immutable registration. A **structured P6 XER schedule
imports itself** the moment it lands — versioned, no confirmation, the same
rule as every structured file (ADR-0055, ADR-0057). The **hand-typed CSV
stopgap keeps a preview** and an attributable confirm, because a person
authored the input and people typo: `preview_import` is a read-only dry run,
and `confirm_import` commits under one stable human identity, bound to the
exact previewed bytes and the predecessor Key Date Versions it was reviewed
against. Both paths register immutable Key Date Versions per ADR-0045.

Deriving which activities govern utility work, linking Constraints by location,
and flowing a moved date through to Required By are the schedule matcher's job
(#371, ADR-0057), not this importer's.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import Dependency, Milestone, MilestoneRegistration
from corridor.principals import HumanPrincipal, require_human_principal

REQUIRED_COLUMNS = ("code", "name", "need_date")

# One captured Key Date is a schedule Milestone (ADR-0045, ADR-0048). In a P6
# XER export those are the zero-duration Start and Finish Milestone activities;
# ordinary task activities are not key dates and are not registered here.
_XER_MILESTONE_TASK_TYPES = {"TT_Mile": "start", "TT_FinMile": "finish"}
# The registered Key Date is the *scheduled* date, not proof the event occurred
# (ADR-0045). Prefer the planned target, then the forecast, and read an actual
# date only when the schedule carries nothing else.
_XER_START_DATE_FIELDS = ("target_start_date", "early_start_date", "act_start_date")
_XER_FINISH_DATE_FIELDS = ("target_end_date", "early_end_date", "act_end_date")

# Preview row classifications. A touched row is not a moved date: a new source
# version whose date is unchanged is called out separately from a changed date
# so a re-registration is never presented as a schedule slip (ticket #337).
NEW = "new"
UNCHANGED = "unchanged"
CHANGED = "changed"
NEW_SOURCE_VERSION = "new_source_version"


class MalformedMilestoneCsv(Exception):
    """Raised rather than importing a partial schedule.

    Every dependency's need date derives from these rows, so a silently
    half-loaded schedule would put `DUE_SOON` and `ORPHAN` quietly wrong
    across the whole ledger.
    """


class MalformedScheduleXer(Exception):
    """Raised rather than registering key dates from an unreadable XER export.

    A P6 XER whose header, TASK table, or milestone date is unreadable refuses
    whole, for the same reason the CSV path does: a partial schedule is worse
    than none.
    """


class StaleMilestoneImport(Exception):
    """A previewed CSV no longer matches its bytes or the project's state.

    Carries a fresh :class:`MilestoneImportPreview` of the current state so the
    caller can present what actually holds now instead of committing a stale
    review (ADR-0035, ADR-0039).
    """

    def __init__(self, message: str, preview: "MilestoneImportPreview") -> None:
        super().__init__(message)
        self.preview = preview


@dataclass
class ImportResult:
    created: list[Milestone] = field(default_factory=list)
    updated: list[Milestone] = field(default_factory=list)
    skipped_blank: int = 0


@dataclass(frozen=True)
class _ParsedRow:
    """One key-date row parsed from a source, before it touches the record.

    ``raw_name`` is kept exactly as authored (possibly blank) so the apply step
    can preserve the create-vs-update naming the CSV path has always used.
    """

    code: str
    raw_name: str
    need_date: date | None


@dataclass(frozen=True)
class RowPreview:
    """What one previewed row would do to the record, without doing it."""

    code: str
    name: str
    need_date: date | None
    prior_need_date: date | None
    classification: str
    prior_registration_id: int | None


@dataclass(frozen=True)
class MilestoneImportPreview:
    """A read-only dry run of a hand-typed CSV import.

    ``predecessors`` is the current-Key-Date-Version snapshot the later confirm
    is bound to; a concurrent import that moves any of them makes the confirm
    refuse and re-present the new state.
    """

    project_id: int
    source_name: str
    source_sha256: str
    rows: tuple[RowPreview, ...]
    skipped_blank: int
    predecessors: dict[str, int | None]

    def _count(self, classification: str) -> int:
        return sum(1 for row in self.rows if row.classification == classification)

    @property
    def new_count(self) -> int:
        return self._count(NEW)

    @property
    def unchanged_count(self) -> int:
        return self._count(UNCHANGED)

    @property
    def changed_count(self) -> int:
        """Rows whose date or name moved — the only rows that are moved dates."""
        return self._count(CHANGED)

    @property
    def new_version_count(self) -> int:
        """New registered versions whose supported values did not change."""
        return self._count(NEW_SOURCE_VERSION)


def import_csv(
    session: Session,
    *,
    project_id: int,
    path: Path | str,
    source: str | None = None,
    actor: str = "import",
) -> ImportResult:
    """Operator CLI path: read a CSV file and register its key dates.

    The customer-facing product flow uses :func:`preview_import` /
    :func:`confirm_import` on provided content instead, so no customer ever
    hands the product a server filesystem path.
    """
    path = Path(path)
    content = path.read_bytes()
    source_name = source or path.name
    rows, skipped_blank = _parse_csv_content(content, source_name)
    result = _apply_import(
        session,
        project_id,
        rows,
        source_name=source_name,
        source_sha256=sha256(content).hexdigest(),
        recorded_by=actor,
    )
    result.skipped_blank = skipped_blank
    return result


def preview_import(
    session: Session,
    *,
    project_id: int,
    content: bytes,
    source_name: str,
) -> MilestoneImportPreview:
    """Dry-run a hand-typed CSV: classify each row, write nothing.

    Shows the exact source identity, source fingerprint, and the values that
    would be imported, and distinguishes a new row, an unchanged row, a changed
    date or name, and a new source version whose date is unchanged.
    """
    source_sha256 = sha256(content).hexdigest()
    rows, skipped_blank = _parse_csv_content(content, source_name)
    previews: list[RowPreview] = []
    predecessors: dict[str, int | None] = {}
    for row in rows:
        preview = _classify_row(
            session,
            project_id,
            row,
            source_name=source_name,
            source_sha256=source_sha256,
        )
        previews.append(preview)
        predecessors[row.code] = preview.prior_registration_id
    return MilestoneImportPreview(
        project_id=project_id,
        source_name=source_name,
        source_sha256=source_sha256,
        rows=tuple(previews),
        skipped_blank=skipped_blank,
        predecessors=predecessors,
    )


def confirm_import(
    session: Session,
    *,
    project_id: int,
    content: bytes,
    source_name: str,
    expected_sha256: str,
    expected_predecessors: dict[str, int | None],
    principal: HumanPrincipal,
) -> ImportResult:
    """Commit a previewed CSV under one stable human identity.

    Refuses the whole operation, and re-presents the current state, when the
    bytes changed since the preview or when any affected Key Date Version moved
    under it. The confirming person's subject — never a generic importer label —
    is recorded on every Key Date Version and audit entry.
    """
    principal = require_human_principal(principal)
    actual_sha256 = sha256(content).hexdigest()
    if actual_sha256 != expected_sha256:
        raise StaleMilestoneImport(
            "the source content changed since it was previewed",
            preview_import(
                session,
                project_id=project_id,
                content=content,
                source_name=source_name,
            ),
        )
    rows, skipped_blank = _parse_csv_content(content, source_name)
    current = {
        row.code: _current_registration_id(session, project_id, row.code)
        for row in rows
    }
    if current != _normalize_predecessors(expected_predecessors):
        raise StaleMilestoneImport(
            "the project's Key dates changed since this preview",
            preview_import(
                session,
                project_id=project_id,
                content=content,
                source_name=source_name,
            ),
        )
    result = _apply_import(
        session,
        project_id,
        rows,
        source_name=source_name,
        source_sha256=actual_sha256,
        recorded_by=principal.subject,
    )
    result.skipped_blank = skipped_blank
    return result


def import_xer(
    session: Session,
    *,
    project_id: int,
    content: bytes,
    source_name: str,
    recorded_by: str = "import:xer",
) -> ImportResult:
    """Register a P6 XER schedule's key dates: versioned, no confirmation.

    A structured file imports itself the moment it lands (ADR-0055, ADR-0057).
    ``recorded_by`` is a system import label, not a person: nobody confirmed
    this, and stamping a fabricated human identity on a mechanical import is
    exactly what ADR-0045 provenance forbids. The milestone Start/Finish
    activities become immutable Key Date Versions; ordinary task activities and
    sequencing relationships are not key dates and are not registered.
    """
    rows, skipped_blank = _parse_xer_content(content, source_name)
    result = _apply_import(
        session,
        project_id,
        rows,
        source_name=source_name,
        source_sha256=sha256(content).hexdigest(),
        recorded_by=recorded_by,
    )
    result.skipped_blank = skipped_blank
    return result


def _apply_import(
    session: Session,
    project_id: int,
    rows: list[_ParsedRow],
    *,
    source_name: str,
    source_sha256: str,
    recorded_by: str,
) -> ImportResult:
    """Create or revise Milestones from parsed rows and register each version.

    The one write path shared by the CSV and XER intakes: the parser differs,
    the record effect is identical.
    """
    result = ImportResult()
    for row in rows:
        code = row.code
        need_date = row.need_date
        existing = session.scalars(
            select(Milestone).where(
                Milestone.project_id == project_id, Milestone.code == code
            )
        ).first()

        if existing is None:
            name = row.raw_name or code
            milestone = Milestone(
                project_id=project_id,
                code=code,
                name=name,
                need_date=need_date,
                source=source_name,
            )
            session.add(milestone)
            session.flush()
            _register_milestone(
                session,
                milestone,
                source_name=source_name,
                source_sha256=source_sha256,
                source_row=_source_row(code, name, need_date),
                recorded_by=recorded_by,
            )
            # Recorded like a revision. Only revisions were, so the first
            # Need Date every linked Dependency inherits — the one that
            # decides whether it is overdue — entered the record with
            # nobody's name on it.
            audit.record(
                session,
                actor=recorded_by,
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
            name = row.raw_name or existing.name
            existing.name = name
            existing.need_date = need_date
            existing.source = source_name
            _register_milestone(
                session,
                existing,
                source_name=source_name,
                source_sha256=source_sha256,
                source_row=_source_row(existing.code, name, need_date),
                recorded_by=recorded_by,
            )
            if was != need_date:
                # A schedule revision moves every linked Dependency's need
                # date on the next relink, so who moved it is part of the
                # record rather than a fact about a CSV nobody kept.
                audit.record(
                    session,
                    actor=recorded_by,
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


def _classify_row(
    session: Session,
    project_id: int,
    row: _ParsedRow,
    *,
    source_name: str,
    source_sha256: str,
) -> RowPreview:
    """Say what one row would do without doing it (read-only)."""
    existing = session.scalars(
        select(Milestone).where(
            Milestone.project_id == project_id, Milestone.code == row.code
        )
    ).first()
    if existing is None:
        return RowPreview(
            code=row.code,
            name=row.raw_name or row.code,
            need_date=row.need_date,
            prior_need_date=None,
            classification=NEW,
            prior_registration_id=None,
        )

    name = row.raw_name or existing.name
    prior_registration_id = existing.current_registration_id
    if existing.need_date != row.need_date or existing.name != name:
        classification = CHANGED
    else:
        current = (
            session.get(MilestoneRegistration, existing.current_registration_id)
            if existing.current_registration_id is not None
            else None
        )
        would_register = _source_row(row.code, name, row.need_date)
        if (
            current is not None
            and current.source_name == source_name
            and current.source_sha256 == source_sha256
            and current.source_row_json == would_register
        ):
            classification = UNCHANGED
        else:
            classification = NEW_SOURCE_VERSION
    return RowPreview(
        code=row.code,
        name=name,
        need_date=row.need_date,
        prior_need_date=existing.need_date,
        classification=classification,
        prior_registration_id=prior_registration_id,
    )


def _current_registration_id(
    session: Session, project_id: int, code: str
) -> int | None:
    return session.scalars(
        select(Milestone.current_registration_id).where(
            Milestone.project_id == project_id, Milestone.code == code
        )
    ).first()


def _normalize_predecessors(
    predecessors: dict[str, int | None]
) -> dict[str, int | None]:
    """Coerce a round-tripped snapshot (JSON string keys) back to comparable form."""
    return {
        str(code): (int(value) if value is not None else None)
        for code, value in predecessors.items()
    }


def _parse_csv_content(
    content: bytes, source_name: str
) -> tuple[list[_ParsedRow], int]:
    """Parse a hand-typed key-dates CSV, refusing a malformed sheet whole."""
    rows = list(csv.DictReader(content.decode().splitlines()))
    if not rows:
        raise MalformedMilestoneCsv(f"{source_name}: no rows")

    headers = {(h or "").strip().lower() for h in rows[0]}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise MalformedMilestoneCsv(
            f"{source_name}: missing column(s) {', '.join(missing)}; "
            f"found {', '.join(sorted(headers))}"
        )

    parsed: list[_ParsedRow] = []
    skipped_blank = 0
    for number, raw in enumerate(rows, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        code = row.get("code")
        if not code:
            skipped_blank += 1
            continue
        parsed.append(
            _ParsedRow(
                code=code,
                raw_name=row.get("name") or "",
                need_date=_parse_date(row.get("need_date"), source_name, number),
            )
        )
    return parsed, skipped_blank


def _parse_xer_content(
    content: bytes, source_name: str
) -> tuple[list[_ParsedRow], int]:
    """Parse a P6 XER export's milestone activities into key-date rows.

    XER is a tab-delimited table dump: ``%T`` names a table, ``%F`` its columns,
    ``%R`` a row. Only the TASK table's Start/Finish milestone activities are
    key dates; task predecessors and every other table are read past, so no
    sequencing or forecast claim enters the record.
    """
    try:
        text = content.decode("cp1252")
    except UnicodeDecodeError:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedScheduleXer(
                f"{source_name}: not a readable P6 XER export"
            ) from exc

    lines = text.splitlines()
    if not lines or not lines[0].startswith("ERMHDR"):
        raise MalformedScheduleXer(
            f"{source_name}: not a P6 XER export (missing ERMHDR header)"
        )

    fields: list[str] = []
    rows: list[dict[str, str]] = []
    in_task = False
    seen_task = False
    for line in lines:
        if not line:
            continue
        parts = line.split("\t")
        tag = parts[0]
        if tag == "%T":
            in_task = len(parts) > 1 and parts[1] == "TASK"
            if in_task:
                seen_task = True
                fields = []
        elif not in_task:
            continue
        elif tag == "%F":
            fields = [f.strip() for f in parts[1:]]
        elif tag == "%R":
            if not fields:
                raise MalformedScheduleXer(
                    f"{source_name}: TASK rows appear before their column header"
                )
            rows.append(dict(zip(fields, parts[1:])))

    if not seen_task:
        raise MalformedScheduleXer(f"{source_name}: no TASK table")
    missing = [c for c in ("task_code", "task_name", "task_type") if c not in fields]
    if missing:
        raise MalformedScheduleXer(
            f"{source_name}: TASK table missing column(s) {', '.join(missing)}"
        )

    parsed: list[_ParsedRow] = []
    skipped_blank = 0
    for row in rows:
        kind = _XER_MILESTONE_TASK_TYPES.get((row.get("task_type") or "").strip())
        if kind is None:
            continue
        code = (row.get("task_code") or "").strip()
        if not code:
            skipped_blank += 1
            continue
        parsed.append(
            _ParsedRow(
                code=code,
                raw_name=(row.get("task_name") or "").strip(),
                need_date=_xer_milestone_date(row, kind, source_name),
            )
        )
    return parsed, skipped_blank


def _xer_milestone_date(
    row: dict[str, str], kind: str, source_name: str
) -> date | None:
    """Read the first scheduled date the milestone carries, or leave it unknown."""
    fields = (
        _XER_FINISH_DATE_FIELDS if kind == "finish" else _XER_START_DATE_FIELDS
    )
    for field_name in fields:
        value = (row.get(field_name) or "").strip()
        if value:
            return _parse_xer_date(value, source_name, field_name)
    return None


def _parse_xer_date(value: str, source_name: str, field_name: str) -> date:
    # XER writes an ISO date, usually with a midnight clock: "2026-11-01 00:00".
    datepart = value.split(" ", 1)[0]
    try:
        return datetime.strptime(datepart, "%Y-%m-%d").date()
    except ValueError as exc:
        raise MalformedScheduleXer(
            f"{source_name}: cannot read {field_name} {value!r}"
        ) from exc


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
    """`make milestones ARGS="<slug> <schedule.csv|schedule.xer> [milestone-code-to-link]"`"""
    import sys

    from corridor.db import WorkerSession as SessionFactory
    from corridor.models import Project

    if len(argv) < 2:
        print(
            "usage: <project-slug> <schedule.csv|schedule.xer> [milestone-code]",
            file=sys.stderr,
        )
        print(
            "Import key dates from a schedule CSV or P6 XER; "
            "the optional code links constraints.",
            file=sys.stderr,
        )
        return 1
    slug, schedule_path = argv[0], argv[1]
    link_code = argv[2] if len(argv) > 2 else None

    with SessionFactory() as session:
        project = session.scalars(select(Project).where(Project.slug == slug)).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        if schedule_path.lower().endswith(".xer"):
            path = Path(schedule_path)
            result = import_xer(
                session,
                project_id=project.id,
                content=path.read_bytes(),
                source_name=path.name,
            )
        else:
            result = import_csv(session, project_id=project.id, path=schedule_path)
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
