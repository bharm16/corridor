"""Disputes: derived from what the revisions said, settled by a human.

ADR-0031. A Dispute is a query over Assertions, not a state a row is put
into — so a disputed row is an ordinary workable row, and settling one
records a judgment beside the claims rather than erasing the losing one.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.disputes import (
    NoSuchDispute,
    disputes_for,
    settle_dispute,
    settled_field_names,
)
from corridor.exceptions import contradicted_fields, exceptions_for
from corridor.models import (
    Assertion,
    Dependency,
    DisputeSettlement,
    DocPage,
    Document,
    EvidenceLink,
    Project,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal

REVIEWER = HumanPrincipal("local:dispute-reviewer")


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def project(session):
    p = Project(slug="dispute-test", name="Dispute Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def disputed(session, project):
    """One record two revisions state differently about `station_from`."""
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00001",
        source_ref="PL1",
        dep_type="utility_relocation",
        title="Pipeline crossing",
        status="identified",
    )
    session.add(dependency)
    session.flush()

    for filename, value in (("ucm-feb.pdf", "1102+20"), ("ucm-may.pdf", "1105+00")):
        document = Document(
            project_id=project.id,
            sha256=hashlib.sha256(filename.encode()).hexdigest(),
            filename=filename,
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text=value))
        link = EvidenceLink(
            dependency_id=dependency.id,
            document_id=document.id,
            page_no=1,
            quote=value,
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
            )
        )
    session.flush()
    return dependency


def test_a_dispute_is_what_the_revisions_said(session, disputed):
    [dispute] = disputes_for(session, disputed.id)

    assert dispute.field_name == "station_from"
    assert set(dispute.values) == {"1102+20", "1105+00"}
    # Each claim carries the page it came from, so the choice is made
    # while looking at both.
    assert {c.document_filename for c in dispute.claims} == {
        "ucm-feb.pdf",
        "ucm-may.pdf",
    }
    assert all(c.page_no == 1 for c in dispute.claims)


def test_a_standing_dispute_is_a_contradiction_exception(session, disputed):
    found = [e for e in exceptions_for(session, disputed.id) if e.rule == "CONTRADICTION"]
    assert len(found) == 1
    assert "station_from" in found[0].detail


def test_settling_records_the_conclusion_and_closes_the_dispute(
    session, disputed
):
    settlement = settle_dispute(
        session,
        disputed.id,
        "station_from",
        value="1105+00",
        principal=REVIEWER,
    )

    assert settlement.settled_by == REVIEWER.subject
    assert settlement.settled_value == "1105+00"
    assert contradicted_fields(session, [disputed.id]) == {}
    assert disputes_for(session, disputed.id) == []
    assert settled_field_names(session, [disputed.id]) == {
        disputed.id: {"station_from"}
    }


def test_settling_never_erases_the_losing_claim(session, disputed):
    settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )

    values = {
        a.asserted_value
        for a in session.scalars(
            select(Assertion).where(Assertion.dependency_id == disputed.id)
        )
    }
    assert values == {"1102+20", "1105+00"}


def test_a_reviewer_may_conclude_a_third_thing(session, disputed):
    """Reading both pages may settle it as neither — the same latitude
    edit-then-accept has always given."""
    settle_dispute(
        session, disputed.id, "station_from", value="1103+00", principal=REVIEWER
    )

    assert disputes_for(session, disputed.id) == []
    session.refresh(disputed)
    assert disputed.station_from == "1103+00"


def test_settling_projects_onto_the_record_where_a_column_exists(
    session, disputed
):
    settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )
    session.refresh(disputed)
    assert disputed.station_from == "1105+00"


def test_a_later_claim_reopens_the_dispute(session, disputed):
    """The settlement covered the claims in front of the reviewer, and
    says so — a revision arriving afterwards is not covered by it."""
    settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )
    assert disputes_for(session, disputed.id) == []

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == disputed.id)
    ).first()
    session.add(
        Assertion(
            dependency_id=disputed.id,
            field_name="station_from",
            asserted_value="1108+40",
            evidence_link_id=link.id,
        )
    )
    session.flush()

    [dispute] = disputes_for(session, disputed.id)
    assert "1108+40" in dispute.values


def test_settling_an_undisputed_field_refuses(session, disputed):
    with pytest.raises(NoSuchDispute):
        settle_dispute(
            session, disputed.id, "utility_type", value="x", principal=REVIEWER
        )


def test_settling_is_a_human_act(session, disputed):
    with pytest.raises(InvalidHumanPrincipal):
        settle_dispute(
            session,
            disputed.id,
            "station_from",
            value="1105+00",
            principal="system:batch",
        )


def test_a_settlement_cannot_be_edited_after_the_fact(session, disputed):
    settlement = settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            DisputeSettlement.__table__.update()
            .where(DisputeSettlement.id == settlement.id)
            .values(covers_assertion_id=10**9)
        )


def test_an_unverified_claim_is_a_bad_citation_not_a_disagreement(
    session, project, disputed
):
    """Only verified assertions can disagree — the same predicate the
    engine applies."""
    document = session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).first()
    link = EvidenceLink(
        dependency_id=disputed.id,
        document_id=document.id,
        page_no=1,
        quote="unfound",
        verified=False,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=disputed.id,
            field_name="utility_type",
            asserted_value="Telecom",
            evidence_link_id=link.id,
        )
    )
    session.flush()

    assert [d.field_name for d in disputes_for(session, disputed.id)] == [
        "station_from"
    ]
