"""The one delivery ledger both transports write (#599, ADR-0089).

ADR-0083 made the ``SourceEnvelope`` common to pull and push; #511 persisted
only the push half, so "did we take delivery of this external version" was a
database read on one transport and a re-listing of the customer's own system on
the other. These tests hold the family that removes that asymmetry: one
identity rule for both transports, one row per outcome of a delivery, and a
cursor derived from an append-only relation rather than from a receipt.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from corridor.connectors.pull_connector import (
    build_delivery_identity,
    build_idempotency_key,
)
from corridor.models import PushIntakeCredential, SourceDelivery
from corridor.source_delivery import (
    DeliveryBinding,
    DeliveryObservation,
    SourceDeliveryRefused,
    delivery_identity_for,
    record_delivery,
    stored_delivery,
    take_delivery,
)


def _pull_binding(project) -> DeliveryBinding:
    return DeliveryBinding(
        customer="acme-utilities",
        project_id=project.id,
        project_slug=project.slug,
        transport="pull",
        channel="shared-files",
        configuration_identity="txdot-rid-box-v1",
        configuration_version="connector-polling-v1",
    )


def _push_binding(session, project) -> DeliveryBinding:
    credential = PushIntakeCredential(
        customer="acme-utilities",
        project_id=project.id,
        channel="project_alias",
        credential_sha256=sha256(uuid4().bytes).hexdigest(),
    )
    session.add(credential)
    session.flush()
    return DeliveryBinding(
        customer="acme-utilities",
        project_id=project.id,
        project_slug=project.slug,
        transport="push",
        channel="project_alias",
        configuration_identity=f"credential:{credential.id}",
        credential_id=credential.id,
    )


def _observation(body: bytes, *, item: str = "item-a", version: str = "v1"):
    digest = sha256(body).hexdigest()
    return DeliveryObservation(
        external_identity=item,
        external_version=version,
        content_digest=digest,
        bytes_reference=f"{digest[:2]}/{digest}.pdf",
    )


def test_pull_and_push_deliveries_are_one_queryable_family(session, project):
    """The asymmetry ADR-0089 removed: both transports, one relation, one rule."""

    pulled = take_delivery(
        session,
        _pull_binding(project),
        _observation(b"%PDF pulled"),
        service_identity="corridor.connector_polling",
        run_identity="due-attempt:1",
    )
    pushed = take_delivery(
        session,
        _push_binding(session, project),
        _observation(b"%PDF pushed", item="mta-1"),
        service_identity="corridor.push_intake",
        run_identity="push-intake:1",
    )

    rows = session.scalars(
        select(SourceDelivery)
        .where(SourceDelivery.project_id == project.id)
        .order_by(SourceDelivery.id)
    ).all()
    assert [row.transport for row in rows] == ["pull", "push"]
    assert {row.disposition for row in rows} == {"stored"}
    assert [row.id for row in rows] == [pulled.delivery_id, pushed.delivery_id]
    # One customer, one project, and the same shape of identity on both.
    assert all(len(row.delivery_identity) == 64 for row in rows)
    assert all(len(row.idempotency_key) == 64 for row in rows)


def test_the_identity_is_the_databases_and_a_replay_converges_on_it(session, project):
    """No Python-side replay check: the insert lets the constraint decide."""

    binding = _pull_binding(project)
    observation = _observation(b"%PDF once")

    first = take_delivery(
        session,
        binding,
        observation,
        service_identity="corridor.connector_polling",
        run_identity="due-attempt:1",
    )
    second = take_delivery(
        session,
        binding,
        observation,
        service_identity="corridor.connector_polling",
        run_identity="due-attempt:2",
    )
    third = take_delivery(
        session,
        binding,
        observation,
        service_identity="corridor.connector_polling",
        run_identity="due-attempt:3",
    )

    assert (first.disposition, first.created) == ("stored", True)
    assert (second.disposition, second.created) == ("duplicate", True)
    # A third pass converges on the duplicate row rather than appending: the
    # ledger records the outcomes a delivery had, not the attempts at it.
    assert (third.disposition, third.created) == ("duplicate", False)
    assert third.delivery_id == second.delivery_id
    assert session.scalar(
        select(func.count(SourceDelivery.id)).where(
            SourceDelivery.project_id == project.id
        )
    ) == 2
    # The run that first took delivery is what the row keeps.
    stored = stored_delivery(session, idempotency_key=first.idempotency_key)
    assert stored.run_identity == "due-attempt:1"


def test_a_refused_delivery_keeps_its_digest_and_its_reason(session, project):
    """A refusal is a record: the digest of what arrived, and why it was refused."""

    binding = _pull_binding(project)
    body = b"%PDF refused"
    refused = record_delivery(
        session,
        binding,
        DeliveryObservation(
            external_identity="item-r",
            external_version="v1",
            content_digest=sha256(body).hexdigest(),
        ),
        disposition="terminally_refused",
        service_identity="corridor.connector_polling",
        run_identity="due-attempt:1",
        refusal_reason="malware_detected: threat detected",
    )

    row = session.get(SourceDelivery, refused.delivery_id)
    assert row.disposition == "terminally_refused"
    assert row.content_sha256 == sha256(body).hexdigest()
    assert row.refusal_reason == "malware_detected: threat detected"
    assert row.bytes_reference == ""
    # A refusal is not a taking: nothing answers "already taken" from it.
    assert stored_delivery(session, idempotency_key=row.idempotency_key) is None


def test_the_ledger_refuses_a_disposition_that_does_not_say_what_it_means(
    session, project
):
    """The five dispositions, and the evidence each of them owes."""

    binding = _pull_binding(project)
    observation = _observation(b"%PDF evidence")

    with pytest.raises(SourceDeliveryRefused, match="not a delivery disposition"):
        record_delivery(
            session,
            binding,
            observation,
            disposition="lost_somewhere",
            service_identity="tests",
            run_identity="tests:1",
        )
    with pytest.raises(SourceDeliveryRefused, match="records why"):
        record_delivery(
            session,
            binding,
            observation,
            disposition="transient_failure",
            service_identity="tests",
            run_identity="tests:1",
        )
    with pytest.raises(SourceDeliveryRefused, match="no refusal reason"):
        record_delivery(
            session,
            binding,
            observation,
            disposition="stored",
            service_identity="tests",
            run_identity="tests:1",
            refusal_reason="but it was stored",
        )


def test_a_binding_names_one_transport_and_the_configuration_it_arrived_under(
    session, project
):
    """A pulled delivery authenticates neither way, a pushed one exactly one."""

    with pytest.raises(SourceDeliveryRefused, match="pull or push"):
        DeliveryBinding(
            customer="acme-utilities",
            project_id=project.id,
            project_slug=project.slug,
            transport="carrier-pigeon",
            channel="shared-files",
            configuration_identity="txdot-rid-box-v1",
        )
    with pytest.raises(SourceDeliveryRefused, match="authenticated it"):
        DeliveryBinding(
            customer="acme-utilities",
            project_id=project.id,
            project_slug=project.slug,
            transport="push",
            channel="project_alias",
            configuration_identity="credential:1",
        )
    with pytest.raises(SourceDeliveryRefused, match="configuration"):
        DeliveryBinding(
            customer="acme-utilities",
            project_id=project.id,
            project_slug=project.slug,
            transport="pull",
            channel="shared-files",
            configuration_identity="   ",
        )


def test_both_transports_derive_one_identity_from_the_same_parts(session, project):
    """#511 and #496 met only at the envelope; they now meet at the identity."""

    binding = _pull_binding(project)
    observation = _observation(b"%PDF shared")
    identity, key = delivery_identity_for(binding, observation)

    assert identity == sha256(
        f"acme-utilities:{project.slug}:shared-files:item-a:v1".encode("utf-8")
    ).hexdigest()
    assert key == sha256(
        f"{identity}:{observation.content_digest}".encode("utf-8")
    ).hexdigest()


