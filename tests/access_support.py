"""Seed project membership for web tests that drive real HTTP routes (#331).

The web ``client`` fixtures override identity to a fixed acting principal; under
the #331 access gate that principal must also be an enrolled project member.
These helpers grant that membership directly (no audit noise), so existing
domain-behavior tests keep exercising the real membership and designation gate
without turning into sign-in tests.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select

from corridor import access
from corridor.models import ProjectRosterEntry
from corridor.principals import HumanPrincipal

_COLUMNS = {
    access.COORDINATION: "can_coordinate",
    access.DOCUMENTATION_REVIEW: "can_review_documentation",
    access.EXTERNAL_RELEASE: "can_release_externally",
    access.TECHNICAL_OPERATIONS: "is_technical_operator",
}


def seed_membership(
    session,
    project,
    principal: HumanPrincipal,
    *,
    designations: Iterable[str] = access.DESIGNATIONS,
    active: bool = True,
    display_name: str | None = None,
) -> ProjectRosterEntry:
    """Make ``principal`` a member of ``project`` with exactly ``designations``."""
    granted = set(designations)
    entry = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.project_id == project.id,
            ProjectRosterEntry.principal_subject == principal.subject,
        )
    ).first()
    if entry is None:
        entry = ProjectRosterEntry(
            project_id=project.id,
            principal_subject=principal.subject,
            display_name=display_name or principal.subject,
        )
        session.add(entry)
    elif display_name is not None:
        entry.display_name = display_name
    entry.active = active
    for name, column in _COLUMNS.items():
        setattr(entry, column, name in granted)
    session.flush()
    return entry


def request_scoped(session):
    """Give each simulated request the transaction boundary a request really has.

    ``corridor.web.app.get_session`` opens a session for one request and closes
    it when the response is finished, and every project surface commits before
    it returns, so in the deployment **one request is one transaction**. The
    project-authorization scope the request declares to PostgreSQL is
    transaction-local, so it ends with that transaction and the next request
    starts with none (#657, #662).

    A web test shares one rollback-scoped transaction with the app, and
    overriding ``get_session`` with that shared session alone models the
    request boundary as *nothing at all*: two ``client.get`` calls in one test
    run inside one transaction, so the second inherits the scope the first
    declared and is refused a scope of its own — a shape the deployment cannot
    produce, because it has no transaction that spans two requests.

    A savepoint per request is that boundary. A transaction-local setting is
    restored when a subtransaction aborts and kept when one is released
    (measured against PostgreSQL 16 by #654), so the savepoint has to be rolled
    back, and rolling it back also discards what the request wrote. Inside one
    transaction those are the same decision: a request cannot both keep its
    rows and give up its scope. **So this boundary suits a test whose requests
    only read.** A test that needs a request's writes to survive into the next
    request keeps the shared session, and takes the deployment's own rule with
    it: one such transaction declares one scope.

    The route's own ``session.commit()`` releases the savepoints the session
    owns, so the request's boundary is taken on the connection underneath it,
    where the route cannot release it. The test's own pending work is committed
    into the surrounding rollback-scoped transaction first, which is where it
    belongs: it is the state the request reads, not part of the request.
    """

    def request_session():
        if session.in_transaction():
            session.commit()
        boundary = session.get_bind().begin_nested()
        try:
            yield session
        finally:
            if session.in_transaction():
                session.rollback()
            if boundary.is_active:
                boundary.rollback()

    return request_session
