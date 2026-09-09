"""Publication support migrates as document/citation provenance, never Ready."""

from hashlib import sha256

import pytest
from sqlalchemy import text

from corridor.db import Session, engine
from corridor.legacy_history import capture_history, inventory_history, reverse_history
from corridor.models import Dependency, Document, EvidenceLink, SourceSegment
from corridor.operative_support import designate_publication_support, resolve_operative_support
from corridor.principals import HumanPrincipal
from corridor.support_history import (
    migrate_support_history, native_publication_support,
    native_publication_support_as_of_revision, support_scope_identities,
)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def support_case(session):
    from corridor.models import Project
    project = Project(slug="native-support-history", name="Native support", is_synthetic=True)
    session.add(project)
    session.flush()
    dependency = Dependency(project_id=project.id, ref_code="DEP-SUPPORT", dep_type="utility_relocation", title="Utility conflict")
    document = Document(project_id=project.id, sha256="d" * 64, filename="minutes.pdf", doc_type="minutes", parse_status="parsed", pages=1)
    session.add_all([dependency, document])
    session.flush()
    words = "Original supporting passage."
    segment = SourceSegment(project_id=project.id, document_id=document.id, kind="prose_span", exact_text=words,
        content_sha256=sha256(words.encode()).hexdigest(), ordinal=1, page_no=1, start_offset=0, end_offset=len(words))
    link = EvidenceLink(dependency_id=dependency.id, document_id=document.id, page_no=1, quote=words, verified=True)
    session.add_all([segment, link])
    session.flush()
    scope = designate_publication_support(session, dependency.id, link.id, principal=HumanPrincipal("local:original-support-author"))
    batch = capture_history(session, inventory_history(session, project.id), run_key="support-1",
                            executor=session.scalar(text("select session_user")), code_revision="a" * 40)
    return project, dependency, document, segment, link, scope, batch


def test_reuses_native_document_decision_and_preserves_source_actor_without_ready(session, support_case):
    project, dependency, _document, segment, _link, _scope, batch = support_case
    before = session.scalar(text("select count(*) from fact_decisions where project_id=:p"), {"p": project.id})
    receipts = migrate_support_history(session, batch)
    assert [row["outcome"] for row in receipts] == ["native"]
    assert migrate_support_history(session, batch) == receipts
    assert session.scalar(text("select count(*) from fact_decisions where project_id=:p"), {"p": project.id}) == before
    reading = native_publication_support(session, (dependency.id,))[(dependency.id, None)]
    assert reading.source_segment_id == segment.id
    assert reading.exact_text == "Original supporting passage."
    assert reading.original_actor == reading.decision_actor == "local:original-support-author"
    assert reading.authority_kind == "human"
    assert support_scope_identities(session, project.id)[0].native_route_active
    publication = resolve_operative_support(session, (dependency.id,))[dependency.id]
    assert publication.publication.quote == "Original supporting passage."
    assert not publication.is_ready
    reverse_history(session, batch, actor=session.scalar(text("select session_user")), reason="rollback source routing")
    assert native_publication_support(session, (dependency.id,)) == {}
    assert native_publication_support_as_of_revision(session, project.id, reading.revision_id)[(dependency.id, None)].exact_text == reading.exact_text


def test_changed_passage_in_same_document_gets_an_immutable_revision_boundary(session, support_case):
    project, dependency, document, _segment, _link, _scope, batch = support_case
    migrate_support_history(session, batch)
    original = native_publication_support(session, (dependency.id,))[(dependency.id, None)]
    words = "A different supporting passage."
    source = SourceSegment(project_id=project.id, document_id=document.id, kind="prose_span", exact_text=words,
        content_sha256=sha256(words.encode()).hexdigest(), ordinal=2, page_no=1, start_offset=50, end_offset=50+len(words))
    link = EvidenceLink(dependency_id=dependency.id, document_id=document.id, page_no=1, quote=words, verified=True)
    session.add_all([source, link])
    session.flush()
    designate_publication_support(session, dependency.id, link.id, principal=HumanPrincipal("local:later-support-author"))
    current = native_publication_support(session, (dependency.id,))[(dependency.id, None)]
    assert current.source_segment_id == source.id and current.revision_id != original.revision_id
    assert current.decision_actor == "local:later-support-author"
    historic = native_publication_support_as_of_revision(session, project.id, original.revision_id)[(dependency.id, None)]
    assert historic.source_segment_id == original.source_segment_id


