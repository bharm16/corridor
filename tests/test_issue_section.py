"""The Issue section of one project's ordered week, and its one act (#536).

#529 prepared an immutable release candidate and #533 authorized one, and both
merged with no route at all: there was no release surface in `web/app.py` to
extend, so nobody could see what had been prepared for a customer or approve
it. These are the properties of the section that finally shows it.

What is proved here is mostly what the screen refuses to do. It does not derive
readiness a second time, so a blocked candidate is un-offerable by #529's own
answer and not by a rule spelled again in a template. It does not check the
external-release designation in Python, so an undesignated principal is refused
by PostgreSQL and reads the database's own sentence. It invents no predecessor
before a project's first issue. And a stale candidate is shown as it was
prepared, with the fresh preparation asked for rather than the approval.

Nothing here reads a clock. Every cutoff, preparation instant and release
instant is declared by the test, and the web routes take theirs from
`get_review_clock`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import html
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor.access import COORDINATION, EXTERNAL_RELEASE, enroll_member
from corridor.config import settings
from corridor.db import Session, engine
from corridor.issue_profile import ArtifactEntry, DecisionBlockingPolicy, RendererRevision
from corridor.issue_content import (
    CHASE_LIST_IDENTITY,
    CHASE_LIST_VERSION,
    UCM_RENDERER_IDENTITY,
    UCM_RENDERER_VERSION,
    WEEKLY_REPORT_IDENTITY,
    WEEKLY_REPORT_VERSION,
)
from corridor.issue_rendering import (
    NO_PRIOR_COMPARISON_STATEMENT,
    ReportSection,
    SECTION_COMMITMENTS,
    SECTION_CONSTRAINT_ALERTS,
    SECTION_FOLLOW_UP_PLANS,
    SECTION_KEY_DATES,
    SECTION_PENDING_COORDINATION,
    SourceCoverage,
    TemplateBinding,
)
from corridor.models import Project, ReleaseCandidate, ReleasePackage
from corridor.object_storage import content_store
from corridor.principals import HumanPrincipal
from corridor.release_candidate import (
    CoverageDeclaration,
    attach_candidate,
    bind_preparation,
    current_release_candidate,
    render_candidate_artifacts,
)
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)
from corridor.web.issue_section import (
    ALREADY_AUTHORIZED,
    AUTHORIZABLE,
    NOTHING_PREPARED,
    NOT_AUTHORIZABLE,
    IssueViewRefused,
    issue_view,
)

from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes
from packet_review_support import Rendition, append_deltas, configure_issue, modify, subject


COORDINATOR = HumanPrincipal("local:coordinator")
RELEASER = HumanPrincipal("local:releaser")
UNDESIGNATED = HumanPrincipal("local:reader")
OPERATOR = HumanPrincipal("local:operator")

JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc)
PREPARED_AT = datetime(2026, 3, 2, 7, 0, tzinfo=timezone.utc)
# The instant every web request in this file is read and decided at.
NOW = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)

UCM_RENDERER = RendererRevision(UCM_RENDERER_IDENTITY, UCM_RENDERER_VERSION)
WEEKLY = ArtifactEntry(
    "weekly_coordination_report",
    RendererRevision(WEEKLY_REPORT_IDENTITY, WEEKLY_REPORT_VERSION),
)
CHASE = ArtifactEntry(
    "chase_list", RendererRevision(CHASE_LIST_IDENTITY, CHASE_LIST_VERSION)
)

TEMPLATE = TemplateBinding(
    template_identity="partner-weekly",
    template_version="3",
    mapping_identity="partner-weekly-mapping",
    mapping_version="2",
    sections=(
        ReportSection(key=SECTION_CONSTRAINT_ALERTS, heading="Items needing attention"),
        ReportSection(key=SECTION_COMMITMENTS, heading="Utility commitments"),
        ReportSection(key=SECTION_KEY_DATES, heading="Dates we need work by"),
        ReportSection(key=SECTION_FOLLOW_UP_PLANS, heading="Our next steps"),
        ReportSection(
            key=SECTION_PENDING_COORDINATION, heading="Open questions with owners"
        ),
    ),
)

# A customer policy that waits on exactly the field an undecided change moves.
# This is what makes a candidate `blocked` rather than merely honest about an
# open difference (#641 executes the selector).
RESOLVE_COMMITTED_DATE = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision="field:committed_date",
    statement="A moved Promised For is decided before this issue goes out.",
)


def coverage_named(identity: str) -> CoverageDeclaration:
    return CoverageDeclaration(
        identity=identity,
        lines=(
            SourceCoverage(
                source_name="Weekly utility conflict matrix",
                requirement="required",
                state="read",
                detail="the 2026-03-01 revision was read in full",
            ),
        ),
    )


COVERAGE = coverage_named("weekly-coverage-2026-03-02")


def preparation_reading(project_id: int, revision_id: int) -> dict:
    return {
        "schema_version": "report-preparation-result-v1",
        "project_id": project_id,
        "configuration_version": "report-preparation-v1",
        "observed_at": PREPARED_AT.isoformat(),
        "health": "healthy",
        "window_start": "",
        "through_delta_id": 10**9,
        "through_disposition_id": 10**9,
        "accepted_revision_id": revision_id,
        "resolved_accepted": 0,
        "resolved_edited": 0,
        "resolved_rejected": 0,
        "proposed_new": 0,
        "open_actionable": 0,
        "open_deferred": 0,
        "superseded": 0,
    }


class Adopted:
    def __init__(self, project, revision_id, template_bytes):
        self.project = project
        self.revision_id = revision_id
        self.template_bytes = template_bytes


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
def store(tmp_path, monkeypatch):
    """The deployment's own store, rooted where this test can throw it away.

    The route resolves its store from settings rather than from a parameter, so
    the preparation below must retain its bytes where the route will look for
    them; `content_store` is what both sides call.
    """

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return content_store()


@pytest.fixture
def adopted(session, tmp_path, store):
    """One adopted project, a coordinator, a designated releaser, a reader."""

    row = Project(
        slug=f"issue-section-{uuid4().hex[:8]}",
        name="Issue Section",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    for principal, email, name, designations in (
        (COORDINATOR, "coordinator@example.test", "Coordinator", [COORDINATION]),
        (RELEASER, "releaser@example.test", "Releaser", [EXTERNAL_RELEASE]),
        (UNDESIGNATED, "reader@example.test", "Reader", []),
    ):
        enroll_member(
            session,
            project_id=row.id,
            email=email,
            principal=principal,
            display_name=name,
            designations=designations,
            operator=OPERATOR,
        )
    body = workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS)
    revision_id, _ = adopt(session, row, body, tmp_path)
    return Adopted(project=row, revision_id=revision_id, template_bytes=body)


@pytest.fixture
def client(session):
    """The app shares the test's transaction and the test's declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RELEASER
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


