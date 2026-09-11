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
from corridor.config import settings
from corridor.due_work import (
    HANDLER_DELTA_GENERATION,
    HANDLER_PROJECT_PROCESSING,
    DeltaGenerationDeclaration,
    DueWorkRefusal,
    ProjectProcessingDeclaration,
    claim_due_work,
    configure_due_work,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.fact_decisions import include_structured_cell_fact_by_policy
from corridor.models import (
    Candidate,
    DeltaGroup,
    Dependency,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    Fact,
    FactDecision,
    Project,
    ProjectRecordRevision,
    ProposedDelta,
)
from corridor.review_packet_reading import (
    KEY_CONTRADICTED_IDENTITY,
    KEY_CONTRADICTED_VALUE,
    KEY_SOURCE_REVISION_BATCH,
    read_open_deltas,
)
from corridor.source_revision_declaration import COMPLETE_ENUMERATION
from source_capture_support import Rendition
from corridor.operating_mode import ADOPTED_BASELINE, adopt_project_baseline
from clock_support import ControlledClock
from later_revision_support import (
    BASELINE_ROWS,
    HEADINGS,
    adopt,
    register_delivered_revision,
    workbook_bytes,
)


ACCEPTED_STATION = "1149+00"
PROPOSED_STATION = "1200+00"
SUBJECT = "Utility Conflicts!3"
NEW_SUBJECT = "Utility Conflicts!9"


def _rendition(session, project, *, registry_id, sha_character, filename):
    """One arriving rendition, with the file identity this module reads."""

    return Rendition(
        session,
        project,
        filename,
        registry_id=registry_id,
        document_sha256=sha_character * 64,
        prompt_version="delta_generation_fixture_v1",
    )


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

        accepted = _rendition(
            setup, project, registry_id="REV-A", sha_character="a", filename="a.xlsx"
        )
        accepted_fact, _ = accepted.capture(
            fact_type="station_from",
            value=ACCEPTED_STATION,
            subject_key=SUBJECT,
            cell="D3",
        )
        _accept_by_policy(
            setup, project, accepted.document, accepted.run, accepted_fact
        )

        incoming = _rendition(
            setup, project, registry_id="REV-B", sha_character="b", filename="b.xlsx"
        )
        incoming.capture(
            fact_type="station_from",
            value=PROPOSED_STATION,
            subject_key=SUBJECT,
            cell="D3",
        )
        incoming.capture(
            fact_type="utility_id",
            value="UC-9",
            subject_key=NEW_SUBJECT,
            cell="A9",
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
        ids = (project.id, schedule.id, incoming.document.id)
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


# --- one delivery, one lineage (#937) --------------------------------------

# The three baseline rows this revision changes, one field each, so a delivery
# whose changes are compared twice is visible as six proposals rather than
# three.
REVISED_FIELDS = (("Size", "18 in"), ("Material", "Ductile Iron"), ("Utility Type", "Gas"))

# The cutoff the Review reading is taken against. Declared, never read from a
# clock, like every other cutoff in this suite.
REVIEW_AS_OF = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _revised(rows):
    """One baseline, with a different single cell changed on each of three rows."""

    copied = [list(row) for row in rows]
    for index, (column, value) in enumerate(REVISED_FIELDS):
        copied[index][HEADINGS.index(column)] = value
    return copied


def _adopted_project_with_a_later_revision(factory, now, tmp_path):
    """An adopted project, a declared later revision, and both standing passes.

    Configured through the same ``configure_due_work`` a deployment configures
    them through, and both of them: the defect this fixture exists for is only
    reachable when the ordinary processing pass and the generic delta pass are
    both standing, which is what a provisioned project has.
    """

    with factory() as setup:
        project = Project(
            slug=f"one-lineage-{uuid4().hex[:8]}",
            name="One delivery, one lineage",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        adopt(
            setup, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
        )
        document, _reading = register_delivered_revision(
            setup,
            project,
            workbook_bytes(tmp_path / "b.xlsx", _revised(BASELINE_ROWS)),
            tmp_path,
            completeness=COMPLETE_ENUMERATION,
        )
        starts_at = now.replace(minute=0, second=0, microsecond=0)
        for declaration in (
            ProjectProcessingDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="project-processing-v1",
                extractor_identity="corridor.extract_project",
                starts_at=starts_at,
            ),
            DeltaGenerationDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="delta-generation-v1",
                comparison_rule_version=COMPARISON_RULE_VERSION,
                starts_at=starts_at,
            ),
        ):
            configure_due_work(setup, declaration, now=now)
        ids = (int(project.id), int(document.id))
        setup.commit()
    return ids


def _work_everything_due(factory, now):
    """Every occurrence the deployed worker would claim, until none is left."""

    results = {}
    for _ in range(12):
        with factory() as ticking:
            with ticking.begin():
                enqueue_due_work(ticking, now=now)
        result = run_due_work_once(
            factory, clock=ControlledClock(now), owner="runtime:one-lineage"
        )
        if result is None:
            return results
        assert result.execution_outcome == "completed", (
            f"{result.handler_key} did not complete: {result.error_code}"
        )
        results[result.handler_key] = result
    raise AssertionError("the runtime never ran out of due work")


def test_a_delivered_revision_is_compared_by_the_one_producer_that_owns_it(
    runtime_database, tmp_path, monkeypatch
):
    """One file's changes arrive as one Delta Group under one lineage (#937).

    A delivery of the project's registered source has a dedicated producer:
    ``later_revision`` reads it under the registered mapping and appends its
    differences in the same transaction.  This pass used to sweep the Facts
    that capture had just written and compare them a second time under a
    lineage of its own, so three changed rows arrived as six proposals in two
    groups over one document and one source revision.
    """

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    project_id, document_id = _adopted_project_with_a_later_revision(
        factory, now, tmp_path
    )

    worked = _work_everything_due(factory, now)

    assert HANDLER_PROJECT_PROCESSING in worked, (
        "the registered revision was never read, so nothing below is a "
        "statement about what two producers do with one delivery"
    )
    # The generic pass ran and declined this delivery, rather than never
    # having been given the chance to double it.
    generic = worked[HANDLER_DELTA_GENERATION].handler_result
    assert generic["groups_created"] == 0
    assert generic["deltas_created"] == 0

    with factory() as verify:
        groups = verify.scalars(
            select(DeltaGroup).where(DeltaGroup.project_id == project_id)
        ).all()
        deltas = _deltas(verify, project_id)
        assert [
            (group.source_family, group.document_id) for group in groups
        ] == [("ucm_workbook:ucm-published-column-headings", document_id)], (
            "one delivery of one registered source opened more than one Delta "
            "Group over the same document and the same revision"
        )
        assert len(deltas) == len(REVISED_FIELDS)
        assert {delta.group_id for delta in deltas} == {groups[0].id}
        assert {
            (delta.source_family, delta.source_revision) for delta in deltas
        } == {(groups[0].source_family, groups[0].source_revision)}


def test_one_delivery_never_reads_as_two_sources_disagreeing(
    runtime_database, tmp_path, monkeypatch
):
    """What the coordinator is told, which is the point of the ticket (#937).

    ``review_packet_reading`` keys an item as a disagreement when two lineages
    answer one subject and field differently, so a second producer over one
    delivery does not merely leave a duplicate row behind: it asks a person to
    settle a Source Discrepancy that their data does not contain.  This asserts
    the sentence the reading prints, because a row count can be right while the
    sentence is still wrong.
    """

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    project_id, _document_id = _adopted_project_with_a_later_revision(
        factory, now, tmp_path
    )

    _work_everything_due(factory, now)

    with factory() as verify:
        reading = read_open_deltas(
            verify, project_id=project_id, as_of=REVIEW_AS_OF
        )
        assert [item.key_reason for item in reading.items] == [
            KEY_SOURCE_REVISION_BATCH
        ], [item.key_sentence for item in reading.items]
        (item,) = reading.items
        assert item.key_sentence == (
            "one authoritative source revision proposed these changes together "
            "and they fail the same way, so they are decided together"
        )
        assert len(item.delta_ids) == len(REVISED_FIELDS)
        assert KEY_CONTRADICTED_VALUE not in {
            item.key_reason for item in reading.items
        }
        assert KEY_CONTRADICTED_IDENTITY not in {
            item.key_reason for item in reading.items
        }
