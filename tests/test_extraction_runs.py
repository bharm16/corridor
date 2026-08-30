"""Extraction run lineage and Active Run declaration contracts."""

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.extraction_runs import record_extraction_run
from corridor.extractor_lineage import injected_extractor_config
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.row_accounting import RowAccounting, RowAccountingFailure
from corridor.models import (
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Candidate,
    Document,
    ExtractionRun,
    Project,
)
from corridor.record_inclusion import record_inclusion_pending

extraction_runs = __import__("corridor.extraction_runs", fromlist=["*"])

PROMPT_VERSION = "test_v1"
DECLARER = HumanPrincipal("local:run-lineage-declarer")
RELIEF_DECLARER = HumanPrincipal("local:run-lineage-relief")


def _row_accounting(prompt_version: str, *, unaccounted: bool = False):
    accounting = RowAccounting(
        reader_version=prompt_version,
        reader_path=(
            "spreadsheet_cells"
            if prompt_version.startswith("sheet_")
            else "page_geometry_and_transcription"
        ),
    )
    accounting.detect("row:1", page=1, row_number=1)
    if unaccounted:
        with pytest.raises(RowAccountingFailure) as failure:
            accounting.finish([])
        return failure.value.receipt
    accounting.account(
        "row:1",
        disposition="blank",
        reason="blank_source_row",
    )
    return accounting.finish([]).row_accounting


