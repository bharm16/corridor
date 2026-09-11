"""The project-level processing banner, against real committed claims (#900).

A claim, a lease and an expiry are facts about *committed* rows seen from
another transaction, so every case here runs on the harness-owned
``runtime_database`` rather than the rollback-scoped session: a lease that only
ever existed inside one open transaction would prove nothing about what a web
request reads. The clock is an argument to the reader, so expiry is stated
rather than waited for.

The cases are the ones #900 asks to be proved, and each of them is a way the
banner could lie:

- another project's claim, which must not become this project's banner;
- an unrelated scheduled handler, whose claim is not a source-processing pass;
- a claim whose lease is in force, which is the one thing the record supports;
- an expired lease, which says the claim lapsed and never that the worker
  stopped;
- a completed pass, which holds no claim;
- a failed pass, the one unclaimed state that still gets a sentence;
- a later pass, which takes the banner off an earlier failure from its own
  record -- queued, claimed or finished -- and is the whole of what lets that
  sentence say *the latest*;
- a lapsed lease, which is not a failure however long ago it lapsed; and
- recovery, where the next worker takes the lapsed occurrence and the banner
  follows the new claim rather than the old one.

Four more follow them. The grant: the page reads the projection and holds
nothing on the scheduler underneath it. Then the page twice, because a reading
the register never prints is a reading nobody has -- the claim and its expiry
through the real route and the real template, and the failure beside the rows
it does not speak for. And last the surface count, which is one: the approved
failure wording ends by pointing a reader at Sources, and Sources is the only
page this banner is on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import unescape
from uuid import uuid4

import pytest
from sqlalchemy import text

from corridor import access
from corridor.due_work import (
    HANDLER_PROJECT_PROCESSING,
    ProcessingHealthDeclaration,
    ProjectProcessingDeclaration,
    claim_due_work,
    complete_due_work,
    configure_due_work,
    enqueue_due_work,
    fail_due_work,
)
from corridor.models import Project, ProjectRosterEntry
from corridor.project_processing_banner import (
    CLAIMED,
    CLAIM_EXPIRED,
    DETAILS,
    FAILED,
    SENTENCES,
    UNCLAIMED,
    read_processing_pass,
)


PRINCIPAL = "local:banner-reader"
EXTRACTOR = "deployed-matrix-v1"


def _member_project(session, label: str) -> int:
    """One project this principal is actively on, so it can declare a partition."""

    project = Project(
        slug=f"{label}-{uuid4().hex}", name=label, is_synthetic=True
    )
    session.add(project)
    session.flush([project])
    session.add(
        ProjectRosterEntry(
            project_id=project.id,
            principal_subject=PRINCIPAL,
            display_name=PRINCIPAL,
            active=True,
            can_coordinate=True,
        )
    )
    session.flush()
    return project.id


def _processing_schedule(session, project_id: int, *, starts_at: datetime) -> None:
    configure_due_work(
        session,
        ProjectProcessingDeclaration.released_hourly(
            project_id=project_id,
            configuration_version="project-processing-v1",
            extractor_identity=EXTRACTOR,
            starts_at=starts_at,
        ),
        now=starts_at,
    )
    session.flush()


def _project(session, label: str, *, starts_at: datetime) -> int:
    """One member project with a project-processing schedule, committed."""

    project_id = _member_project(session, label)
    _processing_schedule(session, project_id, starts_at=starts_at)
    return project_id


def _banner(factory, project_id: int, now: datetime):
    """Read the banner the way a project surface does: inside its partition.

    ``_authorize`` declares the partition before any route reads anything, and
    the projection refuses to answer without one, so a reading that skipped
    this step would be testing a query no request ever issues.
    """

    with factory() as reading:
        access.open_project_partition(
            reading, principal_subject=PRINCIPAL, project_id=project_id
        )
        return read_processing_pass(reading, project_id=project_id, now=now)


def _handler_of(session, occurrence_id: int) -> str:
    return session.execute(
        text(
            "select s.handler_key from public.due_work_schedules s "
            "  join public.due_work_occurrences o "
            "    on o.scheduled_job_id = s.id "
            " where o.id = :occurrence"
        ),
        {"occurrence": occurrence_id},
    ).scalar_one()


def _claim(factory, now: datetime):
    with factory() as claiming:
        claim = claim_due_work(claiming, now=now, owner="runtime:banner-worker")
        assert claim is not None
        claiming.commit()
        return claim


def _completed_result(project_id: int, observed_at: datetime) -> dict:
    # Mirrors `project_processing`'s own receipt exactly, because
    # `due_work._validate_handler_result` checks the key set rather than a
    # subset: #919's containment added `held_unread` and moved the version to
    # v3, and a receipt built here that has drifted from that one is refused
    # by the runtime rather than quietly accepted.
    return {
        "schema_version": "project-processing-result-v3",
        "project_id": project_id,
        "configuration_version": "project-processing-v1",
        "observed_at": observed_at.isoformat(),
        "health": "healthy",
        "eligible_document_count": 0,
        "parsed": 0,
        "extracted": 0,
        "skipped": 0,
        "failed": 0,
        "unreadable": 0,
        "quarantined": 0,
        "held_out": 0,
        "held_unread": 0,
        "processing_failures": [],
        "reconciled": False,
        "admitted": 0,
        "waiting": 0,
        "ambiguous_documents": 0,
    }


@pytest.fixture
def factory(runtime_database):
    return runtime_database.session_factory


def test_another_projects_claim_is_not_this_projects_banner(factory):
    """A claim belongs to the project whose schedule owns it, and to no other.

    The unclaimed project has an occurrence of its own, so a leak would show up
    as the wrong *sentence* rather than as the absence of one -- a banner that
    said nothing here would pass a weaker version of this test while still
    reading the other project's row.
    """

    earlier = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
    with factory() as setup:
        busy = _project(setup, "banner-busy", starts_at=earlier)
        quiet = _project(setup, "banner-quiet", starts_at=later)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=later)
        ticking.commit()

    # Both schedules coalesce onto the same hourly slot, so the claim takes the
    # occurrence with the lower id -- the busy project's, configured first.
    claim = _claim(factory, later)
    with factory() as checking:
        assert _handler_of(checking, claim.occurrence_id) == HANDLER_PROJECT_PROCESSING

    assert _banner(factory, busy, later).status == CLAIMED
    assert _banner(factory, quiet, later).status == UNCLAIMED

    with factory() as reading:
        access.open_project_partition(
            reading, principal_subject=PRINCIPAL, project_id=quiet
        )
        visible = reading.execute(
            text(
                "select project_id from public.current_project_processing_pass"
            )
        ).scalars().all()
    assert visible == [quiet]


def test_an_unrelated_scheduled_handler_is_not_a_source_processing_claim(factory):
    """The banner is about one handler, not about the runtime being busy.

    Every recurring pass this project runs lives in the same two relations, so
    a projection keyed on `state = 'claimed'` alone would report a health
    reading, a notification sweep or a report preparation as "a worker has
    claimed this project's document-processing pass".
    """

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    now = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id = _member_project(setup, "banner-health")
        # The health schedule is configured first, so its occurrence carries the
        # lower id and the single claim below takes it rather than the
        # processing pass, which stays pending beside it.
        configure_due_work(
            setup,
            ProcessingHealthDeclaration.released_hourly(
                project_id=project_id,
                configuration_version="processing-health-v1",
                starts_at=starts_at,
            ),
            now=starts_at,
        )
        _processing_schedule(setup, project_id, starts_at=starts_at)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    claim = _claim(factory, now)
    with factory() as checking:
        assert _handler_of(checking, claim.occurrence_id) != HANDLER_PROJECT_PROCESSING

    banner = _banner(factory, project_id, now)
    assert banner.status == UNCLAIMED
    assert banner.claimed_at is None


def test_a_lease_in_force_says_a_worker_has_claimed_the_pass(factory):
    """The one sentence the record supports, and the one #900 supplied."""

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id = _project(setup, "banner-claimed", starts_at=starts_at)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=starts_at)
        ticking.commit()
    claim = _claim(factory, starts_at)

    banner = _banner(factory, project_id, starts_at + timedelta(minutes=5))

    assert banner.status == CLAIMED
    assert banner.claim_held
    assert banner.sentence == (
        "A worker has claimed this project's document-processing pass."
    )
    assert banner.lease_expires_at == claim.lease_expires_at
    # It says a pass is claimed; it names no document, because no record says
    # which document is being read.
    assert "document-processing pass" in banner.sentence
    assert not any(
        word in banner.sentence.lower() for word in ("file", "page", "reading ")
    )


