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

**A seal belongs to one transaction (#676).** Sealing the scope was not enough
on its own: the web login can read both the scope setting and its seal, and
before #676 it could replay that genuine pair with ``set_config`` in a later
transaction on the same pooled connection and be believed. So the replay tests
capture what a legitimate reading left behind, commit, and try it again on the
same physical connection — including after the principal has been offboarded,
which is the guarantee the replay actually defeats.

**Revocation.** "Tests proving revoked access" cannot mean asserting that
``active`` became false. So the offboarding tests drive the real HTTP routes
with the cookie a real magic link established, offboard the person, and assert
the very next request is refused — and that a link already sitting in their
inbox can no longer open a session.

No test here reads the wall clock: ordering is by ``audit_log.id``, the
append-only watermark, and every moment a test needs is passed in.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import NullPool, QueuePool

from corridor import access, audit, identity_audit, web_boundary
from corridor.config import settings
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
from corridor.object_storage import LocalFilesystemStore
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import (
    app,
    get_content_store,
    get_machine_session,
    get_session,
    get_web_capability,
)

from browser_session_support import form_fields, submit_form
from later_revision_support import BASELINE_ROWS, workbook_bytes

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


# --- A seal belongs to one transaction (#676) ------------------------------

# Everything a legitimate declaration leaves on the connection, and therefore
# everything a replay has to work with. The web login may read all four; that
# is not the defect and no privilege is added to stop it.
SCOPE_SETTINGS = (
    "corridor.project_partition",
    "corridor.project_partition_seal",
)
DECLARATION_SETTINGS = (
    "corridor.project_partition_declaration",
    "corridor.project_partition_declaration_seal",
)


def _capture(web, names) -> dict[str, str]:
    """Read the named settings exactly as the deployed login can."""

    captured = {
        name: web.scalar(text("select current_setting(:name, true)"), {"name": name})
        for name in names
    }
    assert all(value for value in captured.values()), (
        f"nothing was captured to replay, so the test would prove nothing: {captured}"
    )
    return captured


def _replay(web, captured: dict[str, str]) -> None:
    """Put the genuine values back, by hand, in whatever transaction we are in."""

    for name, value in captured.items():
        web.execute(
            text("select set_config(:name, :value, true)"),
            {"name": name, "value": value},
        )


def _declaration_refused(web) -> str:
    """Read the declaration, requiring it to fail closed, and return the SQLSTATE."""

    with pytest.raises(DBAPIError) as refused:
        access.current_partition_declaration(web)
    assert "not one the database sealed" in str(refused.value)
    return str(getattr(refused.value.orig, "sqlstate", ""))


@pytest.fixture
def pooled_web_engine(runtime_database):
    """The deployed login behind a real pool, so one connection outlives one transaction.

    #676 crosses a transaction boundary while keeping the physical connection,
    which is exactly what a pooled deployment does between two requests.
    ``pool_size=1`` with no overflow makes that reuse certain rather than
    likely, and every test below asserts ``pg_backend_pid()`` is unchanged, so
    a pool that quietly opened a second connection cannot let a replay test
    pass for the wrong reason.
    """

    web_engine = create_engine(
        _web_url(runtime_database.name),
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        future=True,
    )
    yield web_engine
    web_engine.dispose()


def _open_and_capture(pooled_web_engine, project_id: int) -> tuple[int, dict[str, str]]:
    """One legitimate reading of one project, committed, with what it left behind."""

    with OrmSession(bind=pooled_web_engine) as web:
        backend_pid = web.scalar(text("select pg_backend_pid()"))
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=project_id
        )
        captured = _capture(web, SCOPE_SETTINGS + DECLARATION_SETTINGS)
        assert [
            row.exact_text for row in web.scalars(select(SourceSegment)).all()
        ] == ["ours-UC-1"]
        web.commit()
    return backend_pid, captured


