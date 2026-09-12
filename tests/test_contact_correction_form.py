"""Correct a shown project contact from its follow-up bundle (#838).

#562 built contact resolution and an authenticated correction endpoint, and it
shipped with no editor: the customer-journey audit of 2026-09-10 found "contact
correction has an API but no visible editor". A partner who sees a wrong contact
on a follow-up bundle should not have to raise a separate request to fix it. So
the bundle now carries a small attributable correction form beside a *resolved*
contact, and this file proves the properties that make such a form dangerous to
get wrong rather than the ones that are easy to assert:

- the correction is visible on the *current* bundle, and an as-of reading before
  it still names the contact recorded at the time -- a correction appends, it
  never rewrites history;
- a duplicate submission converges rather than appending twice (idempotent);
- a stale correction cannot overwrite a newer one;
- another project's contact cannot be reached through a borrowed identifier;
- a role-only follow-up stays valid, with no correction form and no fabricated
  person or address.

The form is not a second write path. It carries the request-forgery token and
posts the corrected contact, a reason and an idempotency key to the one
authoritative endpoint (`web_boundary`'s first manifest entry, driven as JSON by
`project_contacts_cli` and the browser alike). The steps here drive that endpoint
under a *real* magic-link session with the token, reading the payload out of the
rendered form rather than composing it.

Nothing here reads a clock beyond the one instant the fixture declares.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.access import COORDINATION, enroll_member
from corridor.config import settings
from corridor.follow_up_bundles import (
    CONTACT_UNRESOLVED,
    read_follow_up_bundles,
)
from corridor.models import ExternalOrg, Project
from corridor.operating_mode import adopt_project_baseline
from corridor.principals import HumanPrincipal
from corridor.project_contacts import (
    contact_history,
    import_contact_csv,
    resolve_contact,
)
from corridor.push_intake import (
    PushCredential,
    PushPayload,
    accept_delivery,
    bind_credential,
    register_push_credential,
)
from corridor.web import auth
from corridor.web.app import app, get_review_clock, get_session

import journey_matrix
from browser_session_support import form_fields, page_without_shell, sign_in
from journey_harness import Step, run_scenario
from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    register_baseline,
    register_source_row,
    subject,
)


COORDINATOR = HumanPrincipal("local:coordinator")
COORDINATOR_EMAIL = "coordinator@example.test"
OPERATOR = HumanPrincipal("local:operator")

WATER = "City Water"
POWER = "Northline Power"
ROLE = "the responsible contact"

OLD_EMAIL = "old-mains@citywater.example"
NEW_EMAIL = "mains@citywater.example"

FIRST = 42

# One instant the whole file is bound to, an hour ahead of every write it makes,
# so a bundle read as-of it sees the import and any correction. A commitment
# inside the declared horizon is what raises a bundle at all.
NOW = datetime.now(timezone.utc)
CUTOFF = NOW + timedelta(hours=1)
PROMISED = (CUTOFF.date() + timedelta(days=10)).isoformat()

# The ten fields a `ContactInput` carries, and the exact top-level shape the
# correction endpoint accepts around them.
CONTACT_FIELDS = (
    "source_contact_id", "organization_ref", "responsible_role", "person_name",
    "channel", "address", "effective_from", "effective_until",
    "external_system", "external_id",
)


def _contact_csv(*, org, source_contact_id, person, channel, address):
    return (
        "source_contact_id,organization_ref,responsible_role,person_name,channel,address\n"
        f"{source_contact_id},{org},{ROLE},{person},{channel},{address}\n"
    ).encode()


def _adopt_with_commitment(session: Session, *, org: str, member: bool = True) -> Project:
    """One adopted project with an accepted External Organization and a date.

    The commitment trigger raises a bundle addressed to `org`; whether that
    bundle names a person or only the role is then decided by whether a contact
    is imported for it. `member=False` leaves the coordinator off the roster,
    for a project this signed-in person may not open.
    """

    project = Project(
        slug=f"contact-correction-{uuid4().hex[:8]}",
        name="Contact correction",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    if session.scalar(select(ExternalOrg).where(ExternalOrg.name == org)) is None:
        session.add(ExternalOrg(name=org))
        session.flush()
    if member:
        enroll_member(
            session,
            project_id=project.id,
            email=COORDINATOR_EMAIL,
            principal=COORDINATOR,
            display_name="Coordinator",
            designations=[COORDINATION],
            operator=OPERATOR,
        )
    source = Rendition(session, project, "ucm-2026-08.xlsx")
    org_fact, _ = source.capture(
        fact_type="external_org", value=org, subject_key=subject(FIRST)
    )
    date_fact, _ = source.capture(
        fact_type="committed_date", value=PROMISED, subject_key=subject(FIRST)
    )
    revision = accept_baseline_fact(session, project, org_fact, date_fact)
    baseline = register_baseline(session, project, source.document, revision)
    register_source_row(
        session, project, baseline, row_number=FIRST, business_identity="U-042"
    )
    adopt_project_baseline(
        session,
        project_id=project.id,
        adopted_by_principal="local:adopter",
        baseline_source_sha256=source.document.sha256,
        importer_identity="contact_correction_fixture",
        importer_version="v1",
        idempotency_key=f"adopt:{uuid4().hex[:10]}",
    )
    session.expire_all()
    return project


def _import_contact(
    session: Session,
    project: Project,
    *,
    org: str,
    address: str,
    source_contact_id: str = "lead",
    person: str = "Pat",
    channel: str = "email",
) -> int:
    """Import one contact through the bound-delivery path, and return its id."""

    register_push_credential(
        session, customer="fixture", project=project, channel="webhook",
        material=project.slug,
    )
    binding = bind_credential(
        session, PushCredential(channel="webhook", material=project.slug)
    )
    delivery = accept_delivery(
        session,
        binding,
        PushPayload(
            body=_contact_csv(
                org=org, source_contact_id=source_contact_id, person=person,
                channel=channel, address=address,
            ),
            filename="contacts.csv",
        ),
    )
    import_contact_csv(
        session, delivery.envelope, import_identity=f"contacts:{uuid4().hex[:8]}"
    )
    session.expire_all()
    return contact_history(session, project_id=project.id)[-1].id


@pytest.fixture
def sender():
    return auth.RecordingEmailSender()


@pytest.fixture
def browser(session, sender, tmp_path):
    """Sign in the real magic-link way, so the token check is the deployed one.

    A factory rather than a signed-in client, because the email must be enrolled
    before a link is issued and the project setup does the enrolling: the test
    builds its projects, then asks for the browser.
    """

    settings_store = settings.corpus_store
    settings.corpus_store = str(tmp_path / "store")

    def request_session():
        # Production gives each request its own transaction; the shared
        # rollback-scoped session does not, so a request that a database refusal
        # aborts -- a stale or borrowed-identifier correction -- would poison the
        # next one. A per-request savepoint is the whole fix (#938's review says
        # so in as many words): a refused request gives up only its savepoint,
        # and a successful one releases it, leaving the test's own transaction
        # intact for the assertions that follow.
        savepoint = session.begin_nested()
        try:
            yield session
        except Exception:
            if savepoint.is_active:
                savepoint.rollback()
            raise
        else:
            if savepoint.is_active:
                savepoint.commit()

    app.dependency_overrides[get_session] = request_session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    app.dependency_overrides[get_review_clock] = lambda: (lambda: CUTOFF)
    try:
        with ExitStack() as browsers:

            def signed_in(email: str = COORDINATOR_EMAIL) -> TestClient:
                client = browsers.enter_context(
                    TestClient(app, base_url="https://testserver")
                )
                sign_in(client, sender, email)
                return client

            yield signed_in
    finally:
        app.dependency_overrides.clear()
        settings.corpus_store = settings_store


def _correction_form(body: str) -> dict[str, str] | None:
    """The hidden fields of the one contact-correction form on the page.

    The visible person, channel and address a person edits are not here, exactly
    as `browser_session_support` intends: these are the fields the page supplies,
    and a caller adds the typed ones itself.
    """

    return form_fields(body, "/correct")


def _payload(fields: dict[str, str], *, person: str, channel: str, address: str, reason: str):
    """The JSON body the browser builds from the rendered form plus what is typed."""

    contact = {name: fields.get(name, "") for name in CONTACT_FIELDS}
    contact["person_name"] = person
    contact["channel"] = channel
    contact["address"] = address
    return {
        "contact": contact,
        "reason": reason,
        "idempotency_key": fields["idempotency_key"],
    }


def _post(client: TestClient, project: Project, contact_id: int, fields, payload):
    return client.post(
        f"/projects/{project.slug}/contacts/{contact_id}/correct",
        json=payload,
        headers={auth.CSRF_HEADER: fields["csrf_token"]},
    )


# --- the rendered form ------------------------------------------------------


def test_a_resolved_bundle_offers_a_correction_form_with_the_token_and_identity(
    session, browser
):
    """The form carries the record's identity to preserve and the forgery token."""

    project = _adopt_with_commitment(session, org=WATER)
    contact_id = _import_contact(session, project, org=WATER, address=OLD_EMAIL)
    client = browser()

    response = client.get(f"/work/{project.slug}")
    assert response.status_code == 200, response.text[:300]
    body = response.text

    fields = _correction_form(body)
    assert fields is not None, "a resolved bundle rendered no correction form"
    # The request-forgery field a signed-in write must echo.
    assert fields.get("csrf_token")
    # Identity the correction may not change is carried back unchanged.
    assert fields["source_contact_id"] == "lead"
    assert fields["organization_ref"] == WATER
    assert fields["responsible_role"] == ROLE
    # A per-render idempotency key, so a double-submit of this form converges.
    assert fields["idempotency_key"]
    # The form posts to the one authoritative endpoint for this contact.
    assert f"/projects/{project.slug}/contacts/{contact_id}/correct" in body
    # The current person and address are shown for editing, and the retained
    # predecessor identity is named on the form.
    assert OLD_EMAIL in body and "Pat" in body
    assert f"contact record {contact_id}" in body