def test_an_expired_lease_says_the_claim_lapsed_not_that_the_worker_stopped(
    factory,
):
    """#918's fact, said out loud on a customer page.

    The candidate predicate in `claim_due_work` admits `state = 'claimed' and
    lease_expires_at <= now`, an expired lease stops counting toward the
    concurrency limit, and nothing fences the worker that holds the old claim:
    it never re-checks its claim token. So at and after the lease instant the
    runtime treats the occurrence as recoverable -- and the banner says the
    claim expired and recovery is pending, while refusing to say the worker is
    finished. The boundary is asserted at the exact instant so the two
    predicates cannot drift apart by a tick.
    """

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id = _project(setup, "banner-expired", starts_at=starts_at)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=starts_at)
        ticking.commit()
    claim = _claim(factory, starts_at)

    held = _banner(factory, project_id, claim.lease_expires_at - timedelta(seconds=1))
    lapsed = _banner(factory, project_id, claim.lease_expires_at)

    assert held.status == CLAIMED
    assert lapsed.status == CLAIM_EXPIRED
    assert not lapsed.claim_held
    assert lapsed.sentence == "The processing claim expired. Recovery is pending."
    # The uncertainty is still said, and is said underneath: what became of the
    # worker is not a fact the record holds, so it does not get to be the
    # headline about a claim that did lapse.
    assert lapsed.detail == (
        "Nothing records whether the worker that claimed it has stopped."
    )
    assert "worker" not in lapsed.sentence
    assert lapsed.lease_expires_at == claim.lease_expires_at
    # The occurrence is still `claimed` in the record. The banner reports the
    # lease, not the column, which is the whole distinction #918 established.
    with factory() as checking:
        state = checking.execute(
            text("select state from public.due_work_occurrences where id = :id"),
            {"id": claim.occurrence_id},
        ).scalar_one()
    assert state == "claimed"


