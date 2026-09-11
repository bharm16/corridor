"""The core customer journey, written in full and run for real (#848, #849).

#849 is the journey this file walks: *a provisioned but unadopted project and a
person who has not signed in* at one end, *that person retrieving the exact
approved package and coming back for the next cycle* at the other. Every step
between them is written out below in the order the audit wrote it, and the
walk now runs all of them.

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

**The walk runs whole, and nothing here is marked as allowed to fail.** Every
ticket #849 was blocked by has merged, so a step that does not pass is no
longer a ticket outstanding -- it is a defect in something that was delivered,
and marking it expected-to-fail would file it under a ticket that is closed.
The first run of this file as the deployed web login found four such defects,
each invisible to the tests that own its seam because those tests replace the
identity, the session, or both, and all four are fixed:

1. ``POST /projects/{slug}/baseline/prepare`` answered 500 on an enforcing
   deployment, twice over. The adoption guard counted legacy ``dependencies``
   rows the boundary revokes from ``corridor_web``, and it now asks the
   record-decision role's ``project_accepted_record_decision_count`` command
   for both halves of that count instead (#933). And the bounded read
   registered its workbook with the generic parse, which writes a ``doc_pages``
   projection the web capability holds nothing on; it registers the way the web
   confirmation registers a later revision and appends the workbook's own cells
   (#893's arrangement, applied here).
2. No page in the product rendered a control for that route, though
   ``onboarding_view`` computed ``may_prepare`` and no template read it. The
   onboarding page now names what has been supplied and offers the reading to
   whoever that predicate admits (#934).
3. ``POST /review/{slug}/correction`` answered 500 for a report it had
   recorded, and the intake preview on an adopted project rendered a
   confirmation form its own route refuses with 400. Both were one defect: the
   route commits and then keeps working, and the project partition is
   transaction-local, so the second transaction of the request read the empty
   partition. ``access.keep_partition_declared`` makes the declaration last as
   long as the request that made it, and ``tests/test_architecture.py`` fails
   if a web surface declares a partition and does not keep it (#935, #936).

**What the walk found and did not fix.** A project configured with the
standing schedules a deployment configures has *two* producers of Proposed
Deltas over one delivery, and both run: the later-revision comparison, which
names the registered source family, and ``delta_generation``'s own pass, whose
``_lineage`` falls back to ``document:N`` because the Document carries no
registry identity. So this revision's three changed rows arrive as six
proposals in two Delta Groups over the same document and the same source
revision, and every conflict below reads as "two retained sources disagree"
when one file arrived. The steps are written on what the product shows rather
than around it, which is why they open an item expecting more than one child.

**How to read a run.** The report prints one sentence per step. ``-rP`` is what
shows it on a passing run -- xdist keeps a worker's output to itself otherwise
-- and a failing run carries the whole report in its message:

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
from corridor.capture_correction_retirement import (
    CORRECTED_READING,
    NO_CHANGE,
    STILL_DIFFERS,
)
from corridor.baseline_adoption import BASELINE_DOC_TYPE
from corridor.config import settings
from corridor.delta_generation import (
    COMPARISON_RULE_VERSION,
    DeltaGenerationDeclaration,
)
from corridor.due_work import (
    HANDLER_RELEASE_PREPARATION,
    HANDLER_REPORT_PREPARATION,
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
    ProjectRecordRevision,
    ReleaseCandidate,
    ReleasePackage,
)
from corridor.object_storage import content_store
from corridor.operations_repair import correct_captured_reading
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
#: The customer's week closes and the standing weekly reading runs. It is
#: after the coordinator's decisions on purpose and not by arrangement: a
#: current accepted revision needs its own retained reading, so an issue
#: prepared from a reading taken before those decisions is refused by name --
#: "one issue is one accepted record, never two".
WEEK_CLOSES_AT = datetime(2026, 4, 8, 8, 0, tzinfo=timezone.utc)
WORKER_AT = datetime(2026, 4, 8, 9, 0, tzinfo=timezone.utc)
NEXT_CYCLE_AT = datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc)

# When this project's standing schedules begin.
SCHEDULES_FROM = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)

# The date the coordinator says they will come back to the change they defer.
RETURN_DATE = date(2026, 5, 4)

#: A fourth ordinary conflict, this journey's own. The shared fixture's three
#: are enough to review and to decide, and not enough to *correct*: a
#: correction re-reads the retained passage a coordinator points at, so the
#: sheet has to carry a passage for the last conflict to point at as well.
#: Nothing else about it is special -- it is one more row of the same form,
#: and its size never changes.
FOURTH_CONFLICT = [
    "UC-4", "Oncor", "Electric", "6 in", "Copper", "SR-BL",
    "1190+00", "1191+00", "Relocate", "2026-06-01", "", "", "UCM-1004", "",
]

#: The customer's own record, as this journey's project adopts it.
JOURNEY_BASELINE_ROWS = [*BASELINE_ROWS, FOURTH_CONFLICT]

#: The later revision the customer sends, and the one thing wrong with it.
#:
#: The size column on this sheet is a row out: every conflict's size sits on
#: the row below its own. That is one ordinary spreadsheet fault, and it is
#: what makes both of ADR-0101's source-grounded outcomes reachable through
#: the product rather than only in a fixture, because a correction re-reads
#: the passage the coordinator points at and each conflict's real size is a
#: retained passage of this same revision:
#:
#:   UC-1  12 in -> 16 in   the change decided in Review, and undone
#:   UC-2   8 in -> 10 in   corrected to the 8 in below it: matches the record
#:   UC-3   4 in ->  8 in   corrected to the 6 in below it: still differs
#:   UC-4   6 in            unchanged, and the passage UC-3 is corrected to
#:
#: Three changed rows is the smallest this journey can walk on: one change is
#: decided in Review, and a coordinator who had to settle a change before they
#: could say Corridor read it wrong would have no way to report one at all. A
#: burst of them belongs to the ticket that batches one (#527).
LATER_ROWS = [
    [*BASELINE_ROWS[0][:3], "16 in", *BASELINE_ROWS[0][4:]],
    [*BASELINE_ROWS[1][:3], "10 in", *BASELINE_ROWS[1][4:]],
    [*BASELINE_ROWS[2][:3], "8 in", *BASELINE_ROWS[2][4:]],
    FOURTH_CONFLICT,
]

#: And next cycle's, so the second reporting cycle reads a revision rather
#: than the same bytes twice.
SECOND_LATER_ROWS = [
    [*BASELINE_ROWS[0][:3], "18 in", *BASELINE_ROWS[0][4:]],
    *BASELINE_ROWS[1:],
    FOURTH_CONFLICT,
]

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


def deliver(journey: Journey, name: str, rows) -> tuple[str, dict[str, str]]:
    """Hand a workbook over through the one control the product offers.

    Returns the preview page and the confirmation form it rendered. The form
    carries the staged digest every later act names the bytes by; the page is
    returned with it because on an adopted project the confirmation is not
    only those hidden fields -- it also asks what only a person can say about
    the file, and a step that posted the hidden fields alone would be refused
    exactly as a browser would be.
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
    return submitted.text, confirmation


