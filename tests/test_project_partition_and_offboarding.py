"""Project authorization as a data partition, and offboarding that really revokes (#531).

Two claims are proved here, and both are proved by doing the thing rather than
by reading a flag.

**The partition.** ADR-0083 says a project is an authorization *and data
partition* boundary inside the customer database. Until #531 that boundary was
``_authorize`` plus a ``project_id ==`` in every reader, which one missing
predicate defeats. So the partition tests connect as the real ``corridor_web``
login against committed rows and run the query a careless reader would write —
``select * from source_segments``, no predicate at all — and assert it returns
one project's rows. They also forge the session setting by hand and assert the
forged partition is the empty one.

**Revocation.** "Tests proving revoked access" cannot mean asserting that
``active`` became false. So the offboarding tests drive the real HTTP routes
with the cookie a real magic link established, offboard the person, and assert
the very next request is refused — and that a link already sitting in their
inbox can no longer open a session.

No test here reads the wall clock: ordering is by ``audit_log.id``, the
append-only watermark, and every moment a test needs is passed in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import NullPool

from corridor import access, audit, identity_audit
from corridor.config import settings
from corridor.db import Session, engine
from corridor.models import (
    AuditLog,
    Document,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
    SignInToken,
    SourceSegment,
    WebSession,
)
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import app, get_session

OPERATOR = HumanPrincipal("local:operations")
LEAVER = HumanPrincipal("local:leaver")

WEB_PASSWORD = os.environ.get("CORRIDOR_WEB_DB_PASSWORD") or "corridor_web"


# --- The partition, as the deployed web login -----------------------------


def _web_url(database_name: str) -> str:
    return (
        make_url(settings.database_url)
        .set(
            database=database_name,
            username="corridor_web",
            password=WEB_PASSWORD,
        )
        .render_as_string(hide_password=False)
    )


def _segment(project_id: int, document_id: int, text_value: str) -> SourceSegment:
    return SourceSegment(
        project_id=project_id,
        document_id=document_id,
        kind="spreadsheet_cell",
        exact_text=text_value,
        content_sha256=sha256(text_value.encode()).hexdigest(),
        ordinal=1,
        sheet_name="Conflicts",
        cell_range="A2",
    )


# A fixed moment, because no test here reads the wall clock.
RECORDED_AT = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

# One row per project in each newly partitioned family (#657), chosen so the
# whole seam can be seeded without standing up six write commands: each is a
# plain insert the schema owner may make, and each carries `project_id`.
SEEDED_RECORD_ROWS = {
    "delta_groups": (
        "insert into delta_groups (project_id, source_family, source_revision) "
        "values (:project_id, 'ucm', :slug) returning id"
    ),
    "candidates": (
        "insert into candidates (project_id, kind, payload_json, "
        "source_document_id, source_pages) "
        "values (:project_id, 'dependency', '{}'::jsonb, :document_id, "
        "array[1]) returning id"
    ),
    "recorded_verbal_origins": (
        "insert into recorded_verbal_origins "
        "(project_id, recorded_by, recorded_at, exact_text, content_sha256) "
        "values (:project_id, 'local:recorder', :recorded_at, :slug, :digest) "
        "returning id"
    ),
    "facts": (
        "insert into facts (project_id, fact_type, subject_kind, subject_key, "
        "text_value, transformation, recorded_by, content_sha256) "
        "values (:project_id, 'statement_wording', 'statement_candidate', "
        ":slug, :slug, 'exact_prose_span_v1', 'local:recorder', :digest) returning id"
    ),
}


# The two guards that hold "only the typed command writes accepted authority".
# They are lifted for the seeding and put straight back; whether they hold is a
# different claim with its own test, and borrowing them here would only mean
# the partition on the accepted record goes untested.
_RECORD_WRITE_GUARDS = (
    "alter table project_record_revisions "
    "{action} trigger trg_project_record_revisions_guard",
    "alter table fact_decisions {action} trigger trg_fact_decisions_guard",
)


def _seed_record_rows(
    owner, project_id: int, slug: str, *, document_id: int | None = None
) -> dict[str, int]:
    """One committed row per partitioned family, plus the accepted projection.

    Proving that the *view* honours the partition needs a row in it, so the
    two accepted-authority write guards are lifted around those inserts alone.
    """

    if document_id is None:
        document_id = int(
            owner.execute(
                text(
                    "insert into documents (project_id, sha256, filename, "
                    "doc_type, numbering_scheme, pages, parse_status) "
                    "values (:project_id, :digest, :slug, 'matrix', "
                    "'project-unique', 1, 'parsed') returning id"
                ),
                {
                    "project_id": project_id,
                    "digest": sha256(f"{slug}-doc".encode()).hexdigest(),
                    "slug": f"{slug}.xlsx",
                },
            ).scalar_one()
        )
    segment_id = int(
        owner.execute(
            text(
                "insert into source_segments (project_id, document_id, kind, "
                "exact_text, content_sha256, ordinal, sheet_name, cell_range) "
                "values (:project_id, :document_id, 'spreadsheet_cell', :slug, "
                ":digest, 2, 'Conflicts', 'A3') returning id"
            ),
            {
                "project_id": project_id,
                "document_id": document_id,
                "slug": f"{slug}-cell",
                "digest": sha256(f"{slug}-cell".encode()).hexdigest(),
            },
        ).scalar_one()
    )
    ids = {}
    for table, statement in SEEDED_RECORD_ROWS.items():
        ids[table] = int(
            owner.execute(
                text(statement),
                {
                    "project_id": project_id,
                    "slug": f"{slug}-row",
                    "recorded_at": RECORDED_AT,
                    "digest": sha256(f"{slug}-row".encode()).hexdigest(),
                    "document_id": document_id,
                },
            ).scalar_one()
        )
    # A source-backed Fact is refused at the end of the statement unless a
    # `value_source` names the words it came from, so the Fact and its source
    # are one act here exactly as they are everywhere else.
    ids["fact_sources"] = int(
        owner.execute(
            text(
                "insert into fact_sources (project_id, fact_id, "
                "source_segment_id, role, ordinal) "
                "values (:project_id, :fact_id, :segment_id, 'value_source', 1)"
                " returning id"
            ),
            {
                "project_id": project_id,
                "fact_id": ids["facts"],
                "segment_id": segment_id,
            },
        ).scalar_one()
    )
    for statement in _RECORD_WRITE_GUARDS:
        owner.execute(text(statement.format(action="disable")))
    try:
        revision_id = int(
            owner.execute(
                text(
                    "insert into project_record_revisions "
                    "(project_id, command_type, human_principal, "
                    "idempotency_key) "
                    "values (:project_id, 'adopt_baseline', 'local:recorder', "
                    ":key) returning id"
                ),
                {"project_id": project_id, "key": f"{slug}-revision"},
            ).scalar_one()
        )
        ids["project_record_revisions"] = revision_id
        ids["fact_decisions"] = int(
            owner.execute(
                text(
                    "insert into fact_decisions "
                    "(project_id, fact_id, subject_key, fact_type, "
                    "revision_id) values (:project_id, :fact_id, :slug, "
                    "'statement_wording', :revision_id) returning id"
                ),
                {
                    "project_id": project_id,
                    "fact_id": ids["facts"],
                    "slug": f"{slug}-row",
                    "revision_id": revision_id,
                },
            ).scalar_one()
        )
    finally:
        # The deferred foreign keys of the rows just written are still pending,
        # and PostgreSQL refuses to alter a table that has pending trigger
        # events. Making them immediate settles those rows here, which is also
        # where a failure belongs.
        owner.execute(text("set constraints all immediate"))
        for statement in _RECORD_WRITE_GUARDS:
            owner.execute(text(statement.format(action="enable")))
        # And put the deferral back, because a second project seeded in the
        # same transaction needs its Fact and that Fact's value source to be
        # one act again.
        owner.execute(text("set constraints all deferred"))
    return ids


@pytest.fixture
def two_projects(runtime_database):
    """Two committed projects with one segment each, and a member of only one."""

    with runtime_database.session_factory.begin() as owner:
        ours = Project(slug="ours", name="Ours", is_synthetic=True)
        theirs = Project(slug="theirs", name="Theirs", is_synthetic=True)
        owner.add_all([ours, theirs])
        owner.flush()
        documents = {}
        for index, project in enumerate((ours, theirs)):
            document = Document(
                project_id=project.id,
                sha256=str(index + 3) * 64,
                filename=f"{project.slug}.xlsx",
                doc_type="matrix",
                numbering_scheme="project-unique",
                pages=1,
                parse_status="parsed",
            )
            owner.add(document)
            owner.flush()
            documents[project.slug] = document.id
            owner.add(_segment(project.id, document.id, f"{project.slug}-UC-1"))
        owner.add(
            ProjectRosterEntry(
                project_id=ours.id,
                principal_subject=LEAVER.subject,
                display_name="Leaver",
                active=True,
                can_coordinate=True,
            )
        )
        owner.flush()
        ids = (ours.id, theirs.id)
    return ids


@pytest.fixture
def their_record_rows(runtime_database, two_projects):
    """The other project's committed row in each partitioned family, by id.

    A direct id is how a partition gets bypassed when only listing queries were
    considered: `select * from delta_groups` is obviously project-shaped and
    `where id = 41` is not, so the second is the one that has to be tried.
    """

    _ours, theirs = two_projects
    with runtime_database.session_factory.begin() as owner:
        _seed_record_rows(owner, _ours, "ours")
        return _seed_record_rows(owner, theirs, "theirs")


@pytest.fixture
def web_connection(runtime_database):
    """A connection held by the deployed web capability, not by the owner."""

    web_engine = create_engine(
        _web_url(runtime_database.name), poolclass=NullPool, future=True
    )
    with web_engine.connect() as connection:
        yield connection
    web_engine.dispose()


def test_a_reader_that_forgets_its_project_predicate_reads_only_its_partition(
    two_projects, web_connection
):
    """The whole point: the careless query is the safe one now.

    ``select * from source_segments`` names no project. Before #531 it returned
    every customer project's rows to whoever asked; now it returns the rows of
    the declared partition and nothing else.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        careless = web.scalars(select(SourceSegment)).all()

    assert [row.exact_text for row in careless] == ["ours-UC-1"]