def test_one_function_reads_a_retained_delivery_back_as_its_envelope(session, project):
    """Row to envelope was built three ways; it is one function (ADR-0089).

    ``envelope_of`` filled it from the objects a writer happened to hold,
    ``envelope_for_delivery`` reads it from the row, and ``push_intake`` had a
    third for its own transport. ``require_stored_envelope`` compares a
    consumer's envelope against the retained one for equality, so a second
    construction that formats one field differently does not read differently —
    it refuses the delivery.
    """

    from corridor import source_delivery
    from corridor.source_delivery import envelope_for_delivery, require_stored_envelope

    pulled = take_delivery(
        session,
        _pull_binding(project),
        _observation(b"%PDF read back"),
        service_identity="corridor.connector_polling",
        run_identity="due-attempt:1",
    )
    pushed = take_delivery(
        session,
        _push_binding(session, project),
        _observation(b"%PDF pushed back", item="mta-9"),
        service_identity="corridor.push_intake",
        run_identity="push-intake:1",
    )

    # One function, both transports, and it is what the gate admits.
    for recorded in (pulled, pushed):
        envelope = envelope_for_delivery(session, recorded.delivery_id)
        assert envelope.project == project.slug
        assert envelope.delivery_identity == recorded.delivery_identity
        assert envelope.idempotency_key == recorded.idempotency_key
        assert require_stored_envelope(session, envelope).id == recorded.delivery_id

    envelope = envelope_for_delivery(session, pulled.delivery_id)
    for altered in (
        replace(envelope, customer="another-customer"),
        replace(envelope, project="another-project"),
        replace(envelope, bytes_reference="ab/somewhere-else.pdf"),
        replace(envelope, metadata={"filename": "renamed.pdf"}),
    ):
        with pytest.raises(SourceDeliveryRefused, match="exact stored"):
            require_stored_envelope(session, altered)

    # And there is no second construction left to disagree with it.
    assert {name for name in vars(source_delivery) if "envelope" in name} == {
        "envelope_for_delivery",
        "require_stored_envelope",
    }


