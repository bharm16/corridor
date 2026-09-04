"""Review -> Follow-up -> Issue as one executable path, end to end (#536).

Every other test of this workflow proves one seam at a time: `issue_view`
refuses to offer an approval #529 blocked, `request_preparation` refuses a
revision that moved, `run_preparation_request` records which of the three
outcomes happened. None of them walks the path a coordinator actually walks,
and #536's final amendment says that walking it is the thing that closes the
ticket.

So these three tests are deliberately the slow, unglamorous kind. They run
against a real migrated database with real commits, they drive the screens
through the routes, and **they take every value they submit out of the page
they were just shown**. Nothing here composes a coverage digest, a profile
version, an accepted revision or a candidate id of its own: `_form` reads the
hidden inputs out of the rendered HTML, which is the only way a test can prove
that what the screen offers is submittable, rather than that a hand-written
payload happens to satisfy the route.

The one seam a test stands in for is the worker's *input resolver*: #529 takes
the customer's template bytes, the approved template and mapping binding, the
first-issue behaviour and the report-preparation reading from its caller, and
`release_preparation_worker` deliberately does not invent a second authority
over where those come from. `run_preparation_request` — the seam a background
worker would call — is real here, and only its `PreparationInputs` are handed
in.

Nothing here reads a clock. The cutoff, every request instant and every
attempt's start and finish are declared, and the routes take theirs from
`get_review_clock`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor.access import COORDINATION, EXTERNAL_RELEASE, enroll_member
from corridor.config import settings
from corridor.issue_coverage import derive_coverage_reading
from corridor.issue_profile import effective_issue_inventory
from corridor.issue_rendering import NO_PRIOR_COMPARISON_STATEMENT
from corridor.models import (
    IssueCoverageDeclaration,
    Project,
    ReleaseCandidate,
    ReleaseCandidateArtifact,
    ReleasePackage,
    ReleasePreparationRequest,
)
from corridor.object_storage import content_store
from corridor.principals import HumanPrincipal
from corridor.project_workflow import PREPARATION_FAILED, issue_readiness
from corridor.release_candidate import RENDERER_FAILED, current_release_candidate
from corridor.release_preparation import (
    FAILED,
    PREPARED,
    pending_request_ids,
    preparation_standing,
)
from corridor.release_preparation_worker import (
    PreparationInputs,
    run_preparation_request,
)
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)
from corridor.web.issue_section import PREPARE_ACTION, issue_view

from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes
from packet_review_support import configure_issue
from test_release_candidate import (
    BINDING,
    CHASE,
    TEMPLATE,
    UCM_RENDERER,
    WEEKLY,
    _preparation,
)


COORDINATOR = HumanPrincipal("local:coordinator")
RELEASER = HumanPrincipal("local:releaser")
OPERATOR = HumanPrincipal("local:operator")

JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
FEBRUARY = datetime(2026, 2, 2, tzinfo=timezone.utc)
# The instant every request in this file is read and decided at, and the cutoff
# the week is read at. One declared moment, never a clock.
NOW = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
STARTED_AT = datetime(2026, 3, 2, 8, 1, tzinfo=timezone.utc)
FINISHED_AT = datetime(2026, 3, 2, 8, 2, tzinfo=timezone.utc)
RETRY_STARTED_AT = datetime(2026, 3, 2, 9, 1, tzinfo=timezone.utc)
RETRY_FINISHED_AT = datetime(2026, 3, 2, 9, 2, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Adopted:
    project_id: int
    slug: str
    revision_id: int
    template_bytes: bytes


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The deployment's own store, rooted where this test can throw it away."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return content_store()


@pytest.fixture
def factory(runtime_database):
    """A migrated database of this test's own, and real committed transactions.

    The path being proved spans an HTTP act, a background worker that renders
    outside any transaction of its own, and a second HTTP act that reads what
    the worker committed. A rollback-scoped connection would let all three see
    each other's uncommitted work, which is exactly the thing that could hide a
    hole in the path.
    """

    return runtime_database.session_factory


@pytest.fixture
def adopted(factory, tmp_path, store):
    """One adopted project, a coordinator, and a designated releaser."""

    with factory() as setup:
        project = Project(
            slug=f"issue-path-{uuid4().hex[:8]}",
            name="Issue Path",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush()
        for principal, email, name, designations in (
            (COORDINATOR, "coordinator@example.test", "Coordinator", [COORDINATION]),
            (RELEASER, "releaser@example.test", "Releaser", [EXTERNAL_RELEASE]),
        ):
            enroll_member(
                setup,
                project_id=project.id,
                email=email,
                principal=principal,
                display_name=name,
                designations=designations,
                operator=OPERATOR,
            )
        body = workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS)
        revision_id, _ = adopt(setup, project, body, tmp_path)
        configure_issue(
            setup,
            project,
            principal=COORDINATOR,
            effective_from=JANUARY,
            ucm=UCM_RENDERER,
            artifacts=(WEEKLY, CHASE),
        )
        made = Adopted(
            project_id=int(project.id),
            slug=project.slug,
            revision_id=int(revision_id),
            template_bytes=body,
        )
        setup.commit()
    return made


@pytest.fixture
def client(factory):
    """The app on this test's database, at this test's declared instant."""

    def sessions():
        with factory() as one:
            yield one

    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


def as_principal(principal: HumanPrincipal) -> None:
    app.dependency_overrides[get_human_principal] = lambda: principal


def prose(body: str) -> str:
    """The page as a reader receives it, with markup escaping undone."""

    return html.unescape(body)


_FORM = re.compile(
    r'<form[^>]*action="(?P<action>[^"]*)"[^>]*>(?P<body>.*?)</form>', re.S
)
_HIDDEN = re.compile(
    r'<input[^>]*type="hidden"[^>]*name="(?P<name>[^"]*)"[^>]*'
    r'value="(?P<value>[^"]*)"'
)


def _form(body: str, action_suffix: str) -> dict[str, str] | None:
    """The hidden inputs of the one form on the page with this action.

    Reading the payload out of the rendered page is the point: a test that
    composed its own would prove that the route accepts a payload, not that the
    screen offers one a coordinator could submit.
    """

    for match in _FORM.finditer(body):
        if match.group("action").endswith(action_suffix):
            return {
                found.group("name"): html.unescape(found.group("value"))
                for found in _HIDDEN.finditer(match.group("body"))
            }
    return None


def week(client, adopted: Adopted) -> str:
    page = client.get(f"/work/{adopted.slug}")
    assert page.status_code == 200, page.text
    return page.text


def run_worker(
    factory,
    adopted: Adopted,
    *,
    template_bytes: bytes | None = None,
    started_at: datetime = STARTED_AT,
    finished_at: datetime = FINISHED_AT,
):
    """Run the one pending request the way a background worker would.

    The request is found by ``pending_request_ids`` — the worker's own reader —
    rather than by an identifier the test kept from the POST, so a route that
    recorded a request nobody could find would fail here.
    """

    with factory() as reading:
        pending = pending_request_ids(reading, project_id=adopted.project_id)
    assert len(pending) == 1, f"expected one pending request, found {pending}"
    return run_preparation_request(
        factory,
        request_id=pending[0],
        inputs=PreparationInputs(
            preparation=_preparation(adopted.project_id, adopted.revision_id),
            templates=TEMPLATE,
            first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
            template_bytes=(
                adopted.template_bytes if template_bytes is None else template_bytes
            ),
            binding=BINDING,
        ),
        started_at=started_at,
        finished_at=finished_at,
    )


def _count(factory, model, adopted: Adopted) -> int:
    with factory() as reading:
        return int(
            reading.scalar(
                select(func.count())
                .select_from(model)
                .where(model.project_id == adopted.project_id)
            )
        )


# --- proof one: a project with no candidate reaches an authorized issue -----


def test_no_candidate_becomes_an_authorized_issue_without_leaving_the_week(
    factory, adopted, client, store
):
    """The whole of #536's first required proof, in the order it is written.

    No candidate, the derived coverage reading, one confirmation, one worker
    run, the candidate on the same screen, the designated releaser's approval,
    and the authorized package — all through `/work/{slug}` and the two acts it
    carries.
    """

    # 1. The project has no candidate, and the section says so rather than
    #    offering an approval.
    body = week(client, adopted)
    assert "No issue has been prepared for this project yet" in prose(body)
    assert _form(body, "/issue/authorize") is None

    # 2. The Issue section shows the derived coverage reading, and the digest
    #    it offers to confirm is the one `issue_coverage` derives.
    assert "Sources for this issue" in prose(body)
    assert PREPARE_ACTION in prose(body)
    confirmation = _form(body, "/issue/prepare")
    assert confirmation is not None
    with factory() as reading:
        derived = derive_coverage_reading(
            reading,
            project_id=adopted.project_id,
            cutoff=NOW,
            inventory=effective_issue_inventory(reading, adopted.project_id, NOW),
        )
    assert confirmation["derived_reading_digest"] == derived.reading_digest
    assert confirmation["accepted_revision_id"] == str(adopted.revision_id)

    # 3. The coordinator confirms it and asks for the issue to be prepared.
    requested = client.post(
        f"/work/{adopted.slug}/issue/prepare", data=confirmation
    )
    assert requested.status_code == 202, requested.text
    # While a worker holds the request the section says so and offers nothing:
    # no second preparation, and no approval of an issue not yet made.
    assert "Preparing this issue" in prose(requested.text)
    assert _form(requested.text, "/issue/prepare") is None
    assert _form(requested.text, "/issue/authorize") is None
    with factory() as reading:
        assert (
            preparation_standing(reading, project_id=adopted.project_id).in_flight
        )

    # 4. A worker prepares the candidate.
    attempt = run_worker(factory, adopted)
    assert attempt.outcome == "prepared", attempt.reason
    assert attempt.candidate_id is not None

    # 5. The candidate appears in the same workflow, and the approval it offers
    #    names the candidate the worker just made.
    body = week(client, adopted)
    readable = prose(body)
    assert "Ready for your approval" in readable
    assert "the customer's updated UCM workbook" in readable
    assert "the weekly Coordination Report" in readable
    approval = _form(body, "/issue/authorize")
    assert approval is not None
    assert approval["candidate_id"] == str(attempt.candidate_id)
    # The candidate names the coverage the coordinator confirmed on the screen,
    # not one the worker derived for itself.
    with factory() as reading:
        candidate = reading.get_one(ReleaseCandidate, int(attempt.candidate_id))
        declaration = reading.get_one(
            IssueCoverageDeclaration, int(candidate.coverage_declaration_id)
        )
        assert declaration.derived_reading_digest == derived.reading_digest
        assert candidate.prepared_by_principal == COORDINATOR.subject

    # 6. The designated releaser approves it, on the surface they were on.
    as_principal(RELEASER)
    authorized = client.post(
        f"/work/{adopted.slug}/issue/authorize", data=approval
    )
    assert authorized.status_code == 201, authorized.text

    # 7. The Issue section shows the authorized package, and offers nothing
    #    that would send it twice.
    readable = prose(week(client, adopted))
    assert "Approved and sent as this issue" in readable
    assert "is issue 1 for this project" in readable
    assert RELEASER.subject in readable
    assert _form(week(client, adopted), "/issue/authorize") is None
    with factory() as reading:
        package = reading.scalars(
            select(ReleasePackage).where(
                ReleasePackage.project_id == adopted.project_id
            )
        ).one()
        assert int(package.candidate_id) == int(attempt.candidate_id)
        assert package.authorized_by_principal == RELEASER.subject
        assert package.authorized_at == NOW


# --- proof two: a stale candidate is replaced, never approved ---------------


def test_a_stale_candidate_stays_visible_and_is_replaced_rather_than_approved(
    factory, adopted, client, store
):
    """#536's second required proof, in the order it is written.

    Staleness here is the ordinary weekly kind the amendment names first: what
    the project is configured to externally issue changed after the candidate
    was prepared, so the candidate no longer describes the project it was made
    for.
    """

    # An authorizable candidate, made the way proof one makes one.
    first = _form(week(client, adopted), "/issue/prepare")
    assert client.post(
        f"/work/{adopted.slug}/issue/prepare", data=first
    ).status_code == 202
    stale = run_worker(factory, adopted)
    assert stale.outcome == "prepared", stale.reason
    assert _form(week(client, adopted), "/issue/authorize") is not None

    # The candidate becomes stale: the customer's configured issue changes.
    with factory() as change:
        project = change.get_one(Project, adopted.project_id)
        configure_issue(
            change,
            project,
            principal=COORDINATOR,
            effective_from=FEBRUARY,
            ucm=UCM_RENDERER,
            artifacts=(WEEKLY,),
        )
        change.commit()

    body = week(client, adopted)
    readable = prose(body)
    with factory() as reading:
        view = issue_view(reading, project_id=adopted.project_id, as_of=NOW)
        candidate = reading.get_one(ReleaseCandidate, int(stale.candidate_id))
        old_coverage_identity = candidate.coverage_identity
    assert view.stale_reasons
    assert "configured to externally issue changed" in " ".join(view.stale_reasons)

    # The old candidate stays visible as history rather than vanishing: what
    # was prepared for this customer, and what it was prepared under, are both
    # still on the page.
    assert old_coverage_identity in readable
    assert "Cannot be approved as it stands" in readable
    assert "freshly prepared candidate" in readable
    # And it is not offered for approval.
    assert _form(body, "/issue/authorize") is None

    # Coverage is reconfirmed under #675's rules — the reading now carries the
    # profile version in force, and the form offers that reading and no other.
    fresh = _form(body, "/issue/prepare")
    assert fresh is not None
    assert fresh["issue_profile_version"] != first["issue_profile_version"]
    with factory() as reading:
        derived = derive_coverage_reading(
            reading,
            project_id=adopted.project_id,
            cutoff=NOW,
            inventory=effective_issue_inventory(reading, adopted.project_id, NOW),
        )
    assert fresh["derived_reading_digest"] == derived.reading_digest

    # A fresh candidate is prepared from it.
    assert client.post(
        f"/work/{adopted.slug}/issue/prepare", data=fresh
    ).status_code == 202
    replacement = run_worker(
        factory,
        adopted,
        started_at=RETRY_STARTED_AT,
        finished_at=RETRY_FINISHED_AT,
    )
    assert replacement.outcome == "prepared", replacement.reason
    assert int(replacement.candidate_id) != int(stale.candidate_id)
    with factory() as reading:
        assert int(
            current_release_candidate(reading, adopted.project_id).id
        ) == int(replacement.candidate_id)

    # The stale candidate stays visible as history once it has been replaced,
    # which is when it would otherwise vanish: the section names the current
    # candidate, and the one the coordinator was just reading about would have
    # left the screen with nothing to say where it went.
    readable = prose(week(client, adopted))
    assert old_coverage_identity in readable
    assert "Earlier candidates for this issue" in readable
    assert "never approved; a later candidate replaced it" in readable

    # The stale candidate cannot be authorized, by its identity and not by the
    # screen having stopped mentioning it: the act is submitted directly.
    as_principal(RELEASER)
    refused = client.post(
        f"/work/{adopted.slug}/issue/authorize",
        data={"candidate_id": int(stale.candidate_id)},
    )
    assert refused.status_code == 409, refused.text
    assert _count(factory, ReleasePackage, adopted) == 0

    # The replacement, prepared under the configuration now in force, is what
    # the section offers instead.
    approval = _form(week(client, adopted), "/issue/authorize")
    assert approval is not None
    assert approval["candidate_id"] == str(replacement.candidate_id)
    assert client.post(
        f"/work/{adopted.slug}/issue/authorize", data=approval
    ).status_code == 201


# --- proof three: a failed preparation, and a retry that means something ----


def test_a_failed_preparation_leaves_nothing_partial_and_a_retry_puts_it_right(
    factory, adopted, client, store
):
    """#536's third required proof, in the order it is written.

    The failure is a real one from #529's own vocabulary: the bytes handed to
    the UCM renderer are not the workbook the customer registered, so the
    mandatory member of the package cannot be produced.
    """

    confirmation = _form(week(client, adopted), "/issue/prepare")
    assert client.post(
        f"/work/{adopted.slug}/issue/prepare", data=confirmation
    ).status_code == 202

    attempt = run_worker(
        factory, adopted, template_bytes=b"not the registered workbook"
    )
    assert attempt.outcome == "refused"
    assert attempt.refusal_code == RENDERER_FAILED

    # No partial candidate: not a candidate row, not an artifact row.
    assert _count(factory, ReleaseCandidate, adopted) == 0
    assert _count(factory, ReleaseCandidateArtifact, adopted) == 0

    # A bounded issue-readiness problem is shown — in the Issue section, under
    # what must be put right first, and never as a record decision.
    with factory() as reading:
        problems = issue_readiness(
            reading, project_id=adopted.project_id, as_of=NOW
        )
        assert preparation_standing(
            reading, project_id=adopted.project_id
        ).state == FAILED
    assert PREPARATION_FAILED in {problem.code for problem in problems}
    body = week(client, adopted)
    readable = prose(body)
    assert "The last attempt to prepare this issue produced nothing" in readable
    assert "Nothing partial was written" in readable
    assert "What must be put right first" in readable

    # The retry the sentence promises is offered, and it is idempotent: two
    # submissions of the same confirmed reading queue one preparation.
    retry = _form(body, "/issue/prepare")
    assert retry is not None
    assert client.post(
        f"/work/{adopted.slug}/issue/prepare", data=retry
    ).status_code == 202
    assert client.post(
        f"/work/{adopted.slug}/issue/prepare", data=retry
    ).status_code == 202
    # One declaration for one reading, and one request for one retry: the
    # failed attempt is what makes this a second request rather than the same
    # one converged upon, and a second click is not a third.
    assert _count(factory, IssueCoverageDeclaration, adopted) == 1
    assert _count(factory, ReleasePreparationRequest, adopted) == 2

    # And the retry actually runs: the same request the section is waiting on,
    # prepared this time, is a candidate the coordinator can act on.
    retried = run_worker(
        factory,
        adopted,
        started_at=RETRY_STARTED_AT,
        finished_at=RETRY_FINISHED_AT,
    )
    assert retried.outcome == "prepared", retried.reason
    with factory() as reading:
        assert preparation_standing(
            reading, project_id=adopted.project_id
        ).state == PREPARED
    approval = _form(week(client, adopted), "/issue/authorize")
    assert approval is not None
    assert approval["candidate_id"] == str(retried.candidate_id)