# --- the capability, walked over #848's harness -----------------------------


@dataclass
class Correction:
    """What one walk of the contact-correction capability carries between steps."""

    session: Session
    project: Project
    client: TestClient
    contact_id: int
    foreign: Project
    foreign_contact_id: int
    role_only: Project | None = None
    carried: dict[str, object] = field(default_factory=dict)


def _bundle_body(walk: Correction) -> str:
    response = walk.client.get(f"/work/{walk.project.slug}")
    assert response.status_code == 200, response.text[:300]
    return response.text


def _step_correct_a_shown_contact(walk: Correction) -> None:
    fields = _correction_form(_bundle_body(walk))
    assert fields is not None, "the resolved bundle offered no correction form"
    payload = _payload(
        fields, person="Pat", channel="email", address=NEW_EMAIL,
        reason="The address on file bounces; use the mains inbox.",
    )
    posted = _post(walk.client, walk.project, walk.contact_id, fields, payload)
    assert posted.status_code == 200, (
        "a coordinator could not correct a shown contact: " + posted.text[:300]
    )
    assert posted.json()["corrected_by"] == COORDINATOR.subject
    assert posted.json()["record"]["address"] == NEW_EMAIL
    walk.carried["fields"] = fields
    walk.carried["payload"] = payload
    walk.session.expire_all()

    # Visible on the current bundle; the superseded address is gone from it.
    current = _bundle_body(walk)
    assert NEW_EMAIL in current and OLD_EMAIL not in current

    # History is kept: predecessor and successor both readable, and an as-of
    # reading before the correction still names the contact recorded then.
    history = contact_history(walk.session, project_id=walk.project.id)
    assert len(history) == 2, "a correction did not append beside its predecessor"
    predecessor, successor = history
    assert predecessor.values_json["address"] == OLD_EMAIL
    assert successor.corrects_id == predecessor.id
    before = resolve_contact(
        walk.session, project_id=walk.project.id,
        organization_ref=WATER, responsible_role=ROLE, as_of=predecessor.recorded_at,
    )
    after = resolve_contact(
        walk.session, project_id=walk.project.id,
        organization_ref=WATER, responsible_role=ROLE, as_of=successor.recorded_at,
    )
    assert before.address == OLD_EMAIL, "an old reading was rewritten by a later correction"
    assert after.address == NEW_EMAIL, "the current reading did not follow the correction"


