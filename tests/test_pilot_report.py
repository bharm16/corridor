"""Economics and checkpoints preserve units, boundaries and adverse evidence."""

from copy import deepcopy
from datetime import timedelta
import json
import random
import stat

import pytest

from corridor.analytics import EventFamily
from corridor.pilot_measurement import derive_measurement
from corridor.pilot_report import derive_report, project_key, COORDINATOR_CATEGORIES, OPERATIONS_CATEGORIES
from corridor.pilot_checkpoint import build_checkpoint, render_checkpoint, CRITERIA
from corridor.pilot_report_cli import main
from test_pilot_measurement import period, event, surface, START


def inputs():
    p = period(domain_receipts_complete=True)
    contract = {"schema_version": "pilot-contract-v1", "declared_at": START.isoformat(),
        "packet_precision_basis": "all_surfaced", "minimum_stratum_project_weeks": 1,
        "source_classes": ["matrix", "email", "minutes", "schedule"],
        "business_calendar": {"timezone": "UTC", "holidays": [], "rule": "elapsed_weekdays_excluding_declared_holidays"},
        "shape": {"partners": {"fixture-partner": {"project_keys": [project_key(p.as_dict())], "signed_agreement_reference": "signed:1"}},
                  "comparison_method": "same coordinator and same work", "baseline_method": "logged pre-adoption work"},
        "cohorts": [{"id": "one", "period_ids": [p.period_id], "full_feature_flags": {"automation": False}}],
        "responses": {name: "Extend evidence; retain current rollout boundary" for name in CRITERIA}}
    evidence = {"baselines": [{"project_key": project_key(p.as_dict()),
        "start": (START-timedelta(days=21)).isoformat(), "end": (START-timedelta(days=7)).isoformat(),
        "adopted_at": START.isoformat(), "approved_at": (START-timedelta(days=1)).isoformat(),
        "actor": "coordinator", "approved_by": "partner", "evidence_reference": "baseline:1",
        "complete_reporting_cycle": True, "ordinary_work": True, "no_change_work": True,
        "substantive_revision": True, "complete_work_log": True,
        "work": [{"category": "record_maintenance", "minutes": 200, "actor": "coordinator", "evidence_reference": "log:1"}]}],
        "period_attestations": {p.period_id: {"actor": "coordinator", "evidence_reference": "log-completeness:1",
            "complete_time_categories": list(COORDINATOR_CATEGORIES+OPERATIONS_CATEGORIES),
            "disjoint_time_entries": True, "all_provider_costs_complete": True}}}
    return p, contract, evidence


def report_with(events=()):
    p, contract, evidence = inputs()
    return derive_report(derive_measurement([p], events), contract, evidence)


def pooled(report):
    return report["cohorts"][0]["partners"]["fixture-partner"]["pooled"]


def test_all_surfaced_packets_keep_open_and_noninterrupting_denominators():
    shown = surface("one", [1])
    other = event(EventFamily.PACKET_SURFACING, item_key="two", child_consequences=[], child_count=0,
                  consequence_level="informational", hour=2)
    judged = event(EventFamily.MEASUREMENT_SAMPLE, sample_kind="packet_usefulness", item_key="one",
                   necessary=True, actor="coordinator", evidence_reference="triage:1", hour=3)
    result = report_with([shown, other, judged])
    packets = pooled(result)["packet_precision"]
    assert packets["all_surfaced"]["denominator"] == 2
    assert packets["all_surfaced"]["numerator"] == 1
    assert packets["all_surfaced"]["value"] is None
    assert packets["interrupting"]["value"] == 1
    assert packets["all_surfaced"]["unjudged"] == 1


