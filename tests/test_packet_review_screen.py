"""The coordinator-facing source-revision review screen (#527).

This is the surface-specific half of the acceptance criteria, and the
accessibility properties `docs/accessibility-acceptance-checklist.md` requires
each consuming screen to prove for itself: one `h1`, real landmarks and tables,
labelled controls, exactly one `autofocus` per response, keyboard-complete
child selection, an error summary that links to its control, before-and-after
values read in order, and a stale refusal that keeps the coordinator's
selections and writes nothing.

Nothing here reads the wall clock. The route's own clock seam is overridden, so
the reading cutoff, the decision instant, and a deferral's return date are all
stated by the test.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.analytics import EventFamily, capture_events
from corridor.models import (
    DeltaDisposition,
    DeltaFollowUpPlan,
    DeltaReviewPacketReceipt,
    Project,
    ProjectRecordRevision,
)
from corridor.native_follow_up_reading import undone_follow_up_plan_ids
from corridor.packet_review import (
    CUSTOMER_WORKBOOK,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import (
    HELD_OUT_OWNER_MISMATCH,
    SOURCE_REVISION,
)
from corridor.review_packets import (
    NEEDS_COORDINATION,
    packet_children,
    packet_reversal,
)
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)
from corridor.web.packet_receipt import read_packet_receipt
from corridor.web.ui_primitives import FOCUS_IDS

from browser_session_support import page_without_shell
from access_support import seed_membership
from harness_support import move_accepted_value
from record_counts import nothing_written
from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    modify,
    new_subject,
    register_baseline,
    register_output_template,
    register_source_row,
    subject,
    support,
)

COORDINATOR = HumanPrincipal("local:coordinator")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
TEMPLATE_IDENTITY = "district-ucm-template"
TEMPLATE_VERSION = "v3"


@pytest.fixture
def project(session: Session) -> Project:
    row = Project(
        slug=f"review-screen-{uuid4().hex[:8]}",
        name="Review Screen",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR)
    return row


@pytest.fixture
def client(session):
    """The app shares the test's transaction and the test's declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


class Revision:
    """One adopted baseline and the later revision that proposes changes to it."""

    def __init__(self, session: Session, project: Project, *, rows: int):
        self.session = session
        self.project = project
        self.adopted = Rendition(session, project, "ucm-2026-08.xlsx")
        self.incoming = Rendition(session, project, "ucm-2026-09.xlsx")
        self.revision_of: dict[str, int] = {}
        first: int | None = None
        for number in range(1, rows + 1):
            fact, _ = self.adopted.capture(
                fact_type="station_from",
                value=f"{1000 + number}+00",
                subject_key=subject(number),
            )
            revision = accept_baseline_fact(session, project, fact)
            first = first or revision
            self.revision_of[subject(number)] = revision
        assert first is not None
        self.baseline = register_baseline(
            session, project, self.adopted.document, first
        )
        for number in range(1, rows + 1):
            register_source_row(
                session,
                project,
                self.baseline,
                row_number=number,
                business_identity=f"UC-{number:03d}",
                external_system_id=f"UCM-{number:05d}",
                source_url=f"https://records.example.gov/conflict/{number}",
            )
        register_output_template(
            session, project, identity=TEMPLATE_IDENTITY, version=TEMPLATE_VERSION
        )

    def change(self, number: int, *, supported: bool = True):
        value = f"{2000 + number}+00"
        fact, segment = self.incoming.capture(
            fact_type="station_from", value=value, subject_key=subject(number)
        )
        if supported:
            support(self.session, self.project, fact, segment)
        return modify(
            subject_key=subject(number),
            field_name="station_from",
            accepted_value=f"{1000 + number}+00",
            proposed_value=value,
            baseline_revision=self.revision_of[subject(number)],
        )

    def restate(self, number: int) -> None:
        self.incoming.capture(
            fact_type="station_from",
            value=f"{1000 + number}+00",
            subject_key=subject(number),
        )


def _revision(
    session: Session, project: Project, *, changes: int = 3, exceptions: bool = False
) -> Revision:
    extra = 2 if exceptions else 0
    built = Revision(session, project, rows=changes + extra + 1)
    values = [built.change(number) for number in range(1, changes + 1)]
    if exceptions:
        values.append(
            modify(
                subject_key=subject(changes + 1),
                field_name="external_org",
                accepted_value="AT&T Texas",
                proposed_value="AT&T Texas (SWBT)",
                baseline_revision=None,
            )
        )
        values.append(
            new_subject(
                subject_key=subject(changes + 2),
                fields=("station_from",),
                baseline_revision=None,
            )
        )
    built.restate(changes + extra + 1)
    append_deltas(
        session, project, built.incoming, source_revision="2026-09", values=values
    )
    return built


