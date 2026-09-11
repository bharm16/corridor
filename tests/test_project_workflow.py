"""One adopted project's week, in the order it runs (#536).

#527 and #528 built the review screen and deliberately left `/work/{slug}`
alone, so `/review/{slug}` had no way in at all.  This is the surface that
opens a project: review, then the follow-up the project itself recorded a plan
for, then the issue that goes back to the customer, with the landing section
derived from those records rather than stored anywhere.

The properties under test are the ones the ticket names.  Four project shapes
land on the correct section.  The page carries no decision control of its own,
so no Proposed Delta grows a second one (ADR-0085).  Reading the page — and
moving between its sections — writes nothing at all.  Source-coverage and
rendering problems appear as issue readiness and never as a record decision.
A legacy project keeps ADR-0035's item-per-record Work List untouched.

And one honesty guard: ADR-0085's three visible consequence levels are *not*
printed, because ADR-0091 records that the per-project configured issue set
they project onto is not modelled.  A test asserts their absence so a later
change adding them has to be deliberate.

Nothing here reads a clock.  The cutoff is declared by the test.
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import re
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.consequence_levels import AFFECTS_ISSUE, LEVEL_HEADINGS
from corridor.analytics import EventFamily, capture_events
from corridor.issue_content import NO_ISSUE_PROFILE
from corridor.models import (
    DeltaReviewPacketReceipt,
    Project,
)
from corridor.operating_mode import adopt_project_baseline
from corridor.packet_review import (
    FocusedAnswer,
    focused_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.project_workflow import (
    FOLLOW_UP,
    ISSUE,
    NO_OUTPUT_TEMPLATE,
    OPERATIONS_OWNER,
    REVIEW,
    SECTION_ORDER,
    UNREAD_SOURCE,
    read_project_workflow,
)
from corridor.review_packets import (
    KEEP_CURRENT,
    NEEDS_COORDINATION,
    resolve_review_packet,
    reverse_review_packet,
)
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)

from access_support import seed_membership
from record_counts import nothing_written
from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    configure_issue,
    field_mapping,
    register_field_mapping,
    append_deltas,
    modify,
    register_baseline,
    register_output_template,
    register_source_row,
    subject,
    support,
)


COORDINATOR = HumanPrincipal("local:coordinator")
OUTSIDER = HumanPrincipal("local:outsider")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
# When this project's issue profile took effect. Strictly before every cutoff
# read below, because a profile answers only for cutoffs at or after its own
# effective instant (#640).
CONFIGURED_FROM = datetime(2026, 1, 5, tzinfo=timezone.utc)
# The canonical field behind the label "Promised for"; the fixture's proposed
# changes all move it.
COMMITTED = "committed_date"
RETURNS_AT = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
CONFLICT = 42
OTHER = 43

# ADR-0085's three accepted headings, read back from the module that owns the
# derivation rather than respelled here, so this file cannot drift from the
# words the decision settled.
VISIBLE_LEVELS = tuple(LEVEL_HEADINGS.values())


def _project(session: Session, name: str) -> Project:
    row = Project(
        slug=f"workflow-{uuid4().hex[:8]}", name=name, is_synthetic=True
    )
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR)
    return row


@pytest.fixture
def project(session: Session) -> Project:
    return _project(session, "Workflow")


@pytest.fixture
def client(session):
    """The app shares the test's transaction and the test's declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


# --- the fixture: one adopted project with a later revision ----------------