def test_no_declared_partition_reads_nothing_at_all(two_projects, web_connection):
    """Fail closed: a connection that declared nothing is not a connection that sees all."""

    with OrmSession(bind=web_connection) as web:
        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []


def test_a_partition_the_database_did_not_seal_is_the_empty_partition(
    two_projects, web_connection
):
    """Setting the scope by hand buys nothing; the seal is what the policy reads.

    This is the difference between a data partition and an application filter.
    The web login can set the session setting — no privilege stops it — and it
    still reads nothing, because the seal it cannot compute does not verify.
    """

    _ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        web.execute(
            text("select set_config('corridor.project_partition', :scope, true)"),
            {"scope": str(theirs)},
        )
        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []


def test_a_forged_seal_is_refused_as_thoroughly_as_no_seal(
    two_projects, web_connection
):
    """A guessed seal is still not the seal; the secret is unreadable, not obscure."""

    _ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        web.execute(
            text("select set_config('corridor.project_partition', :scope, true)"),
            {"scope": str(theirs)},
        )
        web.execute(
            text("select set_config('corridor.project_partition_seal', :seal, true)"),
            {"seal": sha256(b"guess").hexdigest()},
        )
        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []


def test_the_web_capability_cannot_read_the_seal_secret(web_connection):
    """If the capability could read the secret, every proof above would be theatre."""

    with pytest.raises(ProgrammingError) as refused:
        web_connection.execute(text("select secret from project_partition_secrets"))
    web_connection.rollback()

    assert "permission denied for table project_partition_secrets" in str(
        refused.value
    )


