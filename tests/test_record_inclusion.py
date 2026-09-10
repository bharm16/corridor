"""The Record Inclusion pass body under its watermark.

The watermark's own properties — coalescing, gating, recovery, the advance to
the observed snapshot — are proved once over both bindings in
``test_reconciliation_watermark``. This file proves only what that stub cannot:
that ``reconcile_record_inclusion`` hands ``load_project`` to the loop, so a
pending project is loaded exactly once (each admission pass appends one
PolicyRun) and an idle one appends nothing (#342).
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select

from corridor.admission import LoadResult, reconcile_record_inclusion
from corridor.models import PolicyRun, Project
from corridor.record_inclusion import record_inclusion_pending, request_record_inclusion


def _policy_runs(session, project_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(PolicyRun).where(
            PolicyRun.project_id == project_id
        )
    )


def test_the_pass_loads_a_pending_project_once_and_an_idle_one_never(session):
    project = Project(slug=f"ri-{uuid4().hex}", name="Record Inclusion", is_synthetic=True)
    session.add(project)
    session.flush()

    idle = reconcile_record_inclusion(session, project.id)
    assert (idle.did_load, idle.reconciled_seq, idle.load) == (False, 0, None)
    assert _policy_runs(session, project.id) == 0

    for _ in range(3):
        request_record_inclusion(session, project.id, "extraction_completed")
    result = reconcile_record_inclusion(session, project.id)
    assert result.did_load is True
    assert result.reconciled_seq == 3
    assert isinstance(result.load, LoadResult)
    # Three bumps, one reconciliation: load_project ran exactly once (its two
    # admission passes each append one PolicyRun).
    assert _policy_runs(session, project.id) == 2
    assert record_inclusion_pending(session, project.id) is False

    for _ in range(5):
        assert reconcile_record_inclusion(session, project.id).did_load is False
    assert _policy_runs(session, project.id) == 2