def _step_a_duplicate_correction_converges(walk: Correction) -> None:
    posted = _post(
        walk.client, walk.project, walk.contact_id,
        walk.carried["fields"], walk.carried["payload"],
    )
    assert posted.status_code == 200, (
        "resubmitting the same correction was refused: " + posted.text[:300]
    )
    walk.session.expire_all()
    history = contact_history(walk.session, project_id=walk.project.id)
    assert len(history) == 2, "a duplicate submission appended a second correction"


def _step_a_stale_correction_is_refused(walk: Correction) -> None:
    # A correction of the original record, with a fresh key: a newer record now
    # exists, so the predecessor is stale and nothing may overwrite through it.
    fields = dict(walk.carried["fields"])
    fields["idempotency_key"] = f"correct-contact:{uuid4().hex}"
    payload = _payload(
        fields, person="Sam", channel="email", address="someone-else@citywater.example",
        reason="A stale page tried to correct the superseded record.",
    )
    posted = _post(walk.client, walk.project, walk.contact_id, fields, payload)
    assert posted.status_code == 409, (
        "a stale correction was not refused: "
        f"{posted.status_code} {posted.text[:300]}"
    )
    walk.session.expire_all()
    assert len(contact_history(walk.session, project_id=walk.project.id)) == 2


def _step_a_borrowed_identifier_is_refused(walk: Correction) -> None:
    fields = dict(walk.carried["fields"])
    fields["idempotency_key"] = f"correct-contact:{uuid4().hex}"
    payload = _payload(
        fields, person="Pat", channel="email", address=NEW_EMAIL,
        reason="Reaching another project's contact through this one's slug.",
    )
    posted = _post(walk.client, walk.project, walk.foreign_contact_id, fields, payload)
    assert posted.status_code in (404, 409), (
        "a borrowed contact id from another project was not refused: "
        f"{posted.status_code} {posted.text[:300]}"
    )
    walk.session.expire_all()
    # The other project's contact was untouched: still one record, no correction.
    foreign_history = contact_history(walk.session, project_id=walk.foreign.id)
    assert len(foreign_history) == 1 and foreign_history[0].corrects_id is None