def test_a_completed_pass_holds_no_claim_and_the_page_says_nothing(factory):
    """A finished pass leaves nobody holding the occurrence, and prints nothing.

    The reading is still available -- a caller that wants to know whether
    anything holds the pass gets `UNCLAIMED` -- but it carries no sentence, so
    the register prints no banner. A permanent line saying no worker holds a
    claim is true for almost the whole life of almost every project, and a
    reader learns to stop seeing it long before the day it would have mattered.
    """

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id = _project(setup, "banner-completed", starts_at=starts_at)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=starts_at)
        ticking.commit()
    claim = _claim(factory, starts_at)
    finished_at = starts_at + timedelta(minutes=4)
    with factory() as finalizing:
        complete_due_work(
            finalizing,
            claim,
            handler_result=_completed_result(project_id, finished_at),
            now=finished_at,
        )
        finalizing.commit()

    banner = _banner(factory, project_id, finished_at)

    assert banner.status == UNCLAIMED
    assert banner.sentence == "", (
        "a pass nobody holds has no sentence, so the page renders no banner"
    )
    assert banner.detail == ""
    assert UNCLAIMED not in SENTENCES
    assert banner.claimed_at is None
    assert banner.lease_expires_at is None


def test_a_failed_pass_keeps_its_failure_where_a_coordinator_sees_it(factory):
    """Dropping the unclaimed banner must not drop the one failure it hid.

    "No worker holds a claim" was true of a completed pass and of a pass that
    had burned through its retries and stopped, and it said the same thing
    about both. The runtime writes `failed` only once `max_attempts` is spent,
    so it is a terminal fact rather than a lull between attempts, and it is the
    one unclaimed state that still gets a sentence.
    """

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id = _project(setup, "banner-failed", starts_at=starts_at)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=starts_at)
        ticking.commit()
    claim = _claim(factory, starts_at)
    failed_at = starts_at + timedelta(minutes=2)
    with factory() as failing:
        outcome = fail_due_work(
            failing,
            claim,
            error_code="extraction_failed",
            now=failed_at,
            retryable=False,
        )
        assert outcome.execution_outcome == "failed"
        failing.commit()

    banner = _banner(factory, project_id, failed_at)

    assert banner.status == FAILED
    assert banner.sentence == "The latest document-processing pass failed."
    # No qualification, because there is none to make: the occurrence is not
    # claimed and not recoverable, so nothing about a worker is in question.
    assert banner.detail == ""
    # And none of the three things a failure does not mean. The sentence is
    # about the pass: it names no document and counts nothing, because nothing
    # in this reading counts -- what became of each delivery is the register's
    # own rows, immediately below it. It does not say completed work was
    # undone, and it promises no retry; a subsequent attempt is a later
    # occurrence, and the test below shows it arriving as one.
    assert not any(
        word in banner.sentence.lower()
        for word in ("document ", "documents", "file", "page", "every", "all ")
    )
    assert not any(char.isdigit() for char in banner.sentence)