def _run_id(run):
    return run.id if hasattr(run, "id") else run


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
    p = Project(slug="run-lineage-test", name="Run Lineage Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def add_matrix(session, project, name, sha):
    doc = Document(
        project_id=project.id,
        sha256=sha,
        filename=name,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    return doc


def test_completed_current_matrix_reader_requires_row_accounting(session, project):
    document = add_matrix(session, project, "accounted.xlsx", "r" * 64)

    with pytest.raises(ValueError, match="requires row accounting"):
        record_extraction_run(
            session,
            document,
            prompt_version="sheet_native_v2",
            candidate_count=0,
            page_errors=0,
            allow_unsealed_legacy=True,
        )

    run = record_extraction_run(
        session,
        document,
        prompt_version="sheet_native_v2",
        candidate_count=0,
        page_errors=0,
        row_accounting_json=_row_accounting("sheet_native_v2"),
        allow_unsealed_legacy=True,
    )
    assert run.row_accounting_json["blank_row_count"] == 1


def test_failed_current_matrix_reader_retains_unaccounted_rows(session, project):
    document = add_matrix(session, project, "dropped.pdf", "s" * 64)

    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix_tiered_v4",
        candidate_count=0,
        page_errors=1,
        outcome="failed",
        error_detail="one detected row was unaccounted",
        row_accounting_json=_row_accounting(
            "matrix_tiered_v4",
            unaccounted=True,
        ),
        allow_unsealed_legacy=True,
    )

    assert run.outcome == "failed"
    assert run.row_accounting_json["unaccounted_rows"] == ["row:1"]


def test_active_run_helpers_are_explicit_contracts():
    assert hasattr(
        extraction_runs, "declare_active_run"
    ), "Active Runs must be declared, not inferred"
    assert callable(getattr(extraction_runs, "declare_active_run"))
    assert hasattr(extraction_runs, "active_run_for_document")
    assert callable(getattr(extraction_runs, "active_run_for_document"))


def test_active_run_declared_by_explicit_run_id(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    first = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.v2",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    session.flush()

    extraction_runs.declare_active_run(
        session, doc.id, first.id, principal=DECLARER
    )
    assert _run_id(extraction_runs.active_run_for_document(session, doc.id)) == first.id

    # A newer successful run is not automatically active.
    assert _run_id(extraction_runs.active_run_for_document(session, doc.id)) == first.id


def test_newer_runs_do_not_imply_active_run(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    active = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.v1",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    session.flush()
    extraction_runs.declare_active_run(
        session, doc.id, active.id, principal=DECLARER
    )

    _ = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.v2-experimental",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    _ = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.v2-backfill",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    session.flush()

    assert _run_id(extraction_runs.active_run_for_document(session, doc.id)) == active.id


def test_a_projection_that_diverged_from_its_history_refuses_to_extend(
    session, project
):
    """A tampered projection is corruption, not staleness.

    This scenario used to be a stale-identity-map concern the service
    papered over. With the declaration chain as the authority, a projection
    row that disagrees with the chain tail can only be a write that went
    around the service — extending the chain on top of it would launder the
    tampering into history.
    """
    doc = add_matrix(session, project, "prewarmed.pdf", "b" * 64)
    first = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.first",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    second = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.second",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    extraction_runs.declare_active_run(
        session, doc.id, first.id, principal=DECLARER
    )
    session.execute(
        update(ActiveExtractionRun)
        .where(ActiveExtractionRun.document_id == doc.id)
        .values(extraction_run_id=second.id)
        .execution_options(synchronize_session=False)
    )

    with pytest.raises(ValueError, match="diverged"):
        extraction_runs.declare_active_run(
            session, doc.id, first.id, principal=DECLARER
        )


def test_run_receipt_carries_provenance_and_owns_its_candidates(session, project):
    doc = add_matrix(session, project, "a.pdf", "c" * 64)
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
        source_document_id=doc.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model="test-model",
        citations_verified=True,
    )
    session.add(candidate)

    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
        schema_version="dependency-schema-v3",
        allow_unsealed_legacy=True,
    )
    session.flush()

    assert candidate.extraction_run_id == run.id
    assert (run.prompt_version, run.model, run.schema_version, run.outcome) == (
        PROMPT_VERSION,
        "test-model",
        "dependency-schema-v3",
        "completed",
    )
    assert run.candidate_inputs_json == [
        {
            "candidate_id": candidate.id,
            "project_id": project.id,
            "kind": "dependency",
            "source_document_id": doc.id,
            "payload_json": {
                "kind": "dependency",
                "fields": {"utility_id": "E92"},
            },
            "source_pages": [1],
            "confidence": 1.0,
            "prompt_version": PROMPT_VERSION,
            "model": "test-model",
            "citations_verified": True,
            "state": "pending",
        }
    ]


def test_run_receipt_persists_extractor_time_configuration_and_usage(
    session, project
):
    doc = add_matrix(session, project, "sealed.pdf", "0" * 64)
    config = injected_extractor_config(
        extractor="fixture",
        prompt_version=PROMPT_VERSION,
        model="test-model",
        schema_version="dependency-schema-v3",
        prompt_bytes=b"exact prompt\n",
        schema={"type": "object"},
        postprocessor_bytes=b"exact rules\n",
        request_controls={"strict": True, "store": False},
    )
    usage = {
        "scope": "run",
        "document_ids": [doc.id],
        "measurement": "exact",
        "prompt_tokens": 41,
        "completion_tokens": 7,
        "reasoning_tokens": 0,
        "cached_tokens": 12,
    }

    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        model="test-model",
        schema_version="dependency-schema-v3",
        extractor_config=config,
        token_usage=usage,
    )
    session.flush()

    assert run.prompt_sha256 == config.prompt_sha256
    assert run.schema_sha256 == config.schema_sha256
    assert run.postprocessor_sha256 == config.postprocessor_sha256
    assert run.extractor_config_json == config.config_json
    assert run.extractor_config_sha256 == config.config_sha256
    assert run.token_usage_json == usage


