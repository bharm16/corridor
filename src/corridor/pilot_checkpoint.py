"""Print each pilot criterion and its bounded rollout consequence (#498).

The economics reader owns denominators; this module owns the fixed temporary
pilot thresholds and findings. Missing evidence cannot become a pass, a burst
failure cannot disappear in a pooled value, and a confirmed automatic material
false write stays failed across later measurement cohorts. These are reports,
not policy activation or customer-record commands.
"""

from collections import defaultdict
from datetime import timedelta
import json
import random

from corridor.pilot_report import digest, instant, number, project_key, rate


CRITERIA = (
    "pilot_shape", "cohort_binding", "baseline_representativeness", "net_coordinator_time",
    "review_burden", "operations_time", "source_latency", "decision_latency", "baseline_accuracy",
    "routine_accept_without_edit", "material_accept_without_edit", "material_false_writes",
    "material_misses", "coverage", "low_value_exceptions", "packet_precision", "child_diagnostics",
    "client_ready_packages", "manual_reconstruction", "provider_cost", "commercial",
)
OUTCOMES = ("continue as designed", "continue with revision", "extend evidence", "narrow the slice or cohort",
            "delay broader rollout", "keep a higher-risk capability disabled", "stop the affected commercial path")
STRATIFIED = {"net_coordinator_time", "review_burden", "operations_time", "source_latency",
              "packet_precision", "manual_reconstruction"}


def _finding(name, value, threshold, passes, *, measured=True, numerator=None, denominator=None, unit=None):
    return {"criterion": name, "value": value, "threshold": threshold,
            "numerator": numerator, "denominator": denominator, "unit": unit,
            "result": "pass" if measured and passes else "fail" if measured else "insufficient_evidence"}


def _ratio(name, metric, threshold, *, minimum=None, maximum=None):
    value = metric.get("value")
    return _finding(name, value, threshold,
                    (minimum is None or value is not None and value >= minimum)
                    and (maximum is None or value is not None and value <= maximum),
                    measured=value is not None, numerator=metric["numerator"], denominator=metric["denominator"], unit=metric["unit"])


def _dist(name, metric, threshold, *, median_max=None, p90_max=None, mean_max=None):
    measured = metric["status"] == "measured"
    mean = metric["numerator"] / metric["denominator"] if measured else None
    value = {"median": metric["median"], "p90": metric["p90"], "mean": mean}
    passes = measured and (median_max is None or value["median"] <= median_max) and (
        p90_max is None or value["p90"] <= p90_max) and (mean_max is None or mean <= mean_max)
    return _finding(name, value, threshold, passes, measured=measured,
                    numerator=metric["numerator"], denominator=metric["denominator"], unit=metric["unit"])


def _periods(report, cohort, partner):
    return [p for p in report["periods"] if p["declaration"]["period_id"] in cohort.get("live_period_ids", cohort["period_ids"])
            and p["declaration"]["partner_id"] == partner]


def _shape(report, cohort, partner, rows):
    shape = report["declaration"].get("shape", {})
    declared = shape.get("partners", {})
    projects = declared.get(partner, {}).get("project_keys", [])
    by_project = defaultdict(list)
    for row in rows:
        by_project[project_key(row["declaration"])].append(row)
    duration_ok = bool(projects) and set(projects) == set(by_project)
    for project_rows in by_project.values():
        ordered = sorted(project_rows, key=lambda r: r["declaration"]["start"])
        duration_ok = duration_ok and len(ordered) >= 8 and all(
            instant(r["declaration"]["end"]) - instant(r["declaration"]["start"]) == timedelta(days=7)
            for r in ordered) and all(a["declaration"]["end"] == b["declaration"]["start"] for a, b in zip(ordered, ordered[1:]))
    week_numbers = cohort.get("pilot_week_by_period", {})
    onboarding = cohort.get("onboarding_period_ids", [])
    for project_rows in by_project.values():
        duration_ok = duration_ok and set(week_numbers.get(r["declaration"]["period_id"]) for r in project_rows) >= set(range(3, 11))
    onboarding_by_project = defaultdict(list)
    for row in report["periods"]:
        if row["declaration"]["period_id"] in onboarding and row["declaration"]["partner_id"] == partner:
            onboarding_by_project[project_key(row["declaration"])].append(row)
    duration_ok = duration_ok and all(sum((instant(r["declaration"]["end"])-instant(r["declaration"]["start"])).total_seconds()
        for r in onboarding_by_project[key]) >= 14*86400 for key in projects)
    sources = sum(r["captured_source_arrivals"] for r in rows)
    agreements = all(p.get("signed_agreement_reference") for p in declared.values())
    enough = len(declared) >= 2 and all(len(p.get("project_keys", [])) >= 2 for p in declared.values())
    known = bool(shape.get("baseline_method") and shape.get("comparison_method") and agreements)
    return _finding("pilot_shape", {"partners": len(declared), "projects": len(projects), "captured_arrivals": sources,
                    "consecutive_live_weeks": duration_ok, "declared_shape": shape},
                    "at least 2 signed partners, 2 projects each; 8 consecutive live weeks after 2 onboarding weeks; 40 arrivals per partner",
                    enough and duration_ok and sources >= 40, measured=known and duration_ok and sources >= 40,
                    numerator=sources, denominator=40, unit="captured arrivals per partner")


