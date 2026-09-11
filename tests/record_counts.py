"""Read every row one project holds, so "the refused act wrote nothing" has a name.

A refusal leaves the Project Record exactly as it was. That is a released
invariant, and it was asserted thirty-two times without ever being named:
twenty-two test bodies read three or four models before the act and the
same models after it, and ten modules wrapped the idiom in a private helper.

The helpers are why this is a correctness problem rather than a verbosity
one. They hand-picked their tables, and the hands did not agree: the eight
retired here watched 3, 3, 5, 5, 5, 6, 7 and 8 tables, out of the 208 a
project can hold. A refusal test that reads five tables passes while the
write it forbids lands in the other two hundred and three, and the module
that chose five had no way to know that.

So the table set is not chosen here either. ``committed_scenario_support``
already derives the whole project graph from the ORM metadata and refuses
to import when a table is neither placed against a project nor classified
as global; this reads the counts of exactly that set through exactly that
predicate. A new project-scoped table joins the invariant because it joined
the schema, and nothing in this module has to be edited for it to.

The reading is one statement rather than 208, because the caller pays for
it twice in every test that uses it.

Two properties of the derived predicate the caller should know. A table
placed by foreign key is selected through a subquery over the parent that
places it, so the count is exact while the parents exist -- which is the
whole of "nothing was written", where nothing was removed either -- but
reads zero once a project has been deleted. And the counts are per project:
an act that wrote into a *different* project's record is invisible unless
that project is named too, which is why ``nothing_written`` takes as many
as the test created.
"""

from __future__ import annotations

from collections.abc import Container, Iterator
from contextlib import contextmanager

from sqlalchemy import bindparam, func, select
from sqlalchemy.orm import Session

from committed_scenario_support import PROJECT_GRAPH_TABLES, project_rows

# One statement, built once. A fresh 208-subquery select costs four times as
# much to hand to the Session as to run, because SQLAlchemy's compiled cache
# has to derive a key for every subquery before it can find the hit; a
# statement object keeps its own key, so only the project id varies.
_PROJECT_ID = bindparam("project_id")
_READING = select(
    *(
        select(func.count())
        .select_from(table)
        .where(project_rows(table, _PROJECT_ID))
        .scalar_subquery()
        .label(table.name)
        for table in PROJECT_GRAPH_TABLES
    )
)


def project_record_counts(session: Session, project_id: int) -> dict[str, int]:
    """How many rows of each project-scoped table this one project holds.

    Keyed by table name, over every table the project graph derives -- not a
    family chosen by the caller, because choosing one is what let a refused
    write land unseen.
    """

    counts = session.execute(_READING, {"project_id": project_id}).one()
    return {table.name: count for table, count in zip(PROJECT_GRAPH_TABLES, counts)}


@contextmanager
def nothing_written(
    session: Session, *project_ids: int, apart_from: Container[str] = ()
) -> Iterator[None]:
    """Prove the act performed inside wrote no row of these projects' records.

    Name every project the test created whose record the act could reach: a
    refusal that cites a second project is refused precisely because it
    would have written somewhere, and reading only one of the two would not
    see where.

    ``apart_from`` names the tables the act does write on purpose -- a page
    view appends a ``product_proving_frontend_request`` receipt to
    ``audit_log``, and that is a request observation rather than a change to
    the record. It is an exception a caller states, not a set a caller
    chooses: the other two hundred tables stay guarded, which is what the
    private helpers this replaces could not say.

    The reading is taken in a ``finally``, because the act under test is a
    refusal and half the call sites raise out of the body on purpose. A
    ``yield`` with the check written after it never runs when the body
    raises, which is the shape of guard that passes without the behaviour.
    """

    if not project_ids:
        raise TypeError("nothing_written needs at least one project to read")
    before = {
        project_id: project_record_counts(session, project_id)
        for project_id in project_ids
    }

    try:
        yield
    finally:
        changed = []
        for project_id, counts in before.items():
            after = project_record_counts(session, project_id)
            changed += [
                f"project {project_id} {table} {was} -> {after[table]}"
                for table, was in counts.items()
                if was != after[table] and table not in apart_from
            ]
        assert not changed, "the act wrote to the record: " + ", ".join(sorted(changed))
