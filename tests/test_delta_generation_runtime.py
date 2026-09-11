"""The delta-generation handler through the shared Due Work runtime (#488, #518).

A captured Source Fact that disagrees with the accepted record becomes a
Proposed Delta and nothing else: the pass proposes, it never accepts. These
tests hold that boundary, prove the watermark resumes where the last completed
receipt left off, and drill the durable lease — work committed by a worker that
crashed before its receipt is re-run without appending a second delta.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.delta_generation import (
    COMPARISON_RULE_VERSION,
    execute_delta_generation,
)
from corridor.due_work import (
    HANDLER_DELTA_GENERATION,
    DeltaGenerationDeclaration,
    DueWorkRefusal,
    claim_due_work,
    configure_due_work,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.fact_decisions import include_structured_cell_fact_by_policy
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    ExtractionRun,
    Fact,
    FactDecision,
    FactSource,
    Project,
    ProjectRecordRevision,
    ProposedDelta,
    SourceSegment,
)
from corridor.operating_mode import ADOPTED_BASELINE, adopt_project_baseline
from clock_support import ControlledClock


ACCEPTED_STATION = "1149+00"
PROPOSED_STATION = "1200+00"
SUBJECT = "Utility Conflicts!3"
NEW_SUBJECT = "Utility Conflicts!9"


def _document(session, project, *, registry_id, sha_character, filename):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha_character * 64,
        filename=filename,
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush([document])
    run = ExtractionRun(
        document_id=document.id,
        prompt_version="delta_generation_fixture_v1",
        outcome="completed",
        candidate_count=0,
        page_errors=0,
    )
    session.add(run)
    session.flush([run])
    session.add(ActiveExtractionRun(document_id=document.id, extraction_run_id=run.id))
    session.flush()
    return document, run


def _cell_fact(session, project, document, run, *, ordinal, cell, subject, kind, value):
    segment = SourceSegment(
        project_id=project.id,
        document_id=document.id,
        kind="spreadsheet_cell",
        exact_text=value,
        content_sha256=sha256(f"{document.sha256}:{cell}:{value}".encode()).hexdigest(),
        ordinal=ordinal,
        sheet_name="Utility Conflicts",
        cell_range=cell,
    )
    session.add(segment)
    session.flush([segment])
    fact = Fact(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=run.id,
        fact_type=kind,
        subject_kind="source_row",
        subject_key=subject,
        text_value=value,
        transformation="trim_cell_text_v1",
        recorded_by="extractor:delta_generation_fixture_v1",
        content_sha256=sha256(
            f"fact:{document.sha256}:{cell}:{value}".encode()
        ).hexdigest(),
    )
    session.add(fact)
    session.flush([fact])
    session.add(
        FactSource(
            project_id=project.id,
            document_id=document.id,
            fact_id=fact.id,
            source_segment_id=segment.id,
            role="value_source",
            ordinal=1,
        )
    )
    session.flush()
    return fact


def _accept_by_policy(session, project, document, run, fact):
    """The released structured-cell inclusion path, with the linkage it requires."""

    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="Accepted conflict",
    )
    session.add(dependency)
    session.flush([dependency])
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"fields": {}},
        source_document_id=document.id,
        source_pages=[1],
        prompt_version=run.prompt_version,
        citations_verified=True,
        state="accepted",
        merged_into=dependency.id,
    )
    session.add(candidate)
    session.flush([candidate])
    proposal = ExtractedProposal(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=run.id,
        candidate_id=candidate.id,
        kind="dependency",
        subject_key=fact.subject_key,
        candidate_metadata_json={"state": "pending", "source_pages": [1]},
    )
    session.add(proposal)
    session.flush([proposal])
    session.add(
        ExtractedProposalFact(
            project_id=project.id,
            document_id=document.id,
            extraction_run_id=run.id,
            proposal_id=proposal.id,
            fact_id=fact.id,
            ordinal=1,
        )
    )
    session.flush()
    return include_structured_cell_fact_by_policy(
        session, fact, idempotency_key=f"include:{project.id}:{fact.fact_type}"
    )


def _seed_project(factory, now, *, adopt=False):
    """One accepted station, and a newer source that disagrees and adds a subject."""

    with factory() as setup:
        project = Project(
            slug=f"delta-generation-{uuid4().hex[:8]}",
            name="Delta Generation",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])

        accepted_document, accepted_run = _document(
            setup, project, registry_id="REV-A", sha_character="a", filename="a.xlsx"
        )
        accepted_fact = _cell_fact(
            setup,
            project,
            accepted_document,
            accepted_run,
            ordinal=1,
            cell="D3",
            subject=SUBJECT,
            kind="station_from",
            value=ACCEPTED_STATION,
        )
        _accept_by_policy(setup, project, accepted_document, accepted_run, accepted_fact)

        incoming_document, incoming_run = _document(
            setup, project, registry_id="REV-B", sha_character="b", filename="b.xlsx"
        )
        _cell_fact(
            setup,
            project,
            incoming_document,
            incoming_run,
            ordinal=1,
            cell="D3",
            subject=SUBJECT,
            kind="station_from",
            value=PROPOSED_STATION,
        )
        _cell_fact(
            setup,
            project,
            incoming_document,
            incoming_run,
            ordinal=2,
            cell="A9",
            subject=NEW_SUBJECT,
            kind="utility_id",
            value="UC-9",
        )

        if adopt:
            adopt_project_baseline(
                setup,
                project_id=project.id,
                adopted_by_principal="local:baseline-adopter",
                baseline_source_sha256=sha256(b"ucm-baseline.xlsx").hexdigest(),
                importer_identity="ucm-workbook-importer",
                importer_version="v0",
                idempotency_key=f"adopt-{project.id}",
            )

        schedule = configure_due_work(
            setup,
            DeltaGenerationDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="delta-generation-v1",
                comparison_rule_version=COMPARISON_RULE_VERSION,
                starts_at=now.replace(minute=0, second=0, microsecond=0),
            ),
            now=now,
        )
        ids = (project.id, schedule.id, incoming_document.id)
        setup.commit()
    return ids


def _deltas(session, project_id):
    return session.scalars(
        select(ProposedDelta)
        .where(ProposedDelta.project_id == project_id)
        .order_by(ProposedDelta.id)
    ).all()


def _accepted_counts(session, project_id):
    return (
        session.scalar(
            select(func.count()).select_from(FactDecision).where(
                FactDecision.project_id == project_id
            )
        ),
        session.scalar(
            select(func.count()).select_from(ProjectRecordRevision).where(
                ProjectRecordRevision.project_id == project_id
            )
        ),
    )


def test_new_source_facts_become_proposed_deltas_in_one_group(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    project_id, schedule_id, incoming_document_id = _seed_project(factory, now)

    with factory() as ticking:
        [occurrence] = enqueue_due_work(ticking, now=now)
        assert occurrence.scheduled_job_id == schedule_id
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:delta-worker"
    )
    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_key == HANDLER_DELTA_GENERATION
    body = result.handler_result
    assert body["schema_version"] == "delta-generation-result-v1"
    # The accepted document's own Fact still says what the record says.
    assert body["facts_agreed"] == 1
    assert body["facts_considered"] == 3
    assert body["groups_created"] == 1
    assert body["deltas_created"] == 2
    assert body["operating_mode"] == "legacy"
    assert result.safe_next_step == "none"

    with factory() as verify:
        deltas = _deltas(verify, project_id)
        assert [delta.change_type for delta in deltas] == ["modify", "add"]
        by_target = {delta.target_type: delta for delta in deltas}
        modified = by_target["existing_subject"]
        assert modified.target_subject_identity == SUBJECT
        assert modified.target_field == "station_from"
        assert modified.accepted_value == ACCEPTED_STATION
        assert modified.proposed_value == PROPOSED_STATION
        assert modified.comparison_rule_version == COMPARISON_RULE_VERSION
        assert modified.source_family == "REV-B"
        # A genuinely new subject is one delta carrying its initial fields.
        proposed = by_target["proposed_subject"]
        assert proposed.target_subject_identity == NEW_SUBJECT
        assert proposed.target_field is None
        assert proposed.proposed_value == {"utility_id": "UC-9"}
        assert all(
            delta.group_id == deltas[0].group_id for delta in deltas
        ), "one atomic source change is one group"
        assert deltas[0].source_revision == verify.scalar(
            select(Document.sha256).where(Document.id == incoming_document_id)
        )
        status = due_work_status(verify, project_id=project_id)
        assert status["receipts"][0]["handler"] == HANDLER_DELTA_GENERATION


def test_the_pass_never_writes_an_accepted_value(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    project_id, _, _ = _seed_project(factory, now, adopt=True)
    with factory() as before:
        baseline = _accepted_counts(before, project_id)

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:delta-adopted"
    )

    assert result.execution_outcome == "completed"
    # An adopted-baseline project still captures Facts and proposes deltas; it
    # simply cannot have an accepted value replaced on any path here.
    assert result.handler_result["operating_mode"] == ADOPTED_BASELINE
    assert result.handler_result["deltas_created"] == 2
    with factory() as after:
        assert _accepted_counts(after, project_id) == baseline


def test_a_second_pass_resumes_from_the_retained_watermark(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    project_id, _, _ = _seed_project(factory, now)

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    first = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:delta-worker"
    )
    assert first.handler_result["deltas_created"] == 2
    watermark = first.handler_result["through_fact_id"]

    later = now + timedelta(hours=1)
    with factory() as ticking:
        enqueue_due_work(ticking, now=later)
        ticking.commit()
    second = run_due_work_once(
        factory, clock=ControlledClock(later), owner="runtime:delta-worker"
    )

    assert second.execution_outcome == "completed"
    assert second.handler_result["facts_considered"] == 0
    assert second.handler_result["deltas_created"] == 0
    assert second.handler_result["through_fact_id"] == watermark
    with factory() as verify:
        assert len(_deltas(verify, project_id)) == 2


def test_crash_before_the_receipt_re_runs_without_a_second_delta(runtime_database):
    """The restart drill: committed work, no receipt, recovery after the lease."""

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    project_id, schedule_id, _ = _seed_project(factory, now)

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    with factory() as claiming:
        claim = claim_due_work(claiming, now=now, owner="runtime:crashed")
        assert claim is not None
        claiming.commit()

    # The crashed worker had already committed its deltas durably.
    crashed = execute_delta_generation(
        factory, schedule_id=schedule_id, clock=ControlledClock(now)
    )
    assert crashed["deltas_created"] == 2
    with factory() as verify:
        assert len(_deltas(verify, project_id)) == 2

    recover_at = claim.lease_expires_at + timedelta(seconds=1)
    recovered = run_due_work_once(
        factory, clock=ControlledClock(recover_at), owner="runtime:recovery"
    )

    assert recovered is not None
    assert recovered.execution_outcome == "completed"
    assert recovered.attempt_id != claim.attempt_id
    # The watermark never advanced, so the recovered attempt re-considered the
    # same Facts and found their deltas already appended.
    assert recovered.handler_result["facts_considered"] == 3
    assert recovered.handler_result["deltas_created"] == 0
    assert recovered.handler_result["deltas_already_present"] == 2
    with factory() as verify:
        assert len(_deltas(verify, project_id)) == 2


def test_competing_workers_append_each_delta_once(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
    project_id, _, _ = _seed_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    ready = Barrier(2)

    def compete(owner):
        ready.wait(timeout=5)
        return run_due_work_once(
            factory, clock=ControlledClock(now), owner=owner
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(compete, ("runtime:worker-a", "runtime:worker-b"))
        )

    completed = [
        item
        for item in results
        if item is not None and item.execution_outcome == "completed"
    ]
    assert len(completed) == 1
    with factory() as verify:
        assert len(_deltas(verify, project_id)) == 2


def test_gate7_refuses_a_nonzero_model_budget_without_writing_a_schedule(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"delta-gate7-{uuid4().hex[:8]}",
            name="Delta Gate 7",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        base = DeltaGenerationDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="delta-generation-v1",
            comparison_rule_version=COMPARISON_RULE_VERSION,
            starts_at=now,
        )
        # The pass reads no model, so any positive budget is not a declaration.
        with pytest.raises(DueWorkRefusal, match="resource declaration is invalid"):
            configure_due_work(
                setup, replace(base, model_token_budget=1), now=now
            )
        with pytest.raises(DueWorkRefusal, match="comparison rule identity"):
            configure_due_work(
                setup, replace(base, comparison_rule_version="!!"), now=now
            )
