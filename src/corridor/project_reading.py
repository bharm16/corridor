"""Freeze one provenance-consistent reading for project publication surfaces.

Report, project Briefing, workbook export, and External Report release all need
the same Ledger population, Evaluation, Statement Publication, and Committed
Dates.  Each reader previously assembled and defended that tuple itself.  This
module owns the pairing and the population check; renderers consume its result
without rereading current statement state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.dependency_events import (
    StatementPublication,
    published_dependency_statements,
)
from corridor.exceptions import Evaluation, evaluate_project
from corridor.ledger import LedgerRow, browse
from corridor.models import Dependency, Project


@dataclass(frozen=True)
class FrozenProjectReading:
    """The exact population and provenance inputs one project reader consumes."""

    project: Project
    rows: tuple[LedgerRow, ...]
    evaluation: Evaluation
    statement_publication: StatementPublication

    @property
    def dependency_ids(self) -> tuple[int, ...]:
        return tuple(row.dependency.id for row in self.rows)

    @property
    def committed_dates(self):
        return self.statement_publication.committed_dates


def validate_frozen_reading(
    *,
    project_id: int,
    evaluation: Evaluation,
    statement_publication: StatementPublication,
    dependency_ids: tuple[int, ...] | None = None,
    document_only: bool | None = None,
) -> None:
    """Refuse inputs that do not describe one project reading."""
    if evaluation.project_id != project_id:
        raise ValueError("the evaluation belongs to another project")
    if statement_publication.project_id != project_id:
        raise ValueError("the statement publication belongs to another project")
    if evaluation.committed_dates != statement_publication.committed_dates:
        raise ValueError(
            "the evaluation and statement publication describe different "
            "Committed Date readings"
        )
    if (
        evaluation.statement_publication_fingerprint
        != statement_publication.fingerprint
    ):
        raise ValueError(
            "the evaluation and statement publication describe different "
            "statement provenance"
        )
    if document_only is not None and (
        statement_publication.document_only != document_only
    ):
        raise ValueError(
            "the requested provenance mode differs from the statement publication"
        )
    if dependency_ids is not None and set(dependency_ids) != set(
        statement_publication.by_dependency
    ):
        raise ValueError(
            "the Ledger population changed after the paired evaluation and "
            "statement publication"
        )


def freeze_project_reading(
    session: Session,
    project_id: int,
    *,
    today: date | None = None,
    document_only: bool = False,
    evaluation: Evaluation | None = None,
    statement_publication: StatementPublication | None = None,
) -> FrozenProjectReading:
    """Create or read-verify one exact project-wide publication input."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"no project {project_id}")
    current_ids = tuple(
        session.scalars(
            select(Dependency.id)
            .where(
                Dependency.project_id == project_id,
                Dependency.dismissed_at.is_(None),
            )
            .order_by(Dependency.ref_code)
        ).all()
    )
    if (evaluation is None) != (statement_publication is None):
        raise ValueError(
            "an existing frozen reading requires both Evaluation and Statement Publication"
        )
    if statement_publication is None:
        statement_publication = published_dependency_statements(
            session,
            current_ids,
            project_id=project_id,
            document_only=document_only,
        )
        evaluation = evaluate_project(
            session,
            project_id,
            today=today,
            statement_publication=statement_publication,
        )
    assert evaluation is not None
    assert statement_publication is not None
    validate_frozen_reading(
        project_id=project_id,
        evaluation=evaluation,
        statement_publication=statement_publication,
        dependency_ids=current_ids,
        document_only=document_only,
    )
    rows = tuple(
        browse(session, project_id, limit=100_000, evaluation=evaluation)
    )
    if {row.dependency.id for row in rows} != set(current_ids):
        raise ValueError(
            "the Ledger population changed after the paired evaluation and "
            "statement publication"
        )
    return FrozenProjectReading(
        project=project,
        rows=rows,
        evaluation=evaluation,
        statement_publication=statement_publication,
    )
