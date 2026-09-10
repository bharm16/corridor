"""Project contact import and resolution through the public commands (#562)."""

from datetime import date, timedelta
from uuid import uuid4
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.models import Dependency, ExternalOrg, Project
from corridor.push_intake import PushCredential, PushPayload, accept_delivery, bind_credential, register_push_credential
from corridor.principals import HumanPrincipal

from corridor.project_contacts import read_contact_csv


def test_csv_accounts_for_every_row_and_does_not_guess_missing_addresses():
    parsed = read_contact_csv(
        b"source_contact_id,organization_ref,responsible_role,person_name,channel,address,effective_from,extra\n"
        b"lead,Utility A,coordination lead,Pat,email,pat@example.test,2026-01-01,retained\n"
        b"backup,Utility A,backup,Sam,,,,\n"
        b"bad,Utility A,coordination lead,Jo,email,not an address,,\n"
    )
    assert len(parsed.rows) == 3
    assert parsed.rows[0].record.address == "pat@example.test"
    assert parsed.rows[0].record.effective_from == date(2026, 1, 1)
    assert parsed.rows[1].record.address is None
    assert parsed.rows[2].reason == "invalid_address"
    assert parsed.unknown_columns == ("extra",)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))


def project_and_source(session, raw, *, name="Utility A"):
    project = Project(slug=f"contacts-{uuid4().hex[:8]}", name="Contact fixture", is_synthetic=True)
    organization = session.scalar(select(ExternalOrg).where(ExternalOrg.name == name))
    if organization is None:
        organization = ExternalOrg(name=name)
        session.add(organization)
    session.add(project)
    session.flush()
    session.add(Dependency(project_id=project.id, ref_code="UC-1", title="Utility relocation",
                           dep_type="utility_relocation", external_org_id=organization.id))
    register_push_credential(session, customer="fixture-customer", project=project,
                            channel="webhook", material=project.slug)
    binding = bind_credential(session, PushCredential(channel="webhook", material=project.slug))
    receipt = accept_delivery(session, binding, PushPayload(body=raw, filename="contacts.csv"))
    return project, receipt.envelope


def test_import_correction_and_cutoff_preserve_original_contacts(session):
    from corridor.project_contacts import import_contact_csv, contact_history, correct_contact, resolve_contact, ContactInput

    raw = (b"source_contact_id,organization_ref,responsible_role,person_name,channel,address\n"
           b"lead,Utility A,coordination lead,Pat,email,pat@example.test\n")
    project, envelope = project_and_source(session, raw)
    receipt = import_contact_csv(session, envelope, import_identity="directory-v1")
    [original] = contact_history(session, project_id=project.id)
    before = original.recorded_at + timedelta(microseconds=1)
    resolved = resolve_contact(session, project_id=project.id, organization_ref="Utility A",
        responsible_role="coordination lead", as_of=before)
    assert (resolved.state, resolved.person_name, resolved.address) == ("resolved", "Pat", "pat@example.test")
    changed = correct_contact(session, project_id=project.id, contact_id=original.id,
        replacement=ContactInput("lead", "Utility A", "coordination lead", "Pat", "email", "new@example.test"),
        principal=HumanPrincipal("local:contact-owner"), reason="Onboarding correction", idempotency_key="fix-address")
    assert resolve_contact(session, project_id=project.id, organization_ref="Utility A",
        responsible_role="coordination lead", as_of=changed.recorded_at + timedelta(seconds=1)).address == "new@example.test"
    assert resolve_contact(session, project_id=project.id, organization_ref="Utility A",
        responsible_role="coordination lead", as_of=before).address == "pat@example.test"
    assert import_contact_csv(session, envelope, import_identity="directory-v1").id == receipt.id
    assert len(contact_history(session, project_id=project.id)) == 2


@pytest.mark.parametrize("values,reason", [
    ("role,Utility A,lead,,,,,", "incomplete_contact"),
    ("old,Utility A,lead,Pat,email,pat@example.test,2020-01-01,2021-01-01", "not_effective"),
    ("future,Utility A,lead,Pat,email,pat@example.test,2099-01-01,", "not_effective"),
])
def test_incomplete_expired_and_future_contacts_stay_unresolved(session, values, reason):
    from corridor.project_contacts import import_contact_csv, resolve_contact

    project, envelope = project_and_source(session,
        ("source_contact_id,organization_ref,responsible_role,person_name,channel,address,effective_from,effective_until\n" + values + "\n").encode())
    imported = import_contact_csv(session, envelope, import_identity="contacts")
    assert resolve_contact(session, project_id=project.id, organization_ref="Utility A",
        responsible_role="lead", as_of=imported.recorded_at + timedelta(seconds=1)).reason == reason


