import pytest
from sqlalchemy import select

from corridor.adjudicate import (
    CRITICALITY_SIGNALS,
    AlreadyAdjudicated,
    CriticalitySignal,
    accept_candidate,
    set_criticality,
)
from corridor.db import Session, engine
from corridor.ledger import load_dependency
from corridor.models import (
    Dependency,
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


def test_merging_adds_assertions_without_creating_a_dependency(session, document):
    """Accepting a duplicate instead of merging is the unrecoverable error."""
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), actor="b")
    before = len(
        session.scalars(select(Dependency).where(Dependency.project_id == document.project_id)).all()
    )

    second = make_candidate(
        session, document, fields={**FIELDS, "station_from": "1160+00"}
    )
    merged = merge_candidate(session, second, target, actor="b")

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

    target = accept_candidate(session, make_candidate(session, document), actor="b")
    assert target.station_from == "1149+00"

    merge_candidate(
        session,
        make_candidate(session, document, fields={**FIELDS, "station_from": "1160+00"}),
        target,
        actor="b",
    )
    session.flush()
    assert target.station_from == "1149+00"


def test_a_merged_disagreement_becomes_a_contradiction(session, document):
    """The competing claim survives and is visible, rather than being lost."""
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), actor="b")
    merge_candidate(
        session,
        make_candidate(session, document, fields={**FIELDS, "station_from": "1160+00"}),
        target,
        actor="b",
    )

    view = load_dependency(session, target.id)
    station = next(f for f in view.fields if f.name == "station_from")
    assert station.values == ["1149+00", "1160+00"]
    assert station.contradicted is True


def test_merging_is_audited(session, document):
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), actor="b")
    second = make_candidate(session, document, fields={**FIELDS, "utility_id": "FOC1-9"})
    merge_candidate(session, second, target, actor="reviewer")

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "merge_candidate", AuditLog.entity_id == target.id
        )
    ).one()
    assert entry.after_json["candidate_id"] == second.id
    assert entry.after_json["merged_into"] == target.ref_code


def test_a_candidate_cannot_be_merged_twice(session, document):
    from corridor.adjudicate import merge_candidate

    target = accept_candidate(session, make_candidate(session, document), actor="b")
    second = make_candidate(session, document)
    merge_candidate(session, second, target, actor="b")
    with pytest.raises(AlreadyAdjudicated):
        merge_candidate(session, second, target, actor="b")


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


# ------------------------------------------- criticality (#86, ADR-0007)


@pytest.fixture
def signalled(monkeypatch, session, document):
    """A project whose layout has an identified signal.

    The signal is built here rather than borrowed from the shipped table,
    which is empty by design (ADR-0009) — these tests cover the mechanism
    that reads a signal, and `test_the_shipped_signal_table_is_empty`
    covers what is actually shipped.
    """
    project = session.get(Project, document.project_id)
    monkeypatch.setitem(
        CRITICALITY_SIGNALS,
        project.slug,
        CriticalitySignal(
            field="potential_conflict",
            critical=frozenset({"y", "yes"}),
            not_critical=frozenset({"n", "no", "a"}),
        ),
    )
    return document


def criticality_assertions(session, dep):
    return session.scalars(
        select(Assertion).where(
            Assertion.dependency_id == dep.id, Assertion.field_name == "criticality"
        )
    ).all()


def test_a_document_saying_the_facility_must_move_produces_a_critical_dependency(
    session, signalled
):
    candidate = make_candidate(
        session, signalled, fields={**FIELDS, "potential_conflict": "Y"}
    )

    dep = accept_candidate(session, candidate, actor="reviewer")

    assert dep.criticality == "critical"


def test_the_criticality_cites_the_same_evidence_as_the_other_fields(
    session, signalled
):
    """An ordinary adjudicated field: the document said this, on this page."""
    candidate = make_candidate(
        session, signalled, fields={**FIELDS, "potential_conflict": "Y"}
    )

    dep = accept_candidate(session, candidate, actor="reviewer")

    claim = criticality_assertions(session, dep)
    owner = session.scalars(
        select(Assertion).where(
            Assertion.dependency_id == dep.id, Assertion.field_name == "external_org"
        )
    ).one()
    assert len(claim) == 1
    assert claim[0].asserted_value == "critical"
    assert claim[0].evidence_link_id == owner.evidence_link_id


