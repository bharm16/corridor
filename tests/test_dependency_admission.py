"""Dependency admission: the conflicts a project's matrices can anchor.

ADR-0029: nothing is signed and nothing is unlocked. A conflict enters
when the revisions that state it can anchor it — one matrix is enough —
and where several state it identically the newest is admitted with the
rest merged as corroboration. What the machine cannot anchor abstains
and waits for Adjudication, which is the only thing a human is asked to
do here.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.dependency_admission import (
    DEPENDENCY_ADMISSION_POLICY_VERSION,
    run_dependency_admission,
)
from corridor.extraction_runs import (
    declare_single_run_documents_by_policy,
    record_extraction_run,
)
from corridor.models import (
    Assertion,
    Candidate,
    Dependency,
    DependencyAdmissionOutcome,
    PolicyRun,
    DocPage,
    Document,
    Project,
)
from corridor.principals import HumanPrincipal

OPERATOR = HumanPrincipal("local:dependency-admission-operator")
PIPELINE = "Tejas Pipeline Co"


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
    p = Project(
        slug="dependency-admission-test",
        name="Dependency Admission Test",
        is_synthetic=True,
    )
    session.add(p)
    session.flush()
    return p


def _document(session, project, *, filename):
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
    session.add(DocPage(document_id=document.id, page_no=1, text="rows"))
    session.flush()
    return document


def _fields(uid, *, org=PIPELINE, station="1102+20"):
    return {
        "utility_id": uid,
        "external_org": org,
        "utility_type": "Petroleum and Gaseous Materials",
        "baseline": "SR-BL",
        "station_from": station,
        "station_to": station,
    }


def _candidate(document, fields, *, verified=True):
    quote = " | ".join(str(v) for v in fields.values() if v is not None)
    return Candidate(
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
            "dedupe_hint": quote,
            "text_source": "text_layer",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="matrix_v1",
        model="gpt-test",
        citations_verified=verified,
    )


def _run(session, document, candidates):
    for c in candidates:
        session.add(c)
    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix_v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="gpt-test",
        schema_version="matrix_candidate_shape_v1",
    )
    session.flush()
    return run


def _corpus(session, project, feb_rows, may_rows, *, verified=True):
    feb = _document(session, project, filename="ucm-feb.pdf")
    may = _document(session, project, filename="ucm-may.pdf")
    feb_c = [_candidate(feb, f, verified=verified) for f in feb_rows]
    may_c = [_candidate(may, f, verified=verified) for f in may_rows]
    _run(session, feb, feb_c)
    _run(session, may, may_c)
    declare_single_run_documents_by_policy(session, project.id)
    return feb, may, feb_c, may_c


# ── One matrix is enough ─────────────────────────────────────────────────


def test_a_single_matrix_puts_its_conflicts_on_the_record(session, project):
    """The whole product, in one test: documents land, and the list is
    there. No signature, no second revision, no unlock."""
    only = _document(session, project, filename="ucm.pdf")
    rows = [_fields("PL1"), _fields("PL2", station="1200+00")]
    _run(session, only, [_candidate(only, f) for f in rows])
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 2
    assert result.abstained_count == 0
    assert {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"PL1", "PL2"}


def test_a_row_only_one_revision_states_still_admits(session, project):
    """A revision that never mentions a row does not withhold it."""
    feb, may, *_ = _corpus(
        session, project, [_fields("PL1"), _fields("PL2")], [_fields("PL1")]
    )
    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 2  # PL1 agrees, PL2 stands on February
    assert result.abstained_count == 0


def test_an_undeclared_document_contributes_nothing(session, project):
    """The policy reads declared work only, so a document whose reading is
    ambiguous is absent rather than guessed at."""
    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    stray = _document(session, project, filename="stray.pdf")
    _run(session, stray, [_candidate(stray, _fields("PL9"))])
    _run(session, stray, [_candidate(stray, _fields("PL9"))])

    declarations = declare_single_run_documents_by_policy(session, project.id)
    assert declarations.ambiguous == ["stray.pdf"]

    result = run_dependency_admission(session, project.id)
    assert {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"PL1"}
    assert result.abstained_count == 0


def test_only_matrix_documents_are_read_for_conflicts(session, project):
    """Minutes state what people said, never what the conflicts are."""
    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    minutes = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"minutes").hexdigest(),
        filename="minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(minutes)
    session.flush()
    session.add(DocPage(document_id=minutes.id, page_no=1, text="rows"))
    session.flush()
    _run(session, minutes, [_candidate(minutes, _fields("PL7"))])
    declare_single_run_documents_by_policy(session, project.id)

    run_dependency_admission(session, project.id)
    assert {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"PL1"}


# ── Agreement, disagreement, ambiguity ───────────────────────────────────


def test_exact_agreement_admits_and_merges_the_sibling(session, project):
    feb, may, feb_c, may_c = _corpus(
        session, project, [_fields("PL1")], [_fields("PL1")]
    )

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 1
    assert result.abstained_count == 0

    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    assert dependency.source_ref == "PL1"

    # The primary comes from the newest revision; the sibling merges so
    # its citation attaches as corroboration.
    session.refresh(feb_c[0])
    session.refresh(may_c[0])
    assert may_c[0].state == "accepted"
    assert feb_c[0].state == "merged"
    assert feb_c[0].merged_into == dependency.id

    assertions = session.scalars(
        select(Assertion).where(Assertion.dependency_id == dependency.id)
    ).all()
    links = {a.evidence_link_id for a in assertions}
    assert len(links) == 2  # both documents stand behind the record


def test_disagreeing_revisions_admit_the_row_and_dispute_the_field(
    session, project
):
    """A disagreement rides on the row rather than withholding it
    (ADR-0031): both claims land cited to their own pages, and the field
    is contradicted."""
    from corridor.disputes import disputes_for

    feb, may, feb_c, may_c = _corpus(
        session,
        project,
        [_fields("PL1", station="1102+20")],
        [_fields("PL1", station="1105+00")],
    )

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 1
    assert result.abstained_count == 0

    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    session.refresh(feb_c[0])
    session.refresh(may_c[0])
    assert may_c[0].state == "accepted"  # the newest is the primary
    assert feb_c[0].state == "merged"

    [dispute] = [
        d
        for d in disputes_for(session, dependency.id)
        if d.field_name == "station_from"
    ]
    assert set(dispute.values) == {"1102+20", "1105+00"}
    # Each claim is readable on the page that made it.
    assert {c.document_filename for c in dispute.claims} == {
        "ucm-feb.pdf",
        "ucm-may.pdf",
    }


def test_revisions_naming_different_parties_still_abstain(session, project):
    """Whether these are one conflict is not a field dispute; merging
    two parties' rows would answer it by accident."""
    feb, may, feb_c, may_c = _corpus(
        session,
        project,
        [_fields("PL1", org=PIPELINE)],
        [_fields("PL1", org="Someone Else Entirely")],
    )

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {
        "revisions_disagree_on_party"
    }
    session.refresh(feb_c[0])
    assert feb_c[0].state == "pending"


