"""Proposed Delta identity, grouping, and the appends that create one (#518).

This module writes.  It does not answer what a delta's standing is, and it
does not query the open ones: ``delta_resolution.live_delta_status`` derives
one delta's standing (clocklessly, and reading the packet reversals that a
compensated deferral leaves behind), and ``review_packet_reading``
(``open_deltas``, ``standing_sets``, ``read_open_deltas``) reads the whole
project's.  A second copy of those derivations lived here and answered
differently — it read a wall clock and knew nothing about packet reversals —
so it is gone.

ADR-0075, ADR-0076, ADR-0081, ADR-0082, and ADR-0083 define the target architecture:
    SourceEnvelope -> Source Segment -> Source Fact -> Proposed Delta
    -> Resolve Delta -> Project Record Revision.

This module implements the Proposed Delta layer:
1. Discriminated Target:
   - ``existing_subject``: accepted subject identity + affected field.
   - ``proposed_subject``: source-bound proposed identity + initial proposed fields.
   A genuinely new conflict is normally one delta group carrying its initial fields,
   not many unrelated field deltas.
2. Immutable Occurrence + Derived Live State:
   - ``ProposedDelta`` is an immutable occurrence.
   - ``DeltaGroup`` binds one atomic source change for source lineage and lifecycle.
   - ``DeltaDisposition`` records semantic resolution (accept, edit, reject);
     it is written only by ``delta_resolution`` through the record-decision
     role's command (#519), never from here.
   - ``DeltaSupersession`` links an old occurrence to a newer one from the same lineage.
   - ``DeltaDeferral`` records attributable Work List scheduling with a wake condition;
     it leaves the delta open and writes no Project Record revision (ADR-0035).
   - Live state is a derived query view (open, resolved, superseded, deferred), never
     mutable columns on the occurrence — and that view is derived in
     ``delta_resolution`` and ``review_packet_reading``, not here.
3. Coalescing follows source lineage:
   - Newer revisions of the same source family may supersede or coalesce prior deltas.
   - Independent sources (e.g. email, minutes) mentioning the same field do not
     silently overwrite another source's delta.
4. Apparent removal requires completeness:
   - An ``apparent_removal`` is only produced when the incoming source is a complete
     enumerative revision for the relevant population and its row accounting is sealed.
5. Impact is a Derivation:
   - Computed affected constraints or Key Dates are impact Derivations (rule + inputs
     + evaluation time), not "impact facts".
6. Analytics events:
   - Emits versioned events from ``corridor.analytics``.

What was tried and removed: a ``build_delta_content_hash`` helper here that
re-derived the delta identity in Python.  Nothing called it, and it could not
agree with the command that does derive it — the command hashes PostgreSQL's
``jsonb`` rendering of the values and this hashed ``json.dumps`` — so it was a
second, silently divergent definition of an identity the database now owns
outright (``uq_proposed_deltas_content``, #457).  A delta's identity is
computed in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from corridor.analytics import (
    AnalyticsBinding,
    emit_event,
    proposed_delta_creation_event,
)
from corridor.models import (
    DeltaDeferral,
    ProposedDelta,
    Document,
    SourceDelivery,
)
from corridor.measurement_collection import binding_for_source
from corridor.source_append import append_proposed_deltas


class ApparentRemovalRefused(RuntimeError):
    """Raised when an apparent removal is proposed without a complete sealed source."""


@dataclass(frozen=True)
class ExistingSubjectTarget:
    """Target for a modification or removal of an accepted subject."""

    subject_identity: str
    field: str
    target_type: str = "existing_subject"


@dataclass(frozen=True)
class ProposedSubjectTarget:
    """Target for a genuinely new subject proposed by an incoming source."""

    subject_identity: str
    proposed_fields: tuple[str, ...]
    target_type: str = "proposed_subject"


@dataclass(frozen=True)
class ProposedDeltaValues:
    """The stated attributes of one proposed delta."""

    change_type: str  # add, modify, apparent_removal
    target: ExistingSubjectTarget | ProposedSubjectTarget
    accepted_value: Any | None = None
    proposed_value: Any | None = None
    comparison_rule_version: str = "v1"
    accepted_baseline_revision: str | None = None


@dataclass(frozen=True)
class ImpactDerivation:
    """An impact consequence derived from a proposed delta (ADR-0003, ADR-0082)."""

    rule: str
    inputs: dict[str, Any]
    evaluated_at: datetime
    affected_constraint_ids: tuple[str, ...] = field(default_factory=tuple)
    affected_key_dates: tuple[str, ...] = field(default_factory=tuple)
    rule_version: str = "v1"


def assert_removal_permitted(
    *,
    is_complete_enumerative_source: bool,
    row_accounting_sealed: bool,
) -> None:
    """Apparent removal is only produced when the incoming source is complete and sealed."""

    if not is_complete_enumerative_source or not row_accounting_sealed:
        raise ApparentRemovalRefused(
            "apparent_removal is only lawful when the incoming source is a complete "
            "enumerative revision and row accounting is sealed"
        )


def create_proposed_delta_group(
    session: Session,
    *,
    project_id: int,
    source_family: str,
    source_revision: str,
    document_id: int | None = None,
    statement_id: int | None = None,
    deltas: Sequence[ProposedDeltaValues],
    is_complete_enumerative_source: bool = False,
    row_accounting_sealed: bool = False,
    analytics_binding: AnalyticsBinding | None = None,
) -> tuple[ProposedDelta, ...]:
    """Create one atomic DeltaGroup carrying its proposed deltas (#518).

    Validates:
    - Removals require complete sealed source.
    - Emits versioned analytics events.
    - Invokes append_proposed_deltas security definer command.
    """

    if not deltas:
        return ()

    # Validate apparent removals
    for d in deltas:
        if d.change_type == "apparent_removal":
            assert_removal_permitted(
                is_complete_enumerative_source=is_complete_enumerative_source,
                row_accounting_sealed=row_accounting_sealed,
            )

    payload = []
    for d in deltas:
        if isinstance(d.target, ExistingSubjectTarget):
            t_type = "existing_subject"
            t_id = d.target.subject_identity
            t_field = d.target.field
        else:
            t_type = "proposed_subject"
            t_id = d.target.subject_identity
            t_field = None

        payload.append(
            {
                "change_type": d.change_type,
                "target_type": t_type,
                "target_subject_identity": t_id,
                "target_field": t_field,
                "accepted_value": d.accepted_value,
                "proposed_value": d.proposed_value,
                "comparison_rule_version": d.comparison_rule_version,
                "accepted_baseline_revision": d.accepted_baseline_revision,
            }
        )

    before_id = int(session.scalar(select(func.max(ProposedDelta.id)).where(
        ProposedDelta.project_id == project_id)) or 0)
    appended_ids = append_proposed_deltas(
        session,
        project_id=project_id,
        source_family=source_family,
        source_revision=source_revision,
        document_id=document_id,
        statement_id=statement_id,
        deltas=payload,
    )

    rows = session.scalars(
        select(ProposedDelta)
        .where(ProposedDelta.id.in_(appended_ids))
        .order_by(ProposedDelta.id)
    ).all()

    created_ids = _new_delta_ids(session, project_id, appended_ids, before_id)
    document = session.get(Document, document_id) if document_id is not None else None
    delivery = session.get(SourceDelivery, document.source_delivery_id) if document and document.source_delivery_id else None
    binding = binding_for_source(session, delivery, analytics_binding)
    for row in rows:
        created = row.id in created_ids
        emit_event(
            proposed_delta_creation_event(
                binding,
                occurred_at=row.created_at if created else datetime.now(timezone.utc),
                project_id=project_id,
                source_family=source_family,
                source_revision=source_revision,
                document_id=document_id,
                delta_id=row.id,
                outcome="created" if created else "replayed",
                complete_enumerative_source=is_complete_enumerative_source,
                row_accounting_sealed=row_accounting_sealed,
            )
        )

    return tuple(rows)


def _new_delta_ids(session: Session, project_id: int, ids: Sequence[int], before_id: int) -> frozenset[int]:
    """A returned ID may be a concurrent/idempotent replay, not our insert.

    Only our own uncommitted tuples are visible to this session. Require that
    native inserting transaction to remain in progress AND an ID beyond the
    pre-call watermark. The latter excludes an earlier call in this same
    transaction; the former excludes a concurrent writer that won the insert.
    Qualify recent tuple xids in the nearest epoch to our top-level xid,
    including subtransactions across xid wrap, while the savepoint is active.
    """
    if not ids:
        return frozenset()
    query = text("""
        with boundary as (
            select pg_current_xact_id()::text::numeric as current_xid
        ), recent as (
            select d.id, b.current_xid,
                   b.current_xid - mod(b.current_xid, 4294967296) + d.xmin::text::numeric as qualified_xid
            from proposed_deltas d cross join boundary b
            where d.project_id = :project_id and d.id in :ids and d.id > :before_id
        )
        select id from recent
        where pg_xact_status((qualified_xid + case
            when qualified_xid - current_xid > 2147483648 then -4294967296
            when current_xid - qualified_xid > 2147483648 then 4294967296
            else 0 end
          )::text::xid8) = 'in progress'
    """).bindparams(bindparam("ids", expanding=True))
    return frozenset(session.scalars(query, {"project_id": project_id, "ids": tuple(ids), "before_id": before_id}))


def record_delta_deferral(
    session: Session,
    *,
    project_id: int,
    delta_id: int,
    deferred_at: datetime,
    scheduled_by_principal: str,
    request_identity: str,
    deferred_until: datetime | None = None,
    wake_condition: str | None = None,
    reason: str | None = None,
    supersedes_deferral_id: int | None = None,
) -> DeltaDeferral:
    """Schedule delta on Work List with wake condition (delta remains open).

    The receipt is written by ``defer_proposed_delta``, the record-decision
    role's command (#519): the runtime capabilities hold no write on
    ``delta_deferrals`` and a guard trigger refuses one that does not arrive
    through the command.

    ``request_identity`` is what the caller says this request is, and it is
    required because there is no honest default for it (#903): with the delta
    it decides whether a second call is a replay of this one or an act of its
    own.  A replay asking for anything different is refused rather than
    answered with this receipt.  ``supersedes_deferral_id`` is the receipt the
    caller believed was in force; naming one that has since been replaced is
    refused, and naming none claims nothing, which is what a first Defer of an
    unscheduled change is.
    """

    deferral_id = session.execute(
        select(
            func.defer_proposed_delta(
                project_id,
                delta_id,
                scheduled_by_principal,
                deferred_at,
                deferred_until,
                wake_condition,
                reason,
                request_identity,
                supersedes_deferral_id,
            )
        )
    ).scalar_one()
    session.expire_all()
    return session.get_one(DeltaDeferral, int(deferral_id))
