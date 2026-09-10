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
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.db_roles import RECORD_DECISION_ROLE


@contextmanager
def as_record_decision_role(session: Session) -> Iterator[Session]:
    """Write as the record-decision role, and reset it however the body ends."""

    session.execute(text(f"set local role {RECORD_DECISION_ROLE}"))
    try:
        yield session
    finally:
        session.execute(text("reset role"))