def test_a_later_pass_displaces_the_failure_from_its_own_record(factory):
    """What "the latest" has to earn, in each of the three states it loses to.

    The sentence says *latest*, so the reading has to establish that the run it
    read is the latest relevant attempt rather than the only one it happened to
    find. It does, and by the projection rather than by this module: every
    terminal state releases its lease, so `lease_expires_at desc nulls last`
    ranks only claimed rows and `due_at desc, id desc` decides among the rest.
    A `failed` reading therefore means nothing is claimed *and* nothing is
    newer.

    So the next due slot has to take the banner away from the failure, and it
    does so from its own record in all three of the states it can be in --
    queued, claimed, finished. Nothing is written on the failed occurrence to
    make that happen and nothing about the retry is read off it, which is the
    distinction a sentence promising "a worker is retrying" would erase.
    """

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id = _project(setup, "banner-outlived", starts_at=starts_at)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=starts_at)
        ticking.commit()
    first = _claim(factory, starts_at)
    failed_at = starts_at + timedelta(minutes=2)
    with factory() as failing:
        fail_due_work(
            failing,
            first,
            error_code="extraction_failed",
            now=failed_at,
            retryable=False,
        )
        failing.commit()
    assert _banner(factory, project_id, failed_at).status == FAILED

    # The next due slot, as its own occurrence. Queued and unclaimed, the page
    # says nothing: the failure is no longer the latest attempt, and a queued
    # one is not something the record can describe beyond that.
    next_due = starts_at + timedelta(hours=1)
    with factory() as ticking:
        enqueue_due_work(ticking, now=next_due)
        ticking.commit()
    queued = _banner(factory, project_id, next_due)
    assert queued.status == UNCLAIMED
    assert queued.sentence == ""

    # Claimed, it is the claim sentence and not a qualified failure.
    second = _claim(factory, next_due)
    assert second.occurrence_id != first.occurrence_id
    claimed = _banner(factory, project_id, next_due)
    assert claimed.status == CLAIMED
    assert SENTENCES[FAILED] != claimed.sentence

    # Finished, the page is silent again rather than reverting to the failure
    # underneath it.
    finished_at = next_due + timedelta(minutes=4)
    with factory() as finalizing:
        complete_due_work(
            finalizing,
            second,
            handler_result=_completed_result(project_id, finished_at),
            now=finished_at,
        )
        finalizing.commit()
    assert _banner(factory, project_id, finished_at).status == UNCLAIMED

    # The failure is still in the record; it stopped being the latest attempt,
    # which is a different thing from being forgotten. Both terminal rows carry
    # no lease, which is precisely what leaves `due_at` to decide between them
    # -- a terminal state that kept its lease would let this failure outrank
    # the completed pass that followed it.
    with factory() as checking:
        rows = (
            checking.execute(
                text(
                    "select o.state, o.claimed_at, o.lease_expires_at "
                    "  from public.due_work_occurrences o "
                    "  join public.due_work_schedules s "
                    "    on s.id = o.scheduled_job_id "
                    " where s.project_id = :project "
                    "   and s.handler_key = :handler "
                    " order by o.due_at"
                ),
                {"project": project_id, "handler": HANDLER_PROJECT_PROCESSING},
            )
            .mappings()
            .all()
        )
    assert [dict(row) for row in rows] == [
        {"state": "failed", "claimed_at": None, "lease_expires_at": None},
        {"state": "completed", "claimed_at": None, "lease_expires_at": None},
    ]


