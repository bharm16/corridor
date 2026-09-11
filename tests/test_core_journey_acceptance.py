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

**Where this run stops today, and why nothing here is marked as allowed to
fail.** Every ticket #849 was blocked by has merged, so a step that does not
pass is no longer a ticket outstanding -- it is a defect in something that was
delivered, and marking it expected-to-fail would file it under a ticket that is
closed. Three were found by walking this journey as the deployed web login,
and each is invisible to the tests that own its seam because those tests
replace the identity, the session, or both:

1. ``POST /projects/{slug}/baseline/prepare`` answers 500 on an enforcing
   deployment. ``baseline_adoption._refuse_nonempty_project_record`` counts
   legacy ``dependencies`` rows (``src/corridor/baseline_adoption.py`` 1349),
   and the boundary revokes that relation from ``corridor_web``
   (``src/corridor/web_boundary.py`` 56). The route is in the pilot manifest
   (``web_boundary.py`` 1202) and its recorded relation set does not name
   ``dependencies``. This is where the walk below stops.
2. No page in the product renders a control for that route. ``onboarding.html``
   links to the upload and, once a reading exists, renders the adoption form;
   nothing offers the act in between, though ``onboarding_view`` computes
   ``may_prepare`` (``src/corridor/web/onboarding_view.py`` 250) and no
   template reads it.
3. ``POST /review/{slug}/correction`` answers 500 for a report it recorded.
   The route commits (``src/corridor/web/app.py`` 6673) and then reads
   ``recorded.id`` to compose the receipt sentence (``app.py`` 6690); the
   project partition is declared with ``set_config(..., true)`` and is
   therefore transaction-local (``src/corridor/access.py`` 1944), so after the
   commit the row is invisible and SQLAlchemy raises ``ObjectDeletedError``.

A fourth is user-visible but not fatal to the walk: on an adopted project the
intake preview renders a confirmation form carrying only its six hidden
fields, and ``POST /projects/{slug}/sources/confirm`` refuses exactly that
payload with 400 ``completeness must be one of [...]``. The steps below supply
the declaration a person would type, and say so where they do.

**How to read a run.** The report prints one sentence per step, and a step the
product cannot do yet names the ticket that owes it. ``-rP`` is what shows it
on a passing run -- xdist keeps a worker's output to itself otherwise -- and a
failing run carries the whole report in its message:

    TEST_WORKERS=2 make test-focused \\
      ARGS="tests/test_core_journey_acceptance.py -rP"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import html
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import NullPool

from corridor import access, capture_correction, processing_holds, source_register
from corridor.baseline_adoption import BASELINE_DOC_TYPE
from corridor.config import settings
from corridor.delta_generation import (
    COMPARISON_RULE_VERSION,
    DeltaGenerationDeclaration,
)
from corridor.due_work import (
    HANDLER_RELEASE_PREPARATION,
    ProjectProcessingDeclaration,
    ReleasePreparationDeclaration,
    ReportPreparationDeclaration,
    configure_due_work,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.models import (
    DocPage,
    Document,
    Project,
    ReleaseCandidate,
    ReleasePackage,
)
from corridor.object_storage import content_store
from corridor.onboarding_authorization import (
    ADOPT_BASELINE,
    ONBOARDING_OPERATIONS,
    completed_act,
    record_onboarding_grant,
)
from corridor.operating_mode import ADOPTED_BASELINE, project_operating_mode
from corridor.source_revision_declaration import COMPLETE_ENUMERATION, REPLACES
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import app, get_review_clock, get_session

from browser_session_support import form_fields, sign_in, submit_form
import journey_matrix
import test_manifest_page_links
from journey_harness import (
    BLOCKED,
    ControlledClock,
    EXPECTED_FAIL,
    PASSED,
    Step,
    run_scenario,
)
from later_revision_support import BASELINE_ROWS, workbook_bytes


PROVISIONER = HumanPrincipal("local:provisioner")
OPERATIONS_ACTOR = "operations:journey"
COORDINATOR = HumanPrincipal("local:journey-coordinator")
RELEASER = HumanPrincipal("local:journey-releaser")
OPERATOR = HumanPrincipal("local:journey-operations")
COORDINATOR_EMAIL = "coordinator@example.test"
RELEASER_EMAIL = "releaser@example.test"
OPERATOR_EMAIL = "operations@example.test"

# The deployed web login's own credential, read the way every other
# real-login test reads it.
WEB_PASSWORD = os.environ.get("CORRIDOR_WEB_DB_PASSWORD") or "corridor_web"

# One declared timeline. Nothing below reads a wall clock: the cutoff, every
# request instant and every runtime tick come from the controlled clock.
SIGN_IN_AT = datetime(2026, 4, 6, 8, 0, tzinfo=timezone.utc)
WORKER_AT = datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc)
NEXT_CYCLE_AT = datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc)

