"""Conformance tests for PullConnector contract and SourceEnvelope (#496)."""

import hashlib
import pytest

from corridor.connectors import (
    BoxPullConnector,
    ChangeItem,
    PullConnector,
    SourceEnvelope,
    build_delivery_identity,
    build_idempotency_key,
    sync_pull_connector,
)
from corridor.connectors import pull_connector
from corridor.location_discovery import BoxSharedFile
from corridor.object_storage import content_key, content_store


class FakePullConnector:
    """In-memory test connector implementing PullConnector."""

    def __init__(self, items: dict[str, dict[str, bytes]]) -> None:
        # item_id -> version_id -> bytes
        self.items = items
        self.checkpoints: list[str] = []

    def list_changes(self, cursor: str | None = None) -> tuple[tuple[ChangeItem, ...], str]:
        all_ids = sorted(self.items.keys())
        include = cursor is None
        results: list[ChangeItem] = []
        for i_id in all_ids:
            if not include:
                if i_id == cursor:
                    include = True
                continue
            versions = sorted(self.items[i_id].keys())
            latest_ver = versions[-1]
            results.append(
                ChangeItem(
                    item_id=i_id,
                    version_id=latest_ver,
                    name=f"file_{i_id}.pdf",
                    original_timestamps={"modified": "2026-09-02T12:00:00Z"},
                    metadata={"test_meta": True},
                )
            )
        token = all_ids[-1] if all_ids else (cursor or "")
        return tuple(results), token

    def fetch_version(self, item_id: str, version_id: str) -> bytes:
        return self.items[item_id][version_id]

    def get_metadata(self, item_id: str) -> dict[str, str]:
        return {"item_id": item_id, "name": f"file_{item_id}.pdf"}

    def checkpoint(self, token: str) -> None:
        self.checkpoints.append(token)


def test_pull_connector_conformance() -> None:
    """Verify that FakePullConnector adheres to PullConnector protocol."""
    connector: PullConnector = FakePullConnector(
        {
            "item-1": {"v1": b"%PDF-1.4 item 1"},
            "item-2": {"v1": b"%PDF-1.4 item 2"},
        }
    )

    items, token = connector.list_changes(None)
    assert len(items) == 2
    assert token == "item-2"

    bytes_1 = connector.fetch_version("item-1", "v1")
    assert bytes_1 == b"%PDF-1.4 item 1"

    meta_1 = connector.get_metadata("item-1")
    assert meta_1["item_id"] == "item-1"

    connector.checkpoint(token)
    assert getattr(connector, "checkpoints") == ["item-2"]


def test_source_envelope_attributes_and_idempotency() -> None:
    """SourceEnvelope carries all required fields and deterministic identities."""
    content = b"%PDF-1.5 test document bytes"
    digest = hashlib.sha256(content).hexdigest()

    deliv_id = build_delivery_identity(
        customer="cust-pilot",
        project="proj-101",
        channel="box",
        external_identity="file-987",
        external_version="v2",
    )
    idem_key = build_idempotency_key(deliv_id, digest)

    envelope = SourceEnvelope(
        customer="cust-pilot",
        project="proj-101",
        channel="box",
        external_identity="file-987",
        external_version="v2",
        original_timestamps={"created": "2026-09-01T00:00:00Z"},
        content_digest=digest,
        bytes_reference=f"{digest[:2]}/{digest}.pdf",
        metadata={"author": "TxDOT"},
        delivery_identity=deliv_id,
        idempotency_key=idem_key,
    )

    data = envelope.as_dict()
    assert data["customer"] == "cust-pilot"
    assert data["content_digest"] == digest
    assert data["delivery_identity"] == deliv_id
    assert data["idempotency_key"] == idem_key

    # Rebuilding with identical inputs yields identical keys
    deliv_id_2 = build_delivery_identity(
        customer="cust-pilot",
        project="proj-101",
        channel="box",
        external_identity="file-987",
        external_version="v2",
    )
    assert deliv_id == deliv_id_2
    assert idem_key == build_idempotency_key(deliv_id_2, digest)


