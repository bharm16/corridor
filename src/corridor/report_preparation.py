"""The weekly reading the change summary and weekly report are prepared from (#488).

ADR-0075 promises four first-slice outputs, and ADR-0086 makes them one release
package. ``report_publication`` already retains the Coordination Report half of
that package. The other half — what actually changed since the last weekly
artifact — had no producer at all, so the renderer #534 will write would have
had to recount a week of delta lifecycle at render time and could not say which
accepted revision its counts belonged to.

This pass is that producer. It reads the Proposed Delta lifecycle since the
previous prepared reading and returns a bounded count of it, bound to the
project's current accepted revision. ADR-0084 governs the shape: a deferred
delta is not resolved and is not silently mixed into the open work either, so
resolved, deferred, and actionable-open are three separate readings rather than
one total.

**The window is a watermark pair, not a pair of timestamps.** The first version
of this pass bounded the week with ``created_at > previous observed_at``, and
that compares two different clocks: ``observed_at`` is the logical time the
runtime's clock supplied, while ``ProposedDelta.created_at`` is whatever
PostgreSQL's ``now()`` returned when the row was inserted. The two agree only
by coincidence, and they diverge for every reason that matters — a replayed or
backfilled reading, a schedule that ran late, skew between the application and
database clocks — at which point a delta counted last week is counted new again
this week, or a resolution recorded a moment late is never counted at all.
Append-only identifiers have none of that trouble: the reading covers
``(previous watermark, current watermark]`` on ``proposed_deltas.id`` and
``delta_dispositions.id``, exactly as the delta-generation pass resumes from
``through_fact_id``. ``window_start`` survives only as the human sentence the
summary prints; nothing is filtered by it.

Both ends of each range are taken in this reading, so a row inserted while the
pass runs falls in the next reading rather than being counted twice or lost —
statements in one READ COMMITTED transaction do not share a snapshot.

It writes nothing. A weekly counting pass that needed a table of its own would
need a migration, and the runtime already retains one durable append-only
receipt per attempt; the reading and its watermarks stand on that receipt. That
also makes the pass a read-only Due Work handler: the runtime finalizes its
result and no domain lock is held.
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


def previous_reading(session: Session, schedule_id: int) -> dict[str, Any]:
    """This schedule's newest completed reading, or an empty mapping.

    Ordered by receipt identity rather than ``finished_at``: the identifier is
    monotonic in insertion order no matter what any clock said, and "newest"
    here must mean the last one retained.
    """

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
        .order_by(DueWorkReceipt.id.desc())
        .limit(1)
    ).first()
    return dict(result or {})


def execute_report_preparation(
    session: Session, schedule: DueWorkSchedule, observed_at: datetime
) -> dict[str, Any]:
    """Count one project's delta lifecycle since the last retained reading."""

    project_id = schedule.project_id
    observed_at = _aware_utc(observed_at)
    previous = previous_reading(session, schedule.id)
    delta_floor = int(previous.get("through_delta_id") or 0)
    disposition_floor = int(previous.get("through_disposition_id") or 0)

    # Both ceilings are fixed before anything is counted, so the reading covers
    # one exact half-open range of identifiers and can be recomputed from the
    # receipt alone.
    delta_ceiling = int(
        session.scalar(
            select(func.max(ProposedDelta.id)).where(
                ProposedDelta.project_id == project_id
            )
        )
        or 0
    )
    disposition_ceiling = int(
        session.scalar(
            select(func.max(DeltaDisposition.id)).where(
                DeltaDisposition.project_id == project_id
            )
        )
        or 0
    )

    accepted_revision_id = session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == project_id
        )
    )

    resolved = {"accept": 0, "edit": 0, "reject": 0}
    for disposition, count in session.execute(
        select(DeltaDisposition.disposition, func.count())
        .where(
            DeltaDisposition.project_id == project_id,
            DeltaDisposition.id > disposition_floor,
            DeltaDisposition.id <= disposition_ceiling,
        )
        .group_by(DeltaDisposition.disposition)
    ).all():
        if disposition in resolved:
            resolved[disposition] = int(count)

    proposed_new = int(
        session.scalar(
            select(func.count())
            .select_from(ProposedDelta)
            .where(
                ProposedDelta.project_id == project_id,
                ProposedDelta.id > delta_floor,
                ProposedDelta.id <= delta_ceiling,
            )
        )
        or 0
    )

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
                ProposedDelta.id <= delta_ceiling,
                ~ProposedDelta.id.in_(select(DeltaDisposition.delta_id)),
                ~ProposedDelta.id.in_(select(DeltaSupersession.prior_delta_id)),
            )
        ).all()
    )
    # Unlike the window, this is a genuine comparison of two domain times: a
    # deferral's wake time is a value a person chose, and the question is
    # whether it has arrived as of the moment this reading is taken. Neither
    # side is a row-insertion timestamp.
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
        # The date the summary prints ("changes since ..."). It is a label:
        # the counts above are bounded by the watermarks, never by this.
        "window_start": str(previous.get("observed_at") or ""),
        "through_delta_id": delta_ceiling,
        "through_disposition_id": disposition_ceiling,
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
