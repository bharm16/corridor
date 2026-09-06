"""The project-bound push intake boundary and its refusals (#511).

ADR-0059 let a message name its own project by what it contained. These tests
hold the replacement: a credential binds one customer and one project before
anything parses, and after that every lookup a parser performs is scoped to
that boundary. The refusals come first on purpose — a spoofed thread header and
a cross-customer delivery are the two ways a crafted message would move itself,
and neither may work whatever the payload says.
"""

from __future__ import annotations

from email.message import EmailMessage
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from corridor import email_intake, push_intake
from corridor.config import settings
from corridor.connectors.pull_connector import SourceEnvelope, build_delivery_identity
from corridor.db import Session, engine
from corridor.models import (
    Dependency,
    Document,
    InboundMessage,
    InboundThread,
    Project,
    SourceDelivery,
    PushIntakeCredential,
)
from corridor.object_storage import content_store

from pdf_fixture_support import PdfFixture


ALPHA_ALIAS = "intake+alpha-a1b2c3@corridor.test"
BRAVO_ALIAS = "intake+bravo-d4e5f6@corridor.test"


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "store"


def make_project(session, name: str) -> Project:
    row = Project(slug=f"push-{uuid4().hex[:12]}", name=name, is_synthetic=True)
    session.add(row)
    session.flush()
    return row


def bind_alias(session, *, customer: str, project: Project, alias: str):
    return push_intake.register_push_credential(
        session,
        customer=customer,
        project=project,
        channel="project_alias",
        material=alias,
    )


def raw(
    *,
    message_id: str,
    body: str,
    to: str = "someone@example.test",
    references: str | None = None,
    sender: str = "utility@example.test",
    subject: str = "",
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Message-ID"] = message_id
    if subject:
        message["Subject"] = subject
    if references:
        message["References"] = references
        message["In-Reply-To"] = references.split()[-1]
    message.set_content(body)
    return message.as_bytes()


def push(session, alias: str, payload: bytes, *, delivery_id: str | None = None):
    return email_intake.receive_pushed_message(
        session,
        credential=push_intake.PushCredential(
            channel="project_alias", material=alias
        ),
        raw_bytes=payload,
        transport_delivery_id=delivery_id,
    )


def pdf_bytes(text: str) -> bytes:
    fixture = PdfFixture()
    fixture.add_page().text((72, 72), text)
    return fixture.tobytes()


def dependency(session, project: Project, ref_code: str) -> Dependency:
    row = Dependency(
        project_id=project.id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title="Gas main",
    )
    session.add(row)
    session.flush()
    return row


# --- The refusals ---------------------------------------------------------


def test_a_crafted_message_cannot_route_itself_into_another_customers_project(
    session,
):
    """Cross-customer refusal. The payload carries every routing signal the
    global-address path trusted, and every one of them points at the other
    customer. The delivery still belongs to the alias it arrived on."""

    alpha = make_project(session, "Alpha Segment")
    bravo = make_project(session, "Bravo Segment")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    bind_alias(session, customer="borealis-gas", project=bravo, alias=BRAVO_ALIAS)
    email_intake.register_project_identifier(
        session, project=bravo, kind="csj", value="B-200"
    )
    dependency(session, bravo, "FOC-77")

    received = push(
        session,
        ALPHA_ALIAS,
        raw(
            message_id="<crafted@example.test>",
            body="Regarding CSJ B-200 and conflict FOC-77, please file this.",
            to=BRAVO_ALIAS,
        ),
    )

    assert received.project_id == alpha.id
    assert received.route_status == "routed"
    stored = session.get(InboundMessage, received.message_id)
    assert stored.project_id == alpha.id
    assert session.get(InboundThread, stored.thread_id).project_id == alpha.id
    # The other customer's identifiers decided nothing and are not named in
    # this customer's retained record; only their number survives.
    assert received.route_evidence["candidate_project_ids"] == [alpha.id]
    assert received.route_evidence["sender_registered_contact_project_ids"] == []
    assert received.route_evidence["candidate_dependency_ids"] == []
    assert received.route_evidence["out_of_boundary_evidence"] >= 2
    assert (
        session.scalar(
            select(func.count(InboundMessage.id)).where(
                InboundMessage.project_id == bravo.id
            )
        )
        == 0
    )


def test_a_spoofed_reference_header_cannot_join_another_projects_thread(session):
    """A forged In-Reply-To/References chain naming a real conversation in
    another project resolves to nothing: threads are looked up inside the bound
    project, so the reply starts its own thread and the original is untouched."""

    alpha = make_project(session, "Alpha")
    bravo = make_project(session, "Bravo")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    bind_alias(session, customer="borealis-gas", project=bravo, alias=BRAVO_ALIAS)

    original = push(
        session, BRAVO_ALIAS, raw(message_id="<bravo-1@example.test>", body="First")
    )
    spoof = push(
        session,
        ALPHA_ALIAS,
        raw(
            message_id="<spoof@example.test>",
            body="Continuing the conversation",
            references="<bravo-1@example.test>",
        ),
    )

    assert spoof.thread_id != original.thread_id
    assert spoof.project_id == alpha.id
    assert session.get(InboundThread, spoof.thread_id).project_id == alpha.id
    bravo_thread_messages = session.scalars(
        select(InboundMessage.id).where(
            InboundMessage.thread_id == original.thread_id
        )
    ).all()
    assert bravo_thread_messages == [original.message_id]


def test_a_reply_inside_the_boundary_still_continues_its_own_thread(session):
    """The boundary scopes header threading; it does not disable it."""

    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)

    first = push(
        session, ALPHA_ALIAS, raw(message_id="<alpha-1@example.test>", body="First")
    )
    reply = push(
        session,
        ALPHA_ALIAS,
        raw(
            message_id="<alpha-2@example.test>",
            body="Reply",
            references="<alpha-1@example.test>",
        ),
    )

    assert reply.thread_id == first.thread_id


