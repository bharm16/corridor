"""The rehearsal cohort: a deterministic rule and an immutable receipt.

Membership is a pure function of a pinned, sealed Revision Comparison plus
the verification state its successor inputs snapshot carries — never of the
live Candidate rows Adjudication mutates. Re-deriving from the same inputs
yields the same members and the same digest, or it refuses.
"""

import hashlib

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from corridor.cohort import (
    COHORT_RULE_VERSION,
    CohortDerivationError,
    derive_cohort_receipt,
)
from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.models import Candidate, CohortReceipt, DocPage, Document, Project
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import create_revision_comparison
from corridor.supersession import SupersessionDeclaration, register_supersessions

DECLARER = HumanPrincipal("local:cohort-declarer")
CITY = "City of Houston"


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
    p = Project(slug="cohort-test", name="Cohort Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def _supersede(session, project, predecessor, successor, index_registry_id):
    index = _document(
        session, project, registry_id=index_registry_id, filename="rid.pdf"
    )
    session.add(
        DocPage(document_id=index.id, page_no=1, text="Replaced on 2026-02-13")
    )
    session.flush()
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=__import__("datetime").date(2026, 2, 13),
                source_registry_id=index.registry_id,
                source_page=1,
            )
        ],
        project_id=project.id,
    )


def _document(session, project, *, registry_id, filename):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=hashlib.sha256(registry_id.encode()).hexdigest(),
        filename=filename,
        doc_type="matrix",
        parse_status="parsed",
        pages=4,
    )
    session.add(document)
    session.flush()
    return document


def _run(session, document, rows, *, prompt_version="matrix_tiered_v3"):
    candidates = []
    for fields, verified in rows:
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
                        "quote": " | ".join(str(v) for v in fields.values()),
                        "verified": verified,
                        "whole_row": True,
                    }
                ],
                "unverified_fields": [],
                "unmapped_columns": [],
                "low_confidence_tokens": [],
                "tier": "structure",
                "dedupe_hint": "|".join(str(v) for v in fields.values()),
                "text_source": "text_layer",
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version=prompt_version,
            model="gpt-test",
            citations_verified=verified,
        )
        session.add(candidate)
        candidates.append(candidate)
    run = record_extraction_run(
        session,
        document,
        prompt_version=prompt_version,
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="gpt-test",
        schema_version="matrix_candidate_shape_v1",
    )
    session.flush()
    return run


def _row(utility_id, *, org=CITY, conflict="Y", station="1102+20"):
    return {
        "utility_id": utility_id,
        "external_org": org,
        "utility_type": "WW",
        "baseline": "SR-BL",
        "potential_conflict": conflict,
        "station_from": station,
        "station_to": station,
    }


@pytest.fixture
def compared_chain(session, project):
    """December and February runs, both declared Active, compared."""
    december = _document(
        session, project, registry_id="ucm-dec", filename="dec.pdf"
    )
    february = _document(
        session, project, registry_id="ucm-feb", filename="feb.pdf"
    )
    predecessor_run = _run(
        session,
        december,
        [
            (_row("W1"), True),               # unchanged in February
            (_row("W2", conflict="N"), True), # flips N -> Y
            (_row("W3"), True),               # changes another field only
            (_row("X9", org="CenterPoint Energy", conflict="N"), True),
        ],
        prompt_version="matrix_tiered_v3.dec",
    )
    successor_run = _run(
        session,
        february,
        [
            (_row("W1"), True),
            (_row("W2", conflict="Y"), True),
            (_row("W3", station="1104+00"), True),
            (_row("W4", station="1110+00"), True),  # newly added conflict
            (_row("W5", station="1115+50"), False),  # verification-blocked
            (_row("X9", org="CenterPoint Energy", conflict="Y"), True),
        ],
        prompt_version="matrix_tiered_v3.feb",
    )
    _supersede(session, project, december, february, "rid-main")
    declare_active_run(session, december.id, predecessor_run.id, principal=DECLARER)
    declare_active_run(session, february.id, successor_run.id, principal=DECLARER)
    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    return comparison, february


def test_membership_is_the_stated_rule_and_nothing_else(session, compared_chain):
    comparison, february = compared_chain

    receipt = derive_cohort_receipt(
        session, comparison.id, external_org=CITY
    )

    members = {(m["utility_id"], m["classification"]) for m in receipt.members}
    assert members == {
        ("W2", "conflict_flag_n_to_y"),
        ("W4", "newly_added"),
        ("W5", "verification_blocked"),
    }
    assert all(m["document_registry_id"] == "ucm-feb" for m in receipt.members)
    assert receipt.member_count == 3
    assert receipt.external_org == CITY
    assert receipt.rule_version == COHORT_RULE_VERSION
    assert receipt.content_sha256


def test_rederiving_yields_the_identical_receipt(session, compared_chain):
    comparison, _ = compared_chain

    first = derive_cohort_receipt(session, comparison.id, external_org=CITY)
    second = derive_cohort_receipt(session, comparison.id, external_org=CITY)

    assert second.id == first.id
    assert second.content_sha256 == first.content_sha256


def test_an_undeclared_run_refuses_derivation(session, project):
    december = _document(session, project, registry_id="u-dec", filename="d.pdf")
    february = _document(session, project, registry_id="u-feb", filename="f.pdf")
    predecessor_run = _run(session, december, [(_row("W1"), True)],
                           prompt_version="v.dec")
    successor_run = _run(session, february, [(_row("W1"), True)],
                         prompt_version="v.feb")
    _supersede(session, project, december, february, "rid-undeclared")
    # Only the successor is declared; the predecessor run is not Active.
    declare_active_run(session, february.id, successor_run.id, principal=DECLARER)
    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    with pytest.raises(CohortDerivationError, match="Active"):
        derive_cohort_receipt(session, comparison.id, external_org=CITY)


def test_members_are_registry_identities_never_database_ids(
    session, compared_chain
):
    comparison, _ = compared_chain
    receipt = derive_cohort_receipt(session, comparison.id, external_org=CITY)
    for member in receipt.members:
        assert set(member) == {
            "document_registry_id",
            "utility_id",
            "classification",
        }


def test_receipts_are_immutable_below_the_service_boundary(
    session, compared_chain
):
    comparison, _ = compared_chain
    receipt = derive_cohort_receipt(session, comparison.id, external_org=CITY)

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                update(CohortReceipt)
                .where(CohortReceipt.id == receipt.id)
                .values(member_count=99)
            )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                delete(CohortReceipt).where(CohortReceipt.id == receipt.id)
            )