def as_principal(principal: HumanPrincipal) -> None:
    app.dependency_overrides[get_human_principal] = lambda: principal


def configure(session, adopted, *, artifacts=(WEEKLY, CHASE), policies=()):
    return configure_issue(
        session,
        adopted.project,
        principal=COORDINATOR,
        effective_from=JANUARY,
        ucm=UCM_RENDERER,
        artifacts=tuple(artifacts),
        policies=tuple(policies),
    )


def prepare(session, adopted, store, **overrides):
    """Bind, render and attach one candidate in this rollback-scoped session."""

    arguments = {
        "project_id": adopted.project.id,
        "preparation": preparation_reading(adopted.project.id, adopted.revision_id),
        "source_cutoff": CUTOFF,
        "prepared_at": PREPARED_AT,
        "coverage": COVERAGE,
        "templates": TEMPLATE,
        "first_issue_behavior": NO_PRIOR_COMPARISON_STATEMENT,
        "template_bytes": adopted.template_bytes,
    }
    arguments.update(overrides)
    bound = bind_preparation(session, **arguments)
    rendered = render_candidate_artifacts(
        bound, template_bytes=adopted.template_bytes, session=session, store=store
    )
    candidate = attach_candidate(
        session,
        bound,
        rendered,
        prepared_by=COORDINATOR,
        preparation=arguments["preparation"],
        templates=TEMPLATE,
        first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
        template_bytes=adopted.template_bytes,
    )
    session.flush()
    return candidate


