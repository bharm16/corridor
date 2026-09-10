"""The durable Record Inclusion watermark: coalescing, gating, and recovery.

The watermark exists so idle reconciliation appends no new Policy Runs and so a
committed hand-off survives a process exit while a rolled-back one leaves no
work. Pure gating and coalescing are checked on a rollback-scoped session;
cross-transaction durability uses the harness-owned committed database.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.models import PolicyRun, Project, RecordInclusionRequest
from corridor.admission import reconcile_record_inclusion
from corridor.record_inclusion import (
    pending_record_inclusion_project_ids,
    record_inclusion_pending,
    request_record_inclusion,
)


@pytest.fixture
def project(session):
    p = Project(slug=f"ri-{uuid4().hex}", name="Record Inclusion", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def _policy_runs(session, project_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(PolicyRun).where(
            PolicyRun.project_id == project_id
        )
    )


def test_request_makes_a_project_pending(session, project):
    assert record_inclusion_pending(session, project.id) is False
    request_record_inclusion(session, project.id, "extraction_completed")
    assert record_inclusion_pending(session, project.id) is True
    row = session.get(RecordInclusionRequest, project.id)
    assert (row.dirty_seq, row.reconciled_seq) == (1, 0)
    assert row.last_reason == "extraction_completed"


def test_repeated_requests_coalesce_into_one_pending_load(session, project):
    for _ in range(3):
        request_record_inclusion(session, project.id, "extraction_completed")
    row = session.get(RecordInclusionRequest, project.id)
    assert (row.dirty_seq, row.reconciled_seq) == (3, 0)

    before = _policy_runs(session, project.id)
    result = reconcile_record_inclusion(session, project.id)
    assert result.did_load is True
    assert result.reconciled_seq == 3
    # Three bumps, one reconciliation: load_project ran exactly once (its two
    # admission passes each append one PolicyRun).
    assert _policy_runs(session, project.id) == before + 2
    assert record_inclusion_pending(session, project.id) is False


def test_idle_reconciliation_when_clean_appends_no_policy_runs(session, project):
    request_record_inclusion(session, project.id, "extraction_completed")
    reconcile_record_inclusion(session, project.id)
    baseline = _policy_runs(session, project.id)

    for _ in range(5):
        result = reconcile_record_inclusion(session, project.id)
        assert result.did_load is False
    assert _policy_runs(session, project.id) == baseline


def test_reconcile_on_never_requested_project_is_a_no_op(session, project):
    result = reconcile_record_inclusion(session, project.id)
    assert result.did_load is False
    assert result.reconciled_seq == 0
    assert _policy_runs(session, project.id) == 0
    assert session.get(RecordInclusionRequest, project.id) is None


def test_a_bump_after_reconciliation_makes_the_project_pending_again(session, project):
    request_record_inclusion(session, project.id, "extraction_completed")
    reconcile_record_inclusion(session, project.id)
    assert record_inclusion_pending(session, project.id) is False

    request_record_inclusion(session, project.id, "identity_changed")
    assert record_inclusion_pending(session, project.id) is True
    row = session.get(RecordInclusionRequest, project.id)
    assert (row.dirty_seq, row.reconciled_seq) == (2, 1)


def test_pending_ids_drains_every_pending_project(session):
    pending = []
    clean = []
    for index in range(3):
        p = Project(slug=f"ri-pending-{uuid4().hex}", name="p", is_synthetic=True)
        session.add(p)
        session.flush()
        request_record_inclusion(session, p.id, "extraction_completed")
        pending.append(p.id)
    for index in range(2):
        p = Project(slug=f"ri-clean-{uuid4().hex}", name="c", is_synthetic=True)
        session.add(p)
        session.flush()
        request_record_inclusion(session, p.id, "extraction_completed")
        reconcile_record_inclusion(session, p.id)
        clean.append(p.id)

    drained = pending_record_inclusion_project_ids(session)
    assert set(pending).issubset(set(drained))
    assert set(clean).isdisjoint(set(drained))


def test_request_requires_a_reason(session, project):
    with pytest.raises(ValueError, match="reason"):
        request_record_inclusion(session, project.id, "")


def test_committed_request_survives_a_process_exit(runtime_database):
    factory = runtime_database.session_factory
    with factory() as producing:
        project = Project(
            slug=f"ri-durable-{uuid4().hex}", name="Durable", is_synthetic=True
        )
        producing.add(project)
        producing.flush([project])
        request_record_inclusion(producing, project.id, "extraction_completed")
        project_id = project.id
        producing.commit()

    # A brand-new session, as if the process had restarted, still sees the work.
    with factory() as recovering:
        assert pending_record_inclusion_project_ids(recovering) == [project_id]
        assert record_inclusion_pending(recovering, project_id) is True
        result = reconcile_record_inclusion(recovering, project_id)
        assert result.did_load is True
        recovering.commit()

    with factory() as verifying:
        assert record_inclusion_pending(verifying, project_id) is False


def test_a_rolled_back_producer_leaves_no_work(runtime_database):
    factory = runtime_database.session_factory
    with factory() as setup:
        project = Project(
            slug=f"ri-rollback-{uuid4().hex}", name="Rollback", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        project_id = project.id
        setup.commit()

    with factory() as aborting:
        request_record_inclusion(aborting, project_id, "extraction_completed")
        aborting.rollback()

    with factory() as verifying:
        assert record_inclusion_pending(verifying, project_id) is False
        assert verifying.get(RecordInclusionRequest, project_id) is None