def test_a_document_saying_otherwise_produces_a_dependency_that_is_not_critical(
    session, signalled
):
    candidate = make_candidate(
        session, signalled, fields={**FIELDS, "potential_conflict": "N"}
    )

    dep = accept_candidate(session, candidate, actor="reviewer")

    assert dep.criticality == "normal"
    assert criticality_assertions(session, dep)[0].asserted_value == "normal"


def test_a_document_that_says_nothing_asserts_no_criticality(session, signalled):
    """Silence is not `normal`.

    `potential_conflict` is absent from 1,249 of Project A's rows — the
    column appears in only three of five revisions. Recording those as
    `normal` collapses "the document did not say" into "the document said
    it does not matter", and makes the M7 gate's denominator a lie.
    """
    candidate = make_candidate(session, signalled)

    dep = accept_candidate(session, candidate, actor="reviewer")

    assert dep.criticality is None
    assert criticality_assertions(session, dep) == []


def test_a_layout_with_no_identified_signal_does_not_guess(session, document):
    """`document`'s project is deliberately absent from the signal table.

    The row carries a `potential_conflict` the extractor happened to
    capture, and it is still not read: SH 99 fills that same canonical
    field from a column meaning something else entirely (#85).
    """
    candidate = make_candidate(
        session, document, fields={**FIELDS, "potential_conflict": "Y"}
    )

    dep = accept_candidate(session, candidate, actor="reviewer")

    assert dep.criticality is None
    assert criticality_assertions(session, dep) == []


def test_an_unrecognised_value_asserts_nothing_rather_than_normal(
    session, signalled
):
    """The column being present is not the document making a claim."""
    candidate = make_candidate(
        session, signalled, fields={**FIELDS, "potential_conflict": "TBD"}
    )

    dep = accept_candidate(session, candidate, actor="reviewer")

    assert dep.criticality is None
    assert criticality_assertions(session, dep) == []


def test_a_reviewer_override_wins_and_stays_distinguishable(session, signalled):
    """The matrix may say `N` about a duct bank under the only haul road.

    The document's claim survives as an Assertion; the stored value is the
    reviewer's conclusion. The two disagreeing is what makes the override
    visible rather than silent.
    """
    candidate = make_candidate(
        session, signalled, fields={**FIELDS, "potential_conflict": "N"}
    )
    dep = accept_candidate(session, candidate, actor="extractor")

    set_criticality(session, dep, "critical", actor="reviewer")

    assert dep.criticality == "critical"
    assert criticality_assertions(session, dep)[0].asserted_value == "normal"
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == dep.id, AuditLog.action == "set_criticality"
        )
    ).one()
    assert entry.actor == "reviewer"
    assert (entry.before_json, entry.after_json) == (
        {"criticality": "normal"},
        {"criticality": "critical"},
    )


def test_a_reviewer_cannot_invent_a_criticality_outside_the_enum(session, signalled):
    dep = accept_candidate(session, make_candidate(session, signalled), actor="x")

    with pytest.raises(ValueError, match="urgent"):
        set_criticality(session, dep, "urgent", actor="reviewer")


def test_the_hardcode_is_gone_not_defaulted_differently(session, document):
    """The column has no default at any level.

    A server-side default would reinstate the hardcode silently: every
    insert omitting the column would land on `normal` again, and nothing
    in the code would say so.
    """
    dep = accept_candidate(session, make_candidate(session, document), actor="x")
    session.flush()
    session.refresh(dep)

    assert dep.criticality is None
    assert Dependency.__table__.c.criticality.server_default is None
    assert Dependency.__table__.c.criticality.nullable is True


def test_the_shipped_signal_table_is_empty(session):
    """Nothing in this corpus asserts a criticality (ADR-0009).

    `nhhip-3c2` was listed here until the research in ADR-0009: its
    `Potential Conflict (Yes, No, Abandoned)` column says a conflict
    exists, never how it resolves, and reading `Y` as critical marked 71%
    of Project A on a claim the document does not make. The layouts that
    do record a resolution strategy — FDOT and WSDOT — need the schema
    change, not an entry here.

    Asserted as a whole rather than key by key: a new entry should have to
    argue with this test and the ADR behind it.
    """
    assert CRITICALITY_SIGNALS == {}