class Adopted:
    """An adopted-baseline project, built only as far as each test needs.

    ``accepted`` writes the baseline the incoming values are compared against
    and registers the customer's own rows; ``template`` registers the approved
    output template; ``adopt`` writes the one immutable receipt the operating
    mode is derived from.  A test that wants a quiet project calls ``adopt``
    alone.
    """

    ACCEPTED = "2026-11-01"

    def __init__(self, session: Session, project: Project):
        self.session = session
        self.project = project
        self.source = Rendition(session, project, "ucm-2026-08.xlsx")
        self.revision_of: dict[tuple[str, str], int] = {}
        self.baseline = None
        self.renditions: dict[str, Rendition] = {}

    def accepted(self, *numbers: int) -> "Adopted":
        first: int | None = None
        for number in numbers:
            fact, _ = self.source.capture(
                fact_type="committed_date",
                value=self.ACCEPTED,
                subject_key=subject(number),
            )
            revision = accept_baseline_fact(self.session, self.project, fact)
            first = first or revision
            self.revision_of[(subject(number), "committed_date")] = revision
        assert first is not None
        self.baseline = register_baseline(
            self.session, self.project, self.source.document, first
        )
        for number in numbers:
            register_source_row(
                self.session,
                self.project,
                self.baseline,
                row_number=number,
                business_identity=f"U-{number:03d}",
            )
        return self

    def template(self) -> "Adopted":
        register_output_template(
            self.session, self.project, identity="district-ucm-template", version="v3"
        )
        return self

    def issued(self, *fields: str) -> "Adopted":
        """Configure what this project externally issues, and how (#640, #641).

        A project that has not configured its issued set cannot produce an
        issue at all under ADR-0091, so a test that wants a clean Issue
        readiness has to say what the customer receives.
        """

        register_field_mapping(
            self.session, self.project, field_mapping(*fields or (COMMITTED,))
        )
        configure_issue(
            self.session,
            self.project,
            principal=COORDINATOR,
            effective_from=CONFIGURED_FROM,
        )
        return self

    def adopt(self) -> "Adopted":
        adopt_project_baseline(
            self.session,
            project_id=self.project.id,
            adopted_by_principal="local:adopter",
            baseline_source_sha256=self.source.document.sha256,
            importer_identity="project_workflow_fixture",
            importer_version="v1",
            idempotency_key=f"adopt:{uuid4().hex[:10]}",
        )
        self.session.expire_all()
        return self

    def rendition(self, name: str) -> Rendition:
        if name not in self.renditions:
            self.renditions[name] = Rendition(self.session, self.project, name)
        return self.renditions[name]

    def answer(
        self, *, document: str, family: str, revision: str, value: str,
        number: int = CONFLICT,
    ):
        """One source's own answer for a Promised For, captured and supported."""

        rendition = self.rendition(document)
        fact, segment = rendition.capture(
            fact_type="committed_date",
            value=value,
            subject_key=subject(number),
        )
        support(self.session, self.project, fact, segment)
        return append_deltas(
            self.session,
            self.project,
            rendition,
            source_revision=revision,
            source_family=family,
            values=[
                modify(
                    subject_key=subject(number),
                    field_name="committed_date",
                    accepted_value=self.ACCEPTED,
                    proposed_value=value,
                    baseline_revision=self.revision_of[
                        (subject(number), "committed_date")
                    ],
                )
            ],
            is_complete_enumerative_source=False,
            row_accounting_sealed=False,
        )


def test_the_week_records_all_ten_children_before_the_packet_is_opened(session, project, client):
    fixture = Adopted(session, project).accepted(*range(1, 11)).template().issued().adopt()
    for number in range(1, 11):
        fixture.answer(document="revised-ucm.xlsx", family="ucm-workbook", revision="2026-09",
                       value="2026-12-01", number=number)
    with capture_events() as captured:
        response = client.get(f"/work/{project.slug}")
    assert response.status_code == 200
    surfaced = captured.by_family(EventFamily.PACKET_SURFACING)
    assert len(surfaced) == 1
    assert surfaced[0].payload["child_count"] == 10
    assert len(surfaced[0].payload["child_consequences"]) == 10
    assert surfaced[0].payload["principal_subject"] == COORDINATOR.subject
    assert captured.by_family(EventFamily.PACKET_OPENING) == []
    assert len(captured.by_family(EventFamily.PROJECT_OPENING)) == 1
    assert len(captured.by_family(EventFamily.COVERAGE_READING)) == 1


def _cross_source(session: Session, project: Project) -> Adopted:
    """Two retained sources answering one Promised For differently.

    A cross-source coordination question is the only shape whose children can
    each be answered Needs coordination, which is how a Follow-up Plan gets
    recorded at all today (#528, #526).
    """

    adopted = Adopted(session, project).accepted(CONFLICT, OTHER).template()
    adopted.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value="2026-12-15",
    )
    adopted.answer(
        document="minutes-2026-09-02.pdf",
        family="meeting-minutes",
        revision="2026-09-02",
        value="2027-01-20",
    )
    return adopted.adopt()


def _plan_every_child(session: Session, project: Project) -> int:
    """Answer every source on the one focused item Needs coordination."""

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = next(item for item in reading.items if item.focused)
    request = focused_request(
        reading,
        item,
        principal=COORDINATOR,
        decided_at=NOW,
        answers=[
            FocusedAnswer(
                delta_id=child.delta_id,
                outcome=NEEDS_COORDINATION,
                question="Which date does the utility actually hold to?",
                responsible_organization="City Water",
                return_date=RETURNS_AT,
            )
            for child in item.children
        ],
    )
    result = resolve_review_packet(session, request)
    assert result.status == "saved", result
    session.expire_all()
    return len(item.children)


