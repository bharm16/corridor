"""Render diagnostics for the legacy Project Record reader transition.

The previous station-only overlay lived beside the plain current view and
reported identical outputs as successful cutover. Rendering belongs here,
above the projection and renderers; complete semantic coverage belongs to
reader_coverage. Output equality alone never authorizes a reader switch.
"""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass, replace
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
import re

from pypdf import PdfReader
from sqlalchemy.orm import Session

from corridor.briefing import brief_project, render as render_briefing
from corridor.export import to_xlsx
from corridor.project_reading import FrozenProjectReading, freeze_project_reading
from corridor.record_projection import read_current_project_record

from corridor.report import build_report, render as render_report
from corridor.report_release import render_external_report_pdf
from corridor.native_reader_coverage import workbook_cells
from corridor.reader_coverage import CONTRACTS, CoverageResult, SemanticRecord, SurfaceReading, compare_all_surfaces
from corridor.accepted_field_reading import NativeReadingRefused, accepted_field_text


def compare_native_reader_surfaces(session: Session, project_id: int, *,
                                  legacy: tuple[SurfaceReading, ...],
                                  as_of_revision_id: int,
                                  evaluated_at: datetime) -> tuple[CoverageResult, ...]:
    """Compare a retained reference with freshly observed native reader outputs.

    Native evidence is collected here, never supplied by the caller. Collection
    blockers survive even if the caller's semantic values happen to match. The
    reference's independent custody and the live cutover decision remain separate
    requirements; this function cannot authenticate caller-supplied history.
    """
    from corridor.native_reader_coverage import collect_native_reader_coverage

    observed = collect_native_reader_coverage(session, project_id,
        as_of_revision_id=as_of_revision_id, evaluated_at=evaluated_at)
    collected = {surface.reading.surface: surface for surface in observed.surfaces}
    return tuple(replace(result,
        passed=result.passed and not collected[result.surface].blockers,
        blockers=(*result.blockers, *collected[result.surface].blockers))
        for result in compare_all_surfaces(legacy, observed.readings))


@dataclass(frozen=True)
class ReaderEquivalence:
    coordination_report_identical: bool
    briefing_identical: bool
    workbook_cells_identical: bool
    release_pdf_text_identical: bool
    explanations: tuple[str, ...]
    coverage: tuple[CoverageResult, ...] = ()

    @property
    def passed(self) -> bool:
        # Each surface contract owns its semantic and unchanged-output proof.
        # The broad historical render comparisons below remain diagnostics.
        return (len(self.coverage) == len(CONTRACTS)
                and {row.surface for row in self.coverage} == {item.name for item in CONTRACTS}
                and all(row.passed for row in self.coverage))

    @property
    def rendered_outputs_identical(self) -> bool:
        return all(
            (
                self.coordination_report_identical,
                self.briefing_identical,
                self.workbook_cells_identical,
                self.release_pdf_text_identical,
            )
        )


def freeze_project_reading_from_current_view(
    session: Session,
    project_id: int,
    *,
    today: date | None = None,
    legacy_reading: FrozenProjectReading | None = None,
) -> FrozenProjectReading:
    legacy = legacy_reading or freeze_project_reading(session, project_id, today=today)
    if legacy.native_population is not None:
        # Production adopted UCM readers already consume the native frozen
        # population. Never copy a legacy row or replace selected fields here.
        return legacy
    values = {
        (value.dependency_id, value.fact_type): value.text_value
        for value in read_current_project_record(session, project_id)
        if value.dependency_id is not None
    }
    rows = []
    for row in legacy.rows:
        dependency = copy(row.dependency)
        for fact_type in ("station_from", "station_to"):
            value = values.get((dependency.id, fact_type))
            if value is not None:
                setattr(dependency, fact_type, value)
        rows.append(replace(row, dependency=dependency))
    return FrozenProjectReading(
        project=legacy.project,
        rows=tuple(rows),
        evaluation=legacy.evaluation,
        statement_publication=legacy.statement_publication,
    )