def _step_a_role_only_bundle_needs_no_person(walk: Correction) -> None:
    reading = read_follow_up_bundles(
        walk.session, project_id=walk.role_only.id, as_of=CUTOFF
    )
    assert reading.bundles, "the fixture raised no role-only bundle to render"
    assert all(
        bundle.recipient.contact_state == CONTACT_UNRESOLVED
        for bundle in reading.bundles
    ), "the fixture resolved a contact where it meant to leave the role only"
    response = walk.client.get(f"/work/{walk.role_only.slug}")
    assert response.status_code == 200, response.text[:300]
    page = page_without_shell(response.text)
    assert _correction_form(page) is None, "a role-only bundle offered a correction form"
    assert "/correct" not in page, "a role-only bundle named the correction endpoint"
    assert "no contact is recorded for this role" in page


# The correction lifecycle, all on one project: #680 holds one
# project-authorization scope per transaction, so a single web session walks one
# project's routes. The role-only property is a different project's, walked as
# its own one-step scenario below; both are declared steps of this capability.
LIFECYCLE_STEPS: tuple[Step, ...] = (
    Step(
        name="correct_a_shown_contact",
        sentence="the coordinator corrects a shown contact with a reason, sees it "
        "on the current bundle, and the record it corrects stays readable",
        owner="#838",
        run=_step_correct_a_shown_contact,
    ),
    Step(
        name="a_duplicate_correction_converges",
        sentence="resubmitting the same correction converges rather than appending "
        "a second one",
        owner="#838",
        run=_step_a_duplicate_correction_converges,
    ),
    Step(
        name="a_stale_correction_is_refused",
        sentence="a correction of a record a newer one already superseded is refused, "
        "overwriting nothing",
        owner="#838",
        run=_step_a_stale_correction_is_refused,
    ),
    Step(
        name="a_borrowed_identifier_is_refused",
        sentence="another project's contact cannot be reached through this project's "
        "slug and a borrowed identifier",
        owner="#838",
        run=_step_a_borrowed_identifier_is_refused,
    ),
)