def test_the_web_capability_cannot_seal_a_partition_of_its_own(
    two_projects, web_connection
):
    """Only the two commands that prove something may seal a scope.

    ``seal_project_partition`` asks no question — it is the half of the
    mechanism the proving commands call — so nothing is granted execute on it.
    """

    _ours, theirs = two_projects
    with pytest.raises(ProgrammingError) as refused:
        web_connection.execute(
            text("select seal_project_partition(:scope)"), {"scope": str(theirs)}
        )
    web_connection.rollback()
    assert "permission denied for function seal_project_partition" in str(
        refused.value
    )


def test_declaring_a_partition_for_a_project_you_are_not_on_is_refused(
    two_projects, web_connection
):
    """Membership is proved by the database, as the command's owner, not by the caller."""

    _ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )
    web_connection.rollback()


# --- A refusal a caller can actually recover from (#654) -------------------


def test_catching_a_refusal_leaves_the_surrounding_session_usable(
    two_projects, web_connection
):
    """The refusal is recoverable, not a poisoned transaction (#654).

    The database proves membership by *raising*, which aborts the transaction
    the caller is sitting in. Before #654 the next statement on that session —
    any statement — came back ``InFailedSqlTransaction``, so a caller that
    meant to recover (offer a narrower reading, fall back to a project list,
    render a partial page) found the session unusable for a reason the
    exception name does not suggest. Running the command in a savepoint and
    giving up only that savepoint is the whole fix.
    """

    _ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )

        assert web.scalar(text("select 1")) == 1
    web_connection.rollback()


def test_a_refused_first_attempt_installs_no_partition_and_shows_no_rows(
    two_projects, web_connection
):
    """Recovering from a refusal must not be recovering *into* someone's project.

    Read as the deployed ``corridor_web`` login against committed rows, the
    way every other partition proof here is read: the refused attempt leaves no
    declared partition at all, and the careless predicate-free query still
    returns nothing.
    """

    _ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )

        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []
    web_connection.rollback()


def test_a_refused_switch_keeps_the_partition_the_caller_already_held(
    two_projects, web_connection
):
    """Rolling back the savepoint restores the scope, it does not merely clear it.

    Scope is transaction-local (``set_config(..., true)``), and PostgreSQL
    restores such a setting when a subtransaction aborts. So a caller that
    holds a valid partition and is refused a different one is left holding the
    valid one — not the empty partition, and certainly not the refused one.
    """

    ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )

        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )

        assert access.current_project_partition(web) == (ours,)
        assert [
            row.exact_text for row in web.scalars(select(SourceSegment)).all()
        ] == ["ours-UC-1"]
    web_connection.rollback()


# --- One transaction holds one scope (#662, amending #654) -----------------


@pytest.fixture
def member_of_both(runtime_database, two_projects):
    """The same person on both projects, so a switch is refused by the *rule*.

    Refusing a switch to a project the caller was never on proves nothing about
    scope: the membership proof already refuses that. The switch has to be one
    the database would otherwise have allowed.
    """

    _ours, theirs = two_projects
    with runtime_database.session_factory.begin() as owner:
        owner.add(
            ProjectRosterEntry(
                project_id=theirs,
                principal_subject=LEAVER.subject,
                display_name="Leaver",
                active=True,
            )
        )
    return two_projects


def test_the_first_declaration_of_a_transaction_sets_the_scope(
    member_of_both, web_connection
):
    """None to project A: the ordinary case, and the one everything else is measured against."""

    ours, _theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        assert access.current_partition_declaration(web) is None

        assert access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        ) == ours

        assert access.current_project_partition(web) == (ours,)
        assert (
            access.current_partition_declaration(web)
            == f"project:{LEAVER.subject}:{ours}"
        )
    web_connection.rollback()


