"""Persist and read the consequences of a Proposed Delta (#643).

The Key Date adapter previously kept these only in its capture/audit payload.
This relation preserves each rule/input version independently of that payload.
Evaluation time is evidence, not identity: retrying identical inputs returns
the original row. Accepted authority remains entirely with Resolve Delta.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.delta_generation import revision_label
from corridor.models import ProjectRecordRevision, ProposedDeltaImpactDerivation
from corridor.proposed_deltas import ImpactDerivation
from corridor.source_append import append_delta_impact


@dataclass(frozen=True)
class ImpactReading:
    id: int
    delta_id: int
    rule: str
    rule_version: str
    accepted_revision_id: int | None
    inputs: dict[str, Any]
    input_sha256: str
    evaluated_at: datetime
    affected_constraint_ids: tuple[str, ...]
    affected_key_dates: tuple[str, ...]
    derivation_sha256: str
    stale: bool


def append_impact_derivation(session: Session, *, project_id: int, delta_id: int,
                             derivation: ImpactDerivation) -> int:
    """Append once per delta, rule version, accepted revision and exact inputs."""
    if derivation.evaluated_at.tzinfo is None:
        raise ValueError("impact evaluation needs an explicit timezone")
    return append_delta_impact(session, project_id=project_id, delta_id=delta_id,
        rule=derivation.rule, rule_version=derivation.rule_version,
        inputs=derivation.inputs, evaluated_at=derivation.evaluated_at,
        constraints=sorted(set(derivation.affected_constraint_ids)),
        key_dates=sorted(set(derivation.affected_key_dates)))


def read_impact_derivations(session: Session, *, project_id: int,
                            delta_ids: Sequence[int]) -> tuple[ImpactReading, ...]:
    """Shared Review/packet reading; old revisions remain visible as stale."""
    if not delta_ids:
        return ()
    current = session.scalar(select(func.max(ProjectRecordRevision.id)).where(
        ProjectRecordRevision.project_id == project_id))
    rows = session.scalars(select(ProposedDeltaImpactDerivation).where(
        ProposedDeltaImpactDerivation.project_id == project_id,
        ProposedDeltaImpactDerivation.delta_id.in_(delta_ids)
    ).order_by(ProposedDeltaImpactDerivation.id)).all()
    return tuple(ImpactReading(row.id, row.delta_id, row.rule, row.rule_version,
        row.accepted_revision_id, row.inputs, row.input_sha256, row.evaluated_at,
        tuple(row.affected_constraint_ids), tuple(row.affected_key_dates), row.derivation_sha256,
        revision_label(row.accepted_revision_id) != revision_label(current)) for row in rows)
