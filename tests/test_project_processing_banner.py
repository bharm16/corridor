"""The project-level processing banner, against real committed claims (#900).

A claim, a lease and an expiry are facts about *committed* rows seen from
another transaction, so every case here runs on the harness-owned
``runtime_database`` rather than the rollback-scoped session: a lease that only
ever existed inside one open transaction would prove nothing about what a web
request reads. The clock is an argument to the reader, so expiry is stated
rather than waited for.

The six cases are the ones #900 asks to be proved, and each of them is a way
the banner could lie:

- another project's claim, which must not become this project's banner;
- an unrelated scheduled handler, whose claim is not a source-processing pass;
- a claim whose lease is in force, which is the one thing the record supports;
- an expired lease, which says the claim lapsed and never that the worker
  stopped;
- a completed pass, which holds no claim;
- recovery, where the next worker takes the lapsed occurrence and the banner
  follows the new claim rather than the old one.

Two more follow them. The seventh is the grant: the page reads the projection
and holds nothing on the scheduler underneath it. The eighth is the page, where
both sentences are proved through the real route and the real template, because
a reading the register never prints is a reading nobody has.
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
)
from corridor.models import Project, ProjectRosterEntry
from corridor.project_processing_banner import (
    CLAIMED,
    CLAIM_EXPIRED,
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
    return {
        "schema_version": 1,
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
    assert lapsed.sentence == (
        "The claim on this project's document-processing pass has expired and "
        "recovery is pending. Nothing records whether the worker that claimed "
        "it has stopped."
    )
    assert lapsed.lease_expires_at == claim.lease_expires_at
    # The occurrence is still `claimed` in the record. The banner reports the
    # lease, not the column, which is the whole distinction #918 established.
    with factory() as checking:
        state = checking.execute(
            text("select state from public.due_work_occurrences where id = :id"),
            {"id": claim.occurrence_id},
        ).scalar_one()
    assert state == "claimed"


def test_a_completed_pass_holds_no_claim(factory):
    """A finished pass leaves nobody holding the occurrence, and says so."""

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
    assert banner.sentence == SENTENCES[UNCLAIMED]
    assert banner.claimed_at is None
    assert banner.lease_expires_at is None


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
    # A per-row "Processing" state is the thing #900 refused to invent, and
    # adding a banner is not a way of smuggling it back: the register's state
    # vocabulary still carries no state that says a particular document is
    # being read right now, and neither page says one is.
    from corridor.source_register import STATE_WORDS

    assert "processing" not in STATE_WORDS
    assert "Processing" not in STATE_WORDS.values()
    for page in (held, lapsed):
        assert "is being read" not in page