def test_same_work_savings_exclude_new_artifacts_and_missing_time_is_not_zero():
    events = [event(EventFamily.WORK_OBSERVATION, category="record_maintenance", minutes=20,
                    actor="coordinator", evidence_reference="work:1"),
              event(EventFamily.WORK_OBSERVATION, category="report_preparation", artifact_type="weekly_report", minutes=90,
                    actor="coordinator", evidence_reference="work:2", hour=2)]
    p, contract, evidence = inputs()
    result = derive_report(derive_measurement([p], events), contract, evidence)
    assert pooled(result)["net_coordinator_time"]["value"] == .2
    assert pooled(result)["net_coordinator_time"]["new_output_minutes"] == [90]
    evidence["period_attestations"] = {}
    incomplete = derive_report(derive_measurement([p], events), contract, evidence)
    assert pooled(incomplete)["net_coordinator_time"]["value"] is None
    assert pooled(incomplete)["review_burden"]["median"] is None


def test_two_week_baseline_without_revision_is_insufficient_and_reproducible():
    p, contract, evidence = inputs()
    evidence["baselines"][0]["substantive_revision"] = False
    measurement = derive_measurement([p], [])
    first = derive_report(measurement, contract, evidence)
    assert first == derive_report(measurement, contract, evidence)
    assert pooled(first)["net_coordinator_time"]["value"] is None
    assert first["baselines"][p.period_id]["adequate"] is False


def test_material_configuration_change_cannot_be_pooled_and_partial_week_is_not_full_week():
    from dataclasses import replace
    p, contract, evidence = inputs()
    next_week = replace(p, period_id="two", start=START+timedelta(days=7), end=START+timedelta(days=14),
                        binding=replace(p.binding, packetizer_rules_version="changed"))
    contract["cohorts"][0]["period_ids"].append("two")
    with pytest.raises(ValueError, match="separate cohorts"):
        derive_report(derive_measurement([p, next_week], []), contract, evidence)
    contract["cohorts"][0]["period_ids"] = [p.period_id]
    partial = replace(p, end=START+timedelta(days=3))
    result = derive_report(derive_measurement([partial], []), contract, evidence)
    assert pooled(result)["net_coordinator_time"]["value"] is None


def test_native_unsurfaced_delta_latency_uses_predeclared_business_calendar():
    arrival = event(EventFamily.SOURCE_ARRIVAL, hour=4*24, source_identity="delivery:1", source_class="matrix", receipt={"table": "source_deliveries", "id": 1})
    delta = event(EventFamily.PROPOSED_DELTA_CREATION, hour=6*24, delta_id=1, source_identity="delivery:1", source_class="matrix", receipt={"table": "proposed_deltas", "id": 1})
    result = report_with([arrival, delta])
    metrics = pooled(result)["source_latency"]
    assert metrics["calendar_seconds"]["median"] == 2*86400
    assert metrics["business_days"]["median"] == 1
    assert metrics["calendar_seconds"]["denominator"] == 1


def test_checkpoint_emits_every_criterion_without_claiming_fixture_customer_success():
    result = build_checkpoint(report_with())
    findings = result["cohorts"][0]["partners"]["fixture-partner"]["findings"]
    assert [f["criterion"] for f in findings] == list(CRITERIA)
    assert result["result"] == "insufficient_evidence"
    assert result["temporary_contract_closed"] is False
    assert result["conclusion"]["outcome"] == "extend evidence"
    assert "Ordinary development continues" in render_checkpoint(result)
    assert all("numerator" in f and "denominator" in f and f["source"]["report_sha256"] for f in findings)


def test_confirmed_material_false_write_stays_failed_in_later_cohort():
    p, contract, evidence = inputs()
    failure = event(EventFamily.MEASUREMENT_SAMPLE, sample_kind="false_write", material=True, automatic=True,
        classification="confirmed_policy_error", reversal_receipt="decision:1", policy_class="closure", actor="reviewer", evidence_reference="review:1")
    report = derive_report(derive_measurement([p], [failure]), contract, evidence)
    result = build_checkpoint(report)
    f = next(f for f in result["cohorts"][0]["partners"]["fixture-partner"]["findings"] if f["criterion"] == "material_false_writes")
    assert f["result"] == "fail"
    assert f["value"] == ["closure"]
    assert result["result"] == "fail"
    report["evidence"]["checkpoint_decision"] = {"outcome": "continue as designed", "actor": "operator", "evidence_reference": "decision"}
    with pytest.raises(ValueError, match="every criterion"):
        build_checkpoint(report)


