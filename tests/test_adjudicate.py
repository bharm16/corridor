import pytest
from sqlalchemy import select

from corridor.adjudicate import (
    RESOLUTION_VOCABULARIES,
    AlreadyAdjudicated,
    ResolutionVocabulary,
    accept_candidate,
    set_resolution_strategy,
)
from corridor.db import Session, engine
from corridor.ledger import load_dependency
from corridor.models import (
    CRITICAL_STRATEGIES,
    RESOLUTION_STRATEGIES,
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

    dep = accept_candidate(session, candidate, actor="reviewer")

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

    dep = accept_candidate(session, candidate, actor="reviewer")

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

    dep = accept_candidate(session, candidate, actor="reviewer")

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

    dep = accept_candidate(session, candidate, actor="reviewer")

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

    dep = accept_candidate(session, candidate, actor="reviewer")

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

    dep = accept_candidate(session, candidate, actor="reviewer")

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

    dep = accept_candidate(session, candidate, actor="reviewer")

    assert dep.resolution_strategy == "remove"


def test_a_reviewer_override_wins_and_is_audited(session, with_vocabulary):
    """The matrix may say `Retain and Protect` about a duct bank under the
    only haul road."""
    candidate = make_candidate(
        session,
        with_vocabulary,
        fields={**FIELDS, "resolution_strategy": "Retain and protect"},
    )
    dep = accept_candidate(session, candidate, actor="extractor")
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
    dep = accept_candidate(session, make_candidate(session, with_vocabulary), actor="x")

    with pytest.raises(ValueError, match="bulldoze"):
        set_resolution_strategy(session, dep, "bulldoze", actor="reviewer")


def test_the_column_has_no_default_at_any_level(session, document):
    """A default would be the column claiming something no document said —
    the failure ADR-0007 named and then committed."""
    dep = accept_candidate(session, make_candidate(session, document), actor="x")
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
    assert set(RESOLUTION_VOCABULARIES) == {"fdot-sr789", "wsdot-9424"}

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

    dep = accept_candidate(session, candidate, actor="reviewer")

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