def _baseline_accuracy(report, rows):
    audits = report["evidence"].get("adoption_audits", [])
    known, correct, checked, failures = True, 0, 0, []
    for key in {project_key(r["declaration"]) for r in rows}:
        matches = [a for a in audits if a.get("project_key") == key]
        if len(matches) != 1:
            known = False
            continue
        audit = matches[0]
        fields = audit.get("fields", [])
        populated = [f for f in fields if f.get("material") is True and f.get("populated") is True]
        checked += len(populated)
        correct += sum(f.get("correct") is True for f in populated)
        rows_checked = set(audit.get("sampled_row_ids", []))
        expected = min(100, audit.get("total_rows", 0))
        population = audit.get("population_row_ids", [])
        selected = set(random.Random(audit.get("random_seed")).sample(sorted(population), expected)) if (
            expected > 0 and len(set(population)) == len(population) and len(population) >= expected) else set()
        valid = (audit.get("evidence_reference") and audit.get("actor") and audit.get("random_seed")
                 and audit.get("all_material_fields_checked") is True and len(rows_checked) == expected and expected > 0
                 and len(population) == audit.get("total_rows") and rows_checked == selected
                 and instant(audit["sampled_at"]) <= instant(audit["adopted_at"])
                 and instant(audit["checked_at"]) <= instant(audit["onboarding_end"])
                 and all(f.get("row_id") in rows_checked and f.get("field") and f.get("evidence_reference")
                         and type(f.get("correct")) is bool for f in populated)
                 and len({(f.get("row_id"), f.get("field")) for f in fields}) == len(fields)
                 and type(audit.get("discarded_rows")) is int and type(audit.get("discarded_columns")) is int)
        known = known and bool(valid)
        if audit.get("discarded_rows", 0) or audit.get("discarded_columns", 0):
            failures.append(key)
    metric = rate(correct, checked, "checked populated material fields", complete=known)
    result = _ratio("baseline_accuracy", metric, ">=99% checked populated material fields; zero silently discarded rows/columns", minimum=.99)
    result["discarded_structure_projects"] = failures
    if failures:
        result["result"] = "fail"
    return result


def _false_writes(report, partner):
    failures = set()
    # Deliberately read all cohorts: an error cannot disappear after a boundary.
    for row in report["periods"]:
        if row["declaration"]["partner_id"] != partner:
            continue
        invalid = set(row["sampling"]["invalid_observations"])
        for obs in row["observations"]:
            p = obs["payload"]
            if (obs["event_id"] not in invalid and p.get("sample_kind") == "false_write"
                    and p.get("material") is True and p.get("automatic") is True
                    and p.get("classification") == "confirmed_policy_error" and p.get("reversal_receipt")):
                failures.add(p.get("policy_class", "unattributed policy class"))
    for sample in report["material_sampling"].values():
        failures.update(sample["partners"].get(partner, {}).get("failed_policy_classes", []))
    rows = [r for r in report["periods"] if r["declaration"]["partner_id"] == partner]
    complete = bool(rows) and all(report["work"][r["declaration"]["period_id"]]["attestation"].get("false_write_review_complete") is True
                                  and report["work"][r["declaration"]["period_id"]]["attestation"].get("evidence_reference") for r in rows)
    return _finding("material_false_writes", sorted(failures), "zero confirmed automatic material false writes; affected policy class stays failed",
                    not failures, measured=bool(failures) or complete, numerator=len(failures), denominator=None, unit="failed policy classes")


