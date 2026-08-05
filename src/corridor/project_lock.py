"""Serialize project-scoped lineage decisions that must not race.

Supersession registration, Active Run declaration, and Candidate mutation all
change which rows are actionable.  They take the same project-row lock before
reading that scope, so each decision is made against one committed ordering
rather than an unlocked snapshot that can become stale before commit.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Project


def lock_project(session: Session, project_id: int) -> None:
    """Hold the project's row lock until the surrounding transaction ends."""

    locked_id = session.scalar(
        select(Project.id).where(Project.id == project_id).with_for_update()
    )
    if locked_id is None:
        raise ValueError(f"project {project_id} does not exist")
