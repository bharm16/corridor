"""The connector-polling handler through the shared Due Work runtime (#488, #496).

ADR-0083 fixes one rule for a pull connector: the checkpoint advances only
after every change up to and including its token is durably stored under its
digest. These tests hold that rule where it is actually at risk — a worker that
stores bytes and then dies before its receipt — and prove the next pass resumes
from the token the last completed receipt retained, taking each change again
without producing a second object.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest

from corridor import connector_polling
from corridor.connectors.pull_connector import ChangeItem
from corridor.due_work import (
    HANDLER_CONNECTOR_POLLING,
    ConnectorPollingDeclaration,
    DueWorkRefusal,
    claim_due_work,
    configure_connector_polling,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.models import Project
from corridor.object_storage import content_key, content_store


TEST_CONNECTOR = "test-fixture-v1"
SOURCE_URL = "https://example.test/shared/index"


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


class RecordingConnector:
    """One in-memory PullConnector that counts what the runtime asked of it."""

    def __init__(self, items: dict[str, bytes]):
        self.items = dict(items)
        self.fetches: list[str] = []
        self.checkpoints: list[str] = []

    def list_changes(self, cursor=None):
        ordered = sorted(self.items)
        include = cursor is None
        selected = []
        for item_id in ordered:
            if not include:
                if item_id == cursor:
                    include = True
                continue
            selected.append(
                ChangeItem(
                    item_id=item_id,
                    version_id="v1",
                    name=f"{item_id}.pdf",
                    original_timestamps={"modified": "2026-09-03T00:00:00Z"},
                    metadata={},
                )
            )
        return tuple(selected), (ordered[-1] if ordered else (cursor or ""))

    def fetch_version(self, item_id, version_id):
        self.fetches.append(item_id)
        return self.items[item_id]

    def get_metadata(self, item_id):
        return {"item_id": item_id}

    def checkpoint(self, token):
        self.checkpoints.append(token)


@pytest.fixture
def installed_connector(monkeypatch):
    """Install one test connector beside the shipped ones for this test only.

    The registry is server-owned on purpose: a persisted schedule may name only
    a built-in adapter. Driving the shipped Box adapter here would mean an
    outbound request, so the fixture registers a connector rather than
    loosening the rule the handler depends on.
    """

    connector = RecordingConnector(
        {"item-a": b"first delivery", "item-b": b"second delivery"}
    )
    monkeypatch.setattr(
        connector_polling,
        "CONNECTOR_FACTORIES",
        {**connector_polling.CONNECTOR_FACTORIES, TEST_CONNECTOR: lambda scope: connector},
    )
    return connector


def _seed_project(factory, now):
    with factory() as setup:
        project = Project(
            slug=f"connector-polling-{uuid4().hex[:8]}",
            name="Connector Polling",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        schedule = configure_connector_polling(
            setup,
            ConnectorPollingDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="connector-polling-v1",
                customer="acme-utilities",
                channel="shared-files",
                connector_identity=TEST_CONNECTOR,
                source_url=SOURCE_URL,
                starts_at=now.replace(minute=0, second=0, microsecond=0),
            ),
            now=now,
        )
        ids = (project.id, schedule.id)
        setup.commit()
    return ids


def _stored(payload: bytes) -> bytes:
    digest = sha256(payload).hexdigest()
    return content_store().get(content_key(digest, ".pdf"), sha256=digest)


def test_polling_stores_every_change_and_retains_its_checkpoint(
    runtime_database, installed_connector
):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    project_id, schedule_id = _seed_project(factory, now)

    with factory() as ticking:
        [occurrence] = enqueue_due_work(ticking, now=now)
        assert occurrence.scheduled_job_id == schedule_id
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_key == HANDLER_CONNECTOR_POLLING
    body = result.handler_result
    assert body["schema_version"] == "connector-polling-result-v1"
    assert body["changes_taken"] == 2
    assert body["cursor"] == ""
    assert body["checkpoint_token"] == "item-b"
    assert body["advanced"] is True
    assert body["channel"] == "shared-files"
    assert result.safe_next_step == "none"
    # The bytes are in the content-addressed store before the receipt exists.
    assert _stored(b"first delivery") == b"first delivery"
    assert _stored(b"second delivery") == b"second delivery"
    assert installed_connector.checkpoints == ["item-b"]

    with factory() as verify:
        status = due_work_status(verify, project_id=project_id)
        assert status["receipts"][0]["handler"] == HANDLER_CONNECTOR_POLLING


def test_the_next_pass_resumes_from_the_retained_checkpoint(
    runtime_database, installed_connector
):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    _seed_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    installed_connector.items["item-c"] = b"third delivery"
    later = now + timedelta(hours=1)
    with factory() as ticking:
        enqueue_due_work(ticking, now=later)
        ticking.commit()
    second = run_due_work_once(
        factory, clock=ControlledClock(later), owner="runtime:polling-worker"
    )

    assert second.execution_outcome == "completed"
    assert second.handler_result["cursor"] == "item-b"
    assert second.handler_result["checkpoint_token"] == "item-c"
    assert second.handler_result["changes_taken"] == 1
    # The already-delivered items were not fetched a second time.
    assert installed_connector.fetches == ["item-a", "item-b", "item-c"]


def test_a_crash_before_the_receipt_leaves_the_checkpoint_where_it_was(
    runtime_database, installed_connector
):
    """The restart drill: stored bytes, no receipt, no advanced checkpoint."""

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    _, schedule_id = _seed_project(factory, now)

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    with factory() as claiming:
        claim = claim_due_work(claiming, now=now, owner="runtime:crashed")
        assert claim is not None
        claiming.commit()

    # The crashed worker durably stored both changes and checkpointed its own
    # connector, but never finalized, so nothing retained the token.
    crashed = connector_polling.execute_connector_polling(
        factory,
        schedule_id=schedule_id,
        clock=ControlledClock(now),
        connector=installed_connector,
    )
    assert crashed["changes_taken"] == 2
    with factory() as reading:
        assert connector_polling.last_checkpoint_token(reading, schedule_id) is None

    recover_at = claim.lease_expires_at + timedelta(seconds=1)
    recovered = run_due_work_once(
        factory, clock=ControlledClock(recover_at), owner="runtime:recovery"
    )

    assert recovered is not None
    assert recovered.execution_outcome == "completed"
    assert recovered.attempt_id != claim.attempt_id
    # It re-listed from the same place and re-stored content-addressed bytes.
    assert recovered.handler_result["cursor"] == ""
    assert recovered.handler_result["changes_taken"] == 2
    assert recovered.handler_result["checkpoint_token"] == "item-b"
    assert installed_connector.fetches == ["item-a", "item-b", "item-a", "item-b"]
    assert _stored(b"first delivery") == b"first delivery"
    with factory() as reading:
        assert (
            connector_polling.last_checkpoint_token(reading, schedule_id) == "item-b"
        )


def test_an_idle_pass_takes_nothing_and_keeps_its_token(
    runtime_database, installed_connector
):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    _seed_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    later = now + timedelta(hours=1)
    with factory() as ticking:
        enqueue_due_work(ticking, now=later)
        ticking.commit()
    idle = run_due_work_once(
        factory, clock=ControlledClock(later), owner="runtime:polling-worker"
    )

    assert idle.execution_outcome == "completed"
    assert idle.handler_result["changes_taken"] == 0
    assert idle.handler_result["advanced"] is False
    assert idle.handler_result["checkpoint_token"] == "item-b"
    assert idle.handler_result["health"] == "healthy"


def test_gate7_refuses_an_uninstalled_connector_and_an_unsafe_location(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"polling-gate7-{uuid4().hex[:8]}",
            name="Polling Gate 7",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        base = ConnectorPollingDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="connector-polling-v1",
            customer="acme-utilities",
            channel="shared-files",
            connector_identity="txdot-rid-box-v1",
            source_url=SOURCE_URL,
            starts_at=now,
        )
        with pytest.raises(DueWorkRefusal, match="connector is not installed"):
            configure_connector_polling(
                setup, replace(base, connector_identity="invented-v1"), now=now
            )
        with pytest.raises(DueWorkRefusal, match="source url"):
            configure_connector_polling(
                setup, replace(base, source_url="http://example.test/x"), now=now
            )
        with pytest.raises(DueWorkRefusal, match="resource declaration is invalid"):
            configure_connector_polling(
                setup, replace(base, notification_budget=1), now=now
            )