def prove_reader_equivalence(
    session: Session,
    project_id: int,
    *,
    today: date,
    output_dir: Path,
    briefing_client_factory,
) -> ReaderEquivalence:
    """Diagnose old render equality; never certify the partial overlay as cutover.

    These consumers still receive legacy population, evaluation and statement
    publication. Coverage records that actual read path independently from
    their output equality. The seven-surface semantic engine lives in
    ``reader_coverage`` and must be fed independently native readings.
    """

    legacy = freeze_project_reading(session, project_id, today=today)
    viewed = freeze_project_reading_from_current_view(
        session, project_id, today=today, legacy_reading=legacy
    )
    legacy_report = build_report(
        session, project_id, today=today, frozen_reading=legacy
    )
    viewed_report = build_report(
        session, project_id, today=today, frozen_reading=viewed
    )
    viewed_report = replace(viewed_report, generated_at=legacy_report.generated_at)
    report_identical = render_report(legacy_report) == render_report(viewed_report)

    legacy_briefing = brief_project(
        session,
        project_id,
        client=briefing_client_factory(),
        today=today,
        frozen_reading=legacy,
    )
    viewed_briefing = brief_project(
        session,
        project_id,
        client=briefing_client_factory(),
        today=today,
        frozen_reading=viewed,
    )
    briefing_identical = render_briefing(legacy_briefing) == render_briefing(
        viewed_briefing
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    legacy_xlsx = to_xlsx(
        session,
        project_id,
        output_dir / "legacy.xlsx",
        evaluation=legacy.evaluation,
        statement_publication=legacy.statement_publication,
        frozen_reading=legacy,
    )
    viewed_xlsx = to_xlsx(
        session,
        project_id,
        output_dir / "view.xlsx",
        evaluation=viewed.evaluation,
        statement_publication=viewed.statement_publication,
        frozen_reading=viewed,
    )
    workbook_identical = workbook_cells(legacy_xlsx.read_bytes()) == workbook_cells(viewed_xlsx.read_bytes())

    legacy_release = render_external_report_pdf(
        session, project_id, today=today, frozen_reading=legacy
    )
    viewed_release = render_external_report_pdf(
        session, project_id, today=today, frozen_reading=viewed
    )
    release_identical = _pdf_text(legacy_release.pdf_bytes) == _pdf_text(
        viewed_release.pdf_bytes
    )
    return ReaderEquivalence(
        coordination_report_identical=report_identical,
        briefing_identical=briefing_identical,
        workbook_cells_identical=workbook_identical,
        release_pdf_text_identical=release_identical,
        coverage=tuple(CoverageResult(contract.name, False, 0,
            (("Native UCM population is read directly; no independent legacy seven-surface comparison was supplied."
              if viewed.native_population is not None else
              "Legacy overlay supplies population, evaluation and statement publication; only station_from/station_to are projected."),))
            for contract in CONTRACTS),
        explanations=(
            "XLSX package timestamps are excluded; every workbook cell is compared.",
            "PDF container metadata is excluded; normalized rendered page text is compared.",
        ),
    )


def native_constraint_log_surface(reading: FrozenProjectReading) -> SurfaceReading:
    """Describe the actual native Constraint Log and check population for comparison.

    This is one surface's observed input, not a seven-surface cutover receipt.
    A reviewer must compare it with independently retained legacy semantics.
    """
    population = reading.native_population
    if population is None:
        raise NativeReadingRefused("legacy overlays cannot supply native surface provenance")
    reading_of = {row.reading.id: row.reading for row in reading.rows}
    rows = []
    for record in population.open_records:
        fields = {
            "identity": {"record_subject_key": record.subject_key, "source_row_key": record.source_row_key},
            "accepted_values": {name: accepted_field_text(field) for name, field in sorted(record.fields.items())},
            "source_support": tuple(source.reference for source in record.source_passages),
            # Declared, not invented: the accepted record establishes no
            # Coordination Decision, so the surface carries the reading's own
            # markers rather than three Nones that read as empty fields.
            "coordination": {
                "internal_owner": str(reading_of[record.id].internal_owner),
                "next_action": str(reading_of[record.id].next_action),
                "action_due_date": str(reading_of[record.id].action_due_date),
            },
            "check_results": tuple((item.rule, item.detail, item.quantity_days) for item in reading.evaluation.for_dependency(record.id)),
        }
        origins = {name: f"revision:{population.revision_id}" for name in fields}
        origins["accepted_values"] += "/decisions:" + ",".join(str(field.decision_id) for field in record.fields.values())
        origins["source_support"] += "/source_segments:" + ",".join(str(source.source_segment_id) for source in record.source_passages)
        rows.append(SemanticRecord("constraint", record.subject_key, fields, origins))
        for finding in reading.evaluation.for_dependency(record.id):
            rows.append(SemanticRecord("check", f"{record.subject_key}/{finding.rule}",
                {**fields, "check_results": ((finding.rule, finding.detail, finding.quantity_days),)}, origins))
    return SurfaceReading("constraint_log", tuple(rows), frozenset({"constraint", "check"}))


def _pdf_text(pdf_bytes: bytes) -> tuple[str, ...]:
    return tuple(
        re.sub(r"Generated .*? UTC", "Generated <normalized> UTC", page.extract_text())
        for page in PdfReader(BytesIO(pdf_bytes)).pages
    )