# When this project's standing schedules begin.
SCHEDULES_FROM = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)

# The date the coordinator says they will come back to the change they defer.
RETURN_DATE = date(2026, 5, 4)

#: The later revision the customer sends: the same three conflicts, with an
#: ordinary change to the size recorded on two of them. Two is the smallest
#: number this journey can walk on -- one change is decided in Review and the
#: other is the capture an extraction error is reported against, and a
#: coordinator who had to settle a change before they could say Corridor read
#: it wrong would have no way to report one at all. A burst of them belongs to
#: the ticket that batches one (#527).
LATER_ROWS = [
    [*BASELINE_ROWS[0][:3], "16 in", *BASELINE_ROWS[0][4:]],
    [*BASELINE_ROWS[1][:3], "10 in", *BASELINE_ROWS[1][4:]],
    *BASELINE_ROWS[2:],
]

#: And next cycle's, so the second reporting cycle reads a revision rather
#: than the same bytes twice.
SECOND_LATER_ROWS = [
    [*BASELINE_ROWS[0][:3], "18 in", *BASELINE_ROWS[0][4:]],
    *BASELINE_ROWS[1:],
]

#: What only the coordinator can say about a later revision, chosen from the
#: choices the confirmation offers. A complete enumeration that replaces the
#: revision before it is what a customer's weekly UCM export is.
REVISION_DECLARATION = {
    "revision_identity": "UCM workbook revision D",
    "completeness": COMPLETE_ENUMERATION,
    "revision_relationship": REPLACES,
}


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
# --- the steps, in the order the journey happens in -------------------------


_CHECKED = re.compile(
    r'<input[^>]*type="(?:radio|checkbox)"[^>]*name="(?P<name>[^"]*)"[^>]*'
    r'value="(?P<value>[^"]*)"[^>]*\bchecked\b'
)
_REPEATED = re.compile(
    r'<input[^>]*type="hidden"[^>]*name="(?P<name>[^"]*)"[^>]*value="(?P<value>[^"]*)"'
)


def chosen(body: str) -> dict[str, str]:
    """The radio and checkbox controls the page rendered as already chosen.

    Read out of the markup for the reason ``form_fields`` reads the hidden
    inputs: a payload this file invents proves the route accepts something,
    and only the page's own controls prove a person could have sent it.
    """

    return {
        found.group("name"): html.unescape(found.group("value"))
        for found in _CHECKED.finditer(body)
    }


def repeated(body: str, action_suffix: str, name: str) -> list[str]:
    """Every value of one repeated hidden field on the page's own form.

    ``form_fields`` answers with a mapping, which is the right answer for a
    form whose fields are distinct and the wrong one for the issue
    configuration: its approval re-emits the selection as one hidden input per
    artifact, and a mapping would keep the last and silently approve a
    narrower configuration than the page printed.
    """

    for match in _FORM.finditer(body):
        if match.group("action").endswith(action_suffix):
            return [
                html.unescape(one.group("value"))
                for one in _REPEATED.finditer(match.group("body"))
                if one.group("name") == name
            ]
    return []


def form_on(journey: Journey, path: str, action_suffix: str) -> tuple[str, dict[str, str]]:
    """Open a page and take one of its forms, or say which page had none."""

    page = journey.client.get(path, follow_redirects=False)
    assert page.status_code == 200, f"{path} answered {page.status_code}: {page.text}"
    fields = form_fields(page.text, action_suffix)
    assert fields is not None, (
        f"{path} rendered no form whose action ends in {action_suffix!r}"
    )
    return page.text, fields


def deliver(journey: Journey, name: str, rows) -> dict[str, str]:
    """Hand a workbook over through the one control the product offers.

    Returns the confirmation form the preview page rendered, which carries the
    staged digest every later act names the bytes by.
    """

    path = journey.workbook.parent / name
    workbook_bytes(path, rows)
    form = journey.client.get(
        f"/projects/{journey.slug}/sources/upload", follow_redirects=False
    )
    assert form.status_code == 200, (
        f"the upload screen answered {form.status_code}: it is not served "
        "inside the enforced boundary"
    )
    fields = form_fields(form.text, "/sources/upload") or {}
    submitted = journey.client.post(
        f"/projects/{journey.slug}/sources/upload",
        data={**fields, "doc_type": BASELINE_DOC_TYPE},
        files={"upload": (name, path.read_bytes())},
        follow_redirects=False,
    )
    assert submitted.status_code == 200, submitted.text
    confirmation = form_fields(submitted.text, "/sources/confirm")
    assert confirmation is not None, (
        "the preview of what was delivered offers nothing to confirm, so the "
        "bytes stay staged and no document is ever registered"
    )
    return confirmation


