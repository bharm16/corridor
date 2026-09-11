"""Live Supersession Review routing and human Reconfirmation.

The worklist is derived state.  These tests exercise the public domain seam
against Postgres so a web route cannot accidentally become the implementation
of registry, comparison, or operative-support policy.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import and_, delete, select, text, update
from sqlalchemy.exc import IntegrityError

from corridor import audit
from corridor.adjudicate import accept_candidate, edit_candidate
from corridor.db import Session
from corridor.extraction_runs import (
    declare_active_run,
    record_extraction_run,
)
from corridor.exceptions import evaluate as evaluate_exceptions
from corridor.ledger import mark_satisfies
from committed_scenario_support import delete_committed_project

from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    DependencyEvidenceSufficiency,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    ExtractionRun,
    OperativeSupport,
    Project,
    ReconfirmationReceipt,
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

from proposal_support import proposal


REVIEWER = HumanPrincipal("local:supersession-reviewer")


def _has_direct_readiness(session, evidence: EvidenceLink) -> bool:
    return session.scalar(
        select(DependencyEvidenceSufficiency.id).where(
            DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
            DependencyEvidenceSufficiency.scope_link_id.is_(None),
        )
    ) is not None


def _set_direct_readiness(session, evidence: EvidenceLink, ready: bool) -> None:
    role = session.scalar(
        select(DependencyEvidenceSufficiency).where(
            DependencyEvidenceSufficiency.dependency_id == evidence.dependency_id,
            DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
            DependencyEvidenceSufficiency.scope_link_id.is_(None),
        )
    )
    if ready and role is None:
        session.add(
            DependencyEvidenceSufficiency(
                dependency_id=evidence.dependency_id,
                evidence_link_id=evidence.id,
                scope_link_id=None,
            )
        )
    elif not ready and role is not None:
        session.delete(role)
    session.flush()


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
    return proposal(
        document,
        fields=fields,
        quote=row_quote,
        prompt_version="matrix-v1",
        model="test-model",
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
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=REVIEWER)
    session.flush()
    return run


def _register_external_org(session, name: str = "AT&T") -> None:
    """Register the External Organization these matrix candidates name.

    Issue #345 stopped admission from silently minting an organization from an
    unfamiliar source spelling: accepting a Candidate whose ``external_org`` is
    not already registered now refuses.  Every matrix row built here names
    "AT&T", so the registry must hold that exact spelling before any candidate
    is accepted.  Idempotent because a single Session (and one per-worker
    database) can build several scenarios and ``ExternalOrg.name`` is unique.
    """
    if session.scalar(select(ExternalOrg).where(ExternalOrg.name == name)) is None:
        session.add(ExternalOrg(name=name, aliases=[]))
        session.flush()


def _superseded_dependency(
    session,
    *,
    satisfying: bool = True,
    edit_predecessor: bool = False,
    slug: str = "supersession-review-test",
    candidate_baseline: str | None = None,
    duplicate_predecessor_citation: bool = False,
    include_unrelated_predecessor: bool = False,
):
    project = Project(
        slug=slug,
        name="Supersession Review Test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    _register_external_org(session)
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
    unrelated_predecessor_candidate = None
    predecessor_candidates = [predecessor_candidate]
    if include_unrelated_predecessor:
        unrelated_predecessor_candidate = _candidate(
            project,
            predecessor,
            utility_id="FOC9-9",
            station_from="900+00",
        )
        predecessor_candidates.append(unrelated_predecessor_candidate)
    predecessor_run = _completed_run(
        session, predecessor, *predecessor_candidates
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
        "unrelated_predecessor_candidate": (
            unrelated_predecessor_candidate
        ),
        "predecessor_run": predecessor_run,
        "dependency": dependency,
        "old_evidence": old_evidence,
    }


def _reconfirm_first_revision(
    session,
    scenario,
    *,
    current_readiness_before: bool = False,
):
    existing_successor_evidence = None
    if current_readiness_before:
        existing_successor_evidence = EvidenceLink(
            dependency_id=scenario["dependency"].id,
            document_id=scenario["successor"].id,
            page_no=1,
            quote="FOC1-1 AT&T Telecom 100+00",
            verified=True,
        )
        session.add(existing_successor_evidence)
        session.flush([existing_successor_evidence])
        mark_satisfies(
            session,
            scenario["dependency"].id,
            existing_successor_evidence.id,
            principal=REVIEWER,
        )

    middle_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    middle_run = _completed_run(
        session, scenario["successor"], middle_candidate
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=middle_run.id,
        matcher_version="revision-correspondence-v2-first",
    )
    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation
    middle_evidence = reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["predecessor"].id,
        successor_candidate_id=middle_candidate.id,
        comparison_id=comparison.id,
        finding_id=review.finding_id,
        scope_fingerprint=review.scope_fingerprint,
        principal=REVIEWER,
    )
    receipt = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == scenario["dependency"].id,
            AuditLog.action == "reconfirm_operative_support",
        )
    )
    assert receipt is not None
    durable_receipt = session.get(ReconfirmationReceipt, receipt.id)
    assert durable_receipt is not None
    assert durable_receipt.dependency_id == scenario["dependency"].id
    assert durable_receipt.successor_candidate_id == middle_candidate.id
    assert durable_receipt.before_json == receipt.before_json
    assert durable_receipt.after_json == receipt.after_json
    return {
        "middle_candidate": middle_candidate,
        "middle_run": middle_run,
        "middle_evidence": middle_evidence,
        "existing_successor_evidence": existing_successor_evidence,
        "receipt": receipt,
        "durable_receipt": durable_receipt,
    }


def _prepare_terminal_revision(session, scenario, first):
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
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=first["middle_run"].id,
        successor_extraction_run_id=terminal_run.id,
        matcher_version="revision-correspondence-v2-terminal",
    )
    return terminal, terminal_candidate, comparison


def _delete_committed_review_project(project_id: int) -> None:
    """Remove the committed rows used by the two-Session regression."""

    delete_committed_project(project_id, session_factory=Session)
    # The registry is global, not project-scoped: leaving the committed
    # organization behind pollutes other files' registry-wide reads on the
    # same guarded worker database (duplicate-name seeds, org counts, and
    # deterministic resolution). Its dependencies are already gone above.
    with Session() as cleanup:
        cleanup.execute(delete(ExternalOrg).where(ExternalOrg.name == "AT&T"))
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
        allow_unsealed_legacy=True,
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
        allow_unsealed_legacy=True,
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
        allow_unsealed_legacy=True,
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
        if item.dependency_id
        in {scenario["dependency"].id, other_dependency.id}
    )
    assert len(collision_reviews) == 2
    assert {item.dependency_id for item in collision_reviews} == {
        scenario["dependency"].id,
        other_dependency.id,
    }
    assert {
        item.reason for item in collision_reviews
    } == {"successor_candidate_link_ambiguous"}
    assert sum(
        successor_candidate.id in item.successor_candidate_ids
        for item in collision_reviews
    ) == 1
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        successor_candidate.id,
    ).id == successor_candidate.id


@pytest.mark.parametrize(
    "admission_corruption",
    [
        "missing_principal",
        "non_human_principal",
        "missing_candidate_id",
        "malformed_candidate_id",
        "nonexistent_candidate_id",
    ],
)
def test_fan_in_collision_survives_a_corrupt_admission(
    session, admission_corruption
):
    scenario = _superseded_dependency(session)
    other_predecessor = _document(
        session,
        scenario["project"],
        registry_id="REV-X",
        sha="f",
        filename="revision-x.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    other_candidate = _candidate(scenario["project"], other_predecessor)
    other_run = _completed_run(session, other_predecessor, other_candidate)
    other_dependency = accept_candidate(
        session, other_candidate, principal=REVIEWER
    )
    other_admission = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == other_dependency.id,
            AuditLog.action == "accept_candidate",
        )
    )
    assert other_admission is not None
    if admission_corruption == "missing_principal":
        other_admission.human_principal = None
    elif admission_corruption == "non_human_principal":
        other_admission.human_principal = "agent"
    else:
        admission_after = dict(other_admission.after_json or {})
        if admission_corruption == "missing_candidate_id":
            admission_after.pop("candidate_id", None)
        elif admission_corruption == "malformed_candidate_id":
            admission_after["candidate_id"] = "not-a-candidate-id"
        else:
            admission_after["candidate_id"] = 2_147_483_647
        other_admission.after_json = admission_after
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
    assert [
        finding.state
        for finding in read_revision_comparison(
            session, first_comparison.id
        ).findings
    ] == ["unchanged"]
    assert [
        finding.state
        for finding in read_revision_comparison(
            session, second_comparison.id
        ).findings
    ] == ["unchanged"]

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    affected = tuple(
        item
        for item in worklist.ordinary
        if item.dependency_id
        in {scenario["dependency"].id, other_dependency.id}
    )
    assert len(affected) == 2
    assert {item.reason for item in affected} == {
        "successor_candidate_link_ambiguous"
    }
    assert sum(
        successor_candidate.id in item.successor_candidate_ids
        for item in worklist.ordinary
    ) == 1
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        successor_candidate.id,
    ).id == successor_candidate.id


def test_fan_in_collision_survives_a_corrupt_prior_reconfirmation(session):
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
    corrupt_after = dict(prior_reconfirmation.after_json)
    corrupt_after.pop("successor_candidate_id")
    prior_reconfirmation.after_json = corrupt_after

    terminal = _document(
        session,
        scenario["project"],
        registry_id="REV-C",
        sha="e",
        filename="revision-c.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    other_predecessor = _document(
        session,
        scenario["project"],
        registry_id="REV-X",
        sha="f",
        filename="revision-x.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    other_candidate = _candidate(scenario["project"], other_predecessor)
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
                predecessor_registry_id="REV-B",
                successor_registry_id="REV-C",
                replacement_date=date(2026, 8, 2),
                source_registry_id="INDEX",
                source_page=1,
            ),
            SupersessionDeclaration(
                predecessor_registry_id="REV-X",
                successor_registry_id="REV-C",
                replacement_date=date(2026, 8, 2),
                source_registry_id="INDEX",
                source_page=1,
            ),
        ],
        project_id=scenario["project"].id,
    )
    terminal_candidate = _candidate(scenario["project"], terminal)
    terminal_run = _completed_run(session, terminal, terminal_candidate)
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=middle_run.id,
        successor_extraction_run_id=terminal_run.id,
        matcher_version="revision-correspondence-v2-b",
    )
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=other_run.id,
        successor_extraction_run_id=terminal_run.id,
        matcher_version="revision-correspondence-v2-x",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    affected = tuple(
        item
        for item in worklist.ordinary
        if item.dependency_id
        in {scenario["dependency"].id, other_dependency.id}
    )
    assert len(affected) == 2
    assert {item.reason for item in affected} == {
        "successor_candidate_link_ambiguous"
    }
    assert sum(
        terminal_candidate.id in item.successor_candidate_ids
        for item in affected
    ) == 1
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        terminal_candidate.id,
    ).id == terminal_candidate.id


def test_corrupt_admission_does_not_reserve_unrelated_comparison_rows(session):
    project = Project(
        slug=f"multi-row-corrupt-admission-{uuid4()}",
        name="Multi-row Corrupt Admission",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    _register_external_org(session)
    predecessor = _document(
        session,
        project,
        registry_id="REV-A",
        sha="a",
        filename="multi-a.pdf",
        text="FOC1-1 AT&T Telecom 100+00\nFOC2-2 AT&T Telecom 200+00",
    )
    successor = _document(
        session,
        project,
        registry_id="REV-B",
        sha="b",
        filename="multi-b.pdf",
        text="FOC1-1 AT&T Telecom 100+00\nFOC2-2 AT&T Telecom 200+00",
    )
    _document(
        session,
        project,
        registry_id="INDEX",
        sha="c",
        filename="multi-index.pdf",
        text="REV-A superseded by REV-B on 2026-08-01",
    )
    first_predecessor = _candidate(project, predecessor)
    second_predecessor = _candidate(
        project,
        predecessor,
        utility_id="FOC2-2",
        station_from="200+00",
    )
    predecessor_run = _completed_run(
        session,
        predecessor,
        first_predecessor,
        second_predecessor,
    )
    first_dependency = accept_candidate(
        session, first_predecessor, principal=REVIEWER
    )
    second_dependency = accept_candidate(
        session, second_predecessor, principal=REVIEWER
    )
    for dependency in (first_dependency, second_dependency):
        evidence = session.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.dependency_id == dependency.id)
            .order_by(EvidenceLink.id)
        ).first()
        assert evidence is not None
        mark_satisfies(
            session,
            dependency.id,
            evidence.id,
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
    first_successor = _candidate(project, successor)
    second_successor = _candidate(
        project,
        successor,
        utility_id="FOC2-2",
        station_from="200+00",
    )
    successor_run = _completed_run(
        session,
        successor,
        first_successor,
        second_successor,
    )
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=predecessor_run.id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2-multi",
    )
    first_admission = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == first_dependency.id,
            AuditLog.action == "accept_candidate",
        )
    )
    assert first_admission is not None
    corrupt_after = dict(first_admission.after_json)
    corrupt_after.pop("candidate_id")
    first_admission.after_json = corrupt_after
    session.flush()

    worklist = build_reviewer_worklist(session, project.id)

    [second_review] = worklist.reconfirmation
    assert second_review.dependency_id == second_dependency.id
    assert second_review.successor_candidate_ids == (second_successor.id,)
    first_review = next(
        item
        for item in worklist.ordinary
        if item.dependency_id == first_dependency.id
    )
    assert first_review.successor_candidate_ids == (first_successor.id,)
    assert first_review.reason == "admission_history_corrupt"
    assert ordinary_candidate_for_update(
        session, project.id, first_successor.id
    ).id == first_successor.id
    assert ordinary_candidate_for_update(
        session, project.id, second_successor.id
    ) is None


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
    assert _has_direct_readiness(session, new_evidence)
    session.refresh(scenario["old_evidence"])
    assert _has_direct_readiness(session, scenario["old_evidence"])
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
    durable_receipt = session.get(ReconfirmationReceipt, entry.id)
    assert durable_receipt is not None
    assert durable_receipt.dependency_id == scenario["dependency"].id
    assert durable_receipt.successor_candidate_id == successor_candidate.id
    assert durable_receipt.before_json == entry.before_json
    assert durable_receipt.after_json == entry.after_json
    with pytest.raises(IntegrityError, match="append-only"):
        with session.begin_nested():
            session.execute(
                update(ReconfirmationReceipt)
                .where(ReconfirmationReceipt.audit_log_id == entry.id)
                .values(after_json={})
            )

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
    assert _has_direct_readiness(session, middle_evidence)
    session.refresh(scenario["old_evidence"])
    assert _has_direct_readiness(session, scenario["old_evidence"])

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
    assert _has_direct_readiness(session, terminal_evidence)
    session.refresh(scenario["old_evidence"])
    session.refresh(middle_evidence)
    assert _has_direct_readiness(session, scenario["old_evidence"])
    assert _has_direct_readiness(session, middle_evidence)
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
    support_transfers = audit.support_transfer_records_for_dependencies(
        session, (scenario["dependency"].id,)
    )[scenario["dependency"].id]
    assert [record.action for record in support_transfers] == [
        audit.RECONFIRM_OPERATIVE_SUPPORT,
        audit.RECONFIRM_OPERATIVE_SUPPORT,
    ]


def test_legacy_reconfirmation_lineage_rejects_a_forward_audit_pointer(session):
    scenario = _superseded_dependency(session, satisfying=False)
    first = _reconfirm_first_revision(session, scenario)
    terminal, terminal_candidate, second_comparison = _prepare_terminal_revision(
        session, scenario, first
    )
    [second_review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation
    reconfirm_operative_support(
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
    first_audit, second_audit = tuple(
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
    first_before = dict(first_audit.before_json)
    first_after = dict(first_audit.after_json)
    second_before = dict(second_audit.before_json)
    second_after = dict(second_audit.after_json)

    # Simulate pre-sealed legacy history, then invert the two acts while making
    # the now-earlier B->C receipt point forward to the now-later A->B act.
    session.execute(text("set local session_replication_role = replica"))
    session.execute(
        delete(ReconfirmationReceipt).where(
            ReconfirmationReceipt.audit_log_id.in_(
                (first_audit.id, second_audit.id)
            )
        )
    )
    session.execute(text("set local session_replication_role = origin"))
    first_audit.before_json = second_before
    first_audit.after_json = {
        **second_after,
        "predecessor_reconfirmation_audit_id": second_audit.id,
    }
    second_audit.before_json = first_before
    second_audit.after_json = first_after
    session.flush()

    final = _document(
        session,
        scenario["project"],
        registry_id="REV-D",
        sha="f",
        filename="revision-d.pdf",
        text="FOC1-1 AT&T Telecom 100+00",
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-C",
                successor_registry_id="REV-D",
                replacement_date=date(2026, 8, 3),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=scenario["project"].id,
    )
    terminal_run = session.get(
        ExtractionRun, second_comparison.successor_extraction_run_id
    )
    assert terminal_run is not None
    final_candidate = _candidate(scenario["project"], final)
    final_run = _completed_run(session, final, final_candidate)
    create_revision_comparison(
        session,
        predecessor_extraction_run_id=terminal_run.id,
        successor_extraction_run_id=final_run.id,
        matcher_version="revision-correspondence-v2-forward-audit",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    review = next(
        item
        for item in worklist.ordinary
        if item.dependency_id == scenario["dependency"].id
    )
    assert review.predecessor_document_id == terminal.id
    assert review.successor_candidate_ids == (final_candidate.id,)
    assert review.reason == "reconfirmation_lineage_chronology_invalid"


def test_later_attributable_source_readiness_toggle_preserves_lineage(session):
    scenario = _superseded_dependency(session)
    first = _reconfirm_first_revision(session, scenario)
    assert mark_satisfies(
        session,
        scenario["dependency"].id,
        scenario["old_evidence"].id,
        principal=REVIEWER,
    ) is False
    terminal, terminal_candidate, comparison = _prepare_terminal_revision(
        session, scenario, first
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.ordinary == ()
    [review] = worklist.reconfirmation
    assert review.predecessor_document_id == scenario["successor"].id
    assert {
        (scope.role, scope.evidence.evidence_link_id)
        for scope in review.superseded_scopes
    } == {
        ("publication", first["middle_evidence"].id),
        ("readiness", first["middle_evidence"].id),
    }
    reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["successor"].id,
        successor_candidate_id=terminal_candidate.id,
        comparison_id=comparison.id,
        finding_id=review.finding_id,
        scope_fingerprint=review.scope_fingerprint,
        principal=REVIEWER,
    )
    assert terminal.superseded_by is None


def test_later_attributable_readiness_addition_expands_next_scope(session):
    scenario = _superseded_dependency(session, satisfying=False)
    first = _reconfirm_first_revision(session, scenario)
    assert not _has_direct_readiness(session, first["middle_evidence"])
    assert mark_satisfies(
        session,
        scenario["dependency"].id,
        first["middle_evidence"].id,
        principal=REVIEWER,
    ) is True
    _prepare_terminal_revision(session, scenario, first)

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.ordinary == ()
    [review] = worklist.reconfirmation
    assert {scope.role for scope in review.superseded_scopes} == {
        "publication",
        "readiness",
    }


def test_later_attributable_readiness_lapse_allows_publication_only(session):
    scenario = _superseded_dependency(session, satisfying=False)
    first = _reconfirm_first_revision(session, scenario)
    assert mark_satisfies(
        session,
        scenario["dependency"].id,
        first["middle_evidence"].id,
        principal=REVIEWER,
    ) is True
    assert mark_satisfies(
        session,
        scenario["dependency"].id,
        first["middle_evidence"].id,
        principal=REVIEWER,
    ) is False
    _prepare_terminal_revision(session, scenario, first)

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.ordinary == ()
    [review] = worklist.reconfirmation
    assert [scope.role for scope in review.superseded_scopes] == [
        "publication"
    ]


def test_later_readiness_lapse_does_not_resurrect_transferred_source(session):
    scenario = _superseded_dependency(session)
    first = _reconfirm_first_revision(session, scenario)
    assert _has_direct_readiness(session, scenario["old_evidence"])
    assert _has_direct_readiness(session, first["middle_evidence"])
    assert mark_satisfies(
        session,
        scenario["dependency"].id,
        first["middle_evidence"].id,
        principal=REVIEWER,
    ) is False
    _prepare_terminal_revision(session, scenario, first)

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.ordinary == ()
    [review] = worklist.reconfirmation
    assert [scope.role for scope in review.superseded_scopes] == [
        "publication"
    ]


def test_preexisting_successor_readiness_lapse_remains_a_historical_tombstone(
    session,
):
    scenario = _superseded_dependency(session)
    successor_readiness = EvidenceLink(
        dependency_id=scenario["dependency"].id,
        document_id=scenario["successor"].id,
        page_no=1,
        quote="FOC1-1 AT&T Telecom 100+00",
        verified=True,
    )
    session.add(successor_readiness)
    session.flush([successor_readiness])
    assert mark_satisfies(
        session,
        scenario["dependency"].id,
        successor_readiness.id,
        principal=REVIEWER,
    ) is True
    assert mark_satisfies(
        session,
        scenario["dependency"].id,
        successor_readiness.id,
        principal=REVIEWER,
    ) is False

    first = _reconfirm_first_revision(session, scenario)

    assert [
        scope["role"]
        for scope in first["receipt"].before_json["operative_scopes"]
    ] == ["publication"]
    _prepare_terminal_revision(session, scenario, first)
    worklist = build_reviewer_worklist(session, scenario["project"].id)
    assert worklist.ordinary == ()
    [review] = worklist.reconfirmation
    assert [scope.role for scope in review.superseded_scopes] == [
        "publication"
    ]


def test_corrupt_readiness_history_cannot_restore_an_older_true_scope(session):
    scenario = _superseded_dependency(session)
    successor_readiness = EvidenceLink(
        dependency_id=scenario["dependency"].id,
        document_id=scenario["successor"].id,
        page_no=1,
        quote="FOC1-1 AT&T Telecom 100+00",
        verified=True,
    )
    session.add(successor_readiness)
    session.flush([successor_readiness])
    mark_satisfies(
        session,
        scenario["dependency"].id,
        successor_readiness.id,
        principal=REVIEWER,
    )
    mark_satisfies(
        session,
        scenario["dependency"].id,
        successor_readiness.id,
        principal=REVIEWER,
    )
    lapse = next(
        entry
        for entry in reversed(
            session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "dependency",
                    AuditLog.entity_id == scenario["dependency"].id,
                    AuditLog.action == "mark_satisfies_requirement",
                )
                .order_by(AuditLog.id)
            ).all()
        )
        if entry.after_json["evidence_link_id"] == successor_readiness.id
    )
    lapse.human_principal = None
    session.flush([lapse])

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
        matcher_version="revision-correspondence-v2-corrupt-readiness",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    [review] = worklist.ordinary
    assert review.reason == "readiness_history_untrusted"
    assert {scope.role for scope in review.superseded_scopes} == {
        "publication",
        "readiness",
    }
    assert review.successor_candidate_ids == (successor_candidate.id,)


def test_unaudited_readiness_flag_cannot_be_transferred(session):
    scenario = _superseded_dependency(session, satisfying=False)
    _set_direct_readiness(session, scenario["old_evidence"], True)
    current_publication = EvidenceLink(
        dependency_id=scenario["dependency"].id,
        document_id=scenario["successor"].id,
        page_no=1,
        quote="FOC1-1 AT&T Telecom 100+00",
        verified=True,
    )
    session.add(current_publication)
    session.flush([current_publication])
    designate_publication_support(
        session,
        scenario["dependency"].id,
        current_publication.id,
        principal=REVIEWER,
    )
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
        matcher_version="revision-correspondence-v2-unaudited-readiness",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    [review] = worklist.ordinary
    assert review.reason == "readiness_history_untrusted"
    assert [scope.role for scope in review.superseded_scopes] == [
        "readiness"
    ]
    assert review.successor_candidate_ids == (successor_candidate.id,)


def test_unaudited_readiness_lapse_cannot_offer_publication_only_reconfirmation(
    session,
):
    scenario = _superseded_dependency(session)
    _set_direct_readiness(session, scenario["old_evidence"], False)
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
        matcher_version="revision-correspondence-v2-unaudited-lapse",
    )

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    [review] = worklist.ordinary
    assert review.reason == "readiness_history_untrusted"
    assert {scope.role for scope in review.superseded_scopes} == {
        "publication",
        "readiness",
    }
    readiness_scope = next(
        scope
        for scope in review.superseded_scopes
        if scope.role == "readiness"
    )
    assert readiness_scope.evidence.evidence_link_id == scenario["old_evidence"].id
    readiness_evidence = session.get(
        EvidenceLink, readiness_scope.evidence.evidence_link_id
    )
    assert readiness_evidence is not None
    assert not _has_direct_readiness(session, readiness_evidence)
    assert review.successor_candidate_ids == (successor_candidate.id,)


def test_later_publication_scope_addition_expands_next_scope(session):
    scenario = _superseded_dependency(session, satisfying=False)
    first = _reconfirm_first_revision(session, scenario)
    designate_publication_support(
        session,
        scenario["dependency"].id,
        first["middle_evidence"].id,
        field_name="station_from",
        principal=REVIEWER,
    )
    _prepare_terminal_revision(session, scenario, first)

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.ordinary == ()
    [review] = worklist.reconfirmation
    assert {
        (scope.role, scope.field_name)
        for scope in review.superseded_scopes
    } == {
        ("publication", None),
        ("publication", "station_from"),
    }


def test_historical_frontier_omits_stale_readiness_when_successor_was_ready(
    session,
):
    scenario = _superseded_dependency(session)
    first = _reconfirm_first_revision(
        session, scenario, current_readiness_before=True
    )
    assert {
        scope["role"]
        for scope in first["receipt"].before_json["operative_scopes"]
    } == {"publication"}
    _prepare_terminal_revision(session, scenario, first)

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.ordinary == ()
    [review] = worklist.reconfirmation
    assert {scope.role for scope in review.superseded_scopes} == {
        "publication",
        "readiness",
    }
    readiness_ids = {
        scope.evidence.evidence_link_id
        for scope in review.superseded_scopes
        if scope.role == "readiness"
    }
    assert readiness_ids == {first["existing_successor_evidence"].id}


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


@pytest.mark.parametrize(
    "tampered_receipt_field",
    [
        "operative_scopes",
        "scope_fingerprint",
        "moved_scopes",
        "nonexistent_source_evidence",
        "wrong_dependency_source_evidence",
        "wrong_document_source_evidence",
        "unverified_source_evidence",
        "mismatched_citation_source_evidence",
        "lapsed_readiness_source_evidence",
        "omitted_readiness_scope",
        "omitted_readiness_scope_after_lapse",
    ],
)
def test_tampered_reconfirmation_transfer_receipt_breaks_sequential_lineage(
    session, tampered_receipt_field
):
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
    if tampered_receipt_field == "operative_scopes":
        prior_reconfirmation.before_json = {
            **prior_reconfirmation.before_json,
            "operative_scopes": [],
        }
    elif tampered_receipt_field in {"scope_fingerprint", "moved_scopes"}:
        prior_reconfirmation.after_json = {
            **prior_reconfirmation.after_json,
            tampered_receipt_field: [],
        }
    elif tampered_receipt_field == "lapsed_readiness_source_evidence":
        _set_direct_readiness(session, scenario["old_evidence"], False)
    elif tampered_receipt_field in {
        "omitted_readiness_scope",
        "omitted_readiness_scope_after_lapse",
    }:
        prior_reconfirmation.before_json = {
            **prior_reconfirmation.before_json,
            "operative_scopes": [
                scope
                for scope in prior_reconfirmation.before_json[
                    "operative_scopes"
                ]
                if scope["role"] != "readiness"
            ],
        }
        prior_reconfirmation.after_json = {
            **prior_reconfirmation.after_json,
            "scope_fingerprint": [
                scope
                for scope in prior_reconfirmation.after_json[
                    "scope_fingerprint"
                ]
                if scope[0] != "readiness"
            ],
            "moved_scopes": [
                scope
                for scope in prior_reconfirmation.after_json["moved_scopes"]
                if scope["role"] != "readiness"
            ],
        }
        if (
            tampered_receipt_field
            == "omitted_readiness_scope_after_lapse"
        ):
            mark_satisfies(
                session,
                scenario["dependency"].id,
                scenario["old_evidence"].id,
                principal=REVIEWER,
            )
            mark_satisfies(
                session,
                scenario["dependency"].id,
                prior_reconfirmation.after_json["new_evidence_link_id"],
                principal=REVIEWER,
            )
    else:
        if tampered_receipt_field == "nonexistent_source_evidence":
            source_evidence_id = 2_147_483_647
        elif tampered_receipt_field == "wrong_dependency_source_evidence":
            other = _superseded_dependency(
                session,
                slug=f"foreign-receipt-source-{uuid4()}",
            )
            source_evidence_id = other["old_evidence"].id
        elif tampered_receipt_field == "wrong_document_source_evidence":
            source_evidence_id = prior_reconfirmation.after_json[
                "new_evidence_link_id"
            ]
        else:
            source_evidence = EvidenceLink(
                dependency_id=scenario["dependency"].id,
                document_id=scenario["predecessor"].id,
                page_no=1,
                quote=(
                    "not the immutable predecessor citation"
                    if tampered_receipt_field
                    == "mismatched_citation_source_evidence"
                    else "FOC1-1 AT&T Telecom 100+00"
                ),
                verified=(
                    tampered_receipt_field != "unverified_source_evidence"
                ),
            )
            session.add(source_evidence)
            session.flush([source_evidence])
            _set_direct_readiness(session, source_evidence, True)
            source_evidence_id = source_evidence.id
        before_scopes = [
            {**scope, "evidence_link_id": source_evidence_id}
            for scope in prior_reconfirmation.before_json["operative_scopes"]
        ]
        after = dict(prior_reconfirmation.after_json)
        after["scope_fingerprint"] = [
            [role, field_name, source_evidence_id]
            for role, field_name, _ in after["scope_fingerprint"]
        ]
        after["moved_scopes"] = [
            {**scope, "from_evidence_link_id": source_evidence_id}
            for scope in after["moved_scopes"]
        ]
        prior_reconfirmation.before_json = {
            **prior_reconfirmation.before_json,
            "operative_scopes": before_scopes,
        }
        prior_reconfirmation.after_json = after
    session.flush()

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
    [second_finding] = read_revision_comparison(
        session, second_comparison.id
    ).findings
    assert second_finding.state == "unchanged"

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    later_review = next(
        item
        for item in worklist.ordinary
        if item.dependency_id == scenario["dependency"].id
        and item.predecessor_document_id == scenario["successor"].id
    )
    assert later_review.reason == "reconfirmation_history_corrupt"
    assert later_review.successor_candidate_ids == (terminal_candidate.id,)
    assert sum(
        terminal_candidate.id in item.successor_candidate_ids
        for item in worklist.ordinary
    ) == 1
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        terminal_candidate.id,
    ).id == terminal_candidate.id
    evidence_before = tuple(
        session.execute(
            select(
                EvidenceLink.id,
                EvidenceLink.document_id,
                EvidenceLink.page_no,
                EvidenceLink.quote,
                DependencyEvidenceSufficiency.id,
            )
            .outerjoin(
                DependencyEvidenceSufficiency,
                and_(
                    DependencyEvidenceSufficiency.evidence_link_id
                    == EvidenceLink.id,
                    DependencyEvidenceSufficiency.scope_link_id.is_(None),
                ),
            )
            .where(EvidenceLink.dependency_id == scenario["dependency"].id)
            .order_by(EvidenceLink.id)
        ).all()
    )
    support_before = tuple(
        session.execute(
            select(
                OperativeSupport.id,
                OperativeSupport.evidence_link_id,
                OperativeSupport.role,
                OperativeSupport.field_name,
            )
            .where(OperativeSupport.dependency_id == scenario["dependency"].id)
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
            predecessor_document_id=scenario["successor"].id,
            successor_candidate_id=terminal_candidate.id,
            comparison_id=second_comparison.id,
            finding_id=second_finding.id,
            scope_fingerprint=later_review.scope_fingerprint,
            principal=REVIEWER,
        )

    assert tuple(
        session.execute(
            select(
                EvidenceLink.id,
                EvidenceLink.document_id,
                EvidenceLink.page_no,
                EvidenceLink.quote,
                DependencyEvidenceSufficiency.id,
            )
            .outerjoin(
                DependencyEvidenceSufficiency,
                and_(
                    DependencyEvidenceSufficiency.evidence_link_id
                    == EvidenceLink.id,
                    DependencyEvidenceSufficiency.scope_link_id.is_(None),
                ),
            )
            .where(EvidenceLink.dependency_id == scenario["dependency"].id)
            .order_by(EvidenceLink.id)
        ).all()
    ) == evidence_before
    assert tuple(
        session.execute(
            select(
                OperativeSupport.id,
                OperativeSupport.evidence_link_id,
                OperativeSupport.role,
                OperativeSupport.field_name,
            )
            .where(OperativeSupport.dependency_id == scenario["dependency"].id)
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
    assert terminal_candidate.state == "pending"
    assert terminal_candidate.merged_into is None


def test_duplicate_publication_owner_corrupts_a_reconfirmation_receipt(session):
    scenario = _superseded_dependency(session)
    first = _reconfirm_first_revision(session, scenario)
    duplicate_source = EvidenceLink(
        dependency_id=scenario["dependency"].id,
        document_id=scenario["predecessor"].id,
        page_no=1,
        quote="FOC1-1 AT&T Telecom 100+00",
        verified=True,
    )
    session.add(duplicate_source)
    session.flush([duplicate_source])

    receipt = first["receipt"]
    before_scopes = [
        *receipt.before_json["operative_scopes"],
        {
            "role": "publication",
            "field_name": None,
            "evidence_link_id": duplicate_source.id,
        },
    ]
    before_scopes.sort(
        key=lambda scope: (
            scope["role"],
            scope["field_name"] or "",
            scope["evidence_link_id"],
        )
    )
    fingerprint = [
        [scope["role"], scope["field_name"], scope["evidence_link_id"]]
        for scope in before_scopes
    ]
    moves = [
        {
            "role": scope["role"],
            "field_name": scope["field_name"],
            "from_evidence_link_id": scope["evidence_link_id"],
            "to_evidence_link_id": receipt.after_json[
                "new_evidence_link_id"
            ],
        }
        for scope in before_scopes
    ]
    receipt.before_json = {
        **receipt.before_json,
        "operative_scopes": before_scopes,
    }
    receipt.after_json = {
        **receipt.after_json,
        "scope_fingerprint": fingerprint,
        "moved_scopes": moves,
    }
    _prepare_terminal_revision(session, scenario, first)

    worklist = build_reviewer_worklist(session, scenario["project"].id)

    assert worklist.reconfirmation == ()
    review = next(
        item
        for item in worklist.ordinary
        if item.dependency_id == scenario["dependency"].id
    )
    assert review.reason == "reconfirmation_history_corrupt"


@pytest.mark.parametrize(
    "stored_successor_candidate_id",
    [
        "valid",
        "missing",
        "malformed",
        "nonexistent",
        "missing_successor_and_comparison",
        "missing_successor_and_finding",
        "missing_all_comparison_identity",
        "missing_all_identity_except_moved_target",
    ],
)
def test_reconfirmed_successor_is_not_reused_by_later_supersession_work(
    session, stored_successor_candidate_id
):
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
    first_reconfirmations = tuple(
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
    assert len(first_reconfirmations) == 1
    [first_reconfirmation] = session.scalars(
        select(AuditLog).where(AuditLog.id == first_reconfirmations[0])
    ).all()
    if stored_successor_candidate_id != "valid":
        after = dict(first_reconfirmation.after_json)
        if stored_successor_candidate_id in {
            "missing",
            "missing_successor_and_comparison",
            "missing_successor_and_finding",
            "missing_all_comparison_identity",
            "missing_all_identity_except_moved_target",
        }:
            after.pop("successor_candidate_id", None)
            if (
                stored_successor_candidate_id
                in {
                    "missing_successor_and_comparison",
                    "missing_all_comparison_identity",
                    "missing_all_identity_except_moved_target",
                }
            ):
                after.pop("comparison_id", None)
            if (
                stored_successor_candidate_id
                in {
                    "missing_successor_and_finding",
                    "missing_all_comparison_identity",
                    "missing_all_identity_except_moved_target",
                }
            ):
                after.pop("finding_id", None)
            if (
                stored_successor_candidate_id
                == "missing_all_identity_except_moved_target"
            ):
                after.pop("new_evidence_link_id", None)
        elif stored_successor_candidate_id == "malformed":
            after["successor_candidate_id"] = "not-a-candidate-id"
        else:
            after["successor_candidate_id"] = 2_147_483_647
        first_reconfirmation.after_json = after
        session.flush()

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
    ) == first_reconfirmations


@pytest.mark.parametrize(
    "identity_corruption",
    [
        "wrong_stored_successor",
        "swapped_finding_without_successor",
        "wrong_new_evidence",
        "wrong_stored_and_finding",
        "wrong_stored_and_new_evidence",
        "wrong_finding_and_new_evidence",
        "wrong_stored_finding_and_new_evidence",
        "fully_malformed_audit_json",
        "wrong_audit_action",
        "wrong_audit_entity_type",
        "wrong_audit_entity_id",
    ],
)
def test_reconfirmation_recovery_does_not_hide_an_unrelated_candidate(
    session, identity_corruption
):
    scenario = _superseded_dependency(
        session, include_unrelated_predecessor=True
    )
    successor_candidate = _candidate(
        scenario["project"], scenario["successor"]
    )
    unrelated_candidate = _candidate(
        scenario["project"],
        scenario["successor"],
        utility_id="FOC9-9",
        station_from="900+00",
    )
    successor_run = _completed_run(
        session,
        scenario["successor"],
        successor_candidate,
        unrelated_candidate,
    )
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario["predecessor_run"].id,
        successor_extraction_run_id=successor_run.id,
        matcher_version="revision-correspondence-v2-recovery",
    )
    readback = read_revision_comparison(session, comparison.id)
    matched_finding = next(
        finding
        for finding in readback.findings
        if finding.successor_candidate_ids == [successor_candidate.id]
    )
    unrelated_finding = next(
        finding
        for finding in readback.findings
        if finding.successor_candidate_ids == [unrelated_candidate.id]
    )
    [review] = build_reviewer_worklist(
        session, scenario["project"].id
    ).reconfirmation
    assert review.finding_id == matched_finding.id
    reconfirm_operative_support(
        session,
        project_id=scenario["project"].id,
        dependency_id=scenario["dependency"].id,
        predecessor_document_id=scenario["predecessor"].id,
        successor_candidate_id=successor_candidate.id,
        comparison_id=comparison.id,
        finding_id=matched_finding.id,
        scope_fingerprint=review.scope_fingerprint,
        principal=REVIEWER,
    )
    receipt = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == scenario["dependency"].id,
            AuditLog.action == "reconfirm_operative_support",
        )
    )
    assert receipt is not None
    after = dict(receipt.after_json)
    reverse_lookup_candidate = None
    if identity_corruption == "fully_malformed_audit_json":
        receipt.before_json = []
        receipt.after_json = []
    elif identity_corruption == "wrong_audit_action":
        receipt.action = "set_resolution_strategy"
    elif identity_corruption == "wrong_audit_entity_type":
        receipt.entity_type = "candidate"
    elif identity_corruption == "wrong_audit_entity_id":
        other = _superseded_dependency(
            session,
            slug=f"receipt-reverse-lookup-{uuid4().hex}",
        )
        reverse_lookup_candidate = _candidate(
            other["project"], other["successor"]
        )
        _completed_run(
            session,
            other["successor"],
            reverse_lookup_candidate,
        )
        receipt.entity_id = other["dependency"].id
    if identity_corruption in {
        "wrong_stored_successor",
        "wrong_stored_and_finding",
        "wrong_stored_and_new_evidence",
        "wrong_stored_finding_and_new_evidence",
    }:
        after["successor_candidate_id"] = unrelated_candidate.id
    if identity_corruption == "swapped_finding_without_successor":
        after.pop("successor_candidate_id")
        after["finding_id"] = unrelated_finding.id
    elif identity_corruption in {
        "wrong_stored_and_finding",
        "wrong_finding_and_new_evidence",
        "wrong_stored_finding_and_new_evidence",
    }:
        after["finding_id"] = unrelated_finding.id
    if identity_corruption in {
        "wrong_new_evidence",
        "wrong_stored_and_new_evidence",
        "wrong_finding_and_new_evidence",
        "wrong_stored_finding_and_new_evidence",
    }:
        unrelated_evidence = EvidenceLink(
            dependency_id=scenario["dependency"].id,
            document_id=scenario["successor"].id,
            page_no=1,
            quote="FOC9-9 AT&T Telecom 900+00",
            verified=True,
        )
        session.add(unrelated_evidence)
        session.flush([unrelated_evidence])
        after["new_evidence_link_id"] = unrelated_evidence.id
    if identity_corruption not in {
        "fully_malformed_audit_json",
        "wrong_audit_action",
        "wrong_audit_entity_type",
        "wrong_audit_entity_id",
    }:
        receipt.after_json = after
    session.flush()

    worklist = build_reviewer_worklist(session, scenario["project"].id)
    ordinary_ids = tuple(
        candidate_id
        for item in worklist.ordinary
        for candidate_id in item.successor_candidate_ids
    )

    assert ordinary_ids.count(unrelated_candidate.id) == 1
    assert successor_candidate.id not in ordinary_ids
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        unrelated_candidate.id,
    ).id == unrelated_candidate.id
    assert ordinary_candidate_for_update(
        session,
        scenario["project"].id,
        successor_candidate.id,
    ) is None
    if reverse_lookup_candidate is not None:
        assert ordinary_candidate_for_update(
            session,
            reverse_lookup_candidate.project_id,
            reverse_lookup_candidate.id,
        ).id == reverse_lookup_candidate.id


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
        assert stale_evidence.id == old_evidence_id
        assert stale_designation.evidence_link_id == old_evidence_id

        current_evidence = EvidenceLink(
            dependency_id=dependency_id,
            document_id=successor_document_id,
            page_no=1,
            quote="FOC1-1 AT&T Telecom 100+00",
            verified=True,
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
        assert stale_evidence.id == old_evidence_id
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
                    DependencyEvidenceSufficiency.id,
                )
                .outerjoin(
                    DependencyEvidenceSufficiency,
                    and_(
                        DependencyEvidenceSufficiency.evidence_link_id
                        == EvidenceLink.id,
                        DependencyEvidenceSufficiency.scope_link_id.is_(None),
                    ),
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
                    DependencyEvidenceSufficiency.id,
                )
                .outerjoin(
                    DependencyEvidenceSufficiency,
                    and_(
                        DependencyEvidenceSufficiency.evidence_link_id
                        == EvidenceLink.id,
                        DependencyEvidenceSufficiency.scope_link_id.is_(None),
                    ),
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
    assert _has_direct_readiness(session, scenario["old_evidence"])
    assert _has_direct_readiness(session, other_evidence)


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

    assert not _has_direct_readiness(session, new_evidence)


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