def test_a_human_upload_is_a_pushed_delivery_its_own_person_authenticated(
    session, project
):
    """The seam ADR-0078's own migration header used to record as outstanding.

    A manual upload *is* a delivery in every sense this family means: somebody
    hands Corridor bytes it never asked for, which is exactly what
    ``push_intake`` calls push, and ADR-0078 lists manual upload among the
    connector kinds that enter under one contract. What stopped it being
    recorded here was the database rather than a preference — a pushed delivery
    had to name a ``push_intake_credentials`` row, and a signed-in person
    presents no credential, because the transport did not authenticate the
    delivery, the web session did.

    #823 turns that requirement into an authentication mode, and this test
    holds all four of its limbs plus the two things that deliberately did not
    change.
    """

    binding = DeliveryBinding(
        customer="acme-utilities",
        project_id=project.id,
        project_slug=project.slug,
        transport="push",
        channel="product_upload",
        configuration_identity="product-upload",
        delivered_by_principal="local:dana-fields",
    )
    digest = sha256(b"a matrix a person handed over").hexdigest()
    recorded = take_delivery(
        session,
        binding,
        DeliveryObservation(
            external_identity="matrix.pdf",
            external_version=digest,
            content_digest=digest,
            bytes_reference=f"{digest[:2]}/{digest}.pdf",
        ),
        service_identity="corridor.source_intake",
        run_identity="product-upload:1",
    )
    row = session.get(SourceDelivery, recorded.delivery_id)
    assert (row.transport, row.channel) == ("push", "product_upload")
    assert row.delivered_by_principal == "local:dana-fields"
    assert row.credential_id is None

    # A push that authenticated neither way, and one claiming both, are refused
    # before they reach the database.
    for credential_id, principal in ((None, ""), (1, "local:dana-fields")):
        with pytest.raises(SourceDeliveryRefused, match="authenticated it"):
            DeliveryBinding(
                customer="acme-utilities",
                project_id=project.id,
                project_slug=project.slug,
                transport="push",
                channel="product_upload",
                configuration_identity="product-upload",
                credential_id=credential_id,
                delivered_by_principal=principal,
            )
    # A pull is bound by its configuration and authenticates neither way.
    with pytest.raises(SourceDeliveryRefused, match="connector configuration"):
        DeliveryBinding(
            customer="acme-utilities",
            project_id=project.id,
            project_slug=project.slug,
            transport="pull",
            channel="shared-files",
            configuration_identity="txdot-rid-box-v1",
            delivered_by_principal="local:dana-fields",
        )
    # And the database refuses the same row, so the rule is not one a writer
    # that bypassed `DeliveryBinding` could get wrong differently. The copy
    # names neither a credential nor a principal, so it derives the identity a
    # principal-less delivery would (the person is now part of that identity,
    # #957) and carries it, so what refuses the row is the authentication check
    # and not a mis-derived identity.
    principal_less_identity = build_delivery_identity(
        customer="acme-utilities",
        project=project.slug,
        channel="product_upload",
        external_identity="matrix.pdf",
        external_version=digest,
    )
    with pytest.raises(Exception, match="ck_source_delivery_authentication"):
        with session.begin_nested():
            session.execute(
                text(
                    """
                    insert into source_deliveries (
                        customer, project_id, transport, channel,
                        configuration_identity, external_identity,
                        external_version, content_sha256, bytes_reference,
                        delivery_identity, idempotency_key, service_identity,
                        run_identity, disposition
                    )
                    select customer, project_id, transport, channel,
                           configuration_identity, external_identity,
                           external_version, content_sha256, bytes_reference,
                           :identity, :key, service_identity,
                           run_identity, 'duplicate'
                      from source_deliveries where id = :id
                    """
                ),
                {
                    "id": recorded.delivery_id,
                    "identity": principal_less_identity,
                    "key": build_idempotency_key(principal_less_identity, digest),
                },
            )

    definitions = {
        name: definition
        for name, definition in session.execute(
            text(
                """
                select conname, pg_get_constraintdef(oid)
                  from pg_constraint
                 where conrelid in ('public.source_deliveries'::regclass,
                                    'public.push_intake_credentials'::regclass)
                """
            )
        ).all()
    }
    # No third transport: ADR-0089 describes pull and push, and an upload is a
    # push rather than a new kind of arrival.
    transport = definitions["ck_source_delivery_transport"]
    assert "'pull'" in transport and "'push'" in transport
    assert "'upload'" not in transport
    # And no credential minted for a person: a push secret issued to an
    # uploader would be a real door into the project.
    channels = definitions["ck_push_intake_credential_channel"]
    assert "'project_alias'" in channels and "'upload'" not in channels
    assert "ck_source_delivery_push_credential" not in definitions