def test_opening_a_partition_assigns_a_transaction_id(two_projects, web_connection):
    """The accepted cost of #676, asserted rather than assumed.

    Binding a seal to ``pg_current_xact_id()`` means a request that only reads
    stops being id-less. That is the documented price of the boundary, so it is
    recorded as a fact of the seam and not left to a comment nobody checks.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        assert web.scalar(text("select pg_current_xact_id_if_assigned()")) is None

        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )

        assert web.scalar(text("select pg_current_xact_id_if_assigned()")) is not None
    web_connection.rollback()


def test_a_genuine_scope_and_declaration_replayed_later_reads_nothing(
    two_projects, pooled_web_engine
):
    """The defect, closed: the honest pair is worth nothing in the next transaction.

    Nothing here is forged. Every value was minted by the database for a
    principal who was entitled to it, on this very connection, a moment ago.
    What the connection no longer has is the transaction that earned them.
    """

    ours, _theirs = two_projects
    backend_pid, captured = _open_and_capture(pooled_web_engine, ours)

    with OrmSession(bind=pooled_web_engine) as web:
        assert web.scalar(text("select pg_backend_pid()")) == backend_pid
        _replay(web, captured)

        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []
        assert _declaration_refused(web) == "25000"
        web.rollback()


def test_replay_still_fails_after_the_principal_is_offboarded(
    runtime_database, two_projects, pooled_web_engine
):
    """The guarantee the replay actually defeated, restored.

    ``deprovision_principal`` is the whole offboarding act, and before #676 it
    left every connection that had ever read a project able to read it again by
    putting four settings back. Revoking after the reading, rather than before,
    is the point: the values are genuine and the membership behind them is gone.
    """

    ours, _theirs = two_projects
    backend_pid, captured = _open_and_capture(pooled_web_engine, ours)

    with runtime_database.session_factory.begin() as owner:
        access.deprovision_principal(owner, principal=LEAVER, operator=OPERATOR)

    with OrmSession(bind=pooled_web_engine) as web:
        assert web.scalar(text("select pg_backend_pid()")) == backend_pid
        _replay(web, captured)

        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []
        assert _declaration_refused(web) == "25000"
        web.rollback()

    with OrmSession(bind=pooled_web_engine) as web:
        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=ours
            )
        web.rollback()


def test_replaying_only_the_scope_pair_buys_no_rows(two_projects, pooled_web_engine):
    """The two seals are independent, so each has to refuse on its own.

    Replaying the scope without the declaration is the cheaper attack: it needs
    no declaration at all, only the setting row-level security actually reads.
    """

    ours, _theirs = two_projects
    backend_pid, captured = _open_and_capture(pooled_web_engine, ours)

    with OrmSession(bind=pooled_web_engine) as web:
        assert web.scalar(text("select pg_backend_pid()")) == backend_pid
        _replay(web, {name: captured[name] for name in SCOPE_SETTINGS})

        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []
        # Nothing was declared, so the declaration reads as the empty one
        # rather than refusing: this attempt never touched that half.
        assert access.current_partition_declaration(web) is None
        web.rollback()


def test_replaying_only_the_declaration_pair_refuses_every_declaration(
    member_of_both, pooled_web_engine
):
    """And the other half, alone, fails closed instead of opening the gate.

    The principal is still a member here, so the declaration this replays is
    one the database would grant on request. It is refused anyway, because an
    unverifiable declaration refuses everything after it (#657) and a
    declaration from a finished transaction is unverifiable (#676).
    """

    ours, _theirs = member_of_both
    backend_pid, captured = _open_and_capture(pooled_web_engine, ours)

    with OrmSession(bind=pooled_web_engine) as web:
        assert web.scalar(text("select pg_backend_pid()")) == backend_pid
        _replay(web, {name: captured[name] for name in DECLARATION_SETTINGS})

        assert access.current_project_partition(web) is None

        with pytest.raises(access.PartitionScopeConflict):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=ours
            )
        assert web.scalars(select(SourceSegment)).all() == []

        # Last, because this one raises outside a savepoint and so leaves the
        # transaction aborted: refusing a declaration is the database's answer,
        # not a recoverable one the way a refused *declaration attempt* is.
        assert _declaration_refused(web) == "25000"
        web.rollback()


def test_a_genuine_member_reading_replayed_later_reads_nothing(
    runtime_database, two_projects, pooled_web_engine
):
    """The cross-project partition is sealed by the same rule, so it replays no better.

    Both partitions have to be covered or the weaker one is the way in: the
    member reading is the scope an offboarded person's connection is meant to
    end up holding, empty.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=pooled_web_engine) as web:
        backend_pid = web.scalar(text("select pg_backend_pid()"))
        assert access.open_member_project_partition(
            web, principal_subject=LEAVER.subject
        ) == (ours,)
        captured = _capture(web, SCOPE_SETTINGS + DECLARATION_SETTINGS)
        assert len(web.scalars(select(SourceSegment)).all()) == 1
        web.commit()

    with runtime_database.session_factory.begin() as owner:
        access.deprovision_principal(owner, principal=LEAVER, operator=OPERATOR)

    with OrmSession(bind=pooled_web_engine) as web:
        assert web.scalar(text("select pg_backend_pid()")) == backend_pid
        _replay(web, captured)

        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []
        assert _declaration_refused(web) == "25000"
        web.rollback()


def test_a_replay_that_arms_a_transaction_id_of_its_own_still_reads_nothing(
    runtime_database, two_projects, pooled_web_engine
):
    """The replay that the fail-closed check alone does not catch.

    Refusing a transaction that has *no* id assigned is the cheap half of #676,
    and it is the half a replay usually meets, because a reading request never
    assigns one by itself. But the web login can assign one whenever it likes —
    ``select pg_current_xact_id()``, or any write — and a seal that merely
    required *some* id would then verify a replayed pair against a transaction
    that never earned it.

    So this replays against an armed transaction, which is the case that can
    only fail because the id is *in the sealed material*. Both seals are
    replayed, and the principal is offboarded first, so nothing but the
    binding is left to refuse it.
    """

    ours, _theirs = two_projects
    backend_pid, captured = _open_and_capture(pooled_web_engine, ours)

    with runtime_database.session_factory.begin() as owner:
        access.deprovision_principal(owner, principal=LEAVER, operator=OPERATOR)

    with OrmSession(bind=pooled_web_engine) as web:
        assert web.scalar(text("select pg_backend_pid()")) == backend_pid
        armed = web.scalar(text("select pg_current_xact_id()"))
        assert web.scalar(text("select pg_current_xact_id_if_assigned()")) == armed
        _replay(web, captured)

        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []
        assert _declaration_refused(web) == "25000"
        web.rollback()


def test_a_transaction_may_restore_its_own_settings_by_hand(
    two_projects, web_connection
):
    """What is refused is the *later* transaction, not `set_config` itself.

    Without this the replay tests above would pass just as well if the seal had
    been broken outright, or if any hand-written setting poisoned the scope
    forever. Here the same values go back on the same transaction that earned
    them, and the reading resumes — so the refusals above are about transaction
    identity and nothing else.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        captured = _capture(web, SCOPE_SETTINGS + DECLARATION_SETTINGS)

        access.close_project_partition(web)
        assert access.current_project_partition(web) is None

        _replay(web, captured)

        assert access.current_project_partition(web) == (ours,)
        assert (
            access.current_partition_declaration(web)
            == f"project:{LEAVER.subject}:{ours}"
        )
        assert [
            row.exact_text for row in web.scalars(select(SourceSegment)).all()
        ] == ["ours-UC-1"]
    web_connection.rollback()


def test_redeclaring_the_same_scope_seals_it_to_the_same_transaction(
    two_projects, web_connection
):
    """Idempotence survives the binding: the same scope, sealed twice, is one seal.

    If the seal moved between two declarations of the same scope inside one
    transaction — as it would over a clock reading — #657's idempotence would
    hold only by accident of which seal was read last.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        first = _capture(web, SCOPE_SETTINGS + DECLARATION_SETTINGS)

        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )

        assert _capture(web, SCOPE_SETTINGS + DECLARATION_SETTINGS) == first
        assert access.current_project_partition(web) == (ours,)
    web_connection.rollback()


def test_a_refusal_before_any_scope_does_not_spoil_the_scope_that_follows(
    two_projects, web_connection
):
    """The transaction id an aborted savepoint assigned is still this transaction's.

    ``open_project_partition`` proves membership by raising inside a savepoint
    (#654). A refusal can therefore be the thing that first assigns the
    transaction id, and the savepoint that aborts does not hand a *top-level*
    id back. If the seal had been bound to anything a subtransaction owns, the
    declaration after this refusal would seal against an id the verifier could
    no longer see.
    """

    ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )

        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )

        assert access.current_project_partition(web) == (ours,)
        assert [
            row.exact_text for row in web.scalars(select(SourceSegment)).all()
        ] == ["ours-UC-1"]
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


# --- The live-pilot web capability boundary (#680) -------------------------
#
# #657 proved the partition on 51 relations and recorded 130 more as
# unpartitioned and still directly selectable. #680 revoked every one of those
# from `corridor_web` and partitioned the four an enabled pilot route actually
# reads. Both halves are proved here the way the rest of this file proves
# things: as the deployed login, against committed rows, with the query a
# careless reader would write.


PILOT_PARTITIONED_RELATIONS = (
    "documents",
    "external_report_artifacts",
    "external_report_releases",
    "processing_artifacts",
    "source_deliveries",
)

# #824's second family, and the reason it is a second one: these carry no
# `project_id` of their own. Each row belongs to exactly one `documents` row,
# `documents` is partitioned above, so the policy asks the parent instead --
# "is the document this page belongs to in the caller's partition". The
# assertions below are the same two the family above gets, written against
# each relation's own key because there is no project column to select.
PILOT_DOCUMENT_CHILD_RELATIONS = (
    "doc_pages",
    "document_quarantines",
    "extraction_runs",
    "page_render_derivatives",
    "token_layers",
)

#: The column that names one row of each, because one of the five is keyed by
#: the document it belongs to rather than by a surrogate of its own.
PILOT_DOCUMENT_CHILD_KEYS = {
    relation: "document_id" if relation == "document_quarantines" else "id"
    for relation in PILOT_DOCUMENT_CHILD_RELATIONS
}

# Every table privilege PostgreSQL can grant. A capability denied SELECT and
# left holding INSERT is not denied the relation, which is why the assertion
# below is about all of them rather than about reading.
_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)

