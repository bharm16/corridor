"""Live Supersession Review routing and human Reconfirmation.

The worklist is derived state.  These tests exercise the public domain seam
against Postgres so a web route cannot accidentally become the implementation
of registry, comparison, or operative-support policy.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text, update

from corridor.adjudicate import accept_candidate, edit_candidate
from corridor.db import Session, engine
from corridor.extraction_runs import (
    declare_active_run,
    record_extraction_run,
)
from corridor.exceptions import evaluate as evaluate_exceptions
from corridor.ledger import mark_satisfies
from corridor.models import (
    ActiveExtractionRun,
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    ExtractionRun,
    OperativeSupport,
    Project,
    RevisionComparisonFinding,
    RevisionComparisonRun,
)
from corridor.operative_support import designate_publication_support
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import create_revision_comparison
from corridor.revision_comparison import read_revision_comparison
from corridor.supersession import (
    SupersessionDeclaration,
    register_supersessions,
)
from corridor.supersession_review import (
    ReconfirmationUnavailable,
    build_reviewer_worklist,
    ordinary_candidate_for_update,
    reconfirm_operative_support,
)


REVIEWER = HumanPrincipal("local:supersession-reviewer")


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


def _document(
    session,
    project: Project,
    *,
    registry_id: str,
    sha: str,
    filename: str,
    text: str,
) -> Document:
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha * 64,
        filename=filename,
        doc_type="matrix" if registry_id != "INDEX" else "other",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=text,
            image_path=f"/tmp/{filename}.png",
        )
    )
    session.flush()
    return document


def _candidate(
    project: Project,
    document: Document,
    *,
    utility_id: str = "FOC1-1",
    station_from: str = "100+00",
    baseline: str | None = None,
    quote: str | None = None,
) -> Candidate:
    row_quote = quote or " ".join(
        item
        for item in (
            utility_id,
            "AT&T",
            "Telecom",
            station_from,
            baseline,
        )
        if item
    )
    fields = {
        "utility_id": utility_id,
        "external_org": "AT&T",
        "utility_type": "Telecom",
        "station_from": station_from,
    }
    if baseline is not None:
        fields["baseline"] = baseline
    return Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields,
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": row_quote,
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="matrix-v1",
        model="test-model",
        citations_verified=True,
    )


def _completed_run(session, document: Document, *candidates: Candidate):
    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix-v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="test-model",
        schema_version="candidate-v1",
    )
    declare_active_run(session, document.id, run.id)
    session.flush()
    return run


def _superseded_dependency(
    session,
    *,
    satisfying: bool = True,
    edit_predecessor: bool = False,
    slug: str = "supersession-review-test",
    candidate_baseline: str | None = None,
    duplicate_predecessor_citation: bool = False,
):
    project = Project(
        slug=slug,
        name="Supersession Review Test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    predecessor = _document(
        session,
        project,
        registry_id="REV-A",
        sha="a",
        filename="revision-a.pdf",
        text=" ".join(
            item
            for item in ("FOC1-1 AT&T Telecom 100+00", candidate_baseline)
            if item
        ),
    )
    successor = _document(
        session,
        project,
        registry_id="REV-B",
        sha="b",
        filename="revision-b.pdf",
        text=" ".join(
            item
            for item in ("FOC1-1 AT&T Telecom 100+00", candidate_baseline)
            if item
        ),
    )
    index = _document(
        session,
        project,
        registry_id="INDEX",
        sha="c",
        filename="index.pdf",
        text="REV-A superseded by REV-B on 2026-08-01",
    )

    predecessor_candidate = _candidate(
        project, predecessor, baseline=candidate_baseline
    )
    if duplicate_predecessor_citation:
        payload = dict(predecessor_candidate.payload_json)
        [citation] = payload["citations"]
        payload["citations"] = [dict(citation), dict(citation)]
        predecessor_candidate.payload_json = payload
    predecessor_run = _completed_run(
        session, predecessor, predecessor_candidate
    )
    if edit_predecessor:
        edited_fields = dict(predecessor_candidate.payload_json["fields"])
        edited_fields["station_from"] = "100+25"
        edit_candidate(
            session,
            predecessor_candidate,
            edited_fields,
            principal=REVIEWER,
        )
    dependency = accept_candidate(
        session, predecessor_candidate, principal=REVIEWER
    )
    old_evidence = session.scalars(
        select(EvidenceLink).where(
            EvidenceLink.dependency_id == dependency.id
        ).order_by(EvidenceLink.id)
    ).first()
    assert old_evidence is not None
    if satisfying:
        mark_satisfies(
            session,
            dependency.id,
            old_evidence.id,
            principal=REVIEWER,
        )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-A",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=project.id,
    )
    session.flush()
    return {
        "project": project,
        "predecessor": predecessor,
        "successor": successor,
        "predecessor_candidate": predecessor_candidate,
        "predecessor_run": predecessor_run,
        "dependency": dependency,
        "old_evidence": old_evidence,
    }


def _delete_committed_review_project(project_id: int) -> None:
    """Remove the committed rows used by the two-Session regression."""

    with Session() as cleanup:
        # These production receipts are deliberately append-only. The test
        # commits only so a genuinely independent Session can observe the
        # state. This transaction-local Postgres setting permits deletion of
        # this exact synthetic project, then restores automatically at commit
        # or rollback; it never weakens the schema for another Session.
        cleanup.execute(text("set local session_replication_role = replica"))
        document_ids = tuple(
            cleanup.scalars(
                select(Document.id).where(Document.project_id == project_id)
            ).all()
        )
        dependency_ids = tuple(
            cleanup.scalars(
                select(Dependency.id).where(Dependency.project_id == project_id)
            ).all()
        )
        candidate_ids = tuple(
            cleanup.scalars(
                select(Candidate.id).where(Candidate.project_id == project_id)
            ).all()
        )
        comparison_ids = tuple(
            cleanup.scalars(
                select(RevisionComparisonRun.id).where(
                    RevisionComparisonRun.project_id == project_id
                )
            ).all()
        )

        cleanup.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id.in_(dependency_ids),
            )
        )
        cleanup.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "candidate",
                AuditLog.entity_id.in_(candidate_ids),
            )
        )
        cleanup.execute(
            delete(OperativeSupport).where(
                OperativeSupport.dependency_id.in_(dependency_ids)
            )
        )
        cleanup.execute(
            delete(Assertion).where(Assertion.dependency_id.in_(dependency_ids))
        )
        cleanup.execute(
            delete(EvidenceLink).where(
                EvidenceLink.dependency_id.in_(dependency_ids)
            )
        )
        cleanup.execute(
            delete(RevisionComparisonFinding).where(
                RevisionComparisonFinding.revision_comparison_run_id.in_(
                    comparison_ids
                )
            )
        )
        cleanup.execute(
            delete(RevisionComparisonRun).where(
                RevisionComparisonRun.id.in_(comparison_ids)
            )
        )
        cleanup.execute(
            delete(Candidate).where(Candidate.project_id == project_id)
        )
        cleanup.execute(
            delete(Dependency).where(Dependency.project_id == project_id)
        )
        cleanup.execute(
            delete(ActiveExtractionRun).where(
                ActiveExtractionRun.document_id.in_(document_ids)
            )
        )
        cleanup.execute(
            delete(ExtractionRun).where(
                ExtractionRun.document_id.in_(document_ids)
            )
        )
        cleanup.execute(
            update(Document)
            .where(Document.project_id == project_id)
            .values(
                superseded_by=None,
                superseded_on=None,
                supersession_source_document_id=None,
                supersession_source_page=None,
            )
        )
        cleanup.execute(
            delete(DocPage).where(DocPage.document_id.in_(document_ids))
        )
        cleanup.execute(delete(Document).where(Document.project_id == project_id))
        cleanup.execute(delete(Project).where(Project.id == project_id))
        cleanup.commit()


def test_registry_work_exists_before_extraction_or_comparison(session):
    scenario = _superseded_dependency(session)

    worklist = build_reviewer_worklist(
        session, scenario["project"].id
    )

    assert worklist.reconfirmation == ()
    assert len(worklist.ordinary) == 1
    [review] = worklist.ordinary
    assert review.dependency_id == scenario["dependency"].id
    assert review.predecessor_document_id == scenario["predecessor"].id
    assert review.successor_document_id == scenario["successor"].id
    assert review.predecessor_candidate_ids == (
        scenario["predecessor_candidate"].id,
    )
    assert review.successor_candidate_ids == ()
    assert review.status == "awaiting_extraction"
    assert review.comparison_id is None
    assert review.finding_id is None
    assert review.reconfirmation_available is False
    assert {(scope.role, scope.field_name) for scope in review.superseded_scopes} == {
        ("publication", None),
        ("readiness", None),
    }


def test_a_durable_failed_attempt_is_not_awaiting_extraction(session):
    scenario = _superseded_dependency(session)
    record_extraction_run(
        session,
        scenario["successor"],
        prompt_version="matrix-v1",
        candidate_count=0,
        page_errors=1,
        outcome="failed",
        model="test-model",
        schema_version="candidate-v1",
        error_detail="page could not be read",
    )
    session.flush()

    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).ordinary

    assert review.status == "extraction_failed"
    assert review.successor_candidate_ids == ()
    assert review.reconfirmation_available is False


def test_completed_successor_run_waits_for_comparison_before_reconfirmation(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    _completed_run(session, scenario["successor"], successor_candidate)

    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).ordinary

    assert review.status == "awaiting_comparison"
    assert review.successor_candidate_ids == (successor_candidate.id,)
    assert review.reconfirmation_available is False


def test_completed_but_inactive_successor_run_fails_closed(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    record_extraction_run(
        session,
        scenario["successor"],
        prompt_version="matrix-v1",
        candidate_count=1,
        page_errors=0,
        candidates=(successor_candidate,),
        model="test-model",
        schema_version="candidate-v1",
    )
    # A later failure must not turn the completed receipt into "latest
    # failed" policy. Neither receipt is operative until a human declares
    # an Active Run.
    record_extraction_run(
        session,
        scenario["successor"],
        prompt_version="matrix-v1-retry",
        candidate_count=0,
        page_errors=1,
        outcome="failed",
        model="test-model",
        schema_version="candidate-v1",
        error_detail="retry failed",
    )
    session.flush()

    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).ordinary

    assert review.status == "awaiting_active_run"
    assert review.successor_candidate_ids == ()
    assert review.reconfirmation_available is False


def test_unchanged_exact_match_routes_only_to_reconfirmation(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(session, scenario["successor"], successor_candidate)
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.ordinary == ()
    assert len(worklist.reconfirmation) == 1
    [review] = worklist.reconfirmation
    assert review.comparison_id == comparison.id
    assert review.finding_id is not None
    assert review.reconfirmation_available is True
    assert review.successor_candidate_ids == (successor_candidate.id,)

    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        successor_candidate.id,
    ) is None


@pytest.mark.parametrize("stored_principal", [None, "reviewer", "agent"])
def test_legacy_admission_without_an_attributable_principal_stays_ordinary(
    session, stored_principal
):
    scenario = _superseded_dependency(session)
    admission = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == scenario["dependency"].id,
            AuditLog.action == "accept_candidate",
        )
    )
    assert admission is not None
    admission.actor = "legacy-import"
    admission.human_principal = stored_principal
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    dependency_review = next(
        review
        for review in worklist.ordinary
        if review.dependency_id == scenario["dependency"].id
    )
    assert dependency_review.reason == "admission_not_attributable"
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        successor_candidate.id,
    ).id == successor_candidate.id


def test_multiple_exact_comparison_receipts_never_choose_the_latest(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    first = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    second = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2-review",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    [review] = worklist.ordinary
    assert review.status == "comparison_selection_ambiguous"
    assert review.comparison_id is None
    assert review.reason == "multiple_exact_comparisons"
    assert first.id != second.id


def test_changed_and_unlinked_current_candidates_stay_ordinary(session):
    scenario = _superseded_dependency(session)
    changed = _candidate(
        scenario["project"],
        scenario["successor"],
        station_from="101+00",
    )
    successor_run = _completed_run(session, scenario["successor"], changed)
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    assert any(
        review.successor_candidate_ids == (changed.id,)
        for review in worklist.ordinary
    )
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        changed.id,
    ).id == changed.id


@pytest.mark.parametrize("second_match_state", ["changed", "unchanged"])
def test_fan_in_successor_candidate_is_globally_ordinary(
    session, second_match_state
):
    scenario = _superseded_dependency(session)
    other_predecessor = _document(
        session,
        scenario["project"],
        registry_id="REV-X",
        sha="f",
        filename="revision-x.pdf",
        text=(
            "FOC1-1 AT&T Telecom 100+00 N"
            if second_match_state == "changed"
            else "FOC1-1 AT&T Telecom 100+00"
        ),
    )
    other_candidate = _candidate(scenario["project"], other_predecessor)
    if second_match_state == "changed":
        payload = dict(other_candidate.payload_json)
        fields = dict(payload["fields"])
        fields["potential_conflict"] = "N"
        payload["fields"] = fields
        other_candidate.payload_json = payload
    other_run = _completed_run(session, other_predecessor, other_candidate)
    other_dependency = accept_candidate(
        session, other_candidate, principal=REVIEWER
    )
    other_evidence = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == other_dependency.id)
        .order_by(EvidenceLink.id)
    ).first()
    assert other_evidence is not None
    mark_satisfies(
        session,
        other_dependency.id,
        other_evidence.id,
        principal=REVIEWER,
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-X",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=scenario["project"].id,
    )

    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    first_comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2-a",
    )
    second_comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=other_run.id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2-x",
    )
    [first_finding] = read_revision_comparison(
        session, first_comparison.id
    ).findings
    [second_finding] = read_revision_comparison(
        session, second_comparison.id
    ).findings
    assert first_finding.state == "unchanged"
    assert second_finding.state == second_match_state

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    collision_reviews = tuple(
        item
        for item in worklist.ordinary
        if successor_candidate.id in item.successor_candidate_ids
    )
    assert len(collision_reviews) == 2
    assert {item.dependency_id for item in collision_reviews} == {
        scenario["dependency"].id,
        other_dependency.id,
    }
    assert {
        item.reason for item in collision_reviews
    } == {"successor_candidate_link_ambiguous"}
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        successor_candidate.id,
    ).id == successor_candidate.id


def test_ambiguous_match_stays_only_in_ordinary_adjudication(session):
    scenario = _superseded_dependency(session, candidate_baseline="IH 69")
    successor_candidate = _candidate(
        scenario["project"],
        scenario["successor"],
        utility_id="FOC9-9",
        station_from="101+50",
        baseline="IH 69",
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    states = {
        finding.state
        for finding in read_revision_comparison(session, comparison.id).findings
    }

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert states == {"ambiguous"}
    assert worklist.reconfirmation == ()
    [review] = worklist.ordinary
    assert review.status == "ambiguous"
    assert review.successor_candidate_ids == (successor_candidate.id,)


def test_dropped_predecessor_is_dependency_only_ordinary_work(session):
    scenario = _superseded_dependency(session)
    successor_run = _completed_run(session, scenario["successor"])
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert finding.state == "dropped"
    assert worklist.reconfirmation == ()
    [review] = worklist.ordinary
    assert review.status == "dropped"
    assert review.successor_candidate_ids == ()


def test_successor_unmatched_row_remains_ordinary_candidate_work(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"],
        scenario["successor"],
        utility_id="FOC9-999",
        station_from="",
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    findings = read_revision_comparison(session, comparison.id).findings

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert "unmatched" in {finding.state for finding in findings}
    assert worklist.reconfirmation == ()
    assert sum(
        successor_candidate.id in review.successor_candidate_ids
        for review in worklist.ordinary
    ) == 1


def test_an_ordinary_current_candidate_without_supersession_is_listed(session):
    project = Project(slug="ordinary-only", name="Ordinary only", is_synthetic=True)
    session.add(project)
    session.flush()
    document = _document(
        session,
        project,
        registry_id="CURRENT",
        sha="d",
        filename="current.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    candidate = _candidate(project, document)
    _completed_run(session, document, candidate)

    worklist = build_reviewer_worklist(session, project.id)

    assert worklist.reconfirmation == ()
    assert len(worklist.ordinary) == 1
    assert worklist.ordinary[0].successor_candidate_ids == (candidate.id,)
    assert worklist.ordinary[0].status == "candidate_adjudication"


def test_reconfirmation_moves_support_without_revising_the_dependency(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(session, scenario["successor"], successor_candidate)
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation
    original_title = scenario["dependency"].title
    original_state = successor_candidate.state
    original_assertions = tuple(
        session.scalars(
            select(Assertion)
            .where(Assertion.dependency_id == scenario["dependency"].id)
            .order_by(Assertion.id)
        ).all()
    )

    new_evidence = reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["predecessor"].id,
        successor_candidate_id=successor_candidate.id,
        comparison_id=comparison.id,
        finding_id=review.finding_id,
        scope_fingerprint=review.scope_fingerprint,
        principal=REVIEWER,
    )

    session.flush()
    refreshed = build_reviewer_worklist(session, scenario["project"].id)
    assert refreshed.reconfirmation == ()
    assert refreshed.ordinary == ()

    evidence = tuple(
        session.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.dependency_id == scenario["dependency"].id)
            .order_by(EvidenceLink.id)
        ).all()
    )
    assert {link.document_id for link in evidence} == {
        scenario["predecessor"].id,
        scenario["successor"].id,
    }
    assert new_evidence.document_id == scenario["successor"].id
    assert new_evidence.satisfies_requirement is True
    session.refresh(scenario["old_evidence"])
    assert scenario["old_evidence"].satisfies_requirement is True
    assert session.get(Project, scenario["project"].id) is not None
    assert scenario["dependency"].title == original_title
    assert successor_candidate.state == original_state == "pending"
    assert tuple(
        session.scalars(
            select(Assertion)
            .where(Assertion.dependency_id == scenario["dependency"].id)
            .order_by(Assertion.id)
        ).all()
    ) == original_assertions

    rules = {
        exception.rule
        for exception in evaluate_exceptions(session, scenario["project"].id)
        if exception.dependency_id == scenario["dependency"].id
    }
    assert "SUPERSEDED_CITATION" not in rules

    [entry] = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == scenario["dependency"].id,
            AuditLog.action == "reconfirm_operative_support",
        )
        .order_by(AuditLog.id.desc())
    ).all()
    assert entry.actor == REVIEWER.subject
    assert entry.human_principal == REVIEWER.subject
    assert entry.after_json["comparison_id"] == comparison.id
    assert entry.after_json["finding_id"] == review.finding_id
    assert entry.after_json["scope_fingerprint"] == [
        list(item) for item in review.scope_fingerprint
    ]
    assert entry.after_json["predecessor_candidate_id"] == scenario[
        "predecessor_candidate"
    ].id

    before_repeat = len(session.scalars(select(EvidenceLink)).all())
    with pytest.raises(ReconfirmationUnavailable):
        reconfirm_operative_support(
            session,
            project_id=scenario["project"].id,
            dependency_id=scenario["dependency"].id,
            predecessor_document_id=scenario["predecessor"].id,
            successor_candidate_id=successor_candidate.id,
            comparison_id=comparison.id,
            finding_id=review.finding_id,
            scope_fingerprint=review.scope_fingerprint,
            principal=REVIEWER,
        )
    assert len(session.scalars(select(EvidenceLink)).all()) == before_repeat


def test_exact_reconfirmation_can_continue_across_sequential_revisions(session):
    scenario = _superseded_dependency(session)
    middle_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    middle_run = _completed_run(
        session, scenario["successor"], middle_candidate
    )
    first_comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=middle_run.id,
        matcher_version="revision-correspondence-v2",
    )
    [first_review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation

    middle_evidence = reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["predecessor"].id,
        successor_candidate_id=middle_candidate.id,
        comparison_id=first_comparison.id,
        finding_id=first_review.finding_id,
        scope_fingerprint=first_review.scope_fingerprint,
        principal=REVIEWER,
    )
    assert middle_evidence.document_id == scenario["successor"].id
    assert middle_evidence.verified is True
    assert middle_evidence.satisfies_requirement is True
    session.refresh(scenario["old_evidence"])
    assert scenario["old_evidence"].satisfies_requirement is True

    terminal = _document(
        session,
        scenario["project"],
        registry_id="REV-C",
        sha="e",
        filename="revision-c.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-B",
                successor_registry_id="REV-C",
                replacement_date=date(2026, 8, 2),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=scenario["project"].id,
    )
    terminal_candidate = _candidate(scenario["project"], terminal)
    terminal_run = _completed_run(session, terminal, terminal_candidate)
    second_comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=middle_run.id,
        successor_extraction_run_id=terminal_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.ordinary == ()
    [second_review] = worklist.reconfirmation
    assert second_review.predecessor_document_id == scenario["successor"].id
    assert second_review.predecessor_candidate_ids == (middle_candidate.id,)
    assert second_review.successor_document_id == terminal.id
    assert second_review.successor_candidate_ids == (terminal_candidate.id,)
    terminal_evidence = reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["successor"].id,
        successor_candidate_id=terminal_candidate.id,
        comparison_id=second_comparison.id,
        finding_id=second_review.finding_id,
        scope_fingerprint=second_review.scope_fingerprint,
        principal=REVIEWER,
    )

    assert terminal_evidence.document_id == terminal.id
    assert terminal_evidence.page_no == 1
    assert terminal_evidence.quote == "FOC1-1 AT&T Telecom 100+00"
    assert terminal_evidence.verified is True
    assert terminal_evidence.satisfies_requirement is True
    session.refresh(scenario["old_evidence"])
    session.refresh(middle_evidence)
    assert scenario["old_evidence"].satisfies_requirement is True
    assert middle_evidence.satisfies_requirement is True
    final_worklist = build_reviewer_worklist(session, scenario["project"].id)
    assert final_worklist.reconfirmation == ()
    assert final_worklist.ordinary == ()

    reconfirmations = tuple(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id == scenario["dependency"].id,
                AuditLog.action == "reconfirm_operative_support",
            )
            .order_by(AuditLog.id)
        ).all()
    )
    assert len(reconfirmations) == 2
    assert all(
        entry.human_principal == REVIEWER.subject for entry in reconfirmations
    )
    assert reconfirmations[0].after_json["predecessor_candidate_id"] == scenario[
        "predecessor_candidate"
    ].id
    assert reconfirmations[0].after_json["successor_candidate_id"] == (
        middle_candidate.id
    )
    assert reconfirmations[1].after_json["predecessor_candidate_id"] == (
        middle_candidate.id
    )
    assert reconfirmations[1].after_json["successor_candidate_id"] == (
        terminal_candidate.id
    )


def test_malformed_prior_reconfirmation_principal_breaks_sequential_lineage(session):
    scenario = _superseded_dependency(session)
    middle_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    middle_run = _completed_run(
        session, scenario["successor"], middle_candidate
    )
    first_comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=middle_run.id,
        matcher_version="revision-correspondence-v2",
    )
    [first_review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation
    reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["predecessor"].id,
        successor_candidate_id=middle_candidate.id,
        comparison_id=first_comparison.id,
        finding_id=first_review.finding_id,
        scope_fingerprint=first_review.scope_fingerprint,
        principal=REVIEWER,
    )
    prior_reconfirmation = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == scenario["dependency"].id,
            AuditLog.action == "reconfirm_operative_support",
        )
    )
    assert prior_reconfirmation is not None
    prior_reconfirmation.human_principal = "agent"

    terminal = _document(
        session,
        scenario["project"],
        registry_id="REV-C",
        sha="e",
        filename="revision-c.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-B",
                successor_registry_id="REV-C",
                replacement_date=date(2026, 8, 2),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=scenario["project"].id,
    )
    terminal_candidate = _candidate(scenario["project"], terminal)
    terminal_run = _completed_run(session, terminal, terminal_candidate)
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=middle_run.id,
        successor_extraction_run_id=terminal_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    dependency_review = next(
        review
        for review in worklist.ordinary
        if review.dependency_id == scenario["dependency"].id
    )
    assert dependency_review.predecessor_document_id == scenario["successor"].id
    assert dependency_review.reason == "admission_not_attributable"
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        terminal_candidate.id,
    ).id == terminal_candidate.id


def test_reconfirmed_successor_is_not_reused_by_later_supersession_work(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    first_comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2-a",
    )
    [first_review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation
    reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["predecessor"].id,
        successor_candidate_id=successor_candidate.id,
        comparison_id=first_comparison.id,
        finding_id=first_review.finding_id,
        scope_fingerprint=first_review.scope_fingerprint,
        principal=REVIEWER,
    )
    first_reconfirmation_ids = tuple(
        session.scalars(
            select(AuditLog.id)
            .where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id == scenario["dependency"].id,
                AuditLog.action == "reconfirm_operative_support",
            )
            .order_by(AuditLog.id)
        ).all()
    )
    assert len(first_reconfirmation_ids) == 1

    later_predecessor = _document(
        session,
        scenario["project"],
        registry_id="REV-X",
        sha="f",
        filename="revision-x.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    later_candidate = _candidate(scenario["project"], later_predecessor)
    later_run = _completed_run(session, later_predecessor, later_candidate)
    later_dependency = accept_candidate(
        session, later_candidate, principal=REVIEWER
    )
    later_evidence = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == later_dependency.id)
        .order_by(EvidenceLink.id)
    ).first()
    assert later_evidence is not None
    mark_satisfies(
        session,
        later_dependency.id,
        later_evidence.id,
        principal=REVIEWER,
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-X",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 2),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=scenario["project"].id,
    )
    second_comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=later_run.id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2-x",
    )
    [second_finding] = read_revision_comparison(
        session, second_comparison.id
    ).findings
    assert second_finding.state == "unchanged"

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    later_review = next(
        item
        for item in worklist.ordinary
        if item.dependency_id == later_dependency.id
    )
    assert later_review.reason == "successor_candidate_already_reconfirmed"
    assert later_review.successor_candidate_ids == ()
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        successor_candidate.id,
    ) is None
    assert successor_candidate.state == "pending"
    assert successor_candidate.merged_into is None
    assert tuple(
        session.scalars(
            select(AuditLog.id)
            .where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id == scenario["dependency"].id,
                AuditLog.action == "reconfirm_operative_support",
            )
            .order_by(AuditLog.id)
        ).all()
    ) == first_reconfirmation_ids


def test_human_edited_predecessor_fields_refuse_the_unchanged_shortcut(session):
    scenario = _superseded_dependency(session, edit_predecessor=True)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    [review] = worklist.ordinary
    assert review.reason == "admission_fields_changed"
    before = len(session.scalars(select(EvidenceLink)).all())
    with pytest.raises(ReconfirmationUnavailable):
        reconfirm_operative_support(
            session,
            project_id=scenario["project"].id,
            dependency_id=scenario["dependency"].id,
            predecessor_document_id=scenario["predecessor"].id,
            successor_candidate_id=successor_candidate.id,
            comparison_id=comparison.id,
            finding_id=review.finding_id,
            scope_fingerprint=review.scope_fingerprint,
            principal=REVIEWER,
        )
    assert len(session.scalars(select(EvidenceLink)).all()) == before


@pytest.mark.parametrize(
    "provenance_case",
    ["unlinked_scope", "duplicate_candidate_citation"],
)
def test_every_moved_predecessor_scope_has_one_immutable_candidate_citation(
    session, provenance_case
):
    scenario = _superseded_dependency(
        session,
        duplicate_predecessor_citation=(
            provenance_case == "duplicate_candidate_citation"
        ),
    )
    if provenance_case == "unlinked_scope":
        scenario["predecessor"].pages = 2
        session.add(
            DocPage(
                document_id=scenario["predecessor"].id,
                page_no=2,
                text="Separate verified predecessor support",
                image_path="/tmp/revision-a-page-2.png",
            )
        )
        unrelated_support = EvidenceLink(
            dependency_id=scenario["dependency"].id,
            document_id=scenario["predecessor"].id,
            page_no=2,
            quote="Separate verified predecessor support",
            verified=True,
            satisfies_requirement=False,
        )
        session.add(unrelated_support)
        session.flush()
        designate_publication_support(
            session,
            scenario["dependency"].id,
            unrelated_support.id,
            field_name="station_to",
            principal=REVIEWER,
        )

    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    review = next(
        item
        for item in worklist.ordinary
        if item.dependency_id == scenario["dependency"].id
    )
    assert review.reason == "predecessor_support_provenance_unsafe"
    assert review.finding_id is not None
    evidence_before = tuple(
        session.scalars(
            select(EvidenceLink.id)
            .where(EvidenceLink.dependency_id == scenario["dependency"].id)
            .order_by(EvidenceLink.id)
        ).all()
    )
    support_before = tuple(
        session.scalars(
            select(OperativeSupport.id)
            .where(
                OperativeSupport.dependency_id == scenario["dependency"].id
            )
            .order_by(OperativeSupport.id)
        ).all()
    )
    audit_before = tuple(
        session.scalars(
            select(AuditLog.id)
            .where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id == scenario["dependency"].id,
            )
            .order_by(AuditLog.id)
        ).all()
    )

    with pytest.raises(
        ReconfirmationUnavailable,
        match="stale or no longer safe",
    ):
        reconfirm_operative_support(
            session,
            project_id=scenario["project"].id,
            dependency_id=scenario["dependency"].id,
            predecessor_document_id=scenario["predecessor"].id,
            successor_candidate_id=successor_candidate.id,
            comparison_id=comparison.id,
            finding_id=review.finding_id,
            scope_fingerprint=review.scope_fingerprint,
            principal=REVIEWER,
        )

    assert tuple(
        session.scalars(
            select(EvidenceLink.id)
            .where(EvidenceLink.dependency_id == scenario["dependency"].id)
            .order_by(EvidenceLink.id)
        ).all()
    ) == evidence_before
    assert tuple(
        session.scalars(
            select(OperativeSupport.id)
            .where(
                OperativeSupport.dependency_id == scenario["dependency"].id
            )
            .order_by(OperativeSupport.id)
        ).all()
    ) == support_before
    assert tuple(
        session.scalars(
            select(AuditLog.id)
            .where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id == scenario["dependency"].id,
            )
            .order_by(AuditLog.id)
        ).all()
    ) == audit_before


def test_live_successor_drift_after_comparison_refuses_with_zero_writes(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    [safe_review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation
    changed_payload = dict(successor_candidate.payload_json)
    changed_fields = dict(changed_payload["fields"])
    changed_fields["station_from"] = "999+00"
    changed_payload["fields"] = changed_fields
    successor_candidate.payload_json = changed_payload
    session.flush()

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    assert any(
        review.reason == "successor_candidate_changed"
        for review in worklist.ordinary
    )
    before = len(session.scalars(select(EvidenceLink)).all())
    with pytest.raises(ReconfirmationUnavailable):
        reconfirm_operative_support(
            session,
            project_id=scenario["project"].id,
            dependency_id=scenario["dependency"].id,
            predecessor_document_id=scenario["predecessor"].id,
            successor_candidate_id=successor_candidate.id,
            comparison_id=comparison.id,
            finding_id=safe_review.finding_id,
            scope_fingerprint=safe_review.scope_fingerprint,
            principal=REVIEWER,
        )
    assert len(session.scalars(select(EvidenceLink)).all()) == before


def test_locked_rederivation_discards_a_stale_identity_map(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    [safe_review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation
    stale_payload = successor_candidate.payload_json
    changed_payload = dict(stale_payload)
    changed_fields = dict(changed_payload["fields"])
    changed_fields["station_from"] = "999+00"
    changed_payload["fields"] = changed_fields
    session.execute(
        update(Candidate)
        .where(Candidate.id == successor_candidate.id)
        .values(payload_json=changed_payload)
        .execution_options(synchronize_session=False)
    )
    # This is the stale object a long-lived reviewer Session can retain after
    # another transaction commits. The mutation boundary must discard it
    # after taking the project lock and re-read policy inputs from Postgres.
    assert successor_candidate.payload_json == stale_payload

    before = len(session.scalars(select(EvidenceLink)).all())
    with pytest.raises(ReconfirmationUnavailable):
        reconfirm_operative_support(
            session,
            project_id=scenario["project"].id,
            dependency_id=scenario["dependency"].id,
            predecessor_document_id=scenario["predecessor"].id,
            successor_candidate_id=successor_candidate.id,
            comparison_id=comparison.id,
            finding_id=safe_review.finding_id,
            scope_fingerprint=safe_review.scope_fingerprint,
            principal=REVIEWER,
        )
    assert len(session.scalars(select(EvidenceLink)).all()) == before


def test_cross_session_support_change_refuses_a_stale_reconfirmation_without_writes():
    project_id: int | None = None
    with Session() as setup:
        scenario = _superseded_dependency(
            setup,
            slug=f"supersession-review-race-{uuid4().hex}",
        )
        successor_candidate = _candidate(
            scenario["project"], scenario["successor"]
        )
        successor_run = _completed_run(
            setup, scenario["successor"], successor_candidate
        )
        comparison = create_revision_comparison(
            setup,
            predecessor_extraction_run_id=scenario["predecessor_run"].id,
            successor_extraction_run_id=successor_run.id,
            matcher_version="revision-correspondence-v2",
        )
        project_id = scenario["project"].id
        dependency_id = scenario["dependency"].id
        predecessor_document_id = scenario["predecessor"].id
        successor_document_id = scenario["successor"].id
        successor_candidate_id = successor_candidate.id
        comparison_id = comparison.id
        old_evidence_id = scenario["old_evidence"].id
        setup.commit()

    stale_reviewer = Session()
    support_reviewer = Session()
    try:
        [stale_review] = build_reviewer_worklist(
            stale_reviewer, project_id
        ).reconfirmation
        stale_evidence = stale_reviewer.get(EvidenceLink, old_evidence_id)
        stale_designation = stale_reviewer.scalar(
            select(OperativeSupport).where(
                OperativeSupport.dependency_id == dependency_id,
                OperativeSupport.role == "publication",
                OperativeSupport.field_name.is_(None),
            )
        )
        assert stale_evidence is not None
        assert stale_designation is not None
        assert stale_evidence.satisfies_requirement is True
        assert stale_designation.evidence_link_id == old_evidence_id

        current_evidence = EvidenceLink(
            dependency_id=dependency_id,
            document_id=successor_document_id,
            page_no=1,
            quote="FOC1-1 AT&T Telecom 100+00",
            verified=True,
            satisfies_requirement=False,
        )
        support_reviewer.add(current_evidence)
        support_reviewer.flush()
        designate_publication_support(
            support_reviewer,
            dependency_id,
            current_evidence.id,
            principal=REVIEWER,
        )
        assert mark_satisfies(
            support_reviewer,
            dependency_id,
            old_evidence_id,
            principal=REVIEWER,
        ) is False
        assert mark_satisfies(
            support_reviewer,
            dependency_id,
            current_evidence.id,
            principal=REVIEWER,
        ) is True
        support_reviewer.commit()

        # The first Session still carries the pre-transition objects. The
        # mutation boundary, not the caller, is responsible for expiring and
        # re-deriving them after it joins the shared project-lock order.
        assert stale_evidence.satisfies_requirement is True
        assert stale_designation.evidence_link_id == old_evidence_id

        evidence_before = tuple(
            stale_reviewer.scalars(
                select(EvidenceLink.id)
                .where(EvidenceLink.dependency_id == dependency_id)
                .order_by(EvidenceLink.id)
            ).all()
        )
        audit_before = tuple(
            stale_reviewer.scalars(
                select(AuditLog.id)
                .where(
                    AuditLog.entity_type == "dependency",
                    AuditLog.entity_id == dependency_id,
                )
                .order_by(AuditLog.id)
            ).all()
        )

        with pytest.raises(
            ReconfirmationUnavailable,
            match="stale or no longer safe",
        ):
            reconfirm_operative_support(
                stale_reviewer,
                project_id=project_id,
                dependency_id=dependency_id,
                predecessor_document_id=predecessor_document_id,
                successor_candidate_id=successor_candidate_id,
                comparison_id=comparison_id,
                finding_id=stale_review.finding_id,
                scope_fingerprint=stale_review.scope_fingerprint,
                principal=REVIEWER,
            )

        assert tuple(
            stale_reviewer.scalars(
                select(EvidenceLink.id)
                .where(EvidenceLink.dependency_id == dependency_id)
                .order_by(EvidenceLink.id)
            ).all()
        ) == evidence_before
        assert tuple(
            stale_reviewer.scalars(
                select(AuditLog.id)
                .where(
                    AuditLog.entity_type == "dependency",
                    AuditLog.entity_id == dependency_id,
                )
                .order_by(AuditLog.id)
            ).all()
        ) == audit_before
    finally:
        stale_reviewer.rollback()
        stale_reviewer.close()
        support_reviewer.rollback()
        support_reviewer.close()
        if project_id is not None:
            _delete_committed_review_project(project_id)


def test_scope_fingerprint_refuses_cross_session_scope_drift_without_writes():
    project_id: int | None = None
    with Session() as setup:
        scenario = _superseded_dependency(
            setup,
            slug=f"supersession-review-scope-race-{uuid4().hex}",
        )
        successor_candidate = _candidate(
            scenario["project"], scenario["successor"]
        )
        successor_run = _completed_run(
            setup, scenario["successor"], successor_candidate
        )
        comparison = create_revision_comparison(
            setup,
            predecessor_extraction_run_id=scenario["predecessor_run"].id,
            successor_extraction_run_id=successor_run.id,
            matcher_version="revision-correspondence-v2",
        )
        project_id = scenario["project"].id
        dependency_id = scenario["dependency"].id
        predecessor_document_id = scenario["predecessor"].id
        successor_candidate_id = successor_candidate.id
        comparison_id = comparison.id
        old_evidence_id = scenario["old_evidence"].id
        setup.commit()

    stale_reviewer = Session()
    support_reviewer = Session()
    try:
        [stale_review] = build_reviewer_worklist(
            stale_reviewer, project_id
        ).reconfirmation
        original_fingerprint = stale_review.scope_fingerprint
        assert original_fingerprint

        designate_publication_support(
            support_reviewer,
            dependency_id,
            old_evidence_id,
            field_name="station_to",
            principal=REVIEWER,
        )
        support_reviewer.commit()

        with Session() as current_reader:
            [current_review] = build_reviewer_worklist(
                current_reader, project_id
            ).reconfirmation
            assert (
                current_review.dependency_id,
                current_review.predecessor_document_id,
                current_review.successor_candidate_id,
                current_review.comparison_id,
                current_review.finding_id,
            ) == (
                stale_review.dependency_id,
                stale_review.predecessor_document_id,
                stale_review.successor_candidate_id,
                stale_review.comparison_id,
                stale_review.finding_id,
            )
            assert current_review.scope_fingerprint != original_fingerprint

        evidence_before = tuple(
            stale_reviewer.execute(
                select(
                    EvidenceLink.id,
                    EvidenceLink.document_id,
                    EvidenceLink.page_no,
                    EvidenceLink.quote,
                    EvidenceLink.satisfies_requirement,
                )
                .where(EvidenceLink.dependency_id == dependency_id)
                .order_by(EvidenceLink.id)
            ).all()
        )
        support_before = tuple(
            stale_reviewer.execute(
                select(
                    OperativeSupport.id,
                    OperativeSupport.evidence_link_id,
                    OperativeSupport.role,
                    OperativeSupport.field_name,
                    OperativeSupport.designated_by,
                )
                .where(OperativeSupport.dependency_id == dependency_id)
                .order_by(OperativeSupport.id)
            ).all()
        )
        audit_before = tuple(
            stale_reviewer.scalars(
                select(AuditLog.id)
                .where(
                    AuditLog.entity_type == "dependency",
                    AuditLog.entity_id == dependency_id,
                )
                .order_by(AuditLog.id)
            ).all()
        )

        with pytest.raises(
            ReconfirmationUnavailable,
            match="stale or no longer safe",
        ):
            reconfirm_operative_support(
                stale_reviewer,
                project_id=project_id,
                dependency_id=dependency_id,
                predecessor_document_id=predecessor_document_id,
                successor_candidate_id=successor_candidate_id,
                comparison_id=comparison_id,
                finding_id=stale_review.finding_id,
                scope_fingerprint=original_fingerprint,
                principal=REVIEWER,
            )

        assert tuple(
            stale_reviewer.execute(
                select(
                    EvidenceLink.id,
                    EvidenceLink.document_id,
                    EvidenceLink.page_no,
                    EvidenceLink.quote,
                    EvidenceLink.satisfies_requirement,
                )
                .where(EvidenceLink.dependency_id == dependency_id)
                .order_by(EvidenceLink.id)
            ).all()
        ) == evidence_before
        assert tuple(
            stale_reviewer.execute(
                select(
                    OperativeSupport.id,
                    OperativeSupport.evidence_link_id,
                    OperativeSupport.role,
                    OperativeSupport.field_name,
                    OperativeSupport.designated_by,
                )
                .where(OperativeSupport.dependency_id == dependency_id)
                .order_by(OperativeSupport.id)
            ).all()
        ) == support_before
        assert tuple(
            stale_reviewer.scalars(
                select(AuditLog.id)
                .where(
                    AuditLog.entity_type == "dependency",
                    AuditLog.entity_id == dependency_id,
                )
                .order_by(AuditLog.id)
            ).all()
        ) == audit_before
    finally:
        stale_reviewer.rollback()
        stale_reviewer.close()
        support_reviewer.rollback()
        support_reviewer.close()
        if project_id is not None:
            _delete_committed_review_project(project_id)


def test_multiple_successor_citations_never_offer_one_key_reconfirmation(session):
    scenario = _superseded_dependency(session)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    payload = dict(successor_candidate.payload_json)
    payload["citations"] = [
        *payload["citations"],
        {
            "document_id": scenario["successor"].id,
            "page": 1,
            "quote": "FOC1-1 AT&T Telecom 100+00",
            "verified": True,
        },
    ]
    successor_candidate.payload_json = payload
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    assert any(
        review.reason == "successor_provenance_unsafe"
        for review in worklist.ordinary
    )


def test_reconfirmation_moves_every_publication_scope_in_one_transaction(session):
    scenario = _superseded_dependency(session)
    designate_publication_support(
        session,
        scenario["dependency"].id,
        scenario["old_evidence"].id,
        field_name="station_from",
        principal=REVIEWER,
    )
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation

    new_evidence = reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["predecessor"].id,
        successor_candidate_id=successor_candidate.id,
        comparison_id=comparison.id,
        finding_id=review.finding_id,
        scope_fingerprint=review.scope_fingerprint,
        principal=REVIEWER,
    )

    designations = tuple(
        session.scalars(
            select(OperativeSupport)
            .where(OperativeSupport.dependency_id == scenario["dependency"].id)
            .order_by(OperativeSupport.id)
        ).all()
    )
    assert {(row.role, row.field_name) for row in designations} == {
        ("publication", None),
        ("publication", "station_from"),
    }
    assert {row.evidence_link_id for row in designations} == {new_evidence.id}


def test_reconfirmation_refuses_to_move_only_one_of_two_stale_documents(session):
    scenario = _superseded_dependency(session)
    other_predecessor = _document(
        session,
        scenario["project"],
        registry_id="REV-X",
        sha="f",
        filename="revision-x.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    _document(
        session,
        scenario["project"],
        registry_id="REV-Y",
        sha="g",
        filename="revision-y.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    other_evidence = EvidenceLink(
        dependency_id=scenario["dependency"].id,
        document_id=other_predecessor.id,
        page_no=1,
        quote="FOC1-1 AT&T Telecom 100+00",
        verified=True,
        satisfies_requirement=False,
    )
    session.add(other_evidence)
    session.flush()
    designate_publication_support(
        session,
        scenario["dependency"].id,
        other_evidence.id,
        field_name="station_to",
        principal=REVIEWER,
    )
    mark_satisfies(
        session,
        scenario["dependency"].id,
        other_evidence.id,
        principal=REVIEWER,
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-X",
                successor_registry_id="REV-Y",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=scenario["project"].id,
    )
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    assert {
        review.predecessor_document_id
        for review in worklist.ordinary
        if review.dependency_id == scenario["dependency"].id
    } == {scenario["predecessor"].id, other_predecessor.id}
    selected = next(
        review
        for review in worklist.ordinary
        if review.predecessor_document_id == scenario["predecessor"].id
    )
    assert selected.reason == "partial_scope_transfer"
    before = len(session.scalars(select(EvidenceLink)).all())
    with pytest.raises(ReconfirmationUnavailable):
        reconfirm_operative_support(
            session,
            project_id=scenario["project"].id,
            dependency_id=scenario["dependency"].id,
            predecessor_document_id=scenario["predecessor"].id,
            successor_candidate_id=successor_candidate.id,
            comparison_id=comparison.id,
            finding_id=selected.finding_id,
            scope_fingerprint=selected.scope_fingerprint,
            principal=REVIEWER,
        )
    assert len(session.scalars(select(EvidenceLink)).all()) == before
    session.refresh(scenario["old_evidence"])
    session.refresh(other_evidence)
    assert scenario["old_evidence"].satisfies_requirement is True
    assert other_evidence.satisfies_requirement is True


def test_reconfirmation_does_not_invent_readiness(session):
    scenario = _superseded_dependency(session, satisfying=False)
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    successor_run = _completed_run(
        session, scenario["successor"], successor_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2",
    )
    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation

    new_evidence = reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["predecessor"].id,
        successor_candidate_id=successor_candidate.id,
        comparison_id=comparison.id,
        finding_id=review.finding_id,
        scope_fingerprint=review.scope_fingerprint,
        principal=REVIEWER,
    )

    assert new_evidence.satisfies_requirement is False


def test_multi_hop_chain_refuses_an_intermediate_successor(session):
    scenario = _superseded_dependency(session)
    terminal = _document(
        session,
        scenario["project"],
        registry_id="REV-C",
        sha="e",
        filename="revision-c.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-B",
                successor_registry_id="REV-C",
                replacement_date=date(2026, 8, 2),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=scenario["project"].id,
    )

    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).ordinary

    assert terminal.superseded_by is None
    assert review.status == "blocked"
    assert review.reason == "multi_hop_supersession"
    assert review.reconfirmation_available is False