def test_a_wrong_alias_binds_nothing_and_leaves_nothing_behind(session, isolated_store):
    """An unknown alias, a revoked one, and a credential presented on the wrong
    channel all fail the same way, before any byte is stored or parsed."""

    alpha = make_project(session, "Alpha")
    credential = bind_alias(
        session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS
    )
    payload = raw(message_id="<wrong-alias@example.test>", body="anything")

    with pytest.raises(push_intake.PushIntakeRefused):
        push(session, "intake+not-issued@corridor.test", payload)
    with pytest.raises(push_intake.PushIntakeRefused):
        # The same material on another channel is another credential.
        email_intake.receive_pushed_message(
            session,
            credential=push_intake.PushCredential(
                channel="webhook", material=ALPHA_ALIAS
            ),
            raw_bytes=payload,
        )

    push_intake.revoke_push_credential(session, credential_id=credential.id)
    with pytest.raises(push_intake.PushIntakeRefused):
        push(session, ALPHA_ALIAS, payload)

    assert session.scalar(select(func.count(SourceDelivery.id))) == 0
    assert session.scalar(select(func.count(InboundMessage.id))) == 0
    # Binding precedes persistence, so an unbound delivery leaves no bytes to
    # attribute to anyone later.
    assert not [item for item in isolated_store.rglob("*") if item.is_file()]


def test_the_messages_own_headers_never_supply_the_boundary(session):
    """``To``, ``Cc``, and ``Delivered-To`` are written by the sender. The alias
    the transport delivered to is the only one that binds."""

    alpha = make_project(session, "Alpha")
    bravo = make_project(session, "Bravo")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    bind_alias(session, customer="borealis-gas", project=bravo, alias=BRAVO_ALIAS)

    message = EmailMessage()
    message["From"] = "utility@example.test"
    message["To"] = BRAVO_ALIAS
    message["Cc"] = BRAVO_ALIAS
    message["Delivered-To"] = BRAVO_ALIAS
    message["Message-ID"] = "<header-claim@example.test>"
    message.set_content("Filed under the header I wrote.")

    received = push(session, ALPHA_ALIAS, message.as_bytes())

    assert received.project_id == alpha.id


