"""Real pull adapters keep exact versions and withhold incomplete checkpoints."""

import pytest

from corridor.connectors.box import BoxPullConnector
from corridor.connectors.microsoft365 import (
    GraphLocation, Microsoft365PullConnector, RecordedGraphTransport,
)
from corridor.location_discovery import BoxSharedFile


def box_snapshot(item_id="123", body=b"first version"):
    connector = BoxPullConnector(fetcher=lambda _: body)
    connector.register_shared_file(BoxSharedFile(
        shared_name="project", item_id=int(item_id), filename="matrix.zip",
        archive_url="https://example.test/download",
    ))
    return connector


@pytest.fixture(params=("box", "graph"))
def connector(request):
    if request.param == "box":
        return box_snapshot()
    location = GraphLocation("tenant", "library", "drive")
    return Microsoft365PullConnector(location, RecordedGraphTransport(
        pages={location.delta_url: {
            "value": [{"id": "123", "name": "matrix.zip", "file": {}, "eTag": "v1"}],
            "@odata.deltaLink": location.delta_url + "?$deltatoken=one",
        }},
        versions={("123", "v1"): b"first version"},
    ))


def test_listed_version_is_exact_and_required_before_checkpoint(connector):
    (item,), token = connector.list_changes(None)
    with pytest.raises(ValueError, match="checkpoint"):
        connector.checkpoint(token)
    with pytest.raises(ValueError, match="version"):
        connector.fetch_version(item.item_id, "never-listed")
    assert connector.fetch_version(item.item_id, item.version_id) == b"first version"
    connector.checkpoint(token)
    assert connector.current_checkpoint == token


@pytest.mark.parametrize("next_id,next_body", (("124", b"replacement"), ("123", b"changed")))
def test_box_resume_observes_replacement_or_changed_bytes(next_id, next_body):
    previous = box_snapshot()
    (first,), cursor = previous.list_changes(None)
    previous.fetch_version(first.item_id, first.version_id)
    previous.checkpoint(cursor)

    current = box_snapshot(next_id, next_body)
    (changed,), token = current.list_changes(cursor)
    assert changed.item_id == next_id
    assert token != cursor
    assert current.fetch_version(changed.item_id, changed.version_id) == next_body
    current.checkpoint(token)
    assert box_snapshot(next_id, next_body).list_changes(token)[0] == ()


def test_box_fetch_keeps_the_listed_bytes_when_download_changes():
    body = b"first version"
    # The provider is the only double: listing and fetching are the real adapter.
    source = BoxPullConnector(fetcher=lambda _: body)
    source.register_shared_file(BoxSharedFile("project", 123, "matrix.zip", "https://example.test/download"))
    (item,), token = source.list_changes(None)
    body = b"changed after listing"
    assert source.fetch_version(item.item_id, item.version_id) == b"first version"
    source.checkpoint(token)
    (changed,), _ = source.list_changes(token)
    assert changed.version_id != item.version_id
    with pytest.raises(ValueError, match="version"):
        source.fetch_version(item.item_id, item.version_id)
    assert source.fetch_version(changed.item_id, changed.version_id) == body


def test_box_legacy_item_cursor_replays_instead_of_claiming_a_byte_version():
    source = box_snapshot()
    (item,), token = source.list_changes("123")
    assert source.fetch_version(item.item_id, item.version_id) == b"first version"
    source.checkpoint(token)
    assert box_snapshot().list_changes(token)[0] == ()


def test_box_failed_relisting_invalidates_the_previous_pending_checkpoint():
    unavailable = False

    def fetch(_):
        if unavailable:
            raise OSError("download unavailable")
        return b"first version"

    source = BoxPullConnector(fetcher=fetch)
    source.register_shared_file(BoxSharedFile("project", 123, "matrix.zip", "https://example.test/download"))
    (item,), token = source.list_changes(None)
    source.fetch_version(item.item_id, item.version_id)
    unavailable = True
    with pytest.raises(OSError):
        source.list_changes(token)
    with pytest.raises(ValueError, match="checkpoint"):
        source.checkpoint(token)
    assert source.current_checkpoint is None


def test_box_rejects_foreign_snapshot_scope_before_fetching():
    _, token = box_snapshot().list_changes(None)
    fetched = []
    source = BoxPullConnector(shared_urls=("https://example.test/other",), fetcher=fetched.append)
    with pytest.raises(ValueError, match="scope"):
        source.list_changes(token)
    assert fetched == []


def test_box_shared_url_replacement_does_not_keep_an_old_discovery_member():
    shared_url = "https://txdot.box.com/s/project"
    item_id = 123
    downloaded = []

    def fetch(url):
        if url == shared_url:
            return (
                '<meta property="og:title" content="matrix.zip | Powered by Box">'
                f'{{"sharedName":"project","vanityName":"","itemID":{item_id},"itemType":"file"}}'
            ).encode()
        downloaded.append(url)
        return str(item_id).encode()

    source = BoxPullConnector(shared_urls=(shared_url,), fetcher=fetch)
    (first,), cursor = source.list_changes(None)
    assert source.fetch_version(first.item_id, first.version_id) == b"123"
    source.checkpoint(cursor)
    item_id = 124
    (second,), token = source.list_changes(cursor)
    assert second.item_id == "124"
    assert source.fetch_version(second.item_id, second.version_id) == b"124"
    assert len(downloaded) == 2
    source.checkpoint(token)
    restarted = BoxPullConnector(shared_urls=(shared_url,), fetcher=fetch)
    assert restarted.list_changes(token) == ((), token)


def test_box_changed_snapshot_replay_keeps_unchanged_delivery_identity(isolated_content_store):
    from corridor.connectors.pull_connector import sync_pull_connector
    from corridor.object_storage import content_store

    bodies = {"first": b"%PDF-1.4 first", "second": b"%PDF-1.4 second"}

    def snapshot():
        connector = BoxPullConnector(fetcher=bodies.__getitem__)
        for item_id, name in ((123, "first"), (124, "second")):
            connector.register_shared_file(BoxSharedFile("project", item_id, name + ".pdf", name))
        return connector

    first = sync_pull_connector(snapshot(), customer="fixture", project="project", channel="box")
    bodies["second"] = b"%PDF-1.4 updated second"
    second = sync_pull_connector(
        snapshot(), customer="fixture", project="project", channel="box", cursor=first.checkpoint_token,
    )
    assert first.advanced and second.advanced
    assert len(first.envelopes) == len(second.envelopes) == 2
    assert first.envelopes[0].delivery_identity == second.envelopes[0].delivery_identity
    assert first.envelopes[1].delivery_identity != second.envelopes[1].delivery_identity
    assert content_store().get(
        second.envelopes[1].bytes_reference, sha256=second.envelopes[1].content_digest,
    ) == b"%PDF-1.4 updated second"
