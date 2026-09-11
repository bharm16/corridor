"""What the shared test-harness seam promises the 170 modules that use it.

The rollback-scoped ``session`` fixture, the synthetic ``project`` inside it,
that project with the membership a project surface requires, and the
record-decision role context manager were each copied into every
database test module by hand. Once one definition serves all of them, the
properties the copies asserted only by construction need somewhere to be
asserted on purpose: that a write never survives its test, that a project
exists before the test body runs, and that the borrowed role is given back even
when the body raises. A regression in any of those is otherwise invisible --
the tests keep passing, but they stop testing what they say.
"""

from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

import conftest as harness
from harness_support import adopt_baseline_facts, as_role, move_accepted_value

from corridor import access
from corridor.db import capability_engine
from corridor.db_roles import (
    DATABASE_ROLE_NAMES,
    RECORD_DECISION_ROLE,
    WORKER_CAPABILITY_LOGIN,
)
from corridor.fact_decisions import record_human_fact_decision
from corridor.models import (
    ActiveExtractionRun,
    Document,
    ExtractionRun,
    Fact,
    Project,
    ProjectRosterEntry,
)
from corridor.principals import HumanPrincipal


LEFTOVER = "harness-fixture-leftover"


def _slugs(connection, slug: str) -> int:
    return connection.execute(
        text("select count(*) from projects where slug = :slug"), {"slug": slug}
    ).scalar_one()


def test_the_shared_session_is_a_rollback_scoped_session(session):
    assert isinstance(session, Session)
    bound = session.get_bind()
    assert bound.dialect.name == "postgresql"
    assert bound.in_transaction()


def test_a_write_in_the_shared_session_reaches_no_other_connection(session):
    slug = f"harness-uncommitted-{uuid4().hex[:8]}"
    session.add(Project(slug=slug, name="Uncommitted", is_synthetic=True))
    session.flush()
    assert session.scalar(
        text("select count(*) from projects where slug = :slug"), {"slug": slug}
    ) == 1
    with capability_engine("owner").connect() as independent:
        assert _slugs(independent, slug) == 0


def test_a_write_in_the_shared_session_is_gone_once_the_session_ends():
    """One test proves the rollback: the pair form let worksteal split it across workers."""
    with harness.rollback_scoped_session() as scoped:
        scoped.add(Project(slug=LEFTOVER, name="Leftover", is_synthetic=True))
        scoped.flush()
        assert scoped.scalar(
            text("select count(*) from projects where slug = :slug"), {"slug": LEFTOVER}
        ) == 1
    with capability_engine("owner").connect() as independent:
        assert _slugs(independent, LEFTOVER) == 0
    with harness.rollback_scoped_session() as later:
        assert later.scalar(
            text("select count(*) from projects where slug = :slug"), {"slug": LEFTOVER}
        ) == 0


def test_the_project_fixture_is_a_flushed_synthetic_row(session, project):
    assert isinstance(project, Project)
    assert project.id is not None and project.is_synthetic
    assert session.get(Project, project.id) is project
    with capability_engine("owner").connect() as independent:
        assert _slugs(independent, project.slug) == 0


def test_every_synthetic_project_carries_a_fresh_slug(session, project):
    """Two projects from the one generator in one session never share a slug."""
    second = harness.synthetic_project(session)
    assert project.slug.startswith("project-") and second.slug.startswith("project-")
    assert project.slug != second.slug
    assert second.id is not None and second.id != project.id
    assert session.get(Project, second.id) is second


def test_the_member_project_fixture_seeds_the_membership_the_access_gate_wants(
    session, member_project
):
    """The 17 copies of this fixture existed only for the roster entry."""
    principal = HumanPrincipal(f"local:harness-member-{uuid4().hex[:8]}")
    built = member_project(principal, designations=[access.COORDINATION])
    entry = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.project_id == built.id,
            ProjectRosterEntry.principal_subject == principal.subject,
        )
    ).one()
    assert entry.active and entry.can_coordinate
    assert not entry.is_technical_operator


def test_every_member_project_carries_a_fresh_slug(session, member_project):
    """Two member projects in one session never collide on the unique slug."""
    principal = HumanPrincipal(f"local:harness-member-{uuid4().hex[:8]}")
    first, second = member_project(principal), member_project(principal)
    assert first.slug.startswith("project-") and second.slug.startswith("project-")
    assert first.slug != second.slug and first.id != second.id


def test_a_named_isolation_level_is_the_one_the_transaction_actually_runs_at():
    """The two modules that copied this fixture wanted this setting, not the body."""
    default = harness.rollback_scoped_session
    with default() as scoped:
        assert scoped.scalar(text("show transaction_isolation")) == "read committed"
    for level, reported in (
        ("REPEATABLE READ", "repeatable read"),
        ("SERIALIZABLE", "serializable"),
    ):
        with default(isolation_level=level) as scoped:
            assert scoped.scalar(text("show transaction_isolation")) == reported


def test_the_role_context_manager_borrows_and_returns_the_role(session):
    before = session.scalar(text("select current_user"))
    with as_role(session, RECORD_DECISION_ROLE) as borrowed:
        assert borrowed is session
        assert session.scalar(text("select current_user")) == RECORD_DECISION_ROLE
    assert session.scalar(text("select current_user")) == before


def test_the_role_is_returned_even_when_the_body_raises(session):
    before = session.scalar(text("select current_user"))
    with pytest.raises(RuntimeError, match="body failed"):
        with as_role(session, RECORD_DECISION_ROLE):
            assert session.scalar(text("select current_user")) == RECORD_DECISION_ROLE
            raise RuntimeError("body failed")
    assert session.scalar(text("select current_user")) == before


