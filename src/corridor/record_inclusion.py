"""The durable handoff between record producers and Record Inclusion.

``load_project`` (declare Active Runs, admit dependencies, attach events) is
idempotent — it skips what is already on the record — but it is not free: each
of its two admission passes appends a PolicyRun unconditionally, even when it
applies and abstains nothing. So reconciling on every idle scheduled tick would
grow the receipt log without bound, which #342 forbids. A process-local
post-commit callback was rejected as the delivery mechanism: it cannot survive
the process exiting between the producing commit and the load.

This module is the producer side of the recoverable watermark. A producer —
today, one completed Extraction Run; later, an approved identity or fact change —
calls :func:`request_record_inclusion` inside its own transaction, bumping the
project's ``dirty_seq``. A rolled-back producer therefore leaves no work, and a
committed one leaves a durable marker that outlives the process. Reconciliation
is pending exactly while ``dirty_seq > reconciled_seq``; repeated requests for
unchanged inputs coalesce into a single pending pass.

The consuming side — ``reconcile_record_inclusion``, which locks the row and, only
when it is pending, runs ``load_project`` and advances ``reconciled_seq`` — lives
in :mod:`corridor.admission` beside the load it gates. Keeping the producer API
here (it depends on nothing but the model) is what lets ``extraction_runs`` bump
the watermark without importing the admission stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor.models import RecordInclusionRequest


@dataclass(frozen=True)
class ReconcileResult:
    """What one watermark-gated reconciliation did.

    ``did_load`` is False for the no-op case — nothing was pending, so no
    ``load_project`` ran and no PolicyRun was appended. ``load`` carries the
    ``LoadResult`` when a load did run, and is ``None`` otherwise.
    """

    project_id: int
    did_load: bool
    reconciled_seq: int
    load: object | None = None


def request_record_inclusion(
    session: Session, project_id: int, reason: str
) -> None:
    """Mark a project's Record Inclusion pending, inside the caller's transaction.

    Bumps ``dirty_seq`` by one and coalesces: any number of requests between two
    reconciliations leave the project pending exactly once. The producer must
    call this in the same transaction that commits the record it is handing off,
    so a rolled-back producer bumps nothing.
    """

    if not reason:
        raise ValueError("a Record Inclusion request must state a reason")
    now = datetime.now(timezone.utc)
    existing = session.get(RecordInclusionRequest, project_id)
    statement = insert(RecordInclusionRequest).values(
        project_id=project_id,
        dirty_seq=1,
        reconciled_seq=0,
        last_reason=reason,
        requested_at=now,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[RecordInclusionRequest.project_id],
            set_={
                "dirty_seq": RecordInclusionRequest.dirty_seq + 1,
                "last_reason": reason,
                "requested_at": now,
            },
        )
    )
    # The PostgreSQL upsert changes an already-loaded watermark outside the ORM
    # identity map. Expire it so a writer that immediately asks whether work is
    # pending sees the durable sequence it just advanced.
    if existing is not None:
        session.expire(existing)


def record_inclusion_pending(session: Session, project_id: int) -> bool:
    """Whether the project has unreconciled Record Inclusion work."""

    row = session.get(RecordInclusionRequest, project_id)
    return row is not None and row.dirty_seq > row.reconciled_seq


def pending_record_inclusion_project_ids(session: Session) -> list[int]:
    """Every project with unreconciled Record Inclusion work, for recovery drain."""

    return list(
        session.scalars(
            select(RecordInclusionRequest.project_id)
            .where(
                RecordInclusionRequest.dirty_seq
                > RecordInclusionRequest.reconciled_seq
            )
            .order_by(RecordInclusionRequest.project_id)
        ).all()
    )