# --- where a project opens -------------------------------------------------


def test_an_adopted_project_opens_on_its_own_ordered_week(session, project, client):
    """The review screen finally has a way in, and it is the project's home."""

    _cross_source(session, project)

    page = client.get(f"/work/{project.slug}")

    assert page.status_code == 200
    body = page.text
    assert f'href="/review/{project.slug}"' in body
    positions = [
        body.index(heading)
        for heading in (
            "Source changes to review",
            "Follow-up Plans waiting on an answer",
            "The issue to approve for sharing",
        )
    ]
    assert positions == sorted(positions), "the three sections run in one order"
    assert "Review Packet" not in body
    assert "Release Package" not in body


def test_a_legacy_project_keeps_the_item_per_record_work_list(session, client):
    """ADR-0085 amends only the adopted-project presentation."""

    legacy = _project(session, "Legacy")

    page = client.get(f"/work/{legacy.slug}")

    assert page.status_code == 200
    assert "Work needing attention now" in page.text
    assert "The issue to approve for sharing" not in page.text


def test_a_review_only_project_lands_on_review(session, project):
    _cross_source(session, project)

    workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)

    assert workflow.landing == REVIEW
    assert workflow.section(REVIEW).outstanding == len(workflow.undecided) == 1
    assert workflow.follow_up == ()


def test_a_follow_up_only_project_lands_on_follow_up(session, project):
    """A question whose every part is already planned waits on someone else."""

    _cross_source(session, project)
    planned = _plan_every_child(session, project)

    workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)

    assert workflow.landing == FOLLOW_UP
    assert workflow.section(REVIEW).outstanding == 0
    assert len(workflow.follow_up) == planned
    assert workflow.review.items, "the review screen still offers the item itself"
    for need in workflow.follow_up:
        assert need.subject_name == f"U-{CONFLICT:03d}"
        assert need.open_question.startswith("Which date")
        assert need.responsible == "City Water"


def test_a_ready_to_issue_project_lands_on_the_issue(session, project):
    """Nothing to decide, nobody to chase, and an accepted record to issue."""

    Adopted(session, project).accepted(CONFLICT).template().issued().adopt()

    workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)

    assert workflow.landing == ISSUE
    assert workflow.readiness == ()
    assert workflow.accepted_revision_id is not None


def test_a_quiet_project_lands_at_the_top_and_says_so(session, project, client):
    """An adopted project with nothing accepted has nothing waiting anywhere."""

    Adopted(session, project).adopt()

    workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)
    assert workflow.landing == REVIEW
    assert [section.waiting for section in workflow.sections] == [False] * 3

    page = client.get(f"/work/{project.slug}")
    assert "Nothing in this project needs attention at this cutoff." in page.text


# --- what belongs in issue readiness, and what does not --------------------


def test_coverage_and_rendering_problems_are_issue_readiness_not_decisions(
    session, project, client
):
    """An unread source, an unregistered template, and an unconfigured issue.

    None of the three is a difference between a source and the accepted record,
    so none can reach a record decision. ADR-0091 makes the issued set
    per-project configuration, so a project that has not configured one cannot
    produce an issue either, and saying so belongs here beside the other two
    (#641).
    """

    adopted = Adopted(session, project).accepted(CONFLICT)
    unread = adopted.rendition("permit-2026-09.pdf")
    unread.document.parse_status = "failed"
    session.flush()
    adopted.adopt()

    workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)

    assert {problem.code for problem in workflow.readiness} == {
        NO_OUTPUT_TEMPLATE,
        NO_ISSUE_PROFILE,
        UNREAD_SOURCE,
    }
    assert workflow.review.items == (), "nothing here is a record decision"
    assert workflow.landing == ISSUE

    body = client.get(f"/work/{project.slug}").text
    readiness_at = body.index("The issue to approve for sharing")
    assert body.index("permit-2026-09.pdf") > readiness_at


