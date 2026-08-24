"""Public behavior for live and bounded Product Proving execution."""

from __future__ import annotations

from hashlib import sha256

import pytest
from sqlalchemy.orm import Session

from corridor.db import engine
from corridor.extraction_runs import (
    declare_active_run,
    declare_active_run_by_policy,
    record_extraction_run,
)
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    CandidateDisposition,
    Dependency,
    Document,
    ExternalParty,
    ExtractionRun,
    Milestone,
    MilestoneRegistration,
    Project,
    ReportRun,
    WorkDecision,
)
from corridor.product_proving_execution import (
    GitCheckoutObservation,
    capture_project_write_set,
    compare_extraction_runs,
    diff_project_write_sets,
    load_extraction_run_candidate_set,
    observe_product_proving_preflight,
    residual_candidate_ids_from_active_runs,
    run_bounded_product_proving_operations,
)
from corridor.principals import HumanPrincipal


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


def _project(session: Session, suffix: str) -> Project:
    project = Project(
        slug=f"proving-execution-{suffix}",
        name="Product Proving Execution",
        project_side_parties=["Project Engineer"],
    )
    session.add(project)
    session.flush([project])
    return project


def _document(
    session: Session,
    project: Project,
    *,
    doc_type: str = "minutes",
    name: str = "minutes.pdf",
) -> Document:
    document = Document(
        project_id=project.id,
        sha256=sha256(name.encode()).hexdigest(),
        filename=name,
        doc_type=doc_type,
        parse_status="parsed",
    )
    session.add(document)
    session.flush([document])
    return document


def _candidate(
    project: Project,
    document: Document,
    *,
    quote: str = "Equistar will provide the title package by January 2025.",
) -> Candidate:
    return Candidate(
        project_id=project.id,
        kind="event",
        source_document_id=document.id,
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "commitment",
                "external_org": "Equistar",
                "stated_party": "Equistar",
                "description": quote,
                "committed_date": {
                    "text": "January 2025",
                    "precision": "month",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                },
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                    "whole_row": False,
                }
            ],
            "confidence": 0.9,
        },
        source_pages=[1],
        confidence=0.9,
        prompt_version="minutes_v4",
        model="gpt-5.6-luna",
        citations_verified=True,
        state="pending",
    )


def _run(
    session: Session,
    project: Project,
    document: Document,
    *,
    quote: str = "Equistar will provide the title package by January 2025.",
) -> ExtractionRun:
    candidate = _candidate(project, document, quote=quote)
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes_v4",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="gpt-5.6-luna",
        schema_version="minutes_v4",
    )
    session.flush()
    return run


def _prompt_sources(_version, _root):
    return (("test-prompt", b"stable test prompt"),)


def test_preflight_is_constructed_from_live_project_and_registration_state(session):
    project = _project(session, "preflight")
    matrix = _document(
        session, project, doc_type="matrix", name="registered-matrix.pdf"
    )
    matrix_run = record_extraction_run(
        session,
        matrix,
        prompt_version="matrix_tiered_v3",
        candidate_count=0,
        page_errors=0,
        model="gpt-5.6-luna",
        schema_version="matrix_candidate_shape_v1",
    )
    session.flush()
    declare_active_run_by_policy(session, matrix.id, matrix_run.id)
    milestone = Milestone(
        project_id=project.id,
        code="DESIGN",
        name="Design Complete",
        source="legacy.csv",
    )
    session.add(milestone)
    session.flush([milestone])
    registration = MilestoneRegistration(
        milestone_id=milestone.id,
        source_name="legacy.csv",
        source_sha256=None,
        source_row_json={
            "code": "DESIGN",
            "name": "Design Complete",
            "need_date": None,
        },
        recorded_by="migration",
    )
    session.add(registration)
    session.flush([registration])
    milestone.current_registration_id = registration.id
    session.flush()

    observed = observe_product_proving_preflight(
        session,
        project_slug=project.slug,
        document_ids=(matrix.id,),
        baseline_fingerprint=lambda: "f" * 64,
        git_observer=lambda _root: GitCheckoutObservation(
            source_revision="a" * 40,
            origin_main_revision="b" * 40,
            clean_worktree=True,
        ),
    )

    assert observed.source_revision == "a" * 40
    assert observed.documents == {matrix.id: matrix.sha256}
    assert observed.baseline_runs == {matrix.id: matrix_run.id}
    assert set(observed.policy_digests) == {
        "dependency-admission",
        "event-admission",
    }
    assert all(len(value) == 64 for value in observed.policy_digests.values())
    assert len(observed.milestone_sources["DESIGN"]) == 64
    assert observed.milestone_sources["DESIGN"] != matrix.sha256
    assert observed.baseline_fingerprint == "f" * 64


def test_run_comparison_reads_immutable_inputs_and_exact_configuration(session):
    project = _project(session, "comparison")
    document = _document(session, project)
    baseline = _run(session, project, document)
    fresh = _run(session, project, document)

    loaded = load_extraction_run_candidate_set(
        session,
        baseline.id,
        prompt_source_resolver=_prompt_sources,
    )
    comparison = compare_extraction_runs(
        session,
        baseline.id,
        fresh.id,
        prompt_source_resolver=_prompt_sources,
    )

    assert loaded.candidates == tuple(baseline.candidate_inputs_json)
    assert loaded.configuration.prompt_sha256 == sha256(
        b"test-prompt\0stable test prompt\0"
    ).hexdigest()
    assert comparison.equal

    fresh.candidate_inputs_json[0]["model"] = "a different model"
    with pytest.raises(ValueError, match="model does not match"):
        load_extraction_run_candidate_set(
            session,
            fresh.id,
            prompt_source_resolver=_prompt_sources,
        )


