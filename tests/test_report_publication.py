"""Scheduled weekly report retention and external preparation through Due Work.

These exercise the public seams only: the gate-7 declaration, the shared
supervised runtime claiming a weekly occurrence, and the retained reading it
produces.  Rendering is a deterministic adapter — no model spend, no real
send — and the committed-transaction paths use the harness-owned
``runtime_database`` fixture.  Together they cover the stable predecessor
(the last authorized release package), converging repeated and restarted
occurrences, a missed week, configuration refusal, cross-project access, a
rendering failure, reviewed-byte preservation, and human-only release.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from harness_support import as_record_decision_role
from access_support import seed_membership
from corridor.config import settings
from corridor.db import Session, engine
from corridor.due_work import (
    DueWorkRefusal,
    HANDLER_REPORT_PUBLICATION,
    HandlerContract,
    ReportPublicationDeclaration,
    configure_report_publication,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.models import (
    Dependency,
    DueWorkOccurrence,
    DueWorkSchedule,
    ExternalOrg,
    ExternalReportArtifact,
    ExternalReportRelease,
    Project,
    ProjectRecordRevision,
    ReleasePackage,
    ReportRun,
    ScheduledReportPublication,
)
from corridor.object_storage import LocalFilesystemStore
from corridor.principals import HumanPrincipal
from corridor.release_authorization import authorize_release_package
from corridor.release_candidate import latest_authorized_package
from corridor.report_publication import (
    ComparisonWindow,
    comparison_window,
    execute_report_publication,
    project_publication_history,
)
from corridor.report_release import (
    release_external_report,
    retrieve_prepared_external_report,
)
from corridor.web.app import app, get_human_principal, get_session
from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes
# #533's own candidate fixtures, for the same reason #536's tests import them:
# a predecessor assertion is only honest if the package it names was built by
# the act that authorizes one.
from test_release_authorization import (
    BINDING,
    COORDINATOR as PACKAGE_COORDINATOR,
    RELEASED_AT,
    RELEASER as PACKAGE_RELEASER,
    Adopted,
    configure as configure_issued_set,
    prepare as prepare_candidate,
)


RELEASER = HumanPrincipal("local:publication-releaser")


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _fake_pdf(html: str) -> bytes:
    """A deterministic stand-in renderer: distinct content yields distinct bytes."""

    return b"%PDF-1.7\n" + sha256(html.encode()).hexdigest().encode() + b"\n%%EOF"


def _raising_pdf(html: str) -> bytes:
    raise RuntimeError("renderer unavailable")


def _registry(render_pdf=_fake_pdf):
    """A registry whose publication handler renders through the injected adapter."""

    def run_effectful(context):
        return execute_report_publication(
            context.session_factory,
            occurrence_id=context.claim.occurrence_id,
            schedule_id=context.claim.schedule_id,
            clock=context.clock,
            render_pdf=render_pdf,
        )

    return {
        HANDLER_REPORT_PUBLICATION: HandlerContract(
            key=HANDLER_REPORT_PUBLICATION,
            scope_kind="one_project_scheduled_report_publication",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            run_effectful=run_effectful,
        )
    }


def _seed_project(session, *, slug: str) -> int:
    project = Project(slug=slug, name="Publication", is_synthetic=True)
    party = ExternalOrg(name=f"{slug} Utility")
    session.add_all((project, party))
    session.flush()
    session.add(
        Dependency(
            project_id=project.id,
            external_org_id=party.id,
            ref_code="DEP-PUB-1",
            dep_type="utility_relocation",
            title="Publication telecom relocation",
        )
    )
    session.flush()
    return project.id


def _seed_release(session, project_id: int, *, evaluated_on: date, provenance_mode: str):
    pdf = b"%PDF-1.7\nprior release\n%%EOF"
    artifact = ExternalReportArtifact(
        project_id=project_id,
        artifact_name=f"prior-{evaluated_on.isoformat()}.pdf",
        format="pdf",
        pdf_bytes=pdf,
        pdf_sha256=sha256(pdf).hexdigest(),
        evaluated_on=evaluated_on,
        ruleset_version="v0.4",
        evaluation_context_json={},
        provenance_mode=provenance_mode,
        record_context_json={"dependencies": [], "party_statements": []},
    )
    session.add(artifact)
    session.flush()
    release = ExternalReportRelease(
        project_id=project_id,
        artifact_id=artifact.id,
        artifact_name=artifact.artifact_name,
        format="pdf",
        pdf_sha256=artifact.pdf_sha256,
        evaluated_on=evaluated_on,
        ruleset_version="v0.4",
        provenance_mode=provenance_mode,
        released_by="local:prior-releaser",
        released_by_display="Prior Releaser",
    )
    session.add(release)
    session.flush()
    return release


def _configure(session, project_id: int, now: datetime, **overrides):
    declaration = ReportPublicationDeclaration.released_weekly(
        project_id=project_id,
        configuration_version="report-publication-v1",
        starts_at=now.replace(minute=0, second=0, microsecond=0),
    )
    if overrides:
        declaration = replace(declaration, **overrides)
    return configure_report_publication(session, declaration, now=now)


def _scheduled_project(factory, now: datetime, *, released_on=None, **overrides):
    with factory() as setup:
        project_id = _seed_project(setup, slug=f"pub-{uuid4().hex}")
        if released_on is not None:
            _seed_release(
                setup,
                project_id,
                evaluated_on=released_on,
                provenance_mode=overrides.get(
                    "provenance_mode", "all-supported-sources"
                ),
            )
        schedule = _configure(setup, project_id, now, **overrides)
        ids = (project_id, schedule.id)
        setup.commit()
    return ids


def _run(factory, now, **registry_kwargs):
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    return run_due_work_once(
        factory,
        clock=ControlledClock(now),
        owner="runtime:publication-worker",
        registry=_registry(**registry_kwargs),
    )


@pytest.fixture
def rollback_session():
    """A rollback-scoped session for the predicate seam, which commits nothing."""

    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The content-addressed store a prepared candidate's bytes are retained in."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return LocalFilesystemStore(tmp_path / "artifacts")


def _authorized_package(session, tmp_path, store):
    """One adopted project holding exactly one authorized release package."""

    project = Project(
        slug=f"pkg-{uuid4().hex[:10]}", name="Package", is_synthetic=True
    )
    session.add(project)
    session.flush()
    seed_membership(session, project, PACKAGE_COORDINATOR)
    seed_membership(session, project, PACKAGE_RELEASER)
    body = workbook_bytes(tmp_path / f"{project.slug}.xlsx", BASELINE_ROWS)
    revision_id, _ = adopt(session, project, body, tmp_path)
    adopted = Adopted(project, revision_id, body)
    configure_issued_set(session, adopted)
    _, _, candidate = prepare_candidate(session, adopted, store)
    authorization = authorize_release_package(
        session,
        project_id=project.id,
        candidate_id=candidate.id,
        releaser=PACKAGE_RELEASER,
        authorized_at=RELEASED_AT,
        store=store,
        binding=BINDING,
    )
    return project, session.get(ReleasePackage, authorization.package_id)


# ------------------------------------------------------- the comparison baseline


def test_the_weekly_predecessor_is_the_last_authorized_package(
    rollback_session, tmp_path, store
):
    """One baseline predicate: the package chain, never the release clock.

    The window opens at the predecessor package's source cutoff — the point past
    which that issue excluded later-arriving sources — so the weekly report and
    the change summary in the same package cannot disagree about when the week
    closed.  A legacy ``ExternalReportRelease`` recorded *after* the package is
    seeded here deliberately: it is the newest external thing by wall clock, and
    under ADR-0086 it is not the marker.
    """

    session = rollback_session
    project, package = _authorized_package(session, tmp_path, store)
    _seed_release(
        session,
        project.id,
        evaluated_on=date(2026, 9, 30),
        provenance_mode="all-supported-sources",
    )
    session.flush()

    window = comparison_window(session, project.id, as_of=date(2026, 3, 9))

    assert window.predecessor_package_id == package.id
    assert window.window_start == package.source_cutoff.date()
    assert window.days == 7
    # Not the released PDF's own evaluation date, which is the value the retired
    # predicate would have produced here.
    assert window.window_start != date(2026, 9, 30)


def test_a_project_with_no_authorized_package_has_no_comparison_predecessor(
    rollback_session
):
    """Before a first package the reading stands on current accepted state."""

    session = rollback_session
    project = Project(
        slug=f"nopkg-{uuid4().hex[:10]}", name="No package", is_synthetic=True
    )
    session.add(project)
    session.flush()
    _seed_release(
        session,
        project.id,
        evaluated_on=date(2026, 8, 24),
        provenance_mode="all-supported-sources",
    )
    session.flush()

    window = comparison_window(session, project.id, as_of=date(2026, 8, 31))

    assert window == ComparisonWindow(None, None, None)


# ---------------------------------------------------------------- runtime seams


def test_scheduled_occurrence_retains_a_reading_prepares_a_pdf_and_creates_no_run(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, schedule_id = _scheduled_project(factory, now)

    result = _run(factory, now)
    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_key == HANDLER_REPORT_PUBLICATION
    assert result.handler_result["outcome"] == "retained"
    assert result.handler_result["prepared"] is True
    assert result.handler_result["has_prior_release"] is False
    assert result.safe_next_step == "review_prepared_report"

    with factory() as verify:
        publications = verify.scalars(
            select(ScheduledReportPublication).where(
                ScheduledReportPublication.project_id == project_id
            )
        ).all()
        assert len(publications) == 1
        publication = publications[0]
        assert publication.prepared_artifact_id is not None
        assert publication.predecessor_release_id is None
        assert publication.window_start is None
        artifact = verify.get(
            ExternalReportArtifact, publication.prepared_artifact_id
        )
        assert artifact.pdf_bytes.startswith(b"%PDF-")
        # A retained reading and a prepared PDF never become a ReportRun, so the
        # internal report's comparison baseline is not advanced (ADR-0053).
        assert (
            verify.scalar(
                select(func.count()).select_from(ReportRun).where(
                    ReportRun.project_id == project_id
                )
            )
            == 0
        )
        status = due_work_status(verify, project_id=project_id)
        assert status["occurrences"][0]["state"] == "completed"
        assert status["receipts"][0]["handler"] == HANDLER_REPORT_PUBLICATION


def test_a_released_legacy_pdf_is_never_the_comparison_predecessor(
    runtime_database,
):
    """A sealed legacy PDF, however recent, does not open a comparison window.

    ``changes.last_released_report`` used to select exactly this row by
    ``released_at desc``, so this project would have reported "changes in the
    last 7 days" measured from a receipt that binds no accepted Project Record
    revision (#635).  ADR-0086 moved the marker to the last approved package;
    this project has none, so the honest answer is current state only and no
    invented prior issue occurrence.
    """

    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(
        factory, now, released_on=date(2026, 8, 24)
    )

    _run(factory, now)

    with factory() as verify:
        publication = verify.scalars(
            select(ScheduledReportPublication).where(
                ScheduledReportPublication.project_id == project_id
            )
        ).one()
        # The release exists, is the newest thing by wall clock, and gets no vote.
        assert verify.scalar(
            select(func.count()).select_from(ExternalReportRelease).where(
                ExternalReportRelease.project_id == project_id
            )
        ) == 1
        assert latest_authorized_package(verify, project_id) is None
        assert publication.predecessor_release_id is None
        assert publication.window_start is None
        assert publication.comparison_window_days is None
        assert publication.evaluated_on == date(2026, 8, 31)


def test_repeated_triggers_and_restarted_occurrences_converge_on_one_reading(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, schedule_id = _scheduled_project(factory, now)

    # Every trigger within one week resolves to one occurrence.
    with factory() as ticking:
        first = enqueue_due_work(ticking, now=now)
        ticking.commit()
    with factory() as ticking:
        later = enqueue_due_work(
            ticking, now=now.replace(hour=9)
        )
        ticking.commit()
    assert [o.public_id for o in first] == [o.public_id for o in later]
    occurrence_id = first[0].id

    # A restarted or retried worker re-executing the same occurrence converges
    # on its one retained row rather than a second snapshot or artifact.
    first_summary = execute_report_publication(
        factory,
        occurrence_id=occurrence_id,
        schedule_id=schedule_id,
        clock=ControlledClock(now),
        render_pdf=_fake_pdf,
    )
    second_summary = execute_report_publication(
        factory,
        occurrence_id=occurrence_id,
        schedule_id=schedule_id,
        clock=ControlledClock(now.replace(hour=10)),
        render_pdf=_fake_pdf,
    )
    assert first_summary["snapshot_public_id"] == second_summary["snapshot_public_id"]

    with factory() as verify:
        assert (
            verify.scalar(
                select(func.count()).select_from(ScheduledReportPublication).where(
                    ScheduledReportPublication.project_id == project_id
                )
            )
            == 1
        )
        assert (
            verify.scalar(
                select(func.count()).select_from(ExternalReportArtifact).where(
                    ExternalReportArtifact.project_id == project_id
                )
            )
            == 1
        )


def test_missed_week_records_the_actual_observation_and_invents_no_window(
    runtime_database,
):
    factory = runtime_database.session_factory
    start = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, schedule_id = _scheduled_project(
        factory, start, released_on=date(2026, 8, 24)
    )

    # The first weekly slot is enqueued but never runs.
    with factory() as ticking:
        [missed] = enqueue_due_work(ticking, now=start)
        ticking.commit()

    # Two weeks later the runtime enqueues the current slot and fails the older
    # one under the declared latest-only policy.
    later = datetime(2026, 9, 14, 7, 5, tzinfo=timezone.utc)
    _run(factory, later)

    with factory() as verify:
        missed_row = verify.get(DueWorkOccurrence, missed.id)
        assert missed_row.state == "failed"
        assert missed_row.last_error_code == "missed_run_latest_only"
        publication = verify.scalars(
            select(ScheduledReportPublication).where(
                ScheduledReportPublication.project_id == project_id
            )
        ).one()
        # The reading is the actual execution date, never backdated to the
        # historical slot.  No false report occurrence is created for the missed
        # week either, and with no approved package there is no window to widen
        # (ADR-0053's honest-window rule, under ADR-0086's marker).
        assert publication.evaluated_on == date(2026, 9, 14)
        assert publication.window_start is None
        assert publication.comparison_window_days is None


def test_a_later_release_never_advances_a_retained_occurrence_predecessor(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now)

    _run(factory, now)  # retained before any release: no predecessor

    with factory() as changing:
        _seed_release(
            changing,
            project_id,
            evaluated_on=date(2026, 8, 30),
            provenance_mode="all-supported-sources",
        )
        changing.commit()

    with factory() as verify:
        publication = verify.scalars(
            select(ScheduledReportPublication).where(
                ScheduledReportPublication.project_id == project_id
            )
        ).one()
        # A release recorded after the reading does not re-point its predecessor
        # or invent a comparison window (ADR-0053, AC7).
        assert publication.predecessor_release_id is None
        assert publication.window_start is None


def test_internal_snapshot_only_schedule_retains_without_preparing_a_pdf(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now, prepare_external_pdf=False)

    result = _run(factory, now)
    assert result.handler_result["prepared"] is False
    assert result.safe_next_step == "review_retained_snapshot"

    with factory() as verify:
        publication = verify.scalars(
            select(ScheduledReportPublication).where(
                ScheduledReportPublication.project_id == project_id
            )
        ).one()
        assert publication.prepared_artifact_id is None
        assert publication.snapshot_json["dependencies"]
        assert (
            verify.scalar(
                select(func.count()).select_from(ExternalReportArtifact).where(
                    ExternalReportArtifact.project_id == project_id
                )
            )
            == 0
        )


def test_rendering_failure_fails_the_occurrence_without_a_false_success(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now)

    result = _run(factory, now, render_pdf=_raising_pdf)
    assert result is not None
    assert result.execution_outcome in {"retry_due", "failed"}
    assert result.handler_result is None
    assert result.error_code == "handler_execution_failed"
    assert result.safe_next_step in {
        "retry_after_backoff",
        "inspect_processing_failure",
    }

    with factory() as verify:
        # No retained reading, no prepared artifact, no false successful receipt.
        assert (
            verify.scalar(
                select(func.count()).select_from(ScheduledReportPublication).where(
                    ScheduledReportPublication.project_id == project_id
                )
            )
            == 0
        )
        assert (
            verify.scalar(
                select(func.count()).select_from(ExternalReportArtifact).where(
                    ExternalReportArtifact.project_id == project_id
                )
            )
            == 0
        )
        status = due_work_status(verify, project_id=project_id)
        assert status["occurrences"][0]["state"] in {"retry_due", "failed"}


def test_a_scheduled_prepared_pdf_is_released_only_by_a_human_and_keeps_its_bytes(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now)
    _run(factory, now)

    with factory() as acting:
        publication = acting.scalars(
            select(ScheduledReportPublication).where(
                ScheduledReportPublication.project_id == project_id
            )
        ).one()
        artifact_id = publication.prepared_artifact_id
        reviewed = retrieve_prepared_external_report(acting, project_id, artifact_id)
        reviewed_bytes = bytes(reviewed.pdf_bytes)
        reviewed_digest = reviewed.pdf_sha256

        # A later project change must not alter the reviewed bytes on release.
        acting.add(
            Dependency(
                project_id=project_id,
                ref_code="DEP-PUB-LATE",
                dep_type="utility_relocation",
                title="Added after preparation",
            )
        )
        acting.flush()

        release = release_external_report(
            acting,
            project_id=project_id,
            artifact_id=artifact_id,
            principal=RELEASER,
        )
        assert bytes(release.pdf_bytes) == reviewed_bytes
        assert release.pdf_sha256 == reviewed_digest
        assert release.released_by == RELEASER.subject

        history = project_publication_history(acting, project_id)
        [retained] = history.retained
        assert retained.state == "released"
        assert retained.release_id == release.id
        acting.commit()


# --------------------------------------------------------- configuration + HTTP


@pytest.fixture
def project(session):
    project_id = _seed_project(session, slug=f"pub-http-{uuid4().hex[:12]}")
    return session.get(Project, project_id)


def test_invalid_publication_configuration_is_refused_without_a_schedule(
    session, project
):
    now = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)
    # Publication reads no model: a nonzero token budget is refused, and no
    # schedule is written (no silent production default fills the gap).
    with pytest.raises(DueWorkRefusal, match="resource declaration is invalid"):
        _configure(session, project.id, now, model_token_budget=5)
    with pytest.raises(DueWorkRefusal, match="provenance mode is invalid"):
        _configure(session, project.id, now, provenance_mode="everything")
    with pytest.raises(DueWorkRefusal, match="weekly UTC latest-only"):
        _configure(session, project.id, now, cadence="hourly")
    assert (
        session.scalar(
            select(func.count())
            .select_from(DueWorkSchedule)
            .where(DueWorkSchedule.project_id == project.id)
        )
        == 0
    )


def test_report_controls_surface_states_and_enforce_project_access(session, project):
    now = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)
    schedule = _configure(session, project.id, now)
    [occurrence] = enqueue_due_work(session, now=now.replace(minute=5))

    pdf = b"%PDF-1.7\nprepared\n%%EOF"
    artifact = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="scheduled-prepared.pdf",
        format="pdf",
        pdf_bytes=pdf,
        pdf_sha256=sha256(pdf).hexdigest(),
        evaluated_on=date(2026, 8, 31),
        ruleset_version="v0.4",
        evaluation_context_json={"thresholds": {}},
        provenance_mode="all-supported-sources",
        record_context_json={"dependencies": [], "party_statements": []},
    )
    session.add(artifact)
    session.flush()
    session.add(
        ScheduledReportPublication(
            public_id=f"report-pub:{uuid4().hex}",
            occurrence_id=occurrence.id,
            schedule_id=schedule.id,
            project_id=project.id,
            configuration_version="report-publication-v1",
            provenance_mode="all-supported-sources",
            prepared_artifact_id=artifact.id,
            evaluated_on=date(2026, 8, 31),
            ruleset_version="v0.4",
            thresholds_json={},
            snapshot_json={"dependencies": {}},
            observed_at=now,
        )
    )
    session.flush()

    other = Project(slug=f"other-{uuid4().hex[:12]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    seed_membership(session, project, RELEASER, display_name="Publication Coordinator")

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RELEASER
    try:
        with TestClient(app) as client:
            page = client.get(f"/reports/{project.slug}")
            assert page.status_code == 200
            assert "Scheduled weekly retention" in page.text
            assert "Prepared — awaiting a release decision" in page.text
            assert f"/reports/{project.slug}/prepared/{artifact.id}" in page.text
            # A member of this project cannot reach another project's controls.
            assert client.get(f"/reports/{other.slug}").status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- The accepted revision a retained reading is bound to (#602) ------------


def _seed_accepted_revision(session, project_id: int, key: str) -> int:
    """One accepted revision, written as the role that owns the family.

    A revision is written only by the record-decision role's own commands, and
    reaching one here would mean adopting a whole baseline to prove a property
    of the publication binding rather than of the adoption.
    """

    session.flush()
    with as_record_decision_role(session):
        revision = ProjectRecordRevision(
            project_id=project_id,
            command_type="record_verbal_statement",
            human_principal="local:publication-reviewer",
            idempotency_key=key,
        )
        session.add(revision)
        session.flush()
    return revision.id


def test_a_retained_reading_names_the_accepted_revision_it_stands_on(
    runtime_database,
):
    """The reference #602 adds, read back from the retained row and the receipt.

    ``snapshot_json`` beside it is the immutable Report Reading payload this
    occurrence published (ADR-0092), not a cache of the revision; the revision
    is what the record-owned values are read through, and it is resolved in the
    same writing transaction as the reading so the two can never name states
    taken a moment apart.
    """

    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now)
    with factory() as setup:
        revision_id = _seed_accepted_revision(
            setup, project_id, "publication:bound-reading"
        )
        setup.commit()

    result = _run(factory, now)

    assert result.handler_result["accepted_revision_id"] == revision_id
    with factory() as verify:
        publication = verify.scalars(
            select(ScheduledReportPublication).where(
                ScheduledReportPublication.project_id == project_id
            )
        ).one()
        assert publication.revision_id == revision_id
        # The reading is retained beside the reference, and is its own
        # evidence rather than a copy of it.
        assert publication.snapshot_json["dependencies"] is not None
        history = project_publication_history(verify, project_id)
        assert history.retained[0].revision_id == revision_id


def test_a_project_with_no_accepted_revision_retains_an_explicit_absence(
    runtime_database,
):
    """A project whose accepted record is not on the spine names no revision."""

    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now)

    result = _run(factory, now)

    assert result.handler_result["accepted_revision_id"] is None
    with factory() as verify:
        publication = verify.scalars(
            select(ScheduledReportPublication).where(
                ScheduledReportPublication.project_id == project_id
            )
        ).one()
        assert publication.revision_id is None


def test_the_database_refuses_a_retained_reading_that_names_no_revision(
    runtime_database,
):
    """The rule is enforced on this relation too, not only on ``report_runs``.

    The insert goes straight into the relation, past ``execute_report_publication``
    which resolves the binding itself.  It names a fresh pending occurrence, so
    the append-only and one-row-per-occurrence guards have nothing to say about
    it and the refusal can only be the binding's own.
    """

    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, schedule_id = _scheduled_project(factory, now)
    with factory() as setup:
        revision_id = _seed_accepted_revision(
            setup, project_id, "publication:refusal"
        )
        setup.commit()
    _run(factory, now)

    later = now + timedelta(days=7)
    with factory() as ticking:
        enqueue_due_work(ticking, now=later)
        ticking.commit()

    with factory() as writing:
        occurrence_id = writing.scalar(
            select(DueWorkOccurrence.id)
            .where(
                DueWorkOccurrence.scheduled_job_id == schedule_id,
                DueWorkOccurrence.state == "pending",
            )
            .order_by(DueWorkOccurrence.id.desc())
            .limit(1)
        )
        assert occurrence_id is not None
        statement = text(
            "insert into scheduled_report_publications ("
            "public_id, occurrence_id, schedule_id, project_id, "
            "configuration_version, provenance_mode, revision_id, evaluated_on, "
            "ruleset_version, thresholds_json, snapshot_json, observed_at"
            ") values ("
            ":public_id, :occurrence_id, :schedule_id, :project_id, "
            "'report-publication-v1', 'all-supported-sources', :revision_id, "
            "date '2026-09-07', 'v0.4', '{}'::jsonb, '{}'::jsonb, "
            "timestamptz '2026-09-07 07:05:00+00')"
        )
        parameters = {
            "public_id": f"report-pub:{uuid4().hex}",
            "occurrence_id": occurrence_id,
            "schedule_id": schedule_id,
            "project_id": project_id,
        }

        with pytest.raises(DBAPIError) as refusal:
            writing.execute(statement, {**parameters, "revision_id": None})
        assert "names the accepted Project Record revision" in str(refusal.value)
        writing.rollback()

        # The same statement, bound, is accepted: the refusal above is the
        # binding's and nothing else in the row.
        writing.execute(statement, {**parameters, "revision_id": revision_id})
        writing.commit()

    with factory() as verify:
        assert verify.scalar(
            select(func.count())
            .select_from(ScheduledReportPublication)
            .where(ScheduledReportPublication.occurrence_id == occurrence_id)
        ) == 1


def test_the_database_refuses_a_reading_bound_to_another_projects_revision(
    runtime_database,
):
    """The composite key, proved on its own: a foreign binding is unrepresentable."""

    factory = runtime_database.session_factory
    now = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)
    project_id, schedule_id = _scheduled_project(factory, now)
    with factory() as setup:
        other_id = _seed_project(setup, slug=f"pub-{uuid4().hex}")
        foreign_revision_id = _seed_accepted_revision(
            setup, other_id, "publication:foreign"
        )
        setup.commit()
    _run(factory, now)

    later = now + timedelta(days=7)
    with factory() as ticking:
        enqueue_due_work(ticking, now=later)
        ticking.commit()

    with factory() as writing:
        occurrence_id = writing.scalar(
            select(DueWorkOccurrence.id)
            .where(
                DueWorkOccurrence.scheduled_job_id == schedule_id,
                DueWorkOccurrence.state == "pending",
            )
            .order_by(DueWorkOccurrence.id.desc())
            .limit(1)
        )
        with pytest.raises(IntegrityError) as refusal:
            writing.execute(
                text(
                    "insert into scheduled_report_publications ("
                    "public_id, occurrence_id, schedule_id, project_id, "
                    "configuration_version, provenance_mode, revision_id, "
                    "evaluated_on, ruleset_version, thresholds_json, "
                    "snapshot_json, observed_at"
                    ") values ("
                    ":public_id, :occurrence_id, :schedule_id, :project_id, "
                    "'report-publication-v1', 'all-supported-sources', "
                    ":revision_id, date '2026-09-07', 'v0.4', '{}'::jsonb, "
                    "'{}'::jsonb, timestamptz '2026-09-07 07:05:00+00')"
                ),
                {
                    "public_id": f"report-pub:{uuid4().hex}",
                    "occurrence_id": occurrence_id,
                    "schedule_id": schedule_id,
                    "project_id": project_id,
                    "revision_id": foreign_revision_id,
                },
            )
        assert "fk_scheduled_report_publications_revision" in str(refusal.value)