def test_a_pushed_delivery_cannot_be_accepted_without_an_established_binding(
    session,
):
    """The parse-side functions take a ``PushBinding`` and nothing else does."""

    payload = push_intake.PushPayload(body=b"bytes", filename="message.eml")

    with pytest.raises(push_intake.PushIntakeRefused):
        push_intake.accept_delivery(session, object(), payload)
    with pytest.raises(push_intake.PushIntakeRefused):
        push_intake.delivery_identity_of(None, payload)


# --- Delivery identity ----------------------------------------------------


def test_duplicate_delivery_is_idempotent_by_delivery_identity(session):
    """A transport that sends the same delivery twice produces one delivery,
    one message, and one registered Document.

    The second sending is recorded as its own outcome of the same delivery
    (ADR-0089): one ``stored`` row and one ``duplicate`` row, sharing one
    delivery identity, and nothing new written for the bytes. A third sending
    converges on the ``duplicate`` row rather than appending another."""

    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    payload = raw(message_id="<dupe@example.test>", body="Same delivery twice")

    first = push(session, ALPHA_ALIAS, payload, delivery_id="mta-0001")
    second = push(session, ALPHA_ALIAS, payload, delivery_id="mta-0001")
    push(session, ALPHA_ALIAS, payload, delivery_id="mta-0001")

    assert first.created is True
    assert second.created is False
    assert second.message_id == first.message_id
    dispositions = session.scalars(
        select(SourceDelivery.disposition).order_by(SourceDelivery.id)
    ).all()
    assert dispositions == ["stored", "duplicate"]
    assert (
        len(set(session.scalars(select(SourceDelivery.delivery_identity)).all())) == 1
    )
    assert session.scalar(select(func.count(InboundMessage.id))) == 1


def test_the_same_bytes_delivered_to_two_customers_are_two_deliveries(session):
    """Identity is scoped to the boundary. The one stored object is shared;
    neither customer's alias hands back the other customer's message."""

    alpha = make_project(session, "Alpha")
    bravo = make_project(session, "Bravo")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    bind_alias(session, customer="borealis-gas", project=bravo, alias=BRAVO_ALIAS)
    payload = raw(message_id="<shared@example.test>", body="Copied to both")

    to_alpha = push(session, ALPHA_ALIAS, payload)
    to_bravo = push(session, BRAVO_ALIAS, payload)

    assert to_alpha.created is True and to_bravo.created is True
    assert to_alpha.message_id != to_bravo.message_id
    assert to_alpha.project_id == alpha.id
    assert to_bravo.project_id == bravo.id
    assert session.scalar(select(func.count(SourceDelivery.id))) == 2
    digest = sha256(payload).hexdigest()
    assert (
        session.scalar(
            select(func.count(func.distinct(SourceDelivery.bytes_reference))).where(
                SourceDelivery.content_sha256 == digest
            )
        )
        == 1
    )


def test_the_raw_bytes_are_persisted_through_the_storage_interface(session):
    """No direct path write: the delivery names a content key the store holds."""

    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    payload = raw(message_id="<stored@example.test>", body="Byte exact")

    received = push(session, ALPHA_ALIAS, payload)

    delivery = session.scalars(select(SourceDelivery)).one()
    digest = sha256(payload).hexdigest()
    assert delivery.content_sha256 == digest
    assert content_store().get(delivery.bytes_reference, sha256=digest) == payload
    stored = session.get(InboundMessage, received.message_id)
    assert stored.raw_sha256 == digest
    assert stored.push_delivery_id == delivery.id