_MOMENT = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _seed_pilot_partitioned_rows(owner, project_id: int, slug: str) -> dict[str, int]:
    """One committed row per newly partitioned relation, named after its project."""

    ids: dict[str, int] = {}
    # `delivery_identity` and `idempotency_key` are derived, and a trigger
    # refuses a delivery that supplies anything else, so the insert derives
    # them the same way rather than restating two digests by hand.
    ids["source_deliveries"] = owner.execute(
        text(
            "with bound as ("
            "  select encode(sha256(convert_to(concat_ws(':', "
            "    cast(:slug as text), p.slug, 'mailbox', cast(:slug as text), "
            "    '1'), 'UTF8')), 'hex') as identity"
            "    from projects p where p.id = :project_id"
            ") "
            "insert into source_deliveries ("
            "customer, project_id, channel, external_identity, external_version, "
            "content_sha256, bytes_reference, delivery_identity, idempotency_key, "
            "received_at, transport, configuration_identity, service_identity, "
            "run_identity, disposition"
            ") select cast(:slug as text), :project_id, 'mailbox', "
            "cast(:slug as text), '1', cast(:digest as varchar(64)), "
            "cast(:slug as text), bound.identity, "
            "encode(sha256(convert_to(concat_ws(':', bound.identity, "
            "cast(:digest as text)), 'UTF8')), 'hex'), "
            ":moment, 'pull', cast(:slug as text), cast(:slug as text), "
            "cast(:slug as text), 'stored' from bound returning id"
        ),
        {
            "slug": slug,
            "project_id": project_id,
            "digest": sha256(slug.encode("utf-8")).hexdigest(),
            "moment": _MOMENT,
        },
    ).scalar_one()
    ids["external_report_artifacts"] = owner.execute(
        text(
            "insert into external_report_artifacts ("
            "project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
            "evaluated_on, ruleset_version, evaluation_context_json, "
            "provenance_mode, record_context_json, rendered_at"
            ") values ("
            ":project_id, :slug, 'pdf', :body, :digest, :evaluated_on, 'v1', "
            "'{}'::jsonb, 'document-only', '{}'::jsonb, :moment"
            ") returning id"
        ),
        {
            "project_id": project_id,
            "slug": slug,
            "body": f"%PDF-1.7 {slug}".encode("utf-8"),
            "digest": sha256(slug.encode("utf-8")).hexdigest(),
            "evaluated_on": date(2026, 6, 1),
            "moment": _MOMENT,
        },
    ).scalar_one()
    ids["external_report_releases"] = owner.execute(
        text(
            "insert into external_report_releases ("
            "project_id, artifact_id, artifact_name, format, pdf_sha256, "
            "evaluated_on, ruleset_version, provenance_mode, released_by, "
            "released_by_display, released_at, content_storage"
            ") values ("
            ":project_id, :artifact_id, :slug, 'pdf', :digest, :evaluated_on, "
            "'v1', 'document-only', 'local:operations', 'Operations', "
            ":moment, 'artifact'"
            ") returning id"
        ),
        {
            "project_id": project_id,
            "artifact_id": ids["external_report_artifacts"],
            "slug": slug,
            "digest": sha256(slug.encode("utf-8")).hexdigest(),
            "evaluated_on": date(2026, 6, 1),
            "moment": _MOMENT,
        },
    ).scalar_one()
    ids["documents"] = owner.execute(
        text("select id from documents where project_id = :project_id"),
        {"project_id": project_id},
    ).scalar_one()
    ids["processing_artifacts"] = owner.execute(
        text(
            "insert into processing_artifacts ("
            "project_id, kind, storage_path, content_sha256, terminal_at, "
            "retention_class"
            ") values (:project_id, 'raw_ocr', cast(:slug as text), "
            "cast(:digest as varchar(64)), :moment, 'class_b')"
            " returning id"
        ),
        {
            "project_id": project_id,
            "slug": slug,
            "digest": sha256(slug.encode("utf-8")).hexdigest(),
            "moment": _MOMENT,
        },
    ).scalar_one()
    # #824's document children, one row each, named after the project whose
    # document they hang off. Nothing here carries a `project_id`: that is the
    # point, and the partition has to reach it through `documents`.
    document = ids["documents"]
    digest = sha256(slug.encode("utf-8")).hexdigest()
    ids["doc_pages"] = owner.execute(
        text(
            "insert into doc_pages (document_id, page_no, text) "
            "values (:document_id, 1, cast(:slug as text)) returning id"
        ),
        {"document_id": document, "slug": slug},
    ).scalar_one()
    ids["document_quarantines"] = owner.execute(
        text(
            "insert into document_quarantines (document_id, reason) "
            "values (:document_id, cast(:slug as text)) returning document_id"
        ),
        {"document_id": document, "slug": slug},
    ).scalar_one()
    ids["extraction_runs"] = owner.execute(
        text(
            "insert into extraction_runs ("
            "document_id, prompt_version, candidate_count, outcome"
            ") values (:document_id, 'partition_fixture_v1', 0, 'unreadable')"
            " returning id"
        ),
        {"document_id": document},
    ).scalar_one()
    ids["page_render_derivatives"] = owner.execute(
        text(
            "insert into page_render_derivatives ("
            "document_id, page_number, derivative_key, profile_name, "
            "profile_id, source_sha256, artifact_path, artifact_sha256, "
            "artifact_bytes, manifest_json"
            ") values (:document_id, 1, cast(:slug as varchar(64)), "
            "'review', cast(:slug as varchar(64)), "
            "cast(:digest as varchar(64)), cast(:slug as text), "
            "cast(:digest as varchar(64)), 1, '{}'::jsonb) returning id"
        ),
        {"document_id": document, "slug": slug, "digest": digest},
    ).scalar_one()
    ids["token_layers"] = owner.execute(
        text(
            "insert into token_layers ("
            "document_id, page_no, origin, layer_key, source_sha256, "
            "engine_json, token_count, quality_json, artifact_path, "
            "artifact_sha256, artifact_bytes"
            ") values (:document_id, 1, 'native', "
            "cast(:slug as varchar(64)), cast(:digest as varchar(64)), "
            "'{}'::jsonb, 0, '{}'::jsonb, cast(:slug as text), "
            "cast(:digest as varchar(64)), 1) returning id"
        ),
        {"document_id": document, "slug": slug, "digest": digest},
    ).scalar_one()
    return ids


@pytest.fixture
def their_pilot_rows(runtime_database, two_projects):
    """A committed row of the other project in each newly partitioned relation."""

    ours, theirs = two_projects
    with runtime_database.session_factory.begin() as owner:
        _seed_pilot_partitioned_rows(owner, ours, "ours")
        return _seed_pilot_partitioned_rows(owner, theirs, "theirs")


def test_the_live_pilot_login_holds_no_privilege_on_a_denied_relation(
    runtime_database,
):
    """Not "cannot read": holds nothing at all.

    A revoke aimed only at SELECT would leave the web capability able to
    insert into, update and delete from relations it may not look at, which is
    a worse boundary than none because it reads as closed.
    """

    with runtime_database.session_factory() as owner:
        held = owner.execute(
            text(
                "select c.relname, p from pg_class c "
                "join pg_namespace n on n.oid = c.relnamespace, "
                "unnest(cast(:privileges as text[])) p "
                "where n.nspname = 'public' and c.relkind = 'r' "
                "and c.relname = any(cast(:denied as text[])) "
                "and has_table_privilege('corridor_web', c.oid, p) "
                "order by c.relname, p"
            ),
            {
                "privileges": list(_TABLE_PRIVILEGES),
                "denied": sorted(web_boundary.DENIED_RELATIONS),
            },
        ).all()

    assert held == [], (
        "the live-pilot web capability still holds privileges on relations the "
        "boundary denies it; #680 revokes them in the migration"
    )


def test_the_web_capability_reads_the_parse_output_and_can_no_longer_write_it(
    runtime_database,
):
    """#893's narrower revoke, asked of PostgreSQL rather than of the list.

    #824 left the schema owner's default select/insert/update/delete standing
    on these four because the confirmation route rendered and parsed the
    uploaded file inside the web request and therefore wrote every one of them.
    The read is the standing pass's now, so the writes go and the reading
    stays: a human review surface still shows a page and a render, and the
    partition still decides whose.
    """

    with runtime_database.session_factory() as owner:
        held = {
            (relation, privilege)
            for relation, privilege in owner.execute(
                text(
                    "select c.relname, p from pg_class c "
                    "join pg_namespace n on n.oid = c.relnamespace, "
                    "unnest(cast(:privileges as text[])) p "
                    "where n.nspname = 'public' and c.relkind = 'r' "
                    "and c.relname = any(cast(:relations as text[])) "
                    "and has_table_privilege('corridor_web', c.oid, p) "
                    "order by c.relname, p"
                ),
                {
                    "privileges": list(_TABLE_PRIVILEGES),
                    "relations": sorted(web_boundary.WRITE_DENIED_RELATIONS),
                },
            ).all()
        }

    assert held == {
        (relation, "SELECT") for relation in web_boundary.WRITE_DENIED_RELATIONS
    }, (
        "the human web capability should hold exactly SELECT on the relations "
        "the parse used to write in the request, and nothing else"
    )