def test_adoption_accuracy_counts_checked_populated_fields_instead_of_rows():
    report = report_with()
    key = project_key(report["periods"][0]["declaration"])
    audit = {"project_key": key, "actor": "reviewer", "evidence_reference": "audit:1", "random_seed": "seed",
        "total_rows": 2, "population_row_ids": ["1", "2"], "sampled_row_ids": ["1", "2"],
        "all_material_fields_checked": True, "sampled_at": START.isoformat(), "adopted_at": START.isoformat(),
        "checked_at": START.isoformat(), "onboarding_end": (START+timedelta(days=14)).isoformat(),
        "discarded_rows": 0, "discarded_columns": 0,
        "fields": [{"row_id": str(row), "field": field, "material": True, "populated": True,
                    "correct": (row, field) != (2, "closure"), "evidence_reference": f"cell:{row}:{field}"}
                   for row in (1, 2) for field in ("closure", "utility_owner")]}
    report["evidence"]["adoption_audits"] = [audit]
    checkpoint = build_checkpoint(report)
    finding = next(f for f in checkpoint["cohorts"][0]["partners"]["fixture-partner"]["findings"] if f["criterion"] == "baseline_accuracy")
    assert finding["numerator"] == 3 and finding["denominator"] == 4
    assert finding["result"] == "fail"


def test_cli_writes_private_replayable_json_and_written_checkpoint(tmp_path):
    p, contract, evidence = inputs()
    measurement = derive_measurement([p], [])
    for name, value in (("measurement", measurement), ("contract", contract), ("evidence", evidence)):
        (tmp_path/f"{name}.json").write_text(json.dumps(value))
    output = tmp_path/"report.json"
    assert main(["report", "--measurement", str(tmp_path/"measurement.json"), "--contract", str(tmp_path/"contract.json"),
        "--evidence", str(tmp_path/"evidence.json"), "--output", str(output)]) == 0
    checkpoint = tmp_path/"checkpoint.json"
    assert main(["checkpoint", "--report", str(output), "--output", str(checkpoint)]) == 0
    assert "Pilot checkpoint" in checkpoint.with_suffix(".md").read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(checkpoint.with_suffix(".md").stat().st_mode) == 0o600
    assert output.with_suffix(".inputs.json").exists()


def test_material_sampling_cannot_omit_native_arrivals_or_lower_case_minimum():
    p, contract, evidence = inputs()
    arrival = event(EventFamily.SOURCE_ARRIVAL, source_identity="delivery:1", source_class="matrix",
                    receipt={"table": "source_deliveries", "id": 1})
    measurement = derive_measurement([p], [arrival])
    base = derive_report(measurement, contract, evidence)
    population = base["native_sampling_populations"]["one"]
    assert population[0]["source_class"] == "ucm_revision"
    policy = {"identity": "test", "fields": ["closure"], "material_fields": ["closure"], "sampling_seed": "test", "minimum_material_cases": 30}
    sample = {"cohort_id": "one", "declared_at": START.isoformat(), "policy": policy, "arrivals": [], "inspections": []}
    evidence["material_sampling"] = [sample]
    with pytest.raises(ValueError, match="complete native"):
        derive_report(measurement, contract, evidence)
    sample["arrivals"] = population
    sample["policy"]["minimum_material_cases"] = 1
    with pytest.raises(ValueError, match="less than 30"):
        derive_report(measurement, contract, evidence)
    sample["policy"]["minimum_material_cases"] = 30
    result = derive_report(measurement, contract, evidence)
    assert result["material_sampling"]["one"]["partners"]["fixture-partner"]["status"] == "insufficient_evidence"