ROLE_ONLY_STEP = Step(
    name="a_role_only_bundle_needs_no_person",
    sentence="a role-only follow-up is rendered in full, with no correction form "
    "and no fabricated person or address",
    owner="#838",
    run=_step_a_role_only_bundle_needs_no_person,
)

#: The declared steps of this selected capability, for the #848 inventory check.
CONTACT_CORRECTION_STEPS: tuple[Step, ...] = LIFECYCLE_STEPS + (ROLE_ONLY_STEP,)


@pytest.fixture
def walk(session, browser) -> Correction:
    project = _adopt_with_commitment(session, org=WATER)
    contact_id = _import_contact(session, project, org=WATER, address=OLD_EMAIL)
    # Another project entirely, with a contact whose id is valid there and
    # nowhere else -- the borrowed identifier the endpoint must refuse. The
    # coordinator is deliberately not a member of it.
    foreign = _adopt_with_commitment(session, org=WATER, member=False)
    foreign_contact_id = _import_contact(
        session, foreign, org=WATER, address="elsewhere@citywater.example",
    )
    return Correction(
        session=session,
        project=project,
        client=browser(),
        contact_id=contact_id,
        foreign=foreign,
        foreign_contact_id=foreign_contact_id,
    )


@pytest.fixture
def role_only_walk(session, browser) -> Correction:
    role_only = _adopt_with_commitment(session, org=POWER)  # no contact imported
    return Correction(
        session=session,
        project=role_only,
        client=browser(),
        contact_id=0,
        foreign=role_only,
        foreign_contact_id=0,
        role_only=role_only,
    )


def test_the_contact_correction_capability_runs_as_a_declared_scenario(walk):
    """Walk the correction lifecycle and report every step against its ticket.

    Run with ``-rP`` to read the report on a passing run; a failing run carries
    the whole report in its message.
    """

    report = run_scenario(
        "Contact correction (#838, a selected capability)",
        LIFECYCLE_STEPS,
        walk,
    )
    print("\n" + report.render(), flush=True)
    assert not report.failures, (
        "a step nothing said could fail did:\n" + report.render()
    )
    assert len(report.passed) == len(LIFECYCLE_STEPS), (
        "this capability is built, so every step of it passes:\n" + report.render()
    )


def test_a_role_only_follow_up_stays_valid_as_a_declared_step(role_only_walk):
    """The role-only property, walked as its own one-step scenario (#562, #838)."""

    report = run_scenario(
        "Contact correction, role-only follow-up (#838)",
        (ROLE_ONLY_STEP,),
        role_only_walk,
    )
    print("\n" + report.render(), flush=True)
    assert not report.failures and len(report.passed) == 1, (
        "a role-only follow-up is not valid as declared:\n" + report.render()
    )


def test_every_contact_correction_row_names_a_step_of_this_scenario():
    """A selected-capability row for #838 that no step exercises is a claim (#848)."""

    declared = {step.name for step in CONTACT_CORRECTION_STEPS}
    ours = {
        row.scenario
        for row in journey_matrix.SELECTED_CAPABILITIES
        if "#838" in row.owner
    }
    assert ours and ours <= declared, (
        "these #838 rows name a step that does not exist: "
        + ", ".join(sorted(ours - declared))
    )
    workflows = {
        row.scenario
        for row in journey_matrix.SELECTED_CAPABILITY_WORKFLOWS
        if "#838" in row.owner
    }
    assert workflows <= declared, (
        "these #838 workflows name a step that does not exist: "
        + ", ".join(sorted(workflows - declared))
    )
