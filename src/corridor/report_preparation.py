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

External issue preparation reuses ``count_delta_window`` with the previous
authorized package's floors and its request's frozen ceilings (#709). Its
counts are a separate reading; the internal weekly receipt stays unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.due_work_contract import (
    DueWorkScheduling,
    HandlerRegistration,
    ResolvedSchedule,
    ValidatedDeclaration,
    gate7_configuration,
    previous_completed_reading,
    validate_scheduling,
)
from corridor.models import (
    DeltaDeferral,
    DeltaDisposition,
    DeltaSupersession,
    DueWorkSchedule,
    ProjectRecordRevision,
    ProposedDelta,
)

# The one server-owned handler key this module's work runs under.  It matches
# ``due_work.HANDLER_REPORT_PREPARATION``; the constant lives here because this
# module is the lower layer and the runtime imports its execution, never the
# reverse.
HANDLER_KEY = "report_preparation"
AUTHORIZED_PACKAGE_COMPARISON = "previous_authorized_package"

_RESULT_SCHEMA_VERSION = "report-preparation-result-v1"


def previous_reading(session: Session, schedule_id: int) -> dict[str, Any]:
    """This schedule's newest completed reading, or an empty mapping.

    Which retained receipt counts as "newest" is the runtime's rule, not this
    module's, so the read itself lives in the Due Work contract.
    """

    return previous_completed_reading(
        session, schedule_id=schedule_id, handler_key=HANDLER_KEY
    )


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

    window_counts = count_delta_window(
        session,
        project_id=project_id,
        delta_floor=delta_floor,
        delta_ceiling=delta_ceiling,
        disposition_floor=disposition_floor,
        disposition_ceiling=disposition_ceiling,
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
        **window_counts,
        "open_actionable": len(open_ids - deferred_ids),
        "open_deferred": len(deferred_ids),
        "superseded": superseded,
    }


def count_delta_window(
    session: Session,
    *,
    project_id: int,
    delta_floor: int,
    delta_ceiling: int,
    disposition_floor: int,
    disposition_ceiling: int,
) -> dict[str, int]:
    """Count only the append-only rows inside a caller's frozen watermarks.

    Weekly schedules and authorized issues have different predecessors. They
    share the counting rule, while each caller owns both ends of its window.
    Current open/deferred standing stays on the original preparation receipt.
    """

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
    return {
        "resolved_accepted": resolved["accept"],
        "resolved_edited": resolved["edit"],
        "resolved_rejected": resolved["reject"],
        "proposed_new": proposed_new,
    }


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()


# --- The Due Work declaration this reading runs under ----------------------
#
# A weekly reading that counts and writes nothing is this module's own claim
# about its work; the runtime keeps the lease and the receipt (card 6).


@dataclass(frozen=True)
class ReportPreparationDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables the weekly change reading.

    The reading counts one project's Proposed Delta lifecycle over the week and
    writes nothing, so it is weekly rather than hourly, reads no model, and
    authorizes no destination: what the change summary and weekly report are
    rendered from is a reading, and releasing either stays a separate
    designated-human act (ADR-0040, ADR-0086).
    """

    handler_key: ClassVar[str] = HANDLER_KEY

    @classmethod
    def released_weekly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
    ) -> "ReportPreparationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            starts_at=starts_at,
            cadence="weekly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


def _validated_declaration(
    declaration: ReportPreparationDeclaration,
) -> ValidatedDeclaration:
    """Validate one report-preparation declaration."""

    starts_at = validate_scheduling(
        declaration, subject="report-preparation", cadence="weekly"
    )
    scope = {"project_id": declaration.project_id}
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_KEY,
            scope=scope,
            input_identity={
                "kind": "project_change_summary_reading-v1",
                "project_id": declaration.project_id,
            },
            idempotency_contract="read_only_reconcilable",
            starts_at=starts_at,
            # The week the reading covers starts where the previous retained
            # reading observed, so consecutive readings tile without a gap and
            # without counting one resolution twice.
            extra={"comparison_window_policy": "since_last_prepared_reading"},
        ),
        input_identity={
            "handler": HANDLER_KEY,
            "project_id": declaration.project_id,
            "source": "proposed_delta_lifecycle-v1",
        },
    )


def _stored_declaration(stored: ResolvedSchedule) -> ReportPreparationDeclaration:
    return ReportPreparationDeclaration(**stored.scheduling_fields())


DUE_WORK_REGISTRATION = HandlerRegistration(
    key=HANDLER_KEY,
    scope_kind="one_project_change_summary_reading",
    idempotency_contract="read_only_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=0,
    declaration_type=ReportPreparationDeclaration,
    validate=_validated_declaration,
    stored_declaration=_stored_declaration,
    # A reading runs inside the runtime's own transaction and returns its result.
    run=execute_report_preparation,
)
