"""The Constraint screen's reading, read without a browser.

These are the domain facts the screen used to assert against escaped HTML: which
fields carry a settle card, which carry amendment work instead, and what order
the roster of assignable people is in. Stated against
``corridor.web.dependency_view`` they are readable, they name the rule's owner,
and they fail with the fact rather than with a diff of markup.
"""

from datetime import date
from hashlib import sha256

import pytest

from corridor.db import Session, engine
from corridor.disputes import (
    CONTRACTUAL_AMENDMENT,
    assessed_amendment_field_names,
    history_assessments_for,
)
from corridor.models import (
    Assertion,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    Project,
    ProjectRosterEntry,
)
from corridor.web.dependency_view import (
    NoSuchConstraint,
    active_project_roster,
    dependency_view,
)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    row = Project(slug="dependency-view", name="Dependency View", is_synthetic=True)
    session.add(row)
    session.flush()
    return row


def _dependency(session, project, ref_code="DEP-VIEW-1"):
    row = Dependency(
        project_id=project.id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title="Water main",
    )
    session.add(row)
    session.flush()
    return row


def _claim(session, dependency, *, filename, doc_date, value, doc_type="matrix"):
    document = Document(
        project_id=dependency.project_id,
        sha256=sha256(f"{dependency.project_id}:{filename}".encode()).hexdigest(),
        filename=filename,
        doc_type=doc_type,
        doc_date=doc_date,
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    quote = f"Recorded value: {value}"
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=quote,
        verified=True,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dependency.id,
            field_name="station_from",
            asserted_value=value,
            evidence_link_id=link.id,
            doc_date=doc_date,
        )
    )
    session.flush()
    return document


def test_a_stale_executed_agreement_is_amendment_work_and_carries_no_settle_card(
    session, project
):
    """A contractual outcome stays contested; it is not a pick-one card.

    The rule lives in ``disputes``, and this states its consequence for the
    screen: the field is in ``dispute_amendments`` with its why-line, and it is
    absent from ``disputes``, which is what carries a settle control.
    """
    dependency = _dependency(session, project)
    _claim(
        session,
        dependency,
        filename="executed-agreement.pdf",
        doc_date=date(2025, 1, 10),
        value="12-inch",
        doc_type="agreement",
    )
    _claim(
        session,
        dependency,
        filename="matrix-2025-03.pdf",
        doc_date=date(2025, 3, 4),
        value="16-inch",
    )

    reading = dependency_view(
        session, project_id=project.id, dependency_id=dependency.id
    )

    assert "station_from" in reading.dispute_amendments
    assert reading.dispute_amendments["station_from"].outcome == CONTRACTUAL_AMENDMENT
    assert reading.dispute_amendments["station_from"].why
    # The one place a settle control is offered from.
    assert "station_from" not in reading.disputes
    assert reading.settleable_field_names == ()
    # And the screen's set is the domain reader's set, not a second opinion.
    assert assessed_amendment_field_names(
        history_assessments_for(session, dependency.id)
    ) == {"station_from"}


def test_an_ordinary_disagreement_keeps_its_settle_card_and_its_timeline(
    session, project
):
    dependency = _dependency(session, project)
    _claim(
        session,
        dependency,
        filename="matrix-2025-01.pdf",
        doc_date=date(2025, 1, 10),
        value="12-inch",
    )
    _claim(
        session,
        dependency,
        filename="matrix-2025-03.pdf",
        doc_date=date(2025, 3, 4),
        value="16-inch",
    )

    reading = dependency_view(
        session, project_id=project.id, dependency_id=dependency.id
    )

    assert reading.settleable_field_names == ("station_from",)
    assert "station_from" in reading.dispute_assessments
    assert "station_from" in reading.dispute_timelines
    assert reading.dispute_amendments == {}


def test_the_roster_is_one_list_in_one_total_order(session, project):
    """Two people can share a display name; the identifier breaks the tie.

    The screen used to read this list twice, ordered by ``(display_name, id)``
    once and by ``display_name`` alone the other time, and give both to the same
    control. One reading, one order, so the same person is in the same place on
    every form.
    """
    for index, display_name in enumerate(("Priya Raman", "Alex Chen", "Alex Chen")):
        session.add(
            ProjectRosterEntry(
                project_id=project.id,
                principal_subject=f"local:member-{index}",
                display_name=display_name,
                active=True,
            )
        )
    session.add(
        ProjectRosterEntry(
            project_id=project.id,
            principal_subject="local:retired",
            display_name="Retired Person",
            active=False,
        )
    )
    session.flush()
    dependency = _dependency(session, project)

    roster = active_project_roster(session, project.id)
    reading = dependency_view(
        session, project_id=project.id, dependency_id=dependency.id
    )

    assert [entry.display_name for entry in roster] == [
        "Alex Chen",
        "Alex Chen",
        "Priya Raman",
    ]
    # The tie between the two "Alex Chen" entries is broken by the identifier,
    # so the order is total and the same on every form.
    tied = [entry.id for entry in roster if entry.display_name == "Alex Chen"]
    assert tied == sorted(tied)
    assert reading.plan.roster == roster


def test_a_constraint_of_another_project_is_refused_rather_than_read(session, project):
    other = Project(slug="dependency-view-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    dependency = _dependency(session, other, ref_code="DEP-VIEW-OTHER")

    with pytest.raises(NoSuchConstraint):
        dependency_view(session, project_id=project.id, dependency_id=dependency.id)
    with pytest.raises(NoSuchConstraint):
        dependency_view(session, project_id=project.id, dependency_id=987654)
