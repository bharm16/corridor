"""Public reader provenance cannot borrow identities, actors, dates or derivation inputs."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from corridor.fact_decisions import record_human_fact_decision
from corridor.facts import append_recorded_statement_wording_fact
from corridor.native_reader_coverage import collect_native_reader_coverage
from corridor.source_append import append_recorded_verbal_origin
from corridor.source_segments import append_source_segment, recorded_verbal_statement_segment
from test_native_reader_coverage import adopted
from test_native_accepted_readers import PRINCIPAL, TODAY, _follow_up_plan


def _collect(session, project, adoption):
    return collect_native_reader_coverage(session, project.id, as_of_revision_id=adoption.revision_id,
        evaluated_at=datetime.combine(TODAY, datetime.min.time(), timezone.utc))


def _report_blockers(result):
    return next(surface.blockers for surface in result.surfaces if surface.reading.surface == "coordination_report")


def _accept_words(session, project):
    words = "The utility expects completion around October."
    origin = append_recorded_verbal_origin(session, project_id=project.id,
        recorded_by="local:original-recorder", recorded_at=datetime.now(timezone.utc),
        conversation_date=TODAY, exact_text=words, content_sha256=sha256(words.encode()).hexdigest())
    segment = append_source_segment(session, recorded_verbal_statement_segment(project_id=project.id,
        recorded_verbal_origin_id=origin.id, exact_text=words))
    fact = append_recorded_statement_wording_fact(session, segment=segment, subject_key="statement:citation-test",
        description=words, recorded_by=origin.recorded_by)
    record_human_fact_decision(session, fact, principal=PRINCIPAL, command_type="coordinate_statement",
        idempotency_key="citation-wording")


@pytest.mark.parametrize("change", ["actor", "time", "source_refs"])
def test_record_decision_display_requires_its_exact_native_attribution(session, adopted, monkeypatch, change):
    import corridor.report as reports
    project, adoption = adopted
    _accept_words(session, project)
    actual = reports.build_report
    def altered(*args, **kwargs):
        report = actual(*args, **kwargs)
        cell = next(cell for cell in report.cells if isinstance(cell.provenance, reports.RecordDecision))
        provenance = cell.provenance
        updates = {"actor": "local:someone-else"} if change == "actor" else (
            {"decided_at": provenance.decided_at - timedelta(days=1)} if change == "time" else
            {"source_refs": ("source_segment:999999",)})
        cell.provenance = replace(provenance, **updates)
        return report
    monkeypatch.setattr(reports, "build_report", altered)
    result = _collect(session, project, adoption)
    assert any("RecordDecision attribution or source references" in blocker for blocker in _report_blockers(result))


@pytest.mark.parametrize("change", ["actor", "time", "empty_ids"])
def test_follow_up_provenance_requires_native_plan_ids_actor_and_date(session, adopted, monkeypatch, change):
    import corridor.report as reports
    project, adoption = adopted
    _follow_up_plan(session, project, adoption)
    actual = reports.build_report
    def altered(*args, **kwargs):
        report = actual(*args, **kwargs)
        cell = next(cell for cell in report.cells if isinstance(cell.provenance, reports.WorkDecision))
        provenance = cell.provenance
        updates = {"recorded_by": "local:someone-else"} if change == "actor" else (
            {"recorded_at": provenance.recorded_at - timedelta(days=1)} if change == "time" else
            {"decision_ids": ()})
        cell.provenance = replace(provenance, **updates)
        return report
    monkeypatch.setattr(reports, "build_report", altered)
    result = _collect(session, project, adoption)
    assert any("Follow-up Plan identities or attribution" in blocker for blocker in _report_blockers(result))


@pytest.mark.parametrize("change", ["foreign_record", "missing_input", "foreign_ref", "scope", "ruleset"])
def test_derivation_is_bound_to_exact_verified_inputs_and_evaluation(session, adopted, monkeypatch, change):
    import corridor.report as reports
    project, adoption = adopted
    actual = reports.build_report
    def altered(*args, **kwargs):
        report = actual(*args, **kwargs)
        cell = report.summary[0]
        provenance = cell.provenance
        assert isinstance(provenance, reports.Derivation)
        updates = {
            "foreign_record": {"record_ids": ("invented-subject",)},
            "missing_input": {"record_ids": provenance.record_ids[:1]},
            "foreign_ref": {"input_refs": ("revision:999999",)},
            "scope": {"scope": "an invented comparison scope"},
            "ruleset": {"ruleset_version": "unreleased-rules"},
        }[change]
        cell.provenance = replace(provenance, **updates)
        return report
    monkeypatch.setattr(reports, "build_report", altered)
    result = _collect(session, project, adoption)
    assert any("derivation lacks its exact verified input identities" in blocker for blocker in _report_blockers(result))


def test_unchanged_native_citations_pass_their_lineage_checks(session, adopted):
    project, adoption = adopted
    _follow_up_plan(session, project, adoption)
    _accept_words(session, project)
    result = _collect(session, project, adoption)
    assert not [blocker for blocker in _report_blockers(result) if blocker.startswith("citations:")]


def test_title_cites_typed_fact_decision_origins_and_rejects_another_namespace(session, adopted, monkeypatch):
    import corridor.report as reports
    project, adoption = adopted
    actual = reports.build_report
    def altered(*args, **kwargs):
        report = actual(*args, **kwargs)
        cell = next(cell for cell in report.cells if cell.label == "Title")
        provenance = cell.provenance
        assert provenance.input_refs
        assert all(reference.startswith("native_decision:fact_decision:") for reference in provenance.input_refs)
        cell.provenance = replace(provenance, input_refs=tuple(
            reference.replace("native_decision:fact_decision:", "native_decision:coordination_record_decision:")
            for reference in provenance.input_refs))
        return report
    monkeypatch.setattr(reports, "build_report", altered)
    result = _collect(session, project, adoption)
    assert any("'Title' derivation lacks its exact verified input identities" in blocker for blocker in _report_blockers(result))
