"""The public measurement reader keeps unlike units apart (#532)."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from corridor.analytics import AnalyticsEvent, EventFamily, default_binding
from corridor.pilot_measurement import MeasurementPeriod, derive_measurement


START = datetime(2026, 9, 7, tzinfo=timezone.utc)
BINDING = default_binding(
    code_revision="git:fixture-532", product_revision="pilot-v1",
    packetizer_rules_version="delta-partition-v2",
    source_configuration={"classes": ["ucm"], "cadence": "daily"},
    connector_configuration={"identity": "fixture-folder", "version": "1"},
    template_identity="fixture-template:1", mapping_identity="fixture-mapping:1",
    customer_id="fixture-customer", environment="fixture", database_identity="fixture:db",
)


def period(**changes):
    fields = dict(
        period_id="fixture-week-1", project_id=1, partner_id="fixture-partner",
        customer_id="fixture-customer", environment="fixture", database_identity="fixture:db",
        start=START, end=START + timedelta(days=7), declared_at=START,
        binding=BINDING, issue_profile_identity="fixture-issue",
        issue_profile_version=1, issue_profile_sha256="a" * 64,
        required_artifacts=("updated_ucm", "weekly_report"),
        previously_performed_artifacts=("updated_ucm",),
        quiet_max=0, ordinary_max=10, source_population_complete=True,
        interaction_capture_complete=True, retention_policy="fixture-only",
        access_policy="fixture-reviewers",
    )
    fields.update(changes)
    return MeasurementPeriod(**fields)


def event(family, *, hour=1, event_id=None, **payload):
    return AnalyticsEvent(
        family=family, event_id=event_id or f"{family.value}:{hour}:{payload}",
        occurred_at=START + timedelta(hours=hour), binding=BINDING,
        payload={"project_id": 1, "issue_profile_identity": "fixture-issue",
                 "issue_profile_version": 1, "issue_profile_sha256": "a" * 64,
                 **payload},
    )


def surface(key, children, **kwargs):
    return event(EventFamily.PACKET_SURFACING, item_key=key,
                 child_consequences=[{"delta_id": i} for i in children],
                 child_count=len(children), consequence_level="must_handle_before_issue",
                 **kwargs)


def test_one_surfaced_packet_with_ten_children_is_one_interruption_and_ten_diagnostics():
    shown = surface("packet-a", list(range(1, 11)))
    result = derive_measurement([period()], [shown, shown])
    report = result["periods"][0]
    assert report["packet_denominator"] == 1
    assert report["interrupting_packet_denominator"] == 1
    assert report["child_diagnostic_denominator"] == 10
    assert len(report["packets"]) == 1
    assert len(report["children"]) == 10
    assert report["packets"][0]["outcomes"] == ["ignored/open"]
    assert report["packets"][0]["opened"] is False
    assert report["time"]["coordinator_review"]["minutes"] is None
    assert report["time"]["coordinator_review"]["status"] == "unavailable"
    assert result["customer_findings"] == "insufficient_evidence"


def test_period_close_retains_unopened_deferred_superseded_and_each_child_outcome():
    events = [surface("packet-a", list(range(1, 11))),
              surface("packet-b", [11], hour=2),
              surface("packet-c", [12], hour=3)]
    for delta_id, action in [(1, "apply"), (2, "edit_and_apply"),
                             (3, "keep_current"), (4, "needs_coordination"), (5, "defer")]:
        events.append(event(EventFamily.CHILD_DECISION, hour=4, delta_id=delta_id,
                            action=action, outcome="saved", receipt_id=delta_id))
    events += [event(EventFamily.DELTA_SUPERSESSION, hour=5, prior_delta_id=6,
                     superseding_delta_id=13),
               event(EventFamily.DELTA_SUPERSESSION, hour=5, prior_delta_id=12,
                     superseding_delta_id=14),
               event(EventFamily.CHILD_DECISION, hour=170, delta_id=11,
                     action="apply", outcome="saved", receipt_id=99),
               event(EventFamily.CHILD_DECISION, hour=6, delta_id=7,
                     action="apply", outcome="refused"),
               surface("packet-a", list(range(1, 11)), hour=7)]
    report = derive_measurement([period()], events)["periods"][0]
    assert report["packet_denominator"] == 3
    assert report["child_diagnostic_denominator"] == 12
    outcomes = {c["delta_id"]: c["outcome"] for c in report["children"]}
    assert outcomes == {1: "apply", 2: "edit", 3: "keep current", 4: "needs coordination",
                        5: "deferred", 6: "superseded", 7: "ignored/open", 8: "ignored/open",
                        9: "ignored/open", 10: "ignored/open", 11: "ignored/open", 12: "superseded"}
    assert report["packets"][2]["outcomes"] == ["superseded"]
    assert report["interaction_counts"]["packet_presentations"] == 4


def test_report_joins_real_instants_configured_artifacts_and_attributable_measured_inputs():
    events = [
        event(EventFamily.SOURCE_ARRIVAL, hour=0, source_identity="delivery:1", source_class="ucm"),
        event(EventFamily.SOURCE_CAPTURE, hour=1, source_identity="delivery:1", source_class="ucm"),
        event(EventFamily.PROPOSED_DELTA_CREATION, hour=2, delta_id=1,
              source_identity="delivery:1", source_class="ucm", target_field="promised_for"),
        surface("packet-a", [1], hour=3),
        event(EventFamily.PACKET_OPENING, hour=4, item_key="packet-a"),
        event(EventFamily.CHILD_DECISION, hour=5, delta_id=1, action="apply", outcome="saved",
              revision_id=42, receipt_id=1),
        event(EventFamily.PREPARATION_REQUEST, hour=6, request_id=1, coverage_declaration_id=7,
              annotation_count=1, unchanged_declaration_reused=False),
        event(EventFamily.PREPARATION_ATTEMPT, hour=8, request_id=1, attempt_id=2,
              started_at=(START + timedelta(hours=7)).isoformat(), outcome="prepared", candidate_id=3),
        event(EventFamily.RELEASE_CANDIDATE_PREPARATION, hour=8, candidate_id=3,
              candidate_identity="candidate:3", accepted_revision_id=42, readiness="ready",
              coverage_declaration_id=7, configured_artifact_types=["updated_ucm", "weekly_report"],
              artifacts=[{"artifact_type": "updated_ucm", "content_sha256": "b" * 64},
                         {"artifact_type": "weekly_report", "content_sha256": "c" * 64}]),
        event(EventFamily.RELEASE_AUTHORIZATION, hour=10, candidate_id=3,
              candidate_identity="candidate:3", accepted_revision_id=42,
              package_identity="package:1", status="authorized"),
        event(EventFamily.WORK_OBSERVATION, hour=11, category="coordinator_review",
              minutes=12, actor="human:coordinator", evidence_reference="time-log:1"),
        event(EventFamily.WORK_OBSERVATION, hour=11, category="operations_triage",
              minutes=5, actor="human:operator", evidence_reference="time-log:2"),
        event(EventFamily.WORK_OBSERVATION, hour=11, category="manual_reconstruction",
              minutes=None, unavailable_reason="not recorded", actor="human:coordinator",
              evidence_reference="time-log:3", item_key="packet-a"),
        event(EventFamily.MEASUREMENT_SAMPLE, hour=11, sample_kind="release_readiness",
              candidate_id=3, client_ready=True, manual_repair_required=False,
              actor="human:coordinator", evidence_reference="review:1"),
    ]
    for index, (mode, amount) in enumerate([
        ("actual_call", "0.25"), ("retry", "0.05"), ("cache_reuse", "0.00"),
        ("historical_experiment", "99.00"),
    ]):
        events.append(event(EventFamily.PROVIDER_USAGE, hour=12, usage_id=str(index),
                            mode=mode, actual_cost_usd=amount, purpose="production",
                            source_class="ucm", provider="fixture", model="model-1",
                            prompt_version="prompt-1", policy_version="policy-1",
                            evidence_reference=f"bill:{index}"))
    report = derive_measurement([period()], events)["periods"][0]
    assert report["time"]["coordinator_review"]["minutes"] == 12
    assert report["time"]["operations_triage"]["minutes"] == 5
    assert report["time"]["manual_reconstruction"]["minutes"] is None
    assert report["volume_stratum"] == "ordinary"
    assert report["coverage"][0]["captured_within_window"] == 1
    assert report["children"][0]["latency_seconds"] == {
        "arrival_to_capture": 3600, "capture_to_delta": 3600,
        "delta_to_surfacing": 3600, "surfacing_to_decision": 7200,
        "decision_to_authorized_issue": 18000, "arrival_to_authorized_issue": 36000,
    }
    assert report["preparations"][0]["request_to_candidate_seconds"] == 7200
    assert report["preparations"][0]["annotation_count"] == 1
    assert report["releases"][0]["required_artifacts"] == ["updated_ucm", "weekly_report"]
    assert report["releases"][0]["client_ready"] is True
    assert report["releases"][0]["savings_eligible_artifacts"] == ["updated_ucm"]
    assert report["provider_cost"]["actual_cost_usd"] == "0.30"
    assert report["provider_cost"]["historical_experiment_cost_usd"] == "99.00"
    assert report["provider_cost"]["counts"] == {"actual_call": 1, "retry": 1, "cache_reuse": 1,
                                                   "historical_experiment": 1}
    assert all(row["project_id"] == 1 and row["partner_id"] == "fixture-partner"
               and row["week"] == "2026-W37" for row in report["observations"])


def test_cohort_change_requires_a_new_predeclared_period_and_missing_time_is_not_zero():
    changed = replace(surface("packet-a", [1]),
                      binding=replace(BINDING, packetizer_rules_version="delta-partition-v3"))
    with pytest.raises(ValueError, match="cohort change"):
        derive_measurement([period()], [changed])
    with pytest.raises(ValueError, match="before measurement"):
        period(declared_at=START + timedelta(days=1))
    report = derive_measurement([period(source_population_complete=False,
                                        interaction_capture_complete=False)], [])
    assert report["periods"][0]["volume_stratum"] == "unavailable"
    assert report["periods"][0]["time"]["operations_setup"]["minutes"] is None


def test_quiet_zero_click_requires_presentation_and_complete_observation_period():
    presented = portfolio_event(context=portfolio_context())
    report = derive_measurement([period()], [presented])["periods"][0]
    assert report["portfolio"]["zero_click"] is True
    assert report["volume_stratum"] == "quiet"
    assert derive_measurement([period()], [])["periods"][0]["portfolio"]["zero_click"] is None
    selected = event(EventFamily.PROJECT_OPENING, hour=2, principal_subject="human:reader")
    assert derive_measurement([period()], [presented, selected])["periods"][0]["portfolio"]["zero_click"] is False


def test_sampling_units_and_confirmed_false_writes_are_independent_of_packet_children():
    samples = [
        event(EventFamily.MEASUREMENT_SAMPLE, sample_kind="baseline_field", material=True,
              populated=True, correct=True, source_class="ucm", actor="independent", evidence_reference="field:1"),
        event(EventFamily.MEASUREMENT_SAMPLE, sample_kind="baseline_field", material=True,
              populated=True, correct=False, source_class="ucm", actor="independent", evidence_reference="field:2"),
        event(EventFamily.MEASUREMENT_SAMPLE, sample_kind="baseline_field", material=True,
              populated=False, correct=True, source_class="ucm", actor="independent", evidence_reference="field:3"),
        event(EventFamily.MEASUREMENT_SAMPLE, sample_kind="material_change", material=True,
              confirmed_miss=True, source_class="ucm", actor="independent", evidence_reference="miss:1"),
        event(EventFamily.MEASUREMENT_SAMPLE, sample_kind="false_write", material=True,
              automatic=True, classification="confirmed_policy_error", reversal_receipt="reversal:1",
              source_class="ucm", actor="independent", evidence_reference="false-write:1"),
        event(EventFamily.MEASUREMENT_SAMPLE, sample_kind="false_write", material=True,
              automatic=False, classification="confirmed_policy_error", reversal_receipt="reversal:2",
              source_class="ucm", actor="independent", evidence_reference="ordinary-correction:1"),
    ]
    report = derive_measurement([period()], [surface("packet-a", [1]), *samples, samples[0]])["periods"][0]
    assert report["packet_denominator"] == 1
    assert report["child_diagnostic_denominator"] == 1
    by_source = report["sampling"]["by_source_class"][0]
    assert by_source["checked_populated_material_fields"] == 2
    assert by_source["correct_populated_material_fields"] == 1
    assert by_source["confirmed_material_misses"] == 1
    assert by_source["confirmed_material_false_writes"] == 1
    changed = replace(samples[0], event_id="different", payload={**samples[0].payload, "correct": False})
    with pytest.raises(ValueError, match="retrospectively relabel"):
        derive_measurement([period()], [*samples, changed])


def test_native_log_collection_and_fixture_report_reproduce_private_outputs(tmp_path):
    import json
    import os
    from pathlib import Path
    from corridor.pilot_measurement_cli import main, read_event_log

    shown = surface("packet-a", [1])
    logs = tmp_path / "events.jsonl"
    logs.write_text(json.dumps({"event": "analytics_event", "analytics_event": shown.as_dict()}) + "\n")
    assert read_event_log(logs) == [shown]
    output = tmp_path / "report.json"
    fixture = Path(__file__).parent / "fixtures/pilot-measurement.json"
    assert main(["--input", str(fixture), "--output", str(output)]) == 0
    first = output.read_bytes()
    assert main(["--input", str(output.with_suffix(".inputs.json")), "--output", str(output)]) == 0
    assert output.read_bytes() == first
    assert os.stat(output).st_mode & 0o777 == 0o600
    report = json.loads(first)
    ordinary, quiet, burst = report["periods"]
    assert (ordinary["packet_denominator"], ordinary["child_diagnostic_denominator"]) == (1, 10)
    assert [p["volume_stratum"] for p in report["periods"]] == ["ordinary", "quiet", "burst"]
    assert quiet["portfolio"]["zero_click"] is True
    assert burst["captured_source_arrivals"] == 12
    assert all(p["cohort_status"] == "bound" for p in report["periods"])


def test_observation_collection_refuses_inferred_time_and_running_code_overrides_stale_file(tmp_path, monkeypatch):
    import json
    from corridor.analytics import capture_events
    from corridor.measurement_collection import collect_observation

    measured = event(EventFamily.WORK_OBSERVATION, category="coordinator_review", minutes=3,
                     actor="human:reader", evidence_reference="timer:1")
    with capture_events() as collected:
        collect_observation(measured)
    assert collected.events == [measured]
    for invalid in [float("nan"), -1, True]:
        with pytest.raises(ValueError, match="explicitly measured"):
            collect_observation(replace(measured, payload={**measured.payload, "minutes": invalid}))
    with pytest.raises(ValueError, match="unavailable time requires"):
        collect_observation(replace(measured, payload={**measured.payload, "minutes": None}))
    stale = tmp_path / "binding.json"
    stale.write_text(json.dumps(BINDING.as_dict()))
    monkeypatch.setenv("CORRIDOR_ANALYTICS_BINDING_FILE", str(stale))
    monkeypatch.setenv("CORRIDOR_CODE_REVISION", "actual-image-revision")
    assert default_binding().code_revision == "actual-image-revision"


def test_colliding_project_ids_cannot_blend_or_relabel_customer_origins():
    second = replace(surface("packet-a", [1]),
                     binding=replace(BINDING, customer_id="customer-b", database_identity="database-b"))
    with pytest.raises(ValueError, match="no declared customer measurement period"):
        derive_measurement([period()], [surface("packet-a", [1]), second])
    other = period(period_id="second-customer", customer_id="customer-b", database_identity="database-b",
                   partner_id="partner-b", binding=second.binding)
    reports = derive_measurement([period(), other], [surface("packet-a", [1]), second])["periods"]
    assert [r["packet_denominator"] for r in reports] == [1, 1]
    unbound = replace(second, binding=replace(second.binding, database_identity=None))
    with pytest.raises(ValueError, match="origin is unavailable"):
        derive_measurement([other], [unbound])


def test_verified_receipt_export_does_not_count_a_rolled_back_log_or_duplicate_a_commit():
    shown = surface("packet-a", [1, 2])
    logged = event(EventFamily.CHILD_DECISION, hour=2, delta_id=1, action="apply", outcome="resolved")
    receipt = replace(logged, event_id="retained:1", payload={**logged.payload,
                       "receipt": {"table": "delta_dispositions", "id": 1}})
    rolled_back = event(EventFamily.CHILD_DECISION, hour=2, delta_id=2, action="apply", outcome="resolved")
    report = derive_measurement([period(domain_receipts_complete=True)],
                                 [shown, logged, receipt, rolled_back])["periods"][0]
    assert [c["outcome"] for c in report["children"]] == ["apply", "ignored/open"]
    assert report["committed_decision_count"] == 1
    assert report["interaction_counts"]["child_decisions"] == 2
    assert len(report["observations"]) == 4


@pytest.mark.parametrize("missing", ["actor", "evidence_reference"])
def test_unattributed_samples_cannot_establish_any_positive_numerator(missing):
    candidate = event(EventFamily.RELEASE_CANDIDATE_PREPARATION, hour=2, candidate_id=3,
                      candidate_identity="candidate:3", accepted_revision_id=42,
                      configured_artifact_types=["updated_ucm", "weekly_report"],
                      artifacts=[{"artifact_type": "updated_ucm", "content_sha256": "b" * 64},
                                 {"artifact_type": "weekly_report", "content_sha256": "c" * 64}])
    authorized = event(EventFamily.RELEASE_AUTHORIZATION, hour=3, candidate_id=3,
                       candidate_identity="candidate:3", accepted_revision_id=42,
                       package_identity="package:1", status="authorized")
    samples = [
        event(EventFamily.MEASUREMENT_SAMPLE, hour=4, sample_kind="packet_usefulness",
              item_key="packet-a", necessary=True, actor="human:reader", evidence_reference="judgment:1"),
        event(EventFamily.MEASUREMENT_SAMPLE, hour=4, sample_kind="child_usefulness",
              delta_id=1, necessary=True, actor="human:reader", evidence_reference="judgment:2"),
        event(EventFamily.MEASUREMENT_SAMPLE, hour=4, sample_kind="release_readiness",
              candidate_id=3, client_ready=True, manual_repair_required=False,
              actor="human:reader", evidence_reference="judgment:3"),
    ]
    invalid = [replace(s, payload={key: value for key, value in s.payload.items() if key != missing})
               for s in samples]
    base = [surface("packet-a", [1]), candidate, authorized]
    report = derive_measurement([period()], [*base, *invalid])["periods"][0]
    assert report["necessary_interrupting_packets"] == 0
    assert report["unjudged_interrupting_packets"] == 1
    assert report["children"][0]["necessary"] is None
    assert report["client_ready_candidate_numerator"] == 0
    assert report["releases"][0]["client_ready"] is None
    assert set(report["sampling"]["invalid_observations"]) == {e.event_id for e in invalid}
    valid = derive_measurement([period()], [*base, *samples])["periods"][0]
    assert valid["necessary_interrupting_packets"] == 1
    assert valid["client_ready_candidate_numerator"] == 1


def portfolio_event(*, binding=BINDING, context=None):
    from corridor.analytics import capture_events
    from corridor.project_portfolio import NO_ACTION, PortfolioReading, ProjectStanding, emit_portfolio_reading

    standing = ProjectStanding(project_id=1, slug="fixture", name="Fixture", state=NO_ACTION,
                               landing="review", changes_to_review=0, follow_up_waiting=0,
                               follow_up_overdue=0, follow_up_due=0, readiness_problems=0,
                               accepted_revision_id=None, candidate_ready=False, preparing=False)
    if context is not None:
        standing = replace(standing, measurement_context=context)
    reading = PortfolioReading(START + timedelta(hours=1), "human:reader", (standing,))
    with capture_events() as captured:
        emit_portfolio_reading(reading, binding=binding)
    return captured.events[0]


def portfolio_context():
    return {"issue_profile_identity": "fixture-issue", "issue_profile_version": 1,
            "issue_profile_sha256": "a" * 64, "template_identity": BINDING.template_identity,
            "mapping_identity": BINDING.mapping_identity}


def test_real_portfolio_shape_is_validated_before_a_quiet_positive_conclusion():
    presented = portfolio_event()
    assert "project_id" not in presented.payload
    report = derive_measurement([period()], [presented])["periods"][0]
    assert report["cohort_status"] == "insufficient_evidence"
    assert report["portfolio"]["zero_click"] is None
    changed = replace(presented, binding=replace(BINDING, code_revision="changed-code"))
    with pytest.raises(ValueError, match="cohort change"):
        derive_measurement([period()], [changed])
    valid = portfolio_event(context=portfolio_context())
    result = derive_measurement([period()], [valid])["periods"][0]
    assert result["cohort_status"] == "bound"
    assert result["portfolio"]["quiet_zero_click"] is True
    changed_context = portfolio_event(context={**portfolio_context(), "issue_profile_version": 2})
    with pytest.raises(ValueError, match="cohort change"):
        derive_measurement([period()], [changed_context])