def _diagnostics(report, rows):
    ids = {r["declaration"]["period_id"] for r in rows}
    result = {}
    for kind, unit in (("child_outcome_identifiability", "multi-child packet acts"),
                       ("alert_usefulness", "alerts"), ("release_warning_usefulness", "release warnings"),
                       ("exception_usefulness", "exceptions")):
        observations = [e for e in report["evidence"].get("diagnostics", []) if e.get("period_id") in ids and e.get("kind") == kind]
        identities = [e.get("identity") for e in observations]
        valid = (bool(observations) and len(set(identities)) == len(identities) and all(
            e.get("identity") and e.get("evidence_reference") and e.get("actor") and type(e.get("useful")) is bool
            and e.get("triaged_at") == e.get("judged_at") for e in observations))
        complete = all(report["work"][r["declaration"]["period_id"]]["attestation"].get("complete_diagnostics", {}).get(kind) is True for r in rows)
        if kind == "child_outcome_identifiability":
            native_acts = {(r["declaration"]["period_id"], str(o["payload"]["receipt_id"])) for r in rows for o in r["observations"]
                if o["family"] == "packet_save" and o["payload"].get("receipt")
                and len(o["payload"].get("children", [])) > 1}
            judged_acts = {(e.get("period_id"), str(e.get("receipt_id"))) for e in observations}
            valid = valid and native_acts == judged_acts
        result[kind] = {**rate(sum(e.get("useful") is True for e in observations), len(observations), unit, complete=valid and complete),
                        "observations": observations}
    return result


def _criteria(report, cohort, partner, metrics, rows):
    entries = []
    entries.append(_ratio("net_coordinator_time", metrics["net_coordinator_time"], "<=50% of representative same-work baseline", maximum=.5))
    entries.append(_dist("review_burden", metrics["review_burden"], "median <=15; p90 <=30 minutes/project-week", median_max=15, p90_max=30))
    entries.append(_dist("operations_time", metrics["operations_time"], "mean <=15 minutes/project-week from week 3", mean_max=15))
    latency = _dist("source_latency", metrics["source_latency"]["calendar_seconds"], "median <=24 hours; p90 <=3 business days", median_max=86400)
    business = metrics["source_latency"]["business_days"]
    latency["business_days"] = business
    latency["calendar"] = metrics["source_latency"]["calendar"]
    if business["status"] == "measured" and business["p90"] > 3:
        latency["result"] = "fail"
    elif business["status"] != "measured" and latency["result"] != "fail":
        latency["result"] = "insufficient_evidence"
    entries.append(latency)
    entries.append(_dist("decision_latency", metrics["latency"]["delta_to_decision"], "reported; no threshold"))
    entries.append(_ratio("routine_accept_without_edit", metrics["routine_accept_without_edit"], ">=80% resolved routine deltas", minimum=.8))
    material = {f: _ratio(f, m, ">=70% resolved material deltas", minimum=.7) for f, m in metrics["material_accept_without_edit"].items()}
    material_results = {v["result"] for v in material.values()}
    entry = _finding("material_accept_without_edit", material, ">=70% separately for every predeclared material field", material_results == {"pass"},
                     measured=bool(material) and "insufficient_evidence" not in material_results, unit="resolved deltas per material field")
    if "fail" in material_results:
        entry["result"] = "fail"
    entries.append(entry)
    entries.append(_ratio("coverage", metrics["coverage"], ">=95% connected-channel arrivals captured within declared window", minimum=.95))
    basis = report["declaration"]["packet_precision_basis"]
    entries.append(_ratio("packet_precision", metrics["packet_precision"][basis], f">=80%; predeclared denominator {basis}", minimum=.8))
    entries.append(_ratio("client_ready_packages", metrics["packages"], ">=90% prepared candidates; complete configured artifacts without outside repair", minimum=.9))
    entries.append(_dist("manual_reconstruction", metrics["manual_reconstruction"], "median <=3 minutes per interrupting packet", median_max=3))
    entries.append(_dist("provider_cost", metrics["provider_cost"], "mean <=25 USD/active project-week, all providers", mean_max=25))
    return entries