def open_delta(session, adopted):
    """One unresolved proposed change to an accepted Promised For."""

    rendition = Rendition(session=session, project=adopted.project, name="later.xlsx")
    rendition.capture(
        fact_type="committed_date",
        value="2026-06-01",
        subject_key=subject(3),
        date_value=date(2026, 6, 1),
    )
    return append_deltas(
        session,
        adopted.project,
        rendition,
        source_revision="UCM workbook revision D",
        values=[
            modify(
                subject_key=subject(3),
                field_name="committed_date",
                accepted_value="2026-03-01",
                proposed_value="2026-06-01",
                baseline_revision=adopted.revision_id,
            )
        ],
    )


def week(client, adopted):
    page = client.get(f"/work/{adopted.project.slug}")
    assert page.status_code == 200, page.text
    return page.text


def prose(body: str) -> str:
    """The page as a reader receives it, with markup escaping undone.

    A sentence with an apostrophe in it is `&#39;` in the served bytes; a test
    that spelled the entity would be testing Jinja's escaping rather than the
    words the coordinator is shown.
    """

    return html.unescape(body)


def approve(client, adopted, candidate_id):
    return client.post(
        f"/work/{adopted.project.slug}/issue/authorize",
        data={"candidate_id": candidate_id},
    )


def _packages(session, adopted) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ReleasePackage)
            .where(ReleasePackage.project_id == adopted.project.id)
        )
    )


# --- what the section presents ---------------------------------------------


def test_before_anything_is_prepared_the_section_says_so_and_offers_nothing(
    session, adopted, client
):
    """A project with no candidate has nothing to approve, and no control."""

    configure(session, adopted)

    view = issue_view(session, project_id=adopted.project.id, as_of=NOW)
    assert view.state == NOTHING_PREPARED
    assert view.may_authorize is False
    assert view.candidate is None
    assert view.predecessor is None

    body = prose(week(client, adopted))
    assert "No issue has been prepared for this project yet" in body
    assert "/issue/authorize" not in body


def test_the_section_presents_the_candidate_it_would_send(session, adopted, client, store):
    """The configured set, the bound revision, the cutoff, and the coverage."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)

    view = issue_view(session, project_id=adopted.project.id, as_of=NOW)
    assert view.state == AUTHORIZABLE
    assert view.candidate.id == candidate.id
    assert [row.words for row in view.artifact_rows] == [
        "the customer's updated UCM workbook",
        "the chase list",
        "the weekly Coordination Report",
    ]
    assert view.declaration.coverage_identity == COVERAGE.identity
    assert view.declaration.coverage_sha256 == COVERAGE.content_sha256

    body = prose(week(client, adopted))
    assert "the customer's updated UCM workbook" in body
    assert "the weekly Coordination Report" in body
    assert COVERAGE.identity in body
    assert str(adopted.revision_id) in body
    assert CUTOFF.date().isoformat() in body
    assert "Approve this issue for sharing" in body
    # The internal technical names stay internal, on this section too.
    assert "Release Package" not in body
    assert "Review Packet" not in body


def test_before_the_first_issue_the_section_states_that_there_is_no_predecessor(
    session, adopted, client, store
):
    """ADR-0086: a project's first issue is valid with nothing before it.

    This project has an adopted baseline, an accepted revision, delivered
    documents and a prepared candidate — every timestamp a predecessor could
    have been invented from.
    """

    configure(session, adopted)
    prepare(session, adopted, store)

    view = issue_view(session, project_id=adopted.project.id, as_of=NOW)
    assert view.predecessor is None

    assert (
        "none — this would be this project's first issue"
        in prose(week(client, adopted))
    )


def test_the_second_candidate_names_the_first_issue_as_its_predecessor(
    session, adopted, client, store
):
    """Once a package exists, the next candidate is bound to it and says so."""

    configure(session, adopted)
    first = prepare(session, adopted, store)
    assert approve(client, adopted, first.id).status_code == 201
    session.expire_all()

    second = prepare(session, adopted, store, coverage=coverage_named("second-week"))
    view = issue_view(session, project_id=adopted.project.id, as_of=NOW)

    assert view.candidate.id == second.id
    assert view.predecessor is not None
    assert view.predecessor.issue_number == 1
    assert "issue 1, made from revision" in prose(week(client, adopted))


def test_an_approved_candidate_reads_as_approved_and_is_not_offered_again(
    session, adopted, client, store
):
    """Approving twice is not a thing the screen can be talked into."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    assert approve(client, adopted, candidate.id).status_code == 201
    session.expire_all()

    view = issue_view(session, project_id=adopted.project.id, as_of=NOW)
    assert view.state == ALREADY_AUTHORIZED
    assert view.may_authorize is False
    assert view.authorized is not None and view.authorized.issue_number == 1

    body = prose(week(client, adopted))
    assert "Approved and sent as this issue" in body
    assert "Approve this issue for sharing" not in body