def run_the_worker(journey: Journey) -> list[str]:
    """Publish what is due and work every occurrence until none is left.

    The deployed worker, not a shortcut around it: the standing project
    processing and delta generation this project is configured for are what
    turn a registered revision into changes a coordinator can review, and a
    journey that reached into the database for them would be proving its own
    arrangement rather than the deployment's.
    """

    worked: list[str] = []
    for _ in range(12):
        with journey.factory() as ticking:
            with ticking.begin():
                enqueue_due_work(ticking, now=journey.clock.now())
        result = run_due_work_once(
            journey.factory, clock=journey.clock, owner="runtime:journey-harness"
        )
        if result is None:
            return worked
        assert result.execution_outcome == "completed", (
            f"{result.handler_key} did not complete: {result.error_code}"
        )
        worked.append(result.handler_key)
    raise AssertionError("the runtime never ran out of due work")


def open_the_item(journey: Journey) -> str:
    """The Review reading, with its one item opened, as a person opens it.

    Review lists items and opens one at a time on purpose, so everything a
    step below reads -- the exact-source links, the answers, the
    extraction-error control -- exists only on an opened item.
    """

    review = journey.client.get(f"/review/{journey.slug}", follow_redirects=False)
    assert review.status_code == 200, review.text
    link = re.search(
        rf'href="(/review/{re.escape(journey.slug)}\?item=[^"]+)"', review.text
    )
    assert link is not None, (
        "Review offers no item to open, so the revision proposed no change "
        f"a coordinator could review. What it says: {prose(review.text)[:500]}"
    )
    opened = journey.client.get(
        html.unescape(link.group(1)), follow_redirects=False
    )
    assert opened.status_code == 200, opened.text
    return opened.text


_OPTION = re.compile(r'<option value="(?P<value>[^"]*)"')


def answer_fields(body: str) -> dict[str, object]:
    """The answers form as a browser would submit it.

    Every control on this form repeats once per child, so the answers travel
    as lists rather than as single values: a payload carrying one of each
    would save one answer where the page asked for two, and the route pairs
    them by position. What a person chooses is chosen here from the options
    the page printed -- apply for the first change, and a dated return for the
    second, which is the deferral a later step proves survives a correction.
    """

    form = form_fields(body, "/answers")
    assert form is not None, "the opened item renders no answers form"
    deltas = re.findall(r'name="answer_delta" value="([^"]*)"', body)
    offered = [
        _OPTION.findall(one)
        for one in re.findall(
            r'<select[^>]*name="answer_outcome"[^>]*>(.*?)</select>', body, re.S
        )
    ]
    assert deltas and len(deltas) == len(offered), (
        "the answers form asks about a different number of changes than it "
        "offers outcomes for"
    )
    outcomes = ["apply" if index == 0 else "defer" for index in range(len(deltas))]
    for outcome, choices in zip(outcomes, offered):
        assert outcome in choices, (
            f"the page does not offer {outcome!r} for this change"
        )
    return {
        auth.CSRF_FIELD: form[auth.CSRF_FIELD],
        "item_key": form["item_key"],
        "answer_delta": deltas,
        "answer_outcome": outcomes,
        "answer_source": [""] * len(deltas),
        "answer_question": [""] * len(deltas),
        "answer_person": [""] * len(deltas),
        "answer_organization": [""] * len(deltas),
        "answer_return": [
            RETURN_DATE.isoformat() if outcome == "defer" else ""
            for outcome in outcomes
        ],
    }


def step_sign_in(journey: Journey) -> None:
    landing = journey.client.get("/", follow_redirects=False)
    assert landing.status_code == 303 and landing.headers["location"] == "/sign-in", (
        "a person with no session must be sent to sign in, not served a page"
    )

    sign_in(journey.client, journey.sender, COORDINATOR_EMAIL)
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
    # The sentence, not the word: an unadopted project is entitled to one
    # statement of what Corridor needs next, and this is it (#827).
    assert (
        "Supply the customer's UCM workbook as this project's baseline."
        in readable
    ), (
        "the project opened, and does not say what Corridor needs next to "
        f"start it. What it says: {readable[:400]}"
    )
    # And the work is under the limited authorization operations recorded, not
    # under a relaxed activation check: a project with no recorded grant opens
    # on the refusal instead of on this sentence (ADR-0099).
    assert "has not recorded an onboarding authorization" not in readable
    assert f'href="/projects/{journey.slug}/sources/upload"' in page.text, (
        "the page names the act and offers no control that performs it"
    )


