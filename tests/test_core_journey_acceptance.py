"""The core customer journey, written in full and run for real (#848, #849).

#849 is the journey this file walks: *a provisioned but unadopted project and a
person who has not signed in* at one end, *that person retrieving the exact
approved package and coming back for the next cycle* at the other. Every step
between them is written out below in the order the audit wrote it, whether or
not the product can do it yet.

**The environment is the deployment's, not a test's.** The application answers
on the enforced live-pilot boundary, reading as the real ``corridor_web``
login against a migrated database of this run's own, with committed
transactions -- because the journey spans an HTTP act, a background worker
that renders outside any transaction, and a later HTTP act that reads what the
worker committed. Identity is the real magic-link flow: nothing overrides
``get_human_principal``, so every request below is authorized by a cookie a
consumed link established, and every unsafe request has to carry the token the
page gave it. The two declared seams are the ones the audit allows: a
controlled clock, and a mail delivery that records instead of sending.

**What is deliberately still here.** ``tests/test_issue_path_end_to_end.py``
proves the inner segment -- confirmed coverage to authorized package -- and it
overrides identity to do so. It is not replaced by this file and must not be:
until the steps below reach that segment through the product, it is the only
proof that segment works at all. #849 is where the two become one walk.

**How to read a run.** The report prints one sentence per step, and a step the
product cannot do yet names the ticket that owes it. ``-rP`` is what shows it
on a passing run -- xdist keeps a worker's output to itself otherwise -- and a
failing run carries the whole report in its message:

    TEST_WORKERS=2 make test-focused \\
      ARGS="tests/test_core_journey_acceptance.py -rP"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import NullPool

from corridor import access
from corridor.config import settings
from corridor.due_work import enqueue_due_work, run_due_work_once
from corridor.models import Project, ReleaseCandidate, ReleasePackage
from corridor.object_storage import content_store
from corridor.operating_mode import ADOPTED_BASELINE, project_operating_mode
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import app, get_review_clock, get_session

import journey_matrix
from journey_harness import (
    BLOCKED,
    ControlledClock,
    EXPECTED_FAIL,
    PASSED,
    Step,
    run_scenario,
)
from later_revision_support import BASELINE_ROWS, workbook_bytes


OPERATOR = HumanPrincipal("local:operations")
COORDINATOR = HumanPrincipal("local:journey-coordinator")
RELEASER = HumanPrincipal("local:journey-releaser")
COORDINATOR_EMAIL = "coordinator@example.test"
RELEASER_EMAIL = "releaser@example.test"

# The deployed web login's own credential, read the way every other
# real-login test reads it.
WEB_PASSWORD = os.environ.get("CORRIDOR_WEB_DB_PASSWORD") or "corridor_web"

# One declared timeline. Nothing below reads a wall clock: the cutoff, every
# request instant and every runtime tick come from the controlled clock.
SIGN_IN_AT = datetime(2026, 4, 6, 8, 0, tzinfo=timezone.utc)
WORKER_AT = datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc)
NEXT_CYCLE_AT = datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc)


# --- the journey's own context ---------------------------------------------


@dataclass
class Journey:
    """What one walk of the journey carries from step to step."""

    client: TestClient
    sender: auth.RecordingEmailSender
    clock: ControlledClock
    factory: Any
    slug: str
    workbook: Path
    #: What a step hands the next one: a candidate id, a package id, a
    #: delivery receipt. A step reads only what an earlier step put here, so
    #: nothing below reaches into the database for a value the product was
    #: supposed to have shown it.
    carried: dict[str, Any] = field(default_factory=dict)


def prose(body: str) -> str:
    """The page as a reader receives it, with markup escaping undone."""

    return html.unescape(body)


_FORM = re.compile(
    r'<form[^>]*action="(?P<action>[^"]*)"[^>]*>(?P<body>.*?)</form>', re.S
)
_HIDDEN = re.compile(
    r'<input[^>]*type="hidden"[^>]*name="(?P<name>[^"]*)"[^>]*value="(?P<value>[^"]*)"'
)


def rendered_form(body: str, action_suffix: str) -> dict[str, str] | None:
    """The hidden fields of the one form on this page with this action.

    Taken out of the rendered page rather than composed, for the reason
    ``tests/test_issue_path_end_to_end.py`` gives: a hand-written payload
    proves the route accepts something, and only the page's own fields prove
    the screen offers something a person could submit. Under a real session
    that includes the request-forgery token, which is exactly the field #821
    found missing.
    """

    for match in _FORM.finditer(body):
        if match.group("action").endswith(action_suffix):
            return {
                found.group("name"): html.unescape(found.group("value"))
                for found in _HIDDEN.finditer(match.group("body"))
            }
    return None


# --- the steps, in the order the journey happens in -------------------------


def step_sign_in(journey: Journey) -> None:
    landing = journey.client.get("/", follow_redirects=False)
    assert landing.status_code == 303 and landing.headers["location"] == "/sign-in", (
        "a person with no session must be sent to sign in, not served a page"
    )

    requested = journey.client.post(
        "/sign-in/request", data={"email": COORDINATOR_EMAIL}, follow_redirects=False
    )
    assert requested.status_code == 200, requested.text
    assert journey.sender.sent, "no sign-in link was delivered"

    _address, link = journey.sender.sent[-1]
    token = parse_qs(urlsplit(link).query)["token"][0]
    consumed = journey.client.get(
        "/sign-in/consume", params={"token": token}, follow_redirects=False
    )
    assert consumed.status_code == 303, consumed.text
    assert journey.client.cookies.get(auth.SESSION_COOKIE), (
        "consuming the link established no session cookie"
    )


def step_find_the_project(journey: Journey) -> None:
    page = journey.client.get("/", follow_redirects=False)
    assert page.status_code == 200, page.text
    readable = prose(page.text)
    assert journey.slug in readable, (
        "the signed-in person's own project list does not name the project "
        "they were enrolled on"
    )


def step_read_onboarding_state(journey: Journey) -> None:
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert page.status_code == 200, (
        "opening a provisioned, unadopted project answered "
        f"{page.status_code}; a coordinator has no way to see what Corridor "
        "needs next to start it"
    )
    readable = prose(page.text)
    assert "baseline" in readable.lower(), (
        "the project opened, but says nothing about supplying the baseline"
    )


def step_supply_the_baseline(journey: Journey) -> None:
    form = journey.client.get(
        f"/projects/{journey.slug}/sources/upload", follow_redirects=False
    )
    assert form.status_code == 200, (
        "the upload screen answered "
        f"{form.status_code}: it is not served inside the enforced boundary"
    )
    fields = rendered_form(form.text, "/sources/upload") or {}
    submitted = journey.client.post(
        f"/projects/{journey.slug}/sources/upload",
        data=fields,
        files={"file": (journey.workbook.name, journey.workbook.read_bytes())},
        follow_redirects=False,
    )
    assert submitted.status_code in (200, 201, 303), submitted.text
    journey.carried["baseline_delivery"] = prose(submitted.text)


def step_operations_resolves_mechanics(journey: Journey) -> None:
    register = journey.client.get(
        f"/projects/{journey.slug}/sources", follow_redirects=False
    )
    assert register.status_code == 200, register.text
    readable = prose(register.text)
    assert "Technical Operations" in readable or "operations" in readable.lower(), (
        "a delivery Corridor cannot place names no accountable owner, so the "
        "coordinator is shown work with no way to complete it"
    )


def step_coordinator_answers_and_adopts(journey: Journey) -> None:
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert page.status_code == 200, page.text
    adoption = rendered_form(page.text, "/baseline/adopt")
    assert adoption is not None, (
        "no Adopt Baseline control is offered anywhere in the product; "
        "adoption has no production caller"
    )
    adopted = journey.client.post(
        f"/projects/{journey.slug}/baseline/adopt", data=adoption
    )
    assert adopted.status_code in (200, 201, 303), adopted.text
    with journey.factory() as reading:
        project = reading.scalars(
            select(Project).where(Project.slug == journey.slug)
        ).one()
        assert (
            project_operating_mode(reading, int(project.id)) == ADOPTED_BASELINE
        ), (
            "the baseline was adopted without moving the project into adopted "
            "baseline operating mode"
        )


def step_approve_the_issue_configuration(journey: Journey) -> None:
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert page.status_code == 200, page.text
    configuration = rendered_form(page.text, "/issue/configuration")
    assert configuration is not None, (
        "the set of artifacts this project issues cannot be reviewed or "
        "approved in the product"
    )
    approved = journey.client.post(
        f"/work/{journey.slug}/issue/configuration", data=configuration
    )
    assert approved.status_code in (200, 201, 303), approved.text


def step_submit_a_later_revision(journey: Journey) -> None:
    later = journey.workbook.parent / "later-ucm.xlsx"
    workbook_bytes(later, BASELINE_ROWS)
    form = journey.client.get(
        f"/projects/{journey.slug}/sources/upload", follow_redirects=False
    )
    assert form.status_code == 200, form.text
    fields = rendered_form(form.text, "/sources/upload") or {}
    submitted = journey.client.post(
        f"/projects/{journey.slug}/sources/upload",
        data=fields,
        files={"file": (later.name, later.read_bytes())},
        follow_redirects=False,
    )
    assert submitted.status_code in (200, 201, 303), submitted.text


def step_see_receipt_and_processing_state(journey: Journey) -> None:
    register = journey.client.get(
        f"/projects/{journey.slug}/sources", follow_redirects=False
    )
    assert register.status_code == 200, register.text
    readable = prose(register.text)
    assert "later-ucm.xlsx" in readable, (
        "the register does not show the revision that was just submitted"
    )


def step_inspect_exact_source_context(journey: Journey) -> None:
    review = journey.client.get(f"/review/{journey.slug}", follow_redirects=False)
    assert review.status_code == 200, review.text
    link = re.search(
        rf'href="(/review/{re.escape(journey.slug)}/source[^"]*)"', review.text
    )
    assert link is not None, "Review offers no link to the exact source passage"
    source = journey.client.get(html.unescape(link.group(1)), follow_redirects=False)
    assert source.status_code == 200, (
        f"the source link printed on Review answered {source.status_code}"
    )


def step_review_routine_changes(journey: Journey) -> None:
    review = journey.client.get(f"/review/{journey.slug}", follow_redirects=False)
    assert review.status_code == 200, review.text
    decision = rendered_form(review.text, f"/review/{journey.slug}")
    assert decision is not None, "Review offers no decision form for this packet"
    applied = journey.client.post(
        f"/review/{journey.slug}", data={**decision, "outcome": "apply"}
    )
    assert applied.status_code in (200, 201), applied.text


def step_report_an_extraction_error(journey: Journey) -> None:
    review = journey.client.get(f"/review/{journey.slug}", follow_redirects=False)
    assert review.status_code == 200, review.text
    report = rendered_form(review.text, "/extraction-error")
    assert report is not None, (
        "there is no way to say a capture is wrong at the source, so a wrong "
        "extraction can only be worked around"
    )


def step_undo_one_decision(journey: Journey) -> None:
    review = journey.client.get(f"/review/{journey.slug}", follow_redirects=False)
    assert review.status_code == 200, review.text
    undo = rendered_form(review.text, "/undo")
    assert undo is not None, (
        "a decision that was just recorded offers no receipt and no undo"
    )
    undone = journey.client.post(f"/review/{journey.slug}/undo", data=undo)
    assert undone.status_code in (200, 201), undone.text


def step_prepare_the_issue(journey: Journey) -> None:
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert page.status_code == 200, page.text
    confirmation = rendered_form(page.text, "/issue/prepare")
    assert confirmation is not None, (
        "the Issue section offers no coverage confirmation to submit"
    )
    assert auth.CSRF_FIELD in confirmation, (
        "the rendered Prepare form carries no request-forgery token, so a real "
        "browser submission is refused"
    )
    requested = journey.client.post(
        f"/work/{journey.slug}/issue/prepare", data=confirmation
    )
    assert requested.status_code == 202, requested.text
    assert "Preparing this issue" in prose(requested.text)


def step_worker_prepares_the_candidate(journey: Journey) -> None:
    journey.clock.advance_to(WORKER_AT)
    with journey.factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=journey.clock.now())
    result = run_due_work_once(
        journey.factory, clock=journey.clock, owner="runtime:journey-harness"
    )
    assert result is not None, (
        "the confirmed request was never published as an occurrence, so no "
        "deployed worker would ever run it"
    )
    assert result.execution_outcome == "completed", result.error_code
    assert result.handler_result["outcome"] == "prepared", result.handler_result
    journey.carried["candidate_id"] = int(result.handler_result["candidate_id"])
    with journey.factory() as reading:
        assert reading.get(ReleaseCandidate, journey.carried["candidate_id"])


def step_inspect_the_actual_artifacts(journey: Journey) -> None:
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert page.status_code == 200, page.text
    link = re.search(
        r'href="([^"]*candidates?/\d+[^"]*)"', page.text
    )
    assert link is not None, (
        "the candidate awaiting approval offers no way to look at the actual "
        "bytes it holds"
    )
    artifact = journey.client.get(
        html.unescape(link.group(1)), follow_redirects=False
    )
    assert artifact.status_code == 200, artifact.text


def step_approve_as_the_designated_releaser(journey: Journey) -> None:
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    approval = rendered_form(page.text, "/issue/authorize")
    assert approval is not None, "no approval is offered for the prepared issue"
    assert auth.CSRF_FIELD in approval, (
        "the rendered Approve form carries no request-forgery token"
    )
    refused = journey.client.post(
        f"/work/{journey.slug}/issue/authorize", data=approval
    )
    assert refused.status_code == 403, (
        "the coordinator who asked for the issue was allowed to release it; "
        "preparing an issue is not releasing one"
    )

    sign_in_as(journey, RELEASER_EMAIL)
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    approval = rendered_form(page.text, "/issue/authorize")
    assert approval is not None, (
        "the designated releaser is offered no approval on the surface they "
        "were already on"
    )
    authorized = journey.client.post(
        f"/work/{journey.slug}/issue/authorize", data=approval
    )
    assert authorized.status_code == 201, authorized.text
    with journey.factory() as reading:
        package = reading.scalars(select(ReleasePackage)).one()
        journey.carried["package_id"] = int(package.id)


def step_download_the_approved_package(journey: Journey) -> None:
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert page.status_code == 200, page.text
    link = re.search(r'href="([^"]*package[^"]*download[^"]*)"', page.text)
    assert link is not None, (
        "the issue is described as approved and sent, and nothing on the "
        "screen retrieves the bytes that were approved"
    )
    downloaded = journey.client.get(
        html.unescape(link.group(1)), follow_redirects=False
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content, "the approved package downloaded as no bytes"
    journey.carried["package_bytes"] = downloaded.content


def step_submit_a_second_revision(journey: Journey) -> None:
    journey.clock.advance_to(NEXT_CYCLE_AT)
    sign_in_as(journey, COORDINATOR_EMAIL)
    second = journey.workbook.parent / "second-later-ucm.xlsx"
    workbook_bytes(second, BASELINE_ROWS)
    form = journey.client.get(
        f"/projects/{journey.slug}/sources/upload", follow_redirects=False
    )
    assert form.status_code == 200, form.text
    fields = rendered_form(form.text, "/sources/upload") or {}
    submitted = journey.client.post(
        f"/projects/{journey.slug}/sources/upload",
        data=fields,
        files={"file": (second.name, second.read_bytes())},
        follow_redirects=False,
    )
    assert submitted.status_code in (200, 201, 303), submitted.text


def step_retrieve_the_earlier_package_unchanged(journey: Journey) -> None:
    record = journey.client.get(f"/record/{journey.slug}", follow_redirects=False)
    assert record.status_code == 200, record.text
    link = re.search(r'href="([^"]*package[^"]*download[^"]*)"', record.text)
    assert link is not None, (
        "the Record view shows no package history from which the earlier "
        "approved package can be retrieved"
    )
    again = journey.client.get(html.unescape(link.group(1)), follow_redirects=False)
    assert again.status_code == 200, again.text
    assert again.content == journey.carried["package_bytes"], (
        "the earlier package came back with different bytes than were approved"
    )


#: #849's journey, in its own order. ``owner`` on a passing step is the ticket
#: that made it pass; on a waiting step it is the ticket that owes it.
CORE_JOURNEY_STEPS: tuple[Step, ...] = (
    Step(
        name="sign_in",
        sentence="A person who has not signed in receives a link and signs in",
        owner="#331",
        run=step_sign_in,
    ),
    Step(
        name="find_the_project",
        sentence="They find the project they were enrolled on",
        owner="#331",
        run=step_find_the_project,
    ),
    Step(
        name="read_onboarding_state",
        sentence="They open it and read what Corridor needs next to start it",
        owner="#827",
        run=step_read_onboarding_state,
        expected_to_fail=True,
    ),
    Step(
        name="supply_the_baseline",
        sentence="They supply the customer's UCM workbook as the baseline",
        owner="#823 and #824",
        run=step_supply_the_baseline,
        expected_to_fail=True,
    ),
    Step(
        name="operations_resolves_mechanics",
        sentence="Technical Operations resolves the mechanics of that delivery",
        owner="#842",
        run=step_operations_resolves_mechanics,
        expected_to_fail=True,
    ),
    Step(
        name="coordinator_answers_and_adopts",
        sentence="The coordinator answers the material questions and adopts "
        "the baseline",
        owner="#827",
        run=step_coordinator_answers_and_adopts,
        expected_to_fail=True,
    ),
    Step(
        name="approve_the_issue_configuration",
        sentence="They review and approve what this project issues",
        owner="#828",
        run=step_approve_the_issue_configuration,
        expected_to_fail=True,
    ),
    Step(
        name="submit_a_later_revision",
        sentence="They submit a later UCM revision",
        owner="#823, #824 and #825",
        run=step_submit_a_later_revision,
        expected_to_fail=True,
    ),
    Step(
        name="see_receipt_and_processing_state",
        sentence="They see its receipt and its processing state",
        owner="#841",
        run=step_see_receipt_and_processing_state,
        expected_to_fail=True,
    ),
    Step(
        name="inspect_exact_source_context",
        sentence="They read the exact wording at its place in the source",
        owner="#831",
        run=step_inspect_exact_source_context,
        expected_to_fail=True,
    ),
    Step(
        name="review_routine_changes",
        sentence="They review the routine changes the revision proposed",
        owner="#834",
        run=step_review_routine_changes,
        expected_to_fail=True,
    ),
    Step(
        name="report_an_extraction_error",
        sentence="They report one extraction error against its source passage",
        owner="#832 and #836",
        run=step_report_an_extraction_error,
        expected_to_fail=True,
    ),
    Step(
        name="undo_one_decision",
        sentence="They undo one decision they had just recorded",
        owner="#834",
        run=step_undo_one_decision,
        expected_to_fail=True,
    ),
    Step(
        name="prepare_the_issue",
        sentence="They confirm coverage and ask for the issue to be prepared",
        owner="#821",
        run=step_prepare_the_issue,
        expected_to_fail=True,
    ),
    Step(
        name="worker_prepares_the_candidate",
        sentence="The deployed worker claims the request and prepares the candidate",
        owner="#690",
        run=step_worker_prepares_the_candidate,
        expected_to_fail=True,
    ),
    Step(
        name="inspect_the_actual_artifacts",
        sentence="They inspect the actual artifacts the candidate holds",
        owner="#830",
        run=step_inspect_the_actual_artifacts,
        expected_to_fail=True,
    ),
    Step(
        name="approve_as_the_designated_releaser",
        sentence="The designated releaser, and only they, approve it for sharing",
        owner="#821 and #839",
        run=step_approve_as_the_designated_releaser,
        expected_to_fail=True,
    ),
    Step(
        name="download_the_approved_package",
        sentence="They download exactly the approved package",
        owner="#830",
        run=step_download_the_approved_package,
        expected_to_fail=True,
    ),
    Step(
        name="submit_a_second_revision",
        sentence="They come back next cycle and submit a second later revision",
        owner="#823, #824 and #825",
        run=step_submit_a_second_revision,
        expected_to_fail=True,
    ),
    Step(
        name="retrieve_the_earlier_package_unchanged",
        sentence="They retrieve the earlier approved package, unchanged",
        owner="#830",
        run=step_retrieve_the_earlier_package_unchanged,
        expected_to_fail=True,
    ),
)


# --- the environment: the deployment's, with two declared seams -------------


def _web_url(database_name: str) -> str:
    return (
        make_url(settings.database_url)
        .set(database=database_name, username="corridor_web", password=WEB_PASSWORD)
        .render_as_string(hide_password=False)
    )


def sign_in_as(journey: Journey, email: str) -> None:
    """Sign a different person in, through the same real link flow.

    The journey has two people in it, and swapping them is a sign-in rather
    than a dependency override: that is the whole reason the releaser's
    refusal of the coordinator means something.
    """

    journey.client.cookies.clear()
    requested = journey.client.post(
        "/sign-in/request", data={"email": email}, follow_redirects=False
    )
    assert requested.status_code == 200, requested.text
    token = parse_qs(urlsplit(journey.sender.sent[-1][1]).query)["token"][0]
    consumed = journey.client.get(
        "/sign-in/consume", params={"token": token}, follow_redirects=False
    )
    assert consumed.status_code == 303, consumed.text


@pytest.fixture
def journey_store(tmp_path, monkeypatch):
    """The deployment's own content store, rooted where this test discards it."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return content_store()


