"""The connector-polling handler through the shared Due Work runtime (#488, #496).

ADR-0083 fixes one rule for a pull connector: the checkpoint advances only
after every change up to and including its token is durably stored under its
digest. ADR-0089 states the rest of it — a delivery the intake gate refused
permits an advance once its digest and refusal evidence are durable, and a
transient failure never does — and moves the cursor off the Due Work receipt
onto its own append-only relation.

These tests hold those rules where they are actually at risk: a worker that
stores bytes and then dies before its receipt, a scanner that fails rather than
deciding, and an operator who deletes an old receipt.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

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
from corridor.models import (
    ConnectorCheckpointAdvance,
    ConnectorCheckpointAdvanceDelivery,
    DueWorkReceipt,
    Project,
    SourceDelivery,
)
from corridor.object_storage import content_key, content_store
from corridor.source_delivery import (
    DeliveryBinding,
    DeliveryObservation,
    current_checkpoint_token,
    record_checkpoint_advance,
    record_delivery,
)


TEST_CONNECTOR = "test-fixture-v1"
SOURCE_URL = "https://example.test/shared/index"


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


class RecordingConnector:
    """One in-memory PullConnector that counts what the runtime asked of it."""

    def __init__(self, items: dict[str, bytes], *, suffix: str = ".pdf"):
        self.items = dict(items)
        self.suffix = suffix
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
                    name=f"{item_id}{self.suffix}",
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
        {
            "item-a": b"%PDF-1.4 first delivery",
            "item-b": b"%PDF-1.4 second delivery",
        }
    )
    monkeypatch.setattr(
        connector_polling,
        "CONNECTOR_FACTORIES",
        {**connector_polling.CONNECTOR_FACTORIES, TEST_CONNECTOR: lambda scope: connector},
    )
    return connector


def _seed_project(factory, now, *, channel="shared-files"):
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
                channel=channel,
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
    assert _stored(b"%PDF-1.4 first delivery") == b"%PDF-1.4 first delivery"
    assert _stored(b"%PDF-1.4 second delivery") == b"%PDF-1.4 second delivery"
    assert body["dispositions"] == {"stored": 2}
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

    installed_connector.items["item-c"] = b"%PDF-1.4 third delivery"
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


def test_a_crash_before_the_receipt_keeps_the_advance_it_durably_made(
    runtime_database, installed_connector
):
    """The restart drill, as ADR-0089 leaves it.

    #488 kept the token on the completed receipt, so a worker that stored every
    change and then died lost the cursor with the receipt it never wrote and
    the recovery re-listed the whole location. The cursor now lives with the
    connector configuration and is recorded as its own act, so the crash costs
    the receipt and nothing else — and the recovered attempt resumes from what
    was durably taken instead of taking it all again.
    """

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

    crashed = connector_polling.execute_connector_polling(
        factory,
        schedule_id=schedule_id,
        clock=ControlledClock(now),
        run_identity="due-attempt:crashed",
        connector=installed_connector,
    )
    assert crashed["changes_taken"] == 2
    with factory() as reading:
        assert current_checkpoint_token(reading, schedule_id) == "item-b"

    recover_at = claim.lease_expires_at + timedelta(seconds=1)
    recovered = run_due_work_once(
        factory, clock=ControlledClock(recover_at), owner="runtime:recovery"
    )

    assert recovered is not None
    assert recovered.execution_outcome == "completed"
    assert recovered.attempt_id != claim.attempt_id
    assert recovered.handler_result["cursor"] == "item-b"
    assert recovered.handler_result["changes_taken"] == 0
    assert installed_connector.fetches == ["item-a", "item-b"]
    assert _stored(b"%PDF-1.4 first delivery") == b"%PDF-1.4 first delivery"
    with factory() as reading:
        assert current_checkpoint_token(reading, schedule_id) == "item-b"


def test_a_crash_before_the_advance_leaves_the_cursor_where_it_was(
    runtime_database, installed_connector
):
    """Storing bytes is not advancing: the advance is its own durable act."""

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 9, 30, tzinfo=timezone.utc)
    _, schedule_id = _seed_project(factory, now)

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    class DiesBeforeCheckpoint:
        def __init__(self, inner):
            self._inner = inner

        def list_changes(self, cursor=None):
            return self._inner.list_changes(cursor)

        def fetch_version(self, item_id, version_id):
            return self._inner.fetch_version(item_id, version_id)

        def get_metadata(self, item_id):
            return self._inner.get_metadata(item_id)

        def checkpoint(self, token):
            raise RuntimeError("the worker died before it could checkpoint")

    with pytest.raises(RuntimeError, match="died before it could checkpoint"):
        connector_polling.execute_connector_polling(
            factory,
            schedule_id=schedule_id,
            clock=ControlledClock(now),
            run_identity="due-attempt:half-way",
            connector=DiesBeforeCheckpoint(installed_connector),
        )

    with factory() as reading:
        assert current_checkpoint_token(reading, schedule_id) is None
        # The deliveries it did take are still recorded: the bytes are in the
        # store and the ledger says so, which is what makes the next pass a
        # duplicate rather than a second taking.
        assert reading.scalars(
            select(SourceDelivery.disposition).order_by(SourceDelivery.id)
        ).all() == ["stored", "stored"]


def test_deleting_an_old_receipt_never_moves_the_external_cursor(
    runtime_database, installed_connector
):
    """#488's cursor was a derived property of receipt retention (ADR-0089).

    Retention is not identity. A receipt sweep, a retention-policy change, or
    an ordinary cleanup would have reset a live connector's cursor and re-listed
    an entire location; the cursor now lives with the configuration, which
    nothing sweeps.
    """

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
    _, schedule_id = _seed_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    with factory() as sweeping:
        # A receipt is immutable to the application, so the sweep is modelled
        # the only way one can happen: the disposal path, with the append-only
        # guard lifted.
        sweeping.execute(text("set local session_replication_role = replica"))
        swept = sweeping.execute(
            delete(DueWorkReceipt).where(
                DueWorkReceipt.handler_key == HANDLER_CONNECTOR_POLLING
            )
        ).rowcount
        sweeping.commit()
    assert swept > 0

    with factory() as reading:
        assert current_checkpoint_token(reading, schedule_id) == "item-b"

    later = now + timedelta(hours=1)
    with factory() as ticking:
        enqueue_due_work(ticking, now=later)
        ticking.commit()
    after_sweep = run_due_work_once(
        factory, clock=ControlledClock(later), owner="runtime:polling-worker"
    )

    assert after_sweep.handler_result["cursor"] == "item-b"
    assert after_sweep.handler_result["changes_taken"] == 0
    assert installed_connector.fetches == ["item-a", "item-b"]


def test_a_refused_delivery_is_recorded_and_the_cursor_advances_past_it(
    runtime_database, installed_connector, monkeypatch
):
    """ADR-0089's refused case, end to end and durable.

    ADR-0083's rule read literally stalls a location forever on one object the
    gate will never admit. The advance is safe here because the digest and the
    reason are in the ledger: the record still answers what arrived and why it
    was refused, without the bytes.
    """

    from corridor import intake_hardening

    class Refusing:
        def scan(self, body, filename):
            if b"second delivery" in body:
                return intake_hardening.ScanResult(
                    is_clean=False, threat_name="Test", reason="marked bytes"
                )
            return intake_hardening.ScanResult(is_clean=True)

    monkeypatch.setattr(intake_hardening, "_GLOBAL_SCANNER", Refusing())
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
    _, schedule_id = _seed_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    assert result.execution_outcome == "completed"
    assert result.handler_result["dispositions"] == {
        "stored": 1,
        "terminally_refused": 1,
    }
    assert result.handler_result["advanced"] is True
    with factory() as reading:
        assert current_checkpoint_token(reading, schedule_id) == "item-b"
        refused = reading.scalars(
            select(SourceDelivery).where(
                SourceDelivery.disposition == "terminally_refused"
            )
        ).one()
        assert refused.transport == "pull"
        assert refused.external_identity == "item-b"
        assert refused.content_sha256 == sha256(b"%PDF-1.4 second delivery").hexdigest()
        assert refused.refusal_reason.startswith("malware_detected:")
        assert refused.bytes_reference == ""
        # The advance names the deliveries it covered, refusal included.
        covered = reading.scalars(
            select(ConnectorCheckpointAdvanceDelivery.delivery_id)
        ).all()
        assert refused.id in covered


def test_a_transient_scanner_failure_never_advances_the_cursor(
    runtime_database, installed_connector, monkeypatch
):
    """The other half of the rule, and the one that loses a source revision.

    A cursor that fails to advance is visible: the connector re-lists and
    somebody notices. A cursor that advances past a change nobody stored is
    silent, so a scanner that failed rather than deciding must never move it.
    """

    from corridor import intake_hardening

    class Failing:
        def scan(self, body, filename):
            raise RuntimeError("scanner unavailable")

    monkeypatch.setattr(intake_hardening, "_GLOBAL_SCANNER", Failing())
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)
    _, schedule_id = _seed_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    assert result.execution_outcome == "completed"
    assert result.handler_result["dispositions"] == {"transient_failure": 2}
    assert result.handler_result["advanced"] is False
    assert result.handler_result["blocked_by"] == ["item-a", "item-b"]
    assert result.handler_result["health"] == "polling_attention_required"
    assert installed_connector.checkpoints == []
    with factory() as reading:
        assert current_checkpoint_token(reading, schedule_id) is None
        assert reading.scalar(select(func.count(ConnectorCheckpointAdvance.id))) == 0
        failed = reading.scalars(select(SourceDelivery)).all()
        assert {row.disposition for row in failed} == {"transient_failure"}
        assert all(row.refusal_reason.startswith("scan_failed:") for row in failed)


def test_the_database_refuses_an_advance_past_a_transient_failure(
    runtime_database, installed_connector, monkeypatch
):
    """The rule is enforced where the coverage is written, not remembered.

    ADR-0089 asks for one rule in one place. The pass above declines to advance;
    this proves the database would refuse the coverage even if a writer tried.
    """

    from corridor import intake_hardening

    class Failing:
        def scan(self, body, filename):
            raise RuntimeError("scanner unavailable")

    monkeypatch.setattr(intake_hardening, "_GLOBAL_SCANNER", Failing())
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc)
    project_id, schedule_id = _seed_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    with factory() as writing:
        failed = writing.scalars(select(SourceDelivery.id)).first()
        with pytest.raises(IntegrityError, match="transient_failure"):
            record_checkpoint_advance(
                writing,
                project_id=project_id,
                schedule_id=schedule_id,
                configuration_identity=TEST_CONNECTOR,
                configuration_version="connector-polling-v1",
                channel="shared-files",
                checkpoint_token="item-b",
                service_identity="tests",
                run_identity="tests:forced",
                delivery_ids=(failed,),
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


def test_a_quarantined_delivery_advances_only_where_its_bytes_are_held(
    runtime_database, installed_connector
):
    """The disposition #490's byte-hold carries, and the rule the database keeps.

    Corridor's approved intake policy refuses hostile bytes outright today
    rather than retaining them, so no runtime path produces ``quarantined``
    yet. The ledger carries it because ADR-0089's checkpoint rule turns on it:
    an advance passes a held delivery only where the quarantine reference and
    the refusal evidence are both durable, and the database is where that is
    settled.
    """

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc)
    project_id, schedule_id = _seed_project(factory, now)

    with factory() as writing:
        binding = DeliveryBinding(
            customer="acme-utilities",
            project_id=project_id,
            project_slug=writing.get(Project, project_id).slug,
            transport="pull",
            channel="shared-files",
            configuration_identity=TEST_CONNECTOR,
        )
        held = record_delivery(
            writing,
            binding,
            DeliveryObservation(
                external_identity="item-held",
                external_version="v1",
                content_digest=sha256(b"held bytes").hexdigest(),
                bytes_reference="quarantine/item-held",
            ),
            disposition="quarantined",
            service_identity="tests",
            run_identity="tests:quarantine",
            refusal_reason="malware_detected: held for inspection",
        )
        unheld = record_delivery(
            writing,
            binding,
            DeliveryObservation(
                external_identity="item-unheld",
                external_version="v1",
                content_digest=sha256(b"unheld bytes").hexdigest(),
            ),
            disposition="quarantined",
            service_identity="tests",
            run_identity="tests:quarantine",
            refusal_reason="malware_detected: nothing kept",
        )
        writing.commit()
        held_id, unheld_id = held.delivery_id, unheld.delivery_id

    with factory() as advancing:
        record_checkpoint_advance(
            advancing,
            project_id=project_id,
            schedule_id=schedule_id,
            configuration_identity=TEST_CONNECTOR,
            configuration_version="connector-polling-v1",
            channel="shared-files",
            checkpoint_token="item-held",
            service_identity="tests",
            run_identity="tests:quarantine",
            delivery_ids=(held_id,),
        )
        advancing.commit()

    with factory() as refusing:
        with pytest.raises(IntegrityError, match="missing_quarantine_reference"):
            record_checkpoint_advance(
                refusing,
                project_id=project_id,
                schedule_id=schedule_id,
                configuration_identity=TEST_CONNECTOR,
                configuration_version="connector-polling-v1",
                channel="shared-files",
                checkpoint_token="item-unheld",
                service_identity="tests",
                run_identity="tests:quarantine",
                delivery_ids=(unheld_id,),
            )

    with factory() as reading:
        assert current_checkpoint_token(reading, schedule_id) == "item-held"


# --- The delivery a poll registered, by channel kind (ADR-0089) -------------


def _workbook_bytes() -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Utility Conflict ID", "Utility Owner"])
    book.active.append(["UC-1", "Source Utility"])
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def _message_bytes() -> bytes:
    from email.message import EmailMessage
    from email.policy import SMTP

    message = EmailMessage(policy=SMTP)
    message["From"] = "utility@example.test"
    message["To"] = "project@example.test"
    message["Message-ID"] = "<polled@example.test>"
    message["Subject"] = "Polled from the shared mailbox"
    message.set_content("We will finish in October.")
    return message.as_bytes()


@pytest.fixture
def registering_connector(monkeypatch, tmp_path):
    """One connector whose delivered bytes a registration can actually read."""

    from corridor.config import settings

    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))

    def install(items, *, suffix):
        connector = RecordingConnector(items, suffix=suffix)
        monkeypatch.setattr(
            connector_polling,
            "CONNECTOR_FACTORIES",
            {
                **connector_polling.CONNECTOR_FACTORIES,
                TEST_CONNECTOR: lambda scope: connector,
            },
        )
        return connector

    return install


def test_a_stored_delivery_ends_as_a_registered_document(
    runtime_database, registering_connector
):
    """The step the pull path never took (#688).

    ``execute_connector_polling`` fetched, ran the byte gate, stored the bytes,
    recorded the delivery and advanced the checkpoint — and registered no
    Document, because it never passed ``on_envelope_stored``. The one caller
    that did register was the offline replay command, so a production Box or
    TxDOT poll took delivery of sources the product could not show anybody, and
    the only thing a test could assert about a live poll was its cursor.
    """

    from corridor.models import Document

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
    registering_connector({"item-book": _workbook_bytes()}, suffix=".xlsx")
    project_id, _schedule_id = _seed_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    assert result.execution_outcome == "completed"
    assert result.handler_result["dispositions"] == {"stored": 1}
    with factory() as reading:
        delivery = reading.scalars(select(SourceDelivery)).one()
        document = reading.scalars(
            select(Document).where(Document.project_id == project_id)
        ).one()
        # The Document names the delivery that carried it, in the project the
        # connector configuration bound — never a project the bytes named.
        assert document.source_delivery_id == delivery.id
        assert document.sha256 == delivery.content_sha256
        assert document.filename == "item-book.xlsx"
        # Nobody declared what kind of source this is, and the machine does not
        # invent one (ADR-0007): it registers the source and leaves the kind
        # unresolved rather than guessing it from the bytes.
        assert document.doc_type == "other"


def test_a_delivered_message_is_registered_as_a_message_not_a_document(
    runtime_database, registering_connector
):
    """The channel kind decides which registration a delivery gets.

    A shared-mailbox delivery is a message with a thread, and registering its
    raw MIME as an ordinary Document would lose both. The consumer reads the
    channel the delivery arrived on, which is server-owned configuration, and
    never anything inside the bytes.
    """

    from corridor.models import InboundMessage

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 19, 0, tzinfo=timezone.utc)
    registering_connector({"item-mail": _message_bytes()}, suffix=".eml")
    project_id, _schedule_id = _seed_project(
        factory, now, channel="m365-shared-mailbox-v1"
    )
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:polling-worker"
    )

    assert result.execution_outcome == "completed"
    with factory() as reading:
        delivery = reading.scalars(select(SourceDelivery)).one()
        inbound = reading.scalars(select(InboundMessage)).one()
        assert inbound.project_id == project_id
        assert inbound.push_delivery_id == delivery.id
        assert inbound.raw_sha256 == delivery.content_sha256
        assert inbound.route_evidence_json["boundary"] == "pull_configuration"
        assert inbound.headers_json["message_id"] == "<polled@example.test>"


def test_a_registration_that_fails_keeps_the_delivery_and_the_cursor(
    runtime_database, registering_connector, monkeypatch
):
    """Registration is its own short transaction after the delivery commits.

    The order is the property: the delivery is committed first, so a failing
    registration can never un-record what arrived, and the cursor is recorded
    last, so the next pass re-lists the same change and registers it again
    against the delivery already in the ledger. A pass that swallowed the
    failure and advanced instead would leave bytes nobody can see and no
    record that anything was missing.
    """

    from corridor import delivery_registration

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
    connector = registering_connector({"item-book": _workbook_bytes()}, suffix=".xlsx")
    _project_id, schedule_id = _seed_project(factory, now)
    monkeypatch.setattr(
        delivery_registration,
        "register_delivered_source",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("registrar down")),
    )

    with pytest.raises(RuntimeError, match="registrar down"):
        connector_polling.execute_connector_polling(
            factory,
            schedule_id=schedule_id,
            clock=ControlledClock(now),
            run_identity="due-attempt:registration",
            connector=connector,
        )

    with factory() as reading:
        assert current_checkpoint_token(reading, schedule_id) is None
        assert reading.scalars(select(SourceDelivery.disposition)).all() == ["stored"]
        assert connector.checkpoints == []