def test_a_lapsed_lease_is_not_a_processing_failure(factory):
    """The two readings stay two readings, however long the lease has been gone.

    A lease expiring means the claim lapsed and the occurrence is recoverable.
    It is not a failure, and the distance between them is not a matter of
    degree: `_status` reads `failed` off the occurrence's own column, which the
    runtime writes only once the retries are spent, and reaches `CLAIM_EXPIRED`
    from a `claimed` row whose lease has gone. A lapsed lease can therefore
    never arrive at the failure sentence, no matter how stale it is -- and a
    coordinator reading "Recovery is pending" is being told the truth rather
    than a softened version of "this failed".
    """

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id = _project(setup, "banner-lapsed-not-failed", starts_at=starts_at)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=starts_at)
        ticking.commit()
    claim = _claim(factory, starts_at)

    for now in (
        claim.lease_expires_at,
        claim.lease_expires_at + timedelta(days=30),
    ):
        banner = _banner(factory, project_id, now)
        assert banner.status == CLAIM_EXPIRED
        assert banner.sentence == SENTENCES[CLAIM_EXPIRED]
        assert banner.sentence != SENTENCES[FAILED]
        assert "fail" not in banner.sentence.lower()

    # Because the record says so: the occurrence is `claimed`, and no age of
    # lease turns that column into `failed`. The runtime writing `failed` is a
    # separate act, and the reading follows the act rather than the clock.
    with factory() as checking:
        state = checking.execute(
            text("select state from public.due_work_occurrences where id = :id"),
            {"id": claim.occurrence_id},
        ).scalar_one()
    assert state == "claimed"


def test_recovery_of_a_lapsed_claim_is_reported_as_the_new_claim(factory):
    """The banner follows the lease that is live now, not the one that lapsed.

    Recovery is the case where a stale reading is worst: the occurrence keeps
    its identity across the recovery, so a projection that remembered the first
    claim would keep telling a coordinator that a pass expired while a worker
    was demonstrably holding it.
    """

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id = _project(setup, "banner-recovered", starts_at=starts_at)
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=starts_at)
        ticking.commit()
    first = _claim(factory, starts_at)
    assert _banner(factory, project_id, first.lease_expires_at).status == CLAIM_EXPIRED

    recovered = _claim(factory, first.lease_expires_at)

    assert recovered.occurrence_id == first.occurrence_id
    assert recovered.claim_token != first.claim_token
    banner = _banner(factory, project_id, first.lease_expires_at)
    assert banner.status == CLAIMED
    assert banner.lease_expires_at == recovered.lease_expires_at
    assert banner.lease_expires_at > first.lease_expires_at