def test_redeclaring_the_same_project_scope_is_idempotent(
    member_of_both, web_connection
):
    """A gate that runs twice on one request must not be a defect (#662)."""

    ours, _theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )

        assert access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        ) == ours

        assert access.current_project_partition(web) == (ours,)
    web_connection.rollback()


def test_changing_to_another_project_inside_one_transaction_is_refused(
    member_of_both, web_connection
):
    """The correction #662 makes to what #654 measured.

    #654 found that a person on both projects could move from one to the other
    inside a single transaction, because every successful declaration re-seals
    the setting, and recorded that as the behaviour. It is the wrong
    behaviour: it makes the authorization context of a unit of work a moving
    target, so an atomic read can span two customers' projects and nothing
    objects. A transaction now holds one scope, and a new transaction is the
    boundary for changing it.
    """

    ours, theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )

        with pytest.raises(access.PartitionScopeConflict):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )
    web_connection.rollback()


def test_a_refused_scope_change_leaves_the_caller_holding_what_it_had(
    member_of_both, web_connection
):
    """Refusing a change must not cost the caller the reading it was doing.

    The savepoint #654 established is what makes this true: the scope is a
    transaction-local setting, PostgreSQL restores such a setting when a
    subtransaction aborts, and the command runs inside one. So the reader is
    left with project A's rows — not the empty partition, and certainly not
    project B's.
    """

    ours, theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )

        with pytest.raises(access.PartitionScopeConflict):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )

        assert access.current_project_partition(web) == (ours,)
        assert (
            access.current_partition_declaration(web)
            == f"project:{LEAVER.subject}:{ours}"
        )
        assert [
            row.exact_text for row in web.scalars(select(SourceSegment)).all()
        ] == ["ours-UC-1"]
        assert web.scalar(text("select 1")) == 1
    web_connection.rollback()


def test_widening_a_single_project_to_the_member_reading_is_refused(
    member_of_both, web_connection
):
    """Changing scope *kind* is changing scope.

    A cross-project reading is a wider partition than a single project, so
    reaching it from inside a transaction that already declared one project is
    exactly the widening #662 refuses. The cross-project surfaces declare the
    member partition first and never after.
    """

    ours, _theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )

        with pytest.raises(access.PartitionScopeConflict):
            access.open_member_project_partition(
                web, principal_subject=LEAVER.subject
            )

        assert access.current_project_partition(web) == (ours,)
    web_connection.rollback()


def test_redeclaring_the_same_member_reading_is_idempotent(
    member_of_both, web_connection
):
    """The cross-project scope is identified by the person, not by today's ids.

    Both surfaces that use it declare it at the top of the request, and a
    roster that moves under a long transaction must not turn the second
    declaration into a refusal.
    """

    ours, theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        first = access.open_member_project_partition(
            web, principal_subject=LEAVER.subject
        )

        again = access.open_member_project_partition(
            web, principal_subject=LEAVER.subject
        )

        assert first == again == (ours, theirs)
        assert (
            access.current_partition_declaration(web)
            == f"member:{LEAVER.subject}"
        )
    web_connection.rollback()


def test_narrowing_the_member_reading_to_one_project_is_refused(
    member_of_both, web_connection
):
    """Narrowing is a change too: batch work uses the declared cross-project scope."""

    ours, theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        access.open_member_project_partition(
            web, principal_subject=LEAVER.subject
        )

        with pytest.raises(access.PartitionScopeConflict):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=ours
            )

        assert access.current_project_partition(web) == (ours, theirs)
    web_connection.rollback()


def test_a_second_principal_cannot_take_over_a_declared_transaction(
    runtime_database, member_of_both, web_connection
):
    """Scope is a person's, so a different person is a different scope.

    Without this a pooled connection that had begun one person's cross-project
    reading could be handed to another person's and answer with the first
    person's projects still sealed in, or replace them mid-unit-of-work.
    """

    ours, theirs = member_of_both
    other = HumanPrincipal("local:other")
    with runtime_database.session_factory.begin() as owner:
        owner.add(
            ProjectRosterEntry(
                project_id=theirs,
                principal_subject=other.subject,
                display_name="Other",
                active=True,
            )
        )

    with OrmSession(bind=web_connection) as web:
        access.open_member_project_partition(
            web, principal_subject=LEAVER.subject
        )

        with pytest.raises(access.PartitionScopeConflict):
            access.open_member_project_partition(
                web, principal_subject=other.subject
            )

        assert access.current_project_partition(web) == (ours, theirs)
    web_connection.rollback()


def test_a_new_transaction_is_the_boundary_for_changing_scope(
    member_of_both, web_connection
):
    """The rule is a boundary, not a prohibition (#662).

    Cross-project coordination reads the member partition; batch work takes
    one project per transaction. Both are available — what is refused is doing
    them inside the same unit of work, where an atomic read would span two.
    """

    ours, theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        assert [
            row.exact_text for row in web.scalars(select(SourceSegment)).all()
        ] == ["ours-UC-1"]
    web_connection.rollback()

    with OrmSession(bind=web_connection) as web:
        assert access.current_partition_declaration(web) is None

        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=theirs
        )

        assert [
            row.exact_text for row in web.scalars(select(SourceSegment)).all()
        ] == ["theirs-UC-1"]
    web_connection.rollback()