@pytest.fixture
def provisioned_project(runtime_database):
    """A provisioned, unadopted project and the two people who work on it.

    This is the whole of the setup the journey is allowed: Corridor operations
    provisions the project and enrols the people, exactly as it does before a
    customer's first sign-in. Nothing here adopts a baseline, registers a
    profile or prepares anything -- those are acts the journey has to perform
    through the product, and a fixture that performed them would be the
    "pre-adopting the baseline in a fixture" the audit rules out.
    """

    with runtime_database.session_factory.begin() as owner:
        project = Project(
            slug=f"journey-{uuid4().hex[:8]}",
            name="Journey Project",
            is_synthetic=True,
        )
        owner.add(project)
        owner.flush()
        for principal, email, name, designations in (
            (COORDINATOR, COORDINATOR_EMAIL, "Coordinator", [access.COORDINATION]),
            (RELEASER, RELEASER_EMAIL, "Releaser", [access.EXTERNAL_RELEASE]),
        ):
            access.enroll_member(
                owner,
                project_id=project.id,
                email=email,
                principal=principal,
                display_name=name,
                designations=designations,
                operator=OPERATOR,
            )
        return project.slug


@pytest.fixture
def journey(
    runtime_database, provisioned_project, journey_store, tmp_path, monkeypatch
):
    """The application on the enforced boundary, as the real web login.

    Two overrides and no more. ``get_session`` binds the request to the
    deployed ``corridor_web`` capability rather than to the schema owner the
    rest of the suite reads as, which is what makes the revoke real for every
    request below. The mail sender records. Identity, authorization, the
    domain commands, the dispatch and the worker are the deployment's.
    """

    monkeypatch.setattr(settings, "live_pilot_web_boundary", True)
    web_engine = create_engine(
        _web_url(runtime_database.name), poolclass=NullPool, future=True
    )

    def web_session():
        with OrmSession(bind=web_engine) as opened:
            yield opened

    clock = ControlledClock(SIGN_IN_AT)
    sender = auth.RecordingEmailSender()
    app.dependency_overrides[get_session] = web_session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    app.dependency_overrides[get_review_clock] = lambda: clock.now
    workbook = tmp_path / "baseline-ucm.xlsx"
    workbook_bytes(workbook, BASELINE_ROWS)
    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield Journey(
            client=client,
            sender=sender,
            clock=clock,
            factory=runtime_database.session_factory,
            slug=provisioned_project,
            workbook=workbook,
        )
    app.dependency_overrides.clear()
    web_engine.dispose()


