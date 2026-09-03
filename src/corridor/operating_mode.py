"""Which write paths a project still allows: legacy, or adopted baseline (#520).

ADR-0076 replaced ADR-0029's mechanical Record Inclusion with four operations,
and ADR-0083 fixed Adopt Baseline as a bulk human command. Neither says what
happens to the legacy admission paths that are still running while the new ones
are built, and the readiness review named the hazard exactly: "you could build
Adopt Baseline and Proposed Delta while the old pipeline continues writing
around them". This module is the boundary that makes that impossible.

The first design was a ``projects.mode`` column. It was rejected because a
column anybody can set is not a record of anything: the transition would be a
freely editable toggle, reversible by an ``UPDATE``, and the accepted record's
authority would rest on nobody having flipped it back. Instead the mode is
*derived* from one immutable baseline-adoption receipt. A project holding a
receipt is in ``adopted_baseline`` mode; a project without one is ``legacy``.
The receipt is written once by the record-decision role's own command, and the
database refuses every update, delete, and truncate of it, so the transition is
one-way by construction.

Enforcement does not live here. The legacy accepted-value writers are Python
(``run_dependency_admission``, ``run_event_admission``, the schedule update
paths in ``schedule_linking``) and SQL (``include_structured_cell_fact_decision``)
alike, so a Python branch could only ever cover half of them. The guard is in
PostgreSQL: triggers refuse an insert or update of ``dependencies`` and
``dependency_events`` for an adopted project, and the structured-cell inclusion
command checks the mode as its first act. This module is the seam that reads
the mode and performs the transition; ``adopt_project_baseline`` is deliberately
callable on its own so #509 can invoke it in the same transaction as the
adoption revision it writes.

What the mode does **not** govern: Work List scheduling, deferral, and packet
presentation. Those are ADR-0035 human work with their own attributable
receipts, and ADR-0084 keeps a deferral outside the accepted record entirely.
An adopted project still captures Source Facts and creates Proposed Deltas; it
simply cannot have an accepted value replaced by a legacy path.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import BaselineAdoption


LEGACY = "legacy"
ADOPTED_BASELINE = "adopted_baseline"


class BaselineAdoptionRefused(ValueError):
    """A caller cannot establish the adopted-baseline operating mode."""


def project_operating_mode(session: Session, project_id: int) -> str:
    """Read one project's operating mode from the database's own derivation.

    The SQL function is the single definition, so a Python reader and the
    triggers that refuse legacy writes can never disagree about what mode a
    project is in.
    """

    return str(
        session.scalar(select(func.project_operating_mode(project_id)))
    )


def is_adopted_baseline(session: Session, project_id: int) -> bool:
    """Whether the project's accepted record came from an adopted baseline."""

    return project_operating_mode(session, project_id) == ADOPTED_BASELINE


def baseline_adoption(session: Session, project_id: int) -> BaselineAdoption | None:
    """The receipt the mode is derived from, or ``None`` for a legacy project."""

    return session.scalar(
        select(BaselineAdoption).where(BaselineAdoption.project_id == project_id)
    )


def adopt_project_baseline(
    session: Session,
    *,
    project_id: int,
    adopted_by_principal: str,
    baseline_source_sha256: str,
    importer_identity: str,
    importer_version: str,
    idempotency_key: str,
    revision_id: int | None = None,
) -> BaselineAdoption:
    """Move one project from legacy to adopted-baseline mode, once.

    A replay of the same adoption returns the receipt already written. A second
    adoption naming a different baseline is refused: the transition happens once
    and never runs backwards. ``revision_id`` is the Project Record revision the
    adoption wrote; it is optional so this transition exists before the importer
    that will pass it (#509).
    """

    if not adopted_by_principal.strip():
        raise BaselineAdoptionRefused("Adopt Baseline names the person adopting")
    if not idempotency_key.strip():
        raise BaselineAdoptionRefused("Adopt Baseline needs an idempotency key")

    outcome = session.scalar(
        select(
            func.adopt_project_baseline(
                project_id,
                adopted_by_principal,
                baseline_source_sha256,
                importer_identity,
                importer_version,
                idempotency_key,
                revision_id,
            )
        )
    )
    return session.get_one(BaselineAdoption, int(outcome["adoption_id"]))