def test_the_envelope_is_the_one_shared_after_ingress(session):
    """Push normalizes into #496's ``SourceEnvelope``, with the identity the
    transport supplied rather than anything read out of the payload."""

    alpha = make_project(session, "Alpha")
    credential = bind_alias(
        session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS
    )
    payload = raw(message_id="<envelope@example.test>", body="Envelope")
    binding = push_intake.bind_credential(
        session,
        push_intake.PushCredential(channel="project_alias", material=ALPHA_ALIAS),
    )
    receipt = push_intake.accept_delivery(
        session,
        binding,
        push_intake.PushPayload(
            body=payload, filename="message.eml", transport_delivery_id="mta-77"
        ),
    )

    envelope = receipt.envelope
    assert isinstance(envelope, SourceEnvelope)
    digest = sha256(payload).hexdigest()
    assert envelope.customer == "acme-utilities"
    assert envelope.project == alpha.slug
    assert envelope.channel == "project_alias"
    assert envelope.external_identity == "mta-77"
    assert envelope.external_version == digest
    assert envelope.content_digest == digest
    assert envelope.bytes_reference.endswith(f"{digest}.eml")
    assert envelope.delivery_identity == build_delivery_identity(
        customer="acme-utilities",
        project=alpha.slug,
        channel="project_alias",
        external_identity="mta-77",
        external_version=digest,
    )
    assert binding.credential_id == credential.id


def test_the_push_intake_contract_conforms_and_is_not_the_pull_contract(session):
    """``PushIntake`` is bind/accept/replay, and nothing in it is a cursor.

    ADR-0083 keeps the two contracts apart because a pull connector decides
    when to fetch and a push channel decides nothing; the only thing they share
    is the envelope. A conforming implementation is driven here end to end.
    """

    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    intake: push_intake.PushIntake = push_intake.DatabasePushIntake(session)
    payload = push_intake.PushPayload(
        body=raw(message_id="<conformance@example.test>", body="Delivered"),
        filename="message.eml",
        transport_delivery_id="mta-1",
        original_timestamps={"received": "2026-09-03T00:00:00Z"},
        metadata={"transport": "test"},
    )

    binding = intake.bind(
        push_intake.PushCredential(channel="project_alias", material=ALPHA_ALIAS)
    )
    assert intake.replay(binding, payload) is None
    first = intake.accept(binding, payload)
    assert first.replayed is False
    replayed = intake.replay(binding, payload)
    assert replayed is not None
    assert replayed.envelope == first.envelope
    assert intake.accept(binding, payload).delivery_id == first.delivery_id
    assert first.envelope.original_timestamps == {"received": "2026-09-03T00:00:00Z"}
    assert first.envelope.metadata == {"transport": "test"}

    pull_only = {"list_changes", "fetch_version", "get_metadata", "checkpoint"}
    assert pull_only.isdisjoint(dir(push_intake.DatabasePushIntake))


# --- Inference inside the boundary ---------------------------------------


def test_evidence_naming_one_row_in_the_project_binds_that_row(session):
    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    row = dependency(session, alpha, "FOC-11")

    received = push(
        session,
        ALPHA_ALIAS,
        raw(message_id="<one-row@example.test>", body="About FOC-11 we will reply."),
    )

    assert received.route_evidence["in_project_resolution"] == "row_bound"
    assert received.route_evidence["tier"] == "record_identifier"
    assert session.get(InboundThread, received.thread_id).dependency_id == row.id


def test_ambiguous_in_project_evidence_stays_bounded_triage(session):
    """Two rows in the bound project match. The project was never in question,
    so the delivery is routed; the row stays unbound and visibly ambiguous."""

    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    first = dependency(session, alpha, "FOC-21")
    second = dependency(session, alpha, "FOC-22")

    received = push(
        session,
        ALPHA_ALIAS,
        raw(
            message_id="<ambiguous@example.test>",
            body="Both FOC-21 and FOC-22 move together.",
        ),
    )

    assert received.project_id == alpha.id
    assert received.route_status == "routed"
    assert received.route_evidence["in_project_resolution"] == "ambiguous"
    assert received.route_evidence["candidate_dependency_ids"] == sorted(
        [first.id, second.id]
    )
    assert session.get(InboundThread, received.thread_id).dependency_id is None