# --- the run ----------------------------------------------------------------


def test_the_core_customer_journey_runs_under_the_declared_seams(journey):
    """Walk #849, and report every step against the ticket that owes it.

    Two things fail this. A step nothing said could fail -- which means
    something that worked has stopped working. And a step still marked as
    waiting on a ticket that now passes -- which means the ticket landed and
    the marking is a lie the next reader would believe.
    """

    report = run_scenario(
        "The core customer journey (#849)", CORE_JOURNEY_STEPS, journey
    )
    print("\n" + report.render(), flush=True)

    assert not report.failures, (
        "a step nothing said could fail did:\n" + report.render()
    )
    assert not report.unexpected_passes, (
        "a step is still marked as waiting on its ticket and now passes; "
        "remove the marking in the change that made it pass:\n" + report.render()
    )
    assert report.outcome_of("sign_in") == PASSED
    assert report.outcome_of("find_the_project") == PASSED

    # The report is the deliverable, so its shape is asserted rather than
    # assumed: every declared step appears once, in order, and the walk stops
    # at exactly one waiting step with the rest reported as not reached.
    assert [one.step.name for one in report.results] == [
        step.name for step in CORE_JOURNEY_STEPS
    ]
    waiting = [one for one in report.results if one.outcome == EXPECTED_FAIL]
    assert len(waiting) == 1, (
        "the journey stops at the first step whose ticket has not landed, and "
        "reports every later step as not reached:\n" + report.render()
    )
    assert all(
        waiting[0].step.owner in one.detail
        for one in report.results
        if one.outcome == BLOCKED
    ), "a step that was not reached does not name what stopped the journey"