def test_duplicate_rows_in_one_revision_abstain(session, project):
    feb, may, *_ = _corpus(
        session,
        project,
        [_fields("PL1"), _fields("PL1")],
        [_fields("PL1")],
    )

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {
        "multiple_rows_in_agreement_document"
    }


def test_an_already_admitted_reference_is_not_admitted_twice(session, project):
    feb, may, feb_c, may_c = _corpus(
        session, project, [_fields("PL1")], [_fields("PL1")]
    )
    run_dependency_admission(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    assert result.abstained_count == 0  # nothing pending — zero outcomes
    assert (
        len(
            session.scalars(
                select(Dependency).where(Dependency.project_id == project.id)
            ).all()
        )
        == 1
    )


def test_unverified_citations_abstain(session, project):
    feb, may, *_ = _corpus(
        session, project, [_fields("PL1")], [_fields("PL1")], verified=False
    )
    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {"citations_unverified"}


# ── The receipt ──────────────────────────────────────────────────────────


def test_the_run_is_an_immutable_receipt_of_exact_outcomes(session, project):
    nameless = dict(_fields("", station="1300+00"))
    feb, may, feb_c, may_c = _corpus(
        session,
        project,
        [_fields("PL1"), _fields("PL2", station="1200+00"), nameless],
        [_fields("PL1"), _fields("PL2", station="1201+00")],
    )

    result = run_dependency_admission(session, project.id)
    run = session.get(PolicyRun, result.run_id)
    assert run.policy_approval_id is None  # nothing was signed
    assert run.policy_version == DEPENDENCY_ADMISSION_POLICY_VERSION
    # PL1 agrees and PL2 disputes; both are on the record, and the row
    # that could not be named at all is what abstains.
    assert (run.applied_count, run.abstained_count) == (2, 1)

    outcomes = session.scalars(
        select(DependencyAdmissionOutcome).where(
            DependencyAdmissionOutcome.policy_run_id == run.id
        )
    ).all()
    by_outcome = {}
    for o in outcomes:
        by_outcome.setdefault(o.outcome, []).append(o)
    assert set(by_outcome) == {"admitted", "merged", "abstained"}
    assert by_outcome["admitted"][0].dependency_id is not None
    assert by_outcome["merged"][0].dependency_id is not None
    assert by_outcome["abstained"][0].reason is not None

    with pytest.raises(IntegrityError):
        session.execute(
            update(PolicyRun)
            .where(PolicyRun.id == run.id)
            .values(applied_count=99)
        )


def test_the_receipt_pins_the_documents_and_the_deployed_checks(
    session, project
):
    """Nothing is signed, so the receipt carries the whole replay claim:
    which revisions were read, at which bytes, under which checks."""
    from corridor import dependency_admission as module
    from corridor import policy

    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    result = run_dependency_admission(session, project.id)
    run = session.get(PolicyRun, result.run_id)

    expected = module._canonical_policy(session, project, [feb.id, may.id])
    assert {d["document_id"]: d["sha256"] for d in expected["agreement_documents"]} == {
        feb.id: feb.sha256,
        may.id: may.sha256,
    }
    assert run.policy_sha256 == policy.canonical_sha256(expected)


def test_an_edited_check_changes_the_digest_the_receipt_records(
    session, project, monkeypatch
):
    """The authorization that used to pause on an edited check is gone;
    what remains is that two runs under different checks can never claim
    the same digest, so a replay reads honestly."""
    from corridor import dependency_admission as module

    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    before = run_dependency_admission(session, project.id)
    before_sha = session.get(PolicyRun, before.run_id).policy_sha256

    real = module._rule_source_bytes

    def edited():
        return tuple(
            (name, source + b"\n# edited\n") for name, source in real()
        )

    monkeypatch.setattr(module, "_rule_source_bytes", edited)
    after = run_dependency_admission(session, project.id)
    assert session.get(PolicyRun, after.run_id).policy_sha256 != before_sha


def test_the_run_reads_the_currently_declared_active_run(session, project):
    """Re-declaring a document's Active Run changes what admits next —
    the policy follows the declaration rather than pinning an old one."""
    from corridor.extraction_runs import declare_active_run

    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    second = _run(session, feb, [_candidate(feb, _fields("PL5"))])
    declare_active_run(session, feb.id, second.id, principal=OPERATOR)

    result = run_dependency_admission(session, project.id)
    # February now reads PL5; May still reads PL1. Both stand alone, and
    # both are on the record.
    assert result.admitted_count == 2
    assert {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"PL1", "PL5"}


# ── Lineage and failure ──────────────────────────────────────────────────


def test_policy_admitted_records_carry_attributable_lineage(session, project):
    """Reconfirmation and Carry-Forward read admission lineage from the
    audit log; a policy-admitted record must be as legible there as a
    human-accepted one, backed by its durable receipt."""
    from corridor import audit as audit_module

    feb, may, feb_c, may_c = _corpus(
        session, project, [_fields("PL1")], [_fields("PL1")]
    )
    run_dependency_admission(session, project.id)

    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    records = audit_module.admission_records_for_dependencies(
        session, [dependency.id]
    )[dependency.id]
    assert len(records) == 2  # the primary and the merged sibling
    assert {r.candidate_id for r in records} == {feb_c[0].id, may_c[0].id}
    assert all(r.attributable for r in records)


def test_a_write_refusal_abstains_without_sinking_the_batch(
    session, project, monkeypatch
):
    from corridor import dependency_admission as module
    from corridor.adjudicate import CandidateAssertsNothing

    feb, may, feb_c, may_c = _corpus(
        session,
        project,
        [_fields("PL1"), _fields("PL2", station="1200+00")],
        [_fields("PL1"), _fields("PL2", station="1200+00")],
    )

    real = module.admit_dependency_by_policy

    def refusing(session_, primary, siblings, *, machine_actor):
        uid = (primary.payload_json or {}).get("fields", {}).get("utility_id")
        if uid == "PL1":
            raise CandidateAssertsNothing("synthetic refusal for the test")
        return real(session_, primary, siblings, machine_actor=machine_actor)

    monkeypatch.setattr(module, "admit_dependency_by_policy", refusing)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 1  # PL2 landed despite PL1's refusal
    refused = [a for a in result.abstentions if a.reason == "write_refused"]
    assert {a.candidate_id for a in refused} == {feb_c[0].id, may_c[0].id}

    run = session.get(PolicyRun, result.run_id)
    assert run.applied_count == 1
    assert run.abstained_count == len(result.abstentions)


# ── Row identity under a declared numbering scheme (ADR-0030) ────────────


def _per_party_document(session, project, *, filename="fdot-ucm.pdf"):
    document = _document(session, project, filename=filename)
    document.numbering_scheme = "per-party"
    session.flush()
    return document


def test_a_per_party_matrix_names_rows_by_party_and_number(session, project):
    """Nine parties each correctly have a conflict 1. Declaring the
    scheme puts them all on the record as nine conflicts."""
    document = _per_party_document(session, project)
    rows = [
        _fields("1", org="AT&T TCA"),
        _fields("1", org="Comcast", station="1200+00"),
        _fields("1", org="TECO Peoples Gas", station="1300+00"),
        _fields("2", org="AT&T TCA", station="1400+00"),
    ]
    _run(session, document, [_candidate(document, f) for f in rows])
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 4
    assert result.abstained_count == 0
    admitted = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all()
    assert sorted(d.source_ref for d in admitted) == ["1", "1", "1", "2"]


def test_the_same_party_repeating_a_number_still_abstains(session, project):
    """A genuinely duplicated row must not become two conflicts."""
    document = _per_party_document(session, project)
    rows = [
        _fields("1", org="AT&T TCA"),
        _fields("1", org="AT&T TCA", station="1200+00"),
    ]
    _run(session, document, [_candidate(document, f) for f in rows])
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {
        "multiple_rows_in_agreement_document"
    }


def test_an_undeclared_document_never_infers_the_scheme(session, project):
    """Repeated numbers under the default abstain visibly; the machine
    must never read repetition as a numbering style."""
    document = _document(session, project, filename="undeclared.pdf")
    rows = [
        _fields("1", org="AT&T TCA"),
        _fields("1", org="Comcast", station="1200+00"),
    ]
    _run(session, document, [_candidate(document, f) for f in rows])
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {
        "multiple_rows_in_agreement_document"
    }


def test_a_per_party_row_with_no_stated_party_has_no_name(session, project):
    document = _per_party_document(session, project)
    fields = _fields("1", org="AT&T TCA")
    nameless = dict(_fields("2", station="1200+00"), external_org="")
    _run(
        session,
        document,
        [_candidate(document, fields), _candidate(document, nameless)],
    )
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 1
    assert {a.reason for a in result.abstentions} == {"no_row_identity"}


def test_per_party_agreement_between_revisions_merges_by_the_pair(
    session, project
):
    feb = _per_party_document(session, project, filename="ucm-feb.pdf")
    may = _per_party_document(session, project, filename="ucm-may.pdf")
    feb_c = [
        _candidate(feb, _fields("1", org="AT&T TCA")),
        _candidate(feb, _fields("1", org="Comcast", station="1200+00")),
    ]
    may_c = [
        _candidate(may, _fields("1", org="AT&T TCA")),
        _candidate(may, _fields("1", org="Comcast", station="1200+00")),
    ]
    _run(session, feb, feb_c)
    _run(session, may, may_c)
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 2
    assert result.abstained_count == 0
    for candidate in feb_c:
        session.refresh(candidate)
        assert candidate.state == "merged"


def test_the_record_blocks_a_second_admission_of_the_same_pair_only(
    session, project
):
    """AT&T's 1 on the record does not block Comcast's 1 from entering —
    and does block AT&T's 1 from entering twice."""
    from corridor.models import ExternalOrg

    first = _per_party_document(session, project, filename="first.pdf")
    _run(session, first, [_candidate(first, _fields("1", org="AT&T TCA"))])
    declare_single_run_documents_by_policy(session, project.id)
    assert run_dependency_admission(session, project.id).admitted_count == 1

    second = _per_party_document(session, project, filename="second.pdf")
    _run(
        session,
        second,
        [
            _candidate(second, _fields("1", org="AT&T TCA")),
            _candidate(second, _fields("1", org="Comcast", station="1200+00")),
        ],
    )
    from corridor.extraction_runs import declare_single_run_documents_by_policy as declare
    declare(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 1  # Comcast's 1
    assert {a.reason for a in result.abstentions} == {"already_admitted"}
    parties = {
        session.get(ExternalOrg, d.external_org_id).name
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    }
    assert parties == {"AT&T TCA", "Comcast"}


def test_declaring_a_scheme_changes_the_digest_the_receipt_records(
    session, project
):
    """The scheme decides what counts as one row, so it is part of what
    the receipt claims ran."""
    document = _document(session, project, filename="ucm.pdf")
    _run(session, document, [_candidate(document, _fields("PL1"))])
    declare_single_run_documents_by_policy(session, project.id)

    before = run_dependency_admission(session, project.id)
    before_sha = session.get(PolicyRun, before.run_id).policy_sha256

    document.numbering_scheme = "per-party"
    session.flush()
    after = run_dependency_admission(session, project.id)
    assert session.get(PolicyRun, after.run_id).policy_sha256 != before_sha


def test_a_replaced_revision_is_not_read_at_all(session, project):
    """Every other path refuses a replaced revision's rows, so including
    one here did not corroborate — the write refused and took the current
    revision's own row down with it. Measured on NHHIP: 211 admitted
    where 688 should have been."""
    from corridor.supersession import SupersessionDeclaration, register_supersessions
    from datetime import date

    feb = _document(session, project, filename="ucm-feb.pdf")
    may = _document(session, project, filename="ucm-may.pdf")
    feb.registry_id, may.registry_id = "ucm-feb", "ucm-may"
    session.flush()
    _run(session, feb, [_candidate(feb, _fields("SHARED")),
                        _candidate(feb, _fields("FEBONLY", station="1200+00"))])
    _run(session, may, [_candidate(may, _fields("SHARED")),
                        _candidate(may, _fields("MAYONLY", station="1300+00"))])
    declare_single_run_documents_by_policy(session, project.id)
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="ucm-feb",
                successor_registry_id="ucm-may",
                replacement_date=date(2025, 5, 5),
                source_registry_id="ucm-may",
                source_page=1,
            )
        ],
        project_id=project.id,
    )
    session.flush()

    result = run_dependency_admission(session, project.id)

    # The current revision lands whole. Nothing is refused, and SHARED —
    # which the replaced revision also states — is not dragged down.
    assert result.abstained_count == 0
    assert {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"SHARED", "MAYONLY"}


