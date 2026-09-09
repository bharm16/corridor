"""Retained report metadata must match authority, without changing published bytes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.fact_decisions import record_human_fact_decision
from corridor.models import Fact, FactDecision
from corridor.native_reader_coverage import _Surface, _inventory, _releases
from corridor.reader_coverage import CONTRACTS
from corridor.report import build_report
from corridor.report_release import RenderedExternalReport, prepare_external_report, release_external_report
from test_native_accepted_readers import _adopt_native_workbook, PRINCIPAL, TODAY

PDF = b"%PDF-1.7\nretained native report fixture\n%%EOF"
NOW = datetime.combine(TODAY, datetime.min.time(), timezone.utc)


class _ReadbackOverride:
    """Retain the real public artifact contract while corrupting one readback."""

    def __init__(self, original, **changes):
        self._original = original
        self._changes = changes

    def __getattr__(self, name):
        if name in self._changes:
            return self._changes[name]
        return getattr(self._original, name)


@pytest.fixture
def session():
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection) as session:
            yield session
        if transaction.is_active:
            transaction.rollback()


@pytest.fixture
def prepared(session, tmp_path, monkeypatch):
    project, adoption = _adopt_native_workbook(session, tmp_path, monkeypatch)
    report = build_report(session, project.id, today=TODAY)
    artifact = prepare_external_report(session, project_id=project.id,
        rendered=RenderedExternalReport("native-coverage.pdf", PDF, report))
    return project, adoption, artifact


def _collect_release(session, project, revision):
    surface = _Surface(next(contract for contract in CONTRACTS if contract.name == "report_release"))
    _releases(surface, session, project.id, revision, NOW, _inventory(session, project.id, 10000))
    return surface.result()


def test_retained_manifest_checks_complete_fields_parent_identity_and_source_edges(session, prepared, monkeypatch):
    import corridor.report_release as release

    project, adoption, artifact = prepared
    original_context = deepcopy(artifact.record_context_json)
    actual = release.retrieve_prepared_external_report
    good = _collect_release(session, project, adoption.revision_id)
    assert good.blockers == ()
    assert "release" in good.reading.observed_record_kinds

    # Each alteration keeps the exact decision/fact IDs and overall decision
    # population intact, which was sufficient for the old collector to pass.
    for defect in ("value", "fact_subject", "actor", "time", "source_quote", "source_edge", "parent_subject"):
        forged = deepcopy(original_context)
        record = forged["native_reader_input_manifest"]["records"][0]
        field = record["fields"]["station_from"]
        if defect == "value":
            field["value"] = "999+00"
        elif defect == "fact_subject":
            field["fact_subject_key"] = "another-source-row"
        elif defect == "actor":
            field["actor"] = "local:invented-publisher"
        elif defect == "time":
            field["decided_at"] = "1999-01-01T00:00:00+00:00"
        elif defect == "source_quote":
            field["sources"][0]["quote"] = "invented retained quotation"
        elif defect == "source_edge":
            other = record["fields"]["station_to"]["sources"][0]
            field["sources"] = [deepcopy(other)]
        else:
            record["subject_key"] = "another-native-parent"
        monkeypatch.setattr(release, "retrieve_prepared_external_report", lambda *a, context=forged, **kw:
            _ReadbackOverride(artifact, record_context_json=context))
        observed = _collect_release(session, project, adoption.revision_id)
        assert any("exact as-of" in blocker for blocker in observed.blockers), (defect, observed.blockers)
        assert "release" not in observed.reading.observed_record_kinds
        assert observed.reader_outputs[f"external_report_artifacts:{artifact.id}"]["context"] == forged
        assert actual(session, project.id, artifact.id).record_context_json == original_context
        assert actual(session, project.id, artifact.id).pdf_bytes == PDF


def test_retained_report_uses_its_own_revision_after_later_accepted_change(session, prepared):
    project, adoption, artifact = prepared
    original_context = deepcopy(artifact.record_context_json)
    decision = session.scalar(select(FactDecision).where(FactDecision.project_id == project.id,
        FactDecision.fact_type == "station_from").order_by(FactDecision.id))
    changed = record_human_fact_decision(session, session.get(Fact, decision.fact_id), principal=PRINCIPAL,
        command_type="resolve_discrepancy", idempotency_key="after-retained-release-context",
        expected_predecessor=decision.id, disposition="do_not_add")
    observed = _collect_release(session, project, changed.revision.id)
    assert observed.blockers == ()
    assert observed.reader_outputs[f"external_report_artifacts:{artifact.id}"]["context"] == original_context
    assert artifact.pdf_bytes == PDF


def test_release_readback_actor_and_time_must_match_inventory_authority(session, prepared, monkeypatch):
    import corridor.report_release as release

    project, adoption, artifact = prepared
    receipt = release_external_report(session, project_id=project.id, artifact_id=artifact.id, principal=PRINCIPAL)
    original = release.retrieve_released_external_report
    assert _collect_release(session, project, adoption.revision_id).blockers == ()
    for defect in ("actor", "time"):
        monkeypatch.setattr(release, "retrieve_released_external_report", lambda *a, defect=defect, **kw:
            _ReadbackOverride(receipt, released_by="local:invented-releaser" if defect == "actor" else receipt.released_by,
                released_at=receipt.released_at + timedelta(days=1) if defect == "time" else receipt.released_at))
        observed = _collect_release(session, project, adoption.revision_id)
        assert any("approval actor/time differs" in blocker for blocker in observed.blockers)
        assert "release" not in observed.reading.observed_record_kinds
        stored = original(session, project.id, receipt.id)
        assert stored.released_by == PRINCIPAL.subject
        assert stored.pdf_bytes == PDF


def test_statement_bearing_retained_release_preserves_typed_sources_and_applies_to(session, tmp_path, monkeypatch):
    from hashlib import sha256

    from corridor.facts import append_recorded_applies_to_fact, append_recorded_statement_timing_fact, append_recorded_statement_wording_fact
    from corridor.report_release import render_external_report_pdf
    from corridor.source_append import append_recorded_verbal_origin
    from corridor.source_segments import append_source_segment, recorded_verbal_statement_segment
    from corridor.statement_values import StatementTiming

    project, _adoption = _adopt_native_workbook(session, tmp_path, monkeypatch)
    words = "The utility expects work to finish around October."
    origin = append_recorded_verbal_origin(session, project_id=project.id, recorded_by="local:original-recorder",
        recorded_at=datetime.now(timezone.utc), conversation_date=TODAY, exact_text=words,
        content_sha256=sha256(words.encode()).hexdigest())
    segment = append_source_segment(session, recorded_verbal_statement_segment(project_id=project.id,
        recorded_verbal_origin_id=origin.id, exact_text=words))
    subject = "statement:retained-native-release"
    wording = append_recorded_statement_wording_fact(session, segment=segment, subject_key=subject,
        description=words, recorded_by=origin.recorded_by)
    timing = append_recorded_statement_timing_fact(session, segment=segment, subject_key=subject,
        timings=(("new", StatementTiming.approximate("around October")),), recorded_by=origin.recorded_by)
    scope = append_recorded_applies_to_fact(session, segment=segment, subject_key=subject,
        dependency_ids=(), recorded_by=origin.recorded_by)
    for index, fact in enumerate((wording, timing, scope)):
        decision = record_human_fact_decision(session, fact, principal=PRINCIPAL,
            command_type="coordinate_statement", idempotency_key=f"retained-statement-{index}")
    rendered = render_external_report_pdf(session, project.id, today=TODAY)
    artifact = prepare_external_report(session, project_id=project.id, rendered=rendered)
    receipt = release_external_report(session, project_id=project.id, artifact_id=artifact.id, principal=PRINCIPAL)
    published = deepcopy(artifact.record_context_json)
    fields = published["native_reader_input_manifest"]["statements"][0]["fields"]
    assert fields["applies_to"]["value"] == {"mode": "unknown", "subject_keys": [], "legacy_dependency_ids": []}
    assert {source["role"] for source in fields["statement_wording"]["sources"]} == {"value_source", "attribution_source"}
    observed = _collect_release(session, project, decision.revision.id)
    assert observed.blockers == ()
    assert "release" in observed.reading.observed_record_kinds
    assert observed.reader_outputs[f"external_report_releases:{receipt.id}"]["context"] == published
    assert artifact.pdf_bytes == receipt.pdf_bytes == rendered.pdf_bytes
