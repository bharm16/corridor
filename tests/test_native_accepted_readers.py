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
    with pytest.raises(NativeReadingRefused, match="no declared adopted-row mapping"):
        read_accepted_field_population(session, project.id)


def test_ambiguous_effective_source_and_record_alias_decisions_refuse_instead_of_last_wins(session, adopted):
    project, _ = adopted
    row = session.scalar(select(BaselineSourceRow).where(BaselineSourceRow.project_id == project.id).order_by(BaselineSourceRow.id))
    assert row.source_row_key != row.record_subject_key
    fact = _unselected_fact(session, project, subject_key=row.record_subject_key)
    record_human_fact_decision(session, fact, principal=PRINCIPAL, command_type="resolve_discrepancy", idempotency_key="ambiguous-alias")
    with pytest.raises(NativeReadingRefused, match="more than one accepted decision"):
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
