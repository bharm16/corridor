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