def test_a_message_citing_no_row_is_still_bound_to_its_project(session):
    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)

    received = push(
        session,
        ALPHA_ALIAS,
        raw(message_id="<no-row@example.test>", body="Thanks, talk soon."),
    )

    assert received.project_id == alpha.id
    assert received.route_evidence["in_project_resolution"] == "unmatched"
    assert received.route_evidence["tier"] == "none"


# --- Migration off the global address ------------------------------------


def test_a_legacy_global_address_thread_continues_under_its_new_alias(session):
    """Migrating a project keeps its raw MIME and its thread history: the bound
    path writes the same rows, so a reply delivered to the new alias joins the
    conversation the global-address path had already routed."""

    alpha = make_project(session, "Alpha")
    email_intake.register_project_identifier(
        session, project=alpha, kind="csj", value="A-100"
    )
    legacy = email_intake.receive_message(
        session,
        raw_bytes=raw(
            message_id="<legacy@example.test>",
            body="CSJ A-100 initial note",
            to="intake@corridor.test",
        ),
        service_address="intake@corridor.test",
    )
    assert legacy.project_id == alpha.id

    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    reply = push(
        session,
        ALPHA_ALIAS,
        raw(
            message_id="<migrated@example.test>",
            body="Following up",
            references="<legacy@example.test>",
        ),
    )

    assert reply.thread_id == legacy.thread_id
    assert reply.project_id == alpha.id
    history = session.scalars(
        select(InboundMessage.raw_sha256)
        .where(InboundMessage.thread_id == legacy.thread_id)
        .order_by(InboundMessage.id)
    ).all()
    assert len(history) == 2
    stored_legacy = session.get(InboundMessage, legacy.message_id)
    assert stored_legacy.headers_json["message_id"] == "<legacy@example.test>"
    assert stored_legacy.push_delivery_id is None


def test_a_bound_delivery_registers_its_attachment_as_a_project_document(session):
    """The channel changes; the intake the attachment goes through does not."""

    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    message = EmailMessage()
    message["From"] = "utility@example.test"
    message["To"] = "someone@example.test"
    message["Message-ID"] = "<attachment@example.test>"
    message.set_content("See attached.")
    message.add_attachment(
        pdf_bytes("Relocation letter"),
        maintype="application",
        subtype="pdf",
        filename="relocation-letter.pdf",
    )

    received = push(session, ALPHA_ALIAS, message.as_bytes())

    stored = session.get(InboundMessage, received.message_id)
    receipts = stored.attachments_json
    assert len(receipts) == 1
    registered = session.get(Document, receipts[0]["document_id"])
    assert registered is not None
    assert registered.project_id == alpha.id


def test_a_credential_is_stored_only_as_a_one_way_digest(session):
    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)

    record = session.scalars(select(PushIntakeCredential)).one()
    assert ALPHA_ALIAS not in str(record.credential_sha256)
    assert record.credential_sha256 == push_intake.credential_digest(
        "project_alias", ALPHA_ALIAS
    )
    with pytest.raises(push_intake.PushIntakeRefused):
        # Re-registration would be how an alias silently changes project.
        bind_alias(
            session,
            customer="borealis-gas",
            project=make_project(session, "Bravo"),
            alias=ALPHA_ALIAS,
        )


# --- Crash and retry across independent committed transactions ------------