def test_the_web_capability_cannot_write_a_page_even_inside_its_own_partition(
    their_pilot_rows, two_projects, web_connection
):
    """The privilege, not the policy, is what refuses it (#893).

    Row-level security would have let this insert through: the document named
    is the caller's own, so the partition predicate is satisfied. Refusing it
    is the grant's doing, which is the whole point -- "these are my project's
    rows" was never a reason a web request should be able to rewrite the text
    a citation is replayed against.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        document_id = web.execute(
            text("select id from documents where project_id = :ours"), {"ours": ours}
        ).scalar_one()
        savepoint = web_connection.begin_nested()
        with pytest.raises(ProgrammingError) as refused:
            web.execute(
                text(
                    "insert into doc_pages (document_id, page_no, text) "
                    "values (:document_id, 99, 'rewritten')"
                ),
                {"document_id": document_id},
            )
        savepoint.rollback()

    assert "permission denied" in str(refused.value).lower()


def test_a_naked_select_on_every_denied_relation_is_refused(
    two_projects, web_connection
):
    """The catalog claim, made the way this file makes claims: by asking.

    `has_table_privilege` returning false and `select * from work_decisions`
    raising are two different statements, and only the second is what a reader
    meets. A grant to PUBLIC is exactly the case where the two come apart —
    `subject_resolution_decisions` carried one, so the first draft of the
    revoke left it readable while every list said otherwise.
    """

    served: list[str] = []
    for relation in sorted(web_boundary.DENIED_RELATIONS):
        savepoint = web_connection.begin_nested()
        try:
            web_connection.execute(text(f"select * from {relation} limit 1"))
        except ProgrammingError:
            savepoint.rollback()
        else:
            savepoint.rollback()
            served.append(relation)

    assert served == [], (
        "the live-pilot web capability can still select from these relations"
    )


def test_a_naked_select_on_the_newly_partitioned_relations_reads_one_project(
    their_pilot_rows, two_projects, web_connection
):
    """The four relations an enabled pilot route reads, with no predicate at all."""

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        seen = {
            relation: web.execute(
                text(f"select project_id from {relation}")
            ).scalars().all()
            for relation in PILOT_PARTITIONED_RELATIONS
        }

    assert seen == {relation: [ours] for relation in PILOT_PARTITIONED_RELATIONS}


def test_a_direct_id_lookup_on_the_newly_partitioned_relations_is_empty(
    their_pilot_rows, two_projects, web_connection
):
    """A direct id is how a partition gets bypassed when only lists were tried.

    The ids here are the *other* project's committed rows, so a partition that
    only filters list queries answers every one of them.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        reached = {
            relation: web.execute(
                text(f"select id from {relation} where id = :id"),
                {"id": their_pilot_rows[relation]},
            ).scalars().all()
            for relation in PILOT_PARTITIONED_RELATIONS
        }

    assert reached == {relation: [] for relation in PILOT_PARTITIONED_RELATIONS}


def test_the_newly_partitioned_relations_read_nothing_without_a_partition(
    their_pilot_rows, web_connection
):
    """Fail closed, exactly as the spine relations do."""

    with OrmSession(bind=web_connection) as web:
        assert access.current_project_partition(web) is None
        for relation in PILOT_PARTITIONED_RELATIONS:
            assert web.execute(text(f"select id from {relation}")).scalars().all() == []