def test_sync_pull_connector_crash_safety(isolated_content_store) -> None:
    """Checkpoint never advances if storage fails midway through a sync."""
    connector = FakePullConnector(
        {
            "doc-1": {"v1": b"%PDF-1.4 file 1"},
            "doc-2": {"v1": b"%PDF-1.4 file 2"},
        }
    )

    # Successful sync: all items stored, checkpoint advances
    result = sync_pull_connector(
        connector,
        customer="cust-test",
        project="proj-test",
        channel="test_pull",
    )

    assert len(result.envelopes) == 2
    assert result.advanced is True
    assert [record.disposition for record in result.records] == ["stored", "stored"]
    assert connector.checkpoints == ["doc-2"]
    for env in result.envelopes:
        # Verify stored in content store
        assert content_store().exists(env.bytes_reference)

    # Simulated crash: fetch failure on second item prevents checkpoint
    class FailingFetchConnector(FakePullConnector):
        def fetch_version(self, item_id: str, version_id: str) -> bytes:
            if item_id == "doc-4":
                raise RuntimeError("simulated crash during fetch")
            return super().fetch_version(item_id, version_id)

    failing_connector = FailingFetchConnector(
        {
            "doc-3": {"v1": b"%PDF-1.4 file 3"},
            "doc-4": {"v1": b"%PDF-1.4 file 4"},
        }
    )

    with pytest.raises(RuntimeError, match="simulated crash during fetch"):
        sync_pull_connector(
            failing_connector,
            customer="cust-test",
            project="proj-test",
            channel="test_pull",
        )

    # Checkpoint MUST NOT have advanced
    assert failing_connector.checkpoints == []


def test_box_pull_connector_implements_protocol() -> None:
    """BoxPullConnector satisfies the PullConnector contract."""
    connector = BoxPullConnector()
    shared = BoxSharedFile(
        shared_name="txdot_sample",
        item_id=123456,
        filename="Utility_Matrix_Nov2025.zip",
        archive_url="https://app.box.com/download/123456",
    )
    connector.register_shared_file(shared)

    items, token = connector.list_changes(None)
    assert len(items) == 1
    assert items[0].item_id == "123456"
    assert items[0].name == "Utility_Matrix_Nov2025.zip"
    assert token == "123456"

    meta = connector.get_metadata("123456")
    assert meta["shared_name"] == "txdot_sample"

    connector.checkpoint(token)
    assert connector.current_checkpoint == "123456"


class RecordingLedger:
    """A durable-enough ledger for the sync loop: it hands back identities."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, item, *, disposition, content_digest, bytes_reference, refusal_reason):
        self.records.append(
            {
                "item_id": item.item_id,
                "disposition": disposition,
                "content_digest": content_digest,
                "bytes_reference": bytes_reference,
                "refusal_reason": refusal_reason,
            }
        )
        return len(self.records)


class RefusingScanner:
    """The #490 malware seam, refusing exactly one delivery's bytes."""

    def __init__(self, marker: bytes) -> None:
        self.marker = marker

    def scan(self, body: bytes, filename: str):
        from corridor.intake_hardening import ScanResult

        if self.marker in body:
            return ScanResult(
                is_clean=False, threat_name="Test-Signature", reason="marked bytes"
            )
        return ScanResult(is_clean=True)


class FailingScanner:
    """A scanner that fails rather than deciding: one attempt, not one delivery."""

    def scan(self, body: bytes, filename: str):
        raise RuntimeError("scanner unavailable")


def _gated_connector():
    return FakePullConnector(
        {
            "doc-1": {"v1": b"%PDF-1.4 clean file"},
            "doc-2": {"v1": b"%PDF-1.4 REFUSE ME file"},
        }
    )


