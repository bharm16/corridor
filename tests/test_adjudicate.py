from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Event
from uuid import uuid4

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from corridor.adjudicate import (
    MalformedCandidateShape,
    RESOLUTION_VOCABULARIES,
    _next_ref_code,
    AlreadyAdjudicated,
    CandidateAssertsNothing,
    InvalidCandidateProvenance,
    InvalidRejectReason,
    ResolutionVocabulary,
    UnadjudicableKind,
    accept_candidate,
    edit_candidate,
    reject_candidate,
    set_resolution_strategy,
)
from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import load_dependency
from corridor.models import (
    CRITICAL_STRATEGIES,
    RESOLUTION_STRATEGIES,
    Dependency,
    Assertion,
    AuditLog,
    Candidate,
    Document,
    DocPage,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.principals import HumanPrincipal

BRYCE = HumanPrincipal("local:bryce")
REVIEWER = HumanPrincipal("local:test-reviewer")
B = HumanPrincipal("local:b")
X = HumanPrincipal("local:x")
EXTRACTOR = HumanPrincipal("local:test-extractor")

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
    session.add(
        DocPage(
            document_id=doc.id,
            page_no=1,
            text=(
                "FOC1-1 AT&T Texas (SWBT) Telecom 1149+00 1153+17 B\n"
                "FOC14-69 AT&T Texas (SWBT) Telecom 1124+26 1153+17 B"
            ),
        )
    )
    session.flush()
    return doc


def make_candidate(
    session,
    document,
    *,
    fields=None,
    verified=True,
    quote="FOC1-1 AT&T",
    whole_row=True,
    unverified_fields=(),
    low_confidence_tokens=(),
    tier=None,
    activate=True,
):
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
                    "whole_row": whole_row,
                }
            ],
            "confidence": 1.0,
            "unverified_fields": list(unverified_fields),
            "low_confidence_tokens": list(low_confidence_tokens),
            "tier": tier,
            "dedupe_hint": "AT&T Texas (SWBT)|Telecom|1149+00-1153+17",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=(
            verified and not unverified_fields and not low_confidence_tokens
        ),
    )
    session.add(candidate)
    session.flush()
    if activate:
        _activate_fixture_candidate(session, document, candidate)
    return candidate


def _activate_fixture_candidate(session, document, candidate):
    """Record one complete immutable fixture extraction and activate it."""

    run = record_extraction_run(
        session,
        document,
        prompt_version=candidate.prompt_version,
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model=candidate.model,
    )
    declare_active_run(session, document.id, run.id, principal=BRYCE)
    return candidate


def test_accepting_creates_a_dependency_from_the_candidate(session, document):
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, principal=BRYCE)

    # Our identifier, not the source's: NHHIP's matrix carries two distinct
    # conflicts both labelled FOC14-69.
    assert dep.ref_code == "DEP-00001"
    assert dep.source_ref == "FOC1-1"
    assert dep.dep_type == "utility_relocation"
    assert dep.station_from == "1149+00"
    assert dep.status == "identified"
    assert "AT&T Texas (SWBT)" in dep.title


def test_database_refuses_to_move_a_candidate_source_across_projects(
    session, document
):
    other = Project(slug="adj-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray = Document(
        project_id=other.id,
        sha256="b" * 64,
        filename="other.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(stray)
    session.flush()

    candidate = make_candidate(session, document, activate=False)
    with pytest.raises(IntegrityError, match="Candidate run lineage is immutable"):
        with session.begin_nested():
            candidate.source_document_id = stray.id
            session.flush([candidate])


def test_accepting_refuses_a_candidate_citing_another_projects_document(
    session, document
):
    other = Project(slug="adj-other-citation", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray = Document(
        project_id=other.id,
        sha256="c" * 64,
        filename="other-citation.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(stray)
    session.flush()
    session.add(
        DocPage(document_id=stray.id, page_no=1, text="FOC1-1 AT&T Texas (SWBT)")
    )
    session.flush()

    candidate = make_candidate(session, document)
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                **candidate.payload_json["citations"][0],
                "document_id": stray.id,
            }
        ],
    }
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="citation document"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_refuses_a_candidate_with_a_missing_cited_page(session, document):
    candidate = make_candidate(session, document)
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                **candidate.payload_json["citations"][0],
                "page": 9,
            }
        ],
    }
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="cited page"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_refuses_a_candidate_with_non_mapping_payload(session, document):
    candidate = make_candidate(session, document)
    candidate.payload_json = []
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="payload must be an object"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_refuses_a_candidate_with_non_mapping_fields(session, document):
    candidate = make_candidate(session, document)
    candidate.payload_json = {**candidate.payload_json, "fields": []}
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="fields must be an object"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_refuses_a_candidate_with_non_list_citations(session, document):
    candidate = make_candidate(session, document)
    candidate.payload_json = {**candidate.payload_json, "citations": {"page": 1}}
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="citations must be a list"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_refuses_a_candidate_with_non_mapping_citation(session, document):
    candidate = make_candidate(session, document)
    candidate.payload_json = {**candidate.payload_json, "citations": ["bad"]}
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="citation must be an object"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_accepting_refuses_a_candidate_with_a_missing_quote_key(session, document):
    candidate = make_candidate(session, document)
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                key: value
                for key, value in candidate.payload_json["citations"][0].items()
                if key != "quote"
            }
        ],
    }
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="citation quote is missing"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def _ledger_counts(session):
    return (
        session.scalar(select(func.count()).select_from(EvidenceLink)),
        session.scalar(select(func.count()).select_from(Assertion)),
        session.scalar(select(func.count()).select_from(AuditLog)),
    )


