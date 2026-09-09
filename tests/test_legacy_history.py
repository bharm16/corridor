"""History custody preserves original attribution and supports exact replay."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor.db import Session, engine
from corridor.legacy_history import (
    HistoryRefused, capture_history, coordination_decisions_as_of, history_rows,
    inventory_history, read_history, reverse_history,
)
from corridor.models import Dependency, Project, WorkDecision


@pytest.fixture
def session():
    connection = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def historical_project(session):
    project = Project(slug="historical-custody", name="Historical custody", is_synthetic=True)
    other = Project(slug="historical-other", name="Other", is_synthetic=True)
    session.add_all([project, other])
    session.flush()
    dependency = Dependency(project_id=project.id, ref_code="DEP-1", dep_type="utility_relocation", title="Original title")
    foreign = Dependency(project_id=other.id, ref_code="DEP-1", dep_type="utility_relocation", title="Other project")
    session.add_all([dependency, foreign])
    session.flush()
    first = WorkDecision(dependency_id=dependency.id, decision_type="assign_internal_owner", field="internal_owner",
        after_value="Original owner", recorded_by="local:original-author",
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.add(first)
    session.flush()
    second = WorkDecision(dependency_id=dependency.id, decision_type="assign_internal_owner", field="internal_owner",
        before_value="Original owner", after_value="Corrected owner", recorded_by="local:correcting-author",
        recorded_at=datetime(2026, 2, 1, tzinfo=timezone.utc), predecessor_decision_id=first.id)
    session.add(second)
    session.flush()
    return project, other, dependency


def test_history_replay_preserves_original_actor_and_time_and_reverses_without_loss(session, historical_project):
    project, other, _dependency = historical_project
    inventory = inventory_history(session, project.id)
    assert inventory.counts["dependencies"] == 1
    assert inventory.counts["work_decisions"] == 2
    batch = capture_history(session, inventory, run_key="rehearsal-1", executor="migration:operator", code_revision="a" * 40)
    assert capture_history(session, inventory, run_key="rehearsal-1", executor="migration:operator", code_revision="a" * 40).id == batch.id
    old = coordination_decisions_as_of(batch, at=datetime(2026, 1, 15, tzinfo=timezone.utc))
    new = coordination_decisions_as_of(batch, at=datetime(2026, 2, 15, tzinfo=timezone.utc))
    assert [(item["recorded_by"], item["after_value"]) for item in old] == [("local:original-author", "Original owner")]
    assert [(item["recorded_by"], item["after_value"]) for item in new] == [("local:correcting-author", "Corrected owner")]
    assert batch.executor == "migration:operator"
    assert history_rows(batch, "dependencies")[0]["title"] == "Original title"
    with pytest.raises(HistoryRefused, match="belong"):
        read_history(session, other.id, batch.id)
    withdrawn = reverse_history(session, batch, actor="local:reviewer", reason="rollback rehearsal")
    assert withdrawn.reversed
    assert history_rows(withdrawn, "work_decisions") == history_rows(batch, "work_decisions")
    with pytest.raises(DBAPIError, match="reactivated"), session.begin_nested():
        capture_history(session, inventory, run_key="rehearsal-1", executor="migration:operator", code_revision="a" * 40)


def test_capture_refuses_drift_and_database_rewrites(session, historical_project):
    project, _other, dependency = historical_project
    inventory = inventory_history(session, project.id)
    dependency.title = "Changed after inventory"
    session.flush()
    with pytest.raises(DBAPIError, match="changed since inventory"), session.begin_nested():
        capture_history(session, inventory, run_key="drift", executor="migration:operator", code_revision="a" * 40)
    inventory = inventory_history(session, project.id)
    batch = capture_history(session, inventory, run_key="fresh", executor="migration:operator", code_revision="a" * 40)
    for statement in ("update legacy_history_batches set executor='forged' where id=:id", "delete from legacy_history_batches where id=:id"):
        with pytest.raises(DBAPIError, match="immutable"), session.begin_nested():
            session.execute(text(statement), {"id": batch.id})
    with pytest.raises(HistoryRefused, match="timezone-aware"):
        coordination_decisions_as_of(batch, at=datetime(2026, 1, 15))


def test_evidence_backfill_uses_only_one_exact_locator_and_reverses_reader_routing(session, historical_project):
    from hashlib import sha256
    from corridor.evidence_citations import evidence_quotation
    from corridor.legacy_history import backfill_evidence_sources
    from corridor.models import Document, EvidenceLink, SourceSegment

    project, _other, dependency = historical_project
    document = Document(project_id=project.id, sha256="d" * 64, filename="historical.pdf",
                        doc_type="minutes", parse_status="parsed", pages=1)
    session.add(document)
    session.flush()
    words = "Owner confirmed March relocation."
    segment = SourceSegment(project_id=project.id, document_id=document.id, kind="prose_span",
        exact_text=words, content_sha256=sha256(words.encode()).hexdigest(), ordinal=1,
        page_no=1, start_offset=0, end_offset=len(words))
    exact = EvidenceLink(dependency_id=dependency.id, document_id=document.id, page_no=1,
                         quote=words, verified=True)
    partial = EvidenceLink(dependency_id=dependency.id, document_id=document.id, page_no=1,
                           quote="March relocation.", verified=True)
    session.add_all([segment, exact, partial])
    session.flush()
    inventory = inventory_history(session, project.id)
    batch = capture_history(session, inventory, run_key="source-migration", executor="migration:operator", code_revision="b" * 40)
    results = backfill_evidence_sources(session, batch)
    assert [(row["legacy_evidence_link_id"], row["outcome"]) for row in results] == [
        (exact.id, "segment_reference"), (partial.id, "retained_quote")]
    assert backfill_evidence_sources(session, batch) == results
    assert evidence_quotation(session, exact).source_segment_ids == (segment.id,)
    assert evidence_quotation(session, partial).owner == "legacy_quote"
    session.refresh(exact)
    assert exact.quote == words
    reverse_history(session, batch, actor="local:reviewer", reason="rollback source migration")
    assert evidence_quotation(session, exact).owner == "legacy_quote"
    assert evidence_quotation(session, exact).passages == (words,)
