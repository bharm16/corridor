"""A session that expires mid-form loses nothing and replays nothing (#844).

The maintainer's decision of 2026-09-10: input entered before a session expires
is preserved as a non-authoritative, short-lived draft, scoped to the same
person, customer, project and form occurrence, and restored only after fresh
authentication and current membership verification.  Nothing goes into a
redirect URL.  No POST is replayed.  Revocation is not expiry: a person who no
longer has access never receives the saved draft.

Nothing here overrides identity.  The session is established the way a person
establishes one -- a link requested, captured by the recording sender, consumed
once -- because the defect this ticket exists to close lives in the dependency
that resolves that session, and a test that replaced the dependency would drive
straight past it (#821).  Every payload comes out of the page that rendered it,
for the same reason.

No clock is read.  The reading's cutoff is declared, the draft store carries a
declared instant of its own, and a session is expired by ageing its row rather
than by waiting twelve hours.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from corridor import access, form_drafts
from corridor.models import (
    DeltaDisposition,
    DeltaReviewPacketReceipt,
    Project,
    ProjectRosterEntry,
    WebSession,
)
from corridor.packet_review import LEAVE_OPEN, read_review_items
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import COORDINATION_QUESTION, SOURCE_REVISION
from corridor.review_packets import NEEDS_COORDINATION
from corridor.web import auth
from corridor.web.app import app, get_kept_drafts, get_review_clock, get_session
from corridor.web.ui_primitives import FOCUS_IDS

from browser_session_support import form_fields, sign_in, submit_form
from harness_support import move_accepted_value
from packet_review_support import append_deltas, subject
from record_counts import nothing_written
from test_focused_review import Coordination
from test_packet_review_screen import Revision

COORDINATOR = HumanPrincipal("local:coordinator")
COORDINATOR_EMAIL = "coordinator@example.test"
OPERATOR = HumanPrincipal("local:operations")

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
# The draft store's own declared instant, independent of the reading's cutoff.
KEPT_AT = datetime(2026, 9, 3, 12, 5, tzinfo=timezone.utc)

REPO_ROOT = Path(__file__).resolve().parents[1]


class Instant:
    """One declared moment the draft store reads, which a test may advance."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture
def project(session: Session) -> Project:
    row = Project(
        slug=f"expiry-draft-{uuid4().hex[:8]}",
        name="Expiry Draft",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    access.enroll_member(
        session,
        project_id=row.id,
        email=COORDINATOR_EMAIL,
        principal=COORDINATOR,
        display_name=COORDINATOR.subject,
        designations=[access.COORDINATION],
        operator=OPERATOR,
    )
    return row


@pytest.fixture
def instant() -> Instant:
    return Instant(KEPT_AT)


@pytest.fixture
def drafts(instant: Instant) -> form_drafts.KeptDrafts:
    return form_drafts.KeptDrafts(now=instant)


@pytest.fixture
def sender():
    return auth.RecordingEmailSender()


@pytest.fixture
def client(session, sender, drafts):
    """Only the plumbing is replaced: database, mail, cutoff, draft store.

    `get_human_principal` is left alone on purpose.  It is the dependency that
    refuses an expired session and the one this ticket changes, so a test that
    substituted it would prove nothing about either half.
    """

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    app.dependency_overrides[get_kept_drafts] = lambda: drafts
    with TestClient(app, base_url="https://testserver") as made:
        yield made
    app.dependency_overrides.clear()


# --- the item a coordinator is looking at ---------------------------------


def _batch(session: Session, project: Project, *, changes: int = 2):
    """One source revision proposing `changes` ordinary value changes."""

    built = Revision(session, project, rows=changes + 1)
    values = [built.change(number) for number in range(1, changes + 1)]
    built.restate(changes + 1)
    append_deltas(
        session, project, built.incoming, source_revision="2026-09", values=values
    )
    return built


def _item(session: Session, project: Project):
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (found,) = [
        row
        for row in reading.items
        if row.grouping_key_kind == SOURCE_REVISION and row.held_out_reason is None
    ]
    return found


def _open(client, project: Project, key: str):
    return client.get(f"/review/{project.slug}?item={quote(key, safe='')}")


def _typed(page: str, project: Project, item, *, defer_until: str = "2026-12-01"):
    """The payload the rendered page offers, plus what a coordinator fills in."""

    fields = form_fields(page, f"/review/{project.slug}")
    assert fields is not None, "the batched item must render its Save form"
    return {
        **fields,
        "child": [str(child.delta_id) for child in item.children],
        "defer_until": defer_until,
        "outcome": "defer",
    }


def _expire(session: Session, principal: HumanPrincipal) -> None:
    """Age this person's live session past its expiry, without a real clock."""

    aged = datetime.now(timezone.utc) - access.SESSION_TTL - timedelta(minutes=1)
    session.execute(
        update(WebSession)
        .where(
            WebSession.principal_subject == principal.subject,
            WebSession.revoked_at.is_(None),
        )
        .values(created_at=aged, expires_at=aged + access.SESSION_TTL)
    )
    session.flush()


def _returned_to(location: str) -> str:
    assert location.startswith("/sign-in?"), location
    query = parse_qs(urlsplit(location).query)
    assert set(query) == {"next"}, "the redirect carries a path and nothing else"
    return query["next"][0]


def _nothing_was_applied(session: Session, project: Project) -> None:
    assert session.scalars(
        select(DeltaReviewPacketReceipt).where(
            DeltaReviewPacketReceipt.project_id == project.id
        )
    ).all() == []
    assert session.scalars(
        select(DeltaDisposition).where(DeltaDisposition.project_id == project.id)
    ).all() == []


# --- criterion 1: refusal, safe sign-in, restoration, diff, fresh token ----


def test_a_session_that_expires_mid_form_is_refused_and_handed_back(
    session: Session, project: Project, client, sender, drafts
):
    """The whole walk: fill, expire, submit, sign in again, find it waiting."""

    _batch(session, project)
    item = _item(session, project)
    sign_in(client, sender, COORDINATOR_EMAIL)
    before = client.cookies.get(auth.CSRF_COOKIE)

    page = _open(client, project, item.item_key)
    assert page.status_code == 200
    typed = _typed(page.text, project, item)
    assert typed[auth.CSRF_FIELD] == before

    _expire(session, COORDINATOR)
    with nothing_written(session, project.id):
        refused = submit_form(client, f"/review/{project.slug}", typed)

    # Refused, and sent to sign in rather than answered with a bare status.
    # 303 is the whole of "no POST is replayed": it is the status that turns
    # the browser's next request into a GET, so nothing re-sends this body --
    # not the redirect, not the sign-in link, and not the page below.
    assert refused.status_code == 303
    location = refused.headers["location"]
    back_to = _returned_to(location)
    assert back_to == f"/review/{project.slug}?item={quote(item.item_key, safe='')}"
    # Nothing the form carried is in the redirect: not the typed date, not the
    # decision, not the token, not even the names of the fields that held them.
    for leaked in ("2026-12-01", "defer", "child", "judgment", before):
        assert leaked not in location
    _nothing_was_applied(session, project)
    assert drafts.holding() == 1

    # Sign in again, the way the refusal points: the page carries the return
    # path in its own hidden field and the link comes back to exactly it.
    form = client.get(location)
    assert form.status_code == 200
    hidden = form_fields(form.text, "/sign-in/request")
    assert hidden["next"] == back_to
    consumed = sign_in(client, sender, COORDINATOR_EMAIL, next_path=hidden["next"])
    assert consumed.headers["location"] == back_to

    after = client.cookies.get(auth.CSRF_COOKIE)
    assert after and after != before, "a restored form carries a fresh token"

    restored = client.get(back_to)
    assert restored.status_code == 200
    body = restored.text

    # It says what happened, assertively, and takes the one focus of the page.
    assert "Your session expired, so nothing was saved" in body
    assert f'id="{FOCUS_IDS["refusal"]}"' in body
    assert 'role="alert"' in body
    assert body.count("autofocus") == 1

    # Every change the draft named is listed against the record as it stands.
    for child in item.children:
        assert child.text in body
    assert body.count("Unchanged since you started") == len(item.children)

    # The permitted input is back on the form, and the token on it is the new
    # one -- so the coordinator submits again themselves.
    offered = form_fields(body, f"/review/{project.slug}")
    assert offered[auth.CSRF_FIELD] == after
    assert 'value="2026-12-01"' in body
    for child in item.children:
        assert re.search(
            rf'name="child"[^>]*value="{child.delta_id}"[^>]*checked', body
        ), f"change {child.delta_id} came back unticked"

    # Taken once, and still nothing applied.
    assert drafts.holding() == 0
    _nothing_was_applied(session, project)


def test_a_change_that_moved_while_signing_in_is_named_with_its_reason(
    session: Session, project: Project, client, sender, drafts
):
    """The diff, for a value that moved and for one that did not."""

    built = _batch(session, project, changes=2)
    item = _item(session, project)
    sign_in(client, sender, COORDINATOR_EMAIL)
    typed = _typed(_open(client, project, item.item_key).text, project, item)

    _expire(session, COORDINATOR)
    refused = submit_form(client, f"/review/{project.slug}", typed)
    back_to = _returned_to(refused.headers["location"])

    # The accepted value one change was compared against moves while the
    # coordinator is in their mail client.
    moved, _ = built.adopted.capture(
        fact_type="station_from", value="9999+00", subject_key=subject(1)
    )
    move_accepted_value(session, project, moved)

    sign_in(client, sender, COORDINATOR_EMAIL, next_path=back_to)
    body = client.get(back_to).text

    stale, standing = item.children[0], item.children[1]
    assert f"Proposed change {stale.delta_id}" in body
    assert "the accepted value it was compared against moved from revision" in body
    assert stale.text not in body
    assert standing.text in body
    assert body.count("Unchanged since you started") == 1


def test_typed_coordination_answers_come_back_on_the_focused_form(
    session: Session, project: Project, client, sender, drafts
):
    """The form a coordinator actually types into, held and handed back.

    The batched Save form carries a selection and a date; this one carries the
    question that has to be answered, who owes it and when it is due, all typed
    by hand.  It is the input worth not losing, so it is the one proved end to
    end here as well.
    """

    built = Coordination(session, project)
    for document, family, revision, value in (
        ("ucm-2026-09.xlsx", "ucm-workbook", "2026-09", Coordination.FROM_WORKBOOK),
        (
            "minutes-2026-09-08.pdf",
            "meeting-minutes",
            "2026-09-08",
            Coordination.FROM_MINUTES,
        ),
    ):
        built.answer(
            document=document, family=family, revision=revision, value=value
        )
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = [
        row for row in reading.items if row.grouping_key_kind == COORDINATION_QUESTION
    ]
    first, second = item.children[0], item.children[1]

    sign_in(client, sender, COORDINATOR_EMAIL)
    page = _open(client, project, item.item_key)
    assert page.status_code == 200
    fields = form_fields(page.text, f"/review/{project.slug}/answers")
    assert fields is not None, "the focused item must render its answers form"
    typed = {
        **fields,
        "answer_delta": [str(first.delta_id), str(second.delta_id)],
        "answer_outcome": [NEEDS_COORDINATION, LEAVE_OPEN],
        "answer_source": ["", ""],
        "answer_question": ["Which date does the district stand behind?", ""],
        "answer_person": ["Dana Reyes", ""],
        "answer_organization": ["", ""],
        "answer_return": ["2026-12-01", ""],
    }

    _expire(session, COORDINATOR)
    refused = submit_form(client, f"/review/{project.slug}/answers", typed)
    assert refused.status_code == 303
    back_to = _returned_to(refused.headers["location"])
    for leaked in ("Dana Reyes", "district", "2026-12-01"):
        assert leaked not in back_to

    sign_in(client, sender, COORDINATOR_EMAIL, next_path=back_to)
    body = client.get(back_to).text

    assert "Your session expired, so nothing was saved" in body
    assert "Which date does the district stand behind?" in body
    assert "Dana Reyes" in body
    assert 'value="2026-12-01"' in body
    assert body.count("Unchanged since you started") == 2
    assert drafts.holding() == 0
    _nothing_was_applied(session, project)


# --- criterion 2: revocation is not expiry, and scope is four things -------


def test_a_revoked_session_keeps_nothing_and_is_simply_refused(
    session: Session, project: Project, client, sender, drafts
):
    """Revocation is not expiry, whether or not the revoked session also ran out.

    Both orders are here because only the second one can tell the two rules
    apart. A session revoked while still live is refused by the clock as much
    as by the revocation, so it would pass against a check that looked at
    neither; a session that has been revoked *and* has since expired is exactly
    the cookie a signed-out or offboarded person's browser still holds, and it
    is the one that must never be mistaken for someone who was mid-form.
    """

    _batch(session, project)
    item = _item(session, project)

    sign_in(client, sender, COORDINATOR_EMAIL)
    typed = _typed(_open(client, project, item.item_key).text, project, item)
    access.revoke_web_session(session, client.cookies.get(auth.SESSION_COOKIE))
    session.flush()

    refused = submit_form(client, f"/review/{project.slug}", typed)
    assert refused.status_code == 401
    assert "location" not in refused.headers
    assert drafts.holding() == 0

    sign_in(client, sender, COORDINATOR_EMAIL)
    typed = _typed(_open(client, project, item.item_key).text, project, item)
    _expire(session, COORDINATOR)
    access.revoke_web_session(session, client.cookies.get(auth.SESSION_COOKIE))
    session.flush()

    refused = submit_form(client, f"/review/{project.slug}", typed)
    assert refused.status_code == 401
    assert "location" not in refused.headers
    assert drafts.holding() == 0, "someone signed out is never mid-form"
    _nothing_was_applied(session, project)


def test_a_member_whose_access_was_withdrawn_is_never_handed_the_draft(
    session: Session, project: Project, client, sender, drafts
):
    """Held after the expiry, and unreachable once the roster says no."""

    _batch(session, project)
    item = _item(session, project)
    sign_in(client, sender, COORDINATOR_EMAIL)
    typed = _typed(_open(client, project, item.item_key).text, project, item)

    _expire(session, COORDINATOR)
    refused = submit_form(client, f"/review/{project.slug}", typed)
    back_to = _returned_to(refused.headers["location"])
    assert drafts.holding() == 1

    entry = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.project_id == project.id,
            ProjectRosterEntry.principal_subject == COORDINATOR.subject,
        )
    ).one()
    entry.active = False
    session.flush()

    sign_in(client, sender, COORDINATOR_EMAIL, next_path=back_to)
    answered = client.get(back_to)
    assert answered.status_code == 404, "the membership gate answers first"
    assert "2026-12-01" not in answered.text
    assert drafts.holding() == 1, "the draft is never taken by a refused reader"