# --- the act, and the authority that owns it -------------------------------


def test_a_designated_principal_approves_without_leaving_the_workflow(
    session, adopted, client, store
):
    """One POST from the week itself writes one immutable package receipt."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)

    response = approve(client, adopted, candidate.id)

    assert response.status_code == 201
    assert "Approved for sharing" in prose(response.text)
    # The whole week is re-derived around the outcome; this is the same page.
    assert "Source changes to review" in response.text
    session.expire_all()
    assert _packages(session, adopted) == 1
    package = session.scalars(
        select(ReleasePackage).where(ReleasePackage.candidate_id == candidate.id)
    ).one()
    assert package.authorized_by_principal == RELEASER.subject
    assert int(package.accepted_revision_id) == adopted.revision_id
    # The declared instant, not a clock: the route takes it from the review
    # clock the test overrode.
    assert package.authorized_at == NOW


def test_an_undesignated_principal_is_refused_by_postgresql_not_by_the_route(
    session, adopted, client, store
):
    """#533's designation lives inside the database, and the screen says so.

    The reader here holds an active membership — the project partition opens
    for them and they can read the whole week — and no release designation. The
    route runs no designation check of its own, so what refuses is the
    ``SECURITY DEFINER`` command, and the sentence rendered is its own.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    as_principal(UNDESIGNATED)

    # The control is not hidden from them: hiding it would state a rule the
    # database does not enforce, and a hidden control refuses nothing.
    assert "Approve this issue for sharing" in prose(week(client, adopted))

    response = approve(client, adopted, candidate.id)

    # 403 is this route's answer to `not_designated` alone; every other
    # refusal is a 409, so the status itself says which rule refused.
    assert response.status_code == 403
    assert "holds no external-release designation" in prose(response.text)
    assert "This issue was not approved, and nothing was sent" in response.text
    session.expire_all()
    assert _packages(session, adopted) == 0


def test_the_refused_principal_still_reads_the_whole_week(
    session, adopted, client, store
):
    """The designation refusal gives up its savepoint, not the transaction."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    as_principal(UNDESIGNATED)

    body = approve(client, adopted, candidate.id).text

    assert "Follow-up Plans waiting on an answer" in body
    assert "The issue to approve for sharing" in body
    assert session.scalar(select(func.count()).select_from(ReleaseCandidate)) >= 1


def test_a_non_member_is_answered_exactly_like_a_missing_project(
    session, adopted, client, store
):
    """The partition gate is the same one every slug-addressed surface passes."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    as_principal(HumanPrincipal("local:outsider"))

    assert client.get(f"/work/{adopted.project.slug}").status_code == 404
    assert approve(client, adopted, candidate.id).status_code == 404
    session.expire_all()
    assert _packages(session, adopted) == 0


def test_a_legacy_project_has_no_issue_section_and_no_act(session, client):
    """The approval belongs to the adopted project's week and nowhere else.

    ADR-0085 amends only the adopted-project presentation; a legacy project
    keeps ADR-0035's item-per-record Work List and gains no release surface.
    """

    legacy = Project(
        slug=f"legacy-{uuid4().hex[:8]}", name="Legacy", is_synthetic=True
    )
    session.add(legacy)
    session.flush()
    enroll_member(
        session,
        project_id=legacy.id,
        email="releaser@example.test",
        principal=RELEASER,
        display_name="Releaser",
        designations=[EXTERNAL_RELEASE],
        operator=OPERATOR,
    )

    page = client.get(f"/work/{legacy.slug}")
    assert page.status_code == 200
    assert "The issue to approve for sharing" not in page.text

    refused = client.post(
        f"/work/{legacy.slug}/issue/authorize", data={"candidate_id": 1}
    )
    assert refused.status_code == 404


