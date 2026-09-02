"""Remove the spine rows a committed test scenario leaves behind (#521).

A few regressions commit a real project graph so a genuinely independent
Session can observe it, then delete that graph under
``session_replication_role = replica``. Since #451 every guided human act
dual-writes the spine, so a cleanup that removes only legacy tables leaves
facts, decisions, and revisions in the shared per-worker database and breaks
every later module that asserts an empty spine. Every spine table is
project-scoped; this helper deletes them in dependency order and asserts the
spine is empty afterwards, so the next dual-write a cleanup misses fails in
the leaking test rather than in a victim.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from corridor.models import (
    ExtractedProposalFact,
    Fact,
    FactAppliesTo,
    FactClosureResult,
    FactClosureSource,
    FactDecision,
    FactDisposition,
    FactSource,
    FactStatementTiming,
    ProjectRecordRevision,
    SourceFactAppendReceipt,
    SourceSegment,
)

SPINE_TABLES = (
    FactDecision,
    FactDisposition,
    ProjectRecordRevision,
    FactSource,
    FactAppliesTo,
    FactClosureResult,
    FactClosureSource,
    FactStatementTiming,
    ExtractedProposalFact,
    SourceFactAppendReceipt,
    Fact,
    SourceSegment,
)


def delete_project_spine(cleanup: Session, project_id: int) -> None:
    """Delete every spine row of one project inside an open replica-mode cleanup."""

    for spine_table in SPINE_TABLES:
        cleanup.execute(delete(spine_table).where(spine_table.project_id == project_id))


def project_spine_counts(session: Session, project_id: int) -> dict[str, int]:
    return {
        spine_table.__tablename__: session.scalar(
            select(func.count())
            .select_from(spine_table)
            .where(spine_table.project_id == project_id)
        )
        for spine_table in SPINE_TABLES
    }
