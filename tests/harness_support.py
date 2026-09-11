"""The role a test borrows to write accepted authority, written down once.

Accepted authority is a database boundary rather than a convention (#492), so a
test that needs an accepted revision or decision to exist first has to become
the record-decision role and then stop being it. Twenty-two call sites wrote
that pair by hand, and one of them (``tests/test_delta_resolution.py``'s
refusal walk) had no ``finally``: a body that raised left the role set for the
rest of the transaction, so the *next* write in the same test was silently
performed by the wrong principal and the assertion it was making no longer
meant what it said.

``as_role`` is that pair with the reset in a ``finally``. It began as
``as_record_decision_role`` and covered one of the four roles a test borrows,
so eight modules kept writing the pair by hand for the other three and one of
them grew a third copy of the helper with no reset at all. One context manager
takes every role, and the call site names the role it borrows from
``corridor.db_roles`` so a rename cannot leave a test setting a role that no
longer exists.

The three acts below are the fixture setup those borrows existed for.
``tests/`` held sixteen hand-written inserts into ``project_record_revisions``
and ``fact_decisions`` across nine modules, each one a copy of a
record-decision command's SQL that nothing failed when the command changed.
``tests/test_architecture.py`` forbids exactly that for ``src/corridor``, and
now scans this tree too, with this module as the one place the SQL is written.

None of the three can reach the command that writes its rows in production,
and each docstring says why and what would fail if that command changed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.db_roles import RECORD_DECISION_ROLE

if TYPE_CHECKING:
    from corridor.models import Fact, Project


@contextmanager
def as_role(session: Session, role: str) -> Iterator[Session]:
    """Borrow ``role`` for this transaction, and give it back however the body ends.

    ``reset role`` runs in a ``finally``, so a raising body cannot leave the
    wrong principal in force. A body that aborts the transaction rather than
    raising in Python has to hold its failing statement in a
    ``session.begin_nested()`` savepoint, as every refusal walk in this suite
    already does: PostgreSQL runs nothing at all in a failed transaction, so
    the reset needs the savepoint's rollback to have happened first.
    """

    session.execute(text(f'set local role "{role}"'))
    try:
        yield session
    finally:
        session.execute(text("reset role"))


def accepted_revision(
    session: Session,
    project_id: int,
    *,
    command_type: str = "adopt_baseline",
    principal: str = "local:adopter",
    key: str | None = None,
) -> int:
    """One Project Record revision, and nothing else, at the accepted boundary.

    Six modules wrote this insert by hand to make one thing true: the accepted
    record of ``project_id`` moved. Every command that writes a revision in
    production writes something alongside it -- an adopted baseline, a resolved
    Proposed Delta, a Review Packet receipt -- so reaching one of them here
    would make the fixture's subject the command rather than the question the
    test is asking.

    What it restates is deliberately small: the revision's own columns, no
    decision and no receipt. A change to the relation fails every caller here
    rather than drifting away from it, because PostgreSQL refuses the insert.

    ``project_id`` is an integer rather than a ``Project`` so the borrowed role
    can never be asked to lazily load an ORM attribute it holds no read on.
    """

    with as_role(session, RECORD_DECISION_ROLE):
        revision_id = session.scalar(
            text(
                "insert into project_record_revisions ("
                "project_id, command_type, human_principal, idempotency_key"
                ") values (:project_id, :command_type, :principal, :key)"
                " returning id"
            ),
            {
                "project_id": project_id,
                "command_type": command_type,
                "principal": principal,
                "key": key or f"revision:{uuid4().hex[:12]}",
            },
        )
    session.expire_all()
    return int(revision_id)


def adopt_baseline_facts(
    session: Session, project: "Project", *facts: "Fact", key: str | None = None
) -> int:
    """One accepted baseline revision that includes each Fact, at its own revision.

    The importer that writes this in production is #509's Adopt Baseline
    (``corridor.baseline_adoption``), and it takes a customer workbook, a
    registered field mapping and a preview to get here. A test that only needs
    the accepted record to exist has no reason to supply one, so this writes
    the rows directly.

    What it restates is the *shape* an adoption leaves behind, not a command's
    ordering: one revision, and one included decision per Fact, none of them
    superseded. ``move_accepted_value`` is where an ordering does matter, and
    ``tests/test_harness_fixtures.py`` pins that one against the real command.
    """

    decisions = [_decision_values(project, fact) for fact in facts]
    revision_id = accepted_revision(
        session, project.id, key=key or f"baseline:{uuid4().hex[:12]}"
    )
    with as_role(session, RECORD_DECISION_ROLE):
        for decision in decisions:
            _insert_decision(session, decision, revision_id=revision_id)
    session.expire_all()
    return revision_id


def move_accepted_value(session: Session, project: "Project", fact: "Fact") -> int:
    """Supersede the standing decision for this subject and field with a newer one.

    The accepted record moving under a coordinator mid-review is the exact
    condition #519 refuses on, so a test needs to reproduce it honestly: one
    later revision, one new effective decision, and the predecessor marked
    superseded rather than replaced.

    The command that does this in production is
    ``fact_decisions.record_human_fact_decision``, and this act cannot call it:
    that command admits only the nine ``HUMAN_DECISION_COMMANDS``, and the act
    being reproduced is a Resolve Delta. Recording it as a Coordinate Statement
    to get through the command's door would put a command_type on the revision
    naming an act nobody performed, which is worse than restating the SQL.

    So the SQL is restated, and the drift that leaves is guarded rather than
    trusted. ``tests/test_harness_fixtures.py`` runs the real command over a
    human-settled Fact and asserts the rows it leaves carry the same
    supersession shape this act writes, so a command that stopped marking its
    predecessor superseded -- or started marking it some other way -- fails
    there, instead of leaving every staleness fixture proving a shape
    production no longer produces.
    """

    session.flush()
    decision = _decision_values(project, fact)
    revision_id = accepted_revision(
        session,
        project.id,
        command_type="resolve_delta",
        principal="local:corrector",
        key=f"move:{uuid4().hex[:12]}",
    )
    with as_role(session, RECORD_DECISION_ROLE):
        # The successor's id is claimed before the predecessor is retired
        # against it: the partial unique index on the effective decision is not
        # deferrable, so the other order fails. The self-reference that holds
        # the pair together is what is deferred here.
        session.execute(text("set constraints all deferred"))
        successor = int(session.scalar(text("select nextval('fact_decisions_id_seq')")))
        predecessor = session.scalar(
            text(
                "select max(id) from fact_decisions where project_id = :project_id"
                " and subject_key = :subject_key and fact_type = :fact_type"
                " and superseded_by is null"
            ),
            decision,
        )
        if predecessor is not None:
            session.execute(
                text("update fact_decisions set superseded_by = :successor where id = :id"),
                {"successor": successor, "id": int(predecessor)},
            )
        _insert_decision(session, decision, revision_id=revision_id, decision_id=successor)
    session.expire_all()
    return revision_id


def _decision_values(project: "Project", fact: "Fact") -> dict[str, object]:
    """What a decision names, read before any role is borrowed.

    The record-decision role holds no read on ``facts`` or ``projects``, so an
    ORM attribute reached inside the borrow would fail for the wrong reason.
    """

    return {
        "project_id": project.id,
        "fact_id": fact.id,
        "subject_key": fact.subject_key,
        "fact_type": fact.fact_type,
    }


def _insert_decision(
    session: Session,
    decision: dict[str, object],
    *,
    revision_id: int,
    decision_id: int | None = None,
) -> None:
    """One included decision, at a claimed id when its predecessor named one."""

    session.execute(
        text(
            "insert into fact_decisions ("
            "id, project_id, fact_id, subject_key, fact_type, revision_id, disposition"
            ") values (coalesce(:id, nextval('fact_decisions_id_seq')), :project_id,"
            " :fact_id, :subject_key, :fact_type, :revision_id, 'include')"
        ),
        {**decision, "id": decision_id, "revision_id": revision_id},
    )
