"""Production UCM readers consume explicit native identities and accepted decisions."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import re
from uuid import uuid4

from openpyxl import Workbook, load_workbook
import pytest
from sqlalchemy import event, select

from corridor.accepted_field_reading import NativeReadingRefused, read_accepted_field_population
from corridor.baseline_adoption import adopt_baseline, preview_baseline_adoption
from corridor.briefing import brief_project
from corridor.config import settings
from corridor.db import Session, engine
from corridor.exceptions import evaluate_project
from corridor.export import to_xlsx
from corridor.fact_decisions import record_human_fact_decision
from corridor.field_mapping_manifest import MappingDeclaration
from corridor.ledger import browse
from corridor.materializer import materialize_segment_value
from corridor.models import BaselineSourceRow, Fact, FactDecision, Project, SourceSegment
from corridor.principals import HumanPrincipal
from corridor.project_reading import freeze_project_reading
from corridor.reader_equivalence import native_constraint_log_surface
from corridor.report import Assertion, build_report, render
from corridor.source_append import append_fact
from corridor.source_intake import validate_and_stage


PRINCIPAL = HumanPrincipal("local:coordinator")
TODAY = date(2026, 9, 9)


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


def _adopt_native_workbook(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    project = Project(slug=f"native-readers-{uuid4().hex[:8]}", name="Native Reader Test", is_synthetic=True)
    session.add(project)
    session.flush()
    path = tmp_path / "ucm.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) — Utility Conflicts"])
    sheet.append(["Utility Conflict ID", "Utility Owner", "Utility Type", "Start Station", "End Station", "Promised For", "Action Due Date", "Comment"])
    sheet.append(["UC-1", "City Water", "Water", "100+00", "101+00", "2026-09-01", "2026-08-01", "first facility"])
    sheet.append(["UC-1", "Electric Company", "Electric", "200+00", "201+00", "2026-10-01", "2026-08-02", "different facility"])
    book.save(path)
    preview = preview_baseline_adoption(session, project=project,
        staged=validate_and_stage(path.read_bytes(), path.name), customer="Synthetic Customer",
        source_identity="UCM synthetic baseline", field_mapping=MappingDeclaration())
    result = adopt_baseline(session, preview=preview, principal=PRINCIPAL,
        idempotency_key="adopt-native-readers", images_dir=tmp_path / "images")
    return project, result


@pytest.fixture
def adopted(session, tmp_path, monkeypatch):
    return _adopt_native_workbook(session, tmp_path, monkeypatch)


class CoveringClient:
    model = "synthetic-briefing"
    def complete(self, *, user, **kwargs):
        references = sorted(set(re.findall(r"^  \[((?:XB|X|E|A|V)\d+)\]", user, re.MULTILINE)))
        return {"sentences": [{"text": "Read the accepted values and their cited sources.", "cites": references}]}


def test_actual_readers_never_select_legacy_populations_or_values(session, adopted, tmp_path):
    project, result = adopted
    observed = []
    forbidden = re.compile(r"\b(?:dependencies|candidates|assertions|evidence_links|work_decisions|operative_support)\b", re.I)
    def inspect_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            observed.append(statement)
            assert not forbidden.search(statement), statement
    event.listen(session.get_bind(), "before_cursor_execute", inspect_sql)
    try:
        reading = freeze_project_reading(session, project.id, today=TODAY)
        assert reading.native_population.revision_id == result.revision_id
        assert len(reading.rows) == 2
        assert {row.dependency.source_ref for row in reading.rows} == {"UC-1"}
        assert len({row.dependency.id for row in reading.rows}) == 2
        assert all(isinstance(row.dependency.id, str) for row in reading.rows)
        assert {row.dependency.station_from for row in reading.rows} == {"100+00", "200+00"}
        assert all(row.dependency.action_due_date is None for row in reading.rows)
        assert not any(finding.rule.startswith("ACTION_") for finding in reading.evaluation.found)
        assert len(browse(session, project.id, evaluation=reading.evaluation, owner="unassigned")) == 2
        report = build_report(session, project.id, today=TODAY, frozen_reading=reading)
        body = render(report)
        assert "100+00" in body and "200+00" in body and "p.None" not in body
        assert "Utility Conflicts!" in body
        assert any(isinstance(cell.provenance, Assertion) and cell.provenance.decision_id for cell in report.cells)
        briefing = brief_project(session, project.id, client=CoveringClient(), today=TODAY, frozen_reading=reading)
        assert not briefing.refused and briefing.sentences
        assert any("decision " in item.text for item in briefing.citables)
        output = to_xlsx(session, project.id, tmp_path / "native.xlsx", evaluation=reading.evaluation,
            statement_publication=reading.statement_publication, frozen_reading=reading)
        book = load_workbook(output, data_only=True)
        try:
            assert book.active.max_row == 3
            assert book.active["F2"].value in {"100+00", "200+00"}
            assert "Accepted value sources" in book.sheetnames
        finally:
            book.close()
        surface = native_constraint_log_surface(reading)
        assert surface.observed_record_kinds == {"constraint", "check"}
        assert sum(row.kind == "constraint" for row in surface.records) == 2
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", inspect_sql)
    assert observed


def test_frozen_native_rows_and_fields_cannot_be_mutated(session, adopted):
    project, _ = adopted
    reading = freeze_project_reading(session, project.id, today=TODAY)
    record = reading.rows[0].dependency
    with pytest.raises(FrozenInstanceError):
        record.subject_key = "changed"
    with pytest.raises(TypeError):
        record.fields["station_from"] = None
    assert reading.evaluation.native_population is reading.native_population


def _unselected_fact(session, project, *, subject_key):
    original = session.scalar(select(Fact).where(Fact.project_id == project.id, Fact.fact_type == "station_from").order_by(Fact.id))
    segment = session.scalar(select(SourceSegment).where(SourceSegment.document_id == original.document_id,
        SourceSegment.exact_text == original.text_value))
    return append_fact(session, project_id=project.id, document_id=original.document_id,
        extraction_run_id=original.extraction_run_id, subject_kind="source_row", subject_key=subject_key,
        recorded_by="extractor:synthetic", content_sha256=sha256(f"unselected:{subject_key}".encode()).hexdigest(),
        value=materialize_segment_value(session, "station_from", segment))


def test_source_check_does_not_make_an_undecided_fact_an_accepted_value(session, adopted):
    project, _ = adopted
    before = read_accepted_field_population(session, project.id)
    fact = _unselected_fact(session, project, subject_key="unmapped-new-source")
    after = read_accepted_field_population(session, project.id)
    assert before.fingerprint == after.fingerprint
    assert all(field.fact_id != fact.id for row in after.records for field in row.fields.values())
    record_human_fact_decision(session, fact, principal=PRINCIPAL, command_type="resolve_discrepancy", idempotency_key="decide-unmapped")
    with pytest.raises(NativeReadingRefused, match="no declared baseline"):
        read_accepted_field_population(session, project.id)


def test_ambiguous_effective_source_and_record_alias_decisions_refuse_instead_of_last_wins(session, adopted):
    project, _ = adopted
    row = session.scalar(select(BaselineSourceRow).where(BaselineSourceRow.project_id == project.id).order_by(BaselineSourceRow.id))
    assert row.source_row_key != row.record_subject_key
    fact = _unselected_fact(session, project, subject_key=row.record_subject_key)
    record_human_fact_decision(session, fact, principal=PRINCIPAL, command_type="resolve_discrepancy", idempotency_key="ambiguous-alias")
    with pytest.raises(NativeReadingRefused, match="no declared baseline"):
        read_accepted_field_population(session, project.id)


def test_native_as_of_reading_preserves_the_exact_decision_boundary(session, adopted):
    project, result = adopted
    before = freeze_project_reading(session, project.id, today=TODAY)
    record = before.native_population.records[0]
    held = record.fields["station_from"]
    fact = session.get(Fact, held.fact_id)
    changed = record_human_fact_decision(session, fact, principal=PRINCIPAL, command_type="resolve_discrepancy",
        idempotency_key="withdraw-station", expected_predecessor=held.decision_id, disposition="do_not_add")
    now = freeze_project_reading(session, project.id, today=TODAY)
    original = freeze_project_reading(session, project.id, today=TODAY, revision_id=result.revision_id)
    assert now.native_population.revision_id == changed.revision.id
    assert next(row for row in now.native_population.records if row.subject_key == record.subject_key).station_from is None
    assert original.native_population.fingerprint == before.native_population.fingerprint
    assert original.native_population.records[0].station_from == record.station_from
    paired = freeze_project_reading(session, project.id, today=TODAY, evaluation=before.evaluation,
        statement_publication=before.statement_publication)
    assert paired.native_population is before.native_population


def test_native_revision_and_legacy_population_gaps_are_explicit(session, adopted):
    project, result = adopted
    other = Project(slug=f"unmapped-{uuid4().hex[:8]}", name="No native map", is_synthetic=True)
    session.add(other)
    session.flush()
    with pytest.raises(NativeReadingRefused, match="legacy accepted-field"):
        read_accepted_field_population(session, other.id)
    with pytest.raises(ValueError, match="revision"):
        from corridor.record_projection import read_native_record_values
        read_native_record_values(session, other.id, result.revision_id)


def _follow_up_plan(session, project, adoption):
    from corridor.models import SupportAssessment, SupportAssessmentSource
    from corridor.proposed_deltas import ExistingSubjectTarget, ProposedDeltaValues, create_proposed_delta_group
    from corridor.review_packets import CoordinationRequest, PacketChildRequest, ReviewPacketRequest, NEEDS_COORDINATION, SAVED, resolve_review_packet
    fact = session.scalar(select(Fact).where(Fact.project_id == project.id, Fact.fact_type == "committed_date").order_by(Fact.id))
    assessment = session.scalar(select(SupportAssessment).where(SupportAssessment.fact_id == fact.id,
        SupportAssessment.evidence_role == "value_support", SupportAssessment.superseded_by.is_(None)))
    source_ids = tuple(session.scalars(select(SupportAssessmentSource.source_segment_id).where(
        SupportAssessmentSource.support_assessment_id == assessment.id).order_by(SupportAssessmentSource.ordinal)))
    revision = "native-reader-follow-up"
    (delta,) = create_proposed_delta_group(session, project_id=project.id,
        source_family="ucm-workbook", source_revision=revision,
        deltas=[ProposedDeltaValues(change_type="modify",
            target=ExistingSubjectTarget(subject_identity=fact.subject_key, field="committed_date"),
            accepted_value=fact.date_value.isoformat(), proposed_value="2026-11-01",
            accepted_baseline_revision=f"revision:{adoption.revision_id}")])
    at = datetime.now(timezone.utc)
    result = resolve_review_packet(session, ReviewPacketRequest(project_id=project.id,
        grouping_rule_version="packetizer-v1", grouping_key_kind="source_revision", grouping_key=revision,
        principal=PRINCIPAL, idempotency_key="native-reader-plan", decided_at=at,
        observed_accepted_revision_id=adoption.revision_id,
        children=(PacketChildRequest(delta_id=delta.id, outcome=NEEDS_COORDINATION,
            observed_source_revision=revision, coordination=CoordinationRequest(
                question="Which Promised For date does the utility confirm?",
                responsible_principal="local:utility-coordinator", responsible_organization="City Water",
                return_date=at + timedelta(days=7), affected_scope={"subject": fact.subject_key, "field": "committed_date"},
                evidence_support_assessment_ids=(assessment.id,))),)))
    assert result.status == SAVED
    return result, delta, assessment.id, source_ids


def test_native_follow_up_plan_preserves_question_evidence_without_accepting_its_proposal(session, adopted, tmp_path):
    from corridor.changes import snapshot
    from corridor.models import DeltaDisposition
    project, adoption = adopted
    result, delta, assessment_id, source_ids = _follow_up_plan(session, project, adoption)
    reading = freeze_project_reading(session, project.id, today=TODAY)
    (plan,) = reading.native_population.follow_up_plans
    assert plan.plan_id == result.children[0].follow_up_plan_id
    assert plan.revision_id == result.revision_id
    assert plan.support_assessment_ids == (assessment_id,)
    assert plan.source_segment_ids == source_ids
    assert plan.responsible_principal == "local:utility-coordinator"
    assert all(record.internal_owner is None and record.next_action is None for record in reading.native_population.records)
    assert all(value is None for value in reading.statement_publication.committed_dates.values())
    assert "2026-11-01" not in {str(record.committed_date) for record in reading.native_population.records}
    assert session.scalar(select(DeltaDisposition.id).where(DeltaDisposition.delta_id == delta.id)) is None
    report = build_report(session, project.id, frozen_reading=reading)
    body = render(report)
    assert plan.open_question in body and "local:utility-coordinator" in body
    assert f"Follow-up Plan {plan.plan_id}" in body
    briefing = brief_project(session, project.id, client=CoveringClient(), frozen_reading=reading)
    assert not briefing.refused
    assert any(item.kind == "decision" and plan.open_question in item.text for item in briefing.citables)
    stored = snapshot(session, project.id, evaluation=reading.evaluation)
    assert stored["follow_up_plans"][0]["support_assessment_ids"] == [assessment_id]
    assert stored["follow_up_plans"][0]["source_segment_ids"] == list(source_ids)
    output = to_xlsx(session, project.id, tmp_path / "follow-up.xlsx", evaluation=reading.evaluation,
        statement_publication=reading.statement_publication, frozen_reading=reading)
    book = load_workbook(output, data_only=True)
    try:
        assert book["Follow-up Plans"]["C2"].value == plan.open_question
        assert str(assessment_id) == book["Follow-up Plans"]["I2"].value
        assert book.active["I2"].value is None  # source date is not statement-projected Promised For
    finally:
        book.close()
    before = freeze_project_reading(session, project.id, today=TODAY, revision_id=adoption.revision_id)
    assert before.native_population.follow_up_plans == ()


def test_report_command_persists_the_exact_rendered_native_reading(runtime_database, tmp_path, monkeypatch):
    import corridor.db as database_module
    import corridor.export as export_module
    import corridor.project_reading as reading_module
    import corridor.report as report_module
    from corridor.models import ReportRun
    from corridor.report_reading import digest_is_intact
    database_engine = runtime_database.session_factory.kw["bind"]
    with runtime_database.session_factory() as setup:
        project, adoption = _adopt_native_workbook(setup, tmp_path, monkeypatch)
        project_id, slug, revision_id = project.id, project.slug, adoption.revision_id
        setup.commit()
    monkeypatch.setattr(database_module, "WorkerSession", runtime_database.session_factory)
    monkeypatch.chdir(tmp_path)
    def pdf_sink(body, path):
        Path(path).write_bytes(b"%PDF-synthetic-sink")
        return Path(path)
    monkeypatch.setattr(export_module, "to_pdf", pdf_sink)
    original = reading_module.read_accepted_field_population
    populations = []
    def captured(*args, **kwargs):
        value = original(*args, **kwargs)
        populations.append(value)
        return value
    monkeypatch.setattr(reading_module, "read_accepted_field_population", captured)
    forbidden = re.compile(r"\b(?:dependencies|candidates|assertions|evidence_links|work_decisions|operative_support)\b", re.I)
    def inspect_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            assert not forbidden.search(statement), statement
    event.listen(database_engine, "before_cursor_execute", inspect_sql)
    try:
        assert report_module.main([slug]) == 0
        assert len(populations) == 1
        assert report_module.main([slug]) == 0
        assert len(populations) == 2  # once per command, never a second legacy reading for the snapshot
    finally:
        event.remove(database_engine, "before_cursor_execute", inspect_sql)
    assert "100+00" in (tmp_path / "out/report.html").read_text()
    with runtime_database.session_factory() as verify:
        runs = tuple(verify.scalars(select(ReportRun).where(ReportRun.project_id == project_id).order_by(ReportRun.id)))
        assert len(runs) == 2 and runs[1].id > runs[0].id
        for run, population in zip(runs, populations):
            assert run.revision_id == revision_id == population.revision_id
            assert run.snapshot_json["native_population_sha256"] == population.fingerprint
            assert digest_is_intact(run.snapshot_json) is True
            records = run.snapshot_json["dependencies"]
            assert set(records) == set(population.record_ids)
            assert len(records) == 2
            for record in population.records:
                entry = records[record.ref_code]
                assert entry["published_promised_for"] is None
                assert entry["accepted_field_values"]["committed_date"] == record.committed_date.isoformat()
                assert entry["accepted_field_decisions"]["station_from"]["decision_id"] == record.fields["station_from"].decision_id


def test_historical_native_report_uses_no_future_revision_as_its_predecessor(session, adopted):
    from corridor.changes import record_run
    project, adoption = adopted
    original = freeze_project_reading(session, project.id, today=TODAY)
    first = record_run(session, project.id, evaluation=original.evaluation)
    field = original.native_population.records[0].fields["station_from"]
    record_human_fact_decision(session, session.get(Fact, field.fact_id), principal=PRINCIPAL,
        command_type="resolve_discrepancy", idempotency_key="future-station-removal",
        expected_predecessor=field.decision_id, disposition="do_not_add")
    later = freeze_project_reading(session, project.id, today=TODAY)
    future = record_run(session, project.id, evaluation=later.evaluation)
    assert future.id > first.id and future.revision_id > adoption.revision_id
    historical = freeze_project_reading(session, project.id, today=TODAY, revision_id=adoption.revision_id)
    report = build_report(session, project.id, today=TODAY, frozen_reading=historical)
    assert report.diff.previous_run_id == first.id
    assert report.diff.previous_run_id != future.id


def test_native_projection_keeps_record_identity_apart_from_original_fact_subject(session, adopted):
    from corridor.record_projection import read_native_record_values
    project, adoption = adopted
    source_rows = tuple(session.scalars(select(BaselineSourceRow).where(
        BaselineSourceRow.project_id == project.id).order_by(BaselineSourceRow.id)))
    values = read_native_record_values(session, project.id, adoption.revision_id)
    population = read_accepted_field_population(session, project.id)
    # The real Adopt Baseline command preserves duplicate business identifiers
    # as separate record subjects. The Fact/FactDecision source binding remains
    # unchanged; only the explicit adoption map resolves the record identity.
    assert len({row.record_subject_key for row in source_rows}) == 2
    for source_row in source_rows:
        assert source_row.record_subject_key != source_row.source_row_key
        record = next(item for item in population.records if item.subject_key == source_row.record_subject_key)
        field = record.fields["station_from"]
        selected = next(value for value in values if value.decision_id == field.decision_id)
        fact = session.get(Fact, field.fact_id)
        assert selected.fact_subject_key == fact.subject_key == source_row.source_row_key
        assert field.fact_subject_key == source_row.source_row_key
        assert record.subject_key != field.fact_subject_key


def _captured_workbook_field(session, project, tmp_path, *, subject, value, fact_type="station_from", cell="D3", rows=None):
    from corridor.extraction_runs import record_extraction_run
    from corridor.extractor_lineage import deployed_extractor_config, zero_token_usage
    from corridor.models import Document
    from corridor.object_storage import store_bytes
    from corridor.source_segments import append_ingested_source_segments
    from corridor.source_append import ClosureValues
    from corridor.materializer import materialize_typed_satellite
    from corridor.support_assessments import FactProposition, record_support_assessment
    path = tmp_path / f"incoming-{uuid4().hex[:8]}.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) — Utility Conflicts"])
    sheet.append(["Utility Conflict ID", "Utility Owner", "Utility Type", "Start Station", "End Station", "Resolution Status"])
    for row in rows or [["UC-3", "City Water", "Water", value, "301+00", ""]]:
        sheet.append(row)
    sheet[cell] = value
    book.save(path)
    body = path.read_bytes()
    stored = store_bytes(body, sha256=sha256(body).hexdigest(), suffix=".xlsx")
    document = Document(project_id=project.id, sha256=sha256(body).hexdigest(), filename=path.name, doc_type="matrix")
    session.add(document)
    session.flush()
    segments = append_ingested_source_segments(session, document, stored)
    segment = next(item for item in segments if item.sheet_name == sheet.title and item.cell_range == cell)
    config = deployed_extractor_config("baseline", client=None)
    run = record_extraction_run(session, document, prompt_version=config.prompt_version, schema_version=config.schema_version,
        candidate_count=0, page_errors=0, outcome="completed", model=None, extractor_config=config,
        token_usage=zero_token_usage(document.id), row_accounting_json=None)
    kwargs = {}
    if fact_type == "closure_result":
        materialized = materialize_typed_satellite(fact_type, segment)
        kwargs["closure"] = ClosureValues("constraint_closed", None, (segment.id,))
    else:
        materialized = materialize_segment_value(session, fact_type, segment)
    fact = append_fact(session, project_id=project.id, document_id=document.id, extraction_run_id=run.id,
        subject_kind="source_row", subject_key=subject, recorded_by="extractor:native-reader-test",
        content_sha256=sha256(f"{document.sha256}:{subject}:{fact_type}:{cell}".encode()).hexdigest(), value=materialized, **kwargs)
    support = record_support_assessment(session, project_id=project.id, proposition=FactProposition(fact.id),
        source_segment_ids=(segment.id,), evidence_role="value_support", assessment="supported",
        authority=PRINCIPAL, assessed_at=datetime.now(timezone.utc))
    return fact, segment, support


def _resolve_native_test_delta(session, project, fact, support, *, change="modify", target=None, observed=None):
    from corridor.delta_resolution import ACCEPT, ChildDecisionRequest, RecordEffect, RESOLVED, resolve_delta
    from corridor.proposed_deltas import ExistingSubjectTarget, ProposedSubjectTarget, ProposedDeltaValues, create_proposed_delta_group
    if target is None:
        target = (ProposedSubjectTarget(fact.subject_key, (fact.fact_type,)) if change == "add" else
                  ExistingSubjectTarget(fact.subject_key, fact.fact_type))
    (delta,) = create_proposed_delta_group(session, project_id=project.id, source_family="ucm-workbook",
        source_revision=f"native-source-{uuid4().hex[:8]}", document_id=fact.document_id,
        deltas=(ProposedDeltaValues(change, target, proposed_value=None if change == "apparent_removal" else fact.text_value,
            accepted_baseline_revision=f"revision:{observed}" if observed else None),),
        is_complete_enumerative_source=change == "apparent_removal", row_accounting_sealed=change == "apparent_removal")
    outcome = resolve_delta(session, ChildDecisionRequest(project_id=project.id, delta_id=delta.id,
        action=ACCEPT, principal=PRINCIPAL, idempotency_key=f"native-resolve:{delta.id}", decided_at=datetime.now(timezone.utc),
        observed_accepted_revision_id=observed,
        record_effects=(RecordEffect(fact.id, "do_not_add" if change == "apparent_removal" else "include"),),
        support_assessment_ids=(support.id,)))
    assert outcome.status == RESOLVED, outcome
    return outcome


def test_resolve_delta_addition_removal_and_typed_closure_own_native_population(session, adopted, tmp_path):
    project, adoption = adopted
    new_fact, new_segment, new_support = _captured_workbook_field(session, project, tmp_path, subject="UC-3", value="300+00")
    added = _resolve_native_test_delta(session, project, new_fact, new_support, change="add", observed=adoption.revision_id)
    after_add = read_accepted_field_population(session, project.id)
    assert len(after_add.open_records) == 3
    added_record = next(record for record in after_add.records if record.subject_key == "UC-3")
    assert added_record.identity_decision_id == added.decision_id
    assert added_record.identity_revision_id == added.revision_id
    assert added_record.fields["station_from"].decision_id in added.fact_decision_ids
    assert added_record.fields["station_from"].sources[0].source_segment_id == new_segment.id
    assert len(read_accepted_field_population(session, project.id, revision_id=adoption.revision_id).open_records) == 2
    removed = _resolve_native_test_delta(session, project, new_fact, new_support, change="apparent_removal", observed=added.revision_id)
    after_removal = read_accepted_field_population(session, project.id)
    assert len(after_removal.open_records) == 2
    assert next(record for record in after_removal.records if record.subject_key == "UC-3").removal_decision_id == removed.decision_id
    assert session.get(Fact, new_fact.id) is not None
    assert len(read_accepted_field_population(session, project.id, revision_id=added.revision_id).open_records) == 3
    baseline_record = after_removal.open_records[0]
    closure, _, closure_support = _captured_workbook_field(session, project, tmp_path,
        subject=baseline_record.subject_key, value="Conflict resolved in the field", fact_type="closure_result", cell="F3")
    closed = _resolve_native_test_delta(session, project, closure, closure_support, observed=removed.revision_id)
    final = read_accepted_field_population(session, project.id)
    assert len(final.open_records) == 1
    closed_record = next(record for record in final.records if record.subject_key == baseline_record.subject_key)
    assert closed_record.is_closed and closed_record.fields["closure_result"].decision_id in closed.fact_decision_ids
    assert len(read_accepted_field_population(session, project.id, revision_id=removed.revision_id).open_records) == 2


def test_resolve_delta_reordered_source_updates_only_its_explicit_canonical_target(session, adopted, tmp_path):
    from corridor.proposed_deltas import ExistingSubjectTarget
    project, adoption = adopted
    baseline_rows = tuple(session.scalars(select(BaselineSourceRow).where(BaselineSourceRow.project_id == project.id).order_by(BaselineSourceRow.row_number)))
    target = baseline_rows[1]
    fact, segment, support = _captured_workbook_field(session, project, tmp_path,
        subject=target.record_subject_key, value="222+00", cell="D3",
        rows=[["UC-1", "Electric Company", "Electric", "222+00", "223+00", ""],
              ["UC-1", "City Water", "Water", "111+00", "112+00", ""]])
    result = _resolve_native_test_delta(session, project, fact, support,
        target=ExistingSubjectTarget(target.record_subject_key, "station_from"), observed=adoption.revision_id)
    current = read_accepted_field_population(session, project.id)
    by_subject = {record.subject_key: record for record in current.records}
    assert by_subject[baseline_rows[0].record_subject_key].station_from == "100+00"
    changed = by_subject[target.record_subject_key]
    assert changed.station_from == "222+00"
    assert changed.fields["station_from"].decision_id in result.fact_decision_ids
    assert changed.fields["station_from"].fact_subject_key == fact.subject_key
    assert changed.fields["station_from"].sources[0].source_segment_id == segment.id
    assert changed.fields["station_from"].sources[0].locator.endswith("!D3")
    assert changed.source_row_key == target.source_row_key  # adopted row was !4; later source moved to !3
    before = read_accepted_field_population(session, project.id, revision_id=adoption.revision_id)
    assert next(record for record in before.records if record.subject_key == target.record_subject_key).station_from == "200+00"


def test_selected_key_date_authority_is_not_overridden_by_ucm_values(session, adopted, tmp_path):
    from corridor.milestones import import_csv
    project, _ = adopted
    schedule = tmp_path / "key-dates.csv"
    schedule.write_text("code,name,need_date\nK1,Utility work complete,2027-01-15\n")
    result = import_csv(session, project_id=project.id, path=schedule, actor=PRINCIPAL.subject)
    assert result.created
    with pytest.raises(NativeReadingRefused, match="key-date authority"):
        read_accepted_field_population(session, project.id)


def test_accepted_verbal_statement_is_separate_from_constraint_rows_and_respects_document_only(session, adopted, tmp_path):
    from corridor.facts import append_recorded_statement_wording_fact, append_recorded_statement_timing_fact, append_recorded_applies_to_fact
    from corridor.source_append import append_recorded_verbal_origin
    from corridor.source_segments import recorded_verbal_statement_segment, append_source_segment
    from corridor.statement_values import StatementTiming
    from corridor.report import RecordDecision
    from corridor.changes import snapshot
    project, _ = adopted
    words = "The utility expects work to finish around October."
    origin = append_recorded_verbal_origin(session, project_id=project.id, recorded_by="local:original-recorder",
        recorded_at=datetime.now(timezone.utc), conversation_date=TODAY, exact_text=words,
        content_sha256=sha256(words.encode()).hexdigest())
    segment = append_source_segment(session, recorded_verbal_statement_segment(project_id=project.id,
        recorded_verbal_origin_id=origin.id, exact_text=words))
    subject = "statement:mixed-native"
    wording = append_recorded_statement_wording_fact(session, segment=segment, subject_key=subject,
        description=words, recorded_by=origin.recorded_by)
    timing = append_recorded_statement_timing_fact(session, segment=segment, subject_key=subject,
        timings=(("new", StatementTiming.approximate("around October")),), recorded_by=origin.recorded_by)
    scope = append_recorded_applies_to_fact(session, segment=segment, subject_key=subject, dependency_ids=(), recorded_by=origin.recorded_by)
    for index, fact in enumerate((wording, timing, scope)):
        record_human_fact_decision(session, fact, principal=PRINCIPAL, command_type="coordinate_statement", idempotency_key=f"mixed-statement-{index}")
    reading = freeze_project_reading(session, project.id, today=TODAY)
    assert len(reading.rows) == 2 and len(reading.native_population.statements) == 1
    report = build_report(session, project.id, frozen_reading=reading)
    assert words in render(report) and "around October" in render(report)
    assert any(isinstance(cell.provenance, RecordDecision) for cell in report.cells)
    assert all(value is None for value in reading.statement_publication.committed_dates.values())
    stored = snapshot(session, project.id, evaluation=reading.evaluation)
    assert stored["accepted_statements"][0]["subject_key"] == subject
    assert stored["accepted_statements"][0]["fields"]["statement_wording"]["fact_id"] == wording.id
    documentary = freeze_project_reading(session, project.id, today=TODAY, document_only=True)
    assert words not in render(build_report(session, project.id, frozen_reading=documentary, document_only=True))
    assert snapshot(session, project.id, evaluation=documentary.evaluation)["accepted_statements"] == []


def test_native_manifests_retain_unchanged_customer_cells_and_exact_decisions(session, adopted, tmp_path):
    from corridor.accepted_field_reading import native_reader_input_manifest
    from corridor.workbook_render import render_project_record_workbook, workbook_reader_input_manifest
    project, adoption = adopted
    reading = freeze_project_reading(session, project.id, today=TODAY)
    manifest = native_reader_input_manifest(reading.native_population)
    assert manifest["revision_id"] == adoption.revision_id
    assert len(manifest["records"]) == 2
    for row in manifest["records"]:
        field = row["fields"]["station_from"]
        assert field["decision_kind"] == "fact_decision"
        assert session.get(FactDecision, field["decision_id"]).fact_id == field["fact_id"]
        assert field["actor"] == PRINCIPAL.subject
        assert field["fact_subject_key"] != row["subject_key"]
    rendered = render_project_record_workbook(session, project_id=project.id,
        revision_id=adoption.revision_id, template_bytes=(tmp_path / "ucm.xlsx").read_bytes())
    mapped = workbook_reader_input_manifest(rendered)
    assert not rendered.changed_cells
    assert mapped["mapped_cells"]
    station_cells = [cell for cell in mapped["mapped_cells"] if cell["fields"] == ["station_from"]]
    assert {cell["rendered_value"] for cell in station_cells} == {"100+00", "200+00"}
    for cell in station_cells:
        assert cell["accepted_fields"][0]["decision_id"]
        assert cell["record_subject_key"] != cell["source_row_key"]
    repeat = render_project_record_workbook(session, project_id=project.id,
        revision_id=adoption.revision_id, template_bytes=(tmp_path / "ucm.xlsx").read_bytes())
    assert repeat.content == rendered.content


def test_reversed_new_subject_has_explicit_lifecycle_without_becoming_closed(session, adopted, tmp_path):
    from corridor.changes import snapshot
    from corridor.delta_resolution import RecordEffect
    from corridor.proposed_deltas import ProposedSubjectTarget, ProposedDeltaValues, create_proposed_delta_group
    from corridor.review_packets import APPLY, SAVED, PacketChildRequest, ReviewPacketRequest, resolve_review_packet, reverse_review_packet
    project, adoption = adopted
    fact, _, support = _captured_workbook_field(session, project, tmp_path, subject="UC-new-reversed", value="300+00")
    source_revision = "source-for-reversed-addition"
    (delta,) = create_proposed_delta_group(session, project_id=project.id, source_family="ucm-workbook",
        source_revision=source_revision, document_id=fact.document_id,
        deltas=(ProposedDeltaValues("add", ProposedSubjectTarget(fact.subject_key, (fact.fact_type,)), proposed_value=fact.text_value),))
    saved = resolve_review_packet(session, ReviewPacketRequest(project_id=project.id,
        grouping_rule_version="packetizer-v1", grouping_key_kind="source_revision", grouping_key=source_revision,
        principal=PRINCIPAL, idempotency_key="native-reversed-addition", decided_at=datetime.now(timezone.utc),
        observed_accepted_revision_id=adoption.revision_id,
        children=(PacketChildRequest(delta_id=delta.id, outcome=APPLY, observed_source_revision=source_revision,
            record_effects=(RecordEffect(fact.id, "include"),), support_assessment_ids=(support.id,)),)))
    assert saved.status == SAVED, saved
    assert len(read_accepted_field_population(session, project.id).open_records) == 3
    undone = reverse_review_packet(session, project_id=project.id, receipt_id=saved.receipt_id, principal=PRINCIPAL,
        reversed_at=datetime.now(timezone.utc), idempotency_key="reverse-native-addition")
    assert undone.status == "reversed", undone
    reading = freeze_project_reading(session, project.id, today=TODAY)
    assert len(reading.rows) == 2
    assert reading.native_population.withdrawn_subjects == ((fact.subject_key, undone.reversal_id),)
    stored = snapshot(session, project.id, evaluation=reading.evaluation)
    assert stored["inactive_record_decisions"][fact.subject_key] == {
        "state": "reversed", "decision_kind": "delta_review_packet_reversal", "decision_id": undone.reversal_id}
    assert len(read_accepted_field_population(session, project.id, revision_id=saved.revision_id).open_records) == 3


def test_semantic_coverage_owns_equivalence_except_declared_unchanged_surfaces():
    from corridor.reader_coverage import CONTRACTS, CoverageResult
    from corridor.reader_equivalence import ReaderEquivalence
    coverage = tuple(CoverageResult(contract.name, True, 0, ()) for contract in CONTRACTS)
    result = ReaderEquivalence(False, False, True, True, (), coverage)
    assert result.passed
    assert not result.rendered_outputs_identical
    failed = tuple(CoverageResult(contract.name, contract.name != "workbook_export", 0,
        ("unchanged-output identity differs",) if contract.name == "workbook_export" else ()) for contract in CONTRACTS)
    assert not ReaderEquivalence(True, True, False, True, (), failed).passed