def step_supply_the_baseline(journey: Journey) -> None:
    confirmation = deliver(journey, "baseline-ucm.xlsx", BASELINE_ROWS)
    journey.carried["baseline"] = confirmation


def step_operations_resolves_mechanics(journey: Journey) -> None:
    """Technical Operations reads the supplied workbook and settles its shape.

    This is the one request in the journey composed rather than taken from a
    rendered form, and the reason is a finding rather than a convenience: no
    page in the product prints a control whose action is
    ``/projects/{slug}/baseline/prepare``. ``onboarding.html`` offers the
    upload link and, once a reading exists, the adoption form; nothing offers
    the act in between, though ``onboarding_view`` computes ``may_prepare``
    for it and never renders it. So the route is reached the way the route is
    defined, and the step below asserts what the coordinator's page says
    afterwards, which is the part a person does see.
    """

    baseline = journey.carried["baseline"]
    sign_in_as(journey, OPERATOR_EMAIL)
    prepared = journey.client.post(
        f"/projects/{journey.slug}/baseline/prepare",
        data={
            auth.CSRF_FIELD: journey.client.cookies.get(auth.CSRF_COOKIE, ""),
            "sha256": baseline["sha256"],
            "filename": baseline["filename"],
            "source_identity": "UCM workbook revision A",
            "customer": "Lone Star Transit Authority",
        },
        follow_redirects=False,
    )
    assert prepared.status_code == 201, (
        "the supplied workbook cannot be read for adoption on an enforcing "
        f"deployment: the route answered {prepared.status_code}. "
        "baseline_adoption._refuse_nonempty_project_record counts legacy "
        "`dependencies` rows (src/corridor/baseline_adoption.py 1349) and the "
        "boundary revokes that relation from corridor_web "
        "(src/corridor/web_boundary.py 56), so this route raises "
        "InsufficientPrivilege. Its recorded relation set in the pilot "
        "manifest (web_boundary.py 1202) does not name `dependencies`, which "
        "is why nothing caught it: every other test of this route reads as "
        f"the schema owner. What the route answered: {prepared.text[:200]}"
    )
    readable = prose(prepared.text)
    assert "What adopting this would accept" in readable, readable[:400]
    assert "rows become the accepted record" in readable


def step_coordinator_answers_and_adopts(journey: Journey) -> None:
    sign_in_as(journey, COORDINATOR_EMAIL)
    body, adoption = form_on(journey, f"/work/{journey.slug}", "/baseline/adopt")
    readable = prose(body)
    assert "Questions only you can answer" in readable, (
        "the coordinator is asked to adopt without being shown the questions "
        "only they can answer"
    )
    assert auth.CSRF_FIELD in adoption, (
        "the rendered adoption form carries no request-forgery token"
    )
    answers = chosen(body)
    assert answers, "the page raised no material question a coordinator could answer"

    adopted = submit_form(
        journey.client,
        f"/projects/{journey.slug}/baseline/adopt",
        {**adoption, **answers},
    )
    assert adopted.status_code == 201, adopted.text
    assert "The baseline this project accepted" in prose(adopted.text)
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
        # The limited authorization's adoption permission is consumed by the
        # committed act itself, which is what ADR-0099 means by onboarding
        # running under it rather than under a relaxed activation check.
        assert completed_act(
            reading, project_id=int(project.id), operation=ADOPT_BASELINE
        ), "the baseline was adopted without consuming the onboarding permission"
        journey.carried["project_id"] = int(project.id)


def step_approve_the_issue_configuration(journey: Journey) -> None:
    week = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert week.status_code == 200, week.text
    assert f'href="/issue-configuration/{journey.slug}"' in week.text, (
        "the adopted project offers no way to reach what it externally issues"
    )
    opened = journey.client.get(
        f"/issue-configuration/{journey.slug}", follow_redirects=False
    )
    assert opened.status_code == 200, opened.text
    # Prepared through the page's own selection control, opened on what is
    # configured, so preparing without touching a box proposes what the page
    # showed rather than proposing to stop issuing everything.
    selection = [
        value for name, value in _CHECKED.findall(opened.text) if name == "artifact"
    ]
    prepared = journey.client.get(
        f"/issue-configuration/{journey.slug}",
        params={"propose": "1", "artifact": selection},
        follow_redirects=False,
    )
    assert prepared.status_code == 200, prepared.text
    approval = form_fields(prepared.text, "/approve")
    assert approval is not None, (
        "the set of artifacts this project issues cannot be reviewed or "
        "approved in the product"
    )
    approved = submit_form(
        journey.client,
        f"/issue-configuration/{journey.slug}/approve",
        {**approval, "artifact": repeated(prepared.text, "/approve", "artifact")},
    )
    assert approved.status_code == 201, approved.text
    assert "is in force" in prose(approved.text)