def test_run_receipt_rejects_partial_or_mismatched_configuration(session, project):
    doc = add_matrix(session, project, "mismatch.pdf", "f" * 64)
    config = injected_extractor_config(
        extractor="fixture",
        prompt_version=PROMPT_VERSION,
        model="test-model",
        schema_version="dependency-schema-v3",
        prompt_bytes=b"exact prompt\n",
        schema={"type": "object"},
        postprocessor_bytes=b"exact rules\n",
        request_controls={"strict": True},
    )
    usage = {
        "scope": "run",
        "document_ids": [doc.id],
        "measurement": "exact",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
    }

    with pytest.raises(ValueError, match="recorded together"):
        record_extraction_run(
            session,
            doc,
            prompt_version=PROMPT_VERSION,
            candidate_count=0,
            page_errors=0,
            model="test-model",
            schema_version="dependency-schema-v3",
            extractor_config=config,
        )

    with pytest.raises(ValueError, match="model does not match"):
        record_extraction_run(
            session,
            doc,
            prompt_version=PROMPT_VERSION,
            candidate_count=0,
            page_errors=0,
            model="different-model",
            schema_version="dependency-schema-v3",
            extractor_config=config,
            token_usage=usage,
        )

    invalid_membership = {
        **usage,
        "scope": "batch",
        "document_ids": [doc.id, -1],
    }
    with pytest.raises(ValueError, match="positive integers"):
        record_extraction_run(
            session,
            doc,
            prompt_version=PROMPT_VERSION,
            candidate_count=0,
            page_errors=0,
            model="test-model",
            schema_version="dependency-schema-v3",
            extractor_config=config,
            token_usage=invalid_membership,
        )


def test_new_run_refuses_unsealed_configuration_by_default(session, project):
    doc = add_matrix(session, project, "unsealed.pdf", "e" * 64)

    with pytest.raises(ValueError, match="exact extractor configuration"):
        record_extraction_run(
            session,
            doc,
            prompt_version=PROMPT_VERSION,
            candidate_count=0,
            page_errors=0,
        )

    historical_fixture = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    assert historical_fixture.extractor_config_json is None


def test_run_input_snapshot_precedes_later_candidate_edits(session, project):
    doc = add_matrix(session, project, "snapshot.pdf", "9" * 64)
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "ORIGINAL"}},
        source_document_id=doc.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model="test-model",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
        schema_version="dependency-schema-v3",
        allow_unsealed_legacy=True,
    )

    candidate.payload_json = {
        "kind": "dependency",
        "fields": {"utility_id": "EDITED"},
    }
    candidate.state = "rejected"
    session.flush()

    assert run.candidate_inputs_json[0]["payload_json"]["fields"] == {
        "utility_id": "ORIGINAL"
    }
    assert run.candidate_inputs_json[0]["state"] == "pending"


def test_run_receipt_rejects_missing_or_already_owned_candidate_inputs(
    session, project
):
    doc = add_matrix(session, project, "ownership.pdf", "8" * 64)
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
        source_document_id=doc.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model="test-model",
        citations_verified=True,
    )

    with pytest.raises(ValueError, match="candidate_count"):
        record_extraction_run(
            session,
            doc,
            prompt_version=PROMPT_VERSION,
            candidate_count=1,
            page_errors=0,
            model="test-model",
            allow_unsealed_legacy=True,
        )

    first = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
        allow_unsealed_legacy=True,
    )
    session.flush()
    assert candidate.extraction_run_id == first.id

    with pytest.raises(ValueError, match="already belongs"):
        record_extraction_run(
            session,
            doc,
            prompt_version=PROMPT_VERSION,
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model="test-model",
            allow_unsealed_legacy=True,
        )


def test_run_receipt_rejects_cross_project_or_duplicate_candidates(session, project):
    doc = add_matrix(session, project, "input-integrity.pdf", "1" * 64)
    other = Project(
        slug="other-run-lineage-project",
        name="Other Run Lineage Project",
        is_synthetic=True,
    )
    session.add(other)
    session.flush()
    candidate = Candidate(
        project_id=other.id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
        source_document_id=doc.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model="test-model",
        citations_verified=True,
    )

    with pytest.raises(ValueError, match="another project's"):
        record_extraction_run(
            session,
            doc,
            prompt_version=PROMPT_VERSION,
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model="test-model",
            allow_unsealed_legacy=True,
        )

    candidate.project_id = project.id
    with pytest.raises(ValueError, match="repeat a Candidate"):
        record_extraction_run(
            session,
            doc,
            prompt_version=PROMPT_VERSION,
            candidate_count=2,
            page_errors=0,
            candidates=(candidate, candidate),
            model="test-model",
            allow_unsealed_legacy=True,
        )