def _batch_key(session: Session, project: Project) -> str:
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = [
        row
        for row in reading.items
        if row.grouping_key_kind == SOURCE_REVISION and row.held_out_reason is None
    ]
    return item.item_key


def _delta_ids(session: Session, project: Project) -> list[int]:
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = [
        row
        for row in reading.items
        if row.grouping_key_kind == SOURCE_REVISION and row.held_out_reason is None
    ]
    return [child.delta_id for child in item.children]


def _open(client, project, key: str):
    return client.get(f"/review/{project.slug}?item={quote(key, safe='')}")


# --- the reading, on the screen -------------------------------------------


def test_the_screen_names_the_source_revision_and_never_the_packet_type(
    session: Session, project: Project, client
):
    _revision(session, project, changes=3)

    body = client.get(f"/review/{project.slug}").text

    assert "ucm-workbook 2026-09" in body
    assert "Review Packet" not in body
    assert "review packet" not in body.lower()


def test_the_opened_item_shows_its_counts_values_source_and_artifacts(
    session: Session, project: Project, client
):
    _revision(session, project, changes=3, exceptions=True)
    key = _batch_key(session, project)

    body = _open(client, project, key).text

    assert "Values this revision left unchanged" in body
    assert "Changes ready for decision" in body
    assert "Held out of this batch" in body
    # Not a count of the selection. It was rendered once, from the reading the
    # page was sent with, and a box ticked afterwards did not move it -- the
    # same staleness #834 took off the batch buttons (#890).
    assert "Selected now" not in body
    # Accepted value, its revision, the incoming value, and the exact source.
    assert "1001+00" in body and "2001+00" in body
    assert "sheet Utility Conflicts, cell C1" in body
    assert "UC-001 (Utility Conflicts row 1)" in body
    assert "From station" in body
    assert CUSTOMER_WORKBOOK in body
    assert f"{TEMPLATE_IDENTITY} {TEMPLATE_VERSION}" in body


def test_an_external_identifier_and_url_deep_link_read_only(
    session: Session, project: Project, client
):
    """The 2026-09-02 amendment, on the rendered page."""

    _revision(session, project, changes=1)
    key = _batch_key(session, project)

    body = _open(client, project, key).text

    assert "UCM-00001" in body
    import html
    href = html.unescape(re.search(r'href="([^"]+/source\?[^"]+)"', body).group(1))
    with capture_events() as events:
        opened = client.get(href, follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "https://records.example.gov/conflict/1"
    assert len(events.by_family(EventFamily.EVIDENCE_OPENING)) == 1
    assert events.by_family(EventFamily.EVIDENCE_OPENING)[0].payload["principal_subject"] == COORDINATOR.subject
    assert client.get(href.replace("delta_id=", "delta_id=999"), follow_redirects=False).status_code == 404
    saved = client.post(f"/review/{project.slug}", data={
        "item_key": key, "outcome": "keep_current",
        "child": [str(value) for value in _delta_ids(session, project)],
    })
    assert saved.status_code == 200
    assert client.get(href, follow_redirects=False).headers["location"] == "https://records.example.gov/conflict/1"


def test_only_the_opened_item_carries_decision_controls(
    session: Session, project: Project, client
):
    """Criterion 4 on the screen: one control per delta, never two."""

    _revision(session, project, changes=2, exceptions=True)
    key = _batch_key(session, project)

    closed = client.get(f"/review/{project.slug}").text
    opened = _open(client, project, key).text

    # The navigation shell every customer page carries (#843) posts only its
    # own sign-out, so the item's controls are counted without it.
    assert page_without_shell(closed).count("<form method=\"post\"") == 0
    assert page_without_shell(opened).count("<form method=\"post\"") == 1
    assert "Open this item" in opened


def test_a_held_out_child_is_listed_unselectable_with_its_reason(
    session: Session, project: Project, client
):
    """Checklist item 5: excluded children say why and cannot be submitted."""

    _revision(session, project, changes=2, exceptions=True)
    key = _batch_key(session, project)

    body = _open(client, project, key).text

    assert "Held out of this batch:" in body
    assert "disabled" in body
    assert "an organization change has to say whether it corrects" in body


# --- accessibility properties this screen proves for itself ---------------


def test_one_h1_headings_descend_and_landmarks_are_used_once(
    session: Session, project: Project, client
):
    """Checklist item 2."""

    _revision(session, project, changes=2, exceptions=True)
    body = _open(client, project, _batch_key(session, project)).text

    assert body.count("<h1>") == 1
    assert body.count("<main>") == 1
    levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", body)]
    for previous, current in zip(levels, levels[1:]):
        assert current <= previous + 1, levels


def test_exactly_one_element_of_a_response_carries_autofocus(
    session: Session, project: Project, client
):
    """Checklist item 4, for the plain reading and for a completed Save."""

    _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)

    reading = _open(client, project, key).text
    assert reading.count("autofocus") == 1
    assert f'id="{FOCUS_IDS["item"]}"' in reading

    saved = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "keep_current",
            "child": [str(value) for value in ids],
        },
    )
    assert saved.status_code == 200
    assert saved.text.count("autofocus") == 1
    assert f'id="{FOCUS_IDS["outcome"]}"' in saved.text
    assert 'role="status"' in saved.text