_SELECT_CONTROL = re.compile(
    r'<select[^>]*name="(?P<name>[^"]*)"[^>]*>(?P<body>.*?)</select>', re.S
)
_TEXT_CONTROL = re.compile(
    r'<input[^>]*id="ask-[^"]*"[^>]*type="text"[^>]*name="(?P<name>[^"]*)"'
)

#: What the person delivering a customer's weekly UCM export says about it,
#: named by the value the page's own option carries. A complete enumeration
#: that replaces the revision before it is what that export is.
DECLARED = {
    "completeness": COMPLETE_ENUMERATION,
    "revision_relationship": REPLACES,
}


def declaration_on(body: str, *, revision_identity: str) -> dict[str, str]:
    """Answer "what only you can say about this file" from the page's own controls.

    The section exists only on an adopted project, and the confirmation route
    refuses a payload without it, so a preview that renders no section is a
    page offering a form its own route will not accept (#936). Every answer
    below is one the page printed: a choice is asserted to be among the
    options offered for that control before it is chosen, and the one free
    answer is the customer's own name for the revision, which is exactly the
    thing nothing but a person can supply.
    """

    assert "What only you can say about this file" in prose(body), (
        "the preview asks nothing about a file it is about to register on an "
        "adopted project, and the confirmation refuses exactly that payload"
    )
    answers: dict[str, str] = {}
    for name, inner in _SELECT_CONTROL.findall(body):
        offered = _OPTION.findall(inner)
        chosen_value = DECLARED.get(name)
        assert chosen_value is not None, (
            f"the page asks {name!r}, which this journey has no answer for"
        )
        assert chosen_value in offered, (
            f"the page does not offer {chosen_value!r} for {name!r}: {offered}"
        )
        answers[name] = chosen_value
    for name in _TEXT_CONTROL.findall(body):
        answers[name] = revision_identity if name == "revision_identity" else ""
    assert "completeness" in answers, (
        "the preview never asks whether the file lists every current row, so "
        "a row missing from it could be read as a removal"
    )
    return answers


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