def test_missing_locator_is_an_explicit_gap_and_cannot_hide_as_native(session, support_case):
    project, dependency, document, _segment, _link, _scope, _batch = support_case
    unknown = EvidenceLink(dependency_id=dependency.id, document_id=document.id, page_no=1,
                           quote="No exact retained segment owns these words.", verified=True)
    session.add(unknown)
    session.flush()
    designate_publication_support(session, dependency.id, unknown.id, principal=HumanPrincipal("local:source-author"))
    batch = capture_history(session, inventory_history(session, project.id), run_key="support-gap",
                            executor=session.scalar(text("select session_user")), code_revision="a" * 40)
    receipts = migrate_support_history(session, batch)
    assert [(row["outcome"], row["reason"]) for row in receipts] == [("retained_compatibility", "missing_exact_source_locator")]
    assert native_publication_support(session, (dependency.id,)) == {}
    assert support_scope_identities(session, project.id)[0].reason == "missing_exact_source_locator"


def test_direct_sql_cannot_replay_old_human_support_over_a_later_native_decision(session, support_case):
    from corridor.fact_decisions import record_human_fact_decision
    from corridor.models import Fact

    project, dependency, _document, segment, _link, scope, batch = support_case
    receipt = migrate_support_history(session, batch)[0]
    original_fact = session.get(Fact, session.scalar(text("select fact_id from fact_decisions where id=:id"), {"id": receipt["fact_decision_id"]}))
    record_human_fact_decision(session, original_fact, principal=HumanPrincipal("local:later-authority"),
        command_type="resolve_support", disposition="restore", idempotency_key="later-native-support-withdrawal",
        expected_predecessor=receipt["fact_decision_id"])
    before = session.scalar(text("select count(*) from fact_decisions where project_id=:p"), {"p": project.id})
    descriptor = session.scalar(text("select support_scope_source(:p,:s)"), {"p": project.id, "s": scope.id})
    outcome_id = session.scalar(text("select refresh_support_scope(:p,:s,:f,:segment,:digest)"),
        {"p": project.id, "s": scope.id, "f": original_fact.id, "segment": segment.id, "digest": descriptor["digest"]})
    assert session.scalar(text("select reason from support_history_receipts where id=:id"), {"id": outcome_id}) == "native_authority_advanced"
    assert session.scalar(text("select count(*) from fact_decisions where project_id=:p"), {"p": project.id}) == before
    assert native_publication_support(session, (dependency.id,)) == {}
    # Repeating after the refusal must remain a refusal, not turn the gap
    # receipt itself into a route around the original-act replay check.
    assert session.scalar(text("select refresh_support_scope(:p,:s,:f,:segment,:digest)"),
        {"p": project.id, "s": scope.id, "f": original_fact.id, "segment": segment.id, "digest": descriptor["digest"]}) == outcome_id


def test_direct_sql_cannot_select_an_arbitrary_duplicate_locator_or_refresh_withdrawn_admission(session, support_case):
    from sqlalchemy.exc import DBAPIError

    project, dependency, document, segment, _link, scope, batch = support_case
    receipt = migrate_support_history(session, batch)[0]
    fact_id = session.scalar(text("select fact_id from fact_decisions where id=:id"), {"id": receipt["fact_decision_id"]})
    duplicate = SourceSegment(project_id=project.id, document_id=document.id, kind="prose_span", exact_text=segment.exact_text,
        content_sha256=segment.content_sha256, ordinal=3, page_no=1, start_offset=100, end_offset=100+len(segment.exact_text))
    session.add(duplicate)
    session.flush()
    descriptor = session.scalar(text("select support_scope_source(:p,:s)"), {"p": project.id, "s": scope.id})
    outcome_id = session.scalar(text("select refresh_support_scope(:p,:s,:f,:segment,:digest)"),
        {"p": project.id, "s": scope.id, "f": fact_id, "segment": segment.id, "digest": descriptor["digest"]})
    assert session.scalar(text("select reason from support_history_receipts where id=:id"), {"id": outcome_id}) == "ambiguous_exact_source_locator"
    reverse_history(session, batch, actor=session.scalar(text("select session_user")), reason="withdraw native admission")
    with pytest.raises(DBAPIError, match="active reviewed native admission"), session.begin_nested():
        session.execute(text("select refresh_support_scope(:p,:s,:f,:segment,:digest)"),
            {"p": project.id, "s": scope.id, "f": fact_id, "segment": segment.id, "digest": descriptor["digest"]})


