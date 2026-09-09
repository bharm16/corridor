"""Plain projection compatibility names and measured query timing.

Rendering equivalence belongs to reader_equivalence (#458); projection clients
no longer inherit the report, workbook and PDF rendering stack. Existing gate
callers are forwarded lazily while they migrate to the owning module.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.record_projection import (
    CurrentRecordValue,
    CurrentStatementTiming,
    read_current_project_record,
    read_project_record_as_of_revision,
)

__all__ = ["CurrentRecordValue", "CurrentStatementTiming", "ViewPerformance",
           "read_current_project_record", "read_project_record_as_of_revision",
           "measure_current_record_view", "ReaderEquivalence",
           "freeze_project_reading_from_current_view", "prove_reader_equivalence"]


@dataclass(frozen=True)
class ViewPerformance:
    project_id: int
    row_count: int
    planning_time_ms: float
    execution_time_ms: float
    materialized_view_needed: bool


def measure_current_record_view(
    session: Session,
    project_id: int,
    *,
    slow_threshold_ms: float = 50.0,
) -> ViewPerformance:
    plan = session.scalar(
        text(
            "explain (analyze, format json) "
            "select * from current_project_record where project_id = :project_id"
        ),
        {"project_id": project_id},
    )[0]
    row_count = len(read_current_project_record(session, project_id))
    execution = float(plan["Execution Time"])
    return ViewPerformance(
        project_id=project_id,
        row_count=row_count,
        planning_time_ms=float(plan["Planning Time"]),
        execution_time_ms=execution,
        materialized_view_needed=execution > slow_threshold_ms,
    )



def __getattr__(name):
    if name in {"ReaderEquivalence", "freeze_project_reading_from_current_view",
                "prove_reader_equivalence", "_pdf_text", "_workbook_cells"}:
        from corridor import reader_equivalence
        return getattr(reader_equivalence, name)
    raise AttributeError(name)