@pytest.mark.parametrize(
    "payload_fields,label",
    [({}, "no fields"), (dict(FIELDS), "fields")],
)
@pytest.mark.parametrize("drop_key", [False, True], ids=["empty-list", "absent-key"])
def test_accepting_refuses_a_candidate_that_cites_nothing(
    session, document, payload_fields, label, drop_key
):
    """Every rule ran on each citation; none ran on the absence of one.

    The empty-fields half is the one that mattered. With a field to
    assert, the missing link surfaced as a NOT NULL violation and Postgres
    aborted the transaction, so nothing survived. With no fields there was
    no Assertion to write, nothing objected, and acceptance committed
    `DEP-00001 / "Utility" / utility_relocation` with no evidence at all.
    The quiet case was the durable one.
    """
    candidate = make_candidate(session, document)
    payload = {**candidate.payload_json, "fields": payload_fields}
    if drop_key:
        payload.pop("citations")
    else:
        payload["citations"] = []
    candidate.payload_json = payload
    before = _ledger_counts(session)

    with pytest.raises(InvalidCandidateProvenance, match="cites nothing"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert _ledger_counts(session) == before


@pytest.mark.parametrize(
    "payload_fields,label",
    [({}, "no fields"), (dict(FIELDS), "fields")],
)
@pytest.mark.parametrize("drop_key", [False, True], ids=["empty-list", "absent-key"])
def test_merging_refuses_a_candidate_that_cites_nothing(
    session, document, payload_fields, label, drop_key
):
    """Merge was the quieter path and the worse outcome.

    A citation-less merge added no link and no Assertion, then marked the
    Candidate `merged` and pointed `merged_into` at a Dependency it had
    contributed nothing to — with an audit entry saying it had. Nothing
    entered the Ledger and the Candidate left the queue for good, because
    merging refuses one that is no longer pending.

    Parametrized over the fields for the same reason acceptance is: with
    fields the pre-fix failure was the NOT NULL violation and without them
    it was silence, and a guard that reads the fields rather than the
    citations would still pass one of the two.
    """
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    candidate = make_candidate(session, document, fields={**FIELDS, "utility_id": "FOC1-9"})
    payload = {**candidate.payload_json, "fields": payload_fields}
    if drop_key:
        payload.pop("citations")
    else:
        payload["citations"] = []
    candidate.payload_json = payload
    before = _ledger_counts(session)

    with pytest.raises(InvalidCandidateProvenance, match="cites nothing"):
        merge_candidate(session, candidate, target, principal=REVIEWER)

    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert _ledger_counts(session) == before


@pytest.mark.parametrize(
    "fields",
    [{}, {"utility_id": "", "external_org": "   "}],
    ids=["no-fields", "blank-fields"],
)
def test_accepting_refuses_a_candidate_that_asserts_nothing(
    session, document, fields
):
    """Evidence for nothing is the mirror of a claim with no evidence.

    The blank-fields case is the same row by `is_claim`'s reading: a
    document declining to fill a cell asserts exactly as much as one with
    no cell, and the Ledger already answers "does this value say anything
    a source could disagree with" that way.
    """
    candidate = make_candidate(session, document, fields=fields)
    before = _ledger_counts(session)

    with pytest.raises(CandidateAssertsNothing, match="asserts nothing"):
        accept_candidate(session, candidate, principal=REVIEWER)

    assert candidate.state == "pending"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all() == []
    assert _ledger_counts(session) == before


def test_merging_a_candidate_that_asserts_nothing_keeps_its_evidence(
    session, document
):
    """The asymmetry, stated as behaviour.

    Acceptance builds the record, so a Candidate with no claim leaves
    nothing for it to be made of. A merge attaches to a record that
    already has its claims, and a Candidate carrying only a citation is a
    second document saying the conflict exists.
    """
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    corroborating = make_candidate(session, document, fields={})

    merge_candidate(session, corroborating, target, principal=REVIEWER)

    links = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == target.id)
    ).all()
    assert len(links) == 2
    assert corroborating.state == "merged"


def test_a_candidate_whose_only_citation_is_unverified_is_still_adjudicable(
    session, document
):
    """One citation, not one *verified* citation.

    Admission and readiness are different bars deliberately: an unverified
    quote sinks in the queue and is never filtered out of it, `bad-citation`
    exists so a reviewer makes that call, and readiness already refuses to
    rest on such a quote. The guard counts citations and reads nothing
    about them.
    """
    candidate = make_candidate(session, document, verified=False)

    dependency = accept_candidate(session, candidate, principal=REVIEWER)

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    assert link.verified is False
    assert candidate.citations_verified is False


def test_accepting_records_one_assertion_per_claimed_field(session, document):
    """The ledger row is a conclusion; the assertions are what sources said."""
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, principal=BRYCE)

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
    dep = accept_candidate(session, candidate, principal=BRYCE)

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
    dep = accept_candidate(session, candidate, principal=BRYCE)
    assert load_dependency(session, dep.id).is_ready is False


def test_marking_evidence_as_satisfying_makes_it_ready(session, document):
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, principal=BRYCE)

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dep.id)
    ).one()
    link.satisfies_requirement = True
    session.flush()

    assert load_dependency(session, dep.id).is_ready is True


def test_unverified_evidence_can_never_confer_readiness(session, document):
    candidate = make_candidate(session, document, verified=False)
    dep = accept_candidate(session, candidate, principal=BRYCE)

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dep.id)
    ).one()
    link.satisfies_requirement = True
    session.flush()

    # A reviewer marked it sufficient, but the quote is not on the page.
    assert load_dependency(session, dep.id).is_ready is False


def test_the_external_party_is_resolved_and_reused(session, document):
    first = accept_candidate(session, make_candidate(session, document), principal=B)
    second = accept_candidate(
        session,
        make_candidate(session, document, fields={**FIELDS, "utility_id": "FOC1-2"}),
        principal=B,
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
    first = accept_candidate(session, make_candidate(session, document, fields=dupe), principal=B)
    second = accept_candidate(
        session,
        make_candidate(session, document, fields={**dupe, "station_from": "1124+26"}),
        principal=B,
    )
    assert first.source_ref == second.source_ref == "FOC14-69"
    assert first.ref_code != second.ref_code


def test_accepting_after_a_gap_allocates_above_the_highest_existing_suffix(
    session, document
):
    session.add_all(
        [
            Dependency(
                project_id=document.project_id,
                ref_code="DEP-00001",
                dep_type="utility_relocation",
                title="gap one",
                status="identified",
            ),
            Dependency(
                project_id=document.project_id,
                ref_code="DEP-00003",
                dep_type="utility_relocation",
                title="gap three",
                status="identified",
            ),
        ]
    )
    session.flush()

    dep = accept_candidate(session, make_candidate(session, document), principal=B)

    assert dep.ref_code == "DEP-00004"


def test_ref_code_allocation_blocks_and_advances_across_two_sessions():
    setup = engine.connect()
    setup_tx = setup.begin()
    project_id = setup.execute(
        Project.__table__.insert()
        .values(
            slug=f"adj-race-{uuid4().hex}",
            name="Adjudication Race",
            is_synthetic=True,
        )
        .returning(Project.id)
    ).scalar_one()
    setup_tx.commit()
    setup.close()

    conn1 = engine.connect()
    tx1 = conn1.begin()
    session1 = Session(bind=conn1)
    ready = Event()

    def allocate_from_other_session() -> str:
        conn2 = engine.connect()
        tx2 = conn2.begin()
        session2 = Session(bind=conn2)
        try:
            ready.set()
            code = _next_ref_code(session2, project_id)
            tx2.rollback()
            return code
        finally:
            session2.close()
            conn2.close()

    try:
        first = _next_ref_code(session1, project_id)
        session1.add(
            Dependency(
                project_id=project_id,
                ref_code=first,
                dep_type="utility_relocation",
                title="first",
                status="identified",
            )
        )
        session1.flush()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(allocate_from_other_session)
            assert ready.wait(timeout=1)
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.2)

            tx1.commit()
            assert future.result(timeout=2) == "DEP-00002"
    finally:
        session1.close()
        conn1.close()
        cleanup = engine.connect()
        cleanup_tx = cleanup.begin()
        cleanup.execute(
            Dependency.__table__.delete().where(Dependency.project_id == project_id)
        )
        cleanup.execute(Project.__table__.delete().where(Project.id == project_id))
        cleanup_tx.commit()
        cleanup.close()