def test_one_party_under_two_registered_names_is_one_conflict(
    session, project
):
    """An External Party is known by many names — that is what its
    aliases are for. Keying on the stated spelling made one company's two
    registered names two records with the same number, each invisible to
    the other's already-admitted check, and split the disagreement
    between them across both rows instead of raising it as a Dispute."""
    from corridor.disputes import disputes_for
    from corridor.models import ExternalOrg

    org = ExternalOrg(
        name="Zeta Cable Co TEST", aliases=["Zeta Cable Company TEST"]
    )
    session.add(org)
    session.flush()

    feb = _per_party_document(session, project, filename="alias-feb.pdf")
    may = _per_party_document(session, project, filename="alias-may.pdf")
    _run(session, feb, [_candidate(feb, _fields("1", org="Zeta Cable Co TEST"))])
    _run(
        session,
        may,
        [
            _candidate(
                may,
                _fields("1", org="Zeta Cable Company TEST", station="9999+00"),
            )
        ],
    )
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)

    assert result.admitted_count == 1
    assert result.abstained_count == 0
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    # And the station disagreement is a Dispute on that one row, not two
    # records each holding half of it.
    assert "station_from" in [
        d.field_name for d in disputes_for(session, dependency.id)
    ]


def test_identical_party_disagreement_abstention_does_not_duplicate_outcomes(
    session, project
):
    first_document = _document(session, project, filename="party-a.pdf")
    second_document = _document(session, project, filename="party-b.pdf")
    _run(
        session,
        first_document,
        [_candidate(first_document, _fields("SHARED", org="Party A"))],
    )
    _run(
        session,
        second_document,
        [_candidate(second_document, _fields("SHARED", org="Party B"))],
    )
    declare_single_run_documents_by_policy(session, project.id)

    first = run_dependency_admission(session, project.id)
    after_first = session.scalars(
        select(DependencyAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == DependencyAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            DependencyAdmissionOutcome.reason == "revisions_disagree_on_party",
        )
    ).all()
    second = run_dependency_admission(session, project.id)
    after_second = session.scalars(
        select(DependencyAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == DependencyAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            DependencyAdmissionOutcome.reason == "revisions_disagree_on_party",
        )
    ).all()

    assert first.abstained_count == 2
    assert second.abstained_count == 0
    assert len(after_first) == 2
    assert len(after_second) == 2