def test_every_named_landing_place_is_returnable_by_the_keyboard(
    session: Session, project: Project, client
):
    """Checklist item 4's second clause: `tabindex="-1"` on the focus target."""

    _revision(session, project, changes=1)
    body = _open(client, project, _batch_key(session, project)).text

    assert f'id="{FOCUS_IDS["item"]}" tabindex="-1"' in body


def test_every_child_is_a_labelled_native_checkbox(
    session: Session, project: Project, client
):
    """Checklist items 3 and 5: Tab reaches and Space toggles every child."""

    _revision(session, project, changes=3)
    ids = _delta_ids(session, project)
    body = _open(client, project, _batch_key(session, project)).text

    for delta_id in ids:
        assert f'<label for="child-{delta_id}">' in body
        assert f'id="child-{delta_id}" name="child"' in body
    assert "<fieldset" in body and "<legend>" in body


def test_the_primary_actions_name_their_scope_and_carry_no_count(
    session: Session, project: Project, client
):
    """Criterion 7, including the secondary dated Defer.

    The count these actions used to print was the server's reading of the
    selection at the moment the page was sent, and a checkbox ticked afterwards
    did not change it (#834). The scope is still named; the number is gone, and
    no digit of any kind reaches an action.
    """

    _revision(session, project, changes=3)
    body = _open(client, project, _batch_key(session, project)).text

    assert "Apply the selected changes" in body
    assert "Keep current for the selected changes" in body
    assert "Defer the selected changes until the date above" in body
    assert not re.search(r"(Apply|Keep current for|Defer)\s+\d", body)
    assert "Return date for Defer" in body
    assert "Leaving an item records nothing." in body
    # ADR-0085 rejected a second customer word for the same act.
    assert "Snooze" not in body


def test_the_selection_and_the_held_out_state_stay_readable_on_each_change(
    session: Session, project: Project, client
):
    """Removing the count removes no information: the checkbox carried it.

    Each change states whether it is selected and, where it cannot join the
    batch, why -- through the shared child-selection primitive, in text a
    screen reader announces beside the control it belongs to.
    """

    _revision(session, project, changes=2, exceptions=True)
    body = _open(client, project, _batch_key(session, project)).text

    assert body.count(" checked>") == 2
    assert body.count(" disabled ") == 2
    assert body.count("Held out of this batch:") == 2


def test_the_before_and_after_values_read_in_order_with_their_source(
    session: Session, project: Project, client
):
    """Checklist item 7."""

    _revision(session, project, changes=1)
    body = _open(client, project, _batch_key(session, project)).text

    assert 'th scope="col">Current accepted value' in body
    assert 'th scope="col">Incoming value' in body
    assert 'th scope="col">Source' in body
    assert "<del" not in body and "<s>" not in body


# --- saving ---------------------------------------------------------------


def test_applying_the_selection_saves_through_the_packet_command(
    session: Session, project: Project, client
):
    _revision(session, project, changes=3)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)

    with capture_events() as events:
        response = client.post(
            f"/review/{project.slug}",
            data={
                "item_key": key,
                "outcome": "apply",
                "child": [str(value) for value in ids],
            },
        )

    assert response.status_code == 200
    assert "Applied 3 changes" in response.text
    assert "Project record revision" in response.text
    families = [event.family for event in events.events]
    assert EventFamily.PACKET_SAVE in families
    assert families.count(EventFamily.CHILD_DECISION) == 3
    assert read_review_items(session, project_id=project.id, as_of=NOW).items == ()


def test_an_unselected_child_stays_open_and_is_never_implied(
    session: Session, project: Project, client
):
    """Criterion 6."""

    _revision(session, project, changes=3)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)

    client.post(
        f"/review/{project.slug}",
        data={"item_key": key, "outcome": "apply", "child": [str(ids[0])]},
    )

    remaining = read_review_items(session, project_id=project.id, as_of=NOW)
    assert sorted(remaining.reading.actionable_delta_ids) == sorted(ids[1:])


