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
from jinja2 import UndefinedError

from corridor.cohort import (
    COHORT_CLASSIFICATIONS,
    CLASSIFICATION_ORDER,
    CONFLICT_FLAG_N_TO_Y,
    NEWLY_ADDED,
    VERIFICATION_BLOCKED,
)
from corridor.models import (
    Dependency,
    DocPage,
    Document,
    Project,
    ProjectRosterEntry,
)
from corridor.principals import HumanPrincipal
from corridor.work_decisions import (
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    UNKNOWN_DUE_DATE_REASONS,
)
from corridor.web.app import TEMPLATES
from corridor.web.dependency_view import PlanForm
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


# --------------------------------- the shared partials, as declared interfaces
#
# `_coordinate.html` and `_evidence.html` were composed with `{% include %}`,
# which hands a partial the whole enclosing context and therefore declares no
# interface at all. `_coordinate.html` read eleven names that way, ten of them
# supplied by a five-line `{% set %}` prelude that `diff` found byte-identical
# in `queue.html` and `empty.html`, and nothing anywhere named either file.
# They are macros now, and these render them the way
# `tests/test_ui_primitives.py` renders `_primitives.html`: against an
# explicit context, with no HTTP and no database.


def _plan_form() -> PlanForm:
    return PlanForm(
        roster=(ProjectRosterEntry(id=4, display_name="Dana Reyes"),),
        next_action_choices=FOLLOW_UP_NEXT_ACTION_CHOICES,
        unknown_date_reasons=tuple(sorted(UNKNOWN_DUE_DATE_REASONS)),
        no_follow_up_reasons=(),
        cancellation_reasons=(),
        deferral_reasons=(),
        expected_internal_owner_decision_id=311,
        expected_next_action_decision_id=412,
    )


def _strip(source: str, **context) -> str:
    return TEMPLATES.env.from_string(
        '{% import "_coordinate.html" as coordination with context %}' + source
    ).render(**context)


def _coordinate_call(template_name: str):
    """The one `{{ coordination.coordinate(...) }}` a host page makes."""
    import jinja2
    from jinja2 import nodes

    tree = jinja2.Environment(autoescape=True).parse(
        TEMPLATES.env.loader.get_source(TEMPLATES.env, template_name)[0]
    )
    calls = [
        node
        for node in tree.find_all(nodes.Call)
        if isinstance(node.node, nodes.Getattr) and node.node.attr == "coordinate"
    ]
    assert len(calls) == 1, f"{template_name} calls the strip once"
    return calls[0]


def test_the_coordination_strip_renders_from_its_parameters_alone():
    """Its interface is the parameter list, not the enclosing page."""
    markup = _strip(
        "{{ coordination.coordinate(dependency, plan, lane_url, summary_url,"
        " next_url, cohort_receipt, project) }}",
        dependency=Dependency(
            id=51, ref_code="UC-014", title="12-inch gas main", internal_owner=None
        ),
        plan=_plan_form(),
        lane_url="/queue/acme?lane=candidate",
        summary_url="/queue/acme?lane=candidate&summary=1",
        next_url="/queue/acme?lane=candidate&coordinate=52",
        cohort_receipt=None,
        project=Project(slug="acme", name="Acme Interchange"),
    )

    assert "Recorded UC-014" in markup and "12-inch gas main" in markup
    assert "Dana Reyes" in markup
    assert 'action="/dependencies/51/plan"' in markup
    for choice in FOLLOW_UP_NEXT_ACTION_CHOICES:
        assert choice in markup
    # The skip link reads "next item" only when there is one to go to.
    assert "next item" in markup


def test_the_strip_carries_the_exact_decision_tails_the_save_binds_to():
    """`PlanForm` travels whole, so the stale check cannot arrive half-set.

    `save_follow_up_plan` matches both expected ids against the current tails
    exactly, and the strip is where a coordinator reads them. They used to be
    two hand-copied `{% set %}` lines per host page.
    """
    markup = _strip(
        "{{ coordination.coordinate(dependency, plan, lane_url, summary_url,"
        " next_url, cohort_receipt, project) }}",
        dependency=Dependency(id=51, ref_code="UC-014", title="main"),
        plan=_plan_form(),
        lane_url="/l",
        summary_url="/s",
        next_url="/n",
        cohort_receipt=None,
        project=Project(slug="acme", name="Acme"),
    )

    assert (
        '<input type="hidden" name="expected_internal_owner_decision_id"\n'
        '             value="311">' in markup
    )
    assert (
        '<input type="hidden" name="expected_next_action_decision_id"\n'
        '             value="412">' in markup
    )


def test_a_call_site_that_leaves_out_the_plan_is_refused():
    """What an `{% include %}` rendered blank, a macro call refuses.

    A missing prelude line used to reach the browser as
    `value=""`, which `_optional_form_id` turns into `None`. The parameter
    list is the interface now, and leaving one out is an error at the seam.
    """
    with pytest.raises(UndefinedError) as refusal:
        _strip(
            "{{ coordination.coordinate(dependency, lane_url, summary_url,"
            " next_url, cohort_receipt, project) }}",
            dependency=Dependency(id=51, ref_code="UC-014", title="main"),
            lane_url="/l",
            summary_url="/s",
            next_url="/n",
            cohort_receipt=None,
            project=Project(slug="acme", name="Acme"),
        )

    assert "project" in str(refusal.value)


def test_both_pages_call_the_coordination_strip_with_the_same_arguments():
    """One prelude became one call; the two hosts may not drift apart.

    The five `{% set %}` lines this replaced were byte-identical in
    `queue.html` and `empty.html` and nothing checked that they stayed so. A
    drifted copy rendered an empty stale-check value rather than failing.
    """
    queue_call = _coordinate_call("queue.html")
    empty_call = _coordinate_call("empty.html")

    assert queue_call.args == empty_call.args
    assert (queue_call.kwargs, empty_call.kwargs) == ([], [])


def test_an_evidence_pane_renders_one_page_from_its_one_parameter():
    """`_evidence.html` documented `ev` in prose; it is the signature now."""
    markup = TEMPLATES.env.from_string(
        '{% import "_evidence.html" as evidence_pane %}'
        "{{ evidence_pane.evidence(ev) }}"
    ).render(
        ev={
            "document": Document(id=3, filename="northern-gas-letter.pdf"),
            "page": DocPage(page_no=12, text="page text", text_source="ocr"),
            "page_no": 12,
            "quote": "the 12-inch main will be relocated",
            "highlights": [],
            "label": "the other revision",
        }
    )

    assert "northern-gas-letter.pdf" in markup
    assert "the 12-inch main will be relocated" in markup
    assert "the other revision" in markup
    # How the page's text was obtained is stated in words, not colour alone.
    assert "read by OCR" in markup