def test_every_acceptance_writes_an_audit_entry(session, document):
    candidate = make_candidate(session, document)
    dep = accept_candidate(session, candidate, principal=BRYCE)

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency", AuditLog.entity_id == dep.id
        )
    ).one()
    assert entry.actor == "local:bryce"
    assert entry.human_principal == "local:bryce"
    assert entry.action == "accept_candidate"
    assert entry.after_json["ref_code"].startswith("DEP-")
    assert entry.after_json["source_ref"] == "FOC1-1"


def test_the_candidate_is_marked_accepted(session, document):
    candidate = make_candidate(session, document)
    accept_candidate(session, candidate, principal=BRYCE)
    assert candidate.state == "accepted"
    assert candidate.adjudicated_at is not None


def test_a_candidate_cannot_be_accepted_twice(session, document):
    candidate = make_candidate(session, document)
    accept_candidate(session, candidate, principal=BRYCE)
    with pytest.raises(AlreadyAdjudicated):
        accept_candidate(session, candidate, principal=BRYCE)


def test_editing_replaces_the_payload_fields_and_writes_an_audit_entry(
    session, document
):
    candidate = make_candidate(session, document)
    original_payload = candidate.payload_json

    edited = {
        "utility_id": "FOC1-1",
        "external_org": "AT&T Texas",
        "station_from": "1150+00",
    }
    edit_candidate(session, candidate, edited, principal=REVIEWER)

    assert candidate.payload_json is not original_payload
    assert candidate.payload_json["fields"] == edited
    assert candidate.payload_json["citations"] == original_payload["citations"]
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "candidate",
            AuditLog.entity_id == candidate.id,
            AuditLog.action == "edit_candidate",
        )
    ).one()
    assert entry.before_json == {"fields": FIELDS}
    assert entry.after_json == {"fields": edited}


def test_editing_a_whole_row_candidate_revalidates_and_clears_resolved_diagnostics(
    session, document
):
    candidate = make_candidate(
        session,
        document,
        fields={**FIELDS, "station_from": "1092+00"},
        quote="FOC1-1 AT&T Texas (SWBT) Telecom 1149+00 1153+17 B",
        unverified_fields=["station_from"],
        low_confidence_tokens=["1092"],
    )

    edit_candidate(session, candidate, dict(FIELDS), principal=REVIEWER)

    assert candidate.payload_json["fields"] == FIELDS
    assert candidate.payload_json["unverified_fields"] == []
    assert candidate.payload_json["low_confidence_tokens"] == []
    assert candidate.citations_verified is True


def test_editing_a_whole_row_candidate_to_an_unsupported_value_stays_unverified(
    session, document
):
    candidate = make_candidate(
        session,
        document,
        fields={**FIELDS, "station_from": "1092+00"},
        quote="FOC1-1 AT&T Texas (SWBT) Telecom 1149+00 1153+17 B",
        unverified_fields=["station_from"],
        low_confidence_tokens=["1092"],
    )

    edited = {**FIELDS, "station_from": "9999+00"}
    edit_candidate(session, candidate, edited, principal=REVIEWER)

    assert candidate.payload_json["fields"] == edited
    assert candidate.payload_json["unverified_fields"] == ["station_from"]
    assert candidate.payload_json["low_confidence_tokens"] == []
    assert candidate.citations_verified is False


def test_editing_a_whole_row_candidate_fails_closed_when_any_cited_page_is_missing(
    session, document
):
    candidate = make_candidate(session, document)
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            *candidate.payload_json["citations"],
            {
                "document_id": document.id,
                "page": 2,
                "quote": "FOC1-1 AT&T Texas (SWBT) Telecom 1149+00 1153+17 B",
                "verified": True,
                "whole_row": True,
            },
        ],
    }
    edited = {**FIELDS, "station_from": "1150+00"}

    edit_candidate(session, candidate, edited, principal=REVIEWER)

    assert candidate.payload_json["fields"] == edited
    assert candidate.payload_json["unverified_fields"] == sorted(edited)
    assert candidate.citations_verified is False


def test_editing_a_non_whole_row_candidate_does_not_start_verbatim_field_checks(
    session, document
):
    candidate = make_candidate(
        session,
        document,
        whole_row=False,
        fields={**FIELDS, "station_from": "summary phrasing"},
        quote="AT&T confirmed relocation in August",
    )

    edited = {**FIELDS, "station_from": "a reviewer paraphrase"}
    edit_candidate(session, candidate, edited, principal=REVIEWER)

    assert candidate.payload_json["fields"] == edited
    assert candidate.payload_json["unverified_fields"] == []
    assert candidate.payload_json["low_confidence_tokens"] == []
    assert candidate.citations_verified is True


