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
from corridor.db import engine
from corridor.models import DeltaDisposition, DeltaFollowUpPlan, Project
from corridor.packet_review import (
    CUSTOMER_WORKBOOK,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import (
    HELD_OUT_OWNER_MISMATCH,
    SOURCE_REVISION,
)
from corridor.review_packets import NEEDS_COORDINATION
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)
from corridor.web.ui_primitives import FOCUS_IDS

from access_support import seed_membership
from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    modify,
    move_accepted_value,
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
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


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
    assert "Selected now" in body
    assert "Held out of this batch" in body
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

    assert closed.count("<form method=\"post\"") == 0
    assert opened.count("<form method=\"post\"") == 1
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


def test_the_primary_actions_name_their_scope_and_the_selected_count(
    session: Session, project: Project, client
):
    """Criterion 7, including the secondary dated Defer."""

    _revision(session, project, changes=3)
    body = _open(client, project, _batch_key(session, project)).text

    assert "Apply 3 selected changes" in body
    assert "Keep current for 3 selected changes" in body
    assert "Defer 3 selected changes until the date above" in body
    assert "Return date for Defer" in body
    assert "Leaving an item records nothing." in body
    # ADR-0085 rejected a second customer word for the same act.
    assert "Snooze" not in body


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
    assert sorted(remaining.actionable_delta_ids) == sorted(ids[1:])


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
    assert remaining.actionable_delta_ids == (ids[1],)
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

    assert body.count('<form method="post"') == 1
    assert body.count('type="checkbox"') == 42  # forty selectable, two held out
    assert body.count("disabled") == 2
    assert "Apply 40 selected changes" in body
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