def test_a_retry_after_a_crash_takes_the_delivery_exactly_once(runtime_database):
    """The seam needs real commits, so it runs on the harness-owned database.

    A worker that stores the bytes and dies before committing leaves an object
    and no rows; the retry that follows must produce one delivery and one
    message, and a second retry after that must produce neither.
    """

    factory = runtime_database.session_factory
    payload = raw(message_id="<crash@example.test>", body="Delivered once")

    with factory() as setup:
        alpha = make_project(setup, "Alpha")
        bind_alias(
            setup, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS
        )
        project_id = alpha.id
        setup.commit()

    # The crash: bytes reach the store, the transaction never commits.
    with factory() as crashed:
        binding = push_intake.bind_credential(
            crashed,
            push_intake.PushCredential(
                channel="project_alias", material=ALPHA_ALIAS
            ),
        )
        push_intake.accept_delivery(
            crashed,
            binding,
            push_intake.PushPayload(
                body=payload, filename="message.eml", transport_delivery_id="mta-9"
            ),
        )
        crashed.rollback()

    with factory() as first:
        taken = push(first, ALPHA_ALIAS, payload, delivery_id="mta-9")
        assert taken.created is True
        assert taken.project_id == project_id
        first.commit()
        first_message_id = taken.message_id

    with factory() as retry:
        again = push(retry, ALPHA_ALIAS, payload, delivery_id="mta-9")
        assert again.created is False
        assert again.message_id == first_message_id
        retry.commit()

    with factory() as reading:
        assert reading.scalar(
            select(func.count(SourceDelivery.id)).where(
                SourceDelivery.disposition == "stored"
            )
        ) == 1
        assert reading.scalar(select(func.count(InboundMessage.id))) == 1
        digest = sha256(payload).hexdigest()
        delivery = reading.scalars(
            select(SourceDelivery).where(SourceDelivery.disposition == "stored")
        ).one()
        assert content_store().get(delivery.bytes_reference, sha256=digest) == payload


# --- The transport boundary the deployment webhook presents ---------------


def test_the_inbound_webhook_binds_on_the_envelope_recipient_not_the_headers(
    session, monkeypatch
):
    """The shared secret authenticates the transport; the alias it delivered to
    binds the project. A delivery for an alias nobody issued is refused before
    anything is stored, and one whose headers claim another project is not."""

    from fastapi.testclient import TestClient

    from corridor.web.app import app, get_machine_session, get_session

    monkeypatch.setattr(settings, "inbound_webhook_secret", "server-secret")
    monkeypatch.setattr(settings, "inbound_service_address", "")
    alpha = make_project(session, "Alpha")
    bravo = make_project(session, "Bravo")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    bind_alias(session, customer="borealis-gas", project=bravo, alias=BRAVO_ALIAS)
    app.dependency_overrides[get_session] = lambda: session
    # #680: `/intake/inbound` runs on the operations capability now.
    app.dependency_overrides[get_machine_session] = lambda: session
    try:
        with TestClient(app) as client:
            refused = client.post(
                "/intake/inbound",
                content=raw(message_id="<no-alias@example.test>", body="hello"),
                headers={
                    "X-Corridor-Inbound-Token": "server-secret",
                    "X-Corridor-Delivered-To": "intake+never-issued@corridor.test",
                },
            )
            assert refused.status_code == 403

            accepted = client.post(
                "/intake/inbound",
                content=raw(
                    message_id="<bound@example.test>",
                    body="Filed under the alias I was delivered to.",
                    to=BRAVO_ALIAS,
                ),
                headers={
                    "X-Corridor-Inbound-Token": "server-secret",
                    "X-Corridor-Delivered-To": ALPHA_ALIAS,
                    "X-Corridor-Delivery-Id": "mta-4242",
                },
            )
            assert accepted.status_code == 200
            assert accepted.json()["project_id"] == alpha.id
            assert accepted.json()["route_status"] == "routed"
    finally:
        app.dependency_overrides.clear()

    assert session.scalar(select(func.count(SourceDelivery.id))) == 1
    assert (
        session.scalar(
            select(func.count(InboundMessage.id)).where(
                InboundMessage.project_id == bravo.id
            )
        )
        == 0
    )


