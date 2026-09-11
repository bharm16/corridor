"""Delete the project graph a committed test scenario leaves behind (#521).

A few regressions commit a real project graph so a genuinely independent
Session can observe it, then delete that graph under
``session_replication_role = replica``. Rows left behind in the shared
per-worker database break every later module that reads an empty project.

What "that graph" means used to be remembered rather than derived. Five
modules hand-listed 19, 17, 7, 4 and 3 tables in five different orders; two
of the lists named the same audit entity type as a constant in one file and
as a string literal in the other; three of the five removed no spine rows at
all, although every guided human act has dual-written the spine since #451;
and nothing at all removed ``revision_comparison_runs``,
``policy_activations`` or ``extraction_runs`` in the modules whose lists
forgot them.

The table set is therefore derived from the ORM metadata and never listed.
``projects`` is the root; every table carrying ``project_id`` is placed
against it; every remaining table is placed by a foreign key into a table
already placed. ``audit_log`` is keyed by ``(entity_type, entity_id)``
rather than by a foreign key, so its predicate is derived from
``audit.ENTITY_TYPES``. A table that is neither placed nor named in
``GLOBAL_TABLES`` raises at import, so a new project-scoped table joins the
cleanup on its own and a new registry or configuration table has to say why
it is not one project's.

Tables are deleted dependents-first, because a dependent's predicate reads
the rows of the parent that places it: delete the parent first and the
dependent's rows are orphaned silently instead of removed. The cleanup
reads the keys it means to delete before deleting any of them and refuses
to commit if one survives, so a missed table or a wrong order fails in the
leaking test rather than in a victim.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy import MetaData, Table, and_, delete, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from corridor import audit
from corridor.models import Base

PROJECT_TABLE = "projects"
AUDIT_TABLE = "audit_log"

# Not one project's rows: a registry, a sealed configuration, a deployment
# identity, or a person-level record that outlives every project enrolment.
# A committed scenario that creates one of these removes it itself, naming
# the exact row it committed; the derivation refuses any table that is
# neither placed in the project graph nor classified here on purpose.
GLOBAL_TABLES: dict[str, str] = {
    "customer_environment_binding": "one deployment's local identity",
    "evidence_investigation_evaluation_receipts": "a deterministic shadow evaluation",
    "external_orgs": "the External Party registry, shared by every project",
    "extractor_configurations": "one sealed configuration, cited by every project's runs",
    "person_identities": "the subject a verified email resolves to, not one project",
    "pipeline_configurations": "one immutable full-chain configuration",
    "pipeline_qualification_policies": "metric contracts frozen before any project's observations",
    "retention_manifests": "one retention dry run over the whole deployment",
    "sign_in_attempts": "an issuance or consumption attempt, counted per person",
    "sign_in_tokens": "one person's magic-link secret",
    "web_sessions": "one signed-in browser session",
}

# `audit_log` names its subject by entity type and id. Every entity type but
# one is a project-scoped table, so the audit rows of a committed scenario
# are derived rather than listed; a new entity type must join this map or be
# classified beside `person_identity` below.
AUDIT_ENTITY_TABLES: dict[str, str] = {
    audit.DEPENDENCY: "dependencies",
    audit.CANDIDATE: "candidates",
    audit.MILESTONE: "milestones",
    audit.PROJECT: PROJECT_TABLE,
    audit.COMMITMENT_LINEAGE: "commitment_lineages",
    audit.DOCUMENT: "documents",
}
# Sign-in, sign-out and deprovisioning are acts about a person rather than
# about one project (#531), so their entries survive a project's deletion.
GLOBAL_AUDIT_ENTITY_TYPES = frozenset({audit.PERSON_IDENTITY})

# A table placed only through nullable columns could hold a row carrying no
# placement at all, and that row would outlive the cleanup unnoticed. One
# table is placed that way on purpose, and a check constraint keeps every
# row of it placed.
NULLABLE_PLACEMENT_TABLES: dict[str, str] = {
    "work_decisions": "ck_work_decisions_exactly_one_subject requires one subject",
}

# A placement: the local column, the table it places this one against, and
# the column of that table it names.
Placement = tuple[str, str, str]


def _primary_key(table: Table):
    (column,) = table.primary_key.columns
    return column


def _foreign_placements(table: Table, placed: Iterable[str]) -> tuple[Placement, ...]:
    """Every foreign key from `table` into the primary key of a placed table."""

    known = set(placed)
    placements: list[Placement] = []
    for key in sorted(
        table.foreign_keys, key=lambda key: (key.parent.name, key.column.table.name)
    ):
        parent = key.column.table
        if parent.name == table.name or parent.name not in known:
            continue
        if key.column is not _primary_key(parent):
            continue
        placement = (key.parent.name, parent.name, key.column.name)
        if placement not in placements:
            placements.append(placement)
    return tuple(placements)


def place_project_tables(
    metadata: MetaData,
) -> tuple[tuple[str, ...], dict[str, tuple[Placement, ...]]]:
    """Order every project-scoped table dependents-first, and say how each is placed.

    Raises when a table is neither placed nor classified global, and when the
    placements do not order (a table placed against one that is placed back
    against it has no safe deletion order).
    """

    tables = {table.name: table for table in metadata.sorted_tables}
    reachable = {PROJECT_TABLE} & set(tables)
    reachable |= {name for name, table in tables.items() if "project_id" in table.c}
    if AUDIT_TABLE in tables:
        reachable.add(AUDIT_TABLE)
    growing = True
    while growing:
        growing = False
        for name, table in tables.items():
            if name in reachable:
                continue
            if _foreign_placements(table, reachable):
                reachable.add(name)
                growing = True

    placements: dict[str, tuple[Placement, ...]] = {}
    for name in reachable:
        table = tables[name]
        if name == PROJECT_TABLE:
            placements[name] = ()
        elif "project_id" in table.c:
            placements[name] = (("project_id", PROJECT_TABLE, "id"),)
        elif name == AUDIT_TABLE:
            placements[name] = tuple(
                ("entity_id", parent, "id")
                for parent in sorted(set(AUDIT_ENTITY_TABLES.values()))
            )
        else:
            placements[name] = _foreign_placements(table, reachable)

    unnamed = sorted(
        {parent for found in placements.values() for _, parent, _ in found}
        - set(placements)
    )
    if unnamed:
        raise AssertionError(
            f"these tables place others but are not themselves placed: {unnamed}"
        )

    unplaced = sorted(set(tables) - set(placements) - set(GLOBAL_TABLES))
    if unplaced:
        raise AssertionError(
            "these tables are neither reachable from a project nor classified as "
            f"global in tests/committed_scenario_support.py: {unplaced}"
        )
    misclassified = sorted(set(GLOBAL_TABLES) & set(placements))
    if misclassified:
        raise AssertionError(
            "these tables are project-scoped and must not be classified as global: "
            f"{misclassified}"
        )

    optional = {
        name
        for name, found in placements.items()
        if found
        and name != AUDIT_TABLE
        and "project_id" not in tables[name].c
        and all(tables[name].c[local].nullable for local, _, _ in found)
    }
    unheld = sorted(optional - set(NULLABLE_PLACEMENT_TABLES))
    if unheld:
        raise AssertionError(
            "a row of these tables can carry no placement at all and would outlive "
            "the cleanup; make one placement column NOT NULL, or name the check "
            f"constraint that holds it in tests/committed_scenario_support.py: {unheld}"
        )
    held = sorted(set(NULLABLE_PLACEMENT_TABLES) - optional)
    if held:
        raise AssertionError(
            f"these tables no longer need a nullable-placement reason: {held}"
        )

    return _dependents_first(placements), placements


def _dependents_first(placements: dict[str, tuple[Placement, ...]]) -> tuple[str, ...]:
    """Order the placed tables so each precedes every table that places it."""

    remaining = {
        name: {parent for _, parent, _ in found}
        for name, found in placements.items()
    }
    order: list[str] = []
    while remaining:
        ready = sorted(name for name, parents in remaining.items() if not parents)
        if not ready:
            raise AssertionError(
                f"these tables place each other in a cycle: {sorted(remaining)}"
            )
        for name in ready:
            del remaining[name]
        for parents in remaining.values():
            parents.difference_update(ready)
        order.extend(ready)
    order.reverse()
    return tuple(order)


PROJECT_GRAPH_ORDER, PROJECT_GRAPH_PLACEMENTS = place_project_tables(Base.metadata)
PROJECT_GRAPH_TABLES: tuple[Table, ...] = tuple(
    Base.metadata.tables[name] for name in PROJECT_GRAPH_ORDER
)

assert set(AUDIT_ENTITY_TABLES) | GLOBAL_AUDIT_ENTITY_TYPES == set(
    audit.ENTITY_TYPES
), (
    "an audit entity type is neither placed against a project-scoped table nor "
    "classified as global: "
    f"{sorted(set(audit.ENTITY_TYPES) ^ (set(AUDIT_ENTITY_TABLES) | GLOBAL_AUDIT_ENTITY_TYPES))}"
)


def project_rows(table: Table, project_id: int) -> ColumnElement[bool]:
    """The rows of `table` that belong to one project."""

    if table.name == PROJECT_TABLE:
        return table.c.id == project_id
    if "project_id" in table.c:
        return table.c.project_id == project_id
    if table.name == AUDIT_TABLE:
        return or_(
            *(
                and_(
                    table.c.entity_type == entity_type,
                    table.c.entity_id.in_(
                        select(Base.metadata.tables[parent].c.id).where(
                            project_rows(Base.metadata.tables[parent], project_id)
                        )
                    ),
                )
                for entity_type, parent in sorted(AUDIT_ENTITY_TABLES.items())
            )
        )
    return or_(
        *(
            table.c[local].in_(
                select(Base.metadata.tables[parent].c[column]).where(
                    project_rows(Base.metadata.tables[parent], project_id)
                )
            )
            for local, parent, column in PROJECT_GRAPH_PLACEMENTS[table.name]
        )
    )


def delete_project_graph(cleanup: Session, project_id: int) -> dict[str, list[int]]:
    """Delete one project's whole graph inside an open replica-mode cleanup.

    Returns whatever survived, keyed by table, so the caller can commit the
    rows it did remove before failing: a cleanup that rolled its whole
    deletion back would hand the leak to the next module instead of to the
    one that caused it.

    The keys are read before anything is deleted, because a dependent is
    placed by its parent's rows: were the parent deleted first, the dependent
    would match nothing and stay behind unnoticed.
    """

    doomed = {
        table.name: frozenset(
            cleanup.scalars(
                select(_primary_key(table)).where(project_rows(table, project_id))
            ).all()
        )
        for table in PROJECT_GRAPH_TABLES
    }
    for table in PROJECT_GRAPH_TABLES:
        cleanup.execute(delete(table).where(project_rows(table, project_id)))
    leaked: dict[str, list[int]] = {}
    for table in PROJECT_GRAPH_TABLES:
        keys = doomed[table.name]
        if not keys:
            continue
        key = _primary_key(table)
        surviving = cleanup.scalars(select(key).where(key.in_(keys))).all()
        if surviving:
            leaked[table.name] = sorted(surviving)
    return leaked


def delete_committed_project(
    project_id: int, *, session_factory: Callable[[], Session]
) -> None:
    """Delete every row one committed test scenario wrote for its project.

    Production receipts are deliberately append-only. The scenario commits
    only so a genuinely independent Session can observe the state, so the
    cleanup lifts the append-only guard for this exact synthetic project with
    a transaction-local setting that restores itself at commit or rollback;
    it never weakens the schema for another Session.
    """

    with session_factory() as cleanup:
        cleanup.execute(text("set local session_replication_role = replica"))
        leaked = delete_project_graph(cleanup, project_id)
        cleanup.commit()
    assert leaked == {}, (
        f"the committed-scenario cleanup left rows of project {project_id} behind: "
        f"{leaked}"
    )
