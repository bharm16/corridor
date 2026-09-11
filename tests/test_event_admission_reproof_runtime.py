"""Scheduled stale-class re-proof through the shared supervised Due Work runtime.

These tests use the harness-owned disposable database because re-proof recovery is a
committed-transaction seam: an occurrence is claimed, the ADR-0050 replay records an
immutable receipt and reactivates through the suspension-safe interface in its own
short transaction, and a later tick or worker must observe those durable results. The
disposable clone provisioning and clean-revision checkout are the deferred live step,
so a recording fake stands in for the replay executor — it performs the *real* receipt
recording and activation against the runtime database, proving the mechanism end to end
while only skipping the clone plumbing.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

from sqlalchemy import func, select

from corridor.due_work import (
    EventAdmissionReproofDeclaration,
    HANDLER_EVENT_ADMISSION_REPROOF,
    HandlerContract,
    claim_due_work,
    configure_due_work,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.event_admission import (
    EVENT_ADMISSION_POLICY_VERSION,
    UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
    UNKNOWN_SCOPE_POLICY_VERSION,
    _current_migration_head,
    _current_source_revision,
    canonical_event_admission_policy,
    read_event_admission_policy_status,
)
from corridor.event_admission_acceptance import (
    EventAdmissionAcceptanceResult,
    RECEIPT_VERSION,
    SELECTION_RULE,
    _receipt_promotion_gates,
    activate_passing_acceptance,
    record_acceptance_receipt,
    suspend_unknown_scope_admission,
)
from corridor.event_admission_reproof import execute_scheduled_reproof
from corridor.migrations.policy import SUPPORTED_FROM_REVISION
from corridor.models import (
    EventAdmissionAcceptanceReceipt,
    EventAdmissionActivation,
    Project,
)
from corridor import policy
from clock_support import ControlledClock


def _acceptance_receipt(session, project, *, source_revision, migration_head, eligible):
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "source_revision": source_revision,
        "migration_head": migration_head,
        "selection_rule": SELECTION_RULE,
        "policy_version": UNKNOWN_SCOPE_POLICY_VERSION,
        "policy_sha256": policy.canonical_sha256(
            canonical_event_admission_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
        ),
        "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        "opt_in": {
            "metrics": {
                "admission_count": 1 if eligible else 0,
                "false_party_attribution": 0,
                "false_dependency_scope": 0,
                "project_side_masquerade": 0,
                "cross_project_references": 0,
                "unauthorized_work_decisions": 0,
                "duplicates": 0,
                "protected_dependency_delta": 0,
                "protected_report_delta": 0,
                "invalid_evidence_or_receipts": 0,
            },
            "admissions": (
                [{"candidate_id": 1, "commitment_lineage_id": 1, "statement_event_id": 1}]
                if eligible
                else []
            ),
            "work_items": (
                [
                    {
                        "commitment_lineage_id": 1,
                        "statement_event_id": 1,
                        "source_candidate_id": 1,
                        "dependency_id": None,
                        "attention_reasons": [
                            "unknown_scope",
                            "missing_internal_owner",
                            "missing_next_action",
                        ],
                    }
                ]
                if eligible
                else []
            ),
        },
        "migration_rehearsal": {
            "predecessor": SUPPORTED_FROM_REVISION,
            "head": migration_head,
            "status": "passed",
            "fresh_head": migration_head,
            "fresh_status": "passed",
        },
        "authority_statement": "Only the deterministic unknown-scope class is authorized.",
    }
    receipt["gates"] = _receipt_promotion_gates(receipt)
    return receipt


class RecordingReplay:
    """A stand-in for the clone-provisioning replay executor.

    It performs the same durable tail the real replay does — record one immutable
    acceptance receipt for the deployed identities, then reactivate through the
    suspension-safe interface — in its own committed transactions, so the runtime's
    activation, suspension, and gate behavior are exercised for real. ``eligible``
    toggles the nonzero-eligible gate; ``before_activation`` runs a concurrent human
    act after the receipt commits but before activation.
    """

    def __init__(self, factory, *, eligible=True, before_activation=None):
        self.factory = factory
        self.eligible = eligible
        self.before_activation = before_activation
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        revision = _current_source_revision()
        with self.factory() as reading:
            head = _current_migration_head(reading)
        with self.factory() as recording:
            with recording.begin():
                project = recording.scalar(
                    select(Project).where(Project.slug == request.project_slug)
                )
                receipt_json = _acceptance_receipt(
                    recording,
                    project,
                    source_revision=revision,
                    migration_head=head,
                    eligible=self.eligible,
                )
                stored = record_acceptance_receipt(
                    recording,
                    project_id=project.id,
                    source_revision=revision,
                    migration_head=head,
                    receipt_json=receipt_json,
                )
                receipt_id, status, sha = stored.id, stored.status, stored.receipt_sha256
        if self.before_activation is not None:
            self.before_activation()
        with self.factory() as activating:
            with activating.begin():
                activation = activate_passing_acceptance(activating, receipt_id)
                activated = activation is not None
        return EventAdmissionAcceptanceResult(
            receipt_id=receipt_id,
            status=status,
            activated=activated,
            receipt_sha256=sha,
            source_revision=revision,
            migration_head=head,
            clone_database_names=("reproof_clone_a", "reproof_clone_b"),
        )


class RaisingReplay:
    def __init__(self):
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        raise RuntimeError("disposable clone provisioning failed")


def _registry(replay):
    def run_effectful(context):
        return execute_scheduled_reproof(context, replay=replay)

    return {
        HANDLER_EVENT_ADMISSION_REPROOF: HandlerContract(
            key=HANDLER_EVENT_ADMISSION_REPROOF,
            scope_kind="one_project_event_admission_class",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            run_effectful=run_effectful,
        )
    }


def _make_project(factory, now, *, setup=None):
    """Create a committed project + enabled re-proof schedule, running ``setup``.

    ``setup`` receives an open session (inside a transaction) and the project so a test
    can record the prior proof/activation history that defines the starting state.
    """

    with factory() as session:
        with session.begin():
            project = Project(
                slug=f"reproof-rt-{uuid4().hex}",
                name="Re-proof Runtime",
                is_synthetic=True,
                project_side_parties=["LJA"],
            )
            session.add(project)
            session.flush([project])
            if setup is not None:
                setup(session, project)
            declaration = EventAdmissionReproofDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="event-admission-reproof-v1",
                policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
                reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
                selection_rule=SELECTION_RULE,
                starts_at=now.replace(minute=0, second=0, microsecond=0),
            )
            schedule = configure_due_work(session, declaration, now=now)
            ids = (project.id, schedule.id)
    return ids


def _record_stale_passing(session, project, *, revision="0" * 40):
    record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=revision,
        migration_head=_current_migration_head(session),
        receipt_json=_acceptance_receipt(
            session,
            project,
            source_revision=revision,
            migration_head=_current_migration_head(session),
            eligible=True,
        ),
    )


def _record_prior_activation_then_stale(session, project):
    current = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=_acceptance_receipt(
            session,
            project,
            source_revision=_current_source_revision(),
            migration_head=_current_migration_head(session),
            eligible=True,
        ),
    )
    activate_passing_acceptance(session, current.id)
    _record_stale_passing(session, project, revision="2" * 40)


def _activations(session, project_id):
    return session.scalars(
        select(EventAdmissionActivation)
        .where(EventAdmissionActivation.project_id == project_id)
        .order_by(EventAdmissionActivation.id)
    ).all()


def _run(factory, now, replay, owner="runtime:reproof-worker"):
    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=now)
    return run_due_work_once(
        factory, clock=ControlledClock(now), owner=owner, registry=_registry(replay)
    )


NOW = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)


# --- unchanged vs stale identities ---------------------------------------------


def test_current_active_class_is_a_no_op_and_never_runs_the_replay(runtime_database):
    factory = runtime_database.session_factory

    def setup(session, project):
        current = record_acceptance_receipt(
            session,
            project_id=project.id,
            source_revision=_current_source_revision(),
            migration_head=_current_migration_head(session),
            receipt_json=_acceptance_receipt(
                session,
                project,
                source_revision=_current_source_revision(),
                migration_head=_current_migration_head(session),
                eligible=True,
            ),
        )
        activate_passing_acceptance(session, current.id)

    project_id, _ = _make_project(factory, NOW, setup=setup)
    replay = RecordingReplay(factory)
    result = _run(factory, NOW, replay)

    assert result.execution_outcome == "completed"
    assert result.handler_result["was_stale"] is False
    assert result.handler_result["did_reprove"] is False
    assert result.handler_result["health"] == "healthy"
    assert result.handler_result["detail"] == "passed_current"
    assert result.safe_next_step == "none"
    assert replay.calls == 0


def test_never_proved_class_preserves_first_activation_authority(runtime_database):
    factory = runtime_database.session_factory
    project_id, _ = _make_project(factory, NOW)
    replay = RecordingReplay(factory)

    result = _run(factory, NOW, replay)

    assert result.execution_outcome == "completed"
    assert result.handler_result["proof_status"] == "no_applicable_proof"
    assert result.handler_result["did_reprove"] is False
    assert replay.calls == 0
    with factory() as verify:
        assert _activations(verify, project_id) == []


def test_stale_class_is_reproved_and_reactivated(runtime_database):
    factory = runtime_database.session_factory
    project_id, _ = _make_project(
        factory, NOW, setup=_record_prior_activation_then_stale
    )
    replay = RecordingReplay(factory, eligible=True)

    result = _run(factory, NOW, replay)

    assert result.execution_outcome == "completed"
    assert result.handler_result["was_stale"] is True
    assert result.handler_result["did_reprove"] is True
    assert result.handler_result["proof_outcome"] == "passed"
    assert result.handler_result["activated"] is True
    assert result.handler_result["health"] == "healthy"
    assert result.handler_result["effective_status"] == "active"
    assert replay.calls == 1

    with factory() as verify:
        status = read_event_admission_policy_status(verify, project_id)
        assert status.status == "active"
        assert status.effective_policy_version == UNKNOWN_SCOPE_POLICY_VERSION
        reported = due_work_status(verify, project_id=project_id)
        assert reported["receipts"][-1]["handler"] == HANDLER_EVENT_ADMISSION_REPROOF
        assert reported["receipts"][-1]["handler_result"]["did_reprove"] is True


# --- proof gates: zero eligibility, honest failure, no false reactivation -------


def test_zero_eligible_retains_a_failed_proof_and_safe_fallback(runtime_database):
    factory = runtime_database.session_factory
    project_id, _ = _make_project(
        factory, NOW, setup=_record_prior_activation_then_stale
    )
    replay = RecordingReplay(factory, eligible=False)

    result = _run(factory, NOW, replay)

    assert result.handler_result["did_reprove"] is True
    assert result.handler_result["proof_outcome"] == "failed"
    assert result.handler_result["activated"] is False
    assert result.handler_result["health"] == "recovery_attention_required"
    assert result.safe_next_step == "inspect_event_admission_reproof_attention"

    with factory() as verify:
        status = read_event_admission_policy_status(verify, project_id)
        # The newest proof is a failure, so the predecessor rules stay effective and
        # the older passing receipt is never reactivated behind the newer failure.
        assert status.proof_status == "failed_newest_proof"
        assert status.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION
        assert status.status != "active"

    # A later tick sees a current (failed) newest proof, not staleness, so it does not
    # loop the replay on unchanged unavailable inputs.
    later = NOW + timedelta(hours=1)
    followup = _run(factory, later, replay)
    assert followup.handler_result["was_stale"] is False
    assert followup.handler_result["did_reprove"] is False
    assert replay.calls == 1


# --- deferred live execution ----------------------------------------------------


def test_stale_class_without_a_live_executor_surfaces_attention_and_does_not_loop(
    runtime_database,
):
    factory = runtime_database.session_factory
    project_id, _ = _make_project(factory, NOW, setup=_record_stale_passing)

    # The default handler path with no replay injected and no live executor wired.
    def run_effectful(context):
        return execute_scheduled_reproof(context)

    registry = {
        HANDLER_EVENT_ADMISSION_REPROOF: HandlerContract(
            key=HANDLER_EVENT_ADMISSION_REPROOF,
            scope_kind="one_project_event_admission_class",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            run_effectful=run_effectful,
        )
    }

    for hour in (0, 1, 2):
        tick_at = NOW + timedelta(hours=hour)
        with factory() as ticking:
            with ticking.begin():
                enqueue_due_work(ticking, now=tick_at)
        result = run_due_work_once(
            factory,
            clock=ControlledClock(tick_at),
            owner="runtime:reproof-worker",
            registry=registry,
        )
        assert result.execution_outcome == "completed"
        assert result.handler_result["was_stale"] is True
        assert result.handler_result["did_reprove"] is False
        assert result.handler_result["detail"] == "live_recovery_execution_deferred"
        assert result.handler_result["health"] == "recovery_attention_required"

    with factory() as verify:
        # No acceptance receipt was written: the mechanism refused to fake a proof.
        assert (
            verify.scalar(
                select(func.count())
                .select_from(EventAdmissionAcceptanceReceipt)
                .where(EventAdmissionAcceptanceReceipt.project_id == project_id)
            )
            == 1  # only the stale setup receipt
        )


# --- transport / harness failure cannot masquerade as a passing proof -----------


def test_replay_infrastructure_failure_is_a_bounded_retained_failure(runtime_database):
    factory = runtime_database.session_factory
    project_id, _ = _make_project(factory, NOW, setup=_record_stale_passing)
    replay = RaisingReplay()

    first = _run(factory, NOW, replay)
    assert first.execution_outcome == "retry_due"
    assert first.error_code == "handler_execution_failed"

    # Past the retry backoff the runtime retries the same occurrence once more, then
    # terminates at the declared attempt bound rather than looping unbounded.
    retry_at = NOW + timedelta(seconds=301)
    second = run_due_work_once(
        factory,
        clock=ControlledClock(retry_at),
        owner="runtime:reproof-worker",
        registry=_registry(replay),
    )
    assert second.execution_outcome == "failed"
    assert second.error_code == "handler_execution_failed"
    assert replay.calls == 2  # bounded by max_attempts, no unbounded loop

    # A further tick finds no reclaimable occurrence: the failure is terminal.
    assert (
        run_due_work_once(
            factory,
            clock=ControlledClock(retry_at + timedelta(seconds=1)),
            owner="runtime:reproof-worker",
            registry=_registry(replay),
        )
        is None
    )

    with factory() as verify:
        # No proof receipt was recorded: a failed clone run is never a passing proof.
        assert (
            verify.scalar(
                select(func.count())
                .select_from(EventAdmissionAcceptanceReceipt)
                .where(EventAdmissionAcceptanceReceipt.project_id == project_id)
            )
            == 1
        )


# --- both suspension races ------------------------------------------------------


def test_suspension_committed_while_replay_runs_prevents_activation(runtime_database):
    factory = runtime_database.session_factory
    project_id, _ = _make_project(
        factory, NOW, setup=_record_prior_activation_then_stale
    )

    def suspend_mid_replay():
        with factory() as s:
            with s.begin():
                suspend_unknown_scope_admission(
                    s,
                    project_id=project_id,
                    reason="paused during recovery replay",
                    recorded_by="local:operations",
                )

    replay = RecordingReplay(factory, before_activation=suspend_mid_replay)
    result = _run(factory, NOW, replay)

    assert result.handler_result["did_reprove"] is True
    assert result.handler_result["proof_outcome"] == "passed"
    # The passing-but-unactivated receipt is retained honestly; the human act governs.
    assert result.handler_result["activated"] is False
    assert result.handler_result["detail"] == "reproved_passing_activation_vetoed"
    assert result.handler_result["effective_status"] == "suspended"

    with factory() as verify:
        status = read_event_admission_policy_status(verify, project_id)
        assert status.status == "suspended"
        assert status.proof_status == "passed_current"
        # A passing receipt exists but the class is not active — no auto-lift occurred.
        assert status.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION


def test_suspension_committed_after_activation_governs_processing(runtime_database):
    factory = runtime_database.session_factory
    project_id, _ = _make_project(
        factory, NOW, setup=_record_prior_activation_then_stale
    )
    replay = RecordingReplay(factory, eligible=True)

    result = _run(factory, NOW, replay)
    assert result.handler_result["activated"] is True

    with factory() as s:
        with s.begin():
            suspend_unknown_scope_admission(
                s,
                project_id=project_id,
                reason="suspended after recovery",
                recorded_by="local:operations",
            )

    with factory() as verify:
        status = read_event_admission_policy_status(verify, project_id)
        assert status.status == "suspended"
        # The passing receipt is retained; the later suspension governs ordinary work.
        assert status.proof_status == "passed_current"
        assert status.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION
        # The recovery receipt still reports the activation it genuinely produced.
        reported = due_work_status(verify, project_id=project_id)
        assert reported["receipts"][-1]["handler_result"]["activated"] is True


# --- concurrent / repeated occurrences & crash recovery -------------------------


def test_repeated_ticks_coalesce_to_one_occurrence(runtime_database):
    factory = runtime_database.session_factory
    project_id, schedule_id = _make_project(factory, NOW, setup=_record_stale_passing)

    with factory() as ticking:
        with ticking.begin():
            first = enqueue_due_work(ticking, now=NOW)
    with factory() as ticking:
        with ticking.begin():
            second = enqueue_due_work(ticking, now=NOW.replace(minute=30))
    assert len(first) == 1
    assert [o.public_id for o in first] == [o.public_id for o in second]


def test_competing_workers_coalesce_on_one_reproof(runtime_database):
    factory = runtime_database.session_factory
    project_id, _ = _make_project(
        factory, NOW, setup=_record_prior_activation_then_stale
    )
    replay = RecordingReplay(factory)
    registry = _registry(replay)

    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=NOW)

    barrier = Barrier(2)

    def worker(name):
        barrier.wait()
        return run_due_work_once(
            factory,
            clock=ControlledClock(NOW),
            owner=f"runtime:{name}",
            registry=registry,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            f.result()
            for f in [pool.submit(worker, "worker-a"), pool.submit(worker, "worker-b")]
        ]

    completed = [r for r in results if r is not None and r.execution_outcome == "completed"]
    assert len(completed) == 1
    assert replay.calls == 1
    with factory() as verify:
        # Exactly one reactivation despite two competing workers.
        activate = [a for a in _activations(verify, project_id) if a.action == "activate"]
        # One prior setup activation + one recovery activation.
        assert len(activate) == 2


def test_crash_recovery_reclaims_and_does_not_double_activate(runtime_database):
    factory = runtime_database.session_factory
    project_id, schedule_id = _make_project(
        factory, NOW, setup=_record_prior_activation_then_stale
    )
    replay = RecordingReplay(factory)

    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=NOW)

    # A worker claims the occurrence and then crashes without finalizing.
    with factory() as claiming:
        with claiming.begin():
            claim = claim_due_work(claiming, now=NOW, owner="runtime:crashed")
    assert claim is not None

    # After the lease expires the runtime recovers the abandoned occurrence, runs the
    # replay once, and reactivates once — no duplicate activation from the crash.
    recover_at = claim.lease_expires_at + timedelta(seconds=1)
    recovered = run_due_work_once(
        factory,
        clock=ControlledClock(recover_at),
        owner="runtime:recovery",
        registry=_registry(replay),
    )
    assert recovered is not None
    assert recovered.execution_outcome == "completed"
    assert recovered.handler_result["did_reprove"] is True
    assert replay.calls == 1

    with factory() as verify:
        activate = [a for a in _activations(verify, project_id) if a.action == "activate"]
        assert len(activate) == 2  # one setup, one recovery — never doubled
        status = read_event_admission_policy_status(verify, project_id)
        assert status.status == "active"


# --- gate-7 disabled until declared ---------------------------------------------


def test_reproof_is_disabled_until_gate7_is_declared(runtime_database):
    factory = runtime_database.session_factory
    with factory() as setup:
        with setup.begin():
            project = Project(
                slug=f"reproof-undeclared-{uuid4().hex}",
                name="Undeclared",
                is_synthetic=True,
                project_side_parties=["LJA"],
            )
            setup.add(project)
            setup.flush([project])
            _record_stale_passing(setup, project)
            project_id = project.id

    # No gate-7 declaration exists, so nothing enqueues and nothing runs — a stale
    # class does not schedule itself.
    with factory() as ticking:
        with ticking.begin():
            assert enqueue_due_work(ticking, now=NOW) == ()
    result = run_due_work_once(
        factory, clock=ControlledClock(NOW), owner="runtime:reproof-worker"
    )
    assert result is None