def test_a_transcribed_row_is_revalidated_even_when_its_quote_was_not_whole():
    """`whole_row` is not the question, and 79 live rows prove it.

    `best_verifiable_quote` falls back to the longest contiguous window
    whenever the assembled row is not printed contiguously — two tables
    side by side, a `Data Source` column landing elsewhere in reading
    order. The row is still transcribed cells; only the citation is
    narrower. Reading `whole_row` as "is this a transcription" let an
    edited value that appears nowhere on the page come back verified.
    """
    from corridor.adjudicate import _transcribes_cells

    fallback_quote_row = {
        "tier": "structure",
        "citations": [{"whole_row": False}],
    }
    prose_obligation = {"tier": None, "citations": [{"whole_row": False}]}
    legacy_matrix_row = {"citations": [{"whole_row": True}]}

    assert _transcribes_cells(fallback_quote_row) is True
    assert _transcribes_cells(prose_obligation) is False
    assert _transcribes_cells(legacy_matrix_row) is True


def test_editing_a_structure_tier_row_cited_by_a_partial_quote_is_revalidated(
    session, document
):
    candidate = make_candidate(
        session, document, tier="structure", whole_row=False, quote="FOC1-1 AT&T"
    )
    assert candidate.citations_verified is True

    edited = {**FIELDS, "station_from": "9999+99"}
    edit_candidate(session, candidate, edited, principal=REVIEWER)

    assert candidate.payload_json["unverified_fields"] == ["station_from"]
    assert candidate.citations_verified is False


def test_editing_a_structure_tier_row_clears_a_diagnostic_the_edit_resolved(
    session, document
):
    candidate = make_candidate(
        session,
        document,
        tier="structure",
        whole_row=False,
        quote="FOC1-1 AT&T",
        fields={**FIELDS, "station_from": "1l49+OO"},
        unverified_fields=("station_from",),
    )
    assert candidate.citations_verified is False

    edit_candidate(session, candidate, dict(FIELDS), principal=REVIEWER)

    assert candidate.payload_json["unverified_fields"] == []
    assert candidate.citations_verified is True


def test_rejecting_sets_the_decision_timestamp_and_records_the_reason(
    session, document
):
    candidate = make_candidate(session, document)

    reject_candidate(session, candidate, "duplicate", principal=REVIEWER)

    assert candidate.state == "rejected"
    assert candidate.adjudicated_at is not None
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "candidate",
            AuditLog.entity_id == candidate.id,
            AuditLog.action == "reject_candidate",
        )
    ).one()
    assert entry.after_json == {"reason": "duplicate"}


def test_rejecting_refuses_an_unknown_reason(session, document):
    candidate = make_candidate(session, document)

    with pytest.raises(InvalidRejectReason, match="because"):
        reject_candidate(session, candidate, "because", principal=REVIEWER)

    assert candidate.state == "pending"
    assert candidate.adjudicated_at is None


@pytest.mark.parametrize(
    ("operation", "args", "identity"),
    [
        (edit_candidate, ({"utility_id": "FOC1-1"},), {"principal": REVIEWER}),
        (reject_candidate, ("duplicate",), {"principal": REVIEWER}),
    ],
)
def test_edit_and_reject_refuse_an_already_adjudicated_candidate(
    session, document, operation, args, identity
):
    candidate = make_candidate(session, document)
    accept_candidate(session, candidate, principal=REVIEWER)

    with pytest.raises(AlreadyAdjudicated):
        operation(session, candidate, *args, **identity)


def test_merging_adds_assertions_without_creating_a_dependency(session, document):
    """Accepting a duplicate instead of merging is the unrecoverable error."""
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    before = len(
        session.scalars(select(Dependency).where(Dependency.project_id == document.project_id)).all()
    )

    second = make_candidate(
        session, document, fields={**FIELDS, "station_from": "1160+00"}
    )
    merged = merge_candidate(session, second, target, principal=B)

    after = session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all()
    assert merged.id == target.id
    assert len(after) == before
    assert second.state == "merged"
    assert second.merged_into == target.id


def test_merging_never_overwrites_the_targets_values(session, document):
    """ADR-0001: the ledger row is a conclusion, not the latest write."""
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    assert target.station_from == "1149+00"

    merge_candidate(
        session,
        make_candidate(session, document, fields={**FIELDS, "station_from": "1160+00"}),
        target,
        principal=B,
    )
    session.flush()
    assert target.station_from == "1149+00"


def test_a_merged_disagreement_becomes_a_contradiction(session, document):
    """The competing claim survives and is visible, rather than being lost."""
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    merge_candidate(
        session,
        make_candidate(session, document, fields={**FIELDS, "station_from": "1160+00"}),
        target,
        principal=B,
    )

    view = load_dependency(session, target.id)
    station = next(f for f in view.fields if f.name == "station_from")
    assert station.values == ["1149+00", "1160+00"]
    assert station.contradicted is True


def test_merging_is_audited(session, document):
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    second = make_candidate(session, document, fields={**FIELDS, "utility_id": "FOC1-9"})
    merge_candidate(session, second, target, principal=REVIEWER)

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "merge_candidate", AuditLog.entity_id == target.id
        )
    ).one()
    assert entry.after_json["candidate_id"] == second.id
    assert entry.after_json["merged_into"] == target.ref_code


def test_merging_refuses_a_candidate_from_another_project(session, document):
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    other = Project(slug="adj-merge-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray_doc = Document(
        project_id=other.id,
        sha256="d" * 64,
        filename="other-merge.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(stray_doc)
    session.flush()
    session.add(
        DocPage(document_id=stray_doc.id, page_no=1, text="FOC1-9 AT&T Texas (SWBT)")
    )
    session.flush()
    candidate = make_candidate(session, stray_doc)

    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="different project"):
        merge_candidate(session, candidate, target, principal=REVIEWER)

    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_merging_refuses_a_candidate_with_a_missing_quote_key(session, document):
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    candidate = make_candidate(session, document, fields={**FIELDS, "utility_id": "FOC1-9"})
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                key: value
                for key, value in candidate.payload_json["citations"][0].items()
                if key != "quote"
            }
        ],
    }
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(InvalidCandidateProvenance, match="citation quote is missing"):
        merge_candidate(session, candidate, target, principal=REVIEWER)

    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_merging_refuses_a_candidate_with_non_integer_citation_document_id(
    session, document
):
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    candidate = make_candidate(session, document, fields={**FIELDS, "utility_id": "FOC1-9"})
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                **candidate.payload_json["citations"][0],
                "document_id": {"id": document.id},
            }
        ],
    }
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(
        InvalidCandidateProvenance, match="citation document must be an integer"
    ):
        merge_candidate(session, candidate, target, principal=REVIEWER)

    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_merging_refuses_a_candidate_with_non_integer_cited_page(session, document):
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    candidate = make_candidate(session, document, fields={**FIELDS, "utility_id": "FOC1-9"})
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {
                **candidate.payload_json["citations"][0],
                "page": {"page": 1},
            }
        ],
    }
    before_assertions = session.scalar(select(func.count()).select_from(Assertion))
    before_evidence = session.scalar(select(func.count()).select_from(EvidenceLink))
    before_audit = session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(
        InvalidCandidateProvenance, match="cited page must be a positive integer"
    ):
        merge_candidate(session, candidate, target, principal=REVIEWER)

    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert session.scalar(select(func.count()).select_from(Assertion)) == before_assertions
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == before_evidence
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before_audit


