"""What the shared test-harness seam promises the 170 modules that use it.

The rollback-scoped ``session`` fixture, the synthetic ``project`` inside it,
and the record-decision role context manager were each copied into every
database test module by hand. Once one definition serves all of them, the
properties the copies asserted only by construction need somewhere to be
asserted on purpose: that a write never survives its test, that a project
exists before the test body runs, and that the borrowed role is given back even
when the body raises. A regression in any of those is otherwise invisible --
the tests keep passing, but they stop testing what they say.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from harness_support import as_record_decision_role

from corridor.db import capability_engine
from corridor.db_roles import RECORD_DECISION_ROLE
from corridor.models import Project


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


def test_the_shared_session_writes_a_row_the_next_test_must_not_see(session):
    session.add(Project(slug=LEFTOVER, name="Leftover", is_synthetic=True))
    session.flush()
    assert session.scalar(
        text("select count(*) from projects where slug = :slug"), {"slug": LEFTOVER}
    ) == 1


def test_the_next_test_sees_none_of_the_previous_write(session):
    assert session.scalar(
        text("select count(*) from projects where slug = :slug"), {"slug": LEFTOVER}
    ) == 0


def test_the_project_fixture_is_a_flushed_synthetic_row(session, project):
    assert isinstance(project, Project)
    assert project.id is not None and project.is_synthetic
    assert session.get(Project, project.id) is project
    with capability_engine("owner").connect() as independent:
        assert _slugs(independent, project.slug) == 0


def test_each_test_receives_its_own_project_slug(project):
    assert project.slug.startswith("project-")
    assert project.slug != test_each_test_receives_its_own_project_slug.seen
    test_each_test_receives_its_own_project_slug.seen = project.slug


test_each_test_receives_its_own_project_slug.seen = None


def test_the_role_context_manager_borrows_and_returns_the_role(session):
    before = session.scalar(text("select current_user"))
    with as_record_decision_role(session) as borrowed:
        assert borrowed is session
        assert session.scalar(text("select current_user")) == RECORD_DECISION_ROLE
    assert session.scalar(text("select current_user")) == before


def test_the_role_is_returned_even_when_the_body_raises(session):
    before = session.scalar(text("select current_user"))
    with pytest.raises(RuntimeError, match="body failed"):
        with as_record_decision_role(session):
            assert session.scalar(text("select current_user")) == RECORD_DECISION_ROLE
            raise RuntimeError("body failed")
    assert session.scalar(text("select current_user")) == before
