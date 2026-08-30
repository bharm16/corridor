"""The durable handoff between revision producers and revision reconciliation.

Document Revision Processing and Automatic Support Update are expensive and
receipt-bearing: creating a Revision Comparison and running Carry-Forward each
leave durable rows, so re-deriving them on every idle scheduled tick would grow
the receipt log without bound. This module is the producer side of the same
recoverable watermark that :mod:`corridor.record_inclusion` uses for Record
Inclusion. A producer — a registered Supersession edge or a changed Current
Production Run — calls :func:`request_revision_reconciliation` inside its own
transaction, bumping the project's ``dirty_seq``. A rolled-back producer
therefore leaves no revision work, and a committed one leaves a durable marker
that outlives the process. Reconciliation is pending exactly while
``dirty_seq > reconciled_seq``; repeated bumps for the same project coalesce
into a single pending pass.

The consuming side — discovery, exact comparison, integrity read, and the
released Automatic Support Update Rules — lives in
:mod:`corridor.revision_reconciliation` beside the pass it gates. Keeping this
producer API here, depending on nothing but the model, is what lets the
Supersession and Active Run seams bump the watermark without importing the
Carry-Forward stack (which itself reads those seams) and forming an import
cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor.models import RevisionReconciliationRequest


def request_revision_reconciliation(
    session: Session, project_id: int, reason: str
) -> None:
    """Mark a project's revision reconciliation pending, in the caller's transaction.

    Bumps ``dirty_seq`` by one and coalesces: any number of requests between two
    reconciliations leave the project pending exactly once. The producer must
    call this in the same transaction that commits the structural change it is
    handing off, so a rolled-back producer bumps nothing.
    """

    if not reason:
        raise ValueError("a revision reconciliation request must state a reason")
    now = datetime.now(timezone.utc)
    statement = insert(RevisionReconciliationRequest).values(
        project_id=project_id,
        dirty_seq=1,
        reconciled_seq=0,
        last_reason=reason,
        requested_at=now,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[RevisionReconciliationRequest.project_id],
            set_={
                "dirty_seq": RevisionReconciliationRequest.dirty_seq + 1,
                "last_reason": reason,
                "requested_at": now,
            },
        )
    )


def revision_reconciliation_pending(session: Session, project_id: int) -> bool:
    """Whether the project has unreconciled revision work."""

    row = session.get(RevisionReconciliationRequest, project_id)
    return row is not None and row.dirty_seq > row.reconciled_seq


def pending_revision_reconciliation_project_ids(session: Session) -> list[int]:
    """Every project with unreconciled revision work, for a recovery drain."""

    return list(
        session.scalars(
            select(RevisionReconciliationRequest.project_id)
            .where(
                RevisionReconciliationRequest.dirty_seq
                > RevisionReconciliationRequest.reconciled_seq
            )
            .order_by(RevisionReconciliationRequest.project_id)
        ).all()
    )
