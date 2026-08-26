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
from copy import deepcopy

import pytest
from sqlalchemy import func, select, text, update
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
    AuditLog,
    Candidate,
    Dependency,
    DependencyAdmissionOutcome,
    EvidenceLink,
    ExtractionRun,
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


def _run(
    session,
    document,
    candidates,
    *,
    prompt_version="matrix_v1",
    model="gpt-test",
    schema_version="matrix_candidate_shape_v1",
):
    for c in candidates:
        c.prompt_version = prompt_version
        c.model = model
        session.add(c)
    run = record_extraction_run(
        session,
        document,
        prompt_version=prompt_version,
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model=model,
        schema_version=schema_version,
        allow_unsealed_legacy=True,
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
    assert {
        "same_document_reextraction_requires_exact_fields_citations_and_one_current_dependency",
        "same_document_collision_abstains_before_new_dependency",
        "replay_candidate_matches_immutable_extraction_input",
        "replay_predecessor_association_is_attributable_and_durable",
    } <= set(expected["checks"])
    source_names = {name for name, _ in module._rule_source_bytes()}
    assert {
        "corridor.extraction_runs",
        "corridor.migrations.e6f2a9c7d481",
        "corridor.migrations.b317c5d7e9f2",
    } <= source_names
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
    assert {
        record.candidate_id: record.durable_outcome for record in records
    } == {feb_c[0].id: "merged", may_c[0].id: "admitted"}
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

    repeated = run_dependency_admission(session, project.id)
    repeated_refusals = [
        item for item in repeated.abstentions if item.reason == "write_refused"
    ]
    refusal_outcomes = session.scalars(
        select(DependencyAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == DependencyAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            DependencyAdmissionOutcome.reason == "write_refused",
        )
    ).all()
    assert {item.candidate_id for item in repeated_refusals} == {
        feb_c[0].id,
        may_c[0].id,
    }
    assert len(refusal_outcomes) == 4


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
    first_candidate = _candidate(
        first_document, _fields("SHARED", org="Party A")
    )
    second_candidate = _candidate(
        second_document, _fields("SHARED", org="Party B")
    )
    _run(
        session,
        first_document,
        [first_candidate],
    )
    _run(
        session,
        second_document,
        [second_candidate],
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

    original_payload = deepcopy(first_candidate.payload_json)
    first_candidate.payload_json = {
        **first_candidate.payload_json,
        "fields": {
            **first_candidate.payload_json["fields"],
            "external_org": "Party C",
        },
    }
    session.flush()
    changed = run_dependency_admission(session, project.id)
    first_candidate.payload_json = original_payload
    session.flush()
    reverted = run_dependency_admission(session, project.id)
    final_outcomes = session.scalars(
        select(DependencyAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == DependencyAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            DependencyAdmissionOutcome.reason == "revisions_disagree_on_party",
        )
    ).all()

    assert changed.abstained_count == 2
    assert reverted.abstained_count == 0
    assert len(final_outcomes) == 4


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


# ── Same-Document re-extraction replay ─────────────────────────────────


def _declare_run(session, document, run):
    from corridor.extraction_runs import declare_active_run

    declare_active_run(session, document.id, run.id, principal=OPERATOR)


def _reextracted_candidate(document, predecessor):
    candidate = _candidate(document, deepcopy(predecessor.payload_json["fields"]))
    candidate.payload_json = deepcopy(predecessor.payload_json)
    return candidate


def test_exact_reextraction_replays_453_rows_and_admits_only_the_new_row(
    session, project
):
    """A proving re-extraction is not 453 new human decisions.

    Exact rows from the same registered Document point back to the Dependency
    their earlier Candidates already established.  A genuinely new row remains
    ordinary Admission work and lands independently.
    """
    document = _document(session, project, filename="sh99-ucm.pdf")
    original = [
        _candidate(document, _fields(f"PL{number:03d}"))
        for number in range(1, 454)
    ]
    _run(session, document, original)
    declare_single_run_documents_by_policy(session, project.id)
    assert run_dependency_admission(session, project.id).admitted_count == 453

    replayed = [
        _reextracted_candidate(document, candidate)
        for candidate in original
    ]
    new_row = _candidate(document, _fields("C4", station="9999+00"))
    fresh_run = _run(
        session,
        document,
        [*replayed, new_row],
        prompt_version="matrix_tiered_v3",
        schema_version="matrix_candidate_shape_v3",
    )
    _declare_run(session, document, fresh_run)

    result = run_dependency_admission(session, project.id)

    assert result.admitted_count == 1
    assert result.abstained_count == 0
    assert all(candidate.state == "merged" for candidate in replayed)
    assert new_row.state == "accepted"
    assert session.scalar(
        select(func.count()).select_from(Dependency).where(
            Dependency.project_id == project.id
        )
    ) == 454
    outcomes = session.scalars(
        select(DependencyAdmissionOutcome).where(
            DependencyAdmissionOutcome.policy_run_id == result.run_id
        )
    ).all()
    assert sum(outcome.outcome == "merged" for outcome in outcomes) == 453
    assert sum(outcome.outcome == "admitted" for outcome in outcomes) == 1


def test_exact_reextraction_replay_is_idempotent_and_changes_no_ledger_rows(
    session, project
):
    document = _document(session, project, filename="stable-ucm.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    before = {
        model: session.scalar(
            select(func.count()).select_from(model)
        )
        for model in (Dependency, Assertion, EvidenceLink)
    }
    successor = _reextracted_candidate(document, predecessor)
    fresh_run = _run(session, document, [successor])
    _declare_run(session, document, fresh_run)

    first = run_dependency_admission(session, project.id)
    first_audit_count = session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "replay_dependency_candidate",
            AuditLog.after_json["candidate_id"].astext == str(successor.id),
        )
    )
    second = run_dependency_admission(session, project.id)

    after = {
        model: session.scalar(
            select(func.count()).select_from(model)
        )
        for model in (Dependency, Assertion, EvidenceLink)
    }
    assert after == before
    assert successor.state == "merged"
    assert successor.merged_into is not None
    assert first.admitted_count == first.abstained_count == 0
    assert second.admitted_count == second.abstained_count == 0
    assert session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "replay_dependency_candidate",
            AuditLog.after_json["candidate_id"].astext == str(successor.id),
        )
    ) == first_audit_count == 1

    [outcome] = session.scalars(
        select(DependencyAdmissionOutcome).where(
            DependencyAdmissionOutcome.policy_run_id == first.run_id,
            DependencyAdmissionOutcome.candidate_id == successor.id,
        )
    ).all()
    assert outcome.outcome == "merged"
    assert outcome.dependency_id == successor.merged_into
    [entry] = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "replay_dependency_candidate",
            AuditLog.after_json["candidate_id"].astext == str(successor.id),
        )
    ).all()
    replay = entry.after_json["same_document_reextraction"]
    from corridor import policy

    assert entry.after_json["same_document_reextraction_sha256"] == (
        policy.canonical_sha256(replay)
    )
    assert replay["successor_candidate_id"] == successor.id
    assert replay["successor_configuration"] == {
        "candidate_prompt_version": "matrix_v1",
        "candidate_model": "gpt-test",
        "run_prompt_version": "matrix_v1",
        "run_model": "gpt-test",
        "run_schema_version": "matrix_candidate_shape_v1",
        "lineage_status": "historical_unsealed",
        "prompt_sha256": None,
        "schema_sha256": None,
        "postprocessor_sha256": None,
        "extractor_config_sha256": None,
        "extractor_config_json": None,
        "token_usage_json": None,
    }
    assert len(replay["predecessor_candidates"]) == 1
    replayed_predecessor = replay["predecessor_candidates"][0]
    assert replayed_predecessor["candidate_id"] == predecessor.id
    assert replayed_predecessor["extraction_run_id"] == (
        predecessor.extraction_run_id
    )
    assert replayed_predecessor["associated_dependency_ids"] == [
        successor.merged_into
    ]
    assert replayed_predecessor["run_snapshot_matches_candidate"] is True
    assert replay["supported_fields"] == successor.payload_json["fields"]
    assert replay["citations"] == [
        {
            "document_id": document.id,
            "page": 1,
            "quote": successor.payload_json["citations"][0]["quote"],
            "verified": True,
            "whole_row": True,
        }
    ]
    assert replay["source_quality"]["text_source"] == {
        "present": True,
        "value": "text_layer",
    }

    from corridor import audit
    from corridor.support_transfer_lineage import admission_for_scope

    dependency = session.get(Dependency, successor.merged_into)
    admission_records = audit.admission_records_for_dependencies(
        session, [dependency.id]
    )[dependency.id]
    lineage, candidate_ids, reason = admission_for_scope(
        session,
        dependency,
        document.id,
        admission_records,
        (),
    )
    assert reason is None
    assert candidate_ids == (predecessor.id,)
    assert lineage is not None and lineage.candidate.id == predecessor.id