def test_real_run_receipts_reject_delete_and_truncate(session):
    project = Project(
        slug="real-run-lineage-test",
        name="Real Run Lineage Test",
        is_synthetic=False,
    )
    session.add(project)
    session.flush()
    doc = add_matrix(session, project, "durable.pdf", "7" * 64)
    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )

    with pytest.raises(IntegrityError, match="receipts are immutable"):
        with session.begin_nested():
            session.execute(delete(ExtractionRun).where(ExtractionRun.id == run.id))

    with pytest.raises(IntegrityError, match="receipts are immutable"):
        with session.begin_nested():
            session.execute(text("truncate table extraction_runs cascade"))

    assert session.get(ExtractionRun, run.id) is not None


def test_attached_candidate_lineage_rejects_mutation_delete_and_truncate(session):
    project = Project(
        slug="real-candidate-lineage-test",
        name="Real Candidate Lineage Test",
        is_synthetic=False,
    )
    other = Project(
        slug="other-real-candidate-lineage-test",
        name="Other Real Candidate Lineage Test",
        is_synthetic=False,
    )
    session.add_all([project, other])
    session.flush()
    doc = add_matrix(session, project, "candidate-lineage.pdf", "4" * 64)
    other_doc = add_matrix(session, project, "other-document.pdf", "3" * 64)
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
        source_document_id=doc.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model="test-model",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
        allow_unsealed_legacy=True,
    )
    another_run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        model="test-model",
        allow_unsealed_legacy=True,
    )

    for values in (
        {"id": candidate.id + 1_000_000},
        {"project_id": other.id},
        {"source_document_id": other_doc.id},
        {"extraction_run_id": None},
        {"extraction_run_id": another_run.id},
        {"kind": "event"},
        {"source_pages": [2]},
        {"confidence": 0.5},
        {"prompt_version": "rewritten"},
        {"model": "rewritten"},
    ):
        with pytest.raises(IntegrityError, match="Candidate run lineage is immutable"):
            with session.begin_nested():
                session.execute(
                    update(Candidate)
                    .where(Candidate.id == candidate.id)
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )

    with pytest.raises(IntegrityError, match="Candidate run lineage is immutable"):
        with session.begin_nested():
            session.execute(delete(Candidate).where(Candidate.id == candidate.id))

    with pytest.raises(IntegrityError, match="Candidate run lineage is immutable"):
        with session.begin_nested():
            session.execute(text("truncate table candidates cascade"))

    with pytest.raises(IntegrityError, match="attach to its run after input capture"):
        with session.begin_nested():
            session.add(
                Candidate(
                    project_id=project.id,
                    kind="dependency",
                    payload_json={"kind": "dependency", "fields": {}},
                    source_document_id=doc.id,
                    extraction_run_id=run.id,
                    source_pages=[1],
                    confidence=1.0,
                    prompt_version=PROMPT_VERSION,
                    model="test-model",
                    citations_verified=True,
                )
            )
            session.flush()


def test_non_demo_synthetic_run_receipts_still_reject_delete(session, project):
    doc = add_matrix(session, project, "synthetic-eval.pdf", "6" * 64)
    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )

    with pytest.raises(IntegrityError, match="receipts are immutable"):
        with session.begin_nested():
            session.execute(delete(ExtractionRun).where(ExtractionRun.id == run.id))

    assert session.get(ExtractionRun, run.id) is not None


