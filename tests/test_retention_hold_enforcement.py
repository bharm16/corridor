"""A retention hold suspends every deletion path it claims to (#956, ADR-0080).

#949 closed the sign-in expiry window with the ``retention-hold-ordering``
boundary and recorded three gaps this suite proves closed:

- **A/C** Class B execution now takes that same boundary and runs in bounded
  batches, so a hold placed after one committed batch blocks the next one
  rather than waiting behind an entire unbounded sweep.
- **B** No runtime capability writes ``retention_holds`` directly, and the
  worker cannot lift a hold by any route -- neither a raw write nor a
  ``lift any hold`` command.
- **D** An object-store deletion whose acknowledgement is lost is retained as
  *uncertain until reconciled*, never assumed deleted or rolled back.

Every proof runs on committed transactions through the harness-owned
``runtime_database`` fixture, and the authority proofs connect as the real
``corridor_worker`` and ``corridor_web`` logins rather than the schema owner,
because a proof that runs as the owner proves nothing: the owner can do
everything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.pool import NullPool

from corridor.config import settings
from corridor.models import (
    CoordinationSummaryConfiguration,
    CoordinationSummaryRequest,
    ProcessingArtifact,
    Project,
    RetentionHold,
    RetentionManifestItem,
    SpendAuthorization,
)
from corridor.principals import HumanPrincipal
from corridor.retention import (
    HOLD_ENFORCEMENT_ACKNOWLEDGEMENT,
    RetentionRefused,
    execute_retention,
    execute_retention_in_batches,
    lift_hold,
    place_hold,
    plan_retention,
    register_processing_artifact,
)
from corridor.object_storage import DeletionUncertain, content_key, content_store


OPERATOR = HumanPrincipal("local:retention-operator")
NOW = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)

ADMIN_URL = settings.database_url
LOGIN_PASSWORDS = {
    "corridor_web": os.environ.get("CORRIDOR_WEB_DB_PASSWORD") or "corridor_web",
    "corridor_worker": os.environ.get("CORRIDOR_WORKER_DB_PASSWORD") or "corridor_worker",
}


def _login_engine(database: str, role: str):
    url = (
        make_url(ADMIN_URL)
        .set(database=database, username=role, password=LOGIN_PASSWORDS[role])
        .render_as_string(hide_password=False)
    )
    return create_engine(url, poolclass=NullPool, future=True)


def _due_request(session, project, *, slug_suffix, completed_at):
    authorization = SpendAuthorization(
        project_id=project.id,
        operation="coordination_summary",
        model="test-model",
        max_input_tokens=100,
        max_output_tokens=100,
        timeout_seconds=10,
        max_requests=1,
        retry_policy="none",
        retention_policy="class_b_30_days",
        observation_context="internal_working_view",
        declared_by=OPERATOR.subject,
    )
    session.add(authorization)
    session.flush([authorization])
    configuration = CoordinationSummaryConfiguration(
        project_id=project.id,
        authorization_id=authorization.id,
        source_scope="all_sources",
        prompt_version="coordination_summary_v1",
    )
    session.add(configuration)
    session.flush([configuration])
    request = CoordinationSummaryRequest(
        public_id=f"receipt-{slug_suffix}",
        project_id=project.id,
        configuration_id=configuration.id,
        requested_by=OPERATOR.subject,
        reading_sha256="a" * 64,
        project_reading_json={"facts": ["copied"]},
        evaluated_on=NOW.date(),
        ruleset_version="v1",
        statement_publication_fingerprint="b" * 64,
        provenance_mode="all-supported-sources",
        status="completed",
        summary_markdown="Working draft",
        completed_at=completed_at,
    )
    session.add(request)
    session.flush([request])
    return request


def _seed_two_due_requests(factory):
    with factory() as setup:
        project = Project(
            slug=f"rh-{uuid4().hex[:8]}", name="Retention Hold", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        first = _due_request(
            setup, project, slug_suffix=uuid4().hex[:8], completed_at=NOW - timedelta(days=40)
        )
        second = _due_request(
            setup, project, slug_suffix=uuid4().hex[:8], completed_at=NOW - timedelta(days=41)
        )
        ids = (project.id, first.id, second.id)
        setup.commit()
    return ids


def _plan(factory, project_id):
    with factory() as planning:
        manifest = plan_retention(
            planning, as_of=NOW, principal=OPERATOR, project_id=project_id
        )
        result = (manifest.id, manifest.content_sha256, manifest.public_id)
        planning.commit()
    return result


def _summary(session, request_id):
    return session.get(CoordinationSummaryRequest, request_id).summary_markdown


# --- A/C: the boundary and the bounded batch ------------------------------


def test_a_committed_batch_stays_and_a_later_hold_blocks_the_next_batch(runtime_database):
    """A hold placed after one committed batch blocks the next batch (A, C).

    The delete is bounded to one item per transaction, so the first item is
    removed and committed before the hold exists. The hold then wins the
    boundary the next batch must take, and that batch refuses -- while the row
    the first batch already removed stays removed. Nothing recovers it.
    """

    factory = runtime_database.session_factory
    project_id, first_id, second_id = _seed_two_due_requests(factory)
    manifest_id, expected_sha256, _public = _plan(factory, project_id)

    with factory() as batch_one:
        execute_retention(
            batch_one,
            manifest_id=manifest_id,
            expected_sha256=expected_sha256,
            executed_at=NOW,
            limit=1,
        )
        batch_one.commit()

    with factory() as verify_one:
        summaries = {
            _summary(verify_one, first_id),
            _summary(verify_one, second_id),
        }
        # Exactly one of the two is now expired; the other still holds content.
        assert summaries == {None, "Working draft"}

    with factory() as holding:
        place_hold(
            holding, project_id=project_id, reason="open-records request", principal=OPERATOR
        )
        holding.commit()

    with factory() as batch_two:
        with pytest.raises(RetentionRefused, match="hold"):
            execute_retention(
                batch_two,
                manifest_id=manifest_id,
                expected_sha256=expected_sha256,
                executed_at=NOW,
                limit=1,
            )
        batch_two.rollback()

    with factory() as verify_two:
        # The batch the hold blocked deleted nothing more, and the one the
        # first batch removed did not come back.
        remaining = [
            request_id
            for request_id in (first_id, second_id)
            if _summary(verify_two, request_id) == "Working draft"
        ]
        assert len(remaining) == 1


def test_class_b_execution_waits_on_the_hold_boundary(runtime_database):
    """Class B execution takes ``retention-hold-ordering`` before it deletes (A).

    A hold's transaction holds the boundary from before its INSERT until its
    commit -- the window a delete could otherwise read the holds table in and
    see nothing. A batch arriving inside that window must wait on the lock
    rather than delete under stale hold state; ``lock_timeout`` makes the wait
    observable without a thread, which would pass by luck when the ordering was
    not enforced at all. Removing the boundary from execution makes this stop
    waiting, so it is a real proof that Class B execution takes it -- the gap
    #949 recorded for the Class B paths, closed.
    """

    factory = runtime_database.session_factory
    project_id, _first_id, _second_id = _seed_two_due_requests(factory)
    manifest_id, expected_sha256, _public = _plan(factory, project_id)

    with factory() as holding:
        # place_hold takes the boundary inside its command and holds it until
        # this uncommitted transaction ends.
        place_hold(
            holding, project_id=project_id, reason="open-records request", principal=OPERATOR
        )
        with factory() as blocked:
            blocked.execute(text("set local lock_timeout = '400ms'"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                execute_retention(
                    blocked,
                    manifest_id=manifest_id,
                    expected_sha256=expected_sha256,
                    executed_at=NOW,
                )
            blocked.rollback()
        holding.rollback()


# --- B: the worker cannot lift or write a hold ----------------------------


def test_the_worker_login_cannot_lift_or_write_a_hold(runtime_database):
    """No ``lift any hold`` for the worker, by any route (B).

    The worker observes holds and executes permitted deletion; it holds no
    authority to remove the restriction. Proved as the real ``corridor_worker``
    login including inherited grants: the raw writes are refused, and the lift
    command is not executable.
    """

    factory = runtime_database.session_factory
    with factory() as setup:
        project = Project(slug=f"rh-b-{uuid4().hex[:8]}", name="Hold B", is_synthetic=True)
        setup.add(project)
        setup.flush([project])
        hold = place_hold(
            setup, project_id=project.id, reason="litigation", principal=OPERATOR
        )
        hold_id = hold.id
        setup.commit()

    worker = _login_engine(runtime_database.name, "corridor_worker")
    try:
        with worker.connect() as conn:
            # The worker keeps SELECT: it must read holds to refuse deletion.
            visible = conn.execute(
                text("select count(*) from retention_holds where lifted_at is null")
            ).scalar_one()
            assert visible == 1

            for statement in (
                "update retention_holds set lifted_at = now(), lifted_by = 'x'",
                "delete from retention_holds",
                "truncate retention_holds",
                "insert into retention_holds (project_id, reason, placed_by) "
                "values (1, 'r', 'x')",
            ):
                with pytest.raises(ProgrammingError) as refused:
                    conn.execute(text(statement))
                conn.rollback()
                assert "permission denied" in str(refused.value)

            with pytest.raises(ProgrammingError) as refused_lift:
                conn.execute(
                    text("select lift_retention_hold(:id, 'local:sneaky', null)"),
                    {"id": hold_id},
                )
            conn.rollback()
            assert "permission denied for function" in str(refused_lift.value)

            with pytest.raises(ProgrammingError) as refused_place:
                conn.execute(
                    text("select place_retention_hold(1, 'r', 'local:sneaky')")
                )
            conn.rollback()
            assert "permission denied for function" in str(refused_place.value)
    finally:
        worker.dispose()

    # The hold is still active and unlifted after every attempt.
    with factory() as verify:
        row = verify.get(RetentionHold, hold_id)
        assert row.lifted_at is None


def test_the_web_login_places_and_lifts_a_hold_through_the_commands(runtime_database):
    """The positive half: the human capability may place and lift (B)."""

    factory = runtime_database.session_factory
    with factory() as setup:
        project = Project(slug=f"rh-w-{uuid4().hex[:8]}", name="Hold W", is_synthetic=True)
        setup.add(project)
        setup.flush([project])
        project_id = project.id
        setup.commit()

    web = _login_engine(runtime_database.name, "corridor_web")
    try:
        with web.begin() as conn:
            hold_id = conn.execute(
                text("select place_retention_hold(:p, 'litigation', 'local:op')"),
                {"p": project_id},
            ).scalar_one()
        with web.begin() as conn:
            lifted = conn.execute(
                text("select lift_retention_hold(:id, 'local:op', null)"),
                {"id": hold_id},
            ).scalar_one()
            assert lifted == hold_id
    finally:
        web.dispose()

    with factory() as verify:
        row = verify.get(RetentionHold, hold_id)
        assert row.placed_by == "local:op"
        assert row.lifted_by == "local:op"
        assert row.lifted_at is not None


# --- D: an uncertain object-store outcome is retained as uncertain ---------


def test_an_uncertain_object_store_outcome_is_retained_as_uncertain(
    runtime_database, tmp_path, monkeypatch
):
    """A lost deletion acknowledgement is uncertain until reconciled (D).

    The object-store deletion of a processing artifact cannot be confirmed:
    the batch neither pretends it was deleted nor that it was rolled back. The
    artifact is retained as uncertain, its staged bytes are left in place, and
    the acknowledgement reports it as failed/pending rather than enforced.
    """

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "staging"))
    monkeypatch.setattr(settings, "storage_backend", "filesystem")

    factory = runtime_database.session_factory
    render = tmp_path / "page-1.png"
    render.write_bytes(b"rendered page")
    digest = sha256(render.read_bytes()).hexdigest()
    key = content_key(digest, ".png")

    with factory() as setup:
        project = Project(slug=f"rh-d-{uuid4().hex[:8]}", name="Hold D", is_synthetic=True)
        setup.add(project)
        setup.flush([project])
        artifact = register_processing_artifact(
            setup,
            project_id=project.id,
            kind="page_render",
            path=render,
            terminal_at=NOW - timedelta(days=40),
        )
        project_id, artifact_id = project.id, artifact.id
        setup.commit()

    manifest_id, expected_sha256, public_id = _plan(factory, project_id)

    store = content_store()

    def lose_the_acknowledgement(_self, _key, *, permit):
        raise DeletionUncertain("the delete request timed out with no acknowledgement")

    monkeypatch.setattr(type(store), "delete_under_policy", lose_the_acknowledgement)

    acknowledgement = execute_retention_in_batches(
        factory,
        manifest_id=manifest_id,
        expected_sha256=expected_sha256,
        batch_size=10,
        executed_at=NOW,
    )

    assert acknowledgement["schema_version"] == "retention-execution-v1"
    assert acknowledgement["requested"] == 1
    assert acknowledgement["enforced"] == 0
    assert acknowledgement["failed_or_pending"] == 1
    assert acknowledgement["uncertain"] == 1

    with factory() as verify:
        row = verify.get(ProcessingArtifact, artifact_id)
        assert row.deleted_at is None
        assert row.deletion_uncertain_at is not None
    # The staged bytes were not removed on an unconfirmed delete.
    assert render.exists()
    assert store.exists(key)


# --- E: the acknowledgement distinguishes the three enforcement states -----


def test_the_execution_acknowledgement_distinguishes_the_three_states(runtime_database):
    """requested / enforced / failed-or-pending, printed after the boundary (E)."""

    factory = runtime_database.session_factory
    project_id, _first_id, _second_id = _seed_two_due_requests(factory)
    manifest_id, expected_sha256, public_id = _plan(factory, project_id)

    clean = execute_retention_in_batches(
        factory,
        manifest_id=manifest_id,
        expected_sha256=expected_sha256,
        batch_size=1,
        executed_at=NOW,
    )
    assert clean["schema_version"] == "retention-execution-v1"
    assert clean["requested"] == 2
    assert clean["enforced"] == 2
    assert clean["failed_or_pending"] == 0
    assert clean["outcome"] == "enforced"
    assert clean["manifest_public_id"] == public_id


def test_the_execution_acknowledgement_reports_a_hold_as_blocked(runtime_database):
    """A hold before any batch leaves nothing enforced and says why (E)."""

    factory = runtime_database.session_factory
    project_id, _first_id, _second_id = _seed_two_due_requests(factory)
    manifest_id, expected_sha256, _public = _plan(factory, project_id)

    with factory() as holding:
        place_hold(
            holding, project_id=project_id, reason="litigation", principal=OPERATOR
        )
        holding.commit()

    acknowledgement = execute_retention_in_batches(
        factory,
        manifest_id=manifest_id,
        expected_sha256=expected_sha256,
        batch_size=1,
        executed_at=NOW,
    )
    assert acknowledgement["requested"] == 2
    assert acknowledgement["enforced"] == 0
    assert acknowledgement["failed_or_pending"] == 2
    assert acknowledgement["outcome"] == "blocked"
    assert acknowledgement["refusal"] == "hold_active"


def test_the_hold_acknowledgement_states_only_what_the_boundary_won():
    """The approved sentence, owned once and printed only after commit (E)."""

    assert HOLD_ENFORCEMENT_ACKNOWLEDGEMENT == (
        "The hold is active. New deletion batches within its scope are blocked. "
        "A batch authorized before the hold took effect may already have completed."
    )
