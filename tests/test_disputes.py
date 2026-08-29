"""Disputes: derived from what the revisions said, settled by a human.

ADR-0031. A Dispute is a query over Assertions, not a state a row is put
into — so a disputed row is an ordinary workable row, and settling one
records a judgment beside the claims rather than erasing the losing one.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.disputes import (
    DisputeMovedOn,
    NoSuchDispute,
    disputes_for,
    settle_dispute,
    settled_field_names,
)
from corridor.exceptions import contradicted_fields, exceptions_for
from corridor.eval import EvalResult, artifact
from corridor.extraction_runs import record_extraction_run
from corridor.measurement_cases import CasePredictionSet, score_measurement_cases
from corridor.models import (
    Assertion,
    Candidate,
    Dependency,
    DisputeSettlement,
    DocPage,
    Document,
    EvidenceLink,
    ExtractionMeasurementCaseState,
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
    case = session.scalars(
        select(ExtractionMeasurementCaseState).where(
            ExtractionMeasurementCaseState.ruling_type == "dispute_settlement",
            ExtractionMeasurementCaseState.ruling_id == settlement.id,
        )
    ).one()
    assert case.kind == "source_discrepancy_settlement"
    assert case.case_key == f"dependency:{disputed.id}:dispute:station_from"
    assert case.expected_json["scoring_rule"] == "disputed_claims_preserved"
    assert case.expected_json["field_name"] == "station_from"
    assert case.expected_json["settled_value"] == "1105+00"
    assert {claim["asserted_value"] for claim in case.expected_json["claims"]} == {
        "1102+20",
        "1105+00",
    }
    assert {
        document["sha256"] for document in case.source_identity_json["documents"]
    } == {
        document.sha256
        for document in session.scalars(
            select(Document).where(Document.project_id == disputed.project_id)
        )
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


def test_a_settlement_prediction_is_scored_against_the_human_conclusion(
    session, disputed
):
    settlement = settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )
    case = session.scalars(
        select(ExtractionMeasurementCaseState).where(
            ExtractionMeasurementCaseState.ruling_type == "dispute_settlement",
            ExtractionMeasurementCaseState.ruling_id == settlement.id,
        )
    ).one()
    [source_dispute] = disputes_for(session, disputed.id, include_settled=True)
    run_ids = set()
    for index, claim in enumerate(source_dispute.claims, start=1):
        candidate = Candidate(
            project_id=disputed.project_id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": {
                    "utility_id": "PL1",
                    "station_from": claim.value,
                },
                "citations": [
                    {
                        "document_id": claim.document_id,
                        "page": claim.page_no,
                        "quote": claim.quote,
                        "verified": True,
                        "whole_row": True,
                    }
                ],
            },
            source_document_id=claim.document_id,
            source_pages=[claim.page_no],
            confidence=1.0,
            prompt_version=f"settlement_case_v{index}",
            citations_verified=True,
        )
        session.add(candidate)
        session.flush([candidate])
        document = session.get(Document, claim.document_id)
        run = record_extraction_run(
            session,
            document,
            prompt_version=candidate.prompt_version,
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            allow_unsealed_legacy=True,
        )
        run_ids.add(run.id)

    correct = score_measurement_cases(
        session,
        project_id=disputed.project_id,
        extraction_run_ids=run_ids,
        predictions=CasePredictionSet(
            outputs={case.public_id: {"settled_value": "1105+00"}},
            source="settlement-predictions.json",
            sha256="a" * 64,
        ),
    )
    assert correct.matched == 1
    assert correct.mismatched == 0
    assert correct.prediction_receipt["case_state_public_ids"] == [case.public_id]
    written = artifact(
        EvalResult(project="dispute-test"),
        reference_description="reference.csv",
        ran_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        case_measurement=correct,
    )
    assert written["human_ruling_cases"]["prediction_receipt"] == {
        "schema_version": "corridor.extraction-measurement-case-predictions.v1",
        "source": "settlement-predictions.json",
        "sha256": "a" * 64,
        "case_state_public_ids": [case.public_id],
    }

    wrong = score_measurement_cases(
        session,
        project_id=disputed.project_id,
        extraction_run_ids=run_ids,
        predictions=CasePredictionSet(
            outputs={case.public_id: {"settled_value": "1102+20"}},
            source="settlement-predictions.json",
            sha256="b" * 64,
        ),
    )
    assert wrong.matched == 0
    assert wrong.mismatched == 1


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


def test_a_claim_arriving_while_you_read_refuses_the_settlement(
    session, disputed
):
    """The reviewer compared two pages; a third arrived. Recording their
    judgment as covering it would settle what they never saw."""
    saw = disputes_for(session, disputed.id)[0].newest_claim_id

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == disputed.id)
    ).first()
    session.add(
        Assertion(
            dependency_id=disputed.id,
            field_name="station_from",
            asserted_value="1300+00",
            evidence_link_id=link.id,
        )
    )
    session.flush()

    with pytest.raises(DisputeMovedOn):
        settle_dispute(
            session,
            disputed.id,
            "station_from",
            value="1105+00",
            principal=REVIEWER,
            saw_claim_id=saw,
        )
    assert disputes_for(session, disputed.id) != []


def test_settling_what_you_actually_saw_is_accepted(session, disputed):
    saw = disputes_for(session, disputed.id)[0].newest_claim_id

    settle_dispute(
        session,
        disputed.id,
        "station_from",
        value="1105+00",
        principal=REVIEWER,
        saw_claim_id=saw,
    )
    assert disputes_for(session, disputed.id) == []


def test_an_unverified_claim_never_unsettles_a_decided_field(
    session, project, disputed
):
    """A bad citation is not a source disagreeing, so it cannot reopen
    what a reviewer already decided."""
    settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )
    assert disputes_for(session, disputed.id) == []

    document = session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).first()
    bad = EvidenceLink(
        dependency_id=disputed.id,
        document_id=document.id,
        page_no=1,
        quote="not on the page",
        verified=False,
    )
    session.add(bad)
    session.flush()
    session.add(
        Assertion(
            dependency_id=disputed.id,
            field_name="station_from",
            asserted_value="9999+99",
            evidence_link_id=bad.id,
        )
    )
    session.flush()

    assert disputes_for(session, disputed.id) == []
    assert settled_field_names(session, [disputed.id]) == {
        disputed.id: {"station_from"}
    }


def test_settling_a_not_null_field_as_empty_does_not_break_the_record(
    session, project, disputed
):
    """Concluding that a field says nothing is legitimate; a column that
    cannot be empty must not turn that into a 500."""
    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == disputed.id)
    ).all()
    for value, evidence in zip(("Water main", "Gas main"), link):
        session.add(
            Assertion(
                dependency_id=disputed.id,
                field_name="title",
                asserted_value=value,
                evidence_link_id=evidence.id,
            )
        )
    session.flush()
    assert "title" in [d.field_name for d in disputes_for(session, disputed.id)]

    settlement = settle_dispute(
        session, disputed.id, "title", value=None, principal=REVIEWER
    )
    assert settlement.settled_value is None
    session.refresh(disputed)
    assert disputed.title is not None  # the column keeps its last real value


def test_a_mistaken_settlement_is_corrected_by_settling_again(
    session, disputed
):
    """ADR-0031 rejects irreversibility: append-only history makes a
    wrong verdict correctable for free. 'Already settled' must never
    read as 'nothing to settle'."""
    settle_dispute(
        session, disputed.id, "station_from", value="1102+20", principal=REVIEWER
    )
    assert disputes_for(session, disputed.id) == []

    corrected = settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )

    assert corrected.settled_value == "1105+00"
    session.refresh(disputed)
    assert disputed.station_from == "1105+00"
    # Both verdicts survive in order; nothing was edited.
    settlements = session.scalars(
        select(DisputeSettlement)
        .where(DisputeSettlement.dependency_id == disputed.id)
        .order_by(DisputeSettlement.id)
    ).all()
    assert [s.settled_value for s in settlements] == ["1102+20", "1105+00"]
    case_states = session.scalars(
        select(ExtractionMeasurementCaseState)
        .where(
            ExtractionMeasurementCaseState.case_key
            == f"dependency:{disputed.id}:dispute:station_from"
        )
        .order_by(ExtractionMeasurementCaseState.id)
    ).all()
    assert [state.expected_json["settled_value"] for state in case_states] == [
        "1102+20",
        "1105+00",
    ]
    assert case_states[0].predecessor_state_id is None
    assert case_states[1].predecessor_state_id == case_states[0].id


def test_a_settled_field_still_shows_its_claims_when_asked(
    session, disputed
):
    """The page that offers to change a conclusion needs the claims in
    front of the reviewer."""
    settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )

    assert disputes_for(session, disputed.id) == []
    [dispute] = disputes_for(session, disputed.id, include_settled=True)
    assert dispute.field_name == "station_from"
    assert set(dispute.values) == {"1102+20", "1105+00"}


def test_settling_a_dismissed_record_refuses(session, project, disputed):
    from corridor.adjudicate import dismiss_dependency

    dismiss_dependency(session, disputed, "duplicate", principal=REVIEWER)

    with pytest.raises(ValueError, match="dismissed"):
        settle_dispute(
            session,
            disputed.id,
            "station_from",
            value="1105+00",
            principal=REVIEWER,
        )


def test_a_nonbreaking_space_is_not_a_claim_in_sql_either(
    session, project, disputed
):
    """Postgres's [[:space:]] misses NBSP; Python's strip does not. The
    one-predicate rule holds only if both strip the same characters."""
    document = session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).first()
    link = EvidenceLink(
        dependency_id=disputed.id,
        document_id=document.id,
        page_no=1,
        quote="nbsp",
        verified=True,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=disputed.id,
            field_name="station_from",
            asserted_value=" ",  # a lone non-breaking space
            evidence_link_id=link.id,
        )
    )
    session.flush()

    [dispute] = disputes_for(session, disputed.id)
    assert " " not in dispute.values
    # And it cannot un-settle a decided field by arriving later.
    settle_dispute(
        session, disputed.id, "station_from", value="1105+00", principal=REVIEWER
    )
    assert contradicted_fields(session, [disputed.id]) == {}