def test_demo_run_receipts_remain_deletable_for_reset(session):
    demo = Project(slug="corridor-demo", name="Corridor Demo", is_synthetic=True)
    session.add(demo)
    session.flush()
    doc = add_matrix(session, demo, "demo-reset.pdf", "5" * 64)
    candidate = Candidate(
        project_id=demo.id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "D1"}},
        source_document_id=doc.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model="test-model",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
        allow_unsealed_legacy=True,
    )

    session.execute(delete(Candidate).where(Candidate.id == candidate.id))
    session.execute(delete(ExtractionRun).where(ExtractionRun.id == run.id))

    assert session.get(ExtractionRun, run.id, populate_existing=True) is None


def test_failed_or_foreign_runs_cannot_be_declared_active(session, project):
    first = add_matrix(session, project, "a.pdf", "d" * 64)
    second = add_matrix(session, project, "b.pdf", "e" * 64)
    failed = record_extraction_run(
        session,
        first,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=1,
        outcome="failed",
        error_detail="upstream unavailable",
        allow_unsealed_legacy=True,
    )
    completed = record_extraction_run(
        session,
        second,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    session.flush()

    with pytest.raises(ValueError, match="only a completed"):
        extraction_runs.declare_active_run(
            session, first.id, failed.id, principal=DECLARER
        )
    with pytest.raises(ValueError, match="does not belong"):
        extraction_runs.declare_active_run(
            session, first.id, completed.id, principal=DECLARER
        )


def test_operator_entrypoint_declares_the_exact_active_run(
    session, project, capsys, monkeypatch
):
    from corridor.config import settings

    monkeypatch.setattr(settings, "human_principal", "local:operator")
    doc = add_matrix(session, project, "operator.pdf", "f" * 64)
    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    session.flush()
    document_id = doc.id
    extraction_run_id = run.id

    class ScopedSession:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            session.close()
            return False

    assert extraction_runs.main(
        [str(document_id), str(extraction_run_id)], session_factory=ScopedSession
    ) == 0
    assert (
        _run_id(extraction_runs.active_run_for_document(session, document_id))
        == extraction_run_id
    )
    out = capsys.readouterr().out
    assert f"Active Run {extraction_run_id}" in out
    assert "declared by local:operator" in out


def test_null_or_ambiguous_lineage_is_not_active(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    session.add(
        Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
            source_document_id=doc.id,
            source_pages=[1],
            confidence=1.0,
            citations_verified=True,
        )
    )
    session.flush()

    assert extraction_runs.active_run_for_document(session, doc.id) is None
    runs = session.scalars(
        select(ExtractionRun).where(ExtractionRun.document_id == doc.id)
    ).all()
    assert runs == []



def _completed_run(session, doc, suffix):
    return record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.{suffix}",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )


def test_declaring_an_active_run_requires_an_attributable_human(session, project):
    """The one control every reader trusts is signed, like Admission."""
    doc = add_matrix(session, project, "attributed.pdf", "d" * 64)
    run = _completed_run(session, doc, "attributed")

    with pytest.raises(InvalidHumanPrincipal):
        extraction_runs.declare_active_run(
            session, doc.id, run.id, principal="local:free-text"
        )
    assert extraction_runs.active_run_for_document(session, doc.id) is None


def test_declarations_append_and_the_prior_one_survives_as_history(
    session, project
):
    doc = add_matrix(session, project, "appended.pdf", "e" * 64)
    first = _completed_run(session, doc, "first")
    second = _completed_run(session, doc, "second")

    extraction_runs.declare_active_run(session, doc.id, first.id, principal=DECLARER)
    extraction_runs.declare_active_run(
        session, doc.id, second.id, principal=RELIEF_DECLARER
    )

    declarations = session.scalars(
        select(ActiveRunDeclaration)
        .where(ActiveRunDeclaration.document_id == doc.id)
        .order_by(ActiveRunDeclaration.id)
    ).all()
    assert [d.extraction_run_id for d in declarations] == [first.id, second.id]
    assert [d.declared_by for d in declarations] == [
        DECLARER.subject,
        RELIEF_DECLARER.subject,
    ]
    assert declarations[0].predecessor_declaration_id is None
    assert declarations[1].predecessor_declaration_id == declarations[0].id
    assert (
        _run_id(extraction_runs.active_run_for_document(session, doc.id))
        == second.id
    )


def test_redeclaring_the_same_run_does_not_duplicate_history(session, project):
    """An identical rerun records no new outcome."""
    doc = add_matrix(session, project, "idempotent.pdf", "f" * 64)
    run = _completed_run(session, doc, "only")

    extraction_runs.declare_active_run(session, doc.id, run.id, principal=DECLARER)
    extraction_runs.declare_active_run(
        session, doc.id, run.id, principal=RELIEF_DECLARER
    )

    declarations = session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == doc.id
        )
    ).all()
    assert len(declarations) == 1
    assert declarations[0].declared_by == DECLARER.subject