def open_the_item(journey: Journey, index: int = 0) -> str:
    """The Review reading, with one of its items opened, as a person opens it.

    Review lists items and opens one at a time on purpose, so everything a
    step below reads -- the exact-source links, the answers, the
    extraction-error control -- exists only on an opened item. ``index`` is
    which of the listed items, in the order Review lists them; a step that
    means one conflict rather than whichever is first says which.
    """

    review = journey.client.get(f"/review/{journey.slug}", follow_redirects=False)
    assert review.status_code == 200, review.text
    links = re.findall(
        rf'href="(/review/{re.escape(journey.slug)}\?item=[^"]+)"', review.text
    )
    assert len(links) > index, (
        f"Review lists {len(links)} items to open and this step means the one "
        f"at {index}, so the revision proposed less than it states. What it "
        f"says: {prose(review.text)[:500]}"
    )
    opened = journey.client.get(
        html.unescape(links[index]), follow_redirects=False
    )
    assert opened.status_code == 200, opened.text
    return opened.text


_OPTION = re.compile(r'<option value="(?P<value>[^"]*)"')


def answer_fields(body: str, *, defer_all: bool = False) -> dict[str, object]:
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
    outcomes = [
        "defer" if defer_all or index else "apply" for index in range(len(deltas))
    ]
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
    _preview, confirmation = deliver(journey, "baseline-ucm.xlsx", JOURNEY_BASELINE_ROWS)
    journey.carried["baseline"] = confirmation