def test_a_naked_select_on_a_document_child_reads_one_project(
    their_pilot_rows, two_projects, web_connection
):
    """#824's parent-join family: a page belongs to whoever owns its document.

    These five carry no `project_id`, and #680 read that as a reason a policy
    could not be written. It is not: the policy tests the parent each row
    already names, so a careless `select * from doc_pages` answers with the
    caller's project and nobody else's.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        seen = {
            relation: web.execute(
                text(
                    f"select d.project_id from {relation} child "
                    "join documents d on d.id = child.document_id"
                )
            ).scalars().all()
            for relation in PILOT_DOCUMENT_CHILD_RELATIONS
        }

    assert seen == {relation: [ours] for relation in PILOT_DOCUMENT_CHILD_RELATIONS}


def test_a_direct_id_lookup_on_a_document_child_is_empty(
    their_pilot_rows, two_projects, web_connection
):
    """The same ids, asked for one at a time, which is how a partition is bypassed."""

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        reached = {
            relation: web.execute(
                text(
                    f"select {PILOT_DOCUMENT_CHILD_KEYS[relation]} "
                    f"from {relation} "
                    f"where {PILOT_DOCUMENT_CHILD_KEYS[relation]} = :id"
                ),
                {"id": their_pilot_rows[relation]},
            ).scalars().all()
            for relation in PILOT_DOCUMENT_CHILD_RELATIONS
        }

    assert reached == {relation: [] for relation in PILOT_DOCUMENT_CHILD_RELATIONS}


def test_a_document_child_reads_nothing_without_a_partition(
    their_pilot_rows, web_connection
):
    """Fail closed: no declared partition is the empty partition, here too."""

    with OrmSession(bind=web_connection) as web:
        assert access.current_project_partition(web) is None
        for relation in PILOT_DOCUMENT_CHILD_RELATIONS:
            assert web.execute(
                text(f"select document_id from {relation}")
            ).scalars().all() == []


def test_the_boundary_leaves_the_machine_capability_whole(runtime_database):
    """The worker is the capability machine work runs on, and it lost nothing.

    Denying the human web role is only safe because the transport-authenticated
    ingress moved onto this one. If the revoke had reached it too, the pilot
    would have a partition and no way to receive a source.
    """

    with runtime_database.session_factory() as owner:
        unreadable = owner.execute(
            text(
                "select c.relname from pg_class c "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' and c.relkind = 'r' "
                "and c.relname = any(cast(:denied as text[])) "
                "and not has_table_privilege('corridor_worker', c.oid, 'SELECT') "
                "order by c.relname"
            ),
            {"denied": sorted(web_boundary.DENIED_RELATIONS)},
        ).scalars().all()

    assert unreadable == []


def test_the_classification_and_the_grants_agree_about_every_relation(
    runtime_database,
):
    """The boundary restated as one sentence PostgreSQL can answer.

    Everything `corridor_web` can read is a relation the classification
    answers with something other than "not yet partitioned", and everything it
    answers that way is a relation `corridor_web` cannot read. Either
    direction failing is drift.
    """

    with runtime_database.session_factory() as owner:
        readable = set(_web_readable_relations(owner.connection()))

    assert sorted(readable & web_boundary.DENIED_RELATIONS) == []
    assert sorted(web_boundary.PROTECTED_RELATIONS - readable) == []


# --- #693 A grant to PUBLIC is a grant to every role, decided by nobody ----
#
# #680's revoke reported success on `subject_resolution_decisions` and left it
# readable, because the relation also carried `GRANT SELECT ... TO PUBLIC` and
# a revoke aimed at `corridor_web` does not touch one. Worse, the check that
# was supposed to catch that — `has_table_privilege('corridor_web', ...)`,
# which every assertion in the section above uses — answers *true* through
# PUBLIC without saying where the privilege came from, so both halves agreed.
#
# So the two tests that matter here are of two different kinds and neither
# replaces the other. The catalog test reads the *effective* ACL with
# `aclexplode`, which reports the grant whatever role made it, at relation and
# at column level. The live test connects as a role holding no grant on
# anything — the role somebody adds to this database next year — and runs the
# select. A static reading of the explicit `GRANT ... TO corridor_web`
# statements would pass while both of these failed, which is exactly how the
# bypass survived.

_PUBLIC_GRANT_SCAN = text(
    "select c.relname as relation, a.privilege_type as privilege "
    "  from pg_class c "
    "  join pg_namespace n on n.oid = c.relnamespace "
    "  cross join lateral aclexplode(c.relacl) a "
    " where n.nspname = 'public' "
    "   and c.relkind in ('r', 'p', 'v', 'm', 'f') "
    "   and a.grantee = 0 "
    " union all "
    "select c.relname || '.' || att.attname as relation, "
    "       a.privilege_type as privilege "
    "  from pg_class c "
    "  join pg_namespace n on n.oid = c.relnamespace "
    "  join pg_attribute att "
    "    on att.attrelid = c.oid and att.attnum > 0 and not att.attisdropped "
    "  cross join lateral aclexplode(att.attacl) a "
    " where n.nspname = 'public' "
    "   and c.relkind in ('r', 'p', 'v', 'm', 'f') "
    "   and a.grantee = 0 "
    " order by 1, 2"
)

# The three that carried the grant. Two are partitioned and the pilot reads
# them; one is denied outright. All three are named here because the point is
# that the rule now covers them alike, not that a list was extended.
FORMERLY_PUBLIC_RELATIONS = (
    "fact_decisions",
    "project_record_revisions",
    "subject_resolution_decisions",
)


def _public_grants(connection) -> list[tuple[str, str]]:
    return [
        (str(row.relation), str(row.privilege))
        for row in connection.execute(_PUBLIC_GRANT_SCAN)
    ]


def test_no_application_relation_or_column_grants_a_privilege_to_public(
    runtime_database,
):
    """The effective ACL, read the way PostgreSQL stores it.

    `aclexplode` over `relacl` and `attacl` with `grantee = 0` is every
    privilege PUBLIC holds on a table, partitioned table, view, materialized
    view or foreign table in `public`, and on any single column of one. It
    does not care which role wrote the grant or whether any capability list
    mentions the relation, which is the property `has_table_privilege` on a
    named role does not have.
    """

    with runtime_database.session_factory() as owner:
        observed = _public_grants(owner.connection())

    assert web_boundary.undocumented_public_privileges(observed) == (), (
        "these relations grant a privilege to PUBLIC, which is a privilege "
        "held by every role in the customer database including every role "
        "added later. Revoke it in the migration, or record it in "
        "web_boundary.PUBLIC_RELATION_PRIVILEGES with the reason it is meant"
    )


def test_the_public_grant_scan_reports_a_grant_that_really_is_there(
    runtime_database,
):
    """The scan above is only worth its green if it can go red.

    A catalog query with one clause wrong returns nothing on a clean database
    and reads exactly like a boundary that holds. So this makes a grant to
    PUBLIC, on a table and on a single column of it, and asserts the same
    query and the same allowlist call report both.
    """

    with runtime_database.session_factory.begin() as owner:
        owner.execute(text("create table public.t693_probe (id int, note text)"))
        owner.execute(text("grant select on public.t693_probe to public"))
        owner.execute(text("grant update (note) on public.t693_probe to public"))
        observed = _public_grants(owner.connection())
        undocumented = web_boundary.undocumented_public_privileges(observed)
        owner.execute(text("drop table public.t693_probe"))

    assert ("t693_probe", "SELECT") in undocumented
    assert ("t693_probe.note", "UPDATE") in undocumented


@pytest.fixture
def unprivileged_login(runtime_database):
    """A real login holding no grant on anything: the role added next year.

    Named from a digest of the harness database so parallel workers do not
    collide on a cluster-global role, and dropped with `drop owned by` first
    so a test that granted it something can still give the role back.
    """

    name = "corridor_probe_" + sha256(
        runtime_database.name.encode("utf-8")
    ).hexdigest()[:16]

    def _drop(owner) -> None:
        owner.execute(text(f'drop owned by "{name}"'))
        owner.execute(text(f'drop role "{name}"'))

    with runtime_database.session_factory.begin() as owner:
        exists = owner.execute(
            text("select 1 from pg_roles where rolname = :name"), {"name": name}
        ).scalar()
        if exists:
            _drop(owner)
        owner.execute(text(f"create role \"{name}\" login password '{name}'"))

    probe = create_engine(
        make_url(settings.database_url)
        .set(database=runtime_database.name, username=name, password=name)
        .render_as_string(hide_password=False),
        poolclass=NullPool,
        future=True,
    )
    try:
        yield probe
    finally:
        probe.dispose()
        with runtime_database.session_factory.begin() as owner:
            _drop(owner)


def _select_is_served(connection, statement: str) -> bool:
    savepoint = connection.begin_nested()
    try:
        connection.execute(text(statement))
    except ProgrammingError:
        savepoint.rollback()
        return False
    savepoint.rollback()
    return True


def test_a_login_holding_no_grant_cannot_read_the_formerly_public_relations(
    runtime_database, unprivileged_login
):
    """The harm, as the thing that actually goes wrong.

    A PUBLIC grant is not a wider grant to `corridor_web`; it is a grant to
    every role in the database, including ones nobody has created yet. So the
    proof is a login created with no membership, no designation and no grant
    at all, running the select. Before #693 it read `fact_decisions` and
    `project_record_revisions` — no rows, because both are partitioned and it
    declared no partition, but the read was *permitted*, and a relation that
    later gains a policy-free hole would have been open.
    """

    with unprivileged_login.connect() as probe:
        served = [
            relation
            for relation in FORMERLY_PUBLIC_RELATIONS
            if _select_is_served(probe, f"select 1 from {relation} limit 1")
        ]

    assert served == [], (
        "a login holding no grant at all can still read these relations, "
        "which means they carry a privilege granted to PUBLIC"
    )


def test_the_unprivileged_login_reads_what_it_is_actually_granted(
    runtime_database, unprivileged_login
):
    """The refusal above has to be about the grant, not about the connection.

    An unreachable database, a wrong schema search path or a role that cannot
    log in would all produce the same empty `served` list and prove nothing.
    So the same login is granted `SELECT` on one of the three, reads it
    without raising, and is refused again once the grant is taken back. Only
    the grant changes between the three readings.
    """

    relation = FORMERLY_PUBLIC_RELATIONS[0]
    role = unprivileged_login.url.username
    statement = f"select 1 from {relation} limit 1"

    with unprivileged_login.connect() as probe:
        before = _select_is_served(probe, statement)

    with runtime_database.session_factory.begin() as owner:
        owner.execute(text(f'grant select on public.{relation} to "{role}"'))
    with unprivileged_login.connect() as probe:
        granted = _select_is_served(probe, statement)
    with runtime_database.session_factory.begin() as owner:
        owner.execute(text(f'revoke select on public.{relation} from "{role}"'))
    with unprivileged_login.connect() as probe:
        after = _select_is_served(probe, statement)

    assert (before, granted, after) == (False, True, False)


# --- Offboarding over the real sign-in path --------------------------------


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



# --- The enabled pilot routes, served by the real web login (#680) ---------
#
# Every other web test in this repository overrides the session with the
# schema owner's, which bypasses row-level security *and* holds every
# privilege — so none of them can tell whether a route still works once the
# boundary takes the relations away. These run the routes on `corridor_web`
# itself, over a real magic-link cookie, against committed rows.


@pytest.fixture
def pilot_member(runtime_database, two_projects):
    """A signed-in-able coordinator on the first project, committed."""

    ours, _theirs = two_projects
    with runtime_database.session_factory.begin() as owner:
        access.enroll_member(
            owner,
            project_id=ours,
            email="pilot@example.com",
            principal=LEAVER,
            display_name="Pilot",
            designations=[access.COORDINATION],
            operator=OPERATOR,
        )
    return "pilot@example.com"


@pytest.fixture
def live_pilot_client(runtime_database, pilot_member):
    """The application, wired to the deployed web capability and nothing else."""

    web_engine = create_engine(
        _web_url(runtime_database.name), poolclass=NullPool, future=True
    )

    def _web_session():
        with OrmSession(bind=web_engine) as opened:
            yield opened

    sender = auth.RecordingEmailSender()
    app.dependency_overrides[get_session] = _web_session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as opened:
        _sign_in(opened, sender, "pilot@example.com")
        yield opened
    app.dependency_overrides.clear()
    web_engine.dispose()


@pytest.fixture
def boundary_enabled(monkeypatch):
    monkeypatch.setattr(settings, "live_pilot_web_boundary", True)


@pytest.fixture
def statements_in_flight(monkeypatch):
    """Every statement issued while a request is in flight (#680's instrument).

    Two halves, and both are needed. `before_cursor_execute` on the `Engine`
    class catches every statement any engine issues — an ORM load, a raw
    `text()`, a lazy attribute a template touches, a dependency's own read —
    so nothing can reach PostgreSQL past it. Wrapping `TestClient.request`
    brackets the window, so fixture seeding and sign-in are not mistaken for
    something a route did.

    Recording the statement rather than a route's name is what makes the
    zero-SQL claim checkable: "the handler did not run" is an assertion about
    the application, and "no statement named a revoked relation" is an
    assertion about the database, and only the second one is the invariant.
    """

    recorded: list[str] = []
    in_flight = False

    def _record(conn, cursor, statement, parameters, context, executemany):
        if in_flight:
            recorded.append(statement)

    sa_event.listen(Engine, "before_cursor_execute", _record)
    unwrapped = TestClient.request

    def request(self, *args, **kwargs):
        nonlocal in_flight
        in_flight = True
        try:
            return unwrapped(self, *args, **kwargs)
        finally:
            in_flight = False

    monkeypatch.setattr(TestClient, "request", request)
    yield recorded
    sa_event.remove(Engine, "before_cursor_execute", _record)


def _revoked_relations_touched(statements: list[str]) -> list[str]:
    """Which relations the boundary denies were named by these statements."""

    return sorted(
        {
            relation
            for statement in statements
            for relation in web_boundary.DENIED_RELATIONS
            if re.search(rf"\b{relation}\b", statement)
        }
    )


def test_every_enabled_pilot_reading_serves_as_the_real_web_login(
    two_projects,
    their_pilot_rows,
    live_pilot_client,
    boundary_enabled,
    statements_in_flight,
):
    """The other half of the revoke: the routes that stay on still work.

    A boundary is only worth having if the product survives it, and the only
    way to know is to ask the login that lost the privileges. Each of these
    would answer 500 with `permission denied for table ...` if the revoke had
    reached a relation the route needs.

    The second assertion is the same instrument the refusal tests use, pointed
    the other way (#694): these routes serve *and* they reach nothing the
    boundary denies, so the recorded dependency set is a live reading rather
    than a claim about one.
    """

    statements_in_flight.clear()
    served = {
        path: live_pilot_client.get(path, follow_redirects=False).status_code
        for path in ("/", "/portfolio", "/record/ours", "/review/ours")
    }

    assert served == {
        "/": 200,
        "/portfolio": 200,
        "/record/ours": 200,
        "/review/ours": 200,
    }
    assert _revoked_relations_touched(statements_in_flight) == []


# --- #824 The deterministic intake path, as the real web login ------------
#
# The routes above are readings. Two of the five admitted here write, and the
# confirmation registers *and parses* the file inside the request, so between
# them they reach six relations #680 had revoked. Proving them as
# `corridor_web` is the only way to know the parent-join policies answer the
# way the page needs: the schema owner bypasses row-level security, so every
# other test of this path would pass with no policy at all.


@pytest.fixture
def staged_store(monkeypatch, tmp_path):
    """The content-addressed store, pointed somewhere this test may write."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))


@pytest.fixture
def uploaded_workbook(tmp_path) -> bytes:
    """The customer's own UCM workbook, in the published form's column order.

    One row: confirming parses the file inside the request, and what this
    proves is which relations that parse reaches, not how many rows it reads.
    """

    return workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS[:1])


def test_the_admitted_intake_path_serves_as_the_real_web_login(
    two_projects,
    their_pilot_rows,
    live_pilot_client,
    boundary_enabled,
    staged_store,
    uploaded_workbook,
    statements_in_flight,
):
    """Upload, preview, confirm, register -- walked as `corridor_web` itself.

    The other project holds a committed row in every relation these routes
    touch, so a policy that filtered nothing would show it. Each request
    carries the forgery token the page printed rather than one composed here,
    which is what makes this a walk of the screens rather than of the handlers.
    """

    ours, _theirs = two_projects
    statements_in_flight.clear()

    form = live_pilot_client.get("/projects/ours/sources/upload")
    assert form.status_code == 200, form.text[:400]

    preview = live_pilot_client.post(
        "/projects/ours/sources/upload",
        # The hidden fields are the page's -- the forgery token among them.
        # `doc_type` is the one value this form asks a person for, so it is
        # the one value the test supplies.
        data={**form_fields(form.text, "/sources/upload"), "doc_type": "matrix"},
        files={"upload": ("ucm.xlsx", uploaded_workbook, "application/octet-stream")},
    )
    assert preview.status_code == 200, preview.text[:400]

    confirmation = form_fields(preview.text, "/sources/confirm")
    assert confirmation is not None, preview.text[:400]
    confirmed = submit_form(
        live_pilot_client, "/projects/ours/sources/confirm", confirmation
    )
    assert confirmed.status_code == 303, confirmed.text[:400]
    assert confirmed.headers["location"] == "/projects/ours/sources"

    register = live_pilot_client.get("/projects/ours/sources")
    assert register.status_code == 200, register.text[:400]
    assert "ucm.xlsx" in register.text
    assert "theirs.xlsx" not in register.text

    assert _revoked_relations_touched(statements_in_flight) == []
    # The point of the walk, said as an assertion: the relations #824
    # partitioned were actually reached, so a passing run is evidence the
    # policies answer rather than evidence the routes avoided them. `doc_pages`
    # left this list with #893 -- the confirmation stopped reading the file in
    # the request, so the pages are the standing pass's writes now, and the
    # register is what still reaches the run and the quarantine.
    assert {"extraction_runs", "document_quarantines"} <= {
        relation
        for statement in statements_in_flight
        for relation in ("extraction_runs", "document_quarantines")
        if re.search(rf"\b{relation}\b", statement)
    }
    # And the other half of that change, measured the same way: this walk
    # names none of the four relations the parse used to write here.
    assert [
        relation
        for statement in statements_in_flight
        for relation in sorted(web_boundary.WRITE_DENIED_RELATIONS)
        if re.search(rf"\b{relation}\b", statement)
    ] == []


def test_the_admitted_intake_path_refuses_the_other_project(
    two_projects,
    their_pilot_rows,
    live_pilot_client,
    boundary_enabled,
    staged_store,
    uploaded_workbook,
    statements_in_flight,
):
    """The same login, the same routes, the project it is not a member of.

    A non-member is answered exactly as a missing project is, so nothing here
    distinguishes "no such project" from "not yours" -- and no request reaches
    a relation the boundary revoked on its way to saying so.
    """

    # The token this person legitimately holds, taken off their own project's
    # page: the request being refused is a member of one project reaching for
    # another, not an unauthenticated one.
    own = live_pilot_client.get("/projects/ours/sources/upload")
    assert own.status_code == 200
    carried = form_fields(own.text, "/sources/upload") or {}

    statements_in_flight.clear()
    refused = {
        "upload form": live_pilot_client.get(
            "/projects/theirs/sources/upload", follow_redirects=False
        ),
        "register": live_pilot_client.get(
            "/projects/theirs/sources", follow_redirects=False
        ),
        "preview": live_pilot_client.post(
            "/projects/theirs/sources/upload",
            data={**carried, "doc_type": "matrix"},
            files={
                "upload": (
                    "ucm.xlsx", uploaded_workbook, "application/octet-stream"
                )
            },
            follow_redirects=False,
        ),
        "source link": live_pilot_client.get(
            "/review/theirs/source",
            params={
                "item": "source_revision:ucm@1:record_cleanup",
                "delta_id": 1,
                "source_row_id": 1,
                "role": "source_url",
            },
            follow_redirects=False,
        ),
    }

    assert {name: answer.status_code for name, answer in refused.items()} == {
        "upload form": 404,
        "register": 404,
        "preview": 404,
        "source link": 404,
    }
    assert _revoked_relations_touched(statements_in_flight) == []


def test_the_review_source_link_resolves_its_ids_as_the_real_web_login(
    two_projects,
    their_pilot_rows,
    live_pilot_client,
    boundary_enabled,
    statements_in_flight,
):
    """The admitted redirect runs its own reads, and answers a controlled 404.

    No URL comes from the query: the route resolves the retained child and the
    immutable baseline source row itself, inside the partition `_project`
    declared. This fixture holds neither row, so what it proves is the half
    only the real login can prove -- that the reads *ran*, on relations the
    revoke left standing, and that a pair of ids resolving to nothing answers
    the route's own sentence rather than PostgreSQL's. The redirect itself is
    proved on the rendered screen in `tests/test_packet_review_screen.py`, and
    the cross-project refusal is the test above.
    """

    statements_in_flight.clear()
    answered = live_pilot_client.get(
        "/review/ours/source",
        params={
            "item": "source_revision:ucm@1:record_cleanup",
            "delta_id": 1,
            "source_row_id": 1,
            "role": "source_url",
        },
        follow_redirects=False,
    )

    assert answered.status_code == 404
    assert answered.json() == {"detail": "no such source link in this reading"}
    assert _revoked_relations_touched(statements_in_flight) == []
    assert [
        statement
        for statement in statements_in_flight
        if re.search(r"\bproposed_deltas\b", statement)
    ], "the route answered without reading the relation it resolves ids in"


def test_a_route_outside_the_boundary_is_refused_rather_than_half_served(
    two_projects, live_pilot_client, boundary_enabled, statements_in_flight
):
    """"Disable the route" is the criterion, and this is the difference it makes.

    With the boundary declared, a surface the pilot does not enable answers
    exactly as a missing project does, and #694 adds the part that makes it a
    refusal rather than a coincidence: the request issues **no statement at
    all**. Not "no statement PostgreSQL allowed" — none, because the route
    never entered a handler.
    """

    statements_in_flight.clear()
    refused = live_pilot_client.get("/ledger/ours", follow_redirects=False)

    assert refused.status_code == 404
    assert statements_in_flight == []
    assert refused.json() == {"detail": "not found"}


# --- #694 The deployment that has the revoke and not the route half --------
#
# #680 shipped the route half declared and off by default and said so: with
# the flag off, `/ledger/{slug}` reached its handler and died on the privilege
# it no longer had. That is a real refusal and an unreadable one, and it made
# PostgreSQL the route-selection mechanism. These are the two states that
# replace it, and the control that proves the handler really would have gone
# there.


def test_a_route_the_deployment_cannot_serve_refuses_before_it_queries(
    two_projects, live_pilot_client, statements_in_flight
):
    """The criterion, whole: refused, and refused before any statement.

    The flag is off and the reads run as `corridor_web`, which is the shape a
    live-pilot deployment has the moment it applies the migration and forgets
    the flag. Every assertion here is one of the four things #694 asks for —
    a controlled status, a stable internal reason, no statement that reached a
    revoked relation, and a body carrying neither PostgreSQL's message nor the
    name of a relation for a reader to go looking for.
    """

    statements_in_flight.clear()
    refused = live_pilot_client.get("/ledger/ours", follow_redirects=False)

    assert refused.status_code == 503
    assert refused.json() == {"detail": "live_pilot_web_boundary_disabled"}
    assert _revoked_relations_touched(statements_in_flight) == []
    assert statements_in_flight == []
    assert "permission denied" not in refused.text
    assert not any(
        re.search(rf"\b{relation}\b", refused.text)
        for relation in web_boundary.DENIED_RELATIONS
    )


def test_the_same_route_still_dies_where_the_deployment_kept_its_blanket_read(
    two_projects, live_pilot_client, statements_in_flight
):
    """The control: the handler really does go to a relation it may not read.

    One seam differs from the test above — the login this deployment's reads
    run as. Declared as the legacy development capability, which the revoke
    never touched, the boundary is not this deployment's business and the
    request runs exactly as it always has: into the handler, into a revoked
    relation, into PostgreSQL's own `InsufficientPrivilege`.

    Without this the refusal test proves nothing, because a route that reaches
    no revoked relation refuses zero statements whether the guard exists or
    not. Which relation stops it first is not the claim; that one did is.
    """

    app.dependency_overrides[get_web_capability] = lambda: "corridor_legacy_dev"
    strict = TestClient(app, base_url="https://testserver")
    strict.cookies = live_pilot_client.cookies

    statements_in_flight.clear()
    with pytest.raises(ProgrammingError) as refused:
        strict.get("/ledger/ours", follow_redirects=False)

    assert "permission denied for table" in str(refused.value)
    assert _revoked_relations_touched(statements_in_flight) != []


def test_the_enabled_work_route_refuses_a_legacy_project_the_deployment_cannot_serve(
    two_projects, live_pilot_client, statements_in_flight
):
    """`/work/{slug}` is enabled, and its legacy branch is not — flag or no flag.

    The route gate cannot reach this one: `/work/{slug}` is in the pilot set,
    and only the project's operating mode decides which of its two branches
    runs. So the refusal lives at the branch, and it answers by the same state
    the gate uses rather than by the flag alone — otherwise the deployment
    that has the revoke and not the flag falls into ADR-0035's Work List and
    dies inside a template on `dependencies`.
    """

    statements_in_flight.clear()
    refused = live_pilot_client.get("/work/ours", follow_redirects=False)

    assert refused.status_code == 503
    assert refused.json() == {"detail": "live_pilot_web_boundary_disabled"}
    assert _revoked_relations_touched(statements_in_flight) == []


def test_the_work_list_branch_still_reaches_a_revoked_relation_when_it_may(
    two_projects, live_pilot_client, statements_in_flight
):
    """The control for the branch above, and the reason it is not dead code.

    Declared as the legacy development capability the revoke never touched,
    the same request falls into ADR-0035's Work List and reads `dependencies`
    — which is the failure the refusal above is preventing. Without this the
    503 could be guarding a branch that never had a problem.
    """

    app.dependency_overrides[get_web_capability] = lambda: "corridor_legacy_dev"
    strict = TestClient(app, base_url="https://testserver")
    strict.cookies = live_pilot_client.cookies

    statements_in_flight.clear()
    with pytest.raises(ProgrammingError) as refused:
        strict.get("/work/ours", follow_redirects=False)

    assert "permission denied for table" in str(refused.value)
    assert _revoked_relations_touched(statements_in_flight) != []


@pytest.fixture
def cleared_overrides():
    """Leave the application as this test found it, however it ends."""

    yield
    app.dependency_overrides.clear()


def test_readiness_reports_the_boundary_state_this_deployment_is_in(
    session, tmp_path, cleared_overrides
):
    """A deployment that refuses most of itself must not report healthy.

    `/health` is an enabled route reaching no relation, so it stays served in
    every state — which is what lets it carry the state that refuses the
    others. The reading is the same stable reason the refusal gives, so an
    operator greps one string across the probe and the access log.
    """

    def _machine():
        yield session

    app.dependency_overrides[get_machine_session] = _machine
    app.dependency_overrides[get_content_store] = lambda: LocalFilesystemStore(
        tmp_path
    )
    client = TestClient(app)

    app.dependency_overrides[get_web_capability] = lambda: "corridor_legacy_dev"
    kept_the_blanket_read = client.get("/health")
    app.dependency_overrides[get_web_capability] = lambda: "corridor_web"
    has_the_revoke_only = client.get("/health")
    app.dependency_overrides[get_web_capability] = lambda: ""
    cannot_read_its_capability = client.get("/health")

    assert kept_the_blanket_read.status_code == 200
    assert kept_the_blanket_read.json()["checks"][-1] == {
        "component": "live_pilot_web_boundary",
        "healthy": True,
        "detail": "not_declared",
    }
    assert has_the_revoke_only.status_code == 503
    assert has_the_revoke_only.json()["status"] == "degraded"
    assert has_the_revoke_only.json()["checks"][-1] == {
        "component": "live_pilot_web_boundary",
        "healthy": False,
        "detail": "live_pilot_web_boundary_disabled",
    }
    # #822: a deployment whose capability nobody could read is not serving
    # either, and the activation gate reads this same component before it will
    # write a receipt (`corridor.activation.collect_boundary_smoke`).
    assert cannot_read_its_capability.status_code == 503
    assert cannot_read_its_capability.json()["checks"][-1] == {
        "component": "live_pilot_web_boundary",
        "healthy": False,
        "detail": "live_pilot_web_boundary_disabled",
    }


def test_the_deployment_state_is_derived_from_the_flag_and_the_reading_login():
    """The one decision the two refusal modes hang from, stated on its own.

    A boolean cannot answer this: both flag-off deployments have the flag off,
    and only one of them has had 124 relations taken away.
    """

    assert (
        web_boundary.boundary_state(declared=True, web_capability="corridor_web")
        is web_boundary.BoundaryState.ENFORCED
    )
    assert (
        web_boundary.boundary_state(declared=False, web_capability="corridor_web")
        is web_boundary.BoundaryState.INCONSISTENT
    )
    assert (
        web_boundary.boundary_state(
            declared=False, web_capability="corridor_legacy_dev"
        )
        is web_boundary.BoundaryState.NOT_DECLARED
    )


def test_a_capability_the_boundary_cannot_name_refuses_rather_than_reading_as_legacy():
    """#822: the third answer the reading login can give, and what it means now.

    #694 asked one question — "is this `corridor_web`?" — and read every other
    answer as the legacy development clone, which refuses nothing. Two answers
    are not that clone: a bind this process could not inspect, which arrives as
    no capability at all, and a login this build has never heard of. Neither is
    evidence that nothing was taken away, and treating them as the forgiving
    deployment is how a process that has the revoke serves the surfaces the
    revoke exists to close.

    So the excused logins are a named list rather than a fallback, and it holds
    exactly the two capabilities the revoke never aimed at: the opt-in login
    ADR-0081 keeps for a legacy development deployment, and the schema owner
    that migrations, the test harness and local tooling connect as. Both are
    deployment configuration somebody selected.
    """

    schema_owner = make_url(settings.database_url).username

    assert web_boundary.legacy_capabilities() == {"corridor_legacy_dev", schema_owner}

    def state(capability):
        return web_boundary.boundary_state(declared=False, web_capability=capability)

    assert state("") is web_boundary.BoundaryState.INCONSISTENT
    assert state("postgres") is web_boundary.BoundaryState.INCONSISTENT
    assert (
        state(web_boundary.LIVE_PILOT_WEB_CAPABILITY)
        is web_boundary.BoundaryState.INCONSISTENT
    )
    assert state("corridor_legacy_dev") is web_boundary.BoundaryState.NOT_DECLARED
    assert state(schema_owner) is web_boundary.BoundaryState.NOT_DECLARED


def test_a_bind_this_request_cannot_read_answers_with_no_capability_at_all():
    """The reader's own failure mode, which is what #822 was filed about.

    Two shapes reach it: a substituted session seam whose `get_bind` raises,
    and a bind whose URL carries no username. Both answer with no capability,
    and no capability is not a name `legacy_capabilities` holds — so the
    deployment is inconsistent rather than excused, which is the whole fix.
    """

    from types import SimpleNamespace

    class _NoBind:
        def get_bind(self):
            raise RuntimeError("a substituted seam with no bind")

    nameless = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(
            url=make_url("postgresql+psycopg://localhost:5433/corridor")
        )
    )

    assert get_web_capability(_NoBind()) == ""
    assert get_web_capability(nameless) == ""
    assert (
        web_boundary.boundary_state(declared=False, web_capability="")
        is web_boundary.BoundaryState.INCONSISTENT
    )


