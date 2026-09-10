"""The durable handoff between revision producers and revision reconciliation.

Document Revision Processing and Automatic Support Update are expensive and
receipt-bearing: creating a Revision Comparison and running Carry-Forward each
leave durable rows, so re-deriving them on every idle scheduled tick would grow
the receipt log without bound. This module binds the one reconciliation
watermark (:mod:`corridor.reconciliation_watermark`) — the same loop
:mod:`corridor.record_inclusion` binds for Record Inclusion — to
``revision_reconciliation_requests`` and keeps the producer names callers use.
A producer — a registered Supersession edge or a changed Current Production
Run — calls :func:`request_revision_reconciliation` inside its own transaction,
bumping the project's ``dirty_seq``. A rolled-back producer therefore leaves no
revision work, and a committed one leaves a durable marker that outlives the
process. Reconciliation is pending exactly while ``dirty_seq > reconciled_seq``;
repeated bumps for the same project coalesce into a single pending pass.

The consuming side — discovery, exact comparison, integrity read, and the
released Automatic Support Update Rules — lives in
:mod:`corridor.revision_reconciliation` beside the pass it gates. Keeping this
producer API here, depending on nothing but the watermark and the model, is what
lets the Supersession and Active Run seams bump the watermark without importing
the Carry-Forward stack (which itself reads those seams) and forming an import
cycle.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from corridor.models import RevisionReconciliationRequest
from corridor.reconciliation_watermark import ReconciliationWatermark

REVISION_RECONCILIATION = ReconciliationWatermark(
    RevisionReconciliationRequest, "revision reconciliation"
)


def request_revision_reconciliation(
    session: Session, project_id: int, reason: str
) -> None:
    """Mark a project's revision reconciliation pending, in the caller's transaction."""

    REVISION_RECONCILIATION.request(session, project_id, reason)


def revision_reconciliation_pending(session: Session, project_id: int) -> bool:
    """Whether the project has unreconciled revision work."""

    return REVISION_RECONCILIATION.pending(session, project_id)


def pending_revision_reconciliation_project_ids(session: Session) -> list[int]:
    """Every project with unreconciled revision work, for a recovery drain."""

    return REVISION_RECONCILIATION.pending_project_ids(session)
