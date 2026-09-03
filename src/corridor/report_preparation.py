"""The weekly reading the change summary and weekly report are prepared from (#488).

ADR-0075 promises four first-slice outputs, and ADR-0086 makes them one release
package. ``report_publication`` already retains the Coordination Report half of
that package. The other half — what actually changed since the last weekly
artifact — had no producer at all, so the renderer #534 will write would have
had to recount a week of delta lifecycle at render time and could not say which
accepted revision its counts belonged to.

This pass is that producer. It reads the Proposed Delta lifecycle over the
window since the previous prepared reading and returns a bounded, self-dating
count of it, bound to the project's current accepted revision. ADR-0084 governs
the shape: a deferred delta is not resolved and is not silently mixed into the
open work either, so resolved, deferred, and actionable-open are three separate
readings rather than one total.

It writes nothing. A weekly counting pass that needed a table of its own would
need a migration, and the runtime already retains one durable append-only
receipt per attempt; the reading stands on that receipt, and the window of the
next reading starts where this one observed. That also makes the pass a
read-only Due Work handler: the runtime finalizes its result and no domain lock
is held.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import (
    DeltaDeferral,
    DeltaDisposition,
    DeltaSupersession,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    ProjectRecordRevision,
    ProposedDelta,
)

# The one server-owned handler key this module's work runs under.  It matches
# ``due_work.HANDLER_REPORT_PREPARATION``; the constant lives here because this
# module is the lower layer and the runtime imports its execution, never the
# reverse.
HANDLER_KEY = "report_preparation"

_RESULT_SCHEMA_VERSION = "report-preparation-result-v1"


def previous_reading_observed_at(
    session: Session, schedule_id: int
) -> datetime | None:
    """When this schedule's newest completed attempt observed, if there is one."""

    result = session.scalars(
        select(DueWorkReceipt.handler_result_json)
        .join(
            DueWorkOccurrence,
            DueWorkOccurrence.id == DueWorkReceipt.occurrence_id,
        )
        .where(
            DueWorkOccurrence.scheduled_job_id == schedule_id,
            DueWorkReceipt.handler_key == HANDLER_KEY,
            DueWorkReceipt.execution_outcome == "completed",
        )
        .order_by(DueWorkReceipt.finished_at.desc(), DueWorkReceipt.id.desc())
        .limit(1)
    ).first()
    observed = (result or {}).get("observed_at")
    return datetime.fromisoformat(observed) if observed else None


def execute_report_preparation(
    session: Session, schedule: DueWorkSchedule, observed_at: datetime
) -> dict[str, Any]:
    """Count one project's delta lifecycle over the window since the last reading."""

    project_id = schedule.project_id
    observed_at = _aware_utc(observed_at)
    window_start = previous_reading_observed_at(session, schedule.id)

    accepted_revision_id = session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == project_id
        )
    )

    resolved = {"accept": 0, "edit": 0, "reject": 0}
    disposition_query = select(
        DeltaDisposition.disposition, func.count()
    ).where(DeltaDisposition.project_id == project_id)
    if window_start is not None:
        disposition_query = disposition_query.where(
            DeltaDisposition.decided_at > window_start
        )
    for disposition, count in session.execute(
        disposition_query.group_by(DeltaDisposition.disposition)
    ).all():
        if disposition in resolved:
            resolved[disposition] = int(count)

    proposed_query = select(func.count()).select_from(ProposedDelta).where(
        ProposedDelta.project_id == project_id
    )
    if window_start is not None:
        proposed_query = proposed_query.where(ProposedDelta.created_at > window_start)
    proposed_new = int(session.scalar(proposed_query) or 0)

    superseded = int(
        session.scalar(
            select(func.count())
            .select_from(DeltaSupersession)
            .where(DeltaSupersession.project_id == project_id)
        )
        or 0
    )

    # An open delta is one no disposition resolved and no newer revision
    # superseded. ADR-0084 keeps a live deferral out of the actionable count
    # without pretending the delta was resolved.
    open_ids = set(
        session.scalars(
            select(ProposedDelta.id).where(
                ProposedDelta.project_id == project_id,
                ~ProposedDelta.id.in_(select(DeltaDisposition.delta_id)),
                ~ProposedDelta.id.in_(select(DeltaSupersession.prior_delta_id)),
            )
        ).all()
    )
    deferred_ids = (
        set(
            session.scalars(
                select(DeltaDeferral.delta_id).where(
                    DeltaDeferral.project_id == project_id,
                    DeltaDeferral.delta_id.in_(open_ids),
                    (DeltaDeferral.deferred_until.is_(None))
                    | (DeltaDeferral.deferred_until > observed_at),
                )
            ).all()
        )
        if open_ids
        else set()
    )

    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "project_id": project_id,
        "configuration_version": schedule.configuration_version,
        "observed_at": _iso(observed_at),
        # A change summary states what changed against one accepted revision.
        # Without an accepted record there is no baseline to state it against,
        # and the operator's next step is Adopt Baseline, not another reading.
        "health": (
            "healthy"
            if accepted_revision_id is not None
            else "preparation_attention_required"
        ),
        "window_start": _iso(window_start) if window_start is not None else "",
        "accepted_revision_id": int(accepted_revision_id or 0),
        "resolved_accepted": resolved["accept"],
        "resolved_edited": resolved["edit"],
        "resolved_rejected": resolved["reject"],
        "proposed_new": proposed_new,
        "open_actionable": len(open_ids - deferred_ids),
        "open_deferred": len(deferred_ids),
        "superseded": superseded,
    }


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()
