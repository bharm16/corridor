"""Remove the spine rows a committed test scenario leaves behind (#521).

A few regressions commit a real project graph so a genuinely independent
Session can observe it, then delete that graph under
``session_replication_role = replica``. Since #451 every guided human act
dual-writes the spine, so a cleanup that removes only legacy tables leaves
facts, decisions, and revisions in the shared per-worker database and breaks
every later module that asserts an empty spine.

The spine table set is derived from the ORM metadata by name, never listed
by hand: ``tests/test_architecture.py`` fails when a table that depends on
the spine does not match ``SPINE_TABLE_PATTERN`` or lacks ``project_id``,
so a new support, delta, or decision table cannot recreate the leak
silently. Tables are deleted dependents-first, and the counts helper lets a
cleanup assert the spine is empty afterwards so the next missed dual-write
fails in the leaking test rather than in a victim.
"""

from __future__ import annotations

import re

from sqlalchemy import Table, delete, func, select
from sqlalchemy.orm import Session

from corridor.models import Base

SPINE_TABLE_PATTERN = re.compile(
    r"^(facts|fact_[a-z_]+|source_segments|source_fact_[a-z_]+|"
    r"project_record_[a-z_]+|project_baseline_[a-z_]+|"
    r"extracted_proposal_facts|recorded_verbal_[a-z_]+|"
    r"support_assessment[a-z_]*|proposed_delta[a-z_]*|delta_[a-z_]+|"
    # Not spine state: two report readings that *cite* the accepted revision
    # they were produced against (#602). They are deleted with the spine
    # because a cleanup that removed a revision and left one behind would
    # leave it pointing at a revision that no longer exists.
    r"report_runs|scheduled_report_publications|"
    # Not spine state either: a legacy Evidence Link's citation of the Source
    # Segment that owns its words (#605), deleted with the spine for the same
    # reason — a citation outliving its segment points at nothing.
    r"evidence_link_sources)$"
)
SPINE_ROOTS = frozenset({"facts", "source_segments", "project_record_revisions"})


def spine_tables() -> tuple[Table, ...]:
    """Every spine table, dependents first, so deletion never trips a key."""

    return tuple(
        table
        for table in reversed(Base.metadata.sorted_tables)
        if SPINE_TABLE_PATTERN.match(table.name)
    )


SPINE_TABLES = spine_tables()


def delete_project_spine(cleanup: Session, project_id: int) -> None:
    """Delete every spine row of one project inside an open replica-mode cleanup."""

    for table in SPINE_TABLES:
        cleanup.execute(delete(table).where(table.c.project_id == project_id))


def project_spine_counts(session: Session, project_id: int) -> dict[str, int]:
    return {
        table.name: session.scalar(
            select(func.count()).select_from(table).where(table.c.project_id == project_id)
        )
        for table in SPINE_TABLES
    }