def test_exact_reextraction_can_use_legacy_project_record_when_run_snapshot_is_absent(
    session, project
):
    """A historical unsealed run is proved by its admitted facts and Evidence.

    Legacy Extraction Runs predate immutable Candidate snapshots.  Requiring a
    snapshot they could never contain would turn every exact extractor upgrade
    into hundreds of fake human decisions even though the Project Record still
    retains the exact asserted fields and verified citation.
    """

    document = _document(session, project, filename="legacy-unsnapshotted.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    legacy_run = _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    assert run_dependency_admission(session, project.id).admitted_count == 1
    # New rows cannot be created in this legacy shape.  Temporarily bypass the
    # immutability trigger only to reproduce a pre-migration row inside this
    # rollback-scoped test transaction.
    session.execute(
        text(
            "alter table extraction_runs disable trigger "
            "extraction_run_receipts_are_immutable"
        )
    )
    session.execute(
        update(ExtractionRun)
        .where(ExtractionRun.id == legacy_run.id)
        .values(candidate_inputs_json=None)
    )
    session.execute(
        text(
            "alter table extraction_runs enable trigger "
            "extraction_run_receipts_are_immutable"
        )
    )
    session.expire(legacy_run)
    successor = _reextracted_candidate(document, predecessor)
    fresh_run = _run(
        session,
        document,
        [successor],
        prompt_version="matrix_tiered_v3",
        schema_version="matrix_candidate_shape_v3",
    )
    _declare_run(session, document, fresh_run)

    result = run_dependency_admission(session, project.id)

    assert result.admitted_count == result.abstained_count == 0
    assert successor.state == "merged"
    assert successor.merged_into is not None
    entry = session.scalar(
        select(AuditLog).where(
            AuditLog.action == "replay_dependency_candidate",
            AuditLog.after_json["candidate_id"].astext == str(successor.id),
        )
    )
    assert entry is not None
    [proof] = entry.after_json["same_document_reextraction"][
        "predecessor_candidates"
    ]
    assert proof["run_snapshot_matches_candidate"] is False
    assert proof["legacy_project_record_matches_claim"] is True
    project_record_proof = proof["legacy_project_record_proof"]
    assert project_record_proof["proof_version"] == "legacy-project-record-replay-v1"
    assert project_record_proof["evidence_links"]
    assert project_record_proof["assertions"]
    unretained = project_record_proof["historically_unretained_extractor_metadata"]
    assert unretained["citation_whole_row"] == [True]
    assert unretained["source_quality"]["text_source"] == {
        "present": True,
        "value": "text_layer",
    }


def test_replay_receipt_can_prove_a_later_exact_reextraction(session, project):
    document = _document(session, project, filename="third-extraction.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    first_replay = _reextracted_candidate(document, predecessor)
    first_run = _run(session, document, [first_replay])
    _declare_run(session, document, first_run)
    run_dependency_admission(session, project.id)

    second_replay = _reextracted_candidate(document, first_replay)
    second_run = _run(session, document, [second_replay])
    _declare_run(session, document, second_run)
    result = run_dependency_admission(session, project.id)

    assert result.admitted_count == result.abstained_count == 0
    assert second_replay.state == "merged"
    assert second_replay.merged_into == first_replay.merged_into
    assert session.scalar(
        select(func.count()).select_from(Dependency).where(
            Dependency.project_id == project.id
        )
    ) == 1


@pytest.mark.parametrize("changed_fact", ["field", "quote"])
def test_changed_reextraction_fact_stays_pending(session, project, changed_fact):
    document = _document(session, project, filename=f"changed-{changed_fact}.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    successor = _reextracted_candidate(document, predecessor)
    if changed_fact == "field":
        successor.payload_json["fields"]["station_to"] = "2200+00"
    else:
        successor.payload_json["citations"][0]["quote"] += " changed"
    fresh_run = _run(session, document, [successor])
    _declare_run(session, document, fresh_run)

    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }
    repeated = run_dependency_admission(session, project.id)
    assert repeated.abstained_count == 0
    assert session.scalar(
        select(func.count()).select_from(DependencyAdmissionOutcome).where(
            DependencyAdmissionOutcome.candidate_id == successor.id,
            DependencyAdmissionOutcome.reason == "same_document_replay_unproven",
        )
    ) == 1


def test_changed_party_stays_pending_instead_of_replaying(session, project):
    document = _document(session, project, filename="changed-party.pdf")
    predecessor = _candidate(document, _fields("PL1", org=PIPELINE))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    successor = _candidate(
        document, _fields("PL1", org="Someone Else Entirely")
    )
    fresh_run = _run(session, document, [successor])
    _declare_run(session, document, fresh_run)

    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }


def test_same_facts_from_a_different_document_do_not_replay(session, project):
    predecessor_document = _document(session, project, filename="first-source.pdf")
    predecessor = _candidate(predecessor_document, _fields("PL1"))
    _run(session, predecessor_document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    successor_document = _document(session, project, filename="second-source.pdf")
    successor = _candidate(
        successor_document, deepcopy(predecessor.payload_json["fields"])
    )
    _run(session, successor_document, [successor])
    declare_single_run_documents_by_policy(session, project.id)

    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {"already_admitted"}


def test_missing_predecessor_association_refuses_replay(session, project):
    carrier_document = _document(session, project, filename="carrier.pdf")
    carrier = _candidate(carrier_document, _fields("PL1"))
    _run(session, carrier_document, [carrier])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    replay_document = _document(session, project, filename="orphan-history.pdf")
    orphan = _candidate(replay_document, _fields("PL1"))
    _run(session, replay_document, [orphan])
    orphan.state = "accepted"
    session.flush()
    successor = _reextracted_candidate(replay_document, orphan)
    fresh_run = _run(session, replay_document, [successor])
    _declare_run(session, replay_document, fresh_run)

    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }


def test_projected_identity_change_does_not_turn_exact_replay_into_duplicate(
    session, project
):
    document = _document(session, project, filename="corrected-identity.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    dependency.source_ref = "CORRECTED-PL1"
    session.flush()

    successor = _reextracted_candidate(document, predecessor)
    fresh_run = _run(session, document, [successor])
    _declare_run(session, document, fresh_run)
    result = run_dependency_admission(session, project.id)

    assert result.admitted_count == result.abstained_count == 0
    assert successor.state == "merged"
    assert successor.merged_into == dependency.id
    assert dependency.source_ref == "CORRECTED-PL1"
    assert session.scalar(
        select(func.count()).select_from(Dependency).where(
            Dependency.project_id == project.id
        )
    ) == 1


def test_projected_identity_change_plus_changed_fact_abstains_without_duplicate(
    session, project
):
    document = _document(session, project, filename="corrected-changed-row.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)
    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    dependency.source_ref = "CORRECTED-PL1"
    session.flush()

    successor = _reextracted_candidate(document, predecessor)
    successor.payload_json["fields"]["station_to"] = "2200+00"
    fresh_run = _run(session, document, [successor])
    _declare_run(session, document, fresh_run)
    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }
    assert session.scalar(
        select(func.count()).select_from(Dependency).where(
            Dependency.project_id == project.id
        )
    ) == 1


def test_new_source_quality_metadata_prevents_replay(session, project):
    document = _document(session, project, filename="new-source-quality.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    successor = _reextracted_candidate(document, predecessor)
    successor.payload_json["unmapped_columns"] = ["Newly discovered column"]
    fresh_run = _run(session, document, [successor])
    _declare_run(session, document, fresh_run)
    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }


def test_mutable_candidate_cannot_hide_changed_immutable_run_input(session, project):
    document = _document(session, project, filename="snapshot-mismatch.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    successor = _reextracted_candidate(document, predecessor)
    successor.payload_json["fields"]["station_to"] = "2200+00"
    fresh_run = _run(session, document, [successor])
    successor.payload_json = deepcopy(predecessor.payload_json)
    session.flush()
    _declare_run(session, document, fresh_run)
    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }


def test_ambiguous_predecessor_association_refuses_replay(session, project):
    from corridor import audit

    document = _document(session, project, filename="ambiguous.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    other = _candidate(document, _fields("PL2", station="2200+00"))
    _run(session, document, [predecessor, other])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)
    dependencies = {
        dependency.source_ref: dependency
        for dependency in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    }
    audit.record(
        session,
        principal=OPERATOR,
        action=audit.ACCEPT_CANDIDATE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependencies["PL2"].id,
        after={
            "candidate_id": predecessor.id,
            "role": "synthetic_ambiguous_association",
            "fields": predecessor.payload_json["fields"],
        },
    )

    successor = _reextracted_candidate(document, predecessor)
    fresh_run = _run(session, document, [successor])
    _declare_run(session, document, fresh_run)
    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }


def test_machine_outcome_must_match_predecessor_state(
    session, project, monkeypatch
):
    from dataclasses import replace

    from corridor import audit

    document = _document(session, project, filename="outcome-state-mismatch.pdf")
    predecessor = _candidate(document, _fields("PL1"))
    _run(session, document, [predecessor])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)

    real = audit.admission_records_for_dependencies

    def mismatched_records(session_, dependency_ids):
        return {
            dependency_id: tuple(
                replace(record, durable_outcome="merged")
                for record in records
            )
            for dependency_id, records in real(
                session_, dependency_ids
            ).items()
        }

    monkeypatch.setattr(audit, "admission_records_for_dependencies", mismatched_records)
    successor = _reextracted_candidate(document, predecessor)
    fresh_run = _run(session, document, [successor])
    _declare_run(session, document, fresh_run)
    result = run_dependency_admission(session, project.id)

    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }


def test_conflicting_exact_predecessors_with_corrected_carriers_never_add_row(
    session, project
):
    from corridor import audit

    document = _document(session, project, filename="conflicting-history.pdf")
    first = _candidate(document, _fields("PL1"))
    other_row = _candidate(document, _fields("PL2", station="2200+00"))
    _run(session, document, [first, other_row])
    declare_single_run_documents_by_policy(session, project.id)
    run_dependency_admission(session, project.id)
    dependencies = {
        dependency.source_ref: dependency
        for dependency in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    }

    conflicting = _reextracted_candidate(document, first)
    conflicting_run = _run(session, document, [conflicting])
    conflicting.state = "accepted"
    audit.record(
        session,
        principal=OPERATOR,
        action=audit.ACCEPT_CANDIDATE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependencies["PL2"].id,
        after={
            "candidate_id": conflicting.id,
            "fields": conflicting.payload_json["fields"],
        },
    )
    dependencies["PL1"].source_ref = "CORRECTED-A"
    dependencies["PL2"].source_ref = "CORRECTED-B"
    session.flush()

    successor = _reextracted_candidate(document, first)
    successor_run = _run(session, document, [successor])
    _declare_run(session, document, successor_run)
    result = run_dependency_admission(session, project.id)

    assert conflicting_run.id != successor_run.id
    assert successor.state == "pending"
    assert {item.reason for item in result.abstentions} == {
        "same_document_replay_unproven"
    }
    assert session.scalar(
        select(func.count()).select_from(Dependency).where(
            Dependency.project_id == project.id
        )
    ) == 2