def test_every_readiness_problem_names_who_puts_it_right_and_what_happens_next(
    session, project, client
):
    """A blocker that says only what is wrong is half a sentence (#840).

    All three of these are operations or configuration facts, so none of them
    is something the person reading the page can put right; saying so, and
    naming who does, is the difference between a screen that explains a dead
    end and one that gets somebody out of it. The owner is not a designation
    claim — nothing gates any of this on a designation today — it is who does
    the work.
    """

    adopted = Adopted(session, project).accepted(CONFLICT)
    unread = adopted.rendition("permit-2026-09.pdf")
    unread.document.parse_status = "failed"
    session.flush()
    adopted.adopt()

    problems = read_project_workflow(
        session, project_id=project.id, as_of=NOW
    ).readiness

    assert problems
    for problem in problems:
        assert problem.owner, problem.code
        assert problem.next_action, problem.code
    # All three are Corridor's own work rather than the reader's, and the page
    # says which of the two it is rather than leaving it to be guessed.
    assert {problem.owner for problem in problems} == {OPERATIONS_OWNER}

    # Unescaped, because an apostrophe is `&#39;` in the served bytes and a
    # test that spelled the entity would be testing Jinja rather than the
    # words a coordinator reads.
    body = html.unescape(client.get(f"/work/{project.slug}").text)
    assert "Who puts it right" in body
    assert "What happens next" in body
    for problem in problems:
        assert problem.owner in body
        assert problem.next_action in body


# --- exactly once, and nothing recorded ------------------------------------


def test_the_week_carries_no_decision_control_of_its_own(session, project, client):
    """ADR-0085's exactly-once rule survives a second surface listing the work."""

    _cross_source(session, project)

    body = client.get(f"/work/{project.slug}").text

    assert "<form" not in body
    assert "<button" not in body
    assert "<input" not in body
    for token in ("apply", "keep_current", "needs_coordination", "defer"):
        assert f'value="{token}"' not in body


def test_a_proposed_change_is_offered_on_one_screen_only(session, project, client):
    """The week names the work; only the review screen offers to decide it."""

    _cross_source(session, project)
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    delta_ids = [
        child.delta_id for item in reading.items for child in item.children
    ]
    assert delta_ids

    week = client.get(f"/work/{project.slug}").text
    # Only the opened item carries controls, so exactly-once is counted across
    # every item the review screen can open, not on one rendering of it.
    opened = "".join(
        client.get(
            f"/review/{project.slug}?item={quote(item.item_key, safe='')}"
        ).text
        for item in reading.items
    )

    for delta_id in delta_ids:
        offered = re.findall(rf'name="child"\s+value="{delta_id}"', opened)
        offered += re.findall(rf'name="answer_delta" value="{delta_id}"', opened)
        assert len(offered) == 1, f"delta {delta_id} is offered {len(offered)} times"
        assert f'value="{delta_id}"' not in week


def test_opening_and_moving_between_sections_records_nothing(
    session, project, client
):
    """Leaving a section is not an act; only the explicit decisions are."""

    _cross_source(session, project)

    # The page view appends a `product_proving_frontend_request` receipt, which
    # is a server-observed request rather than a decision about the project.
    with nothing_written(session, project.id, apart_from={"audit_log"}):
        for _ in range(3):
            assert client.get(f"/work/{project.slug}").status_code == 200


def test_the_reading_is_derived_and_repeats_itself(session, project):
    """No stored close status, owner, cadence, or completion flag exists."""

    _cross_source(session, project)

    first = read_project_workflow(session, project_id=project.id, as_of=NOW)
    second = read_project_workflow(session, project_id=project.id, as_of=NOW)

    assert first.sections == second.sections
    assert first.landing == second.landing
    assert [name for name in SECTION_ORDER] == [
        section.name for section in first.sections
    ]


def test_an_undone_packet_leaves_no_outside_ask(session, project):
    """A recorded act that was reversed asks nobody for anything."""

    _cross_source(session, project)
    _plan_every_child(session, project)
    assert read_project_workflow(
        session, project_id=project.id, as_of=NOW
    ).follow_up

    receipt = session.scalars(
        select(DeltaReviewPacketReceipt).where(
            DeltaReviewPacketReceipt.project_id == project.id
        )
    ).one()
    result = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=receipt.id,
        principal=COORDINATOR,
        reversed_at=NOW,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )
    assert result.status == "reversed", result
    session.expire_all()

    workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)
    assert workflow.follow_up == ()
    assert workflow.landing == REVIEW