def test_deferring_needs_a_date_and_says_so_in_an_error_summary(
    session: Session, project: Project, client
):
    """Checklist item 6: one summary, a count, and a link to the control."""

    _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)

    response = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "defer",
            "child": [str(value) for value in ids],
            "defer_until": "",
        },
    )

    assert response.status_code == 400
    assert 'class="error-summary"' in response.text
    assert "1 field needs attention" in response.text
    assert f'href="#{FOCUS_IDS["item"]}-defer-until"' in response.text
    assert response.text.count("autofocus") == 1
    assert read_review_items(session, project_id=project.id, as_of=NOW).items != ()


def test_a_dated_defer_records_scheduling_and_writes_no_revision(
    session: Session, project: Project, client
):
    _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)

    response = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "defer",
            "child": [str(value) for value in ids],
            "defer_until": "2026-10-01",
        },
    )

    assert response.status_code == 200
    assert "Deferred 2 changes" in response.text
    assert "the accepted record is unchanged" in response.text
    assert read_review_items(session, project_id=project.id, as_of=NOW).items == ()


def test_a_stale_child_refuses_the_save_and_keeps_the_selections(
    session: Session, project: Project, client
):
    """Criterion 8: no partial write, and nothing the coordinator chose is lost."""

    built = _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    moved, _ = built.adopted.capture(
        fact_type="station_from", value="9999+00", subject_key=subject(1)
    )
    move_accepted_value(session, project, moved)

    response = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "apply",
            "child": [str(value) for value in ids],
        },
    )

    assert response.status_code == 409
    assert "Nothing was saved" in response.text
    assert 'role="alert"' in response.text
    assert response.text.count("autofocus") == 1
    assert f'id="{FOCUS_IDS["refusal"]}"' in response.text
    assert "the accepted value it was compared against moved from revision" in response.text
    # The delta that did not move is still selected for the resubmission, and
    # the one that did left the actionable set by rule rather than silently.
    assert f'id="child-{ids[1]}" name="child"' in response.text
    assert f'value="{ids[1]}" checked' in response.text
    remaining = read_review_items(session, project_id=project.id, as_of=NOW)
    assert remaining.reading.actionable_delta_ids == (ids[1],)
    assert ids[0] in [row.delta_id for row in remaining.reading.stale]
    assert not [
        row for row in remaining.reading.standings if row.standing == "resolved"
    ]


def test_saving_an_item_that_left_the_reading_writes_nothing(
    session: Session, project: Project, client
):
    _revision(session, project, changes=1)

    response = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": "source_revision:ucm-workbook@1999-01:record_cleanup",
            "outcome": "apply",
            "child": ["1"],
        },
    )

    assert response.status_code == 409
    assert "no longer part of the reading" in response.text


def test_the_screen_emits_surfacing_and_opening_through_the_558_contract(
    session: Session, project: Project, client
):
    _revision(session, project, changes=2, exceptions=True)
    key = _batch_key(session, project)

    with capture_events() as events:
        _open(client, project, key)

    surfacing = events.by_family(EventFamily.PACKET_SURFACING)
    opening = events.by_family(EventFamily.PACKET_OPENING)
    assert len(surfacing) == 3
    assert len(opening) == 1
    assert opening[0].payload["item_key"] == key
    for event in surfacing + opening:
        assert event.binding.packetizer_rules_version
        assert event.binding.code_revision
        assert event.binding.product_revision
        assert "project_id" not in event.metric_labels


def test_a_member_without_the_coordination_designation_cannot_save(
    session: Session, project: Project, client
):
    _revision(session, project, changes=1)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    stranger = Project(
        slug=f"review-other-{uuid4().hex[:8]}", name="Other", is_synthetic=True
    )
    session.add(stranger)
    session.flush()
    seed_membership(session, stranger, COORDINATOR, designations=())

    reading = client.get(f"/review/{stranger.slug}")
    refused = client.post(
        f"/review/{stranger.slug}",
        data={
            "item_key": key,
            "outcome": "apply",
            "child": [str(value) for value in ids],
        },
    )

    assert reading.status_code == 200
    assert refused.status_code == 403


def test_a_non_member_sees_the_same_answer_as_a_missing_project(
    session: Session, project: Project, client
):
    other = Project(
        slug=f"review-unseen-{uuid4().hex[:8]}", name="Unseen", is_synthetic=True
    )
    session.add(other)
    session.flush()

    assert client.get(f"/review/{other.slug}").status_code == 404
    assert client.get("/review/no-such-project").status_code == 404