def step_submit_a_later_revision(journey: Journey) -> None:
    confirmation = deliver(journey, "later-ucm.xlsx", LATER_ROWS)
    registered = submit_form(
        journey.client,
        f"/projects/{journey.slug}/sources/confirm",
        {**confirmation, **REVISION_DECLARATION},
    )
    assert registered.status_code == 303, registered.text
    journey.carried["later"] = confirmation
    # The standing passes a deployment runs: reading the registered revision,
    # then proposing the differences it states.
    assert "delta_generation" in run_the_worker(journey), (
        "the registered revision was never compared with the accepted record, "
        "so nothing it proposes can reach Review"
    )


def step_see_receipt_and_processing_state(journey: Journey) -> None:
    register = journey.client.get(
        f"/projects/{journey.slug}/sources", follow_redirects=False
    )
    assert register.status_code == 200, register.text
    readable = prose(register.text)
    assert "later-ucm.xlsx" in readable, (
        "the register does not show the revision that was just submitted"
    )
    # Its processing state in the register's own words. "Processed" is the
    # promise the coordinator is owed; a row count is not one (#841).
    assert source_register.STATE_WORDS["processed"] in readable, (
        "the register shows the delivery and says nothing about what became "
        f"of it. What it says: {readable[:600]}"
    )


def step_a_held_source_is_not_read_and_says_so(journey: Journey) -> None:
    """A source prohibited from reading never reaches the parser (#919).

    The register's promise is the load-bearing part. Before #919 a registered,
    unread, held source printed "waiting for the processing pass" even where
    that pass would skip it, which is a queue position the coordinator was
    never in. So this asserts the sentence the register prints, and then that
    the parser really did not run: a prohibition that only changed the wording
    would pass the first half and fail the customer.
    """

    confirmation = deliver(journey, "held-ucm.xlsx", SECOND_LATER_ROWS)
    submitted = submit_form(
        journey.client,
        f"/projects/{journey.slug}/sources/confirm",
        {**confirmation, **REVISION_DECLARATION,
         "revision_identity": "UCM workbook revision D2"},
    )
    assert submitted.status_code == 303, submitted.text

    with journey.factory() as operations:
        with operations.begin():
            document = operations.scalars(
                select(Document).where(
                    Document.project_id == journey.carried["project_id"],
                    Document.sha256 == confirmation["sha256"],
                )
            ).one()
            journey.carried["held_document"] = int(document.id)
            processing_holds.impose_hold(
                operations,
                document_id=int(document.id),
                prohibited_stage=processing_holds.DOCUMENT_READING,
                reason_code=processing_holds.INTAKE_SECURITY_FINDING,
                reason="an intake check refused these bytes for rich reading",
                authority=processing_holds.INTAKE_SECURITY,
                imposed_by="operations:journey",
                evidence="journey-intake-finding-1",
            )

    run_the_worker(journey)

    register = journey.client.get(
        f"/projects/{journey.slug}/sources", follow_redirects=False
    )
    assert register.status_code == 200, register.text
    readable = prose(register.text)
    assert source_register.STATE_WORDS[source_register.READING_HELD] in readable, (
        "a source nobody may read is not reported as held. What the register "
        f"says: {readable[:700]}"
    )
    assert "an intake check refused these bytes for rich reading" in readable, (
        "the register reports a restriction and not the reason its writer "
        "recorded for it"
    )
    with journey.factory() as reading:
        pages = reading.scalar(
            select(func.count())
            .select_from(DocPage)
            .where(DocPage.document_id == journey.carried["held_document"])
        )
    assert pages == 0, (
        f"{pages} pages were written from a source whose reading is "
        "prohibited, so the prohibition changed the wording and not the work"
    )


def step_inspect_exact_source_context(journey: Journey) -> None:
    opened = open_the_item(journey)
    link = re.search(
        rf'href="(/sources/{re.escape(journey.slug)}/passage/\d+)"', opened
    )
    assert link is not None, (
        "the opened item offers no link to the exact wording at its place in "
        "the source"
    )
    source = journey.client.get(link.group(1), follow_redirects=False)
    assert source.status_code == 200, (
        f"the source link printed on Review answered {source.status_code}"
    )


