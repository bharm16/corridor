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
from corridor.location_discovery import BoxSharedFile
from corridor.object_storage import content_store


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
    envelopes = sync_pull_connector(
        connector,
        customer="cust-test",
        project="proj-test",
        channel="test_pull",
    )

    assert len(envelopes) == 2
    assert connector.checkpoints == ["doc-2"]
    for env in envelopes:
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
