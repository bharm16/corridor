"""The three frozen legacy forms, driven by a real signed-in browser (#880).

#911 put `{{ csrf_field() }}` on these three forms and widened the architecture
rule that reads every authenticated state-changing form.  That proof is static:
it reads the markup.  #880's first criterion is not -- it asks that a real
signed-in submission of each form *succeeds*, and that a missing or forged
token is refused.  Neither suite that drives these routes could answer it,
because `tests/test_key_dates_web.py` and `tests/test_web.py` both override
`get_human_principal`, which is the dependency that checks the token: under
that override a form carrying the field and a form omitting it submit
identically, which is exactly how three inoperable forms stayed green.  #821
built `browser_session_support` so the next form ticket would not have to build
it again, and this module is that next ticket.

**Where these routes are served.**  None of the three is in the live-pilot
manifest, and none of them joins it to be tested here: `web_boundary` is
untouched.  The deployment that serves a frozen legacy surface is the one whose
boundary state is `NOT_DECLARED` -- the flag off *and* the reads running as a
login `legacy_capabilities` names, which is what says nothing was revoked from
it (ADR-0081, #822).  The first test below declares that mode rather than
inheriting it: it sets the flag explicitly and reads the state back through the
application's own dependencies, on the same bind the requests use.  Every
submission below is answered by that deployment; under the enforced one the
same three routes are 404, which the same test states.

**Why the refusals are submitted before the acceptance.**  Two of these forms
are bound to the state they were rendered against -- the import to the Key Date
Versions the preview showed, the identity decision to a proposal that is still
pending.  Submitted after the accepted one, either would be refused for being
stale, and a refusal earned that way proves nothing about the token.  So each
test walks steps five to seven on the form as rendered, and only then spends it.

**Why the refusal proof counts rather than asserts one absence.**  A row count
over the whole project graph cannot see a value changed in place, and it cannot
see a global registry table at all.  So each refusal here is guarded by
`nothing_written` *and* by a direct reading of the one thing that form would
have written: the previously registered key date keeps its own date, and the
chosen External Party keeps its own alias list -- a column in `external_orgs`,
which carries no project and is therefore outside the graph entirely.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import date
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor import web_boundary
from corridor.access import COORDINATION, enroll_member
from corridor.config import settings
from corridor.dependency_admission import run_dependency_admission
from corridor.extraction_runs import declare_single_run_documents, record_extraction_run
from corridor.milestones import import_xer
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    ExternalOrg,
    Milestone,
    MilestoneRegistration,
    OrganizationIdentityReceipt,
    Project,
)
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import (
    app,
    get_live_pilot_boundary_state,
    get_session,
    get_web_capability,
)

from browser_session_support import form_fields, sign_in, submit_form
from proposal_support import proposal
from record_counts import nothing_written

COORDINATOR = HumanPrincipal("local:legacy-form-coordinator")
COORDINATOR_EMAIL = "legacy-form-coordinator@example.test"
OPERATOR = HumanPrincipal("local:operator")

# The three forms this ticket names, as the routes they post to.
PREVIEW_ROUTE = ("POST", "/key-dates/{slug}/preview")
CONFIRM_ROUTE = ("POST", "/key-dates/{slug}/confirm")
CONFIRM_ORGANIZATION_ROUTE = ("POST", "/candidates/{candidate_id}/confirm-organization")

# One registered key date, then a typed correction of it plus a new one. The
# correction is the point: a refused confirmation that moved this date in place
# leaves every row count identical, so the date itself is read directly.
REGISTERED_DATE = date(2026, 11, 1)
CORRECTED_DATE = date(2027, 3, 15)
NEW_DATE = date(2027, 1, 20)
TYPED_CSV = (
    "code,name,need_date\n"
    f"UTIL-CLEAR,Utility clearance complete,{CORRECTED_DATE}\n"
    f"LET,Letting,{NEW_DATE}\n"
)

STATED_WORDING = "Fresh Spelling Gas Partners"
REGISTERED_PARTY = "AT&T Texas (SWBT)"


def _xer_bytes() -> bytes:
    """A P6 export registering UTIL-CLEAR, the way a structured schedule lands."""

    fields = ["task_id", "proj_id", "task_code", "task_name", "task_type", "target_end_date"]
    lines = [
        "\t".join(["ERMHDR", "19.12", "2026-08-29", "P", "a", "a", "db", "US", "USD"]),
        "\t".join(["%T", "TASK"]),
        "\t".join(["%F", *fields]),
        "\t".join(
            [
                "%R",
                "102",
                "1",
                "UTIL-CLEAR",
                "Utility clearance complete",
                "TT_FinMile",
                f"{REGISTERED_DATE} 00:00",
            ]
        ),
        "%E",
    ]
    return "\n".join(lines).encode("cp1252")


@pytest.fixture
def undeclared_boundary(monkeypatch):
    """Declare the deployment mode that serves a frozen legacy surface.

    Left to the environment this would be whatever a stray variable said. The
    mode is a choice the ticket makes -- the flag off, on a login that kept the
    blanket read -- so it is set here and read back through the application's
    own dependencies in the first test.
    """

    monkeypatch.setattr(settings, "live_pilot_web_boundary", False)


@pytest.fixture
def factory(runtime_database):
    """A migrated database of this test's own, and real committed transactions.

    A signed-in session is established by one request and spent by the next, so
    the rows the sign-in writes have to be committed before the following
    request can read them. That is the harness-owned database, not the
    rollback-scoped session ordinary web tests share.
    """

    return runtime_database.session_factory


@pytest.fixture
def sender():
    """The replaceable mail seam, as a non-sending capture: a link is never sent."""

    return auth.RecordingEmailSender()


@pytest.fixture
def browser(factory, sender, undeclared_boundary):
    """A signed-in browser on this test's database, with identity left alone.

    Only the plumbing is replaced -- the database and the mail seam. In
    particular `get_human_principal` is not, because it is the dependency that
    checks the request-forgery token, and replacing it is what made the static
    proof static. `https` so the Secure cookies round-trip (#821).

    Signing in is a call rather than the fixture's own body because the roster
    this person is enrolled on is written by the scenario fixture, and a link
    can only be minted for an email already bound to a principal.
    """

    def sessions():
        with factory() as one:
            yield one

    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    with ExitStack() as open_browsers:

        def signed_in(email: str = COORDINATOR_EMAIL) -> TestClient:
            made = open_browsers.enter_context(
                TestClient(app, base_url="https://testserver")
            )
            sign_in(made, sender, email)
            return made

        yield signed_in
    app.dependency_overrides.clear()


def _project_with_coordinator(setup) -> Project:
    """One legacy project whose roster carries the signed-in coordinator."""

    project = Project(
        slug=f"legacy-forms-{uuid4().hex[:8]}",
        name="Legacy Forms",
        is_synthetic=True,
    )
    setup.add(project)
    setup.flush()
    enroll_member(
        setup,
        project_id=project.id,
        email=COORDINATOR_EMAIL,
        principal=COORDINATOR,
        display_name="Coordinator",
        designations=[COORDINATION],
        operator=OPERATOR,
    )
    return project


@pytest.fixture
def key_dates_project(factory):
    """A project with one key date already registered from a structured schedule."""

    with factory() as setup:
        project = _project_with_coordinator(setup)
        import_xer(
            setup,
            project_id=project.id,
            content=_xer_bytes(),
            source_name="seg3c2.xer",
        )
        setup.commit()
        return project.id, project.slug


@pytest.fixture
def unresolved_identity(factory):
    """A pending proposal whose External Party spelling no evidence resolves.

    The same scenario `tests/test_web.py` builds for the identity card, on a
    committed database: a matrix source naming a spelling the registry does not
    carry, admitted mechanically, abstaining for one human decision.
    """

    with factory() as setup:
        project = _project_with_coordinator(setup)
        setup.add_all(
            [
                ExternalOrg(name=REGISTERED_PARTY, aliases=[]),
                ExternalOrg(name="City of Houston", aliases=[]),
            ]
        )
        setup.flush()
        matrix = Document(
            project_id=project.id,
            sha256=f"{uuid4().hex}{uuid4().hex}",
            filename="identity-card.pdf",
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        setup.add(matrix)
        setup.flush()
        fields = {
            "utility_id": "PL9",
            "external_org": STATED_WORDING,
            "utility_type": "Petroleum and Gaseous Materials",
            "station_from": "1102+20",
            "station_to": "1102+80",
        }
        quote = " | ".join(fields.values())
        setup.add(DocPage(document_id=matrix.id, page_no=1, text=quote))
        row = proposal(
            matrix,
            fields=fields,
            quote=quote,
            confidence=0.99,
            prompt_version="matrix_v1",
            dedupe=quote,
            model="gpt-test",
        )
        setup.add(row)
        record_extraction_run(
            setup,
            matrix,
            prompt_version="matrix_v1",
            candidate_count=1,
            page_errors=0,
            candidates=(row,),
            model="gpt-test",
            schema_version="matrix_candidate_shape_v1",
            allow_unsealed_legacy=True,
        )
        setup.flush()
        declare_single_run_documents(setup, project.id, principal=COORDINATOR)
        result = run_dependency_admission(setup, project.id)
        assert {item.reason for item in result.abstentions} == {
            "external_org_identity_unresolved"
        }
        party_id = setup.scalar(
            select(ExternalOrg.id).where(ExternalOrg.name == REGISTERED_PARTY)
        )
        made = (project.id, project.slug, int(row.id), int(party_id))
        setup.commit()
        return made


def _refused_twice(client, url: str, fields: dict[str, str]) -> None:
    """Steps five and six: the field dropped, then a wrong one in its place.

    Dropping it is what a form without `csrf_field()` produces, so the first
    attempt is #880's defect itself rather than a hypothetical; the second is
    the cross-site POST the token exists to refuse. Everything else about both
    submissions is the payload the page rendered.
    """

    dropped = {name: value for name, value in fields.items() if name != auth.CSRF_FIELD}
    forged = {**fields, auth.CSRF_FIELD: "forged"}
    for attempt in (dropped, forged):
        answer = submit_form(client, url, attempt)
        assert answer.status_code == 403, (attempt, answer.text)


# --- the deployment these three routes are served by ------------------------


def test_the_three_legacy_forms_are_served_outside_the_pilot_and_join_nothing(
    factory, undeclared_boundary
):
    """The mode is declared and read back, and the manifest is untouched.

    `boundary_state` has three answers, and only one of them serves a route
    outside the pilot set: the flag off, on a login `legacy_capabilities` names
    as having kept the blanket read. That is this deployment, and it is read
    back through `get_web_capability` on the same bind the requests below use
    rather than assumed from the flag alone -- an unreadable or unknown login
    is `INCONSISTENT` since #822, and would refuse these routes with 503.

    The other half of the maintainer's constraint is the one asserted second:
    none of the three routes is in `PILOT_ROUTES`, so nothing was admitted to
    make these tests pass. Under the enforced deployment all three answer 404,
    which is what being outside the manifest means.
    """

    with factory() as reading:
        capability = get_web_capability(reading)
    state = get_live_pilot_boundary_state(capability)

    assert settings.live_pilot_web_boundary is False
    assert capability in web_boundary.legacy_capabilities()
    assert state is web_boundary.BoundaryState.NOT_DECLARED

    for route in (PREVIEW_ROUTE, CONFIRM_ROUTE, CONFIRM_ORGANIZATION_ROUTE):
        assert route not in web_boundary.PILOT_ROUTES, route
        assert web_boundary.route_refusal(state, *route) is None, route
        enforced = web_boundary.route_refusal(
            web_boundary.BoundaryState.ENFORCED, *route
        )
        assert enforced is not None and enforced.status_code == 404, route


# --- form one: preview a hand-typed CSV -------------------------------------


def test_the_key_dates_preview_form_previews_for_a_browser_and_a_forgery_cannot(
    factory, browser, key_dates_project
):
    """The Preview form, as a coordinator submits it.

    Preview's intended result is a reading, not a write: it classifies each
    typed row against what is registered and offers the confirmation. So the
    refusals are told from the acceptance by what came back -- a 403 with no
    preview against a 200 that classifies the change and names the fingerprint
    the confirmation is bound to.
    """

    project_id, slug = key_dates_project
    url = f"/key-dates/{slug}/preview"

    # 1 and 2. Signed in normally, the coordinator fetches the actual page.
    coordinator = browser()
    page = coordinator.get(f"/key-dates/{slug}")
    assert page.status_code == 200
    rendered = form_fields(page.text, url)
    assert rendered is not None, "the page must offer the preview form"
    assert rendered.get(auth.CSRF_FIELD), (
        "the rendered Preview form must carry the request-forgery field; "
        "without it every real coordinator's click is refused with 403"
    )

    # The page's own fields, plus the two a person types into it.
    typed = {**rendered, "source_name": "typed.csv", "content": TYPED_CSV}

    # 5, 6 and 7. The field dropped, then forged. Neither previews, and
    # neither touches the record: not a row of it, and not the registered date.
    with factory() as reading:
        with nothing_written(reading, project_id):
            _refused_twice(coordinator, url, typed)
        assert _registered_date(reading, project_id, "UTIL-CLEAR") == REGISTERED_DATE

    # 3 and 4. Submitted as the page rendered it, it previews -- and still
    # writes nothing, which is this route's whole contract.
    with factory() as reading:
        with nothing_written(reading, project_id):
            previewed = submit_form(coordinator, url, typed)
    assert previewed.status_code == 200, previewed.text
    assert "Changed" in previewed.text
    assert "New key date" in previewed.text
    confirmation = form_fields(previewed.text, f"/key-dates/{slug}/confirm")
    assert confirmation is not None, "a preview offers the confirmation"
    assert confirmation["expected_sha256"] == sha256(TYPED_CSV.encode()).hexdigest(), (
        "the confirmation the preview offers is bound to the bytes that were "
        "typed into it"
    )


# --- form two: confirm the previewed import ---------------------------------


def test_the_key_dates_confirm_form_imports_for_a_browser_and_a_forgery_cannot(
    factory, browser, key_dates_project
):
    """The Confirm form, as a coordinator submits the page it was just shown.

    Every field here comes off the rendered confirmation -- the previewed CSV
    included, which the page carries in a hidden textarea -- so what is proved
    is that the screen offers a submittable payload, not that the route accepts
    a composed one.

    The refusals are guarded twice over. `nothing_written` watches every row of
    every project-scoped table, which is what catches an import landing
    somewhere nobody named; and the registered date is read directly, because
    this import's effect on an existing key date is a value changed in place,
    and no row count can see that.
    """

    project_id, slug = key_dates_project
    preview_url = f"/key-dates/{slug}/preview"
    confirm_url = f"/key-dates/{slug}/confirm"

    # 1 and 2. Signed in normally, on the page, previewing what was typed into
    # it -- which is what renders the confirmation this test is about.
    coordinator = browser()
    page = coordinator.get(f"/key-dates/{slug}")
    typed = {
        **form_fields(page.text, preview_url),
        "source_name": "typed.csv",
        "content": TYPED_CSV,
    }
    previewed = submit_form(coordinator, preview_url, typed)
    assert previewed.status_code == 200, previewed.text

    confirmation = form_fields(previewed.text, confirm_url)
    assert confirmation is not None
    assert confirmation.get(auth.CSRF_FIELD), (
        "the rendered Confirm form must carry the request-forgery field"
    )
    assert confirmation["content"] == TYPED_CSV, (
        "the confirmation carries the previewed CSV itself; a payload missing "
        "it is one the route refuses as incomplete"
    )
    assert set(json.loads(confirmation["expected_predecessors"])) == {
        "UTIL-CLEAR",
        "LET",
    }, "the confirmation is bound to the Key Date Versions the preview showed"

    # 5, 6 and 7. Dropped, then forged. Nothing is imported, and the date
    # already registered is still the one that was registered.
    with factory() as reading:
        with nothing_written(reading, project_id):
            _refused_twice(coordinator, confirm_url, confirmation)
        assert _registered_date(reading, project_id, "UTIL-CLEAR") == REGISTERED_DATE
        assert _registered_date(reading, project_id, "LET") is None

    # 3 and 4. Submitted as the page rendered it, the import lands under the
    # signed-in person.
    imported = submit_form(coordinator, confirm_url, confirmation)
    assert imported.status_code == 303, imported.text
    assert imported.headers["location"].startswith(f"/key-dates/{slug}?imported=")
    with factory() as reading:
        assert _registered_date(reading, project_id, "UTIL-CLEAR") == CORRECTED_DATE
        assert _registered_date(reading, project_id, "LET") == NEW_DATE
        recorded_by = reading.scalars(
            select(MilestoneRegistration.recorded_by)
            .join(
                Milestone,
                Milestone.current_registration_id == MilestoneRegistration.id,
            )
            .where(Milestone.project_id == project_id)
        ).all()
        assert set(recorded_by) == {COORDINATOR.subject}, (
            "each current Key Date Version is recorded under the person who "
            "confirmed it, not the session that carried the click"
        )


def _registered_date(session, project_id: int, code: str) -> date | None:
    """The scheduled date one registered Key date currently carries, or None."""

    return session.scalar(
        select(Milestone.need_date).where(
            Milestone.project_id == project_id, Milestone.code == code
        )
    )


# --- form three: confirm the External Party behind a source spelling --------


def test_the_confirm_organization_form_records_for_a_browser_and_a_forgery_cannot(
    factory, browser, unresolved_identity
):
    """The identity card's one form, as a coordinator submits it.

    The refusal proof needs a reading the project graph cannot take. This form
    writes a receipt inside the project -- which `nothing_written` does watch --
    and appends the confirmed spelling to the chosen party's alias list, which
    lives in `external_orgs`: a customer-wide registry carrying no project, so
    it is outside the graph entirely and no count over the record would ever
    notice. Both are read here, and the proposal is checked to be still pending.
    """

    project_id, slug, candidate_id, party_id = unresolved_identity
    url = f"/candidates/{candidate_id}/confirm-organization"

    # 1 and 2. Signed in normally, the coordinator opens the review queue.
    coordinator = browser()
    page = coordinator.get(f"/queue/{slug}")
    assert page.status_code == 200
    assert STATED_WORDING in page.text
    rendered = form_fields(page.text, url)
    assert rendered is not None, "the identity card must offer its form"
    assert rendered.get(auth.CSRF_FIELD), (
        "the rendered Confirm organization form must carry the "
        "request-forgery field"
    )
    assert rendered["slug"] == slug

    # The card's hidden fields, plus the choice a person makes on it.
    assert f'<option value="{party_id}">' in page.text, (
        "the party chosen below has to be one the card actually offered"
    )
    chosen = {**rendered, "external_org_id": str(party_id), "facility_classes": "Telecom"}

    # 5, 6 and 7. Dropped, then forged. No receipt, no alias, still pending.
    with factory() as reading:
        with nothing_written(reading, project_id):
            _refused_twice(coordinator, url, chosen)
        assert _aliases(reading, party_id) == []
        assert _candidate_state(reading, candidate_id) == "pending"

    # 3 and 4. Submitted as the card rendered it, the registry decision is
    # recorded under the signed-in person, and the spelling joins the party.
    confirmed = submit_form(coordinator, url, chosen)
    assert confirmed.status_code == 303, confirmed.text
    assert confirmed.headers["location"] == f"/queue/{slug}"
    with factory() as reading:
        receipt = reading.scalars(
            select(OrganizationIdentityReceipt).where(
                OrganizationIdentityReceipt.candidate_id == candidate_id
            )
        ).one()
        assert receipt.external_org_id == party_id
        assert receipt.recorded_by == COORDINATOR.subject
        assert receipt.stated_wording == STATED_WORDING
        assert receipt.facility_classes_json == ["Telecom"]
        assert _aliases(reading, party_id) == [STATED_WORDING]
        assert _candidate_state(reading, candidate_id) == "pending", (
            "the decision is a registry one; the proposal stays for the "
            "ordinary admission path"
        )


def _aliases(session, party_id: int) -> list[str]:
    """The confirmed source spellings one registered External Party carries."""

    return list(
        session.scalar(select(ExternalOrg.aliases).where(ExternalOrg.id == party_id))
        or []
    )


def _candidate_state(session, candidate_id: int) -> str:
    return session.scalar(select(Candidate.state).where(Candidate.id == candidate_id))