def step_operations_resolves_mechanics(journey: Journey) -> None:
    """Technical Operations reads the supplied workbook and settles its shape.

    Through the page's own control, like every other act in this journey.
    Until #934 there was none: ``onboarding.html`` offered the upload link
    and, once a reading existed, the adoption form, and nothing offered the
    act in between -- so this step had to compose the request itself and said
    so. The control is rendered from the ``may_prepare`` the view already
    computed, which is why Technical Operations sees it here at all.
    """

    baseline = journey.carried["baseline"]
    sign_in_as(journey, OPERATOR_EMAIL)
    page = journey.client.get(f"/work/{journey.slug}", follow_redirects=False)
    assert page.status_code == 200, page.text
    assert baseline["filename"] in prose(page.text), (
        "the workbook the coordinator supplied is not named on the page the "
        f"next person opens. What it says: {prose(page.text)[:400]}"
    )
    reading = form_fields(page.text, "/baseline/prepare")
    assert reading is not None, (
        "nothing in the product offers the act between supplying the workbook "
        "and adopting it, so this delivery can only be read by composing the "
        "request the route defines"
    )
    assert reading["sha256"] == baseline["sha256"], (
        "the control offers a different file than the one that was delivered"
    )

    prepared = submit_form(
        journey.client, f"/projects/{journey.slug}/baseline/prepare", reading
    )

    assert prepared.status_code == 201, (
        "the supplied workbook cannot be read for adoption on an enforcing "
        f"deployment: the route answered {prepared.status_code}. "
        f"What the route answered: {prepared.text[:300]}"
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
    preview, confirmation = deliver(journey, "later-ucm.xlsx", LATER_ROWS)
    registered = submit_form(
        journey.client,
        f"/projects/{journey.slug}/sources/confirm",
        {
            **confirmation,
            **declaration_on(preview, revision_identity="UCM workbook revision D"),
        },
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

    preview, confirmation = deliver(journey, "held-ucm.xlsx", SECOND_LATER_ROWS)
    submitted = submit_form(
        journey.client,
        f"/projects/{journey.slug}/sources/confirm",
        {
            **confirmation,
            **declaration_on(preview, revision_identity="UCM workbook revision D2"),
        },
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


_CORRECTION_FORM = re.compile(
    r'<form[^>]*action="[^"]*/correction"[^>]*>(?P<body>.*?)</form>', re.S
)
_PASSAGE_OPTION = re.compile(
    r'<option value="(?P<id>\d+)"[^>]*>(?P<label>.*?)</option>', re.S
)


def ordered_deltas(body: str) -> list[str]:
    """The changes this item holds, in the order the page prints them."""

    return re.findall(r'name="answer_delta" value="([^"]*)"', body)


def correction_form_for(body: str, delta_id: str) -> dict[str, str] | None:
    """The extraction-error form the page renders beside one change."""

    for match in _CORRECTION_FORM.finditer(body):
        fields = {
            name: html.unescape(value)
            for name, value in _REPEATED.findall(match.group("body"))
        }
        if fields.get("correction_delta") == delta_id:
            return fields
    return None


def passages_for(body: str, delta_id: str) -> list[tuple[str, str]]:
    """Every passage of this source the page offers for one change."""

    for match in _CORRECTION_FORM.finditer(body):
        fields = {
            name: html.unescape(value)
            for name, value in _REPEATED.findall(match.group("body"))
        }
        if fields.get("correction_delta") != delta_id:
            continue
        return [
            (found.group("id"), html.unescape(found.group("label")).strip())
            for found in _PASSAGE_OPTION.finditer(match.group("body"))
        ]
    return []


def find_the_passage(
    journey: Journey, *, item_key: str, delta_id: str, words: str
) -> tuple[str, str]:
    """Search this source for the passage a capture should have been read from.

    Through the page's own "Find another passage of this source" control,
    because the passages a report may name are the ones the product offers:
    the window it opens on is the cited passage's neighbourhood, and a
    coordinator who believes the value is somewhere else looks for it.
    """

    found = journey.client.get(
        f"/review/{journey.slug}",
        params={"item": item_key, "correction": delta_id, "search": words},
        follow_redirects=False,
    )
    assert found.status_code == 200, found.text
    offered = [
        (segment_id, label)
        for segment_id, label in passages_for(found.text, delta_id)
        if label.endswith(f": {words}")
    ]
    assert offered, (
        f"searching this source for {words!r} offered no passage saying it: "
        f"{passages_for(found.text, delta_id)}"
    )
    return found.text, offered[0][0]


def report_the_extraction_error(
    journey: Journey, *, delta_id: str, words: str, interpretation: str,
    item: int = 0,
) -> int:
    """Say that one capture was read from the wrong passage of its source.

    Returns the report's own identity, read out of the receipt the product
    printed rather than out of the database: the number operations is given to
    act on is the number the coordinator was shown.
    """

    opened = open_the_item(journey, item)
    item_key = form_fields(opened, "/answers")["item_key"]
    searched, passage_id = find_the_passage(
        journey, item_key=item_key, delta_id=delta_id, words=words
    )
    report = correction_form_for(searched, delta_id)
    assert report is not None, (
        "the page offers no extraction-error form beside this change"
    )
    reported = journey.client.post(
        f"/review/{journey.slug}/correction",
        data={
            **report,
            "correction_passage": passage_id,
            "correction_interpretation": interpretation,
        },
        follow_redirects=False,
    )
    assert reported.status_code == 200, (
        "reporting an extraction error answered "
        f"{reported.status_code}: {reported.text[:400]}"
    )
    readable = prose(reported.text)
    assert "are retained as report" in readable, (
        f"the report was not receipted to its reporter: {readable[:500]}"
    )
    assert "The record is unchanged" in readable, (
        "reporting an extraction error was described as changing something"
    )
    numbered = re.search(r"are retained as report (\d+)", readable)
    assert numbered is not None, (
        f"the receipt names no report to act on: {readable[:500]}"
    )
    return int(numbered.group(1))


def operations_corrects_the_capture(journey: Journey, request_id: int):
    """The runbook hop, run as the runbook runs it.

    No HTTP route performs a correction and that is deliberate: the re-capture
    is a managed technical operation, and
    ``src/corridor/operations_repair_cli.py`` says a staff-only runbook may
    perform it so long as it is attributable and repeatable. So this calls
    what the runbook's ``correct-capture`` calls, as the person holding the
    technical-operations designation, in its own committed transaction.
    """

    with journey.factory() as operations:
        with operations.begin():
            return correct_captured_reading(
                operations,
                request_id=request_id,
                principal=OPERATOR,
                performed_at=journey.clock.now(),
            )


def step_report_an_extraction_error(journey: Journey) -> None:
    """The coordinator says one capture was read from the wrong passage.

    The passage they name is the size cell one row down, which is the shift
    this revision really has: every size on the sheet is a row out. Reporting
    it settles nothing and decides nothing -- what the record holds is
    untouched, and the change stays theirs to decide.
    """

    opened = open_the_item(journey)
    assert capture_correction.CORRECTION_CONTROL in prose(opened), (
        "there is no way to say a capture is wrong at the source, so a wrong "
        "extraction can only be worked around"
    )
    # The conflict this item is about is the one whose accepted size the sheet
    # still states, one row down from where it was read.
    dated = ordered_deltas(opened)
    assert dated, (
        "the item the coordinator dated holds no change they could report an "
        "extraction error against"
    )
    journey.carried["dated_deltas"] = dated
    journey.carried["no_change_report"] = report_the_extraction_error(
        journey,
        delta_id=dated[0],
        words="8 in",
        interpretation=(
            "the size column on this sheet is a row out: this conflict's size "
            "is the cell below the one that was read, and it still says 8 in"
        ),
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


def record_revisions(journey: Journey) -> int:
    """How many revisions this project's accepted record has been through.

    A correction never writes one, and counting them is the shortest true
    statement of "no accepted value changed": the accepted record changes only
    by a Project Record revision, so a path that writes none changed nothing.
    """

    with journey.factory() as reading:
        return int(
            reading.scalar(
                select(func.count())
                .select_from(ProjectRecordRevision)
                .where(
                    ProjectRecordRevision.project_id
                    == journey.carried["project_id"]
                )
            )
        )


def record_rows(journey: Journey) -> str:
    """The Record view, which is where a change's whole standing is printed."""

    record = journey.client.get(f"/record/{journey.slug}", follow_redirects=False)
    assert record.status_code == 200, record.text
    return prose(record.text)


def step_a_corrected_capture_that_still_differs_replaces_the_proposal(
    journey: Journey,
) -> None:
    """Operations re-reads the passage, and the record still differs from it.

    The first of ADR-0101's two source-grounded outcomes. Corridor's reading
    was wrong and the corrected reading is still not what the record holds, so
    the obsolete proposal is retired and a corrected one is raised in its
    place -- a replacement, not a newer source version superseding an older
    one, because the cause is a correction.
    """

    # The next conflict down, where the same fault puts the real size on a row
    # whose value the record has never held.
    next_item = open_the_item(journey, index=1)
    changes = ordered_deltas(next_item)
    assert changes, "the second item Review lists holds no change to correct"
    report = report_the_extraction_error(
        journey,
        item=1,
        delta_id=changes[0],
        words="6 in",
        interpretation=(
            "the size column on this sheet is a row out: this conflict's size "
            "is the cell below the one that was read, and it says 6 in"
        ),
    )

    outcome = operations_corrects_the_capture(journey, report)

    assert outcome.outcome == STILL_DIFFERS, outcome
    assert outcome.retired, (
        "a corrected capture established a different reading and the proposal "
        "it made obsolete is still in Review"
    )

    readable = record_rows(journey)
    assert CORRECTED_READING in readable, (
        "the record does not say Corridor corrected its reading of this "
        f"source. What it says: {readable[:700]}"
    )
    assert (
        "The original proposed change has been replaced by a corrected "
        "proposal. Review the corrected proposal before changing the accepted "
        "record." in readable
    ), (
        "the retired proposal does not say a corrected one replaced it: "
        f"{readable[:900]}"
    )
    # And the corrected proposal is a change a coordinator can still open,
    # stating what the corrected passage says rather than what was misread.
    assert outcome.replacement_delta_id is not None, (
        "a corrected reading that still differs raised no corrected proposal"
    )
    corrected = open_the_item(journey, index=1)
    assert str(outcome.replacement_delta_id) in ordered_deltas(corrected), (
        "the corrected proposal is not a change this item offers to answer, "
        "so nobody can decide what the corrected reading established"
    )
    assert "6 in" in prose(corrected), (
        "the corrected proposal does not state the words of the passage it "
        f"was re-read from. What the item says: {prose(corrected)[:700]}"
    )


def step_a_deferred_proposal_is_retired_by_a_corrected_capture(
    journey: Journey,
) -> None:
    """The other outcome, on a change the coordinator had put a date on.

    Three things at once, and all three are the point. A corrected reading
    that *matches* the accepted record retires the obsolete proposal and
    changes no accepted value. A proposal a coordinator deferred is retired
    without being woken. And the scheduling receipt they wrote survives it:
    the record still says when they meant to come back, beside the reason the
    change left Review rather than instead of it (ADR-0101).
    """

    deferred = journey.carried["dated_deltas"][0]
    opened = open_the_item(journey)
    assert deferred in ordered_deltas(opened), (
        "the change this step dates is not one this item still holds"
    )
    answers = answer_fields(opened, defer_all=True)
    saved = journey.client.post(
        f"/review/{journey.slug}/answers", data=answers, follow_redirects=False
    )
    assert saved.status_code in (200, 201), saved.text
    # A packet of dated Defers writes no Project Record revision, and the
    # page says exactly that rather than naming one (ADR-0084).
    assert (
        "the proposed changes stay open and the accepted record is unchanged"
        in prose(saved.text)
    ), (
        "the dated return was not recorded as scheduling: "
        f"{prose(saved.text)[-1500:]}"
    )
    before = record_revisions(journey)

    outcome = operations_corrects_the_capture(
        journey, journey.carried["no_change_report"]
    )

    assert outcome.outcome == NO_CHANGE, outcome
    assert outcome.retired, (
        "a corrected capture matching the accepted record left its obsolete "
        "proposal in Review"
    )
    assert record_revisions(journey) == before, (
        "correcting a capture wrote a Project Record revision, and this path "
        "never changes an accepted value"
    )

    readable = record_rows(journey)
    assert CORRECTED_READING in readable, readable[:700]
    assert "No accepted value changed." in readable, (
        "the record does not say that nothing it holds changed: "
        f"{readable[:900]}"
    )
    assert f"Deferred until {RETURN_DATE.isoformat()}." in readable, (
        "the dated return the coordinator recorded did not survive the "
        f"retirement of the change it was about: {readable[:900]}"
    )


def step_prepare_the_issue(journey: Journey) -> None:
    """The week closes, its reading is taken, and the coordinator asks.

    The reading is the deployment's own standing weekly pass, not an
    arrangement this file makes: an issue is prepared from the retained
    reading of the week it covers, and that reading has to count against the
    accepted revision the coordinator confirmed. The decisions above moved
    that revision, so a reading taken before them describes a different
    accepted record and the preparation refuses in exactly those words.
    """

    journey.clock.advance_to(WEEK_CLOSES_AT)
    assert HANDLER_REPORT_PREPARATION in run_the_worker(journey), (
        "the week closed and the standing weekly reading never ran, so there "
        "is nothing for an issue to be prepared from"
    )

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
    preview, confirmation = deliver(
        journey, "second-later-ucm.xlsx", SECOND_LATER_ROWS
    )
    registered = submit_form(
        journey.client,
        f"/projects/{journey.slug}/sources/confirm",
        {
            **confirmation,
            **declaration_on(preview, revision_identity="UCM workbook revision E"),
        },
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
        name="a_corrected_capture_that_still_differs_replaces_the_proposal",
        sentence="A corrected capture that still differs retires the "
        "proposal it made obsolete and raises the corrected one",
        owner="#836 and #842",
        run=step_a_corrected_capture_that_still_differs_replaces_the_proposal,
    ),
    Step(
        name="a_deferred_proposal_is_retired_by_a_corrected_capture",
        sentence="A corrected capture matching the record retires a change "
        "they had dated, changes no accepted value, and the date survives",
        owner="#836 and #842",
        run=step_a_deferred_proposal_is_retired_by_a_corrected_capture,
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
    workbook_bytes(workbook, JOURNEY_BASELINE_ROWS)
    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=True
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
    # assumed: every declared step appears once, in order, and the walk runs
    # to its end. Nothing is marked as waiting and nothing is reported as not
    # reached -- the journey is walked whole, which is what #849 asks for.
    assert [one.step.name for one in report.results] == [
        step.name for step in CORE_JOURNEY_STEPS
    ]
    assert [one for one in report.results if one.outcome == EXPECTED_FAIL] == [], (
        "no step of this journey is waiting on a ticket any more:\n"
        + report.render()
    )
    assert [one for one in report.results if one.outcome == BLOCKED] == [], (
        "the walk stopped somewhere and the steps after it were never run:\n"
        + report.render()
    )
    assert all(one.outcome == PASSED for one in report.results), report.render()


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