def test_absent_and_malformed_boundary_configuration_never_declare_the_boundary(
    monkeypatch,
):
    """The flag half of the same question, at both of its failure shapes (#822).

    Absent, the boundary is undeclared, and the deployment reading as the
    revoked live-pilot login refuses. Malformed, the process does not start at
    all: the field is typed, so `Settings` rejects the value rather than
    resolving it to whichever of true and false the string is truthy for.
    `boundary_state` asks for the boolean itself for the same reason — a value
    that reaches it from somewhere that never parsed it declares nothing.
    """

    from pydantic import ValidationError

    from corridor.config import Settings

    monkeypatch.delenv("CORRIDOR_LIVE_PILOT_WEB_BOUNDARY", raising=False)
    absent = Settings(_env_file=None).live_pilot_web_boundary

    assert absent is False
    assert (
        web_boundary.boundary_state(
            declared=absent, web_capability=web_boundary.LIVE_PILOT_WEB_CAPABILITY
        )
        is web_boundary.BoundaryState.INCONSISTENT
    )

    monkeypatch.setenv("CORRIDOR_LIVE_PILOT_WEB_BOUNDARY", "sometimes")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

    assert (
        web_boundary.boundary_state(
            declared="true", web_capability=web_boundary.LIVE_PILOT_WEB_CAPABILITY
        )
        is web_boundary.BoundaryState.INCONSISTENT
    )