def test_closing_the_partition_does_not_release_the_declared_scope(
    member_of_both, web_connection
):
    """Being finished with a reading is not permission to start another one.

    If closing cleared the declaration, the guard would be one extra call to
    step around and would refuse nothing at all.
    """

    ours, theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        access.close_project_partition(web)
        assert access.current_project_partition(web) is None

        with pytest.raises(access.PartitionScopeConflict):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )
    web_connection.rollback()


def test_the_same_scope_can_be_taken_up_again_after_it_was_given_up(
    member_of_both, web_connection
):
    """Closing is reversible for the scope this transaction actually holds."""

    ours, _theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        access.close_project_partition(web)

        assert access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        ) == ours

        assert [
            row.exact_text for row in web.scalars(select(SourceSegment)).all()
        ] == ["ours-UC-1"]
    web_connection.rollback()


def test_a_hand_written_declaration_refuses_every_further_declaration(
    member_of_both, web_connection
):
    """The declaration is sealed, so tampering fails closed rather than open.

    The guard is a consistency rule and not a second forgery defence — a
    caller that clears the setting still has to pass the membership proof to
    obtain any scope. What the seal buys is that a *forged* declaration cannot
    be used to pretend the transaction already holds the scope it wants: an
    unverifiable declaration refuses everything after it.
    """

    ours, _theirs = member_of_both
    with OrmSession(bind=web_connection) as web:
        web.execute(
            text(
                "select set_config("
                "'corridor.project_partition_declaration', :value, true)"
            ),
            {"value": f"project:{LEAVER.subject}:{ours}"},
        )

        with pytest.raises(access.PartitionScopeConflict):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=ours
            )

        assert web.scalars(select(SourceSegment)).all() == []
    web_connection.rollback()


def test_the_web_capability_cannot_seal_a_declaration_of_its_own(
    two_projects, web_connection
):
    """Sealing a declaration is the half of the guard that asks no question."""

    with pytest.raises(ProgrammingError) as refused:
        web_connection.execute(
            text("select seal_partition_declaration('project:anyone:1')")
        )
    web_connection.rollback()
    assert "permission denied for function seal_partition_declaration" in str(
        refused.value
    )


def test_a_cross_project_reading_is_partitioned_by_active_membership(
    two_projects, web_connection
):
    """The #537 cross-project reading is not an exemption from the partition."""

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        scope = access.open_member_project_partition(
            web, principal_subject=LEAVER.subject
        )
        assert scope == (ours,)
        assert [row.exact_text for row in web.scalars(select(SourceSegment)).all()] == [
            "ours-UC-1"
        ]


def test_giving_up_the_partition_leaves_the_connection_reading_nothing(
    two_projects, web_connection
):
    """A unit of work that is finished with a project can say so and stop reading it."""

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        assert len(web.scalars(select(SourceSegment)).all()) == 1

        access.close_project_partition(web)

        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []


def test_offboarding_leaves_a_live_connection_with_the_empty_partition(
    runtime_database, two_projects, web_connection
):
    """Revocation proved by a refused read, not by a flag.

    The connection is the same one that read a row a moment ago. After the
    offboarding commits, the same query on the same connection returns nothing
    and the partition it may declare is empty.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        assert len(web.scalars(select(SourceSegment)).all()) == 1
    # The reading above is finished, and finishing it is a transaction
    # boundary: a declared scope belongs to one transaction (#662), so the
    # connection has to leave that one before it can declare anything again.
    web_connection.rollback()

    with runtime_database.session_factory.begin() as owner:
        access.deprovision_principal(owner, principal=LEAVER, operator=OPERATOR)

    with OrmSession(bind=web_connection) as web:
        assert (
            access.open_member_project_partition(
                web, principal_subject=LEAVER.subject
            )
            == ()
        )
        assert web.scalars(select(SourceSegment)).all() == []
    web_connection.rollback()

    with OrmSession(bind=web_connection) as web:
        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=ours
            )
    web_connection.rollback()


# --- Complete partition coverage (#657) ------------------------------------


def test_a_naked_select_on_every_partitioned_family_reads_one_project(
    their_record_rows, two_projects, web_connection
):
    """The careless query, family by family, not only on the four #531 covered.

    Each of these answered `select * from <table>` with both customers' rows
    an hour ago. The seam is the same as `source_segments`, so the proof is
    the same: no predicate, read as the deployed login, against committed rows.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        seen = {
            table: web.execute(
                text(f"select project_id from {table}")  # noqa: S608
            ).scalars().all()
            for table in sorted(their_record_rows)
        }

    assert seen == {table: [ours] for table in sorted(their_record_rows)}


