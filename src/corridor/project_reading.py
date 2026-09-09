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

from corridor.check_configuration import effective_thresholds
from corridor.dependency_events import (
    StatementPublication,
    published_dependency_statements,
)
from corridor.exceptions import Evaluation, evaluate_project, evaluate_native_population
from corridor.accepted_field_reading import AcceptedFieldPopulation, NativeReadingRefused, read_accepted_field_population
from corridor.operating_mode import is_adopted_baseline
from corridor.ledger import LedgerRow, browse
from corridor.models import Dependency, Project


@dataclass(frozen=True)
class FrozenProjectReading:
    """The exact population and provenance inputs one project reader consumes."""

    project: Project
    rows: tuple[LedgerRow, ...]
    evaluation: Evaluation
    statement_publication: StatementPublication
    native_population: AcceptedFieldPopulation | None = None
    coverage_blockers: tuple[str, ...] = ()

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
    revision_id: int | None = None,
) -> FrozenProjectReading:
    """Create or read-verify one exact project-wide publication input."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"no project {project_id}")
    if is_adopted_baseline(session, project_id):
        if (evaluation is None) != (statement_publication is None):
            raise NativeReadingRefused("a frozen native reading requires both evaluation and publication")
        if evaluation is not None:
            population = evaluation.native_population
            if population is None or population.project_id != project_id or (revision_id is not None and population.revision_id != revision_id):
                raise NativeReadingRefused("evaluation does not bind this native project/revision")
            if evaluation.statement_publication is not statement_publication:
                raise NativeReadingRefused("native evaluation and publication are not the same frozen reading")
        else:
            population = read_accepted_field_population(session, project_id, revision_id=revision_id)
            evaluation = evaluate_native_population(population, today=today,
                thresholds=effective_thresholds(session, project_id), document_only=document_only)
            statement_publication = evaluation.statement_publication
        validate_frozen_reading(project_id=project_id, evaluation=evaluation,
            statement_publication=statement_publication, dependency_ids=population.record_ids,
            document_only=document_only)
        return FrozenProjectReading(project, tuple(browse(session, project_id, limit=100_000, evaluation=evaluation)),
                                    evaluation, statement_publication, population)
    if revision_id is not None:
        raise NativeReadingRefused("legacy population has no complete revision-bound native accepted-field mapping")
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
        # Every publication surface reads through here, so the project's own
        # declared check thresholds are resolved once and bound to the shared
        # Evaluation. No declaration returns the supported defaults, unchanged.
        evaluation = evaluate_project(
            session,
            project_id,
            today=today,
            thresholds=effective_thresholds(session, project_id),
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
        coverage_blockers=("Legacy population and accepted-field ownership are not native.",),
    )