def test_the_web_capability_is_granted_the_projection_and_nothing_under_it(
    factory,
):
    """What a banner costs in privilege: one SELECT on one view.

    #900's first draft proposed restoring `corridor_web`'s grants on both
    scheduler relations, which is a read of every claim token and -- through
    the default privileges the revoke took back -- an INSERT, UPDATE and DELETE
    on the runtime's own claim state. The page needs none of it. This asserts
    the whole privilege surface of the change, in the catalog, rather than
    trusting the migration's text.
    """

    with factory() as owner:
        projection = owner.execute(
            text(
                "select r.rolname, "
                "       has_table_privilege(r.rolname, c.oid, 'select') as readable, "
                "       has_table_privilege(r.rolname, c.oid, 'insert') as insertable, "
                "       has_table_privilege(r.rolname, c.oid, 'update') as updatable, "
                "       has_table_privilege(r.rolname, c.oid, 'delete') as deletable "
                "  from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "  cross join pg_roles r "
                " where n.nspname = 'public' "
                "   and c.relname = 'current_project_processing_pass' "
                "   and r.rolname in ('corridor_web', 'corridor_worker', "
                "                     'corridor_legacy_dev') "
                " order by r.rolname"
            )
        ).mappings().all()
        scheduler = owner.execute(
            text(
                "select relname, "
                "       has_table_privilege('corridor_web', c.oid, 'select') as readable, "
                "       has_table_privilege('corridor_web', c.oid, 'insert') as insertable, "
                "       has_table_privilege('corridor_web', c.oid, 'update') as updatable, "
                "       has_table_privilege('corridor_web', c.oid, 'delete') as deletable "
                "  from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                " where n.nspname = 'public' "
                "   and c.relname in ('due_work_occurrences', 'due_work_schedules', "
                "                     'due_work_receipts')"
            )
        ).mappings().all()
        columns = owner.execute(
            text(
                "select a.attname from pg_attribute a "
                "  join pg_class c on c.oid = a.attrelid "
                "  join pg_namespace n on n.oid = c.relnamespace "
                " where n.nspname = 'public' and a.attnum > 0 "
                "   and not a.attisdropped "
                "   and c.relname = 'current_project_processing_pass' "
                " order by a.attnum"
            )
        ).scalars().all()

    # The legacy development login keeps the reading it holds on every other
    # relation the frozen surfaces reach (ADR-0081); `corridor_worker` reads
    # the scheduler itself and has no use for a partitioned projection. Nobody
    # holds a write on it: a joined view is not auto-updatable anyway, and the
    # schema owner's default privileges are taken back rather than relied on.
    assert [dict(row) for row in projection] == [
        {
            "rolname": "corridor_legacy_dev",
            "readable": True,
            "insertable": False,
            "updatable": False,
            "deletable": False,
        },
        {
            "rolname": "corridor_web",
            "readable": True,
            "insertable": False,
            "updatable": False,
            "deletable": False,
        },
        {
            "rolname": "corridor_worker",
            "readable": False,
            "insertable": False,
            "updatable": False,
            "deletable": False,
        },
    ]
    assert [dict(row) for row in scheduler] == [
        {
            "relname": name,
            "readable": False,
            "insertable": False,
            "updatable": False,
            "deletable": False,
        }
        for name in ("due_work_occurrences", "due_work_receipts", "due_work_schedules")
    ]
    assert columns == [
        "project_id",
        "occurrence_state",
        "claimed_at",
        "lease_expires_at",
    ]


# --- The words on the page ------------------------------------------------
#
# The six cases above prove what the record supports. This one proves the page
# prints it: a sentence that never reaches the template is a reading nobody
# reads, and the register is the only surface #900 puts this on.


@pytest.fixture
def rendering_client(session):
    from fastapi.testclient import TestClient

    from corridor.principals import HumanPrincipal
    from corridor.web.app import app, get_human_principal, get_session

    principal = HumanPrincipal(PRINCIPAL)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: principal
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_the_register_prints_the_claim_sentence_and_then_the_expiry_one(
    session, rendering_client
):
    """Both readings, on the real route, through the real template.

    The clock is overridden rather than waited on, which is the point of taking
    it as a dependency: the same committed claim is read once inside its lease
    and once after it, and the page says two different true things about it.
    """

    from corridor.web.app import app, get_review_clock

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    project_id = _member_project(session, "banner-page")
    _processing_schedule(session, project_id, starts_at=starts_at)
    enqueue_due_work(session, now=starts_at)
    claim = claim_due_work(session, now=starts_at, owner="runtime:banner-worker")
    assert claim is not None
    session.flush()
    slug = session.get(Project, project_id).slug

    def _page(now: datetime) -> str:
        app.dependency_overrides[get_review_clock] = lambda: (lambda: now)
        response = rendering_client.get(f"/projects/{slug}/sources")
        assert response.status_code == 200
        # Unescaped, because the sentences carry an apostrophe and Jinja's
        # autoescape renders it `&#39;`. Comparing the escaped form would tie
        # this proof to the escaping rather than to the words.
        return unescape(response.text)

    held = _page(claim.lease_expires_at - timedelta(minutes=1))
    lapsed = _page(claim.lease_expires_at)

    assert SENTENCES[CLAIMED] in held
    assert SENTENCES[CLAIM_EXPIRED] not in held
    assert SENTENCES[CLAIM_EXPIRED] in lapsed
    assert SENTENCES[CLAIMED] not in lapsed
    # The qualification is on the page, and under the sentence rather than in
    # it: the claim lapsing is the headline, what became of the worker is not.
    assert DETAILS[CLAIM_EXPIRED] in lapsed
    assert DETAILS[CLAIM_EXPIRED] not in held

    # And once the pass finishes there is no banner at all. The permanent line
    # saying no worker holds a claim is the thing this page stopped printing.
    # The worker finalizes inside its lease, because a finalization after it
    # is the stale claim `complete_due_work` refuses.
    finished_at = claim.lease_expires_at - timedelta(minutes=1)
    complete_due_work(
        session,
        claim,
        handler_result=_completed_result(project_id, finished_at),
        now=finished_at,
    )
    session.flush()
    finished = _page(claim.lease_expires_at + timedelta(minutes=1))
    assert "holds a claim" not in finished
    assert 'class="pass"' not in finished
    assert SENTENCES[CLAIMED] not in finished
    assert SENTENCES[CLAIM_EXPIRED] not in finished
    # A per-row "Processing" state is the thing #900 refused to invent, and
    # adding a banner is not a way of smuggling it back: the register's state
    # vocabulary still carries no state that says a particular document is
    # being read right now, and neither page says one is.
    from corridor.source_register import STATE_WORDS

    assert "processing" not in STATE_WORDS
    assert "Processing" not in STATE_WORDS.values()
    for page in (held, lapsed):
        assert "is being read" not in page