def test_two_halves_that_disagree_are_inconsistent_however_they_are_declared(
    monkeypatch,
):
    """The other inconsistency: an enabled route needing a revoked relation.

    `make check` fails on this, so a deployment can only reach it by shipping
    past the build guard — which is exactly when a runtime that trusts the
    flag would serve half a page. Neither the declared nor the undeclared
    deployment is serving in that state, so neither is told it is.
    """

    monkeypatch.setattr(
        web_boundary, "unprotected_route_relations", lambda: ("work_decisions",)
    )

    assert (
        web_boundary.boundary_state(
            declared=True, web_capability="corridor_legacy_dev"
        )
        is web_boundary.BoundaryState.INCONSISTENT
    )
    assert (
        web_boundary.boundary_state(
            declared=False, web_capability="corridor_legacy_dev"
        )
        is web_boundary.BoundaryState.INCONSISTENT
    )


def test_each_state_answers_an_unapproved_route_in_its_own_way():
    """404 when the surface is declared, 503 when the deployment is not.

    The status code says whose problem it is. An enforced boundary is serving
    the surface it declared and the route is simply not part of it; an
    inconsistent one is misconfigured, and a probe that reads 404 as "fine"
    would never learn that.
    """

    outside = ("GET", "/ledger/{slug}")
    inside = ("GET", "/portfolio")

    assert web_boundary.route_refusal(
        web_boundary.BoundaryState.ENFORCED, *outside
    ) == web_boundary.RouteRefusal(404, "not found")
    assert web_boundary.route_refusal(
        web_boundary.BoundaryState.INCONSISTENT, *outside
    ) == web_boundary.RouteRefusal(503, "live_pilot_web_boundary_disabled")
    assert (
        web_boundary.route_refusal(web_boundary.BoundaryState.NOT_DECLARED, *outside)
        is None
    )
    for state in web_boundary.BoundaryState:
        assert web_boundary.route_refusal(state, *inside) is None


def test_the_enabled_work_route_refuses_a_project_the_pilot_cannot_serve(
    two_projects, live_pilot_client, boundary_enabled
):
    """`/work/{slug}` is enabled, and its legacy branch is not.

    An unadopted project falls to ADR-0035's item-per-record Work List, which
    reads `dependencies`, `work_decisions` and `evidence_links` — all revoked.
    Leaving them readable to reach this branch is the trade #680 refuses, so
    the route refuses the project instead.
    """

    refused = live_pilot_client.get("/work/ours", follow_redirects=False)

    assert refused.status_code == 404


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
