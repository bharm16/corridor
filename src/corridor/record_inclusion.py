"""The durable handoff between record producers and Record Inclusion.

``load_project`` (declare Active Runs, admit dependencies, attach events) is
idempotent — it skips what is already on the record — but it is not free: each
of its two admission passes appends a PolicyRun unconditionally, even when it
applies and abstains nothing. So reconciling on every idle scheduled tick would
grow the receipt log without bound, which #342 forbids. A process-local
post-commit callback was rejected as the delivery mechanism: it cannot survive
the process exiting between the producing commit and the load.

This module binds the one reconciliation watermark
(:mod:`corridor.reconciliation_watermark`) to ``record_inclusion_requests`` and
keeps the producer names callers use. A producer — one completed Extraction Run,
a declared Active Run, a committed organization identity, a Carry-Forward that
moved support — calls :func:`request_record_inclusion` inside its own
transaction, bumping the project's ``dirty_seq``. A rolled-back producer
therefore leaves no work, and a committed one leaves a durable marker that
outlives the process. Reconciliation is pending exactly while
``dirty_seq > reconciled_seq``; repeated requests for unchanged inputs coalesce
into a single pending pass.

The consuming side — ``reconcile_record_inclusion``, which hands ``load_project``
to the watermark's lock-snapshot-advance loop — lives in
:mod:`corridor.admission` beside the load it gates. Keeping the producer API
here (it depends on nothing but the watermark and the model) is what lets
``extraction_runs`` bump the watermark without importing the admission stack.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from corridor.models import RecordInclusionRequest
from corridor.reconciliation_watermark import ReconciliationWatermark

RECORD_INCLUSION = ReconciliationWatermark(RecordInclusionRequest, "Record Inclusion")


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
    """Mark a project's Record Inclusion pending, inside the caller's transaction."""

    RECORD_INCLUSION.request(session, project_id, reason)


def record_inclusion_pending(session: Session, project_id: int) -> bool:
    """Whether the project has unreconciled Record Inclusion work."""

    return RECORD_INCLUSION.pending(session, project_id)


def pending_record_inclusion_project_ids(session: Session) -> list[int]:
    """Every project with unreconciled Record Inclusion work, for recovery drain."""

    return RECORD_INCLUSION.pending_project_ids(session)