def test_the_register_prints_the_failure_above_rows_it_does_not_speak_for(
    session, rendering_client
):
    """The failure sentence on the real route, and what it deliberately omits.

    The approved wording is two sentences: "The latest document-processing pass
    failed. Open Sources to see the affected documents and next steps." This
    page is Sources. The affected documents are the rows immediately below the
    banner, so the second sentence would send a reader to where they already
    are; it is left off rather than reworded, because what to say instead to a
    reader already here is a customer-facing wording decision and not one to
    make in passing.

    What the page does carry is the division the first sentence depends on. The
    banner speaks for the *pass*. Each delivery's own outcome is a register row
    with its own state word, and one of those words is the one that says a
    later pass will retry -- so a failed pass never has to claim that every
    document failed, that finished work was undone, or that nothing is trying
    again.
    """

    from corridor.source_register import STATE_WORDS
    from corridor.web.app import app, get_review_clock

    starts_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    project_id = _member_project(session, "banner-failed-page")
    _processing_schedule(session, project_id, starts_at=starts_at)
    enqueue_due_work(session, now=starts_at)
    claim = claim_due_work(session, now=starts_at, owner="runtime:banner-worker")
    assert claim is not None
    failed_at = starts_at + timedelta(minutes=2)
    fail_due_work(
        session,
        claim,
        error_code="extraction_failed",
        now=failed_at,
        retryable=False,
    )
    session.flush()
    slug = session.get(Project, project_id).slug

    app.dependency_overrides[get_review_clock] = lambda: (lambda: failed_at)
    response = rendering_client.get(f"/projects/{slug}/sources")
    assert response.status_code == 200
    page = unescape(response.text)

    assert SENTENCES[FAILED] in page
    assert SENTENCES[CLAIMED] not in page
    assert SENTENCES[CLAIM_EXPIRED] not in page
    # Not printed, and not silently: the link half of the approved wording has
    # no reader here, and the surface count below is what notices the day it
    # acquires one.
    assert "Open Sources" not in page
    # The rows are on this page and they are the ones that carry per-delivery
    # outcomes, including a retry. The banner's sentence is none of them.
    assert "Source register" in page
    assert SENTENCES[FAILED] not in STATE_WORDS.values()
    assert "processing_failed" in STATE_WORDS


def test_the_banner_has_one_surface_and_it_is_the_page_it_would_link_to():
    """Why the approved second sentence is printed nowhere.

    "Open Sources to see the affected documents and next steps" is a link out
    of wherever the banner is, and today the banner is only ever on Sources.
    That is a fact about the templates rather than an opinion, so it is checked
    like one: the day a second surface prints this reading, this fails, and the
    half of the approved sentence that was waiting for a reader somewhere else
    has one.
    """

    from pathlib import Path

    import corridor.web

    templates = Path(corridor.web.__file__).parent / "templates"
    printing = sorted(
        path.name
        for path in templates.glob("*.html")
        if "processing_pass" in path.read_text()
    )
    assert printing == ["source_uploads.html"]
