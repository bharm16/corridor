"""The role a test borrows to write accepted authority, written down once.

Accepted authority is a database boundary rather than a convention (#492), so a
test that needs an accepted revision or decision to exist first has to become
the record-decision role and then stop being it. Twenty-two call sites wrote
that pair by hand, and one of them (``tests/test_delta_resolution.py``'s
refusal walk) had no ``finally``: a body that raised left the role set for the
rest of the transaction, so the *next* write in the same test was silently
performed by the wrong principal and the assertion it was making no longer
meant what it said.

``as_record_decision_role`` is that pair with the reset in a ``finally``, and
it names the role from ``corridor.db_roles`` so a rename cannot leave a test
setting a role that no longer exists.

``adopt_baseline_fact`` is the one act four modules wrote out identically: the
accepted baseline revision a staleness or supersession question is judged
against, and the decision that includes one Fact in it. The importer that
writes it in production is #509; a test that only needs the accepted record to
exist has no reason to restate its SQL.
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
def as_record_decision_role(session: Session) -> Iterator[Session]:
    """Write as the record-decision role, and reset it however the body ends."""

    session.execute(text(f"set local role {RECORD_DECISION_ROLE}"))
    try:
        yield session
    finally:
        session.execute(text("reset role"))


def adopt_baseline_fact(
    session: Session, project: "Project", fact: "Fact", key: str | None = None
) -> int:
    """One accepted baseline decision for a subject and field, at its own revision."""

    with as_record_decision_role(session):
        revision_id = session.scalar(
            text(
                "insert into project_record_revisions ("
                "project_id, command_type, human_principal, idempotency_key"
                ") values (:project_id, 'adopt_baseline', 'local:adopter', :key)"
                " returning id"
            ),
            {"project_id": project.id, "key": key or f"baseline:{uuid4().hex[:12]}"},
        )
        session.execute(
            text(
                "insert into fact_decisions ("
                "project_id, fact_id, subject_key, fact_type, revision_id, disposition"
                ") values (:project_id, :fact_id, :subject_key, :fact_type,"
                " :revision_id, 'include')"
            ),
            {
                "project_id": project.id,
                "fact_id": fact.id,
                "subject_key": fact.subject_key,
                "fact_type": fact.fact_type,
                "revision_id": revision_id,
            },
        )
    session.expire_all()
    return int(revision_id)