def test_a_burst_of_forty_changes_is_one_bounded_item_on_the_screen(
    session: Session, project: Project, client
):
    """The burst criterion, rendered: one item, one form, exceptions apart."""

    _revision(session, project, changes=40, exceptions=True)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    assert len(ids) == 40

    body = _open(client, project, key).text

    assert page_without_shell(body).count('<form method="post"') == 1
    assert body.count('type="checkbox"') == 42  # forty selectable, two held out
    assert body.count("disabled") == 2
    assert "Apply the selected changes" in body
    assert "Held out of this batch</dt><dd>2" in body.replace("\n", "").replace(
        "  ", ""
    )
    # Each exception is still its own item, listed and openable on its own.
    assert body.count("Open this item") == 2


# --- Needs coordination on a held-out single-source item (#659) ------------


def test_a_held_out_change_offers_needs_coordination_and_records_the_plan(
    session: Session, project: Project, client
):
    """#659: the coordinator's real answer for a lone exception is reachable.

    A held-out change is one delta answered on its own, so the screen renders
    the per-change answer form rather than a one-row batch, and the ask a
    coordinator actually has — "somebody owes me an answer before this can be
    accepted" — is one of the outcomes offered for it.
    """

    _revision(session, project, changes=3, exceptions=True)
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = [
        row for row in reading.items if row.held_out_reason == HELD_OUT_OWNER_MISMATCH
    ]
    (child,) = item.children

    body = _open(client, project, item.item_key).text
    assert f'action="/review/{project.slug}/answers"' in body
    assert "Needs coordination" in body

    saved = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": item.item_key,
            "answer_delta": str(child.delta_id),
            "answer_outcome": NEEDS_COORDINATION,
            "answer_source": "",
            "answer_question": "Does this correct the name, or did ownership move?",
            "answer_person": "",
            "answer_organization": "AT&T Texas",
            "answer_return": "2026-09-24",
        },
        follow_redirects=False,
    )

    assert saved.status_code in (200, 303), saved.text[:2000]
    (plan,) = session.scalars(
        select(DeltaFollowUpPlan).where(DeltaFollowUpPlan.project_id == project.id)
    ).all()
    assert plan.delta_id == child.delta_id
    assert plan.responsible_organization == "AT&T Texas"
    # Nothing was accepted: the answer is a question, not a resolution.
    assert not session.scalars(
        select(DeltaDisposition).where(
            DeltaDisposition.project_id == project.id,
            DeltaDisposition.delta_id == child.delta_id,
        )
    ).all()


def test_a_held_out_single_source_change_is_not_described_as_several_sources(
    session: Session, project: Project, client
):
    """The focused screen stopped meaning "sources disagree" (#659).

    A single change held out of its batch is focused now, so wording written
    for a cross-source question — "sources that answer it differently", "answer
    the others" — names a disagreement that is not there. The counts are
    unchanged; only the sentences naming them are.
    """

    _revision(session, project, changes=3, exceptions=True)
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = [
        row for row in reading.items if row.held_out_reason == HELD_OUT_OWNER_MISMATCH
    ]

    body = _open(client, project, item.item_key).text

    assert len(item.children) == 1
    assert "Sources that answer it differently" not in body
    assert "Sources answering it" in body
    assert "answer the others with" not in body


# ── One refusal presentation, shared with the focused screen (#794 card 22) ──
#
# This handler and `save_focused_answers` built nine `{"heading", "detail",
# "rows"}` dicts between them and wrote three of the sentences twice. The words
# now live in one place; these bind this screen to that place, and
# `tests/test_focused_review.py` binds the other screen to the same one.


class _Moved:
    """One refused child, shaped as the atomic packet result reports it."""

    subject_identity = "Conflict 14"
    field = "station_from"
    detail = "the accepted value moved from revision 3"


class _Result:
    refusals = (_Moved(),)


def test_the_two_review_screens_share_one_refusal_presentation():
    """One heading, one shape, and the moved sentence written once.

    The two screens differ by a single word — a coordinator *selects* changes
    on this screen and *answers* them on the focused one — so that word is the
    only thing either handler supplies.
    """

    from corridor.web.app import (
        REVIEW_REFUSAL_HEADING,
        _a_change_moved_under_the_reading,
        _item_left_the_reading,
    )

    selected = _a_change_moved_under_the_reading(_Result(), chosen="selected")
    answered = _a_change_moved_under_the_reading(_Result(), chosen="answered")

    assert selected["heading"] == answered["heading"] == REVIEW_REFUSAL_HEADING
    assert selected["detail"].replace("selected", "•") == answered[
        "detail"
    ].replace("answered", "•")
    assert selected["rows"] == answered["rows"]
    assert selected["rows"] == (
        {
            "subject": "Conflict 14 — station_from",
            "detail": "the accepted value moved from revision 3",
        },
    )
    assert set(_item_left_the_reading()) == {"heading", "detail", "rows"}