def test_write_set_capture_derives_actual_rows_and_id_independent_digests(session):
    project = _project(session, "write-set")
    document = _document(session, project)
    run = _run(session, project, document)
    candidate = session.get(Candidate, run.candidate_inputs_json[0]["candidate_id"])
    party = ExternalParty(name="Equistar Product Proving")
    session.add(party)
    session.flush([party])
    dependencies = []
    for ref_code in ("DEP-PROVING-1", "DEP-PROVING-2"):
        dependency = Dependency(
            project_id=project.id,
            ref_code=ref_code,
            dep_type="utility_relocation",
            title=ref_code,
            external_org_id=party.id,
        )
        session.add(dependency)
        dependencies.append(dependency)
    session.flush(dependencies)
    for dependency in dependencies:
        session.add(
            WorkDecision(
                dependency_id=dependency.id,
                decision_type="set",
                field="internal_owner",
                before_value=None,
                after_value="Product Proving Owner",
                recorded_by="local:practitioner",
            )
        )
    session.flush()
    before = capture_project_write_set(session, project.slug)
    original_fingerprint = before.rows["candidates"][0]

    _run(session, project, document)
    candidate.state = "rejected"
    session.add(
        CandidateDisposition(
            candidate_id=candidate.id,
            disposition="not_relevant",
            reason="not_an_external_party_statement",
            recorded_by="local:practitioner",
        )
    )
    session.add(
        ReportRun(
            project_id=project.id,
            ruleset_version="report-v1",
            snapshot_json={},
            document_only=False,
        )
    )
    session.flush()
    after = capture_project_write_set(session, project.slug)
    write_set = diff_project_write_sets(before, after)

    assert len(write_set.created["candidate_dispositions"]) == 1
    assert len(write_set.created["report_runs"]) == 1
    assert len(write_set.updated["candidates"]) == 1
    candidate_rows = after.rows["candidates"]
    assert len(candidate_rows) == 2
    assert candidate_rows[0].content_sha256 != candidate_rows[1].content_sha256
    assert (
        original_fingerprint.stable_content_sha256
        != candidate_rows[1].stable_content_sha256
    )
    assert write_set.as_write_set()["report_runs"][0]["operation"] == "created"
    assert len(before.rows["external_orgs"]) == 1
    work_decision_rows = before.rows["work_decisions"]
    assert len(work_decision_rows) == 2
    assert (
        work_decision_rows[0].stable_content_sha256
        != work_decision_rows[1].stable_content_sha256
    ), "different Dependency relationships must not collapse after ids are remapped"


def test_bounded_operations_never_declare_or_admit_after_semantic_difference(session):
    project = _project(session, "bounded-failure")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    calls: list[tuple] = []

    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=lambda db, doc: _run(
            db,
            project,
            doc,
            quote="Equistar might provide a different package.",
        ),
        active_run_operation=lambda *values: calls.append(("declare", *values[1:])),
        admission_operation=lambda *values: calls.append(("admit", *values[1:])),
        prompt_source_resolver=_prompt_sources,
    )

    assert not result.extraction_equal
    assert result.admission_started is False
    assert result.active_run_document_ids == ()
    assert calls == []
    assert session.get(ActiveExtractionRun, document.id).extraction_run_id == baseline.id


def test_bounded_operations_declare_every_equal_run_before_admission(session):
    project = _project(session, "bounded-pass")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    calls: list[tuple] = []

    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=lambda db, doc: _run(db, project, doc),
        active_run_operation=lambda db, document_id, run_id: (
            declare_active_run(
                db,
                document_id,
                run_id,
                principal=HumanPrincipal("local:product-proving-test"),
            ),
            calls.append(("declare", document_id, run_id)),
        )[0],
        admission_operation=lambda _db, project_id: calls.append(
            ("admit", project_id)
        )
        or "admitted",
        prompt_source_resolver=_prompt_sources,
    )

    assert result.extraction_equal
    assert result.admission_started is True
    assert result.admission_result == "admitted"
    assert calls[0][0] == "declare"
    assert calls[1] == ("admit", project.id)


def test_bounded_operations_roll_back_an_extractor_that_changes_active_state(session):
    project = _project(session, "extractor-boundary")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)

    def invalid_extraction(db, doc):
        fresh = _run(db, project, doc)
        declare_active_run(
            db,
            doc.id,
            fresh.id,
            principal=HumanPrincipal("local:invalid-extractor"),
        )
        return fresh

    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=invalid_extraction,
        active_run_operation=lambda *_args: pytest.fail("must not declare"),
        admission_operation=lambda *_args: pytest.fail("must not admit"),
        prompt_source_resolver=_prompt_sources,
    )

    assert result.admission_started is False
    assert "changed an Active Run" in result.extraction_failures[0]
    assert session.get(ActiveExtractionRun, document.id).extraction_run_id == baseline.id


def test_residual_candidates_are_read_only_from_exact_fresh_active_runs(session):
    project = _project(session, "residual")
    document = _document(session, project)
    baseline = _run(session, project, document)
    fresh = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    declare_active_run(
        session,
        document.id,
        fresh.id,
        principal=HumanPrincipal("local:product-proving-residual"),
    )
    session.flush()
    fresh_candidate_id = fresh.candidate_inputs_json[0]["candidate_id"]

    assert residual_candidate_ids_from_active_runs(
        session,
        project_slug=project.slug,
        active_runs={document.id: fresh.id},
    ) == (fresh_candidate_id,)

    with pytest.raises(ValueError, match="exact completed fresh Active Run"):
        residual_candidate_ids_from_active_runs(
            session,
            project_slug=project.slug,
            active_runs={document.id: baseline.id},
        )
