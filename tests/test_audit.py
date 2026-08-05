"""The audit trail, as a property rather than a docstring.

`models.AuditLog` says "every ledger mutation writes here". Six sites
built the row by hand and one mutating module built none at all, so the
claim was unfalsifiable — there was no interface to walk.
"""

import pytest
from sqlalchemy import select

from corridor import audit
from corridor.adjudicate import accept_candidate, merge_candidate, set_resolution_strategy
from corridor.db import Session, engine
from corridor.ledger import load_dependency, mark_satisfies
from corridor.milestones import link_dependency
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    Document,
    EvidenceLink,
    Milestone,
    Project,
)

FIELDS = {
    "utility_id": "FOC1-1",
    "external_org": "AT&T Texas",
    "utility_type": "Telecom",
    "station_from": "1149+00",
}


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
def document(session):
    project = Project(slug="audit-test", name="Audit Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="a9" * 32,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    return doc


def make_candidate(session, document, *, utility_id="FOC1-1"):
    c = Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {**FIELDS, "utility_id": utility_id},
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": f"{utility_id} AT&T Texas Telecom",
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=True,
    )
    session.add(c)
    session.flush()
    return c


def entries(session, dependency_id):
    return [e.action for e in audit.trail_for_dependency(session, dependency_id)]


def test_an_unknown_entity_is_refused(session):
    """Free-text `entity_type` is how the write and the read drifted."""
    with pytest.raises(ValueError, match="evidence_link"):
        audit.record(
            session,
            actor="tester",
            action="x",
            entity_type="evidence_link",
            entity_id=1,
        )


def test_every_ledger_mutation_writes_an_entry(session, document):
    """One test walks the mutating entry points.

    Adding a mutation without an audit entry now fails here rather than
    being noticed years later by someone reading a record with a hole in
    its history.
    """
    dependency = accept_candidate(
        session, make_candidate(session, document), actor="tester"
    )
    assert entries(session, dependency.id) == ["accept_candidate"]

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    mark_satisfies(session, dependency.id, link.id, actor="tester")

    set_resolution_strategy(session, dependency, "relocate", actor="tester")

    milestone = Milestone(
        project_id=document.project_id, code="UTIL-CLEAR", name="Utility clearance"
    )
    session.add(milestone)
    session.flush()
    link_dependency(session, dependency, milestone, actor="tester")

    merge_candidate(
        session,
        make_candidate(session, document, utility_id="FOC1-2"),
        dependency,
        actor="tester",
    )

    assert entries(session, dependency.id) == [
        "accept_candidate",
        "mark_satisfies_requirement",
        "set_resolution_strategy",
        "link_milestone",
        "merge_candidate",
    ]
    assert {e.actor for e in audit.trail_for_dependency(session, dependency.id)} == {
        "tester"
    }


def test_relinking_a_milestone_records_the_date_it_moved(session, document):
    """`need_date` drives DUE_SOON and ORPHAN and lands in every recorded
    run, so a relink moves published numbers. It used to write nothing."""
    from datetime import date

    dependency = accept_candidate(
        session, make_candidate(session, document), actor="tester"
    )
    first = Milestone(
        project_id=document.project_id,
        code="M1",
        name="First",
        need_date=date(2026, 6, 1),
    )
    second = Milestone(
        project_id=document.project_id,
        code="M2",
        name="Second",
        need_date=date(2026, 9, 1),
    )
    session.add_all([first, second])
    session.flush()

    link_dependency(session, dependency, first, actor="tester")
    link_dependency(session, dependency, second, actor="scheduler")

    relinks = [
        e
        for e in audit.trail_for_dependency(session, dependency.id)
        if e.action == "link_milestone"
    ]
    assert [e.actor for e in relinks] == ["tester", "scheduler"]
    assert relinks[1].before_json["need_date"] == "2026-06-01"
    assert relinks[1].after_json["need_date"] == "2026-09-01"
    assert relinks[1].after_json["milestone_code"] == "M2"


def test_the_edit_trail_appears_on_the_record_it_produced(session, document):
    """The defect this seam closes.

    A reviewer edits before the Dependency exists, so the entry is written
    against the Candidate — and the detail view queried only `dependency`
    entries. What the extractor originally said, which the edit-accept
    route's docstring promises survives, was unreachable from the record.
    """
    candidate = make_candidate(session, document)
    audit.record(
        session,
        actor="reviewer",
        action="edit_candidate",
        entity_type=audit.CANDIDATE,
        entity_id=candidate.id,
        before={"fields": {"utility_id": "FOC1-l"}},
        after={"fields": {"utility_id": "FOC1-1"}},
    )
    dependency = accept_candidate(session, candidate, actor="reviewer")

    assert entries(session, dependency.id) == ["edit_candidate", "accept_candidate"]
    assert load_dependency(session, dependency.id).audit[0].before_json == {
        "fields": {"utility_id": "FOC1-l"}
    }


def test_another_record_s_candidate_history_does_not_leak_in(session, document):
    """The join is the candidate id this Dependency actually resolved."""
    mine = make_candidate(session, document)
    theirs = make_candidate(session, document, utility_id="FOC9-9")
    audit.record(
        session,
        actor="reviewer",
        action="edit_candidate",
        entity_type=audit.CANDIDATE,
        entity_id=theirs.id,
        after={"fields": {}},
    )
    dependency = accept_candidate(session, mine, actor="reviewer")

    assert entries(session, dependency.id) == ["accept_candidate"]
