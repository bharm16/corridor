"""The one reconciliation watermark, proved once over both request tables.

The watermark exists so idle reconciliation runs no receipt-bearing pass and so
a committed hand-off survives a process exit while a rolled-back one leaves no
work. Every property here is stated against the watermark interface a caller
would use — request, pending, drain, reconcile under lock — and parameterised
over the two bindings (Record Inclusion and revision reconciliation), so the
two request tables cannot drift apart again. The pass body is a stub that
counts; what each real pass body does is proved beside that body
(``test_record_inclusion``, ``test_revision_reconciliation``). Pure gating and
coalescing use a rollback-scoped session; cross-transaction durability uses the
harness-owned committed database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from corridor.models import Project
from corridor.record_inclusion import RECORD_INCLUSION
from corridor.revision_reconciliation_request import REVISION_RECONCILIATION

WATERMARKS = [
    pytest.param(RECORD_INCLUSION, id="record_inclusion"),
    pytest.param(REVISION_RECONCILIATION, id="revision_reconciliation"),
]

STAMP = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


class CountingPass:
    """A stand-in pass body that records each run and returns a receipt."""

    def __init__(self):
        self.runs = 0

    def __call__(self, session, project_id):
        self.runs += 1
        return ("ran", project_id, self.runs)


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_request_makes_a_project_pending(session, project, watermark):
    assert watermark.pending(session, project.id) is False
    watermark.request(session, project.id, "first_change")
    assert watermark.pending(session, project.id) is True
    row = session.get(watermark.model, project.id)
    assert (row.dirty_seq, row.reconciled_seq) == (1, 0)
    assert row.last_reason == "first_change"


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_repeated_requests_coalesce_into_one_pass(session, project, watermark):
    for _ in range(3):
        watermark.request(session, project.id, "first_change")
    row = session.get(watermark.model, project.id)
    assert (row.dirty_seq, row.reconciled_seq) == (3, 0)

    body = CountingPass()
    passed = watermark.reconcile(session, project.id, body, now=STAMP)
    assert (passed.was_pending, passed.ran) == (True, True)
    assert passed.reconciled_seq == 3
    assert passed.outcome == ("ran", project.id, 1)
    # Three bumps, one reconciliation: the pass body ran exactly once.
    assert body.runs == 1
    assert watermark.pending(session, project.id) is False
    assert row.reconciled_at == STAMP


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_a_clean_watermark_runs_no_pass(session, project, watermark):
    watermark.request(session, project.id, "first_change")
    watermark.reconcile(session, project.id, CountingPass())

    body = CountingPass()
    for _ in range(5):
        passed = watermark.reconcile(session, project.id, body)
        assert (passed.was_pending, passed.ran) == (False, False)
        assert passed.outcome is None
        assert passed.reconciled_seq == 1
    assert body.runs == 0


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_reconcile_on_a_never_requested_project_is_a_no_op(session, project, watermark):
    body = CountingPass()
    passed = watermark.reconcile(session, project.id, body)
    assert (passed.was_pending, passed.ran) == (False, False)
    assert passed.reconciled_seq == 0
    assert body.runs == 0
    assert session.get(watermark.model, project.id) is None


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_a_bump_after_reconciliation_makes_the_project_pending_again(
    session, project, watermark
):
    watermark.request(session, project.id, "first_change")
    watermark.reconcile(session, project.id, CountingPass())
    assert watermark.pending(session, project.id) is False

    watermark.request(session, project.id, "second_change")
    assert watermark.pending(session, project.id) is True
    row = session.get(watermark.model, project.id)
    assert (row.dirty_seq, row.reconciled_seq) == (2, 1)


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_the_pass_advances_only_to_the_snapshot_it_observed(session, project, watermark):
    watermark.request(session, project.id, "first_change")

    def bumping_body(inner, project_id):
        # A producer's bump lands while the pass runs; it must not be swallowed
        # by the advance.
        watermark.request(inner, project_id, "arrived_during_pass")
        return "ran"

    passed = watermark.reconcile(session, project.id, bumping_body)
    assert passed.ran is True
    assert passed.reconciled_seq == 1
    assert watermark.pending(session, project.id) is True
    row = session.get(watermark.model, project.id)
    assert (row.dirty_seq, row.reconciled_seq) == (2, 1)


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_run_when_clean_runs_the_pass_without_advancing(session, project, watermark):
    # No row at all: the pass runs and no watermark row is invented.
    body = CountingPass()
    passed = watermark.reconcile(session, project.id, body, run_when_clean=True)
    assert (passed.was_pending, passed.ran) == (False, True)
    assert passed.reconciled_seq == 0
    assert body.runs == 1
    assert session.get(watermark.model, project.id) is None

    # A clean row: the pass runs and the row is untouched.
    watermark.request(session, project.id, "first_change")
    watermark.reconcile(session, project.id, CountingPass(), now=STAMP)
    passed = watermark.reconcile(session, project.id, body, run_when_clean=True)
    assert (passed.was_pending, passed.ran) == (False, True)
    assert body.runs == 2
    row = session.get(watermark.model, project.id)
    assert (row.dirty_seq, row.reconciled_seq) == (1, 1)
    assert row.reconciled_at == STAMP


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_pending_ids_drains_every_pending_project(session, watermark):
    pending = []
    clean = []
    for _ in range(3):
        p = Project(slug=f"wm-pending-{uuid4().hex}", name="p", is_synthetic=True)
        session.add(p)
        session.flush()
        watermark.request(session, p.id, "first_change")
        pending.append(p.id)
    for _ in range(2):
        p = Project(slug=f"wm-clean-{uuid4().hex}", name="c", is_synthetic=True)
        session.add(p)
        session.flush()
        watermark.request(session, p.id, "first_change")
        watermark.reconcile(session, p.id, CountingPass())
        clean.append(p.id)

    drained = watermark.pending_project_ids(session)
    assert set(pending).issubset(set(drained))
    assert set(clean).isdisjoint(set(drained))


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_request_requires_a_reason(session, project, watermark):
    with pytest.raises(ValueError, match="reason"):
        watermark.request(session, project.id, "")


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_a_request_is_pending_to_a_session_that_already_loaded_the_row(
    session, project, watermark
):
    # The upsert runs outside the ORM identity map. A session holding the clean
    # row must still see the bump without a manual refresh; the revision copy
    # failed this before the two copies became one module. ``held`` is what
    # makes the session hold the row: the identity map only weakly references
    # a clean object, so without a live reference the row is collected as soon
    # as ``request`` returns and the next ``pending`` selects it afresh, and
    # the stale path this test exists for is never taken.
    watermark.request(session, project.id, "first_change")
    watermark.reconcile(session, project.id, CountingPass())
    held = session.get(watermark.model, project.id)
    assert watermark.pending(session, project.id) is False

    watermark.request(session, project.id, "second_change")
    assert watermark.pending(session, project.id) is True
    assert (held.dirty_seq, held.reconciled_seq) == (2, 1)


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_committed_request_survives_a_process_exit(runtime_database, watermark):
    factory = runtime_database.session_factory
    with factory() as producing:
        project = Project(
            slug=f"wm-durable-{uuid4().hex}", name="Durable", is_synthetic=True
        )
        producing.add(project)
        producing.flush([project])
        watermark.request(producing, project.id, "first_change")
        project_id = project.id
        producing.commit()

    # A brand-new session, as if the process had restarted, still sees the work.
    with factory() as recovering:
        assert watermark.pending_project_ids(recovering) == [project_id]
        assert watermark.pending(recovering, project_id) is True
        passed = watermark.reconcile(recovering, project_id, CountingPass())
        assert passed.ran is True
        recovering.commit()

    with factory() as verifying:
        assert watermark.pending(verifying, project_id) is False


@pytest.mark.parametrize("watermark", WATERMARKS)
def test_a_rolled_back_producer_leaves_no_work(runtime_database, watermark):
    factory = runtime_database.session_factory
    with factory() as setup:
        project = Project(
            slug=f"wm-rollback-{uuid4().hex}", name="Rollback", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        project_id = project.id
        setup.commit()

    with factory() as aborting:
        watermark.request(aborting, project_id, "first_change")
        aborting.rollback()

    with factory() as verifying:
        assert watermark.pending(verifying, project_id) is False
        assert verifying.get(watermark.model, project_id) is None
