import pytest
from sqlalchemy import select

from corridor.adjudicate import AlreadyAdjudicated, accept_candidate
from corridor.db import Session, engine
from corridor.ledger import load_dependency
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)

FIELDS = {
    "utility_id": "FOC1-1",
    "external_org": "AT&T Texas (SWBT)",
    "utility_type": "Telecom",
    "station_from": "1149+00",
    "station_to": "1153+17",
    "sue_level": "B",
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
    project = Project(slug="adj-test", name="Adjudication Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=28,
    )
    session.add(doc)
    session.flush()
    return doc


def make_candidate(session, document, *, fields=None, verified=True, quote="FOC1-1 AT&T"):
    fields = FIELDS if fields is None else fields
    candidate = Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields,
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": verified,
                    "whole_row": True,
                }
            ],
            "confidence": 1.0,
            "dedupe_hint": "AT&T Texas (SWBT)|Telecom|1149+00-1153+17",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=verified,
    )
    session.add(candidate)
    session.flush()
    return candidate


def test_accepting_creates_a_dependency_from_the_candidate(session, document):
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, actor="bryce")

    # Our identifier, not the source's: NHHIP's matrix carries two distinct
    # conflicts both labelled FOC14-69.
    assert dep.ref_code == "DEP-00001"
    assert dep.source_ref == "FOC1-1"
    assert dep.dep_type == "utility_relocation"
    assert dep.station_from == "1149+00"
    assert dep.status == "identified"
    assert "AT&T Texas (SWBT)" in dep.title


def test_accepting_records_one_assertion_per_claimed_field(session, document):
    """The ledger row is a conclusion; the assertions are what sources said."""
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, actor="bryce")

    assertions = session.scalars(
        select(Assertion).where(Assertion.dependency_id == dep.id)
    ).all()
    assert {a.field_name for a in assertions} == set(FIELDS)
    assert all(a.evidence_link_id is not None for a in assertions)

    # `sue_level` has no Dependency column, but a source claimed it. Dropping
    # that is a loss of evidence, not a simplification.
    sue = next(a for a in assertions if a.field_name == "sue_level")
    assert sue.asserted_value == "B"


def test_accepting_links_the_evidence_with_its_quote(session, document):
    candidate = make_candidate(session, document, quote="FOC1-1 AT&T Texas (SWBT)")
    dep = accept_candidate(session, candidate, actor="bryce")

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dep.id)
    ).one()
    assert link.quote == "FOC1-1 AT&T Texas (SWBT)"
    assert link.page_no == 1
    assert link.verified is True
    # Acceptance never asserts readiness.
    assert link.satisfies_requirement is False


def test_acceptance_does_not_make_a_dependency_ready(session, document):
    """ADR-0002: readiness is proven separately, never a side effect."""
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, actor="bryce")
    assert load_dependency(session, dep.id).is_ready is False


def test_marking_evidence_as_satisfying_makes_it_ready(session, document):
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, actor="bryce")

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dep.id)
    ).one()
    link.satisfies_requirement = True
    session.flush()

    assert load_dependency(session, dep.id).is_ready is True


def test_unverified_evidence_can_never_confer_readiness(session, document):
    candidate = make_candidate(session, document, verified=False)
    dep = accept_candidate(session, candidate, actor="bryce")

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dep.id)
    ).one()
    link.satisfies_requirement = True
    session.flush()

    # A reviewer marked it sufficient, but the quote is not on the page.
    assert load_dependency(session, dep.id).is_ready is False


def test_the_external_party_is_resolved_and_reused(session, document):
    first = accept_candidate(session, make_candidate(session, document), actor="b")
    second = accept_candidate(
        session,
        make_candidate(session, document, fields={**FIELDS, "utility_id": "FOC1-2"}),
        actor="b",
    )
    assert first.external_org_id == second.external_org_id
    orgs = session.scalars(
        select(ExternalOrg).where(ExternalOrg.name == "AT&T Texas (SWBT)")
    ).all()
    assert len(orgs) == 1


def test_duplicate_source_ids_do_not_collide(session, document):
    """The 2/13/2026 matrix has two distinct conflicts both labelled FOC14-69.

    Keying the ledger on a source identifier would either collide or
    silently merge two genuinely different records.
    """
    dupe = {**FIELDS, "utility_id": "FOC14-69"}
    first = accept_candidate(session, make_candidate(session, document, fields=dupe), actor="b")
    second = accept_candidate(
        session,
        make_candidate(session, document, fields={**dupe, "station_from": "1124+26"}),
        actor="b",
    )
    assert first.source_ref == second.source_ref == "FOC14-69"
    assert first.ref_code != second.ref_code


def test_every_acceptance_writes_an_audit_entry(session, document):
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, actor="bryce")

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency", AuditLog.entity_id == dep.id
        )
    ).one()
    assert entry.actor == "bryce"
    assert entry.action == "accept_candidate"
    assert entry.after_json["ref_code"].startswith("DEP-")
    assert entry.after_json["source_ref"] == "FOC1-1"


def test_the_candidate_is_marked_accepted(session, document):
    candidate = make_candidate(session, document)
    accept_candidate(session, candidate, actor="bryce")
    assert candidate.state == "accepted"
    assert candidate.adjudicated_at is not None


def test_a_candidate_cannot_be_accepted_twice(session, document):
    candidate = make_candidate(session, document)
    accept_candidate(session, candidate, actor="bryce")
    with pytest.raises(AlreadyAdjudicated):
        accept_candidate(session, candidate, actor="bryce")


def test_competing_sources_are_both_kept_and_flagged(session, document):
    """The reason assertions exist at all (ADR-0001)."""
    dep = accept_candidate(session, make_candidate(session, document), actor="b")

    other = Document(
        project_id=document.project_id,
        sha256="b" * 64,
        filename="minutes-2026-03-04.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=3,
    )
    session.add(other)
    session.flush()
    link = EvidenceLink(
        dependency_id=dep.id,
        document_id=other.id,
        page_no=2,
        quote="AT&T relocation now at STA 1160+00",
        verified=True,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dep.id,
            field_name="station_from",
            asserted_value="1160+00",
            evidence_link_id=link.id,
        )
    )
    session.flush()

    view = load_dependency(session, dep.id)
    station = next(f for f in view.fields if f.name == "station_from")
    assert station.values == ["1149+00", "1160+00"]
    assert station.contradicted is True
    assert [f.name for f in view.contradictions] == ["station_from"]


def test_an_unverified_claim_is_not_a_contradiction(session, document):
    """A bad citation is a bad citation, not evidence that sources disagree."""
    dep = accept_candidate(session, make_candidate(session, document), actor="b")

    link = EvidenceLink(
        dependency_id=dep.id,
        document_id=document.id,
        page_no=9,
        quote="a quote that is not on the page",
        verified=False,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dep.id,
            field_name="station_from",
            asserted_value="9999+00",
            evidence_link_id=link.id,
        )
    )
    session.flush()

    station = next(
        f for f in load_dependency(session, dep.id).fields if f.name == "station_from"
    )
    assert station.contradicted is False


def test_the_view_carries_the_full_provenance_chain(session, document):
    dep = accept_candidate(session, make_candidate(session, document), actor="b")
    view = load_dependency(session, dep.id)

    assert view.org_name == "AT&T Texas (SWBT)"
    assert view.evidence and view.evidence[0][1].filename.endswith(".pdf")
    owner = next(f for f in view.fields if f.name == "external_org")
    claim = owner.assertions[0]
    assert claim.page_no == 1
    assert claim.filename == "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"
    assert claim.verified is True