def test_declaring_a_new_active_run_leaves_record_inclusion_pending(session, project):
    """The newly operative candidates reach the durable load handoff."""
    doc = add_matrix(session, project, "handoff.pdf", "9" * 64)
    run = _completed_run(session, doc, "handoff")
    # Simulate the prior completed-run handoff having already drained before a
    # person chooses which of several readings is current.
    from corridor.models import RecordInclusionRequest

    pending = session.get(RecordInclusionRequest, project.id)
    assert pending is not None
    pending.reconciled_seq = pending.dirty_seq
    session.flush()

    extraction_runs.declare_active_run(session, doc.id, run.id, principal=DECLARER)

    assert record_inclusion_pending(session, project.id) is True


def test_the_current_declaration_is_the_chain_tail_not_an_id_order(
    session, project
):
    """Current is the declaration nothing has superseded — a chain fact."""
    doc = add_matrix(session, project, "tail.pdf", "1" * 64)
    first = _completed_run(session, doc, "one")
    second = _completed_run(session, doc, "two")

    extraction_runs.declare_active_run(session, doc.id, first.id, principal=DECLARER)
    extraction_runs.declare_active_run(
        session, doc.id, second.id, principal=DECLARER
    )
    extraction_runs.declare_active_run(session, doc.id, first.id, principal=DECLARER)

    current = extraction_runs.current_active_run_declaration(session, doc.id)
    assert current is not None
    assert current.extraction_run_id == first.id
    successors = session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.predecessor_declaration_id == current.id
        )
    ).all()
    assert successors == []
    projection = session.get(ActiveExtractionRun, doc.id, populate_existing=True)
    assert projection.extraction_run_id == current.extraction_run_id


def test_declaration_history_is_immutable_below_the_service_boundary(
    session, project
):
    doc = add_matrix(session, project, "sealed.pdf", "2" * 64)
    run = _completed_run(session, doc, "sealed")
    extraction_runs.declare_active_run(session, doc.id, run.id, principal=DECLARER)
    declaration = session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == doc.id
        )
    ).one()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                update(ActiveRunDeclaration)
                .where(ActiveRunDeclaration.id == declaration.id)
                .values(declared_by="local:revisionist")
            )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                delete(ActiveRunDeclaration).where(
                    ActiveRunDeclaration.id == declaration.id
                )
            )


def test_a_role_label_is_not_a_declarer(session, project):
    """'reviewer' and 'system' are roles; the declarer is a person."""
    doc = add_matrix(session, project, "role.pdf", "3" * 64)
    run = _completed_run(session, doc, "role")

    for label in ("local:system", "local:reviewer"):
        with pytest.raises(InvalidHumanPrincipal):
            extraction_runs.declare_active_run(
                session, doc.id, run.id, principal=HumanPrincipal(label)
            )
    assert extraction_runs.active_run_for_document(session, doc.id) is None