def test_a_candidate_cannot_be_merged_twice(session, document):
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), principal=B)
    second = make_candidate(session, document)
    merge_candidate(session, second, target, principal=B)
    with pytest.raises(AlreadyAdjudicated):
        merge_candidate(session, second, target, principal=B)


def test_competing_sources_are_both_kept_and_flagged(session, document):
    """The reason assertions exist at all (ADR-0001)."""
    dep = accept_candidate(session, make_candidate(session, document), principal=B)

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
    dep = accept_candidate(session, make_candidate(session, document), principal=B)

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
    dep = accept_candidate(session, make_candidate(session, document), principal=B)
    view = load_dependency(session, dep.id)

    assert view.org_name == "AT&T Texas (SWBT)"
    assert view.evidence and view.evidence[0][1].filename.endswith(".pdf")
    owner = next(f for f in view.fields if f.name == "external_org")
    claim = owner.assertions[0]
    assert claim.page_no == 1
    assert claim.filename == "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"
    assert claim.verified is True



# --------------------------------- resolution strategy (#96, ADR-0009)


@pytest.fixture
def with_vocabulary(monkeypatch, session, document):
    """A project whose layout has an identified resolution vocabulary.

    Built here rather than borrowed from the shipped table: `projects.slug`
    is unique and `fdot-sr789` is a real row, so the mechanism is exercised
    with a local vocabulary and the shipped one is covered separately by
    `test_the_shipped_vocabulary_covers_sr789_and_nothing_else`.
    """
    project = session.get(Project, document.project_id)
    monkeypatch.setitem(
        RESOLUTION_VOCABULARIES,
        project.slug,
        ResolutionVocabulary(
            phrases={
                "to be removed": "remove",
                "to be relocated": "relocate",
                "to be adjusted to proposed grade": "adjust_vertical",
                "retain and protect": "protect_in_place",
            }
        ),
    )
    return document


def strategy_assertions(session, dep):
    return session.scalars(
        select(Assertion).where(
            Assertion.dependency_id == dep.id,
            Assertion.field_name == "resolution_strategy",
        )
    ).all()


def test_a_document_saying_the_facility_moves_produces_a_critical_dependency(
    session, with_vocabulary
):
    candidate = make_candidate(
        session, with_vocabulary, fields={**FIELDS, "resolution_strategy": "To be removed"}
    )

    dep = accept_candidate(session, candidate, principal=REVIEWER)

    assert dep.resolution_strategy == "remove"
    assert load_dependency(session, dep.id).is_critical is True


def test_a_document_saying_the_facility_stays_is_not_critical(
    session, with_vocabulary
):
    candidate = make_candidate(
        session,
        with_vocabulary,
        fields={**FIELDS, "resolution_strategy": "To be adjusted to proposed grade"},
    )

    dep = accept_candidate(session, candidate, principal=REVIEWER)

    assert dep.resolution_strategy == "adjust_vertical"
    assert load_dependency(session, dep.id).is_critical is False


def test_reading_a_strategy_does_not_make_the_row_contradict_itself(
    session, with_vocabulary
):
    """The single sharpest trap in this change.

    `criticality` was never a field the extractor emitted, so adjudication
    wrote it an Assertion by hand. `resolution_strategy` **is** one, and the
    ordinary per-field loop already records what the document printed. A
    second, canonical-token Assertion beside it would put `To be removed`
    and `remove` under one `field_name` behind the same verified
    EvidenceLink — which is exactly how a CONTRADICTION is computed, so
    every mapped SR 789 row would contradict itself at a MISSING_EVIDENCE exception.
    """
    candidate = make_candidate(
        session, with_vocabulary, fields={**FIELDS, "resolution_strategy": "To be removed"}
    )

    dep = accept_candidate(session, candidate, principal=REVIEWER)

    claims = strategy_assertions(session, dep)
    assert len(claims) == 1
    # The Assertion preserves what the source said; the column holds the
    # conclusion drawn from it (ADR-0001).
    assert claims[0].asserted_value == "To be removed"
    assert dep.resolution_strategy == "remove"

    view = load_dependency(session, dep.id)
    assert [f.name for f in view.contradictions] == []


def test_a_document_that_records_no_strategy_asserts_none(session, with_vocabulary):
    """Most of the corpus. An inventory says conflicts exist, never how they
    resolve — Project A's 3,235 rows and SH 99's 1,401 assert nothing."""
    candidate = make_candidate(session, with_vocabulary)

    dep = accept_candidate(session, candidate, principal=REVIEWER)

    assert dep.resolution_strategy is None
    assert strategy_assertions(session, dep) == []
    assert load_dependency(session, dep.id).is_critical is False


def test_a_layout_with_no_identified_vocabulary_does_not_guess(session, document):
    """`document`'s project is deliberately absent from the table.

    The row carries a resolution phrase the extractor captured, and it is
    still not read: prose that means one thing on one form means another
    elsewhere, which is what #85 paid for.
    """
    candidate = make_candidate(
        session, document, fields={**FIELDS, "resolution_strategy": "To be removed"}
    )

    dep = accept_candidate(session, candidate, principal=REVIEWER)

    assert dep.resolution_strategy is None
    assert load_dependency(session, dep.id).is_critical is False


def test_a_phrase_the_vocabulary_does_not_carry_asserts_nothing(
    session, with_vocabulary
):
    """SR 789 prints `To be adjusted or relocated` — two answers on opposite
    sides of the line. The document has not settled it, so neither does the
    Ledger."""
    candidate = make_candidate(
        session,
        with_vocabulary,
        fields={**FIELDS, "resolution_strategy": "To be adjusted or relocated"},
    )

    dep = accept_candidate(session, candidate, principal=REVIEWER)

    assert dep.resolution_strategy is None
    # The document's prose survives as evidence even though no conclusion
    # was drawn from it.
    assert strategy_assertions(session, dep)[0].asserted_value == (
        "To be adjusted or relocated"
    )


