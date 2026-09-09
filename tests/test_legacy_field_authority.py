"""Exact protected field/alias authority; no mutable admission-label fallback."""

from hashlib import sha256

import pytest
from sqlalchemy import text

from corridor.coordination_history import migrate_coordination_history
from corridor.db import Session, engine
from corridor.fact_decisions import include_structured_cell_fact_by_policy
from corridor.legacy_field_authority import read_legacy_field_authority
from corridor.legacy_history import capture_history, inventory_history
from corridor.models import ActiveExtractionRun, Dependency, Document, ExtractionRun, Fact, FactSource, Project, SourceSegment
from corridor.principals import HumanPrincipal
from corridor.subject_resolution import decide_subject_alias, resolve_subject_reference


@pytest.fixture
def session():
    connection = engine.connect().execution_options(isolation_level="READ COMMITTED")
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


def _source(session, project, label):
    document = Document(project_id=project.id, sha256=sha256(label.encode()).hexdigest(),
        filename=f"{label}.xlsx", doc_type="matrix", pages=1, parse_status="parsed")
    session.add(document)
    session.flush()
    run = ExtractionRun(document_id=document.id, prompt_version="exact-field-map-v1", outcome="completed", candidate_count=0, page_errors=0)
    session.add(run)
    session.flush()
    session.add(ActiveExtractionRun(document_id=document.id, extraction_run_id=run.id))
    facts, segments = [], []
    for ordinal, (field, value) in enumerate((("utility_id", label), ("station_from", "10+00")), 1):
        segment = SourceSegment(project_id=project.id, document_id=document.id, kind="spreadsheet_cell",
            exact_text=value, content_sha256=sha256(value.encode()).hexdigest(), ordinal=ordinal,
            sheet_name="Matrix", cell_range=f"{'A' if ordinal == 1 else 'B'}3")
        session.add(segment)
        session.flush()
        fact = Fact(project_id=project.id, document_id=document.id, extraction_run_id=run.id,
            subject_kind="source_row", subject_key=f"{label}:Matrix!3", fact_type=field, text_value=value,
            transformation="trim_cell_text_v1", recorded_by="extractor:field-map",
            content_sha256=sha256(f"{label}:{field}".encode()).hexdigest())
        session.add(fact)
        session.flush()
        session.add(FactSource(project_id=project.id, document_id=document.id, fact_id=fact.id,
            source_segment_id=segment.id, role="value_source", ordinal=1))
        facts.append(fact)
        segments.append(segment)
    session.flush()
    return facts, segments


def _alias(session, project, dependency, segment):
    attempt = resolve_subject_reference(session, project_id=project.id, source_segment_id=segment.id,
        reference_kind="source_identifier", raw_reference=segment.exact_text,
        expected_subject_type="constraint", usage="identity")
    return decide_subject_alias(session, attempt_id=attempt.attempt_id, subject_type="constraint",
        subject_id=dependency.id, principal=HumanPrincipal("local:identity-author"))


def test_exact_alias_requires_accepted_identifier_and_preserves_independent_revisions(session):
    project = Project(slug="legacy-native-field-map", name="Field map", is_synthetic=True)
    session.add(project)
    session.flush()
    dependency = Dependency(project_id=project.id, ref_code="DEP-FIELD", dep_type="utility_relocation", title="Compatibility title")
    session.add(dependency)
    session.flush()
    facts, segments = _source(session, project, "SOURCE-ONE")
    value = include_structured_cell_fact_by_policy(session, facts[1], idempotency_key="field-value")
    alias = _alias(session, project, dependency, segments[0])
    batch = capture_history(session, inventory_history(session, project.id), run_key="field-identity",
        executor=session.scalar(text("select session_user")), code_revision="c" * 40)
    migrate_coordination_history(session, batch)
    # A worker could append an identifier Fact with matching source keys.
    # An alias alone must not authorize that unaccepted identifier's join.
    assert read_legacy_field_authority(session, project.id).bindings == ()
    identifier = include_structured_cell_fact_by_policy(session, facts[0], idempotency_key="identifier-value")
    mapping = read_legacy_field_authority(session, project.id)
    station = next(item for item in mapping.bindings if item.field == "station_from")
    assert station.value == "10+00"
    assert station.source_fact_decision_id == value.decision.id
    assert station.revision_id == value.revision.id
    assert station.identifier_fact_decision_id == identifier.decision.id
    assert station.subject_resolution_revision_id == alias.revision_id
    assert station.subject_resolution_actor == "local:identity-author"
    assert {gap.reason for gap in mapping.gaps} == {"legacy_population_and_original_field_history_unproven"}
    assert read_legacy_field_authority(session, project.id, alias.revision_id).bindings == ()
    assert read_legacy_field_authority(session, project.id, identifier.revision.id).bindings == mapping.bindings
    with pytest.raises(ValueError, match="does not belong"):
        read_legacy_field_authority(session, project.id, identifier.revision.id + 100000)

    # A different accepted source can identify the same record. Equal source
    # values are still competing ownership claims, never a tie to pick by age.
    other_facts, other_segments = _source(session, project, "SOURCE-TWO")
    _alias(session, project, dependency, other_segments[0])
    for fact in other_facts:
        include_structured_cell_fact_by_policy(session, fact, idempotency_key=f"other:{fact.fact_type}")
    ambiguous = read_legacy_field_authority(session, project.id)
    assert ambiguous.bindings == ()
    assert any(gap.field == "station_from" and gap.reason == "ambiguous_accepted_source_identity" for gap in ambiguous.gaps)
    assert read_legacy_field_authority(session, project.id, identifier.revision.id).bindings == mapping.bindings