def test_another_person_occurrence_project_or_customer_is_handed_nothing(
    session: Session, project: Project, client, sender, drafts
):
    """All four parts of the scope, and each one alone withholds the draft.

    The wrong scopes are asked of the store and the right one is asked last, so
    this proves both halves at once: the store discriminates on each of the
    four, and the refusal recorded *exactly* the person, customer, project and
    occurrence the request carried rather than something close to them.

    The two signed-in browsers this would otherwise drive cannot share one
    rollback-scoped transaction: a project-authorization scope is declared per
    transaction and #662 refuses a second, different one in the same
    transaction, which is the same rule that stops one request answering as two
    people. So the person dimension is proved here, against the key the route
    actually wrote.
    """

    _batch(session, project)
    item = _item(session, project)
    sign_in(client, sender, COORDINATOR_EMAIL)
    typed = _typed(_open(client, project, item.item_key).text, project, item)
    _expire(session, COORDINATOR)
    submit_form(client, f"/review/{project.slug}", typed)
    assert drafts.holding() == 1

    held = form_drafts.DraftScope(
        customer="local",
        principal_subject=COORDINATOR.subject,
        project_slug=project.slug,
        occurrence=item.item_key,
    )
    for apart in (
        {"customer": "another-customer/pilot/one"},
        {"principal_subject": "local:somebody-else"},
        {"project_slug": "another-project"},
        {"occurrence": "another-occurrence"},
    ):
        assert drafts.take(replace(held, **apart)) is None
        assert drafts.holding() == 1, "a reader it is not for cannot consume it"

    # The same person on a different occurrence of the same form, over HTTP:
    # the whole reading, and nothing of what anyone typed.
    sign_in(client, sender, COORDINATOR_EMAIL)
    landing = client.get(f"/review/{project.slug}")
    assert landing.status_code == 200
    assert "Your session expired" not in landing.text
    assert "2026-12-01" not in landing.text
    assert drafts.holding() == 1

    assert drafts.take(held) is not None, "and it is exactly this scope's"