def test_whitespace_in_a_printed_cell_does_not_defeat_the_vocabulary(
    session, with_vocabulary
):
    """Internal runs of whitespace are an artifact of reading the cell, not
    something the document said."""
    candidate = make_candidate(
        session, with_vocabulary, fields={**FIELDS, "resolution_strategy": "To  be\nremoved "}
    )

    dep = accept_candidate(session, candidate, principal=REVIEWER)

    assert dep.resolution_strategy == "remove"


def test_a_reviewer_override_wins_and_is_audited(session, with_vocabulary):
    """The matrix may say `Retain and Protect` about a duct bank under the
    only haul road."""
    candidate = make_candidate(
        session,
        with_vocabulary,
        fields={**FIELDS, "resolution_strategy": "Retain and protect"},
    )
    dep = accept_candidate(session, candidate, principal=EXTRACTOR)
    assert dep.resolution_strategy == "protect_in_place"

    set_resolution_strategy(session, dep, "relocate", actor="reviewer")

    assert dep.resolution_strategy == "relocate"
    assert load_dependency(session, dep.id).is_critical is True
    # The document still says what it said.
    assert strategy_assertions(session, dep)[0].asserted_value == "Retain and protect"
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == dep.id,
            AuditLog.action == "set_resolution_strategy",
        )
    ).one()
    assert entry.actor == "reviewer"
    assert (entry.before_json, entry.after_json) == (
        {"resolution_strategy": "protect_in_place"},
        {"resolution_strategy": "relocate"},
    )


def test_a_reviewer_cannot_invent_a_strategy_outside_the_vocabulary(
    session, with_vocabulary
):
    dep = accept_candidate(session, make_candidate(session, with_vocabulary), principal=X)

    with pytest.raises(ValueError, match="bulldoze"):
        set_resolution_strategy(session, dep, "bulldoze", actor="reviewer")


def test_the_column_has_no_default_at_any_level(session, document):
    """A default would be the column claiming something no document said —
    the failure ADR-0007 named and then committed."""
    dep = accept_candidate(session, make_candidate(session, document), principal=X)
    session.flush()
    session.refresh(dep)

    assert dep.resolution_strategy is None
    assert Dependency.__table__.c.resolution_strategy.server_default is None
    assert Dependency.__table__.c.resolution_strategy.nullable is True
    assert "criticality" not in Dependency.__table__.c


def test_criticality_is_derived_from_the_strategy_and_never_stored():
    """The set and the gold set's labelling rule are one sentence (ADR-0009).

    If they diverge the M7 gate scores one definition against another. This
    pins the Ledger side; `eval._critical` is the label side.
    """
    from corridor.models import is_critical

    assert CRITICAL_STRATEGIES == {"relocate", "remove", "abandon_in_place"}
    for strategy in CRITICAL_STRATEGIES:
        assert is_critical(strategy) is True
    for strategy in set(RESOLUTION_STRATEGIES) - CRITICAL_STRATEGIES:
        assert is_critical(strategy) is False
    assert is_critical(None) is False


def test_the_shipped_vocabulary_covers_the_two_layouts_that_state_one(session):
    """The table is what a human edits, so its contents are the test.

    Asserted as a whole: a new project entry should have to argue with this
    test and ADR-0009 behind it. Two layouts in the corpus print a
    resolution strategy — SR 789 as prose, 9424 as four marked columns
    (#105). Projects A and C print none and must stay absent, because a
    project absent here asserts no strategy at all, which is the right
    answer for a document that records none rather than a gap.
    """
    assert set(RESOLUTION_VOCABULARIES) == {
        "fdot-sr789",
        "wsdot-9424",
        "wsdot-9540",
    }

    vocabulary = RESOLUTION_VOCABULARIES["fdot-sr789"]
    assert vocabulary.read("To be removed") == "remove"
    assert vocabulary.read("To be relocated") == "relocate"
    assert vocabulary.read("To be adjusted to proposed grade") == "adjust_vertical"
    # Declined on purpose — the document offers two answers, a condition,
    # or an adjustment it does not say is vertical. ADR-0009's Brown is
    # specifically "adjusted **vertically** … same horizontal alignment",
    # and this layout prints the specific sibling separately.
    for undecided in (
        "To be adjusted or relocated",
        "To be monitored and adjusted as needed",
        "To be monitored and adjusted",
        'To be replaced with 24"X36" handhole and adjusted to proposed grade',
        "To be adjusted",
    ):
        assert vocabulary.read(undecided) is None


def test_the_ledger_and_the_gold_labels_agree_on_what_critical_means():
    """The one divergence that would be invisible in the gate's number.

    `is_critical` decides the Ledger side; `eval._critical` reads the gold
    set's `critical` column, which is the label side, and the M7 gate
    scores one against the other. ADR-0009 says outright that the two are
    one sentence — so a token of the deleted `critical | high | normal`
    scale surviving in the eval's accepted labels would let a gold set be
    written in a vocabulary the Ledger no longer has.
    """
    from corridor.eval import CRITICAL_FALSE, CRITICAL_TRUE

    # The labelling rule is a boolean about a row, not a strategy name:
    # gold sets say yes/no, and the mapping from strategy to yes/no lives
    # in `CRITICAL_STRATEGIES` alone.
    assert CRITICAL_TRUE & CRITICAL_FALSE == frozenset()
    assert not (CRITICAL_TRUE | CRITICAL_FALSE) & set(RESOLUTION_STRATEGIES), (
        "a gold label must not be spelled as a strategy name — the two "
        "vocabularies are different questions and sharing a token invites "
        "a gold set that reads as neither"
    )
    # `normal` was a value of the scale ADR-0009 deleted. It survives here
    # only as a falsy gold label, which is fine, but it must never be
    # readable as a strategy.
    assert "normal" not in RESOLUTION_STRATEGIES


def test_a_placeholder_owner_never_becomes_an_external_party(session, document):
    """`NA` is the document declining to name an owner (#77).

    Minting an ExternalOrg for it puts a party in the ledger that nobody
    can chase, and 86 of Project A's rows would join it. The Assertion
    still records what the document printed — dropping that would lose
    evidence — but the Dependency is left unowned, which is true.
    """
    candidate = make_candidate(
        session, document, fields={**FIELDS, "external_org": "NA"}
    )

    dep = accept_candidate(session, candidate, principal=REVIEWER)

    assert dep.external_org_id is None
    assert session.scalars(
        select(ExternalOrg).where(ExternalOrg.name == "NA")
    ).first() is None
    # The document said `NA`, and the record still says it said so.
    claim = session.scalars(
        select(Assertion).where(
            Assertion.dependency_id == dep.id,
            Assertion.field_name == "external_org",
        )
    ).one()
    assert claim.asserted_value == "NA"


