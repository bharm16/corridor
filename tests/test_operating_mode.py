"""The baseline/delta operating mode and the legacy writes it refuses (#520).

ADR-0076 replaced automatic Record Inclusion with capture, adopted baseline,
proposed delta, and resolved delta. Until a project has crossed into the new
mode the legacy admission paths keep writing accepted values, and the readiness
review's hazard is that both run at once. These tests hold the boundary: the
mode is derived from an immutable receipt, it moves one way, and PostgreSQL —
not a Python branch — refuses a legacy accepted-value write for a project that
has adopted its baseline.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from corridor.db import Session, engine
from corridor.dependency_admission import run_dependency_admission
from corridor.extraction_runs import (
    declare_single_run_documents_by_policy,
    record_extraction_run,
)
from corridor.models import (
    BaselineAdoption,
    Candidate,
    Dependency,
    DocPage,
    Document,
    ExternalOrg,
    Project,
    ProjectRecordRevision,
    SourceSegment,
)
from corridor.operating_mode import (
    ADOPTED_BASELINE,
    LEGACY,
    BaselineAdoptionRefused,
    adopt_project_baseline,
    baseline_adoption,
    is_adopted_baseline,
    project_operating_mode,
)
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
    record_delta_deferral,
)
from corridor.source_append import SegmentValues, append_source_segments


BASELINE_DIGEST = hashlib.sha256(b"ucm-baseline.xlsx").hexdigest()
OTHER_DIGEST = hashlib.sha256(b"another-workbook.xlsx").hexdigest()


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def project(session) -> Project:
    row = Project(
        slug=f"operating-mode-{uuid4().hex[:8]}",
        name="Operating Mode Test",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    return row


def adopt(session, project, **overrides) -> BaselineAdoption:
    """The fixture #509 will replace: an adopted-mode project, no importer.

    Every acceptance test below needs a project already in adopted-baseline
    mode, and the Adopt Baseline importer and its preview do not exist yet
    (#509). The transition is a command in its own right precisely so this is
    possible, and so #509 can later call it inside the same transaction as the
    adoption revision it writes.
    """

    values = {
        "project_id": project.id,
        "adopted_by_principal": "local:baseline-adopter",
        "baseline_source_sha256": BASELINE_DIGEST,
        "importer_identity": "ucm-workbook-importer",
        "importer_version": "v0",
        "idempotency_key": f"adopt-{project.id}",
    }
    values.update(overrides)
    return adopt_project_baseline(session, **values)


def _revision(session, project_id: int, key: str) -> ProjectRecordRevision:
    """One Project Record revision, written as the record-decision role."""

    session.execute(text("set local role corridor_fact_decision_writer"))
    revision_id = session.scalar(
        text(
            "insert into project_record_revisions ("
            "project_id, command_type, human_principal, idempotency_key"
            ") values (:project_id, 'adopt_baseline', 'local:adopter', :key)"
            " returning id"
        ),
        {"project_id": project_id, "key": key},
    )
    session.execute(text("reset role"))
    return session.get_one(ProjectRecordRevision, int(revision_id))


# --- The mode is derived from an immutable receipt ------------------------


def test_a_project_with_no_receipt_is_in_legacy_mode(session, project):
    assert project_operating_mode(session, project.id) == LEGACY
    assert is_adopted_baseline(session, project.id) is False
    assert baseline_adoption(session, project.id) is None


def test_the_mode_comes_from_the_adoption_receipt_not_a_project_column(
    session, project
):
    receipt = adopt(session, project)

    assert project_operating_mode(session, project.id) == ADOPTED_BASELINE
    assert is_adopted_baseline(session, project.id) is True
    assert baseline_adoption(session, project.id).id == receipt.id
    assert receipt.adopted_by_principal == "local:baseline-adopter"
    assert receipt.baseline_source_sha256 == BASELINE_DIGEST
    assert receipt.importer_identity == "ucm-workbook-importer"
    # Nothing on `projects` carries the mode, so nothing on `projects` can set it.
    assert "mode" not in Project.__table__.c
    assert "operating_mode" not in Project.__table__.c


def test_replaying_the_same_adoption_returns_the_receipt_already_written(
    session, project
):
    first = adopt(session, project)
    again = adopt(session, project)

    assert again.id == first.id
    assert session.scalar(
        select(func.count()).select_from(BaselineAdoption).where(
            BaselineAdoption.project_id == project.id
        )
    ) == 1


def test_the_adoption_binds_the_revision_it_was_written_with(session, project):
    revision = _revision(session, project.id, f"adopt-rev-{project.id}")

    receipt = adopt(session, project, revision_id=revision.id)

    assert receipt.revision_id == revision.id


def test_an_adoption_cannot_borrow_another_projects_revision(session, project):
    other = Project(
        slug=f"operating-mode-other-{uuid4().hex[:8]}",
        name="Other Project",
        is_synthetic=True,
    )
    session.add(other)
    session.flush()
    foreign = _revision(session, other.id, f"adopt-rev-{other.id}")

    with pytest.raises(DBAPIError, match="belongs to another project"):
        with session.begin_nested():
            adopt(session, project, revision_id=foreign.id)


def test_adopt_baseline_names_the_person_adopting(session, project):
    with pytest.raises(BaselineAdoptionRefused):
        adopt(session, project, adopted_by_principal="  ")


# --- The transition is one way -------------------------------------------


def test_a_second_different_baseline_is_refused(session, project):
    adopt(session, project)

    with pytest.raises(DBAPIError, match="already has an adopted baseline"):
        with session.begin_nested():
            adopt(
                session,
                project,
                baseline_source_sha256=OTHER_DIGEST,
                idempotency_key=f"adopt-again-{project.id}",
            )

    assert project_operating_mode(session, project.id) == ADOPTED_BASELINE


@pytest.mark.parametrize(
    "statement",
    (
        "update project_baseline_adoptions set importer_version = 'v9' "
        "where project_id = :project_id",
        "delete from project_baseline_adoptions where project_id = :project_id",
    ),
)
def test_the_receipt_cannot_be_changed_or_removed(session, project, statement):
    adopt(session, project)

    with pytest.raises(DBAPIError, match="immutable"):
        with session.begin_nested():
            session.execute(text(statement), {"project_id": project.id})

    assert project_operating_mode(session, project.id) == ADOPTED_BASELINE


def test_the_receipt_cannot_be_written_around_the_command(session, project):
    with pytest.raises(DBAPIError, match="typed adoption command"):
        with session.begin_nested():
            session.execute(
                text(
                    "insert into project_baseline_adoptions ("
                    "project_id, adopted_by_principal, baseline_source_sha256, "
                    "importer_identity, importer_version, idempotency_key"
                    ") values (:project_id, 'local:forged', :digest, 'raw', "
                    "'v0', 'forged')"
                ),
                {"project_id": project.id, "digest": BASELINE_DIGEST},
            )

    assert project_operating_mode(session, project.id) == LEGACY


# --- The database refuses the legacy accepted-value writes ----------------


def _document(session, project, *, filename) -> Document:
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(filename.encode()).hexdigest(),
        filename=filename,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text="rows"))
    session.flush()
    return document


def _matrix_candidate(document, utility_id: str) -> Candidate:
    fields = {
        "utility_id": utility_id,
        "external_org": "Tejas Pipeline Co",
        "utility_type": "Petroleum and Gaseous Materials",
        "baseline": "SR-BL",
        "station_from": "1102+20",
        "station_to": "1102+20",
    }
    quote = " | ".join(str(value) for value in fields.values())
    return Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields,
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": quote,
            "text_source": "text_layer",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="matrix_v1",
        model="gpt-test",
        citations_verified=True,
    )


def _declared_matrix(session, project) -> None:
    document = _document(session, project, filename="ucm.pdf")
    candidates = [_matrix_candidate(document, "PL1")]
    session.add(ExternalOrg(name="Tejas Pipeline Co", aliases=[]))
    for candidate in candidates:
        session.add(candidate)
    session.flush()
    record_extraction_run(
        session,
        document,
        prompt_version="matrix_v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="gpt-test",
        schema_version="matrix_candidate_shape_v1",
        allow_unsealed_legacy=True,
    )
    session.flush()
    declare_single_run_documents_by_policy(session, project.id)


def test_legacy_dependency_admission_cannot_admit_into_an_adopted_project(
    session, project
):
    """The real legacy path, not a hand-written insert standing in for it."""

    _declared_matrix(session, project)
    adopt(session, project)

    with pytest.raises(DBAPIError, match="has an adopted baseline"):
        with session.begin_nested():
            run_dependency_admission(session, project.id)

    assert session.scalar(
        select(func.count()).select_from(Dependency).where(
            Dependency.project_id == project.id
        )
    ) == 0


def test_a_legacy_project_still_admits_its_conflicts(session, project):
    """The refusal is the mode's, not a new gate in front of every project."""

    _declared_matrix(session, project)

    result = run_dependency_admission(session, project.id)

    assert result.admitted_count == 1
    assert session.scalar(
        select(func.count()).select_from(Dependency).where(
            Dependency.project_id == project.id
        )
    ) == 1


def test_the_structured_cell_inclusion_command_checks_the_mode_itself(
    session, project
):
    """The check is the command's first act, ahead of its own policy check.

    Calling it with an unrecognized policy proves the ordering: a legacy
    project reaches the policy refusal, an adopted one never gets that far.
    """

    with pytest.raises(DBAPIError, match="unrecognized structured-cell inclusion"):
        with session.begin_nested():
            session.execute(
                select(
                    func.include_structured_cell_fact_decision(
                        project.id, 1, "subject", "size", "key", "not-a-policy"
                    )
                )
            )

    adopt(session, project)

    with pytest.raises(DBAPIError, match="has an adopted baseline"):
        with session.begin_nested():
            session.execute(
                select(
                    func.include_structured_cell_fact_decision(
                        project.id, 1, "subject", "size", "key", "not-a-policy"
                    )
                )
            )


def test_event_admission_cannot_attach_a_statement_to_an_adopted_project(
    session, project
):
    """``run_event_admission`` writes one statement row; the guard is on it.

    The refusal is proved at the table rather than through a full minutes
    corpus, because the guard fires before any other constraint on the row and
    is what the admission path would hit.
    """

    adopt(session, project)

    with pytest.raises(DBAPIError, match="has an adopted baseline"):
        with session.begin_nested():
            session.execute(
                text(
                    "insert into dependency_events (project_id, event_type) "
                    "values (:project_id, 'commitment')"
                ),
                {"project_id": project.id},
            )


def test_a_schedule_update_cannot_move_an_accepted_required_by(session, project):
    """A Constraint admitted while the project was legacy is frozen after adoption.

    ``flow_through_revisions`` advances ``need_date`` in place; that update is
    an accepted-value write, and the guard covers it without the schedule path
    needing a branch of its own.
    """

    _declared_matrix(session, project)
    run_dependency_admission(session, project.id)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    adopt(session, project)

    with pytest.raises(DBAPIError, match="has an adopted baseline"):
        with session.begin_nested():
            session.execute(
                text(
                    "update dependencies set need_date = date '2027-01-04' "
                    "where id = :id"
                ),
                {"id": dependency.id},
            )


# --- What an adopted project still does ----------------------------------


def test_an_adopted_project_still_captures_source_facts_and_proposes_deltas(
    session, project
):
    adopt(session, project)
    document = _document(session, project, filename="ucm-revision-2.xlsx")
    exact = "2027-01-04"

    segments = append_source_segments(
        session,
        project_id=project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=[
            SegmentValues(
                kind="spreadsheet_cell",
                exact_text=exact,
                content_sha256=hashlib.sha256(exact.encode()).hexdigest(),
                ordinal=1,
                sheet_name="UCM",
                cell_range="H12",
            )
        ],
    )
    deltas = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="workbook",
        source_revision="rev-2027-01",
        document_id=document.id,
        deltas=(
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget(
                    subject_identity="PL1", field="need_date"
                ),
                accepted_value={"date": "2026-10-01"},
                proposed_value={"date": exact},
            ),
        ),
    )

    assert len(segments) == 1
    assert session.get(SourceSegment, segments[0].id).exact_text == exact
    assert len(deltas) == 1
    assert deltas[0].proposed_value == {"date": exact}
    assert project_operating_mode(session, project.id) == ADOPTED_BASELINE


def test_work_list_scheduling_stays_outside_operating_mode_authority(
    session, project
):
    """ADR-0084: a deferral schedules work and writes no accepted value."""

    adopt(session, project)
    document = _document(session, project, filename="ucm-revision-3.xlsx")
    delta = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="workbook",
        source_revision="rev-2027-02",
        document_id=document.id,
        deltas=(
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget(
                    subject_identity="PL1", field="utility_owner"
                ),
                accepted_value={"name": "Tejas Pipeline Co"},
                proposed_value={"name": "Tejas Midstream"},
            ),
        ),
    )[0]

    deferral = record_delta_deferral(
        session,
        project_id=project.id,
        delta_id=delta.id,
        deferred_at=datetime(2027, 2, 1, tzinfo=timezone.utc),
        scheduled_by_principal="local:coordinator",
        wake_condition="newer_source_version",
    )

    assert deferral.id is not None
    assert deferral.wake_condition == "newer_source_version"