# --- what is never offered -------------------------------------------------


def test_a_blocked_candidate_is_not_offered_and_cannot_be_approved(
    session, adopted, client, store
):
    """#529's `authorization_blockers` is the authority, not a second reading.

    The candidate here is complete — every configured artifact was rendered and
    retained — and blocked, because an undecided change moves a field this
    project's own policy waits on. The section does not offer it, and the route
    refuses it even when the form is posted anyway.
    """

    configure(session, adopted, policies=(RESOLVE_COMMITTED_DATE,))
    open_delta(session, adopted)
    candidate = prepare(session, adopted, store)
    assert candidate.readiness == "blocked"

    view = issue_view(session, project_id=adopted.project.id, as_of=NOW)
    assert view.state == NOT_AUTHORIZABLE
    assert view.may_authorize is False
    assert view.blocked is True
    assert view.blockers and "blocked" in view.blockers[0]

    body = prose(week(client, adopted))
    assert "Why this cannot be approved as it stands" in body
    assert "Approve this issue for sharing" not in body
    # It is still visible: what was prepared and refused is not hidden.
    assert view.declaration.coverage_identity in body
    assert "freshly prepared candidate" in body

    response = approve(client, adopted, candidate.id)
    assert response.status_code == 409
    assert "newly prepared candidate" in response.text
    session.expire_all()
    assert _packages(session, adopted) == 0


def test_the_rendered_screen_offers_no_approval_for_a_blocked_candidate(
    session, adopted, client, store
):
    """The screen-level half of the rule, on its own.

    Kept separate from the reading's own assertions so that a change which
    leaves ``issue_view`` correct and lets the template offer the control
    anyway still turns something red. A control whose only outcome is a
    refusal is not an offer.
    """

    configure(session, adopted, policies=(RESOLVE_COMMITTED_DATE,))
    open_delta(session, adopted)
    prepare(session, adopted, store)

    body = week(client, adopted)

    assert "/issue/authorize" not in body
    assert "<form" not in body
    assert "Approve this issue for sharing" not in prose(body)


def test_a_stale_candidate_asks_for_a_fresh_preparation_and_stays_visible(
    session, adopted, client, store
):
    """A candidate the project moved past is history, not an approval.

    Approving the first candidate moves the release chain head, so the second
    prepared candidate — bound to no predecessor — no longer describes the
    project. The section says which fact moved, keeps the candidate on the
    page, and asks for a fresh preparation instead of offering the approval.
    """

    configure(session, adopted)
    first = prepare(session, adopted, store)
    stale = prepare(session, adopted, store, coverage=coverage_named("prepared-early"))
    assert approve(client, adopted, first.id).status_code == 201
    session.expire_all()

    # The stale candidate is the current one: it was prepared last, and an
    # older authorizable candidate never quietly stands in for it.
    assert current_release_candidate(session, adopted.project.id).id == stale.id
    view = issue_view(session, project_id=adopted.project.id, as_of=NOW)
    assert view.state == NOT_AUTHORIZABLE
    assert view.may_authorize is False
    assert view.blocked is False
    assert view.stale_reasons and view.stale_reasons == view.blockers
    assert "a package was authorized after this candidate was prepared" in (
        " ".join(view.stale_reasons)
    )

    body = prose(week(client, adopted))
    assert "Approve this issue for sharing" not in body
    assert "freshly prepared candidate" in body
    assert "prepared-early" in body, "the old candidate stays visible as history"

    response = approve(client, adopted, stale.id)
    assert response.status_code == 409
    session.expire_all()
    assert _packages(session, adopted) == 1


def test_the_section_refuses_to_offer_an_approval_the_blockers_forbid(
    session, adopted, store
):
    """The guard itself, exercised directly rather than through a template.

    `_refuse_offered_with_blockers` is what stops a later change deciding on
    its own that a blocked candidate "looks fine"; a screen that offered the
    act would put a coordinator in front of a control whose only outcome is a
    refusal.
    """

    from corridor.web import issue_section

    configure(session, adopted, policies=(RESOLVE_COMMITTED_DATE,))
    open_delta(session, adopted)
    prepare(session, adopted, store)

    with pytest.raises(IssueViewRefused) as refused:
        issue_section._refuse_offered_with_blockers(
            AUTHORIZABLE, ("the accepted record moved",)
        )
    assert "cannot be authorized" in str(refused.value)