def test_a_refusal_the_ledger_recorded_lets_the_checkpoint_advance_past_it(
    isolated_content_store, monkeypatch
) -> None:
    """ADR-0089's refused case: the evidence is what makes the advance safe.

    Read literally, ADR-0083's rule stalls a location forever on one poisoned
    object. The digest and the reason, durably recorded, are what let the
    cursor move without losing the answer to what arrived and why it was
    refused.
    """

    from corridor import intake_hardening

    monkeypatch.setattr(
        intake_hardening, "_GLOBAL_SCANNER", RefusingScanner(b"REFUSE ME")
    )
    connector = _gated_connector()
    ledger = RecordingLedger()

    result = sync_pull_connector(
        connector,
        customer="cust-test",
        project="proj-test",
        channel="test_pull",
        ledger=ledger,
    )

    assert [record.disposition for record in result.records] == [
        "stored",
        "terminally_refused",
    ]
    refused = result.records[1]
    assert refused.content_digest == hashlib.sha256(
        b"%PDF-1.4 REFUSE ME file"
    ).hexdigest()
    assert refused.refusal_reason.startswith("malware_detected:")
    assert refused.delivery_id is not None
    assert result.advanced is True
    assert connector.checkpoints == ["doc-2"]
    # The refused bytes are not in the store; the record of them is.
    assert not content_store().exists(content_key(refused.content_digest, ".pdf"))


def test_a_refusal_nobody_recorded_holds_the_checkpoint_where_it_was(
    isolated_content_store, monkeypatch
) -> None:
    """No ledger, no evidence, no advance: the condition is the record."""

    from corridor import intake_hardening

    monkeypatch.setattr(
        intake_hardening, "_GLOBAL_SCANNER", RefusingScanner(b"REFUSE ME")
    )
    connector = _gated_connector()

    result = sync_pull_connector(
        connector,
        customer="cust-test",
        project="proj-test",
        channel="test_pull",
    )

    assert result.advanced is False
    assert result.blocked_by == ("doc-2",)
    assert connector.checkpoints == []


def test_a_transient_scanner_failure_never_advances_the_checkpoint(
    isolated_content_store, monkeypatch
) -> None:
    """A scanner that failed said nothing about the delivery (ADR-0089).

    The failure is recorded — an intake failure is an operational metric, not
    an absence somebody has to notice — and the cursor stays exactly where it
    was, so the next pass re-lists the change instead of dropping it.
    """

    from corridor import intake_hardening

    monkeypatch.setattr(intake_hardening, "_GLOBAL_SCANNER", FailingScanner())
    connector = _gated_connector()
    ledger = RecordingLedger()

    result = sync_pull_connector(
        connector,
        customer="cust-test",
        project="proj-test",
        channel="test_pull",
        ledger=ledger,
    )

    assert [record.disposition for record in result.records] == [
        "transient_failure",
        "transient_failure",
    ]
    assert all(
        record.refusal_reason.startswith("scan_failed:") for record in result.records
    )
    assert result.advanced is False
    assert result.blocked_by == ("doc-1", "doc-2")
    assert connector.checkpoints == []


def test_a_transient_storage_failure_never_advances_the_checkpoint(
    isolated_content_store, monkeypatch
) -> None:
    """An object store that rejected the write is the same kind of fact."""

    def refuse_write(data, *, sha256, suffix):
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(pull_connector, "store_bytes", refuse_write)
    connector = FakePullConnector({"doc-1": {"v1": b"%PDF-1.4 clean file"}})
    ledger = RecordingLedger()

    result = sync_pull_connector(
        connector,
        customer="cust-test",
        project="proj-test",
        channel="test_pull",
        ledger=ledger,
    )

    assert [record.disposition for record in result.records] == ["transient_failure"]
    assert result.records[0].refusal_reason.startswith("store_failed:")
    assert result.advanced is False
    assert connector.checkpoints == []
