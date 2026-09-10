"""The queue's reading, and the one place the cohort's words are spelled.

The classification vocabulary used to exist four times: an ordered pair list in
`web/queue.py` whose labels nothing read, a badge map in `queue.html`, a second
differently-capitalised ordered pair list in the same template, and a bare
`newly_added` literal further down it. These state it once, from
``corridor.cohort``, and state the lane refusals against the reading rather than
against a status code.
"""

import pytest
from fastapi.testclient import TestClient

from corridor.cohort import (
    COHORT_CLASSIFICATIONS,
    CLASSIFICATION_ORDER,
    CONFLICT_FLAG_N_TO_Y,
    NEWLY_ADDED,
    VERIFICATION_BLOCKED,
)
from corridor.models import Project
from corridor.principals import HumanPrincipal
from corridor.web.queue import RailEntry
from corridor.web.queue_view import (
    CLASSIFICATIONS_BY_KEY,
    LaneManifestRequired,
    NoSuchLaneManifest,
    QueueView,
    queue_view,
)
from access_support import seed_membership

COORDINATOR = HumanPrincipal("local:queue-view")


@pytest.fixture
def project(session):
    row = Project(slug="queue-view", name="Queue View", is_synthetic=True)
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR)
    return row


@pytest.fixture
def client(session):
    from corridor.web.app import app, get_human_principal, get_session

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _rail_entry(classification: str, utility_id: str) -> RailEntry:
    return RailEntry(
        candidate_id=None,
        utility_id=utility_id,
        classification=classification,
        station="10+00",
        utility_type="water",
        state="pending",
        current=False,
        dependency_id=None,
        dependency_ref=None,
        dependency_title=None,
        coordination_gaps=(),
        admission_refusal=None,
        dismissed=False,
    )


def test_the_cohort_vocabulary_is_one_ordered_sequence_with_its_own_words():
    """Three classifications, one order, one heading and one badge each."""
    assert [entry.key for entry in COHORT_CLASSIFICATIONS] == [
        CONFLICT_FLAG_N_TO_Y,
        VERIFICATION_BLOCKED,
        NEWLY_ADDED,
    ]
    assert [entry.heading for entry in COHORT_CLASSIFICATIONS] == [
        "Flipped N \N{RIGHTWARDS ARROW} Y",
        "Verification blocked",
        "Newly added",
    ]
    assert [entry.badge for entry in COHORT_CLASSIFICATIONS] == [
        "flipped N \N{RIGHTWARDS ARROW} Y",
        "verification blocked",
        "newly added",
    ]
    # The rail's sort key and the screen's lookup are built from the same
    # sequence, so neither can name a classification the other does not.
    assert CLASSIFICATION_ORDER == {CONFLICT_FLAG_N_TO_Y: 0, VERIFICATION_BLOCKED: 1, NEWLY_ADDED: 2}
    assert set(CLASSIFICATIONS_BY_KEY) == set(CLASSIFICATION_ORDER)


def test_the_rail_groups_its_members_in_the_vocabularys_order(project):
    """The screen iterates the vocabulary; it does not restate the groups."""
    reading = QueueView(
        project=project,
        lane="rehearsal",
        template="queue.html",
        lane_url="/queue/queue-view?lane=rehearsal&cohort_receipt_id=1",
        summary_url="/queue/queue-view?lane=rehearsal&cohort_receipt_id=1&summary=1",
        next_coordinate_url="/queue/queue-view?lane=rehearsal&cohort_receipt_id=1",
        remaining=3,
        waiting_statements=0,
        candidate_count=3,
        support_update_count=0,
        ordinary_reviews=(),
        rail=[
            _rail_entry(NEWLY_ADDED, "U-3"),
            _rail_entry(CONFLICT_FLAG_N_TO_Y, "U-1"),
            _rail_entry(VERIFICATION_BLOCKED, "U-2"),
        ],
        classification=CLASSIFICATIONS_BY_KEY[NEWLY_ADDED],
    )

    grouped = [
        (classification.heading, [entry.utility_id for entry in reading.rail_members(classification)])
        for classification in reading.classifications
    ]

    assert grouped == [
        ("Flipped N \N{RIGHTWARDS ARROW} Y", ["U-1"]),
        ("Verification blocked", ["U-2"]),
        ("Newly added", ["U-3"]),
    ]
    # The "not in the predecessor revision" note is asked for by name, not by a
    # string literal compared inside the template.
    assert reading.row_is_new is True


def test_a_row_of_another_classification_is_not_reported_as_new(project):
    reading = QueueView(
        project=project,
        lane="rehearsal",
        template="queue.html",
        lane_url="/queue/queue-view?lane=rehearsal&cohort_receipt_id=1",
        summary_url="/queue/queue-view?lane=rehearsal&cohort_receipt_id=1&summary=1",
        next_coordinate_url="/queue/queue-view?lane=rehearsal&cohort_receipt_id=1",
        remaining=1,
        waiting_statements=0,
        candidate_count=1,
        support_update_count=0,
        ordinary_reviews=(),
        classification=CLASSIFICATIONS_BY_KEY[VERIFICATION_BLOCKED],
    )

    assert reading.row_is_new is False


def test_a_pinned_lane_refuses_without_its_own_input_manifest(session, project):
    """Membership is the receipt's fact, so the receipt is required to read it."""
    with pytest.raises(LaneManifestRequired):
        queue_view(session, project=project, lane="rehearsal")
    with pytest.raises(LaneManifestRequired):
        queue_view(session, project=project, lane="events")
    with pytest.raises(NoSuchLaneManifest):
        queue_view(session, project=project, lane="rehearsal", cohort_receipt_id=987654)
    with pytest.raises(NoSuchLaneManifest):
        queue_view(
            session, project=project, lane="events", event_cohort_receipt_id=987654
        )


def test_an_empty_ordinary_lane_names_its_own_page_and_its_counts(session, project):
    reading = queue_view(session, project=project, lane="candidate")

    assert reading.template == "empty.html"
    assert reading.candidate is None
    assert reading.remaining == 0
    assert reading.candidate_count == 0
    assert reading.waiting_statements == 0
    assert reading.lane_url == f"/queue/{project.slug}?lane=candidate"
    assert reading.summary_url == f"/queue/{project.slug}?lane=candidate&summary=1"
    assert reading.redirect_to is None


def test_the_retired_reconfirmation_lane_is_part_of_the_reading(session, project):
    """The retired ceremony points at the Work List, and says so once."""
    reading = queue_view(session, project=project, lane="reconfirmation")

    assert reading.redirect_to == f"/work/{project.slug}"
    assert reading.candidate is None


def test_the_handler_renders_the_reading_and_carries_its_refusals(
    client, session, project
):
    """A thin check that the route keeps only the params, the gate and the status."""
    empty = client.get(f"/queue/{project.slug}")
    assert empty.status_code == 200
    assert "Queue View" in empty.text

    assert (
        client.get(
            f"/queue/{project.slug}?lane=reconfirmation", follow_redirects=False
        ).status_code
        == 303
    )
    assert client.get(f"/queue/{project.slug}?lane=rehearsal").status_code == 400
    assert (
        client.get(
            f"/queue/{project.slug}?lane=rehearsal&cohort_receipt_id=987654"
        ).status_code
        == 404
    )