def test_a_borrow_inside_a_borrow_hands_back_the_role_it_was_given(session):
    """A nested borrow returns the caller's principal, not the session user.

    ``reset role`` returns to the session user, which is the schema owner
    here, so an inner borrow used to hand the outer body the owner. The Source
    Fact capture seam performs exactly that inner borrow inside a test that is
    already holding the record-decision role, and every assertion made after
    it was then made by a principal the test never chose.
    """
    before = session.scalar(text("select current_user"))
    with as_role(session, RECORD_DECISION_ROLE):
        with as_role(session, WORKER_CAPABILITY_LOGIN):
            assert session.scalar(text("select current_user")) == WORKER_CAPABILITY_LOGIN
        assert session.scalar(text("select current_user")) == RECORD_DECISION_ROLE
    assert session.scalar(text("select current_user")) == before


def test_a_nested_borrow_hands_the_role_back_even_when_its_body_raises(session):
    """The same promise for the failure the seam exists for."""
    before = session.scalar(text("select current_user"))
    with as_role(session, RECORD_DECISION_ROLE):
        with pytest.raises(RuntimeError, match="body failed"):
            with as_role(session, WORKER_CAPABILITY_LOGIN):
                raise RuntimeError("body failed")
        assert session.scalar(text("select current_user")) == RECORD_DECISION_ROLE
    assert session.scalar(text("select current_user")) == before


@pytest.mark.parametrize("role", sorted(DATABASE_ROLE_NAMES))
def test_every_role_is_returned_even_when_the_body_raises(session, role):
    """The defect this seam exists for, for each of the roles it now takes.

    ``as_record_decision_role`` held this for one role, so eight modules wrote
    the pair by hand for the other three and one of them left the role set. A
    body that raises may not be able to leave the wrong principal in force for
    any role the suite borrows.
    """
    before = session.scalar(text("select current_user"))
    with pytest.raises(RuntimeError, match="body failed"):
        with as_role(session, role):
            assert session.scalar(text("select current_user")) == role
            raise RuntimeError("body failed")
    assert session.scalar(text("select current_user")) == before


def _decided_fact(session, project, key: str, value: str) -> Fact:
    """One structured-cell Fact a decision can be recorded against.

    ``station_from`` is dual-use since #451 stage 3 -- automatic off its
    spreadsheet cell, human-settled by a Discrepancy Resolution -- so the same
    Fact type reaches both the real command and the fixture below.
    """
    document = session.scalar(
        select(Document).where(Document.project_id == project.id)
    )
    if document is None:
        document = Document(
            project_id=project.id,
            sha256=sha256(project.slug.encode()).hexdigest(),
            filename="ucm.xlsx",
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        session.add(document)
        session.flush()
        run = ExtractionRun(
            document_id=document.id,
            prompt_version="harness_fixture_v1",
            outcome="completed",
            candidate_count=0,
            page_errors=0,
        )
        session.add(run)
        session.flush()
        session.add(
            ActiveExtractionRun(document_id=document.id, extraction_run_id=run.id)
        )
        session.flush()
    run_id = session.scalar(
        select(ActiveExtractionRun.extraction_run_id).where(
            ActiveExtractionRun.document_id == document.id
        )
    )
    fact = Fact(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=run_id,
        fact_type="station_from",
        subject_kind="source_row",
        subject_key=key,
        text_value=value,
        transformation="trim_cell_text_v1",
        recorded_by="extractor:harness_fixture_v1",
        content_sha256=sha256(f"{project.slug}:{key}:{value}".encode()).hexdigest(),
    )
    session.add(fact)
    session.flush()
    return fact


def _supersession(session, project, key: str) -> tuple[int | None, ...]:
    """Which decision each decision was retired against, by position not by id.

    Positions make the two paths comparable: the same shape written at
    different ids is the same shape.
    """
    rows = session.execute(
        text(
            "select id, superseded_by from fact_decisions"
            " where project_id = :project_id and subject_key = :key"
            " order by id"
        ),
        {"project_id": project.id, "key": key},
    ).all()
    positions = {row.id: index for index, row in enumerate(rows)}
    return tuple(
        None if row.superseded_by is None else positions[row.superseded_by]
        for row in rows
    )


def test_moving_the_accepted_value_leaves_what_the_real_command_leaves(session):
    """The fixture's supersession shape is the command's, not a remembered copy.

    ``harness_support.move_accepted_value`` cannot call
    ``record_human_fact_decision`` -- the act it reproduces is a Resolve Delta,
    which that command does not admit -- so it restates the command's SQL. This
    is what stops the restatement drifting: the command runs here over the same
    Fact type, and the rows it leaves are compared against the rows the fixture
    leaves. A command that stopped retiring its predecessor, or started
    retiring it some other way, fails here.
    """
    key = "Sheet1!3"
    commanded = harness.synthetic_project(session)
    first = _decided_fact(session, commanded, key, "1149+00")
    second = _decided_fact(session, commanded, key, "1150+00")
    opened = record_human_fact_decision(
        session,
        first,
        principal=HumanPrincipal("local:coordinator"),
        command_type="resolve_discrepancy",
        idempotency_key="pin:first",
    )
    record_human_fact_decision(
        session,
        second,
        principal=HumanPrincipal("local:coordinator"),
        command_type="resolve_discrepancy",
        idempotency_key="pin:second",
        expected_predecessor=opened.decision.id,
    )
    session.expire_all()

    fixtured = harness.synthetic_project(session)
    adopted = _decided_fact(session, fixtured, key, "1149+00")
    moved = _decided_fact(session, fixtured, key, "1150+00")
    adopt_baseline_facts(session, fixtured, adopted)
    move_accepted_value(session, fixtured, moved)

    assert _supersession(session, fixtured, key) == _supersession(session, commanded, key)
    assert _supersession(session, fixtured, key) == (1, None)
