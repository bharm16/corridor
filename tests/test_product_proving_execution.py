"""Public behavior for live and bounded Product Proving execution."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.db import engine
from corridor.extraction_runs import (
    declare_active_run,
    declare_active_run_by_policy,
    record_extraction_run,
)
from corridor.extractor_lineage import injected_extractor_config, zero_token_usage
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    CandidateDisposition,
    Dependency,
    DocPage,
    Document,
    EvidenceInvestigationCandidateReviewStart,
    ExternalParty,
    ExtractionRun,
    Milestone,
    MilestoneRegistration,
    PolicyRun,
    Project,
    ReportRun,
    WorkDecision,
)
from corridor.product_proving_execution import (
    GitCheckoutObservation,
    capture_live_product_proving_operations,
    capture_project_write_set,
    compare_extraction_runs,
    diff_project_write_sets,
    load_extraction_run_candidate_set,
    observe_product_proving_preflight,
    proving_database_identity,
    residual_candidate_ids_from_operations,
    run_bounded_product_proving_operations,
)
from corridor.product_proving_database import (
    DatabaseFingerprint,
    VerifiedProductProvingDatabaseBaseline,
    observe_database_connection_identity,
)
from corridor.principals import HumanPrincipal
from corridor.product_proving_run import ExpectedPreflight


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


def _config(
    *,
    prompt_version: str,
    schema_version: str,
    model: str | None = "gpt-5.6-luna",
    extractor: str = "test-extractor",
):
    return injected_extractor_config(
        extractor=extractor,
        prompt_version=prompt_version,
        model=model,
        schema_version=schema_version,
        prompt_bytes=f"stable {extractor} prompt".encode(),
        schema={"type": "object", "additionalProperties": False},
        postprocessor_bytes=f"stable {extractor} postprocessor".encode(),
        request_controls={"strict": True},
    )


def _run(
    session: Session,
    project: Project,
    document: Document,
    *,
    quote: str = "Equistar will provide the title package by January 2025.",
) -> ExtractionRun:
    candidate = _candidate(project, document, quote=quote)
    config = _config(
        extractor="test-minutes",
        prompt_version="minutes_v4",
        schema_version="minutes_v4",
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes_v4",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="gpt-5.6-luna",
        schema_version="minutes_v4",
        extractor_config=config,
        token_usage=zero_token_usage(document.id),
    )
    session.flush()
    return run


def _test_commit(db: Session) -> None:
    """Exercise the commit boundary without escaping the rollback fixture."""

    db.flush()


def _verified_baseline(
    session: Session,
    *,
    revision: str,
    migration_head: str,
    state_sha256: str = "f" * 64,
) -> VerifiedProductProvingDatabaseBaseline:
    database_url = _database_url(session)
    source_identity = proving_database_identity(database_url)
    fingerprint = DatabaseFingerprint(
        tables=(),
        sequences=(),
        state_sha256=state_sha256,
    )
    return VerifiedProductProvingDatabaseBaseline(
        bundle_dir=Path("/verified-test-baseline"),
        manifest_sha256="d" * 64,
        dump_sha256="e" * 64,
        baseline={
            "checkout": {
                "revision": revision,
                "migration_head": migration_head,
            },
            "source_database": source_identity,
            "source_connection": observe_database_connection_identity(
                database_url
            ).as_dict(),
        },
        fingerprint=fingerprint,
    )


def _database_url(session: Session) -> str:
    bound = session.get_bind()
    bound_engine = getattr(bound, "engine", bound)
    return bound_engine.url.render_as_string(hide_password=False)


def test_preflight_is_constructed_from_live_project_and_registration_state(session):
    project = _project(session, "preflight")
    matrix = _document(
        session, project, doc_type="matrix", name="registered-matrix.pdf"
    )
    matrix_config = _config(
        extractor="test-matrix",
        prompt_version="matrix_tiered_v3",
        schema_version="matrix_candidate_shape_v1",
    )
    matrix_run = record_extraction_run(
        session,
        matrix,
        prompt_version="matrix_tiered_v3",
        candidate_count=0,
        page_errors=0,
        model="gpt-5.6-luna",
        schema_version="matrix_candidate_shape_v1",
        extractor_config=matrix_config,
        token_usage=zero_token_usage(matrix.id),
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

    migration_head = str(session.scalar(text("select version_num from alembic_version")))
    verified_baseline = _verified_baseline(
        session,
        revision="a" * 40,
        migration_head=migration_head,
    )
    observed = observe_product_proving_preflight(
        session,
        project_slug=project.slug,
        document_ids=(matrix.id,),
        verified_baseline=verified_baseline,
        database_url=_database_url(session),
        git_observer=lambda _root: GitCheckoutObservation(
            source_revision="a" * 40,
            origin_main_revision="b" * 40,
            clean_worktree=True,
        ),
        _fingerprint_for_test=lambda _url: verified_baseline.fingerprint,
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
    )
    comparison = compare_extraction_runs(
        session,
        baseline.id,
        fresh.id,
    )

    assert loaded.candidates == tuple(baseline.candidate_inputs_json)
    assert loaded.configuration.prompt_sha256 == baseline.prompt_sha256
    assert loaded.configuration.schema_sha256 == baseline.schema_sha256
    assert loaded.configuration.postprocessor_sha256 == baseline.postprocessor_sha256
    assert loaded.configuration.config_sha256 == baseline.extractor_config_sha256
    assert comparison.equal

    fresh.candidate_inputs_json[0]["model"] = "a different model"
    with pytest.raises(ValueError, match="model does not match"):
        load_extraction_run_candidate_set(
            session,
            fresh.id,
        )


def test_live_capture_builds_observed_comparisons_and_write_sets_without_json(session):
    project = _project(session, "live-capture")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    git = GitCheckoutObservation(
        source_revision="a" * 40,
        origin_main_revision="b" * 40,
        clean_worktree=True,
    )
    migration_head = str(session.scalar(text("select version_num from alembic_version")))
    verified_baseline = _verified_baseline(
        session,
        revision=git.source_revision,
        migration_head=migration_head,
    )
    observed = observe_product_proving_preflight(
        session,
        project_slug=project.slug,
        document_ids=(document.id,),
        verified_baseline=verified_baseline,
        database_url=_database_url(session),
        git_observer=lambda _root: git,
        _fingerprint_for_test=lambda _url: verified_baseline.fingerprint,
    )
    expected = ExpectedPreflight(
        source_revision=observed.source_revision,
        origin_main_revision=observed.origin_main_revision,
        migration_head=observed.migration_head,
        policy_digests=observed.policy_digests,
        documents=observed.documents,
        baseline_runs=observed.baseline_runs,
        milestone_sources=observed.milestone_sources,
        baseline_fingerprint=observed.baseline_fingerprint,
    )

    capture = capture_live_product_proving_operations(
        session,
        project_slug=project.slug,
        expected=expected,
        verified_baseline=verified_baseline,
        database_url=_database_url(session),
        pass_number=1,
        extraction_operation=lambda db, doc: _run(db, project, doc),
        active_run_operation=lambda db, document_id, run_id: declare_active_run(
            db,
            document_id,
            run_id,
            principal=HumanPrincipal("local:live-capture"),
        ),
        admission_operation=lambda _db, _project_id: "admitted",
        git_observer=lambda _root: git,
        _commit_for_test=_test_commit,
        _fingerprint_for_test=lambda _url: verified_baseline.fingerprint,
    )

    assert capture.expected == expected
    assert capture.observed == observed
    assert capture.operations.extraction_equal
    assert capture.operations.write_set.created["extraction_runs"]
    assert capture.operations.residual_candidate_ids
    assert capture.pass_number == 1
    assert capture.prior_restore_operation_id is None


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
        EvidenceInvestigationCandidateReviewStart(
            project_id=project.id,
            candidate_id=candidate.id,
            principal="local:practitioner",
            observed_at=datetime.now(timezone.utc),
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
    assert (
        len(write_set.created["evidence_investigation_candidate_review_starts"])
        == 1
    )
    assert len(write_set.created["report_runs"]) == 1
    assert len(write_set.updated["candidates"]) == 1
    candidate_rows = after.rows["candidates"]
    assert len(candidate_rows) == 2
    assert candidate_rows[0].content_sha256 != candidate_rows[1].content_sha256
    assert (
        original_fingerprint.stable_content_sha256
        == candidate_rows[1].stable_content_sha256
    )
    assert write_set.as_write_set()["report_runs"][0]["operation"] == "created"
    assert len(before.rows["external_orgs"]) == 1
    work_decision_rows = before.rows["work_decisions"]
    assert len(work_decision_rows) == 2
    assert (
        work_decision_rows[0].stable_content_sha256
        != work_decision_rows[1].stable_content_sha256
    ), "different Dependency relationships must not collapse after ids are remapped"
    assert {
        "evidence_investigation_runs",
        "evidence_investigation_step_receipts",
        "evidence_investigation_packet_receipts",
        "evidence_investigation_shadow_cases",
        "evidence_investigation_shadow_executions",
        "evidence_investigation_review_observations",
        "evidence_investigation_candidate_review_starts",
        "evidence_investigation_shadow_outcomes",
        "evidence_investigation_evaluation_receipts",
    }.issubset(after.rows)


def test_candidate_confidence_does_not_change_stable_receipt_relationships(session):
    project = _project(session, "confidence-stability")
    document = _document(session, project)
    first = _candidate(project, document)
    second = _candidate(project, document)
    first.confidence = 0.51
    first.payload_json = {**first.payload_json, "confidence": 0.51}
    second.confidence = 0.99
    second.payload_json = {**second.payload_json, "confidence": 0.99}
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes_v4",
        candidate_count=2,
        page_errors=0,
        candidates=(first, second),
        model="gpt-5.6-luna",
        schema_version="minutes_v4",
        extractor_config=_config(
            prompt_version="minutes_v4", schema_version="minutes_v4"
        ),
        token_usage=zero_token_usage(document.id),
    )
    assert run.candidate_count == 2
    for candidate in (first, second):
        session.add_all(
            (
                CandidateDisposition(
                    candidate_id=candidate.id,
                    disposition="not_relevant",
                    reason="duplicate_statement",
                    recorded_by="local:practitioner",
                ),
                EvidenceInvestigationCandidateReviewStart(
                    project_id=project.id,
                    candidate_id=candidate.id,
                    principal="local:practitioner",
                    observed_at=datetime.now(timezone.utc),
                ),
            )
        )
    session.flush()

    snapshot = capture_project_write_set(session, project.slug)
    candidate_rows = snapshot.rows["candidates"]
    assert len(candidate_rows) == 2
    assert candidate_rows[0].content_sha256 != candidate_rows[1].content_sha256
    assert (
        candidate_rows[0].stable_content_sha256
        == candidate_rows[1].stable_content_sha256
    )
    disposition_rows = snapshot.rows["candidate_dispositions"]
    assert (
        disposition_rows[0].stable_content_sha256
        == disposition_rows[1].stable_content_sha256
    )
    review_rows = snapshot.rows["evidence_investigation_candidate_review_starts"]
    assert review_rows[0].stable_content_sha256 == review_rows[1].stable_content_sha256


def test_bounded_operations_never_declare_or_admit_after_semantic_difference(session):
    project = _project(session, "bounded-failure")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    calls: list[tuple] = []
    commits: list[str] = []

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
        _commit_for_test=lambda db: (commits.append("commit"), db.flush())[1],
    )

    assert not result.extraction_equal
    assert result.admission_started is False
    assert result.active_run_document_ids == ()
    assert calls == []
    assert commits == ["commit"]
    assert result.observed_active_runs == {document.id: baseline.id}
    assert len(result.write_set.created["extraction_runs"]) == 1
    assert session.get(ActiveExtractionRun, document.id).extraction_run_id == baseline.id


def test_bounded_operations_preserve_a_terminal_failed_extraction_receipt(session):
    project = _project(session, "terminal-extraction-failure")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    commits: list[str] = []

    def failed_extraction(db, doc):
        config = _config(
            extractor="test-minutes",
            prompt_version="minutes_v4",
            schema_version="minutes_v4",
        )
        return record_extraction_run(
            db,
            doc,
            prompt_version="minutes_v4",
            candidate_count=0,
            page_errors=1,
            outcome="failed",
            model="gpt-5.6-luna",
            schema_version="minutes_v4",
            error_detail="model request failed",
            extractor_config=config,
            token_usage=zero_token_usage(doc.id),
        )

    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=failed_extraction,
        active_run_operation=lambda *_args: pytest.fail("must not declare"),
        admission_operation=lambda *_args: pytest.fail("must not admit"),
        _commit_for_test=lambda db: (commits.append("commit"), db.flush())[1],
    )

    [failed_run_id] = result.fresh_run_ids.values()
    failed_run = session.get(ExtractionRun, failed_run_id)
    assert commits == ["commit"]
    assert result.admission_started is False
    assert "not complete without errors" in result.extraction_failures[0]
    assert failed_run.outcome == "failed"
    assert failed_run.error_detail == "model request failed"


def test_bounded_operations_declare_every_equal_run_before_admission(session):
    project = _project(session, "bounded-pass")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    calls: list[tuple] = []
    commits: list[str] = []

    def admit(db, project_id):
        calls.append(("admit", project_id))
        db.add(
            PolicyRun(
                project_id=project_id,
                family="event-admission",
                policy_approval_id=None,
                policy_version="test-event-admission",
                policy_sha256="a" * 64,
                abstention_reason_version="test-reasons",
                applied_count=0,
                abstained_count=0,
            )
        )
        db.flush()
        return "admitted"

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
        admission_operation=admit,
        _commit_for_test=lambda db: (commits.append("commit"), db.flush())[1],
    )

    assert result.extraction_equal
    assert result.admission_started is True
    assert result.admission_result == "admitted"
    assert calls[0][0] == "declare"
    assert calls[1] == ("admit", project.id)
    assert commits == ["commit"]
    assert result.observed_active_runs == result.fresh_run_ids
    assert len(result.admission_policy_receipts) == 1
    assert result.admission_policy_receipts[0].policy_sha256 == "a" * 64
    fresh_run_id = next(iter(result.fresh_run_ids.values()))
    assert result.residual_candidate_ids == tuple(
        session.scalars(
            select(Candidate.id).where(Candidate.extraction_run_id == fresh_run_id)
        ).all()
    )


def test_bounded_operations_trace_and_commit_real_dependency_admission(session):
    project = _project(session, "real-admission")
    document = _document(
        session, project, doc_type="matrix", name="real-admission-matrix.pdf"
    )
    fields = {
        "utility_id": "PL1",
        "external_org": "Tejas Pipeline Co",
        "utility_type": "Petroleum and Gaseous Materials",
        "baseline": "SH99",
        "station_from": "1102+20",
        "station_to": "1102+20",
    }
    quote = " | ".join(fields.values())
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))

    def matrix_run(db, doc):
        candidate = Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": fields,
                "citations": [
                    {
                        "document_id": doc.id,
                        "page": 1,
                        "quote": quote,
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "dedupe_hint": quote,
                "text_source": "text_layer",
            },
            source_document_id=doc.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version="matrix_tiered_v3",
            model="gpt-5.6-luna",
            citations_verified=True,
            state="pending",
        )
        config = _config(
            extractor="test-matrix",
            prompt_version="matrix_tiered_v3",
            schema_version="matrix_candidate_shape_v1",
        )
        return record_extraction_run(
            db,
            doc,
            prompt_version="matrix_tiered_v3",
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model="gpt-5.6-luna",
            schema_version="matrix_candidate_shape_v1",
            extractor_config=config,
            token_usage=zero_token_usage(doc.id),
        )

    baseline = matrix_run(session, document)
    session.flush()
    declare_active_run_by_policy(session, document.id, baseline.id)
    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=matrix_run,
        active_run_operation=lambda db, document_id, run_id: declare_active_run(
            db,
            document_id,
            run_id,
            principal=HumanPrincipal("local:real-admission"),
        ),
        _commit_for_test=_test_commit,
    )

    assert result.admission_started
    assert {receipt.family for receipt in result.admission_policy_receipts} == {
        "dependency-admission",
        "event-admission",
    }
    admitted = session.scalar(
        select(Dependency).where(Dependency.project_id == project.id)
    )
    assert admitted is not None
    assert admitted.source_ref == "PL1"


def test_bounded_operations_admit_only_fresh_candidates_not_other_active_work(
    session,
):
    project = _project(session, "fresh-admission-scope")

    def matrix_run(db, document, utility_id):
        fields = {
            "utility_id": utility_id,
            "external_org": "Tejas Pipeline Co",
            "utility_type": "Petroleum and Gaseous Materials",
            "baseline": "SH99",
            "station_from": "1102+20",
            "station_to": "1102+20",
        }
        quote = " | ".join(fields.values())
        page = db.scalar(
            select(DocPage).where(
                DocPage.document_id == document.id,
                DocPage.page_no == 1,
            )
        )
        if page is None:
            db.add(DocPage(document_id=document.id, page_no=1, text=quote))
        else:
            page.text = quote
        candidate = Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": fields,
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": quote,
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "dedupe_hint": quote,
                "text_source": "text_layer",
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version="matrix_tiered_v3",
            model="gpt-5.6-luna",
            citations_verified=True,
            state="pending",
        )
        config = _config(
            extractor="test-matrix",
            prompt_version="matrix_tiered_v3",
            schema_version="matrix_candidate_shape_v1",
        )
        run = record_extraction_run(
            db,
            document,
            prompt_version="matrix_tiered_v3",
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model="gpt-5.6-luna",
            schema_version="matrix_candidate_shape_v1",
            extractor_config=config,
            token_usage=zero_token_usage(document.id),
        )
        return run, candidate

    bounded = _document(
        session,
        project,
        doc_type="matrix",
        name="bounded-matrix.pdf",
    )
    baseline, _ = matrix_run(session, bounded, "PL1")
    declare_active_run_by_policy(session, bounded.id, baseline.id)

    other = _document(
        session,
        project,
        doc_type="matrix",
        name="other-active-matrix.pdf",
    )
    other_run, out_of_packet = matrix_run(session, other, "PL9")
    declare_active_run_by_policy(session, other.id, other_run.id)

    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={bounded.id: baseline.id},
        extraction_operation=lambda db, document: matrix_run(
            db, document, "PL1"
        )[0],
        active_run_operation=lambda db, document_id, run_id: declare_active_run(
            db,
            document_id,
            run_id,
            principal=HumanPrincipal("local:fresh-admission-scope"),
        ),
        _commit_for_test=_test_commit,
    )

    assert result.admission_started is True
    assert result.extraction_failures == ()
    assert {
        dependency.source_ref
        for dependency in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    } == {"PL1"}
    session.refresh(out_of_packet)
    assert out_of_packet.state == "pending"


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
        _commit_for_test=_test_commit,
    )

    assert result.admission_started is False
    assert "changed an Active Run" in result.extraction_failures[0]
    assert session.get(ActiveExtractionRun, document.id).extraction_run_id == baseline.id


def test_bounded_operations_reject_and_roll_back_out_of_packet_project_writes(session):
    project = _project(session, "write-allowlist")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)

    def widening_extraction(db, doc):
        fresh = _run(db, project, doc)
        db.add(
            ReportRun(
                project_id=project.id,
                ruleset_version="not-an-extraction-write",
                snapshot_json={},
                document_only=False,
            )
        )
        return fresh

    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=widening_extraction,
        active_run_operation=lambda *_args: pytest.fail("must not declare"),
        admission_operation=lambda *_args: pytest.fail("must not admit"),
        _commit_for_test=_test_commit,
    )

    assert result.admission_started is False
    assert "out-of-packet tables: report_runs" in result.extraction_failures[0]
    assert session.scalar(
        select(ReportRun.id).where(ReportRun.project_id == project.id)
    ) is None


def test_bounded_operations_expire_rolled_back_declarations_before_failure_capture(
    session, monkeypatch
):
    project = _project(session, "admission-rollback-state")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    expire_calls: list[str] = []
    real_expire_all = session.expire_all

    def observe_expiration():
        expire_calls.append("expire")
        real_expire_all()

    monkeypatch.setattr(session, "expire_all", observe_expiration)

    def invalid_admission(db, project_id):
        db.add(
            ReportRun(
                project_id=project_id,
                ruleset_version="invalid-admission-write",
                snapshot_json={},
                document_only=False,
            )
        )
        return "invalid"

    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=lambda db, doc: _run(db, project, doc),
        active_run_operation=lambda db, document_id, run_id: declare_active_run(
            db,
            document_id,
            run_id,
            principal=HumanPrincipal("local:rollback-state"),
        ),
        admission_operation=invalid_admission,
        _commit_for_test=_test_commit,
    )

    assert result.admission_started is False
    assert result.active_run_document_ids == ()
    assert "out-of-packet tables: report_runs" in result.extraction_failures[0]
    assert result.observed_active_runs == {document.id: baseline.id}
    # Once immediately after the failed savepoint, then again after the
    # durable failure receipt boundary.
    assert expire_calls == ["expire", "expire"]
    assert session.get(
        ActiveExtractionRun, document.id, populate_existing=True
    ).extraction_run_id == baseline.id
    assert session.scalar(
        select(ReportRun.id).where(ReportRun.project_id == project.id)
    ) is None


def test_residual_candidates_refuse_a_caller_narrowed_operations_packet(session):
    project = _project(session, "residual")
    document = _document(session, project)
    baseline = _run(session, project, document)
    declare_active_run_by_policy(session, document.id, baseline.id)
    result = run_bounded_product_proving_operations(
        session,
        project_slug=project.slug,
        baseline_runs={document.id: baseline.id},
        extraction_operation=lambda db, doc: _run(db, project, doc),
        active_run_operation=lambda db, document_id, run_id: declare_active_run(
            db,
            document_id,
            run_id,
            principal=HumanPrincipal("local:product-proving-residual"),
        ),
        admission_operation=lambda _db, _project_id: "admitted",
        _commit_for_test=_test_commit,
    )
    assert residual_candidate_ids_from_operations(session, result)

    with pytest.raises(ValueError, match="complete compared Active Run set"):
        residual_candidate_ids_from_operations(
            session, replace(result, active_run_document_ids=())
        )