def test_a_declared_partner_with_no_data_still_gets_every_finding():
    p, contract, evidence = inputs()
    contract["shape"]["partners"]["absent-partner"] = {"project_keys": ["unobserved:1", "unobserved:2"], "signed_agreement_reference": "signed:2"}
    result = build_checkpoint(derive_report(derive_measurement([p], []), contract, evidence))
    missing = result["cohorts"][0]["partners"]["absent-partner"]["findings"]
    assert len(missing) == len(CRITERIA)
    assert all(f["result"] == "insufficient_evidence" for f in missing)


def test_source_latency_joins_an_arrival_in_the_preceding_retained_week():
    from dataclasses import replace
    p, contract, evidence = inputs()
    second = replace(p, period_id="second", start=START+timedelta(days=7), end=START+timedelta(days=14))
    contract["cohorts"][0]["period_ids"].append("second")
    arrival = event(EventFamily.SOURCE_ARRIVAL, hour=6*24, source_identity="delivery:1", source_class="matrix", receipt={"table": "source_deliveries", "id": 1})
    delta = event(EventFamily.PROPOSED_DELTA_CREATION, hour=8*24, delta_id=1, source_identity="delivery:1", source_class="matrix", receipt={"table": "proposed_deltas", "id": 1})
    result = derive_report(derive_measurement([p, second], [arrival, delta]), contract, evidence)
    latency = pooled(result)["source_latency"]
    assert latency["calendar_seconds"]["median"] == 2*86400
    assert latency["business_days"]["median"] == 1


def test_later_artifact_obligation_cannot_overwrite_an_earlier_baseline_assessment():
    from dataclasses import replace
    p, contract, evidence = inputs()
    later = replace(p, period_id="later", start=START+timedelta(days=7), end=START+timedelta(days=14),
                    previously_performed_artifacts=("updated_ucm", "weekly_report"))
    contract["cohorts"].append({"id": "later", "period_ids": ["later"], "full_feature_flags": {}})
    evidence["baselines"][0]["work"].append({"category": "report_preparation", "artifact_type": "weekly_report",
        "minutes": 100, "actor": "coordinator", "evidence_reference": "baseline:extra"})
    evidence["period_attestations"]["later"] = deepcopy(evidence["period_attestations"][p.period_id])
    result = derive_report(derive_measurement([p, later], []), contract, evidence)
    assert result["baselines"][p.period_id]["weekly_minutes"] is None
    assert result["baselines"]["later"]["weekly_minutes"] == 150
    assert result["cohorts"][0]["partners"]["fixture-partner"]["pooled"]["net_coordinator_time"]["value"] is None
    assert result["cohorts"][1]["partners"]["fixture-partner"]["pooled"]["net_coordinator_time"]["baseline_minutes"] == 150


def test_unattributed_completeness_cannot_establish_false_write_or_diagnostic_success():
    p, contract, evidence = inputs()
    attestation = evidence["period_attestations"][p.period_id]
    attestation.pop("actor")
    attestation["false_write_review_complete"] = True
    attestation["complete_diagnostics"] = {"alert_usefulness": True}
    evidence["diagnostics"] = [{"kind": "alert_usefulness", "period_id": p.period_id, "identity": "alert:1",
        "actor": "coordinator", "evidence_reference": "alert:judgment:1", "useful": True,
        "triaged_at": START.isoformat(), "judged_at": START.isoformat()}]
    checkpoint = build_checkpoint(derive_report(derive_measurement([p], []), contract, evidence))
    partner = checkpoint["cohorts"][0]["partners"]["fixture-partner"]
    false_writes = next(f for f in partner["findings"] if f["criterion"] == "material_false_writes")
    assert false_writes["result"] == "insufficient_evidence"
    assert partner["diagnostics"]["alert_usefulness"]["value"] is None