def test_receipted_transfer_contract_does_not_allow_generic_automatic_inclusion(session, support_case):
    from corridor.fact_decisions import FactDecisionRefused, include_structured_cell_fact_by_policy
    from corridor.models import Fact

    project, _dependency, document, _segment, _link, _scope, _batch = support_case
    fact_id = session.scalar(text("""
        select id from facts where project_id=:project
          and fact_type='supporting_documentation_in_use' and document_value_id=:document
    """), {"project": project.id, "document": document.id})
    fact = session.get(Fact, fact_id)
    before = session.scalar(text("select count(*) from fact_decisions where project_id=:project"), {"project": project.id})
    with pytest.raises(FactDecisionRefused, match="not eligible for automatic inclusion"):
        include_structured_cell_fact_by_policy(session, fact, idempotency_key="generic-support-policy-refused")
    assert session.scalar(text("select count(*) from fact_decisions where project_id=:project"), {"project": project.id}) == before


def test_direct_sql_cannot_backdate_policy_authority_from_legacy_machine_label(session, support_case):
    project, _dependency, _document, segment, _link, scope, _batch = support_case
    # The schema-owner fixture establishes retained protected legacy state.
    # Runtime cannot perform this write; its metadata may only claim which
    # policy ran, and cannot authenticate a new native policy decision.
    session.execute(text("update operative_support set designated_by='corridor:automatic-carry-forward' where id=:id"), {"id": scope.id})
    batch = capture_history(session, inventory_history(session, project.id), run_key="machine-policy-identity-gap",
        executor=session.scalar(text("select session_user")), code_revision="b" * 40)
    before = session.scalar(text("select count(*) from project_record_revisions where project_id=:p"), {"p": project.id})
    receipts = migrate_support_history(session, batch)
    assert [(row["outcome"], row["reason"]) for row in receipts] == [
        ("retained_compatibility", "unproven_original_policy_identity")]
    assert receipts[0]["fact_decision_id"] is None
    assert session.scalar(text("select count(*) from project_record_revisions where project_id=:p"), {"p": project.id}) == before
    assert session.scalar(text("select exact_text from source_segments where id=:id"), {"id": segment.id}) == segment.exact_text


def test_support_identity_does_not_bypass_unmigrated_coordination_history_drift(session, support_case):
    from sqlalchemy.exc import DBAPIError

    from corridor.coordination_history import migrate_coordination_history
    from corridor.models import WorkDecision

    project, dependency, _document, _segment, _link, _scope, batch = support_case
    migrate_support_history(session, batch)
    assert session.scalar(text("select count(*) from coordination_subject_lineage where history_batch_id=:batch"), {"batch": batch.id}) == 1
    assert session.scalar(text("select count(*) from coordination_history_activations where history_batch_id=:batch"), {"batch": batch.id}) == 0
    session.add(WorkDecision(dependency_id=dependency.id, field="internal_owner", decision_type="assign_internal_owner",
        after_value="Later owner", recorded_by="local:later-coordination-author"))
    session.flush()
    with pytest.raises(DBAPIError, match="coordination history changed"), session.begin_nested():
        migrate_coordination_history(session, batch)
    assert session.scalar(text("select count(*) from coordination_history_activations where history_batch_id=:batch"), {"batch": batch.id}) == 0