# --- Refused deliveries are records, not absences (#599, ADR-0089) --------


def test_a_delivery_the_intake_gate_refuses_is_recorded_with_its_reason(session):
    """A refusal used to be lost: the gate raised and nothing was written.

    ADR-0089 makes the refusal a record — the exact digest of what arrived, the
    rule that refused it, and the binding it arrived under — so an intake
    failure is an operational metric rather than an absence somebody has to
    notice. The bytes themselves are not stored: the digest and the reason are
    what the record needs.
    """

    alpha = make_project(session, "Alpha")
    credential = bind_alias(
        session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS
    )
    binding = push_intake.bind_credential(
        session,
        push_intake.PushCredential(channel="project_alias", material=ALPHA_ALIAS),
    )
    hostile = b"this is not a PDF at all"

    with pytest.raises(push_intake.PushDeliveryRefused) as refused:
        push_intake.accept_delivery(
            session,
            binding,
            push_intake.PushPayload(
                body=hostile, filename="exhibit.pdf", transport_delivery_id="mta-99"
            ),
        )

    assert "magic_mismatch" in str(refused.value)
    delivery = session.scalars(select(SourceDelivery)).one()
    assert delivery.id == refused.value.delivery_id
    assert delivery.disposition == "terminally_refused"
    assert delivery.transport == "push"
    assert delivery.credential_id == credential.id
    assert delivery.configuration_identity == f"credential:{credential.id}"
    assert delivery.service_identity == "corridor.push_intake"
    assert delivery.content_sha256 == sha256(hostile).hexdigest()
    assert delivery.external_identity == "mta-99"
    assert delivery.refusal_reason.startswith("magic_mismatch:")
    assert delivery.bytes_reference == ""
    # Nothing was admitted: no message, and no object under that digest.
    assert session.scalar(select(func.count(InboundMessage.id))) == 0
    assert content_store().resolve(delivery.content_sha256) is None


def test_the_inbound_webhook_keeps_the_refusal_it_recorded(session, monkeypatch):
    """Refusing the request must not roll back the record of the refusal.

    The endpoint commits the ledger row the gate's refusal produced and then
    answers 400: a refusal that rolls back with its response is exactly the
    loss ADR-0089 set out to remove.
    """

    from fastapi.testclient import TestClient

    from corridor import intake_hardening
    from corridor.web.app import app, get_machine_session, get_session

    monkeypatch.setattr(settings, "inbound_webhook_secret", "server-secret")
    monkeypatch.setattr(settings, "inbound_service_address", "")
    monkeypatch.setattr(
        intake_hardening,
        "_GLOBAL_SCANNER",
        intake_hardening.FakeMalwareScanner(infected_signatures={b"EICAR-MARKER"}),
    )
    alpha = make_project(session, "Alpha")
    bind_alias(session, customer="acme-utilities", project=alpha, alias=ALPHA_ALIAS)
    hostile = raw(message_id="<hostile@example.test>", body="EICAR-MARKER inside")
    app.dependency_overrides[get_session] = lambda: session
    # #680: `/intake/inbound` runs on the operations capability now.
    app.dependency_overrides[get_machine_session] = lambda: session
    try:
        with TestClient(app) as client:
            response = client.post(
                "/intake/inbound",
                content=hostile,
                headers={
                    "X-Corridor-Inbound-Token": "server-secret",
                    "X-Corridor-Delivered-To": ALPHA_ALIAS,
                    "X-Corridor-Delivery-Id": "mta-hostile",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    delivery = session.scalars(select(SourceDelivery)).one()
    assert delivery.disposition == "terminally_refused"
    assert delivery.external_identity == "mta-hostile"
    assert delivery.refusal_reason.startswith("malware_detected:")
    assert session.scalar(select(func.count(InboundMessage.id))) == 0