def step_review_routine_changes(journey: Journey) -> None:
    """Answer the changes this revision proposed, on the item that holds them.

    The page offers "Save the answers for these sources" rather than a bare
    Apply: two retained sources answer the same field differently, so the
    disagreement is the decision (ADR-0082). The answers are therefore chosen
    per child from the options the page itself printed, which is also why this
    step cannot post a mapping -- every control repeats once per child, in
    document order.
    """

    opened = open_the_item(journey)
    answers = answer_fields(opened)
    assert answers, "the opened item offers no answer to save"
    saved = journey.client.post(
        f"/review/{journey.slug}/answers", data=answers, follow_redirects=False
    )
    assert saved.status_code in (200, 201), saved.text
    receipt = re.search(
        rf'href="(/review/{re.escape(journey.slug)}/packet/\d+)"', saved.text
    )
    assert receipt is not None, (
        "a decision was recorded and the page offers no receipt to read it "
        f"back from. What it says: {prose(saved.text)[:500]}"
    )
    journey.carried["receipt_url"] = receipt.group(1)


def step_report_an_extraction_error(journey: Journey) -> None:
    opened = open_the_item(journey)
    assert capture_correction.CORRECTION_CONTROL in prose(opened), (
        "there is no way to say a capture is wrong at the source, so a wrong "
        "extraction can only be worked around"
    )
    report = form_fields(opened, "/correction")
    assert report is not None, "the opened item renders no extraction-error form"
    passage = re.search(
        r'<select[^>]*name="correction_passage".*?<option value="(\d+)"[^>]*selected',
        opened,
        re.S,
    )
    assert passage is not None, (
        "the report offers no passage of this source to point the capture at"
    )
    reported = journey.client.post(
        f"/review/{journey.slug}/correction",
        data={
            **report,
            "correction_passage": passage.group(1),
            "correction_interpretation": (
                "this row's size column reads the value the record already holds"
            ),
        },
        follow_redirects=False,
    )
    assert reported.status_code == 200, reported.text
    readable = prose(reported.text)
    assert "are retained as report" in readable, (
        f"the report was not receipted to its reporter: {readable[:500]}"
    )
    assert "The record is unchanged" in readable, (
        "reporting an extraction error was described as changing something"
    )


def step_undo_one_decision(journey: Journey) -> None:
    receipt = journey.client.get(
        journey.carried["receipt_url"], follow_redirects=False
    )
    assert receipt.status_code == 200, receipt.text
    assert "Undo this decision" in prose(receipt.text), (
        "the receipt of a decision just recorded offers no way to undo it"
    )
    undo = form_fields(receipt.text, "/undo")
    assert undo is not None, "the receipt page renders no undo form"
    undone = submit_form(
        journey.client, journey.carried["receipt_url"] + "/undo", undo
    )
    assert undone.status_code == 200, undone.text
    assert "This decision was undone" in prose(undone.text), (
        f"the undo did not report itself: {prose(undone.text)[:400]}"
    )


def step_prepare_the_issue(journey: Journey) -> None:
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert page.status_code == 200, page.text
    confirmation = form_fields(page.text, "/issue/prepare")
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
    """The deployed worker claims the request and prepares the candidate.

    It runs every occurrence that is due rather than one, because by now this
    project has three standing schedules and the deployment does not get to
    choose which is claimed first. Taking the first occurrence and reading it
    as the preparation is how a passing step would depend on the order two
    unrelated schedules happened to publish in.
    """

    journey.clock.advance_to(WORKER_AT)
    prepared = None
    for _ in range(12):
        with journey.factory() as ticking:
            with ticking.begin():
                enqueue_due_work(ticking, now=journey.clock.now())
        result = run_due_work_once(
            journey.factory, clock=journey.clock, owner="runtime:journey-harness"
        )
        if result is None:
            break
        assert result.execution_outcome == "completed", (
            f"{result.handler_key} did not complete: {result.error_code}"
        )
        if result.handler_key == HANDLER_RELEASE_PREPARATION:
            prepared = result
    assert prepared is not None, (
        "the confirmed request was never published as an occurrence, so no "
        "deployed worker would ever run it"
    )
    assert prepared.handler_result["outcome"] == "prepared", prepared.handler_result
    journey.carried["candidate_id"] = int(prepared.handler_result["candidate_id"])
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
    approval = form_fields(page.text, "/issue/authorize")
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
    approval = form_fields(page.text, "/issue/authorize")
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
    link = re.search(r'href="([^"]*/issue/packages/\d+/bundle)"', page.text)
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
    confirmation = deliver(journey, "second-later-ucm.xlsx", SECOND_LATER_ROWS)
    registered = submit_form(
        journey.client,
        f"/projects/{journey.slug}/sources/confirm",
        {**confirmation, **REVISION_DECLARATION,
         "revision_identity": "UCM workbook revision E"},
    )
    assert registered.status_code == 303, registered.text


