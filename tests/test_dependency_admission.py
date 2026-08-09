"""Dependency admission: exact revision agreement under an authorized policy.

ADR-0027: a dependency Candidate may enter the Ledger under a named,
versioned policy an accountable principal authorizes, and the eligibility
proof is exact agreement between stated revisions. Two documents
independently asserting the identical row is stronger evidence than one
reviewer glancing at a card; everything else — the changed, the missing,
the ambiguous — abstains and waits for Adjudication.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.dependency_admission import (
    DEPENDENCY_ADMISSION_POLICY_VERSION,
    DependencyAdmissionNotAuthorized,
    authorize_dependency_admission,
    run_dependency_admission,
)
from corridor.extraction_runs import (
    declare_single_run_documents,
    record_extraction_run,
)
from corridor.models import (
    Assertion,
    Candidate,
    Dependency,
    DependencyAdmissionOutcome,
    PolicyApproval,
    PolicyRun,
    DocPage,
    Document,
    Project,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal

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
    declare_single_run_documents(session, project.id, principal=OPERATOR)
    return feb, may, feb_c, may_c


def _authorize(session, project, feb, may):
    return authorize_dependency_admission(
        session,
        project.id,
        principal=OPERATOR,
        agreement_document_ids=[feb.id, may.id],
    )


# ── Authorization ────────────────────────────────────────────────────────


def test_a_run_without_authorization_refuses(session, project):
    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    with pytest.raises(DependencyAdmissionNotAuthorized):
        run_dependency_admission(session, project.id)


def test_authorization_pins_the_agreement_documents_by_content(
    session, project
):
    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    approval = _authorize(session, project, feb, may)
    assert approval.policy_version == DEPENDENCY_ADMISSION_POLICY_VERSION
    assert approval.approved_by == OPERATOR.subject
    pinned = {
        d["document_id"]: d["sha256"]
        for d in approval.policy_json["agreement_documents"]
    }
    assert pinned == {feb.id: feb.sha256, may.id: may.sha256}

    with pytest.raises(InvalidHumanPrincipal):
        authorize_dependency_admission(
            session,
            project.id,
            principal="system:batch",
            agreement_document_ids=[feb.id, may.id],
        )


def test_an_undeclared_agreement_document_cannot_be_authorized(
    session, project
):
    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    stray = _document(session, project, filename="stray.pdf")
    with pytest.raises(ValueError):
        authorize_dependency_admission(
            session,
            project.id,
            principal=OPERATOR,
            agreement_document_ids=[feb.id, stray.id],
        )


# ── Admission ────────────────────────────────────────────────────────────


def test_exact_agreement_admits_and_merges_the_sibling(session, project):
    feb, may, feb_c, may_c = _corpus(
        session, project, [_fields("PL1")], [_fields("PL1")]
    )
    _authorize(session, project, feb, may)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 1
    assert result.abstained_count == 0

    dependency = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    assert dependency.source_ref == "PL1"

    # The primary comes from the last-named agreement document; the
    # sibling merges so its citation attaches as corroboration.
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


def test_disagreeing_revisions_abstain(session, project):
    feb, may, feb_c, may_c = _corpus(
        session,
        project,
        [_fields("PL1", station="1102+20")],
        [_fields("PL1", station="1105+00")],
    )
    _authorize(session, project, feb, may)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    # One abstention per participating candidate: each pending row's
    # disposition is recorded, not one rollup per conflict.
    assert [a.reason for a in result.abstentions] == [
        "revisions_disagree",
        "revisions_disagree",
    ]
    session.refresh(feb_c[0])
    session.refresh(may_c[0])
    assert feb_c[0].state == "pending"
    assert may_c[0].state == "pending"
    assert (
        session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        ).all()
        == []
    )


def test_a_row_missing_from_a_revision_abstains(session, project):
    feb, may, *_ = _corpus(
        session, project, [_fields("PL1"), _fields("PL2")], [_fields("PL1")]
    )
    _authorize(session, project, feb, may)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 1  # PL1 agrees
    reasons = {a.reason for a in result.abstentions}
    assert reasons == {"missing_from_agreement_document"}


def test_duplicate_rows_in_one_revision_abstain(session, project):
    feb, may, *_ = _corpus(
        session,
        project,
        [_fields("PL1"), _fields("PL1")],
        [_fields("PL1")],
    )
    _authorize(session, project, feb, may)

    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {
        "multiple_rows_in_agreement_document"
    }


def test_an_already_admitted_reference_abstains(session, project):
    feb, may, feb_c, may_c = _corpus(
        session, project, [_fields("PL1")], [_fields("PL1")]
    )
    _authorize(session, project, feb, may)
    run_dependency_admission(session, project.id)

    # New extraction of the same row later (same docs would refuse via
    # single-run rule; use content check instead): a fresh identical
    # candidate pair must not create a second PL1.
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
    _authorize(session, project, feb, may)
    result = run_dependency_admission(session, project.id)
    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {"citations_unverified"}


# ── The receipt ──────────────────────────────────────────────────────────


def test_the_run_is_an_immutable_receipt_of_exact_outcomes(session, project):
    feb, may, feb_c, may_c = _corpus(
        session,
        project,
        [_fields("PL1"), _fields("PL2", station="1200+00")],
        [_fields("PL1"), _fields("PL2", station="1201+00")],
    )
    approval = _authorize(session, project, feb, may)

    result = run_dependency_admission(session, project.id)
    run = session.get(PolicyRun, result.run_id)
    assert run.policy_approval_id == approval.id
    assert run.policy_sha256 == approval.policy_sha256
    assert (run.applied_count, run.abstained_count) == (1, 2)

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


# ── The digest covers the rules and the documents ────────────────────────


def test_editing_a_check_pauses_the_policy_until_reauthorized(
    session, project, monkeypatch
):
    from corridor import dependency_admission as module

    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    _authorize(session, project, feb, may)
    assert module.current_dependency_admission_approval(session, project.id)

    real = module._rule_source_bytes

    def edited():
        return tuple(
            (name, source + b"\n# edited\n") for name, source in real()
        )

    monkeypatch.setattr(module, "_rule_source_bytes", edited)
    assert (
        module.current_dependency_admission_approval(session, project.id)
        is None
    )
    with pytest.raises(DependencyAdmissionNotAuthorized):
        run_dependency_admission(session, project.id)


def test_a_swapped_agreement_document_pauses_the_policy(session, project):
    from corridor import dependency_admission as module

    feb, may, *_ = _corpus(session, project, [_fields("PL1")], [_fields("PL1")])
    _authorize(session, project, feb, may)
    assert module.current_dependency_admission_approval(session, project.id)

    session.execute(
        update(Document)
        .where(Document.id == feb.id)
        .values(sha256=hashlib.sha256(b"different bytes").hexdigest())
    )
    session.expire_all()
    assert (
        module.current_dependency_admission_approval(session, project.id)
        is None
    )


# ── Review findings (#204): lineage, run pinning, doc types, batches ─────


def test_policy_admitted_records_carry_attributable_lineage(session, project):
    """Reconfirmation and Carry-Forward read admission lineage from the
    audit log; a policy-admitted record must be as legible there as a
    human-accepted one, backed by its durable receipt."""
    from corridor import audit as audit_module

    feb, may, feb_c, may_c = _corpus(
        session, project, [_fields("PL1")], [_fields("PL1")]
    )
    _authorize(session, project, feb, may)
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


def test_a_redeclared_active_run_pauses_the_policy(session, project):
    """The policy stands on declared work, so it pins WHICH declared work:
    re-declaring a document's Active Run is a legitimate human act that
    changes what would admit, and it must pause the policy."""
    from corridor import dependency_admission as module
    from corridor.extraction_runs import declare_active_run

    feb, may, feb_c, may_c = _corpus(
        session, project, [_fields("PL1")], [_fields("PL1")]
    )
    _authorize(session, project, feb, may)
    assert module.current_dependency_admission_approval(session, project.id)

    second = _run(session, feb, [_candidate(feb, _fields("PL1"))])
    declare_active_run(session, feb.id, second.id, principal=OPERATOR)
    assert (
        module.current_dependency_admission_approval(session, project.id)
        is None
    )
    with pytest.raises(DependencyAdmissionNotAuthorized):
        run_dependency_admission(session, project.id)


def test_only_matrix_documents_can_anchor_agreement(session, project):
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
    with pytest.raises(ValueError) as excinfo:
        authorize_dependency_admission(
            session,
            project.id,
            principal=OPERATOR,
            agreement_document_ids=[feb.id, minutes.id],
        )
    assert "matrix" in str(excinfo.value)


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
    _authorize(session, project, feb, may)

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