def test_a_direct_id_lookup_does_not_reach_the_other_project(
    their_record_rows, two_projects, web_connection
):
    """The bypass a listing-shaped proof never finds.

    `select * from fact_decisions` looks project-shaped and invites a
    predicate; `where id = 41` does not, and every id-addressed surface in the
    web app is written that way. So the row is fetched by the id it really
    has, and the partition has to be what refuses it.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        reached = {
            table: web.execute(
                text(f"select id from {table} where id = :id"),  # noqa: S608
                {"id": row_id},
            ).scalar()
            for table, row_id in sorted(their_record_rows.items())
        }

    assert reached == {table: None for table in their_record_rows}


def test_the_accepted_record_view_reads_as_its_caller_not_as_its_owner(
    their_record_rows, two_projects, web_connection
):
    """A view over partitioned tables is not partitioned by itself.

    `current_project_record` is owned by the schema owner, and PostgreSQL
    evaluates row-level security inside a view as the *view's* owner. So the
    projection handed `corridor_web` every project's accepted record while
    `facts` and `fact_decisions` under it refused — the partition was real and
    the thing everybody reads through was not. `security_invoker` is what makes
    the view ask the question as the caller.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        rows = web.execute(
            text("select project_id from current_project_record")
        ).scalars().all()

    assert rows == [ours]


def test_a_cross_project_reading_of_every_family_stops_at_the_roster(
    runtime_database, their_record_rows, two_projects, web_connection
):
    """Member-project scope returns the authorized projects and no others."""

    ours, theirs = two_projects
    with runtime_database.session_factory.begin() as owner:
        third = Project(slug="third", name="Third", is_synthetic=True)
        owner.add(third)
        owner.flush()
        _seed_record_rows(owner, third.id, "third")
        owner.add(
            ProjectRosterEntry(
                project_id=theirs,
                principal_subject=LEAVER.subject,
                display_name="Leaver",
                active=True,
            )
        )

    with OrmSession(bind=web_connection) as web:
        scope = access.open_member_project_partition(
            web, principal_subject=LEAVER.subject
        )
        seen = {
            table: sorted(
                set(
                    web.execute(
                        text(f"select project_id from {table}")  # noqa: S608
                    ).scalars().all()
                )
            )
            for table in sorted(their_record_rows)
        }

    assert scope == (ours, theirs)
    assert seen == {table: [ours, theirs] for table in sorted(their_record_rows)}


# --- The classification, checked against the database it describes ---------


def _web_readable_relations(connection) -> set[str]:
    return set(
        connection.execute(
            text(
                "select c.relname from pg_class c "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' "
                "and c.relkind in ('r', 'v', 'm', 'p') "
                "and has_table_privilege('corridor_web', c.oid, 'SELECT')"
            )
        ).scalars()
    )


def _partition_policy_relations(connection) -> set[str]:
    return set(
        connection.execute(
            text(
                "select c.relname from pg_policy p "
                "join pg_class c on c.oid = p.polrelid "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' "
                "and p.polname like 'p\\_%\\_project\\_partition' "
                "and c.relrowsecurity"
            )
        ).scalars()
    )


def test_the_classification_answers_every_relation_the_login_can_read(
    runtime_database,
):
    """The static ratchet reads the models; this reads the database it built.

    A relation created by the migration and mapped by nothing — a view, a
    retired table — would slip past a models-only check, so the live schema is
    what is enumerated here.
    """

    with runtime_database.session_factory() as owner:
        readable = _web_readable_relations(owner.connection())

    unclassified = access.unclassified_relations(readable)

    assert unclassified == ()


def test_every_relation_classified_as_partitioned_really_carries_the_policy(
    runtime_database,
):
    """The list is a claim about PostgreSQL, so PostgreSQL is asked."""

    with runtime_database.session_factory() as owner:
        policied = _partition_policy_relations(owner.connection())

    assert sorted(access.PARTITIONED_RELATIONS - policied) == []


def test_no_relation_carries_the_policy_without_being_recorded_as_covered(
    runtime_database,
):
    """A policy nobody wrote down is coverage nobody can rely on or retire."""

    with runtime_database.session_factory() as owner:
        policied = _partition_policy_relations(owner.connection())

    assert sorted(policied - access.PARTITIONED_RELATIONS) == []


def test_the_relations_named_as_uncovered_really_are_uncovered(runtime_database):
    """An honest hole list stops being honest the moment a hole is filled.

    If a relation on the uncovered list gains a policy, the list is what is now
    wrong: it advertises a gap that no longer exists, and the ceiling that is
    supposed to fall with it never does.
    """

    with runtime_database.session_factory() as owner:
        policied = _partition_policy_relations(owner.connection())

    covered = sorted(set(access.NOT_YET_PARTITIONED_RELATIONS) & policied)

    assert covered == [], (
        "these relations carry the partition policy but are still recorded as "
        "not yet partitioned; move them to PARTITIONED_RELATIONS and lower "
        "NOT_YET_PARTITIONED_CEILING"
    )


# --- Offboarding over the real sign-in path --------------------------------


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    opened = Session(bind=connection)
    yield opened
    opened.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def sender():
    return auth.RecordingEmailSender()


@pytest.fixture
def client(session, sender):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    with TestClient(app, base_url="https://testserver") as opened:
        yield opened
    app.dependency_overrides.clear()