# ------------- a strategy spelled as more than one answer (#105, ADR-0009)


def test_two_answers_that_agree_read_as_that_strategy():
    """Eleven of 9424's rows are marked `509` and `ST Relocation Needed`.

    Two marks, one answer: both columns say the facility relocates, and
    which agency's project pays for it is not a resolution strategy.
    """
    vocabulary = RESOLUTION_VOCABULARIES["wsdot-9424"]

    assert (
        vocabulary.read("509 Relocation Needed; ST Relocation Needed") == "relocate"
    )


def test_two_answers_that_differ_read_as_no_strategy():
    """One row of 9424 is marked under both `ST Relocation Needed` and
    `Retain and Protect` — opposite sides of ADR-0009's line.

    This is the same situation SR 789 spells as `To be adjusted or
    relocated`, which the FDOT vocabulary declines for exactly this reason:
    the document offers two answers and has settled on neither. Resolving
    it here would assert a strategy the document does not.
    """
    vocabulary = RESOLUTION_VOCABULARIES["wsdot-9424"]

    assert vocabulary.read("ST Relocation Needed; Retain and Protect") is None


def test_an_answer_the_vocabulary_cannot_read_sinks_the_whole_value():
    """A phrase nobody has identified is not a phrase that can be outvoted.

    `Retain and Protect` beside something unrecognised is not a
    protect-in-place row: the unread half may be an answer from the other
    side of the line. None is the only honest reading.
    """
    vocabulary = RESOLUTION_VOCABULARIES["wsdot-9424"]

    assert vocabulary.read("Retain and Protect; Some New Column") is None


def test_a_single_answer_reads_exactly_as_it_did():
    """Projects A, B and C go through the same code path and must not move."""
    vocabulary = RESOLUTION_VOCABULARIES["fdot-sr789"]

    assert vocabulary.read("To be removed") == "remove"
    assert vocabulary.read("To be adjusted or relocated") is None
    assert vocabulary.read("") is None
    assert vocabulary.read(None) is None


def test_a_marked_layout_and_a_prose_layout_agree_on_the_canonical_value():
    """#105's first acceptance criterion, as one assertion.

    WSDOT prints `509 Relocation Needed` as a marked column and FDOT prints
    `To be relocated` as prose. They are the same claim about the same
    thing, and the Ledger stores one token for both — which is what makes
    critical recall computable across layouts at all (ADR-0009).
    """
    marked = RESOLUTION_VOCABULARIES["wsdot-9424"].read("509 Relocation Needed")
    prose = RESOLUTION_VOCABULARIES["fdot-sr789"].read("To be relocated")

    assert marked == prose == "relocate"


def test_the_wsdot_vocabulary_reads_9424s_four_columns():
    """Written from 9424 before any document of this layout is measured.

    9424 is the unsealed twin, fetched for exactly this (ADR-0008). Each
    reading is ADR-0009's published table, not an inference:

      relocation  -> relocate          FDOT Red, WSDOT `Relocation Needed`
      retain      -> protect_in_place  FDOT Green, WSDOT `Retain and Protect`
      abandon     -> abandon_in_place  FDOT Red — deactivation is scheduled
                                       utility-owner work, not a facility
                                       that stays
    """
    vocabulary = RESOLUTION_VOCABULARIES["wsdot-9424"]

    assert vocabulary.read("509 Relocation Needed") == "relocate"
    assert vocabulary.read("ST Relocation Needed") == "relocate"
    assert vocabulary.read("Retain and Protect") == "protect_in_place"
    assert vocabulary.read("Abandon / Deactivate") == "abandon_in_place"


def test_three_of_wsdots_four_columns_are_critical_and_one_is_not():
    """The gate's number turns on this split, so it is asserted directly.

    `relocate` and `abandon_in_place` are FDOT Red; `protect_in_place` is
    Green. A vocabulary that put all four on one side would give the M7
    gate a critical set of everything, which ADR-0007 already named as a
    gate that cannot catch the failure it exists for.
    """
    from corridor.models import is_critical

    vocabulary = RESOLUTION_VOCABULARIES["wsdot-9424"]
    critical = {
        heading: is_critical(vocabulary.read(heading))
        for heading in (
            "509 Relocation Needed",
            "ST Relocation Needed",
            "Retain and Protect",
            "Abandon / Deactivate",
        )
    }

    assert critical == {
        "509 Relocation Needed": True,
        "ST Relocation Needed": True,
        "Retain and Protect": False,
        "Abandon / Deactivate": True,
    }


def test_the_wsdot_vocabulary_reads_both_contracts_columns():
    """The M7 cold run's finding, pinned.

    9540 prints the same Appendix U with its columns named after nothing
    in particular — `RELOCATION`, `PROTECTION IN PLACE`, `ABANDON/
    DEACTIVATE/ REMOVE` — where 9424 names them after its route number.
    The vocabulary was written from 9424 alone, so all 192 of 9540's rows
    read as None: the structural machinery generalised and the words did
    not. One layout, one vocabulary, both contracts' spellings.
    """
    vocabulary = RESOLUTION_VOCABULARIES["wsdot-9540"]

    assert vocabulary is RESOLUTION_VOCABULARIES["wsdot-9424"]
    assert vocabulary.read("RELOCATION") == "relocate"
    assert vocabulary.read("ADVANCE RELOCATION") == "relocate"
    assert vocabulary.read("PROTECTION IN PLACE") == "protect_in_place"
    assert vocabulary.read("ABANDON/ DEACTIVATE") == "abandon_in_place"
    assert vocabulary.read("ABANDON/ DEACTIVATE/ REMOVE") == "abandon_in_place"


def test_a_9540_row_marked_on_both_sides_still_settles_nothing():
    """12 of its rows are marked RELOCATION *and* PROTECTION IN PLACE.
    Two answers from opposite sides of ADR-0009's line: the document has
    not settled, and the Ledger records none."""
    vocabulary = RESOLUTION_VOCABULARIES["wsdot-9540"]

    assert vocabulary.read("RELOCATION; PROTECTION IN PLACE") is None
    # Two answers on the same side still settle.
    assert vocabulary.read("RELOCATION; ABANDON/ DEACTIVATE/ REMOVE") is None