# --- criterion 3: a short bound, and no domain reader ----------------------


def test_a_draft_expires_on_its_own_short_bound(
    session: Session, project: Project, client, sender, drafts, instant: Instant
):
    """Held briefly and then gone, whether or not anyone comes back for it."""

    assert form_drafts.DRAFT_TTL <= timedelta(hours=1)
    assert form_drafts.DRAFT_TTL < access.SESSION_TTL

    _batch(session, project)
    item = _item(session, project)
    sign_in(client, sender, COORDINATOR_EMAIL)
    typed = _typed(_open(client, project, item.item_key).text, project, item)
    _expire(session, COORDINATOR)
    refused = submit_form(client, f"/review/{project.slug}", typed)
    back_to = _returned_to(refused.headers["location"])
    assert drafts.holding() == 1

    instant.moment = KEPT_AT + form_drafts.DRAFT_TTL
    sign_in(client, sender, COORDINATOR_EMAIL, next_path=back_to)
    body = client.get(back_to).text
    assert body.count("autofocus") == 1
    assert "Your session expired" not in body
    assert "2026-12-01" not in body
    assert drafts.holding() == 0


def test_no_domain_module_reads_a_form_draft():
    """A draft is unreviewed input, so nothing that reads the record sees it."""

    reads = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "src").rglob("*.py")
        if path.name != "form_drafts.py"
        and re.search(r"\bform_drafts\b", path.read_text())
    }
    assert reads == {"src/corridor/web/app.py"}, (
        "only the HTTP adapter may hold or hand back a draft; a module that "
        "reads the Project Record must never be able to see one"
    )