def test_this_screens_left_the_reading_refusal_is_the_shared_sentence(
    session: Session, project: Project, client
):
    """Criterion: the words a coordinator reads come from the shared place."""

    from corridor.web.app import _item_left_the_reading

    _revision(session, project, changes=1)

    response = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": "source_revision:ucm-workbook@1999-01:record_cleanup",
            "outcome": "apply",
            "child": ["1"],
        },
    )

    shared = _item_left_the_reading()
    assert response.status_code == 409
    assert shared["heading"] in response.text
    assert shared["detail"] in response.text


# --- One recorded decision, and Undo (#834) --------------------------------
#
# The saved result used to name a count and a Project Record revision with no
# link to anything, and a packet of dated Defers writes no revision at all, so
# for that act there was nothing a revision link could have pointed at. What
# every act has is its own receipt, so that is what the saved result links to
# and what carries Undo.
#
# Nothing here re-proves `reverse_review_packet`. `tests/test_review_packets.py`
# owns the compensation itself -- the restored predecessor decisions, the
# refusal when a later act depends on a result, the deferral-only act that
# writes no revision -- and `tests/test_native_follow_up_reading.py` owns the
# Follow-up Plan an undone act stops asking for. These prove the route reaches
# those behaviours and renders what they answer.

_RECEIPT_LINK = re.compile(r'href="(/review/[^"]+/packet/(\d+))"')


def _receipt_link(response) -> tuple[str, int]:
    match = _RECEIPT_LINK.search(response.text)
    assert match is not None, response.text[:2000]
    return match.group(1), int(match.group(2))


def test_every_outcome_a_child_can_carry_has_words_on_the_receipt():
    """A new decision cannot reach this screen without words for it.

    The receipt reads the outcome each child row recorded, so an outcome the
    table does not know would be a page that fails while rendering. The keys
    are #526's own, and this is the check that the two stay the same set.
    """

    from corridor.models import PACKET_CHILD_OUTCOMES
    from corridor.web.packet_receipt import OUTCOME_RECORDED

    assert set(OUTCOME_RECORDED) == set(PACKET_CHILD_OUTCOMES)


def test_the_saved_result_links_to_the_act_and_lists_every_childs_outcome(
    session: Session, project: Project, client
):
    """Criterion 1: the exact act, and what each change in it became."""

    _revision(session, project, changes=3)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)

    saved = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "apply",
            "child": [str(value) for value in ids],
        },
    )
    url, receipt_id = _receipt_link(saved)
    receipt = client.get(url)

    assert saved.status_code == 200
    assert receipt.status_code == 200
    assert receipt.text.count("Applied this source") == 3
    # The act itself, not a search of record history: every child it decided is
    # here, named by the Utility Conflict and field it moved.
    assert receipt.text.count('<th scope="row">') == 3
    recorded = session.get(DeltaReviewPacketReceipt, receipt_id)
    assert recorded.project_id == project.id
    assert str(recorded.revision_id) in receipt.text


def test_a_defer_only_save_links_to_its_receipt_and_claims_no_revision(
    session: Session, project: Project, client
):
    """Criterion 1: ADR-0084's scheduling act has a receipt and no revision."""

    _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)

    saved = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "defer",
            "child": [str(value) for value in ids],
            "defer_until": "2026-10-01",
        },
    )
    url, receipt_id = _receipt_link(saved)
    receipt = client.get(url)

    assert receipt.status_code == 200
    assert receipt.text.count("Deferred until a date") == 2
    assert "it recorded no Project Record revision," in receipt.text
    assert "none written" in receipt.text
    assert session.get(DeltaReviewPacketReceipt, receipt_id).revision_id is None


def test_a_receipt_is_read_inside_its_own_project_or_not_at_all(
    session: Session, project: Project, client
):
    """Project scope is part of the lookup, so another project's act is absent.

    The reader is exercised directly for the cross-project half: one
    transaction declares one project-authorization scope (#657, #662), so a
    test cannot make two projects' requests inside one, and the route half is
    proved by the receipt this project genuinely does not hold.
    """

    _revision(session, project, changes=1)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    saved = client.post(
        f"/review/{project.slug}",
        data={"item_key": key, "outcome": "apply", "child": [str(ids[0])]},
    )
    _, receipt_id = _receipt_link(saved)
    other = Project(
        slug=f"review-elsewhere-{uuid4().hex[:8]}", name="Elsewhere", is_synthetic=True
    )
    session.add(other)
    session.flush()

    assert read_packet_receipt(
        session, project_id=other.id, receipt_id=receipt_id
    ) is None
    assert read_packet_receipt(
        session, project_id=project.id, receipt_id=receipt_id
    ) is not None
    absent = f"/review/{project.slug}/packet/{receipt_id + 10 ** 6}"
    assert client.get(absent).status_code == 404
    assert client.post(f"{absent}/undo").status_code == 404