def test_adopted_workbook_uses_its_registered_contact_mapping_and_bundle_resolution(session):
    from io import BytesIO
    from openpyxl import Workbook
    from corridor.baseline_adoption import preview_baseline_adoption, adopt_baseline
    from corridor.contact_mapping import ContactMapping
    from corridor.field_mapping_manifest import MappingDeclaration
    from corridor.source_intake import validate_and_stage
    from corridor.project_contacts import import_adopted_contacts, contact_history
    from corridor.follow_up_bundles import read_follow_up_bundles, bundle_reading_payload

    project = Project(slug=f"contact-ucm-{uuid4().hex[:8]}", name="Mapped contacts", is_synthetic=True)
    organization = ExternalOrg(name=f"Utility {project.slug}")
    session.add_all([project, organization])
    session.flush()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict ID", "Utility Owner", "Utility Type", "Promised For", "Utility Conflict Description"])
    sheet.append(["UC-1", organization.name, "Water", "2020-01-01", "Relocate main"])
    contacts = workbook.create_sheet("Contacts")
    contacts.append(["Contact ID", "Organization", "Role", "Person", "Channel", "Address"])
    contacts.append(["lead", organization.name, "the responsible contact", "Pat", "email", "pat@example.test"])
    stream = BytesIO()
    workbook.save(stream)
    staged = validate_and_stage(stream.getvalue(), "ucm.xlsx")
    mapping = ContactMapping("Contacts", 1, (
        ("source_contact_id", "Contact ID"), ("organization_ref", "Organization"),
        ("responsible_role", "Role"), ("person_name", "Person"), ("channel", "Channel"), ("address", "Address")))
    preview = preview_baseline_adoption(session, project=project, staged=staged,
        customer="fixture-customer", source_identity="adopted-ucm",
        field_mapping=MappingDeclaration(contact_mapping=mapping))
    adopt_baseline(session, preview=preview, principal=HumanPrincipal("local:contact-owner"), idempotency_key="adopt")
    receipt = import_adopted_contacts(session, project_id=project.id, import_identity="ucm-contacts")
    [contact] = contact_history(session, project_id=project.id)
    assert contact.source_locators["address"]["cell_range"] == "F2"
    reading = read_follow_up_bundles(session, project_id=project.id, as_of=contact.recorded_at + timedelta(seconds=1))
    [bundle] = reading.bundles
    assert (bundle.recipient.contact_name, bundle.recipient.channel, bundle.recipient.address) == ("Pat", "email", "pat@example.test")
    assert contact.id in bundle.recipient.contact_record_ids
    assert bundle_reading_payload(reading)["bundles"][0]["recipient"]["address"] == "pat@example.test"
    assert import_adopted_contacts(session, project_id=project.id, import_identity="ucm-contacts").id == receipt.id


def test_phone_conflicts_unknown_organizations_and_duplicate_accounting(session):
    from corridor.project_contacts import import_contact_csv, resolve_contact

    raw = (b"source_contact_id,organization_ref,responsible_role,person_name,channel,address\n"
           b"phone,Utility A,field lead,Alex,phone,+17135550123\n"
           b"a,Utility A,lead,Pat,email,pat@example.test\n"
           b"b,Utility A,lead,Lee,email,lee@example.test\n"
           b"unknown,Unregistered Utility,lead,Lee,email,lee@example.test\n"
           b"dup,Utility A,backup,Sam,email,sam@example.test\n"
           b"dup,Utility A,backup,Sam,email,sam@example.test\n"
           b"conflict,Utility A,backup,Sam,email,one@example.test\n"
           b"conflict,Utility A,backup,Sam,email,two@example.test\n")
    project, envelope = project_and_source(session, raw)
    receipt = import_contact_csv(session, envelope, import_identity="mixed")
    as_of = receipt.recorded_at + timedelta(seconds=1)
    phone = resolve_contact(session, project_id=project.id, organization_ref="Utility A", responsible_role="field lead", as_of=as_of)
    assert (phone.person_name, phone.channel, phone.address) == ("Alex", "phone", "+17135550123")
    assert resolve_contact(session, project_id=project.id, organization_ref="Utility A", responsible_role="lead", as_of=as_of).reason == "competing_contacts"
    assert resolve_contact(session, project_id=project.id, organization_ref="Unregistered Utility", responsible_role="lead", as_of=as_of).reason == "unknown_organization"
    assert [row["reason"] for row in receipt.accounting_json["rows"]] == [None, None, None, None, None, "duplicate_row", "conflicting_duplicate_identifier", "conflicting_duplicate_identifier"]


def test_contact_envelope_refuses_cross_customer_and_changed_mapping_replay(session):
    from sqlalchemy.exc import IntegrityError
    from corridor.project_contacts import import_contact_csv
    from corridor.source_delivery import SourceDeliveryRefused

    project, envelope = project_and_source(session,
        b"source_contact_id,organization_ref,responsible_role\nlead,Utility A,lead\n")
    with pytest.raises(SourceDeliveryRefused):
        import_contact_csv(session, replace(envelope, customer="foreign"), import_identity="same")
    import_contact_csv(session, envelope, import_identity="same", source_family="directory")
    with pytest.raises(IntegrityError, match="different content"), session.begin_nested():
        import_contact_csv(session, envelope, import_identity="same", source_family="different-directory")


