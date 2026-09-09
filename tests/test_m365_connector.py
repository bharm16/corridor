"""Offline Graph responses exercise the existing pull/storage boundary (#497)."""

from hashlib import sha256

import pytest

from corridor.connectors.pull_connector import sync_pull_connector


@pytest.fixture
def session():
    from corridor.db import engine, Session
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection) as session:
            yield session
        transaction.rollback()


def test_recorded_mail_retains_exact_mime_thread_and_workbook_attachment(session, isolated_content_store, tmp_path, monkeypatch):
    from email.message import EmailMessage
    from email.policy import SMTP
    from io import BytesIO
    from openpyxl import Workbook
    from sqlalchemy import select, func, text
    from corridor.config import settings
    from corridor.connectors.microsoft365 import GraphLocation, RecordedGraphTransport, Microsoft365PullConnector
    from corridor.m365_replay import replay_graph
    from corridor.models import Project, Document, InboundMessage, SourceDelivery, ProjectRecordRevision
    from corridor.object_storage import content_store

    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    book = Workbook()
    book.active.append(["Utility Conflict ID", "Utility Owner"])
    book.active.append(["UC-1", "Source Utility"])
    stream = BytesIO()
    book.save(stream)
    attachment = stream.getvalue()
    message = EmailMessage(policy=SMTP)
    message["From"], message["To"] = "utility@example.test", "project@example.test"
    message["Message-ID"] = "<first@example.test>"
    message["Subject"] = "Project source"
    message.set_content("We will finish in October.")
    message.add_attachment(attachment, maintype="application", subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="matrix.xlsx")
    raw = message.as_bytes()
    project = Project(slug="graph-recording-mail", name="Graph fixture", is_synthetic=True)
    session.add(project)
    session.flush()
    session.execute(text("set local role corridor_worker"))
    location = GraphLocation("tenant", "mailbox", "shared-project", "inbox", "shared")
    pages = {location.delta_url: {"value": [{"id": "immutable-message", "changeKey": "change-one", "isDraft": False}],
        "@odata.deltaLink": location.delta_url + "?$deltatoken=one"}}

    def replay():
        connector = Microsoft365PullConnector(location, RecordedGraphTransport(pages=pages,
            versions={("immutable-message", "change-one"): raw}))
        return replay_graph(session, project=project, customer="fixture", connector=connector,
            run_identity="mail-fixture", attachment_doc_types={sha256(attachment).hexdigest(): "matrix"})

    with session.begin_nested() as crash:
        replay()
        crash.rollback()
    first, repeated = replay(), replay()
    assert first == repeated
    inbound = session.get_one(InboundMessage, first["registered"][0]["message_id"])
    delivery = session.get_one(SourceDelivery, inbound.push_delivery_id)
    assert inbound.route_evidence_json["boundary"] == "pull_configuration"
    assert content_store().get(delivery.bytes_reference, sha256=delivery.content_sha256) == raw
    assert inbound.headers_json["message_id"] == "<first@example.test>"
    assert inbound.attachments_json[0]["document_id"] is not None
    assert session.scalar(select(func.count(Document.id)).where(Document.project_id == project.id)) == 2
    assert session.scalar(select(func.count(ProjectRecordRevision.id)).where(ProjectRecordRevision.project_id == project.id)) == 0

    library = GraphLocation("tenant", "library", "drive")
    library_connector = Microsoft365PullConnector(library, RecordedGraphTransport(
        pages={library.delta_url: {"value": [{"id": "workbook", "file": {}, "name": "matrix.xlsx", "eTag": "v1"}],
            "@odata.deltaLink": library.delta_url + "?$deltatoken=one"}}, versions={("workbook", "v1"): attachment}))
    registered = replay_graph(session, project=project, customer="fixture", connector=library_connector,
                             run_identity="library-fixture", doc_types={"workbook": "matrix"})
    assert registered["registered"][0]["document_id"] == inbound.attachments_json[0]["document_id"]


def test_graph_pages_and_exact_versions_replay_without_advancing_after_failure(isolated_content_store):
    from corridor.connectors.microsoft365 import GraphLocation, RecordedGraphTransport, Microsoft365PullConnector

    location = GraphLocation(tenant_id="tenant-one", kind="library", resource_id="drive-one")
    first = location.delta_url
    second = first + "?$skiptoken=two"
    final = first + "?$deltatoken=done"
    body = b"%PDF-1.4 exact Graph file"
    pages = {first: {"value": [{"id": "file-one", "name": "schedule.pdf", "file": {}, "eTag": "version-one",
        "parentReference": {"driveId": "drive-one"}}], "@odata.nextLink": second},
        second: {"value": [], "@odata.deltaLink": final}}
    transport = RecordedGraphTransport(pages=pages, versions={("file-one", "version-one"): body})
    connector = Microsoft365PullConnector(location, transport)
    with pytest.raises(ValueError, match="checkpoint"):
        connector.checkpoint(final)
    result = sync_pull_connector(connector, customer="customer-one", project="project-one", channel=location.channel)
    assert result.advanced and connector.current_checkpoint == final
    (envelope,) = result.envelopes
    assert envelope.content_digest == sha256(body).hexdigest()
    assert envelope.external_version == "version-one"
    assert "tenant-one" in envelope.external_identity and "drive-one" in envelope.external_identity
    retry = Microsoft365PullConnector(location, transport)
    assert sync_pull_connector(retry, customer="customer-one", project="project-one", channel=location.channel).envelopes == result.envelopes
    missing = Microsoft365PullConnector(location, RecordedGraphTransport(pages=pages, versions={}))
    with pytest.raises(ValueError, match="exact version"):
        sync_pull_connector(missing, customer="customer-one", project="project-one", channel=location.channel)
    assert missing.current_checkpoint is None


def test_graph_refuses_personal_mailboxes_and_foreign_cursor():
    from corridor.connectors.microsoft365 import GraphLocation, RecordedGraphTransport, Microsoft365PullConnector
    with pytest.raises(ValueError, match="shared"):
        GraphLocation(tenant_id="t", kind="mailbox", resource_id="personal", mailbox_type="personal", folder_id="inbox")
    location = GraphLocation(tenant_id="t", kind="library", resource_id="drive")
    connector = Microsoft365PullConnector(location, RecordedGraphTransport(pages={}, versions={}))
    with pytest.raises(ValueError, match="location"):
        connector.list_changes("https://graph.microsoft.com/v1.0/drives/other/root/delta?$token=x")


@pytest.mark.parametrize("page", [
    {"error": {"code": "resyncRequired"}},
    {"value": [{"id": "removed", "deleted": {}}]},
    {"value": [{"id": "foreign", "file": {}, "eTag": "v1", "name": "f.pdf", "parentReference": {"driveId": "other"}}]},
    {"value": [{"id": "missing-version", "file": {}, "name": "f.pdf"}]},
])
def test_incomplete_or_unhandled_graph_round_never_advances(page):
    from corridor.connectors.microsoft365 import GraphLocation, RecordedGraphTransport, Microsoft365PullConnector
    location = GraphLocation("tenant", "library", "drive")
    connector = Microsoft365PullConnector(location, RecordedGraphTransport(
        pages={location.delta_url: {**page, "@odata.deltaLink": location.delta_url + "?$deltatoken=one"}}, versions={}))
    with pytest.raises(ValueError):
        connector.list_changes()
    assert connector.current_checkpoint is None