def test_the_undo_form_carries_the_request_forgery_token(
    session: Session, project: Project, client
):
    """Criterion 4: the one state-changing form this screen adds echoes it."""

    _revision(session, project, changes=1)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    saved = client.post(
        f"/review/{project.slug}",
        data={"item_key": key, "outcome": "apply", "child": [str(ids[0])]},
    )
    url, receipt_id = _receipt_link(saved)

    # The screen's own form, without the shell's sign-out, which carries the
    # same field on every customer page (#843).
    body = page_without_shell(client.get(url).text)

    assert f'action="/review/{project.slug}/packet/{receipt_id}/undo"' in body
    assert body.count('name="csrf_token"') == 1


def test_reading_one_act_moves_no_focus_and_answering_undo_announces_one(
    session: Session, project: Project, client
):
    """The accessibility checklist, items 2 and 4, for this screen.

    Reading a recorded act performs no act, so nothing on it claims the
    keyboard; the response to Undo has an outcome and announces exactly one.
    """

    _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    saved = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "apply",
            "child": [str(value) for value in ids],
        },
    )
    url, _ = _receipt_link(saved)

    reading = client.get(url).text
    undone = client.post(f"{url}/undo").text

    assert reading.count("autofocus") == 0
    assert reading.count("<h1") == 1
    assert reading.count("<main") == 1
    assert "<h3" not in reading
    assert f'id="{FOCUS_IDS["item"]}" tabindex="-1"' in reading
    assert undone.count("autofocus") == 1
    assert f'id="{FOCUS_IDS["outcome"]}"' in undone
    assert 'role="status"' in undone


def test_undo_compensates_the_decision_and_deletes_nothing(
    session: Session, project: Project, client
):
    """Criterion 2: the route reaches #526's reversal command, and appends."""

    _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    saved = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "apply",
            "child": [str(value) for value in ids],
        },
    )
    url, receipt_id = _receipt_link(saved)
    decided_revision = session.get(DeltaReviewPacketReceipt, receipt_id).revision_id

    response = client.post(f"{url}/undo")

    assert response.status_code == 200
    assert "This decision was undone" in response.text
    reversal = packet_reversal(session, receipt_id)
    assert reversal is not None
    assert reversal.reversed_by_principal == COORDINATOR.subject
    compensating = session.get(ProjectRecordRevision, reversal.revision_id)
    assert compensating.command_type == "reverse_review_packet"
    assert str(reversal.revision_id) in response.text
    # Nothing about the original act was removed or rewritten.
    assert session.get(DeltaReviewPacketReceipt, receipt_id).revision_id == (
        decided_revision
    )
    assert [child.delta_id for child in packet_children(session, receipt_id)] == ids
    # And the page now says so instead of offering the button again. The
    # shell's own sign-out carries the field on every customer page (#843),
    # so what is read here is the page without it.
    assert "was already undone by" in client.get(url).text
    assert "csrf_token" not in page_without_shell(client.get(url).text)


def test_undoing_a_defer_only_act_returns_the_changes_to_immediate_work(
    session: Session, project: Project, client
):
    """Criterion 2: compensating scheduling writes no revision and says so.

    A reversed scheduling receipt holds nothing out any more
    (`review_packet_reading.live_deferrals_by_project`), so the changes this
    act put away are offered again -- and the response claims no revision,
    because ADR-0084's act wrote none to compensate for.
    """

    _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    saved = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "defer",
            "child": [str(value) for value in ids],
            "defer_until": "2026-10-01",
        },
    )
    url, receipt_id = _receipt_link(saved)
    assert read_review_items(session, project_id=project.id, as_of=NOW).items == ()

    response = client.post(f"{url}/undo")
    session.expire_all()

    assert response.status_code == 200
    assert "back in immediate work" in response.text
    assert "Project record revision" not in response.text
    assert packet_reversal(session, receipt_id).revision_id is None
    returned = read_review_items(session, project_id=project.id, as_of=NOW)
    assert sorted(returned.reading.actionable_delta_ids) == sorted(ids)