def test_an_unregistered_spelling_stays_its_own_party(session, project):
    """Nothing guesses. A spelling nobody has registered surfaces as a
    row to look at rather than silently merging two companies."""
    feb = _per_party_document(session, project, filename="unreg-feb.pdf")
    may = _per_party_document(session, project, filename="unreg-may.pdf")
    _run(session, feb, [_candidate(feb, _fields("1", org="Aardvark Gas TEST"))])
    _run(
        session,
        may,
        [_candidate(may, _fields("1", org="Aardvark Gas Company TEST"))],
    )
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 2  # two parties, as far as anyone knows


def test_a_registered_alias_binds_to_its_party_not_a_duplicate(
    session, project
):
    """The write path resolves registered spellings exactly as the key
    does. Before this, a row stating an alias minted a duplicate
    External Party, and the already-admitted check — which reads the
    bound party's names — could never see the carrier again."""
    from corridor.models import ExternalOrg

    org = ExternalOrg(
        name="Zeta Cable Co WTEST", aliases=["Zeta Cable Company WTEST"]
    )
    session.add(org)
    session.flush()

    first = _per_party_document(session, project, filename="w-feb.pdf")
    _run(
        session,
        first,
        [_candidate(first, _fields("1", org="Zeta Cable Company WTEST"))],
    )
    declare_single_run_documents_by_policy(session, project.id)
    assert run_dependency_admission(session, project.id).admitted_count == 1

    admitted = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    assert admitted.external_org_id == org.id  # no duplicate party minted

    second = _per_party_document(session, project, filename="w-may.pdf")
    _run(
        session,
        second,
        [_candidate(second, _fields("1", org="Zeta Cable Co WTEST"))],
    )
    declare_single_run_documents_by_policy(session, project.id)
    result = run_dependency_admission(session, project.id)

    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {"already_admitted"}


def test_a_placeholder_against_a_named_party_is_not_a_disagreement(
    session, project
):
    """ADR-0031 withholds only where revisions name different External
    Parties. `N/A` names nobody, so it is a gap beside a name, never a
    second party."""
    feb, may, feb_c, may_c = _corpus(
        session,
        project,
        [_fields("PL1", org=PIPELINE)],
        [_fields("PL1", org="N/A")],
    )

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 1
    assert result.abstained_count == 0