def test_a_refused_replacement_blocks_the_old_contact_until_corrected(session):
    from sqlalchemy.exc import IntegrityError
    from corridor.project_contacts import ContactInput, import_contact_csv, contact_history, correct_contact, resolve_contact

    header = b"source_contact_id,organization_ref,responsible_role,person_name,channel,address\n"
    project, envelope = project_and_source(session, header + b"lead,Utility A,lead,Pat,email,pat@example.test\n")
    import_contact_csv(session, envelope, import_identity="v1", source_family="directory")
    [original] = contact_history(session, project_id=project.id)
    cutoff = original.recorded_at + timedelta(microseconds=1)
    binding = bind_credential(session, PushCredential(channel="webhook", material=project.slug))
    newer = accept_delivery(session, binding, PushPayload(body=header + b"lead,,,Pat,email,broken address\n", filename="contacts.csv"))
    imported = import_contact_csv(session, newer.envelope, import_identity="v2", source_family="directory")
    assert imported.accounting_json["rows"][0]["reason"] == "missing_required_identity"
    [_, refusal] = contact_history(session, project_id=project.id)
    assert resolve_contact(session, project_id=project.id, organization_ref="Utility A", responsible_role="lead",
                           as_of=refusal.recorded_at + timedelta(seconds=1)).reason == "refused_contact_revision"
    assert resolve_contact(session, project_id=project.id, organization_ref="Utility A", responsible_role="lead", as_of=cutoff).address == "pat@example.test"
    replacement = ContactInput("lead", "Utility A", "lead", "Pat", "email", "new@example.test")
    with pytest.raises(IntegrityError, match="stale"), session.begin_nested():
        correct_contact(session, project_id=project.id, contact_id=original.id, replacement=replacement,
                        principal=HumanPrincipal("local:owner"), reason="Fix", idempotency_key="stale")
    fixed = correct_contact(session, project_id=project.id, contact_id=refusal.id, replacement=replacement,
                            principal=HumanPrincipal("local:owner"), reason="Fix", idempotency_key="current")
    assert resolve_contact(session, project_id=project.id, organization_ref="Utility A", responsible_role="lead",
                           as_of=fixed.recorded_at + timedelta(seconds=1)).address == "new@example.test"


def test_contact_crash_retry_converges(runtime_database):
    from corridor.project_contacts import import_contact_csv, contact_history

    factory = runtime_database.session_factory
    with factory() as setup:
        project, envelope = project_and_source(setup,
            b"source_contact_id,organization_ref,responsible_role\nlead,Utility A,lead\n")
        project_id = project.id
        setup.commit()
    with factory() as crashed:
        import_contact_csv(crashed, envelope, import_identity="retry")
        crashed.rollback()
    with factory() as retry:
        first = import_contact_csv(retry, envelope, import_identity="retry")
        first_id = first.id
        retry.commit()
    with factory() as reread:
        assert import_contact_csv(reread, envelope, import_identity="retry").id == first_id
        assert len(contact_history(reread, project_id=project_id)) == 1


def test_contact_correction_uses_the_authenticated_project_member(runtime_database):
    import os
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool
    from corridor.project_contacts import import_contact_csv, contact_history
    from corridor.web.app import app, get_session, get_human_principal
    from access_support import seed_membership
    from minutes_fixture_support import adopted_project

    actor = HumanPrincipal("local:authenticated-contact-owner")
    with runtime_database.session_factory.begin() as setup:
        project = adopted_project(setup)
        register_push_credential(setup, customer="fixture", project=project, channel="webhook", material=project.slug)
        binding = bind_credential(setup, PushCredential(channel="webhook", material=project.slug))
        delivery = accept_delivery(setup, binding, PushPayload(body=b"source_contact_id,organization_ref,responsible_role\nlead,Utility A,lead\n", filename="contacts.csv"))
        import_contact_csv(setup, delivery.envelope, import_identity="http")
        [contact] = contact_history(setup, project_id=project.id)
        seed_membership(setup, project, actor)
        slug, contact_id = project.slug, contact.id
    web_url = make_url(settings.database_url).set(database=runtime_database.name, username="corridor_web",
                                                 password=os.environ.get("CORRIDOR_WEB_DB_PASSWORD") or "corridor_web")
    web_engine = create_engine(web_url, poolclass=NullPool)

    def web_session():
        with Session(web_engine) as session:
            yield session

    app.dependency_overrides[get_session] = web_session
    app.dependency_overrides[get_human_principal] = lambda: actor
    try:
        with TestClient(app) as client:
            result = client.post(f"/projects/{slug}/contacts/{contact_id}/correct", json={
                "contact": {"source_contact_id": "lead", "organization_ref": "Utility A", "responsible_role": "lead",
                            "person_name": "Pat", "channel": "email", "address": "pat@example.test"},
                "reason": "Onboarding", "idempotency_key": "http-correction"})
            assert result.status_code == 200, result.text
            assert result.json()["corrected_by"] == actor.subject
            assert result.json()["record"]["address"] == "pat@example.test"
    finally:
        app.dependency_overrides.clear()
        web_engine.dispose()