def _partner_findings(report, cohort, partner, values):
    rows = _periods(report, cohort, partner)
    entries = _criteria(report, cohort, partner, values["pooled"], rows)
    entries.append(_shape(report, cohort, partner, rows))
    population = set(report["declaration"].get("source_classes", []))
    actual_sources = {c["source_class"] for r in rows for c in r["coverage"]}
    bound = (report["declaration"].get("evidence_kind") == "live" and bool(population) and actual_sources <= population and all(
        r["cohort_status"] == "bound" and r["declaration"]["domain_receipts_complete"] for r in rows))
    # Disabled flags also matter; #532's enabled list alone is not a full state.
    pins = cohort.get("full_feature_flags")
    bound = bound and isinstance(pins, dict) and all(type(value) is bool for value in pins.values()) and all(
        {name for name, enabled in pins.items() if enabled} == set(r["declaration"]["binding"]["enabled_feature_flags"]) for r in rows)
    entries.append(_finding("cohort_binding", [r["declaration"] for r in rows], "all configuration pinned before observation, including disabled flags",
                            bound and isinstance(pins, dict), measured=bound and isinstance(pins, dict)))
    baseline = {project_key(r["declaration"]): report["baselines"][project_key(r["declaration"])] for r in rows}
    entries.append(_finding("baseline_representativeness", baseline, ">=2 weeks; full reporting cycle, ordinary/no-change work, substantive revision or predeclared historical match",
                            all(b["adequate"] for b in baseline.values()), measured=all(b["adequate"] for b in baseline.values())))
    entries.append(_baseline_accuracy(report, rows))
    entries.append(_false_writes(report, partner))
    sample = report["material_sampling"].get(cohort["id"], {}).get("partners", {}).get(partner, {})
    entries.append(_ratio("material_misses", rate(sample.get("material_miss_numerator", 0), sample.get("material_case_denominator", 0),
        "independently inspected material changes", complete=sample.get("status") == "measured"), "<=5%; all arrivals up to 20/week else 20 random; at least 30 material cases", maximum=.05))
    diagnostics = _diagnostics(report, rows)
    entries.append(_ratio("child_diagnostics", diagnostics["child_outcome_identifiability"], ">=95% multi-child packet acts identify every child outcome without outside help", minimum=.95))
    # Early and late windows are pinned before measurement, not inferred by
    # selecting the quietest observations from a date range.
    week_numbers = cohort.get("pilot_week_by_period", {})
    early_ids = {p for p, week in week_numbers.items() if week in {3, 4}}
    late_ids = {p for p, week in week_numbers.items() if week in {7, 8, 9, 10}}
    early = _diagnostics(report, [r for r in rows if r["declaration"]["period_id"] in early_ids])["exception_usefulness"]
    late = _diagnostics(report, [r for r in rows if r["declaration"]["period_id"] in late_ids])["exception_usefulness"]
    e, l = early["value"], late["value"]
    measured = e is not None and l is not None and early_ids.isdisjoint(late_ids)
    low = {"early_low_value_rate": 1-e if e is not None else None, "late_low_value_rate": 1-l if l is not None else None}
    entries.append(_finding("low_value_exceptions", low, "late weeks <=20% low-value and >=30% relative reduction from early weeks",
        measured and 1-l <= .2 and 1-l <= .7*(1-e), measured=measured,
        numerator={"early": early["denominator"]-early["numerator"], "late": late["denominator"]-late["numerator"]},
        denominator={"early": early["denominator"], "late": late["denominator"]}, unit="exceptions at triage"))
    commercial = [c for c in report["evidence"].get("commercial", []) if c.get("partner_id") == partner and c.get("cohort_id") == cohort["id"]]
    valid = len(commercial) == 1 and commercial[0].get("evidence_reference") and commercial[0].get("actor") and type(commercial[0].get("will_pay")) is bool
    entries.append(_finding("commercial", commercial, "each partner states in writing it will pay for continued or expanded use",
                            valid and commercial[0]["will_pay"], measured=bool(valid), numerator=int(bool(valid and commercial[0]["will_pay"])), denominator=1, unit="written partner statements"))
    by_name = {e["criterion"]: e for e in entries}
    minimum = report["declaration"].get("minimum_stratum_project_weeks")
    if type(minimum) is not int or minimum < 1:
        raise ValueError("predeclare a positive minimum_stratum_project_weeks")
    for stratum, metrics in values["volume_strata"].items():
        for sub in _criteria(report, cohort, partner, metrics, rows):
            name = sub["criterion"]
            if name not in STRATIFIED:
                continue
            if metrics["project_week_denominator"] < minimum and sub["result"] != "fail":
                sub["result"] = "insufficient_evidence"
            by_name[name].setdefault("volume_strata", {})[stratum] = sub
            if sub["result"] == "fail":
                by_name[name]["result"] = "fail"
            elif sub["result"] == "insufficient_evidence" and by_name[name]["result"] == "pass":
                by_name[name]["result"] = "insufficient_evidence"
    # A small or unbound cohort cannot support a pass, but known defects stay
    # visible even when its sample is too small for positive claims.
    if by_name["pilot_shape"]["result"] != "pass" or by_name["cohort_binding"]["result"] != "pass":
        for finding in by_name.values():
            if finding["result"] == "pass":
                finding["result"] = "insufficient_evidence"
                finding["evidence_limit"] = "pilot shape or cohort binding is incomplete"
    for name in CRITERIA:
        finding = by_name[name]
        finding["source"] = {"report_sha256": report["input_sha256"], "cohort_id": cohort["id"], "partner_id": partner,
                             "period_ids": [r["declaration"]["period_id"] for r in rows]}
        response = report["declaration"].get("responses", {}).get(name)
        finding["predeclared_response"] = response
        finding["response_taken"] = [r for r in report["evidence"].get("responses_taken", [])
            if r.get("criterion") == name and r.get("cohort_id") == cohort["id"] and r.get("partner_id") == partner]
        if finding["result"] != "pass" and not response:
            finding["response_status"] = "predeclared response missing"
        elif finding["result"] != "pass" and not any(
            r.get("actor") and r.get("evidence_reference") and r.get("action")
            and r.get("next_period_or_extension") for r in finding["response_taken"]):
            finding["response_status"] = "response taken and extension/new period not evidenced"
        else:
            finding["response_status"] = "recorded"
    return {"findings": [by_name[name] for name in CRITERIA], "diagnostics": diagnostics}