# --- criterion 4: only forms a person fills keep anything -----------------


def test_a_form_carrying_no_typed_input_keeps_nothing(
    session: Session, project: Project, client, sender, drafts
):
    """The Issue forms send machine-derived state, so there is nothing to hold.

    `/work/{slug}/issue/authorize` sends a candidate id and
    `/work/{slug}/issue/prepare` a profile version, an accepted revision and a
    coverage digest -- every one of them state the page emitted and the route
    re-derives. Restoring those would restore a stale attestation, which #675
    refuses outright, so an expiry on one of them is the refusal it always was.
    """

    assert set(form_drafts.KEPT_FORMS) == {
        form_drafts.REVIEW_ITEM_SAVE,
        form_drafts.REVIEW_ITEM_ANSWERS,
    }
    sign_in(client, sender, COORDINATOR_EMAIL)
    _expire(session, COORDINATOR)

    refused = client.post(
        f"/work/{project.slug}/issue/authorize",
        data={"candidate_id": "1"},
        follow_redirects=False,
    )
    assert refused.status_code == 401
    assert drafts.holding() == 0


def test_only_permitted_input_is_ever_held(session: Session):
    """The forgery token and the displayed occurrence are not a person's input."""

    submitted = [
        ("csrf_token", "a-live-secret"),
        ("item_key", "batch:2026-09"),
        ("child", "11"),
        ("child", "12"),
        ("defer_until", "2026-12-01"),
        ("outcome", "defer"),
        ("judgment", "required"),
        ("judgment_minutes", "20"),
        ("judgment_cutoff", NOW.isoformat()),
        ("judgment_consequence", "must_handle"),
        ("judgment_rule_version", "v4"),
    ]
    held = form_drafts.permitted_input(form_drafts.REVIEW_ITEM_SAVE, submitted)
    assert held == (
        ("child", "11"),
        ("child", "12"),
        ("defer_until", "2026-12-01"),
        ("judgment", "required"),
        ("judgment_minutes", "20"),
    )
    assert form_drafts.permitted_input("/somewhere/else", submitted) == ()