def step_retrieve_the_earlier_package_unchanged(journey: Journey) -> None:
    record = journey.client.get(f"/record/{journey.slug}", follow_redirects=False)
    assert record.status_code == 200, record.text
    link = re.search(r'href="([^"]*/issue/packages/\d+/bundle)"', record.text)
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
    ),
    Step(
        name="supply_the_baseline",
        sentence="They supply the customer's UCM workbook as the baseline",
        owner="#823 and #824",
        run=step_supply_the_baseline,
    ),
    Step(
        name="operations_resolves_mechanics",
        sentence="Technical Operations resolves the mechanics of that delivery",
        owner="#842",
        run=step_operations_resolves_mechanics,
    ),
    Step(
        name="coordinator_answers_and_adopts",
        sentence="The coordinator answers the material questions and adopts "
        "the baseline",
        owner="#827",
        run=step_coordinator_answers_and_adopts,
    ),
    Step(
        name="approve_the_issue_configuration",
        sentence="They review and approve what this project issues",
        owner="#828",
        run=step_approve_the_issue_configuration,
    ),
    Step(
        name="submit_a_later_revision",
        sentence="They submit a later UCM revision",
        owner="#823, #824 and #825",
        run=step_submit_a_later_revision,
    ),
    Step(
        name="see_receipt_and_processing_state",
        sentence="They see its receipt and its processing state",
        owner="#841",
        run=step_see_receipt_and_processing_state,
    ),
    Step(
        name="a_held_source_is_not_read_and_says_so",
        sentence="A source held against reading never reaches the parser, and "
        "the register says so",
        owner="#919",
        run=step_a_held_source_is_not_read_and_says_so,
    ),
    Step(
        name="inspect_exact_source_context",
        sentence="They read the exact wording at its place in the source",
        owner="#831",
        run=step_inspect_exact_source_context,
    ),
    Step(
        name="review_routine_changes",
        sentence="They review the routine changes the revision proposed",
        owner="#834",
        run=step_review_routine_changes,
    ),
    Step(
        name="report_an_extraction_error",
        sentence="They report one extraction error against its source passage",
        owner="#832 and #836",
        run=step_report_an_extraction_error,
    ),
    Step(
        name="undo_one_decision",
        sentence="They undo one decision they had just recorded",
        owner="#834",
        run=step_undo_one_decision,
    ),
    Step(
        name="prepare_the_issue",
        sentence="They confirm coverage and ask for the issue to be prepared",
        owner="#821",
        run=step_prepare_the_issue,
    ),
    Step(
        name="worker_prepares_the_candidate",
        sentence="The deployed worker claims the request and prepares the candidate",
        owner="#690",
        run=step_worker_prepares_the_candidate,
    ),
    Step(
        name="inspect_the_actual_artifacts",
        sentence="They inspect the actual artifacts the candidate holds",
        owner="#830",
        run=step_inspect_the_actual_artifacts,
    ),
    Step(
        name="approve_as_the_designated_releaser",
        sentence="The designated releaser, and only they, approve it for sharing",
        owner="#821 and #839",
        run=step_approve_as_the_designated_releaser,
    ),
    Step(
        name="download_the_approved_package",
        sentence="They download exactly the approved package",
        owner="#830",
        run=step_download_the_approved_package,
    ),
    Step(
        name="submit_a_second_revision",
        sentence="They come back next cycle and submit a second later revision",
        owner="#823, #824 and #825",
        run=step_submit_a_second_revision,
    ),
    Step(
        name="retrieve_the_earlier_package_unchanged",
        sentence="They retrieve the earlier approved package, unchanged",
        owner="#830",
        run=step_retrieve_the_earlier_package_unchanged,
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

    The journey has three people in it -- the coordinator, Technical
    Operations and the designated releaser -- and swapping them is a sign-in
    rather than a dependency override: that is the whole reason the releaser's
    refusal of the coordinator means something.
    """

    journey.client.cookies.clear()
    sign_in(journey.client, journey.sender, email)


@pytest.fixture
def journey_store(tmp_path, monkeypatch):
    """The deployment's own content store, rooted where this test discards it."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return content_store()


@pytest.fixture
def provisioned_project(runtime_database):
    """A provisioned, unadopted project, its people, and its authorization.

    This is the whole of the setup the journey is allowed, and every part of it
    is an act Corridor operations performs before a customer's first sign-in:
    the project is provisioned, the people are enrolled, and the limited
    onboarding authorization the control plane issued is recorded here through
    the operations capability. Nothing adopts a baseline, registers a profile
    or prepares anything -- those are acts the journey has to perform through
    the product, and a fixture that performed them would be the "pre-adopting
    the baseline in a fixture" the audit rules out.

    The grant is not optional scaffolding either. ADR-0099 is that onboarding
    runs under this limited authorization rather than under relaxed activation
    checks, so a fixture that omitted it would be proving the journey against
    a project the product refuses.
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
            (
                OPERATOR,
                OPERATOR_EMAIL,
                "Operations",
                [access.TECHNICAL_OPERATIONS],
            ),
        ):
            access.enroll_member(
                owner,
                project_id=project.id,
                email=email,
                principal=principal,
                display_name=name,
                designations=designations,
                operator=PROVISIONER,
            )
        record_onboarding_grant(
            owner,
            project_id=int(project.id),
            authorization_id="loa-journey",
            grant_version=1,
            customer="lone-star-transit",
            environment="pilot-1",
            permitted_operations=ONBOARDING_OPERATIONS,
            source_scope="ucm workbook revisions",
            governing_authorization_identity="customer-authorization-7",
            governing_authorization_version="2026-04-01",
            evidence_identity="s3://authorizations/7.pdf",
            evidence_sha256="a" * 64,
            # Inside ADR-0099's revalidation window, which is what makes the
            # grant's positive answer a current one rather than a stale one.
            issued_at=SIGN_IN_AT - timedelta(minutes=1),
            expires_at=SIGN_IN_AT + timedelta(days=30),
            issued_by_actor=OPERATIONS_ACTOR,
            recorded_by_actor=OPERATIONS_ACTOR,
        )
        # The four standing schedules a deployed project runs on, configured
        # through the same `configure_due_work` a deployment configures them
        # through. Provisioning a project is what turns them on; a journey
        # that switched one on midway would be arranging its own runtime
        # rather than walking the deployment's.
        for declaration in (
            ProjectProcessingDeclaration.released_hourly(
                project_id=int(project.id),
                configuration_version="project-processing-v1",
                extractor_identity="corridor.extract_project",
                starts_at=SCHEDULES_FROM,
            ),
            DeltaGenerationDeclaration.released_hourly(
                project_id=int(project.id),
                configuration_version="delta-generation-v1",
                comparison_rule_version=COMPARISON_RULE_VERSION,
                starts_at=SCHEDULES_FROM,
            ),
            ReportPreparationDeclaration.released_weekly(
                project_id=int(project.id),
                configuration_version="report-preparation-v1",
                starts_at=SCHEDULES_FROM,
            ),
            ReleasePreparationDeclaration.released_on_request(
                project_id=int(project.id),
                configuration_version="release-preparation-v1",
                starts_at=SCHEDULES_FROM,
            ),
        ):
            configure_due_work(owner, declaration, now=SCHEDULES_FROM)
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


# --- the link ratchet, asked about the journey's own pages ------------------


def test_the_link_ratchet_is_empty_for_every_core_journey_page():
    """#849's third criterion, asked of the pages this journey actually opens.

    ``tests/test_manifest_page_links.py`` keeps two lists that may fall and may
    never rise: internal links a live-pilot page prints that the deployment
    does not serve, and links whose whole target is a template expression its
    reading cannot resolve. Both are down to one template, ``work_list.html``
    -- ADR-0035's legacy item-per-record Work List, which an enforcing
    deployment refuses *before* the template rather than rendering badly.

    That is what makes this criterion answerable rather than aspirational: the
    ratchet is not empty, and it is empty of every page the core journey
    opens. The other half of the claim -- that no core-journey page is the
    legacy Work List -- is not asserted here but walked: the scenario above
    opens an unadopted project and reads the onboarding page, and opens an
    adopted one and reads the week, and a run that rendered the legacy list
    instead would fail on the sentences those steps assert.
    """

    offenders = {
        entry.split(":", 1)[0]
        for entry in test_manifest_page_links.UNADMITTED_LINKS
        | test_manifest_page_links.COMPUTED_LINKS
    }
    assert offenders <= {"work_list.html"}, (
        "a page outside the legacy Work List prints a link the live pilot "
        "does not serve, or one this reading cannot resolve: "
        + ", ".join(sorted(offenders - {"work_list.html"}))
    )
    assert not test_manifest_page_links.APPROVED_EXTERNAL_LINKS, (
        "a page in the pilot set now sends a coordinator out of the product "
        "mid-workflow, which is a decision the core journey has not made"
    )