def build_checkpoint(report):
    if report.get("schema_version") != "pilot-report-v1":
        raise ValueError("checkpoint requires the reproducible #424 report")
    cohorts = [{"declaration": c["declaration"], "partners": {
        partner: _partner_findings(report, c["declaration"], partner, values)
        for partner, values in c["partners"].items()}} for c in report["cohorts"]]
    findings = [f for c in cohorts for p in c["partners"].values() for f in p["findings"]]
    results = {f["result"] for f in findings}
    outcome = report["evidence"].get("checkpoint_decision", {})
    selected = outcome.get("outcome")
    if selected is not None and selected not in OUTCOMES:
        raise ValueError("checkpoint outcome must use one of the declared rollout responses")
    if selected == "continue as designed" and results != {"pass"}:
        raise ValueError("continue as designed requires every criterion to pass")
    decision_recorded = bool(selected and outcome.get("actor") and outcome.get("evidence_reference"))
    suggested = "continue as designed" if results == {"pass"} else "continue with revision" if "fail" in results else "extend evidence"
    return {"schema_version": "pilot-checkpoint-v1", "report_sha256": report["input_sha256"],
            "input_sha256": digest(report), "declaration": report["declaration"], "cohorts": cohorts,
            "result": "fail" if "fail" in results else "pass" if results == {"pass"} else "insufficient_evidence",
            "conclusion": {"outcome": selected if decision_recorded else suggested,
                           "status": "recorded human decision" if decision_recorded else "recommendation; human decision not recorded",
                           "evidence": outcome, "gates": ["validated claims", "cohort expansion", "broader rollout", "feature enablement"],
                           "ordinary_development": "continues under the roadmap unless a separate safety defect blocks it"},
            "temporary_contract_closed": bool(findings) and decision_recorded and all(f["response_status"] == "recorded" for f in findings),
            "limits": report["limits"]}


def render_checkpoint(checkpoint):
    """Render the exact findings and declared responses as a portable written artifact."""
    lines = ["# Pilot checkpoint", "", f"Result: **{checkpoint['result']}**", "",
             "Declared shape, boundaries and population:", "", "```json",
             json.dumps(checkpoint["declaration"], indent=2, sort_keys=True), "```", ""]
    for cohort in checkpoint["cohorts"]:
        for partner, report in cohort["partners"].items():
            lines += [f"## {partner} / {cohort['declaration']['id']}", ""]
            for finding in report["findings"]:
                lines += [f"### {finding['criterion']}: {finding['result']}", "",
                    f"Threshold: {finding['threshold']}", "",
                    "```json", json.dumps(finding, indent=2, sort_keys=True), "```", ""]
            lines += ["Separate diagnostics:", "", "```json", json.dumps(report["diagnostics"], indent=2, sort_keys=True), "```", ""]
    lines += ["## Checkpoint outcome", "", checkpoint["conclusion"]["outcome"], "",
              checkpoint["conclusion"]["status"], "",
              "Gates validated claims, cohort expansion, broader rollout and feature enablement.", "",
              "Ordinary development continues under the roadmap unless a separate safety defect blocks it.", "",
              f"Temporary contract closed: {checkpoint['temporary_contract_closed']}", "",
              f"Source report SHA-256: {checkpoint['report_sha256']}", ""]
    return "\n".join(lines)