def test_a_settled_question_stops_being_an_outside_ask(session, project):
    """A plan leaves the week when the change it was raised on is decided.

    No status is stored on the plan and none is needed: the ask is live
    exactly while its Proposed Delta is open, which is the same open set the
    review reading already keeps (#494).
    """

    _cross_source(session, project)
    _plan_every_child(session, project)
    before = read_project_workflow(session, project_id=project.id, as_of=NOW)
    assert len(before.follow_up) == 2

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = next(item for item in reading.items if item.focused)
    settled = item.children[0].delta_id
    result = resolve_review_packet(
        session,
        focused_request(
            reading,
            item,
            principal=COORDINATOR,
            decided_at=NOW,
            answers=[FocusedAnswer(delta_id=settled, outcome=KEEP_CURRENT)],
        ),
    )
    assert result.status == "saved", result
    session.expire_all()

    after = read_project_workflow(session, project_id=project.id, as_of=NOW)
    assert [need.delta_id for need in after.follow_up] == [
        need.delta_id for need in before.follow_up if need.delta_id != settled
    ]


# --- how it reads, and what it refuses to claim ----------------------------


def test_the_landing_section_is_the_one_element_that_takes_focus(
    session, project, client
):
    """A keyboard user starts where the work is, not at the document title."""

    _cross_source(session, project)

    body = client.get(f"/work/{project.slug}").text

    assert body.count("autofocus") == 1
    autofocused = re.search(r'id="([a-z_]+)" tabindex="-1" autofocus', body)
    assert autofocused is not None and autofocused.group(1) == REVIEW


def test_every_section_is_a_region_bound_to_its_own_heading(
    session, project, client
):
    """Each section is reachable by heading and by landmark, in one order."""

    _cross_source(session, project)

    body = client.get(f"/work/{project.slug}").text

    for name in SECTION_ORDER:
        assert f'aria-labelledby="{name}-heading"' in body
        assert f'<h2 id="{name}-heading">' in body


def test_the_page_reads_as_one_document_with_descending_headings(
    session, project, client
):
    """`docs/accessibility-acceptance-checklist.md` §2, for this screen."""

    _cross_source(session, project)

    body = client.get(f"/work/{project.slug}").text

    assert body.count("<main>") == 1
    assert body.count("<h1>") == 1
    levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", body)]
    assert levels[0] == 1
    for previous, level in zip(levels, levels[1:]):
        assert level <= previous + 1, "a heading level is skipped"
    assert body.count('<table class="record"') == body.count("<caption>")


def test_the_visible_consequence_level_is_the_derived_one(
    session, project, client
):
    """#536 asserted these three strings were absent; #641 derives them.

    The guard is replaced rather than deleted, because a deleted guard and a
    satisfied one look identical in a diff. What it now asserts is the whole
    point of the replacement: the level this page prints is the one
    ``packet_review`` derived from the project's configured issue content, and
    the other two headings are not printed at all — an unconfigured project
    still prints none of them, which is the old assertion's real content.
    """

    _cross_source(session, project).issued()

    workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)
    body = client.get(f"/work/{project.slug}").text

    derived = {item.consequence_heading for item in workflow.undecided}
    assert derived == {LEVEL_HEADINGS[AFFECTS_ISSUE]}
    for level in VISIBLE_LEVELS:
        assert (level in body) is (level in derived)


def test_an_unconfigured_project_prints_no_consequence_level_at_all(
    session, project, client
):
    """The absence #536 guarded, now stated with its reason beside it.

    A project whose issued set is not configured has nothing to project a level
    onto, so none of the three headings appears — and the page says why in
    Issue readiness rather than defaulting to one of them.
    """

    _cross_source(session, project)

    body = client.get(f"/work/{project.slug}").text

    for level in VISIBLE_LEVELS:
        assert level not in body
    assert "not derived" in body
    assert NO_ISSUE_PROFILE in {
        problem.code
        for problem in read_project_workflow(
            session, project_id=project.id, as_of=NOW
        ).readiness
    }


def test_a_non_member_is_answered_exactly_like_a_missing_project(
    session, project, client
):
    """Membership gates the week exactly as it gates every other surface."""

    _cross_source(session, project)
    app.dependency_overrides[get_human_principal] = lambda: OUTSIDER

    assert client.get(f"/work/{project.slug}").status_code == 404
    assert client.get("/work/no-such-project").status_code == 404


def test_the_cutoff_comes_from_the_caller_never_a_clock(session, project, client):
    """The declared instant bounds the reading and is what the page prints."""

    _cross_source(session, project)

    body = client.get(f"/work/{project.slug}").text

    assert "Sources captured up to 2026-09-03" in body
    workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)
    assert workflow.cutoff == NOW