def _project(session, slug="alpha") -> Project:
    project = Project(slug=slug, name=slug.title(), is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _enroll(session, project, principal, email, designations):
    return access.enroll_member(
        session,
        project_id=project.id,
        email=email,
        principal=principal,
        display_name="Leaver",
        designations=designations,
        operator=OPERATOR,
    )


def _sign_in(client, sender, email):
    response = client.post(
        "/sign-in/request", data={"email": email}, follow_redirects=False
    )
    assert response.status_code == 200
    token = parse_qs(urlsplit(sender.sent[-1][1]).query)["token"][0]
    consume = client.get(
        "/sign-in/consume", params={"token": token}, follow_redirects=False
    )
    assert consume.status_code == 303
    return consume


def test_the_next_request_after_offboarding_is_refused(session, client, sender):
    """The proof the criterion asks for: a real route, a real cookie, refused.

    The person is signed in and reading their project one request before the
    offboarding and refused one request after it, on the same cookie.
    """

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    _sign_in(client, sender, "leaver@example.com")

    before = client.get(f"/work/{project.slug}", follow_redirects=False)
    assert before.status_code == 200

    access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)

    after = client.get(f"/work/{project.slug}", follow_redirects=False)
    assert after.status_code == 401


def test_a_link_already_in_the_inbox_cannot_open_a_session_afterwards(
    session, client, sender
):
    """Deactivating the roster is not enough while an unspent link is still live."""

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    requested = client.post(
        "/sign-in/request",
        data={"email": "leaver@example.com"},
        follow_redirects=False,
    )
    assert requested.status_code == 200
    token = parse_qs(urlsplit(sender.sent[-1][1]).query)["token"][0]

    result = access.deprovision_principal(
        session, principal=LEAVER, operator=OPERATOR
    )
    assert result.tokens_invalidated == 1

    consumed = client.get(
        "/sign-in/consume", params={"token": token}, follow_redirects=False
    )
    assert consumed.status_code != 303
    assert client.cookies.get(auth.SESSION_COOKIE) is None
    assert (
        client.get(f"/work/{project.slug}", follow_redirects=False).status_code == 401
    )


def test_offboarding_deactivates_every_membership_and_strips_every_designation(
    session,
):
    """A later re-enrollment has to grant each authority again, not revive it."""

    first = _project(session, "one")
    second = _project(session, "two")
    _enroll(session, first, LEAVER, "leaver@example.com", access.DESIGNATIONS)
    _enroll(session, second, LEAVER, "leaver@example.com", [access.EXTERNAL_RELEASE])

    result = access.deprovision_principal(
        session, principal=LEAVER, operator=OPERATOR
    )

    assert result.projects_left == (first.id, second.id)
    entries = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.principal_subject == LEAVER.subject
        )
    ).all()
    assert [entry.active for entry in entries] == [False, False]
    assert not any(
        entry.can_coordinate
        or entry.can_review_documentation
        or entry.can_release_externally
        or entry.is_technical_operator
        for entry in entries
    )
    assert access.resolve_membership(session, LEAVER.subject, first.id) is None
    assert access.member_projects(session, LEAVER.subject) == []
    assert access.coordinated_projects(session, LEAVER.subject) == []


def test_offboarding_keeps_the_person_nameable_on_what_they_decided(session):
    """Access ends; authorship does not (ADR-0081, #503).

    An export that could no longer say who made an accepted decision would be a
    worse record, not a safer one.
    """

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    decided = audit.record(
        session,
        principal=LEAVER,
        action=audit.SET_NEXT_ACTION,
        entity_type=audit.PROJECT,
        entity_id=project.id,
        after={"next_action": "call the party"},
    )

    access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)

    identity = session.scalars(
        select(PersonIdentity).where(
            PersonIdentity.principal_subject == LEAVER.subject
        )
    ).first()
    assert identity is not None
    assert identity.email_normalized == "leaver@example.com"
    assert session.get(AuditLog, decided.id).human_principal == LEAVER.subject


def test_a_second_offboarding_revokes_nothing_a_second_time(session, client, sender):
    """Idempotent: the act is recorded, the revocations are not invented twice."""

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    _sign_in(client, sender, "leaver@example.com")

    first = access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)
    second = access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)

    assert first.sessions_revoked == 1
    assert second == access.Deprovisioning(
        principal_subject=LEAVER.subject,
        email_normalized="leaver@example.com",
        projects_left=(),
        sessions_revoked=0,
        tokens_invalidated=0,
    )


def test_every_live_session_of_one_person_is_revoked_together(session):
    """Two browsers is two sessions; offboarding one of them would be no offboarding."""

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    laptop = access.create_web_session(
        session, principal=LEAVER, email_normalized="leaver@example.com"
    )
    phone = access.create_web_session(
        session, principal=LEAVER, email_normalized="leaver@example.com"
    )

    result = access.deprovision_principal(
        session, principal=LEAVER, operator=OPERATOR
    )

    assert result.sessions_revoked == 2
    assert access.resolve_web_session(session, laptop.raw_session_id) is None
    assert access.resolve_web_session(session, phone.raw_session_id) is None
    assert (
        session.scalars(
            select(WebSession).where(
                WebSession.principal_subject == LEAVER.subject,
                WebSession.revoked_at.is_(None),
            )
        ).all()
        == []
    )


