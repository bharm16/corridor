"""Proposed Delta identity, grouping, lifecycle, and query seams (#518).

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
     mutable columns on the occurrence.
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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.analytics import (
    AnalyticsBinding,
    AnalyticsEvent,
    EventFamily,
)
from corridor.models import (
    DeltaDeferral,
    DeltaDisposition,
    DeltaGroup,
    DeltaSupersession,
    ProposedDelta,
)
from corridor.source_append import append_proposed_deltas


class ApparentRemovalRefused(RuntimeError):
    """Raised when an apparent removal is proposed without a complete sealed source."""


class StaleAcceptedRevisionRefused(RuntimeError):
    """Raised when a delta is proposed against a stale accepted baseline."""


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


@dataclass(frozen=True)
class LiveDeltaState:
    """The derived live status of a proposed delta."""

    delta_id: int
    status: str  # open, resolved, superseded, deferred
    disposition: str | None = None
    deferred_until: datetime | None = None
    wake_condition: str | None = None
    superseded_by_delta_id: int | None = None


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

    # Emit versioned analytics event if binding provided
    if analytics_binding is not None:
        AnalyticsEvent(
            family=EventFamily.PROPOSED_DELTA_CREATION,
            binding=analytics_binding,
            payload={
                "project_id": project_id,
                "source_family": source_family,
                "source_revision": source_revision,
                "delta_count": len(rows),
                "delta_ids": [r.id for r in rows],
            },
        )

    return tuple(rows)


def derive_live_delta_state(session: Session, delta_id: int) -> LiveDeltaState:
    """Walk dispositions, supersessions, and deferrals to derive live state."""

    # Check disposition
    disp = session.scalar(
        select(DeltaDisposition).where(DeltaDisposition.delta_id == delta_id)
    )
    if disp is not None:
        return LiveDeltaState(
            delta_id=delta_id,
            status="resolved",
            disposition=disp.disposition,
        )

    # Check supersession
    sup = session.scalar(
        select(DeltaSupersession).where(DeltaSupersession.prior_delta_id == delta_id)
    )
    if sup is not None:
        return LiveDeltaState(
            delta_id=delta_id,
            status="superseded",
            superseded_by_delta_id=sup.superseding_delta_id,
        )

    # Check deferral
    deferral = session.scalars(
        select(DeltaDeferral)
        .where(DeltaDeferral.delta_id == delta_id)
        .order_by(DeltaDeferral.id.desc())
    ).first()
    if deferral is not None:
        now = datetime.now(timezone.utc)
        if deferral.deferred_until is None or deferral.deferred_until > now:
            return LiveDeltaState(
                delta_id=delta_id,
                status="deferred",
                deferred_until=deferral.deferred_until,
                wake_condition=deferral.wake_condition,
            )

    return LiveDeltaState(delta_id=delta_id, status="open")


def query_live_deltas(
    session: Session,
    *,
    project_id: int,
    target_subject_identity: str | None = None,
) -> tuple[ProposedDelta, ...]:
    """Query currently open deltas (not resolved or superseded)."""

    stmt = select(ProposedDelta).where(
        ProposedDelta.project_id == project_id,
        ~ProposedDelta.id.in_(select(DeltaDisposition.delta_id)),
        ~ProposedDelta.id.in_(select(DeltaSupersession.prior_delta_id)),
    )
    if target_subject_identity is not None:
        stmt = stmt.where(ProposedDelta.target_subject_identity == target_subject_identity)

    deltas = session.scalars(stmt.order_by(ProposedDelta.id)).all()
    return tuple(deltas)


def record_delta_supersession(
    session: Session,
    *,
    project_id: int,
    prior_delta_id: int,
    superseding_delta_id: int,
    reason: str = "newer_source_revision",
) -> DeltaSupersession:
    """Link an old delta to a newer superseding delta in the same source lineage."""

    row = DeltaSupersession(
        project_id=project_id,
        prior_delta_id=prior_delta_id,
        superseding_delta_id=superseding_delta_id,
        reason=reason,
    )
    session.add(row)
    session.flush()
    return row


def record_delta_deferral(
    session: Session,
    *,
    project_id: int,
    delta_id: int,
    deferred_at: datetime,
    scheduled_by_principal: str,
    deferred_until: datetime | None = None,
    wake_condition: str | None = None,
    reason: str | None = None,
) -> DeltaDeferral:
    """Schedule delta on Work List with wake condition (delta remains open).

    The receipt is written by ``defer_proposed_delta``, the record-decision
    role's command (#519): the runtime capabilities hold no write on
    ``delta_deferrals`` and a guard trigger refuses one that does not arrive
    through the command.
    """

    deferral_id = session.scalar(
        select(
            func.defer_proposed_delta(
                project_id,
                delta_id,
                scheduled_by_principal,
                deferred_at,
                deferred_until,
                wake_condition,
                reason,
            )
        )
    )
    session.expire_all()
    return session.get_one(DeltaDeferral, int(deferral_id))