def test_undo_surfaces_the_commands_own_refusal_when_a_later_act_depends(
    session: Session, project: Project, client
):
    """Criterion 2: the stale and successor rules stay PostgreSQL's.

    The screen neither anticipates the refusal nor rewrites its words: it asks
    the command, and prints the sentence the command raised with the machine
    token that routes it taken off the front.
    """

    built = _revision(session, project, changes=1)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    saved = client.post(
        f"/review/{project.slug}",
        data={"item_key": key, "outcome": "apply", "child": [str(ids[0])]},
    )
    url, receipt_id = _receipt_link(saved)
    moved, _ = built.adopted.capture(
        fact_type="station_from", value="9999+00", subject_key=subject(1)
    )
    move_accepted_value(session, project, moved)

    refused = client.post(f"{url}/undo")

    assert refused.status_code == 409
    assert 'role="alert"' in refused.text
    assert refused.text.count("autofocus") == 1
    assert (
        "a later decision already superseded a value this packet made effective"
        in refused.text
    )
    # The token is how the two halves of the rule agree, not a sentence.
    assert "review_packet:" not in refused.text
    assert packet_reversal(session, receipt_id) is None


def test_undo_reaches_no_record_beyond_the_compensation_it_appends(
    session: Session, project: Project, client
):
    """Criterion 2: an approved issue package is not something Undo can alter.

    ADR-0086 binds an approved package to the accepted revision it released,
    and that binding is a row Undo never reaches: the compensation appends one
    new revision and its decisions beside the released one, so the package goes
    on saying exactly what it said. This is the whole reach of the act, read
    over every table the project graph holds rather than the handful a test
    would otherwise pick.
    """

    _revision(session, project, changes=2)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    saved = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": key,
            "outcome": "apply",
            "child": [str(value) for value in ids],
        },
    )
    url, receipt_id = _receipt_link(saved)
    released = session.get(DeltaReviewPacketReceipt, receipt_id).revision_id
    as_released = session.get(ProjectRecordRevision, released).command_type

    with nothing_written(
        session,
        project.id,
        apart_from={
            "project_record_revisions",
            "fact_decisions",
            "delta_review_packet_reversals",
        },
    ):
        response = client.post(f"{url}/undo")

    assert response.status_code == 200
    # The revision an approved package binds is still there, saying what it
    # said; the compensation is a new one appended beside it.
    assert session.get(ProjectRecordRevision, released).command_type == as_released
    assert packet_reversal(session, receipt_id).revision_id != released


def test_undo_reaches_the_follow_up_plan_compensation_the_command_proves(
    session: Session, project: Project, client
):
    """Criterion 2: a Needs coordination answer is compensated through the route.

    What an undone plan means is decided in one place and proved there
    (`native_follow_up_reading.undone_follow_up_plan_ids`,
    `tests/test_native_follow_up_reading.py`). This proves the route arrives at
    it, and that the plan itself is still in history rather than deleted.
    """

    _revision(session, project, changes=3, exceptions=True)
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = [
        row for row in reading.items if row.held_out_reason == HELD_OUT_OWNER_MISMATCH
    ]
    (child,) = item.children
    saved = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": item.item_key,
            "answer_delta": str(child.delta_id),
            "answer_outcome": NEEDS_COORDINATION,
            "answer_source": "",
            "answer_question": "Does this correct the name, or did ownership move?",
            "answer_person": "",
            "answer_organization": "AT&T Texas",
            "answer_return": "2026-09-24",
        },
    )
    url, receipt_id = _receipt_link(saved)
    (plan,) = session.scalars(
        select(DeltaFollowUpPlan).where(DeltaFollowUpPlan.project_id == project.id)
    ).all()
    assert "Needs coordination" in client.get(url).text

    response = client.post(f"{url}/undo")
    session.expire_all()

    assert response.status_code == 200
    assert session.scalars(undone_follow_up_plan_ids((project.id,))).all() == [plan.id]
    # Compensated, not deleted: the plan and its child are still recorded.
    assert session.get(DeltaFollowUpPlan, plan.id) is not None
    assert packet_children(session, receipt_id)[0].follow_up_plan_id == plan.id


def test_a_member_without_the_coordination_designation_cannot_undo(
    session: Session, project: Project, client
):
    """Reading one act is the plain read boundary; compensating it is not."""

    _revision(session, project, changes=1)
    key = _batch_key(session, project)
    ids = _delta_ids(session, project)
    saved = client.post(
        f"/review/{project.slug}",
        data={"item_key": key, "outcome": "apply", "child": [str(ids[0])]},
    )
    url, receipt_id = _receipt_link(saved)
    seed_membership(session, project, COORDINATOR, designations=())

    reading = client.get(url)
    refused = client.post(f"{url}/undo")

    assert reading.status_code == 200
    assert refused.status_code == 403
    assert packet_reversal(session, receipt_id) is None