def test_the_section_refuses_a_predecessor_the_candidate_was_not_bound_to(
    session, adopted, store
):
    """No timestamp, baseline or newest package may stand in for the chain."""

    from corridor.web import issue_section

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    assert candidate.previous_package_id is None

    invented = ReleasePackage(id=99, project_id=adopted.project.id)
    with pytest.raises(IssueViewRefused) as refused:
        issue_section._refuse_invented_predecessor(candidate, invented)
    assert "bound into the candidate" in str(refused.value)


# --- what the section still does not store or grow -------------------------


def test_the_week_grows_no_record_decision_control_beside_the_approval(
    session, adopted, client, store
):
    """ADR-0085's exactly-once rule survives the page gaining one act.

    The approval is not a decision about the project record, and it is the only
    form on the page: no outcome token, no child selection, and no proposed
    change is offered here.
    """

    configure(session, adopted)
    open_delta(session, adopted)
    prepare(session, adopted, store)

    body = week(client, adopted)

    assert body.count("<form") == 1
    assert f'action="/work/{adopted.project.slug}/issue/authorize"' in body
    assert body.count("<button") == 1
    assert re.findall(r'name="([a-z_]+)"', body) == ["candidate_id"]
    for token in ("apply", "keep_current", "needs_coordination", "defer"):
        assert f'value="{token}"' not in body


def test_reading_the_section_stores_no_release_state(
    session, adopted, client, store
):
    """Looking at a prepared issue is not preparing, approving, or recording."""

    configure(session, adopted)
    prepare(session, adopted, store)
    before = (
        session.scalar(select(func.count()).select_from(ReleaseCandidate)),
        _packages(session, adopted),
    )

    for _ in range(3):
        week(client, adopted)

    session.expire_all()
    assert (
        session.scalar(select(func.count()).select_from(ReleaseCandidate)),
        _packages(session, adopted),
    ) == before


def test_the_reading_is_derived_and_repeats_itself(session, adopted, store):
    """Two readings of one state land on the same section and the same act."""

    configure(session, adopted)
    prepare(session, adopted, store)

    first = issue_view(session, project_id=adopted.project.id, as_of=NOW)
    second = issue_view(session, project_id=adopted.project.id, as_of=NOW)

    assert (first.state, first.blockers, first.artifact_rows) == (
        second.state,
        second.blockers,
        second.artifact_rows,
    )


# --- how it reads --------------------------------------------------------


def test_the_approval_control_names_the_rule_the_database_enforces(
    session, adopted, client, store
):
    """A control whose caption implies a different rule is a lie in markup."""

    configure(session, adopted)
    prepare(session, adopted, store)

    body = week(client, adopted)

    assert "external-release designation" in body
    assert 'aria-describedby="approve-issue-rule"' in body
    assert 'id="approve-issue-rule"' in body


def test_a_refusal_takes_focus_and_is_announced(session, adopted, client, store):
    """`docs/accessibility-acceptance-checklist.md`: one focus per response."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    as_principal(UNDESIGNATED)

    body = approve(client, adopted, candidate.id).text

    assert body.count("autofocus") == 1
    assert 'id="work-refusal" tabindex="-1" autofocus' in body
    assert 'role="alert"' in body


def test_an_approval_takes_focus_and_is_announced_politely(
    session, adopted, client, store
):
    """A completed act is a status, not an alert, and is where focus lands."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)

    body = approve(client, adopted, candidate.id).text

    assert body.count("autofocus") == 1
    assert 'id="work-outcome" tabindex="-1" autofocus' in body
    assert 'aria-live="polite"' in body


def test_the_page_still_reads_as_one_document_with_descending_headings(
    session, adopted, client, store
):
    """`docs/accessibility-acceptance-checklist.md` §2, with the Issue section."""

    configure(session, adopted)
    prepare(session, adopted, store)

    body = week(client, adopted)

    assert body.count("<main>") == 1
    assert body.count("<h1>") == 1
    levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", body)]
    assert levels[0] == 1
    for previous, level in zip(levels, levels[1:]):
        assert level <= previous + 1, "a heading level is skipped"
    assert body.count('<table class="record"') == body.count("<caption>")