# --- the matrix, checked against this scenario ------------------------------


def test_every_matrix_row_names_a_step_of_this_scenario():
    """A row nothing exercises is a claim, not an inventory entry."""

    declared = {step.name for step in CORE_JOURNEY_STEPS}
    unknown = sorted(
        {row.scenario for row in journey_matrix.CORE_JOURNEY} - declared
    )
    assert not unknown, (
        "these matrix rows name a scenario step that does not exist: "
        + ", ".join(unknown)
    )
    unknown_workflows = sorted(
        {row.scenario for row in journey_matrix.CLAIMED_WORKFLOWS} - declared
    )
    assert not unknown_workflows, (
        "these claimed workflows name a scenario step that does not exist: "
        + ", ".join(unknown_workflows)
    )


def test_every_actionable_state_has_an_action_or_an_accountable_handoff():
    """The first thing a code search cannot answer, asked of the inventory."""

    stranded = journey_matrix.rows_without_an_act()
    assert not stranded, "\n".join(
        f"{row.role} in \"{row.state}\" has neither a permitted action nor a "
        f"named handoff ({row.owner})"
        for row in stranded
    )


def test_a_matrix_row_with_no_route_is_a_step_still_waiting_on_its_ticket():
    """An empty route cell is work, and the scenario has to agree it is.

    This is what stops the matrix from becoming a wish: a row may claim a
    route the product does not serve only while the step that exercises it is
    marked as waiting, and the ticket is written on both.
    """

    waiting = {step.name for step in CORE_JOURNEY_STEPS if step.expected_to_fail}
    contradictions = [
        row
        for row in journey_matrix.CORE_JOURNEY
        if not row.route.strip() and row.scenario not in waiting
    ]
    assert not contradictions, "\n".join(
        f"\"{row.action}\" has no route, and its step {row.scenario!r} is not "
        f"marked as waiting on {row.owner}"
        for row in contradictions
    )


def test_every_approved_output_is_retrieved_by_some_row():
    """The third thing a code search cannot answer: bytes nobody can fetch."""

    stranded = journey_matrix.unretrieved_outputs()
    assert not stranded, (
        "the matrix produces these and retrieves none of them: "
        + ", ".join(stranded)
    )


def test_every_half_built_workflow_names_the_ticket_that_finishes_it():
    """A workflow with a writer and no reader is allowed only with a ticket."""

    by_workflow = {row.workflow: row for row in journey_matrix.CLAIMED_WORKFLOWS}
    unowned = [
        workflow
        for workflow, _side in journey_matrix.half_built_workflows()
        if not by_workflow[workflow].owner.strip()
    ]
    assert not unowned, (
        "these claimed workflows are half built and name no ticket: "
        + ", ".join(unowned)
    )
