"""One adopted-baseline project, built the way a customer's really is.

The reader tests for the Constraint Log, the Coordination Report and the alert
engine each need a project in ``adopted_baseline`` mode with accepted values,
their Source Segments and the Support Assessments Adopt Baseline records. Each of
them used to reach into ``test_native_accepted_readers``'s own fixture or build a
fourth variant, so "an adopted project" meant something slightly different in
each file. It is built here once, through the real ``adopt_baseline`` command, so
a reader test cannot pass against a shape the importer never produces.

Nothing here reads a clock or invents a value: every date is in the workbook the
caller hands over, and every Support Assessment change goes through
``record_support_assessment`` with an explicit predecessor.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from openpyxl import Workbook
from sqlalchemy import select

from corridor.baseline_adoption import adopt_baseline, preview_baseline_adoption
from corridor.config import settings
from corridor.field_mapping_manifest import MappingDeclaration
from corridor.models import Fact, Project, SupportAssessment, SupportAssessmentSource
from corridor.principals import HumanPrincipal
from corridor.source_intake import validate_and_stage
from corridor.support_assessments import FactProposition, record_support_assessment

PRINCIPAL = HumanPrincipal("local:coordinator")

# ``accepted_field_reading`` reads a Resolution Strategy through the project's own
# declared source vocabulary, keyed by slug, so an adopted project whose slug names
# no vocabulary has no strategy on any record and nothing is ever critical. The
# tests need the criticality reading to be exercisable, so the default slug is one
# of the declared vocabularies and the workbook uses its exact wording.
VOCABULARY_SLUG = "fdot-sr789"

HEADER = [
    "Utility Conflict ID",
    "Utility Owner",
    "Utility Type",
    "Start Station",
    "End Station",
    "Promised For",
    "Required By",
    "Recommended Action or Resolution",
    "Action Due Date",
    "Comment",
]

# Two conflicts for one utility owner. The first is late against its own Promised
# For date and carries a Required By date inside the thirty-day horizon, so the
# three date checks ADR-0090 keeps have something to fire on; the second is
# neither, so a test can tell a fired check from a check that fires on everything.
ROWS = [
    ["UC-1", "City Water", "Water", "100+00", "101+00", "2026-08-01",
     "2026-09-20", "To be relocated", "2026-08-01", "first facility"],
    ["UC-2", "City Water", "Electric", "200+00", "201+00", "2027-04-01",
     "2027-06-01", "", "2026-08-02", "second facility"],
]


def adopt_ucm_workbook(session, tmp_path, monkeypatch, *, rows=None, slug=None):
    """Adopt one UCM workbook and return ``(project, adoption result)``."""
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    project = Project(
        slug=slug or VOCABULARY_SLUG,
        name="Adopted Reader Test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    path = tmp_path / f"ucm-{uuid4().hex[:8]}.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) — Utility Conflicts"])
    sheet.append(HEADER)
    for row in rows or ROWS:
        sheet.append(row)
    book.save(path)
    preview = preview_baseline_adoption(
        session,
        project=project,
        staged=validate_and_stage(path.read_bytes(), path.name),
        customer="Synthetic Customer",
        source_identity="UCM synthetic baseline",
        field_mapping=MappingDeclaration(),
    )
    result = adopt_baseline(
        session,
        preview=preview,
        principal=PRINCIPAL,
        idempotency_key=f"adopt-{path.stem}",
        images_dir=tmp_path / "images",
    )
    return project, result


def facts_of(session, project, *, subject_key=None):
    """Every captured Fact for the project, optionally one source row's."""
    query = select(Fact).where(Fact.project_id == project.id).order_by(Fact.id)
    if subject_key is not None:
        query = query.where(Fact.subject_key == subject_key)
    return tuple(session.scalars(query))


def withdraw_support(session, project, fact, *, outcome="contradicted", at=None):
    """Replace a Fact's effective Support Assessment with a non-supporting one.

    The Source Segments stay exactly where they were and their Source Passage
    Check still passes; only the assessment of whether they support the value
    changes. That is the difference ADR-0082 drew and ADR-0090 re-based
    MISSING_EVIDENCE onto, so a test can prove the alert follows the assessment
    and not the locator.
    """
    effective = session.scalar(
        select(SupportAssessment).where(
            SupportAssessment.fact_id == fact.id,
            SupportAssessment.evidence_role == "value_support",
            SupportAssessment.superseded_by.is_(None),
        )
    )
    assert effective is not None, "Adopt Baseline records one per accepted value"
    segment_ids = tuple(
        session.scalars(
            select(SupportAssessmentSource.source_segment_id)
            .where(SupportAssessmentSource.support_assessment_id == effective.id)
            .order_by(SupportAssessmentSource.ordinal)
        )
    )
    return record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(fact_id=fact.id),
        source_segment_ids=segment_ids,
        evidence_role="value_support",
        assessment=outcome,
        authority=PRINCIPAL,
        assessed_at=at or effective.assessed_at + timedelta(seconds=1),
        supersedes_id=effective.id,
    )
