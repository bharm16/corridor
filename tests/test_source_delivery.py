"""The one delivery ledger both transports write (#599, ADR-0089).

ADR-0083 made the ``SourceEnvelope`` common to pull and push; #511 persisted
only the push half, so "did we take delivery of this external version" was a
database read on one transport and a re-listing of the customer's own system on
the other. These tests hold the family that removes that asymmetry: one
identity rule for both transports, one row per outcome of a delivery, and a
cursor derived from an append-only relation rather than from a receipt.
"""

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.db import Session, engine
from corridor.models import Project, PushIntakeCredential, SourceDelivery
from corridor.source_delivery import (
    DeliveryBinding,
    DeliveryObservation,
    SourceDeliveryRefused,
    delivery_identity_for,
    record_delivery,
    stored_delivery,
    take_delivery,
)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    row = Project(
        slug=f"delivery-{uuid4().hex[:12]}", name="Delivery", is_synthetic=True
    )
    session.add(row)
    session.flush()
    return row


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
    """A pulled delivery has no credential and a pushed one always does."""

    with pytest.raises(SourceDeliveryRefused, match="pull or push"):
        DeliveryBinding(
            customer="acme-utilities",
            project_id=project.id,
            project_slug=project.slug,
            transport="carrier-pigeon",
            channel="shared-files",
            configuration_identity="txdot-rid-box-v1",
        )
    with pytest.raises(SourceDeliveryRefused, match="bound credential"):
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
