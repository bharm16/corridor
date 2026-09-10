"""One durable, coalescing reconciliation watermark, bound to a request model.

A producer commits a record change that leaves a project needing an expensive,
receipt-bearing pass — Record Inclusion appends a PolicyRun on every
``load_project``; Document Revision Processing and Automatic Support Update each
leave durable rows. Running such a pass on every idle scheduled tick would grow
the receipt log without bound (#342), and a process-local post-commit callback
cannot survive the process exiting between the producing commit and the pass.
So each pass is gated by a durable watermark row: a producer bumps ``dirty_seq``
inside its own transaction (a rolled-back producer bumps nothing; a committed
one leaves a marker that outlives the process), work is pending exactly while
``dirty_seq > reconciled_seq``, repeated bumps coalesce into one pending pass,
and the consumer locks the row, snapshots ``dirty_seq``, runs the pass, and
advances ``reconciled_seq`` to the exact snapshot it observed — so a bump that
arrives during the pass keeps the project pending for the next one.

Before this module that mechanism existed twice, once per request table
(``record_inclusion`` with its consumer in ``admission``;
``revision_reconciliation_request`` with its consumer in
``revision_reconciliation``), and the copies had already diverged on a
correctness fix: only the Record Inclusion producer expired the ORM-loaded row
after its PostgreSQL upsert (#398), so a session that had already loaded the
revision watermark read a stale identity-map value when it asked whether work
was pending. The four moves — request, pending, drain, reconcile under lock —
now live here once, parameterised by the request model they own. The two
request modules bind a watermark each and keep their public names; both tables
stay, and each consumer keeps its own pass body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor.models import RecordInclusionRequest, RevisionReconciliationRequest

T = TypeVar("T")


@dataclass(frozen=True)
class WatermarkPass(Generic[T]):
    """What one watermark-gated reconciliation did.

    ``was_pending`` is False when the watermark was clean under the lock;
    ``ran`` says whether the pass body ran (it did not for a clean watermark
    unless the consumer asked to run when clean). ``outcome`` carries the pass
    body's return value when it ran and is ``None`` otherwise.
    """

    project_id: int
    was_pending: bool
    ran: bool
    reconciled_seq: int
    outcome: T | None = None


@dataclass(frozen=True)
class ReconciliationWatermark:
    """The producer and consumer moves over one request table.

    ``model`` is the request row class (one row per project, keyed by
    ``project_id``, carrying ``dirty_seq``, ``reconciled_seq``, ``last_reason``,
    ``requested_at`` and ``reconciled_at``). ``label`` names the work in the
    refusal a reason-less request receives.
    """

    model: type[RecordInclusionRequest] | type[RevisionReconciliationRequest]
    label: str

    def request(self, session: Session, project_id: int, reason: str) -> None:
        """Mark a project's work pending, inside the caller's transaction.

        Bumps ``dirty_seq`` by one and coalesces: any number of requests between
        two reconciliations leave the project pending exactly once. The producer
        must call this in the same transaction that commits the change it is
        handing off, so a rolled-back producer bumps nothing.
        """

        if not reason:
            raise ValueError(f"a {self.label} request must state a reason")
        now = datetime.now(timezone.utc)
        existing = session.get(self.model, project_id)
        statement = insert(self.model).values(
            project_id=project_id,
            dirty_seq=1,
            reconciled_seq=0,
            last_reason=reason,
            requested_at=now,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[self.model.project_id],
                set_={
                    "dirty_seq": self.model.dirty_seq + 1,
                    "last_reason": reason,
                    "requested_at": now,
                },
            )
        )
        # The PostgreSQL upsert changes an already-loaded watermark outside the
        # ORM identity map. Expire it so a writer that immediately asks whether
        # work is pending sees the durable sequence it just advanced.
        if existing is not None:
            session.expire(existing)

    def pending(self, session: Session, project_id: int) -> bool:
        """Whether the project has unreconciled work."""

        row = session.get(self.model, project_id)
        return row is not None and row.dirty_seq > row.reconciled_seq

    def pending_project_ids(self, session: Session) -> list[int]:
        """Every project with unreconciled work, for a recovery drain."""

        return list(
            session.scalars(
                select(self.model.project_id)
                .where(self.model.dirty_seq > self.model.reconciled_seq)
                .order_by(self.model.project_id)
            ).all()
        )

    def reconcile(
        self,
        session: Session,
        project_id: int,
        pass_body: Callable[[Session, int], T],
        *,
        run_when_clean: bool = False,
        now: datetime | None = None,
    ) -> WatermarkPass[T]:
        """Run ``pass_body`` under the watermark lock and advance to the snapshot.

        The row is locked so a concurrent producer's bump serializes behind this
        pass rather than being lost. A clean watermark is a no-op that runs
        nothing unless ``run_when_clean`` is set, for a consumer that decided
        from committed state that it has work regardless (the revision pass
        runs when the released policy already reports eligible support). The
        watermark advances only when it was pending, to the exact ``dirty_seq``
        observed under the lock, so a bump that arrives during the pass keeps
        the project pending. ``now`` stamps ``reconciled_at``; it defaults to the
        wall clock for consumers without a controlled clock.
        """

        row = session.scalar(
            select(self.model)
            .where(self.model.project_id == project_id)
            .with_for_update()
        )
        was_pending = row is not None and row.dirty_seq > row.reconciled_seq
        if not was_pending and not run_when_clean:
            return WatermarkPass(
                project_id=project_id,
                was_pending=False,
                ran=False,
                reconciled_seq=row.reconciled_seq if row is not None else 0,
            )

        snapshot = row.dirty_seq if row is not None else 0
        outcome = pass_body(session, project_id)
        if was_pending:
            row.reconciled_seq = snapshot
            row.reconciled_at = now if now is not None else datetime.now(timezone.utc)
        return WatermarkPass(
            project_id=project_id,
            was_pending=was_pending,
            ran=True,
            reconciled_seq=row.reconciled_seq if row is not None else 0,
            outcome=outcome,
        )