def test_a_lapsed_or_spent_link_is_not_counted_as_an_invalidated_one(session):
    """The receipt says what was taken away, not what had already gone."""

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    moment = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    session.add(
        SignInToken(
            email_normalized="leaver@example.com",
            token_sha256="1" * 64,
            created_at=moment - timedelta(hours=1),
            expires_at=moment - timedelta(minutes=1),
        )
    )
    session.add(
        SignInToken(
            email_normalized="leaver@example.com",
            token_sha256="2" * 64,
            created_at=moment - timedelta(minutes=5),
            expires_at=moment + timedelta(minutes=10),
            consumed_at=moment - timedelta(minutes=4),
        )
    )
    session.add(
        SignInToken(
            email_normalized="leaver@example.com",
            token_sha256="3" * 64,
            created_at=moment - timedelta(minutes=2),
            expires_at=moment + timedelta(minutes=13),
        )
    )
    session.flush()

    result = access.deprovision_principal(
        session, principal=LEAVER, operator=OPERATOR, now=moment
    )

    assert result.tokens_invalidated == 1
    spent = {
        token.token_sha256: token.consumed_at
        for token in session.scalars(select(SignInToken)).all()
    }
    # The lapsed link is left exactly as it lapsed, the already-spent one keeps
    # the moment it was spent, and only the live one is taken away now.
    assert spent["1" * 64] is None
    assert spent["2" * 64] == moment - timedelta(minutes=4)
    assert spent["3" * 64] == moment


# --- The identity and authorization export ---------------------------------


def _watermark(session) -> int:
    """The last audit id before a test's own acts, so the export is read forward."""

    return int(session.scalar(select(AuditLog.id).order_by(AuditLog.id.desc())) or 0)


def test_the_export_carries_every_access_act_in_recorded_order(
    session, client, sender
):
    """Enrollment, sign-in, sign-out, and offboarding, ordered by the append-only id."""

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    _sign_in(client, sender, "leaver@example.com")
    client.post(
        "/sign-out",
        headers={auth.CSRF_HEADER: client.cookies.get(auth.CSRF_COOKIE)},
        follow_redirects=False,
    )
    access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)

    events = identity_audit.identity_events(session, after_id=watermark)

    assert [event.action for event in events] == [
        audit.ENROLL_PROJECT_MEMBER,
        audit.SIGN_IN,
        audit.SIGN_OUT,
        audit.DEPROVISION_PROJECT_MEMBER,
        audit.DEPROVISION_PRINCIPAL,
    ]
    assert [event.id for event in events] == sorted(event.id for event in events)
    offboarding = events[-1]
    assert offboarding.actor == OPERATOR.subject
    assert offboarding.after["principal_subject"] == LEAVER.subject
    # Nothing was left to revoke: the sign-out above already gave the session
    # up, and offboarding reports what it actually took, not what it would have.
    assert offboarding.after["sessions_revoked"] == 0
    assert offboarding.after["identity_binding_retained"] is True


def test_the_export_resumes_exactly_where_the_previous_one_ended(session):
    """Consecutive exports abut: the id watermark, never a clock reading."""

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    first = identity_audit.identity_events(session, after_id=watermark)
    assert len(first) == 1

    access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)
    resumed = identity_audit.identity_events(session, after_id=first[-1].id)

    assert [event.action for event in resumed] == [
        audit.DEPROVISION_PROJECT_MEMBER,
        audit.DEPROVISION_PRINCIPAL,
    ]


def test_the_export_leaves_the_coordination_history_where_it_is(session):
    """An access review is not a reason to hand over the whole Ledger."""

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    audit.record(
        session,
        principal=LEAVER,
        action=audit.SET_NEXT_ACTION,
        entity_type=audit.PROJECT,
        entity_id=project.id,
        after={"next_action": "call the party"},
    )

    events = identity_audit.identity_events(session, after_id=watermark)

    assert [event.action for event in events] == [audit.ENROLL_PROJECT_MEMBER]


def test_both_export_formats_carry_the_same_acts(session):
    """CSV keeps the flat columns usable and the detail lossless, as JSON text."""

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    events = identity_audit.identity_events(session, after_id=watermark)

    as_json = identity_audit.export_json(events)
    as_csv = identity_audit.export_csv(events)

    assert LEAVER.subject in as_json
    header, row, *rest = as_csv.strip().splitlines()
    assert header.split(",")[:5] == [
        "id",
        "action",
        "actor",
        "human_principal",
        "entity_type",
    ]
    assert rest == []
    assert audit.ENROLL_PROJECT_MEMBER in row
    assert "documentation_review" not in row


def test_an_unknown_export_format_is_refused(session):
    with pytest.raises(ValueError):
        identity_audit.export(session, fmt="xlsx")


def test_the_export_adapter_prints_what_the_reader_returns(session, capsys):
    """The export is a file someone can actually produce, not only a function."""

    from corridor import identity_audit_cli

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])

    identity_audit_cli.main(
        ["--format=csv", f"--after-id={watermark}"],
        session_factory=lambda: session,
    )

    printed = capsys.readouterr().out
    assert printed.startswith("id,action,actor,")
    assert audit.ENROLL_PROJECT_MEMBER in printed