def make_event_candidate(session, document):
    """What `make minutes` writes: a commitment read off meeting notes."""
    candidate = Candidate(
        project_id=document.project_id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "description": "AT&T confirmed relocation NTP in August",
                "committed_date": "2026-08-14",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "AT&T confirmed relocation NTP in August",
                    "verified": True,
                }
            ],
            "confidence": 1.0,
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="minutes_v1",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    _activate_fixture_candidate(session, document, candidate)
    return candidate


def test_accepting_an_event_is_refused_rather_than_faked(session, document):
    """An event Candidate carries nothing a Dependency is made of.

    Acceptance used to read no kind at all, so it built a
    `utility_relocation` titled "Utility" out of a commitment about a
    conflict that already exists — a Ledger record no document describes,
    entering by the one path that exists to stop exactly that.
    """
    candidate = make_event_candidate(session, document)

    with pytest.raises(UnadjudicableKind, match="event"):
        accept_candidate(session, candidate, principal=BRYCE)

    assert candidate.state == "pending"
    assert (
        session.scalars(
            select(Dependency).where(Dependency.project_id == document.project_id)
        ).all()
        == []
    )


# --- The agreement materializer (#170) --------------------------------------

AGREEMENT_FIELDS = {
    "title": "Signal maintenance at the IH 69 crossings",
    "external_org": "City of Houston",
    "obligation": "The City maintains the traffic signals listed in Exhibit A",
    "committed_date": "1996-01-31",
    "evidence_required": "executed amendment listing the affected signals",
}


@pytest.fixture
def agreement_document(session, document):
    doc = Document(
        project_id=document.project_id,
        sha256="b" * 64,
        filename="city-of-houston-signal-agreement-1-31-1996-executed.pdf",
        doc_type="agreement",
        parse_status="parsed",
        pages=25,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocPage(
            document_id=doc.id,
            page_no=1,
            text="The City maintains the traffic signals listed in Exhibit A",
        )
    )
    session.flush()
    return doc


def agreement_candidate(session, document, *, fields=None):
    fields = AGREEMENT_FIELDS if fields is None else fields
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
                    "quote": "The City maintains the traffic signals",
                    "verified": True,
                    "whole_row": False,
                }
            ],
            "confidence": 1.0,
            "dedupe_hint": "City of Houston|agreement|signal maintenance",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="agreement_v3",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    _activate_fixture_candidate(session, document, candidate)
    return candidate


def test_accepting_an_agreement_obligation_states_what_it_obligated(
    session, agreement_document
):
    candidate = agreement_candidate(session, agreement_document)

    dependency = accept_candidate(session, candidate, principal=BRYCE)

    assert dependency.dep_type == "agreement"
    assert dependency.title == AGREEMENT_FIELDS["title"]
    assert dependency.committed_date == date(1996, 1, 31)
    assert dependency.evidence_required == AGREEMENT_FIELDS["evidence_required"]
    assert dependency.notes == AGREEMENT_FIELDS["obligation"]
    assert dependency.station_from is None
    assert dependency.station_to is None
    assert dependency.source_ref is None
    assert dependency.resolution_strategy is None
    assert dependency.external_org_id is not None


def test_a_matrix_candidate_still_materializes_as_a_relocation(
    session, document
):
    candidate = make_candidate(session, document)
    dependency = accept_candidate(session, candidate, principal=BRYCE)
    assert dependency.dep_type == "utility_relocation"
    assert dependency.source_ref == "FOC1-1"


def test_an_agreement_shape_missing_its_obligation_refuses(
    session, agreement_document
):
    fields = {k: v for k, v in AGREEMENT_FIELDS.items() if k != "obligation"}
    candidate = agreement_candidate(session, agreement_document, fields=fields)

    with pytest.raises(MalformedCandidateShape, match="obligation"):
        accept_candidate(session, candidate, principal=BRYCE)
    session.refresh(candidate)
    assert candidate.state == "pending"


def test_an_unparseable_committed_date_refuses_rather_than_guessing(
    session, agreement_document
):
    fields = dict(AGREEMENT_FIELDS, committed_date="next spring")
    candidate = agreement_candidate(session, agreement_document, fields=fields)

    with pytest.raises(MalformedCandidateShape, match="committed_date"):
        accept_candidate(session, candidate, principal=BRYCE)


def test_a_dependency_candidate_from_an_unmaterializable_source_refuses(
    session, document
):
    plan = Document(
        project_id=document.project_id,
        sha256="c" * 64,
        filename="sue-quality-level-a.pdf",
        doc_type="plan",
        parse_status="parsed",
        pages=1,
    )
    session.add(plan)
    session.flush()
    session.add(DocPage(document_id=plan.id, page_no=1, text="FOC1-1 AT&T"))
    session.flush()
    candidate = Candidate(
        project_id=plan.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": dict(FIELDS),
            "citations": [
                {
                    "document_id": plan.id,
                    "page": 1,
                    "quote": "FOC1-1 AT&T",
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "confidence": 1.0,
            "dedupe_hint": "x",
        },
        source_document_id=plan.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    _activate_fixture_candidate(session, plan, candidate)

    with pytest.raises(MalformedCandidateShape, match="plan"):
        accept_candidate(session, candidate, principal=BRYCE)


def test_an_agreement_shape_missing_its_title_refuses(
    session, agreement_document
):
    fields = {k: v for k, v in AGREEMENT_FIELDS.items() if k != "title"}
    candidate = agreement_candidate(session, agreement_document, fields=fields)

    with pytest.raises(MalformedCandidateShape, match="title"):
        accept_candidate(session, candidate, principal=BRYCE)


def test_agreement_acceptance_still_writes_assertions_and_evidence(
    session, agreement_document
):
    """The typed columns are conclusions; the claims beneath them survive."""
    candidate = agreement_candidate(session, agreement_document)

    dependency = accept_candidate(session, candidate, principal=BRYCE)

    links = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).all()
    assert len(links) == 1
    assert links[0].verified is True
    asserted = {
        assertion.field_name
        for assertion in session.scalars(
            select(Assertion).where(Assertion.dependency_id == dependency.id)
        )
    }
    assert "obligation" in asserted
    assert "committed_date" in asserted
