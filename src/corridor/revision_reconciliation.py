"""Reconcile registered revisions and committed eligibility changes automatically.

Document Revision Processing already exists as an exact primitive: given two
explicit Extraction Run ids, :func:`corridor.revision_processing.process_revision_pair`
converges on one immutable Revision Comparison, integrity-reads it, and routes
the released Automatic Support Update Rules. What did not exist was the step
before it — deciding *which* exact predecessor/successor run pair a project
actually needs processed, without an operator transcribing run ids or
remembering another command. The revision-process CLI made that a human chore;
this module makes it a bounded, restart-safe pass driven from committed state.

Discovery is deliberately narrow (ADR-0043, ADR-0044). A pair exists only where
a registered Supersession edge names a predecessor and successor, *and* each of
those documents has an explicitly declared Current Production Run
(``active_extraction_runs``). The exact run identities come from those
declarations — never the latest attempt, never a relationship guessed from
filename, date, or upload order. Held (quarantined) documents are excluded, and
a document with no declared Active Run yields no pair rather than an inferred
one. Many Work Items over one document pair coalesce to one processing identity,
because the pair is keyed by its two exact run ids.

Two things could make a project's revision pass grow the receipt log without
bound, so both are gated:

1. Creating a Revision Comparison and running Carry-Forward each leave durable
   receipts. Re-deriving them on every idle scheduled tick would append a
   PolicyRun forever. So the pass runs only when the durable watermark
   (:mod:`corridor.revision_reconciliation_request`, bumped in-transaction by a
   registered Supersession edge or a changed Active Run) is pending, or when the
   released policy already reports work waiting — a support set that a committed
   predecessor Record Inclusion, Human Support Update, or accepted fact change
   made newly eligible. An idle project with a clean watermark and no eligible
   support does nothing and appends nothing.
2. A pair whose exact inputs cannot honestly produce a comparison (a failed or
   unsupported successor extraction, unprovable legacy inputs, or already
   ambiguous comparison history) is a per-pair failure that is recorded and does
   not sink its siblings. Its downstream consequence is preserved by the released
   policy's own stable Abstention for the affected Dependency; the watermark then
   advances, so the doomed comparison is not retried until its inputs change.

The whole pass runs in one transaction so a crash leaves neither a Revision
Comparison without its Carry-Forward nor an advanced watermark without its work.
Recovery after restart is therefore re-derivation from committed state, not a
process-local queue: the shared Due Work runtime re-claims the occurrence, and
unchanged files with an already-sealed comparison receipt reconcile to a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.automatic_carry_forward import (
    AutomaticCarryForwardResult,
    automatic_carry_forward_status,
    run_automatic_carry_forward,
)
from corridor.extraction_runs import active_run_for_document
from corridor.models import (
    Document,
    DocumentQuarantine,
    Project,
    RevisionReconciliationRequest,
)
from corridor.project_lock import lock_project
from corridor.record_inclusion import request_record_inclusion
from corridor.revision_comparison import (
    DEFAULT_MATCHER_VERSION,
    RevisionComparisonError,
)
from corridor.revision_processing import obtain_verified_revision_pair
from corridor.revision_reconciliation_request import revision_reconciliation_pending


class RevisionReconciliationRefused(ValueError):
    """The pass was asked to reconcile an unknown project or unapproved matcher."""


@dataclass(frozen=True)
class RevisionPair:
    """One exact predecessor-successor run pair discovered from committed state."""

    predecessor_document_id: int
    successor_document_id: int
    predecessor_extraction_run_id: int
    successor_extraction_run_id: int

    @property
    def identity(self) -> tuple[int, int]:
        """The processing identity many Work Items over this pair coalesce to."""

        return (
            self.predecessor_extraction_run_id,
            self.successor_extraction_run_id,
        )


@dataclass(frozen=True)
class RevisionComparisonFailure:
    """One discovered pair whose exact inputs did not produce a comparison."""

    predecessor_extraction_run_id: int
    successor_extraction_run_id: int
    error_code: str


@dataclass(frozen=True)
class RevisionReconciliationResult:
    """The honest per-pass outcome of one bounded revision reconciliation."""

    project_id: int
    did_reconcile: bool
    pairs_discovered: int
    comparisons_created: int
    comparisons_reused: int
    comparison_failures: tuple[RevisionComparisonFailure, ...]
    carry_forward: AutomaticCarryForwardResult | None
    requested_record_inclusion: bool
    reconciled_seq: int

    @property
    def carried_count(self) -> int:
        return len(self.carry_forward.carried) if self.carry_forward is not None else 0

    @property
    def abstained_count(self) -> int:
        return (
            len(self.carry_forward.abstentions)
            if self.carry_forward is not None
            else 0
        )


def discover_revision_pairs(
    session: Session, project_id: int
) -> tuple[RevisionPair, ...]:
    """Select the exact revision run pairs a project needs, from committed state.

    A pair is a registered Supersession edge whose predecessor and successor both
    carry an explicitly declared Current Production Run. The run identities are
    read from those declarations, never inferred; a missing declaration or a held
    document yields no pair. The result is coalesced by exact run identity, so a
    document pair referenced by many Work Items is one processing identity.
    """

    predecessors = session.scalars(
        select(Document)
        .where(
            Document.project_id == project_id,
            Document.superseded_by.is_not(None),
        )
        .order_by(Document.id)
    ).all()
    if not predecessors:
        return ()

    quarantined = set(
        session.scalars(
            select(DocumentQuarantine.document_id)
            .join(Document, Document.id == DocumentQuarantine.document_id)
            .where(Document.project_id == project_id)
        ).all()
    )

    pairs: list[RevisionPair] = []
    seen: set[tuple[int, int]] = set()
    for predecessor in predecessors:
        successor_id = predecessor.superseded_by
        # A held input on either side is deliberately unread, so it is not a
        # pair the machine may reconcile.
        if predecessor.id in quarantined or successor_id in quarantined:
            continue
        predecessor_run = active_run_for_document(session, predecessor.id)
        successor_run = active_run_for_document(session, successor_id)
        # No declared Current Production Run means no exact run identity. That is
        # a pair not yet ready, never a run guessed from recency.
        if predecessor_run is None or successor_run is None:
            continue
        identity = (predecessor_run.id, successor_run.id)
        if identity in seen:
            continue
        seen.add(identity)
        pairs.append(
            RevisionPair(
                predecessor_document_id=predecessor.id,
                successor_document_id=successor_id,
                predecessor_extraction_run_id=predecessor_run.id,
                successor_extraction_run_id=successor_run.id,
            )
        )
    return tuple(pairs)


def reconcile_project_revisions(
    session_factory,
    *,
    project_id: int,
    matcher_version: str = DEFAULT_MATCHER_VERSION,
    clock,
) -> RevisionReconciliationResult:
    """Run one bounded, restart-safe revision reconciliation for a project.

    This is the stable handoff a later approved organization decision can call.
    It owns its transactions through ``session_factory`` and does no model work:
    it obtains each permitted exact comparison, integrity-reads it, then applies
    the released Automatic Support Update Rules once over the project's current
    state. Support is never transferred merely because a comparison row exists —
    the released policy decides every row and keeps its own stable Abstention.
    """

    # A matcher the deployed Automatic Support Update Rules do not honor is
    # unresolved input authority, so it is refused work rather than run with a
    # comparison the policy would only abstain on.
    if matcher_version != DEFAULT_MATCHER_VERSION:
        raise RevisionReconciliationRefused(
            f"matcher {matcher_version!r} is not the approved revision matcher"
        )

    # First, decide from committed state whether there is anything to do, without
    # opening a write transaction or holding the project lock across the read-only
    # eligibility assessment.
    with session_factory() as reading:
        if reading.get(Project, project_id) is None:
            raise RevisionReconciliationRefused(
                f"project {project_id} is not a registered project"
            )
        pending = revision_reconciliation_pending(reading, project_id)
        row = reading.get(RevisionReconciliationRequest, project_id)
        reconciled_seq = row.reconciled_seq if row is not None else 0
        eligible_support = 0
        if not pending:
            eligible_support = automatic_carry_forward_status(
                reading, project_id
            ).eligible_count

    if not pending and eligible_support == 0:
        return RevisionReconciliationResult(
            project_id=project_id,
            did_reconcile=False,
            pairs_discovered=0,
            comparisons_created=0,
            comparisons_reused=0,
            comparison_failures=(),
            carry_forward=None,
            requested_record_inclusion=False,
            reconciled_seq=reconciled_seq,
        )

    # Then do the work in one transaction: obtain each permitted exact comparison,
    # integrity-read it, apply the released rules, hand off the downstream load,
    # and advance the watermark — atomically, so a crash leaves none of it.
    with session_factory() as session:
        with session.begin():
            lock_project(session, project_id)
            row = session.scalar(
                select(RevisionReconciliationRequest)
                .where(RevisionReconciliationRequest.project_id == project_id)
                .with_for_update()
            )
            was_pending = row is not None and row.dirty_seq > row.reconciled_seq
            snapshot = row.dirty_seq if row is not None else 0

            pairs = discover_revision_pairs(session, project_id)
            created = 0
            reused = 0
            failures: list[RevisionComparisonFailure] = []
            for pair in pairs:
                try:
                    verified = obtain_verified_revision_pair(
                        session,
                        predecessor_extraction_run_id=(
                            pair.predecessor_extraction_run_id
                        ),
                        successor_extraction_run_id=(
                            pair.successor_extraction_run_id
                        ),
                        matcher_version=matcher_version,
                    )
                except RevisionComparisonError as exc:
                    failures.append(
                        RevisionComparisonFailure(
                            predecessor_extraction_run_id=(
                                pair.predecessor_extraction_run_id
                            ),
                            successor_extraction_run_id=(
                                pair.successor_extraction_run_id
                            ),
                            error_code=type(exc).__name__,
                        )
                    )
                    continue
                if verified.created:
                    created += 1
                else:
                    reused += 1

            carry = run_automatic_carry_forward(session, project_id)
            requested_record_inclusion = bool(carry.carried)
            if requested_record_inclusion:
                # The moved support is a committed change to the Project Record;
                # its downstream reconciliation continues through the same durable
                # handoff #342 owns, inside this transaction so a rollback leaves
                # no load request.
                request_record_inclusion(
                    session, project_id, "revision_reconciliation"
                )

            if was_pending:
                row.reconciled_seq = snapshot
                row.reconciled_at = _aware_utc(clock.now())
            reconciled_seq = row.reconciled_seq if row is not None else 0

    return RevisionReconciliationResult(
        project_id=project_id,
        did_reconcile=True,
        pairs_discovered=len(pairs),
        comparisons_created=created,
        comparisons_reused=reused,
        comparison_failures=tuple(failures),
        carry_forward=carry,
        requested_record_inclusion=requested_record_inclusion,
        reconciled_seq=reconciled_seq,
    )


def summarize_revision_reconciliation(
    result: RevisionReconciliationResult,
    *,
    configuration_version: str,
    observed_at: datetime,
) -> dict:
    """A bounded, counts-only receipt of one reconciliation, for the handler.

    The rich per-pair detail lives in ``RevisionReconciliationResult``; a durable
    Due Work receipt keeps only counts and a health verdict so it stays within the
    handler's byte contract regardless of how many revision pairs a project has.
    """

    failures = len(result.comparison_failures)
    health = "healthy" if failures == 0 else "revision_attention_required"
    return {
        "schema_version": "revision-reconciliation-result-v1",
        "project_id": result.project_id,
        "configuration_version": configuration_version,
        "observed_at": _aware_utc(observed_at).isoformat(),
        "health": health,
        "did_reconcile": result.did_reconcile,
        "pairs_discovered": result.pairs_discovered,
        "comparisons_created": result.comparisons_created,
        "comparisons_reused": result.comparisons_reused,
        "comparison_failures": failures,
        "carried": result.carried_count,
        "abstained": result.abstained_count,
        "requested_record_inclusion": result.requested_record_inclusion,
    }


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RevisionReconciliationRefused(
            "revision reconciliation clock must supply an aware datetime"
        )
    return value.astimezone(timezone.utc)
