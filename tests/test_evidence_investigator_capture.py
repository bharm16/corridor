"""Public cutoff-correct outcome capture over real PostgreSQL.

These tests drive the delayed association through its real seams: the frozen
shadow-case interface, the shared Due Work runtime under a controlled clock, and
the declared gate-7 observation contract. They assert the behaviors the ticket
turns on: a run before its cutoff refuses to associate; an exact or late run
associates the outcome as of the cutoff; history that cannot support an exact
answer stays incomplete with a reason rather than being backdated; the frozen
case, its run, and the immutable one-time capture are never rewritten; and
repeated ticks, competing workers, and restart converge on one association. What
the outcome *is* as of an instant (Not Relevant, guided Save, correction, Undo,
review duration) is one shared reading, proved in
``test_evidence_investigator_human_outcome.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.due_work import (
    EvidenceOutcomeCaptureDeclaration,
    HANDLER_EVIDENCE_OUTCOME_CAPTURE,
    configure_evidence_outcome_capture,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.evidence_investigator_capture import (
    CaptureContractRefusal,
    CaptureObservationContract,
    capture_status,
    declare_capture_contract,
    reconstruct_cutoff_outcome,
    run_outcome_capture,
)
from corridor.evidence_investigator import InvestigationBudget
from corridor.evidence_investigator_shadow import (
    capture_shadow_outcome,
    run_shadow_batch,
)
from corridor.models import (
    EvidenceInvestigationCaptureContract,
    EvidenceInvestigationCaptureResult,
    EvidenceInvestigationShadowOutcome,
    Project,
)
from access_support import seed_membership
from evidence_outcome_support import (
    HOUR,
    RECORDER,
    StubRuntime,
    freeze_case,
    identity,
    record_disposition,
    unplaced_statement,
)


# --------------------------------------------------------------------------- #
# Fixtures and builders
# --------------------------------------------------------------------------- #


@pytest.fixture
def project(session):
    project = Project(
        slug=f"capture-{uuid4().hex}",
        name="Capture test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    seed_membership(session, project, RECORDER)
    return project


def _contract(case, *, cutoff, window_start=None, history_retained_from=None, **overrides):
    frozen = case.frozen_at
    values = dict(
        project_id=case.project_id,
        cohort_id=f"cohort-{case.public_id[:8]}",
        model=case.model,
        prompt_version=case.prompt_version,
        prompt_sha256=case.prompt_sha256,
        adapter_contract_version=case.adapter_contract_version,
        tool_contract_version=case.tool_contract_version,
        validator_version="capture-validator-v1",
        baseline_identity="capture-baseline-v1",
        baseline_collection_contract="baseline-collection-v1",
        window_start=window_start or (frozen - HOUR),
        cutoff_at=cutoff,
        protection_end=cutoff + HOUR,
        history_retained_from=(
            history_retained_from
            if history_retained_from is not None
            else frozen - HOUR
        ),
        eligibility="unplaced-statement-v1",
        missing_label_policy="remain_missing",
        member_case_public_ids=(case.public_id,),
        declared_by="local:capture-approver",
    )
    values.update(overrides)
    return CaptureObservationContract(**values)


# --------------------------------------------------------------------------- #
# Gate-7 declaration
# --------------------------------------------------------------------------- #


def test_declaration_requires_a_complete_verifiable_contract(session, project):
    case = freeze_case(session, project)
    cutoff = case.frozen_at + HOUR
    now = case.frozen_at

    # A malformed missing-label policy is refused (missing labels must stay missing).
    with pytest.raises(CaptureContractRefusal):
        declare_capture_contract(
            session, _contract(case, cutoff=cutoff, missing_label_policy="fill_default"),
            now=now,
        )
    # A window that starts after its cutoff is refused.
    with pytest.raises(CaptureContractRefusal):
        declare_capture_contract(
            session, _contract(case, cutoff=cutoff, window_start=cutoff + HOUR), now=now
        )
    # A member frozen case whose configuration identity does not match is refused,
    # so a fresh run can never satisfy the frozen cohort.
    with pytest.raises(CaptureContractRefusal):
        declare_capture_contract(
            session, _contract(case, cutoff=cutoff, model="a-different-model"), now=now
        )
    assert session.scalar(
        select(func.count()).select_from(EvidenceInvestigationCaptureContract)
    ) == 0

    contract = declare_capture_contract(session, _contract(case, cutoff=cutoff), now=now)
    assert contract.contract_sha256
    # Re-declaring the identical contract returns the same content identity.
    again = declare_capture_contract(session, _contract(case, cutoff=cutoff), now=now)
    assert again.id == contract.id


def test_declaration_refuses_a_cross_project_member(session, project):
    other = Project(slug=f"other-{uuid4().hex}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    seed_membership(session, other, RECORDER)
    case = freeze_case(session, project)
    contract = _contract(case, cutoff=case.frozen_at + HOUR)
    cross = CaptureObservationContract(**{**contract.__dict__, "project_id": other.id})
    with pytest.raises(CaptureContractRefusal):
        declare_capture_contract(session, cross, now=case.frozen_at)


# --------------------------------------------------------------------------- #
# Cutoff-correct reconstruction from retained history
# --------------------------------------------------------------------------- #


def test_incomplete_when_retained_history_does_not_cover_the_case(session, project):
    case = freeze_case(session, project)
    cutoff = case.frozen_at + (2 * HOUR)
    # Coverage begins AFTER the case was frozen, so an earlier decision could have
    # existed and not been retained; the cutoff state cannot be proven.
    contract = declare_capture_contract(
        session,
        _contract(case, cutoff=cutoff, history_retained_from=case.frozen_at + HOUR),
        now=case.frozen_at,
    )
    record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=case.frozen_at + HOUR, reason="outside_project_scope",
    )
    outcome = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff + HOUR)
    assert outcome.completeness == "incomplete"
    assert outcome.incomplete_reason == "case_predates_retained_history"
    assert outcome.candidate_disposition is None
    assert outcome.unresolved is False


def test_accepted_disposition_without_a_grouped_save_is_incomplete(session, project):
    case = freeze_case(session, project)
    cutoff = case.frozen_at + (2 * HOUR)
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    record_disposition(session, case.candidate_id, "accepted", created_at=case.frozen_at + HOUR)
    outcome = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)
    assert outcome.completeness == "incomplete"
    assert outcome.incomplete_reason == "accepted_outcome_missing_receipt"


def test_overlapping_protection_windows_stay_separately_identified(session, project):
    case = freeze_case(session, project)
    frozen = case.frozen_at
    record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=frozen + HOUR, reason="outside_project_scope",
    )
    # Two contracts over the same case with overlapping protection windows.
    window_a = declare_capture_contract(
        session,
        _contract(case, cutoff=frozen + (2 * HOUR), protection_end=frozen + (5 * HOUR)),
        now=frozen,
    )
    window_b = declare_capture_contract(
        session,
        _contract(case, cutoff=frozen + (3 * HOUR), protection_end=frozen + (6 * HOUR)),
        now=frozen,
    )
    assert window_a.contract_sha256 != window_b.contract_sha256
    assert window_a.protection_end < window_b.protection_end  # windows overlap
    a = reconstruct_cutoff_outcome(session, case, window_a, executed_at=frozen + (2 * HOUR))
    b = reconstruct_cutoff_outcome(session, case, window_b, executed_at=frozen + (3 * HOUR))
    # Each window reconstructs its own complete association; neither collapses the
    # other, and the same human outcome is a legitimate label in both windows.
    assert a.completeness == "complete" and b.completeness == "complete"
    assert a.human_outcome_identity == b.human_outcome_identity


# --------------------------------------------------------------------------- #
# Identity binding and duplicate refusal
# --------------------------------------------------------------------------- #


def test_association_binds_exact_case_run_and_outcome_identities(session, project):
    case = freeze_case(session, project)
    cutoff = case.frozen_at + (2 * HOUR)
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=case.frozen_at + HOUR, reason="outside_project_scope",
    )
    outcome = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)
    # The association names the exact project, frozen case, run, and human-outcome
    # source identities, so it can never be mistaken for another case's outcome.
    assert outcome.outcome_identities["shadow_case_id"] == case.id
    assert outcome.outcome_identities["candidate_id"] == case.candidate_id
    assert outcome.outcome_identities["candidate_disposition_id"] is not None
    assert outcome.human_outcome_identity is not None


def test_reconstruction_refuses_a_case_from_another_project(session, project):
    other = Project(slug=f"other-{uuid4().hex}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    seed_membership(session, other, RECORDER)
    # A contract sealed for `project`, and a frozen case that belongs to `other`.
    home_case = freeze_case(session, project)
    contract = declare_capture_contract(
        session, _contract(home_case, cutoff=home_case.frozen_at + HOUR),
        now=home_case.frozen_at,
    )
    foreign_case = freeze_case(session, other)
    outcome = reconstruct_cutoff_outcome(
        session, foreign_case, contract, executed_at=contract.cutoff_at
    )
    assert outcome.completeness == "incomplete"
    assert outcome.incomplete_reason == "cross_project_or_missing_candidate"


# --------------------------------------------------------------------------- #
# The shared Due Work runtime: timing, idempotency, recovery, protection
# --------------------------------------------------------------------------- #


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _committed_case(factory):
    """Freeze a case, record a Not Relevant decision, and enable capture.

    Returns identities plus the intended cutoff, all anchored to the real freeze
    time so the human decision, the cutoff, and the controlled runtime clock stay
    consistent.
    """

    with factory() as setup:
        project = Project(
            slug=f"capture-rt-{uuid4().hex}", name="Capture runtime", is_synthetic=True
        )
        setup.add(project)
        setup.flush()
        seed_membership(setup, project, RECORDER)
        candidate = unplaced_statement(setup, project)
        [shadow] = asyncio.run(
            run_shadow_batch(
                setup, project.id, runtime_factory=StubRuntime,
                identity=identity(model=f"rt-{uuid4().hex[:8]}"),
                budget=InvestigationBudget(),
            )
        )
        case = shadow.case
        frozen = case.frozen_at
        cutoff = frozen + HOUR
        record_disposition(
            setup, candidate.id, "not_relevant",
            created_at=frozen + timedelta(minutes=1),
            reason="outside_project_scope",
        )
        contract = declare_capture_contract(
            setup,
            _contract(case, cutoff=cutoff, protection_end=cutoff + (4 * HOUR)),
            now=frozen,
        )
        schedule = configure_evidence_outcome_capture(
            setup,
            EvidenceOutcomeCaptureDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="capture-config-v1",
                cohort_id=contract.cohort_id,
                observation_contract_sha256=contract.contract_sha256,
                starts_at=(frozen - (2 * HOUR)).replace(
                    minute=0, second=0, microsecond=0
                ),
            ),
            now=frozen,
        )
        ids = (
            project.id, contract.id, contract.contract_sha256, schedule.id, cutoff
        )
        setup.commit()
    return ids


def test_runtime_refuses_before_cutoff_then_associates_late_and_reads_back(
    runtime_database,
):
    factory = runtime_database.session_factory
    project_id, contract_id, _sha, _schedule_id, cutoff = _committed_case(factory)

    # A tick BEFORE the declared cutoff refuses to associate; the case is pending.
    before = cutoff - timedelta(minutes=30)
    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=before)
    early = run_due_work_once(
        factory, clock=ControlledClock(before), owner="runtime:capture-worker"
    )
    assert early is not None
    assert early.handler_result["before_cutoff_pending"] == 1
    assert early.handler_result["captured_complete"] == 0
    with factory() as reading:
        assert reading.scalar(
            select(func.count()).select_from(EvidenceInvestigationCaptureResult).where(
                EvidenceInvestigationCaptureResult.capture_contract_id == contract_id
            )
        ) == 0

    # A later tick associates facts as of the cutoff, honestly late.
    executed = cutoff + (2 * HOUR)  # still inside protection
    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=executed)
    result = run_due_work_once(
        factory, clock=ControlledClock(executed), owner="runtime:capture-worker"
    )
    assert result is not None
    assert result.handler_key == HANDLER_EVIDENCE_OUTCOME_CAPTURE
    assert result.execution_outcome == "completed"
    assert result.handler_result["captured_complete"] == 1

    with factory() as reading:
        rows = reading.scalars(
            select(EvidenceInvestigationCaptureResult).where(
                EvidenceInvestigationCaptureResult.capture_contract_id == contract_id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].completeness == "complete"
        assert rows[0].cutoff_at == cutoff
        assert rows[0].executed_at == executed
        assert rows[0].executed_at > rows[0].cutoff_at  # honestly late
        assert rows[0].candidate_disposition == "not_relevant"
        status = capture_status(reading, project_id=project_id)
    result_view = status["results"][0]
    assert result_view["cutoff_at"] != result_view["executed_at"]
    assert result_view["on_time"] is False
    assert result_view["protection_active"] is True  # protection not released


def test_runtime_repeated_ticks_and_competing_workers_converge(runtime_database):
    factory = runtime_database.session_factory
    project_id, contract_id, contract_sha, _schedule_id, cutoff = _committed_case(
        factory
    )
    executed = cutoff + HOUR

    # Repeated ticks: a second full run finds the association already present.
    for _ in range(2):
        with factory() as ticking:
            with ticking.begin():
                enqueue_due_work(ticking, now=executed)
        run_due_work_once(
            factory, clock=ControlledClock(executed), owner="runtime:capture-worker"
        )

    # Competing direct passes race the same durable pass.
    barrier = Barrier(2)

    def _pass():
        barrier.wait()
        return run_outcome_capture(
            factory, project_id=project_id, contract_sha256=contract_sha,
            configuration_version="capture-config-v1", clock=ControlledClock(executed),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        [future.result() for future in [pool.submit(_pass), pool.submit(_pass)]]

    with factory() as reading:
        assert reading.scalar(
            select(func.count()).select_from(EvidenceInvestigationCaptureResult).where(
                EvidenceInvestigationCaptureResult.capture_contract_id == contract_id
            )
        ) == 1


def test_capture_never_rewrites_the_one_time_capture_or_the_frozen_case(session, project):
    case = freeze_case(session, project)
    cutoff = case.frozen_at + (2 * HOUR)
    record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=case.frozen_at + HOUR, reason="outside_project_scope",
    )
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    one_time = capture_shadow_outcome(session, case.public_id)
    one_time_sha = one_time.outcome_sha256
    frozen_at = case.frozen_at

    reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)

    session.refresh(one_time)
    session.refresh(case)
    assert one_time.outcome_sha256 == one_time_sha
    assert case.frozen_at == frozen_at
    # There is exactly one immutable one-time outcome and it is a distinct record.
    assert session.scalar(
        select(func.count()).select_from(EvidenceInvestigationShadowOutcome).where(
            EvidenceInvestigationShadowOutcome.shadow_case_id == case.id
        )
    ) == 1
