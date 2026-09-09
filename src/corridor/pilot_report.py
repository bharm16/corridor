"""Derive economics from #532 receipts without turning missing evidence into savings.

The instrumentation reader owns event joins. This layer consumes its frozen
analytical output, approved baseline logs and #499 independent inspections.
Previously the report stopped at raw project-week rows; this reader preserves
those rows and adds comparable work, exact denominator measures and explicit
cohort boundaries for the pilot checkpoint. No function writes project state.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from statistics import median
from zoneinfo import ZoneInfo

from corridor.shadow_comparison import ComparisonPolicy, assess_material_sample


COORDINATOR_CATEGORIES = ("record_maintenance", "report_preparation", "coordinator_review",
                          "manual_repair", "manual_reconstruction")
OPERATIONS_CATEGORIES = ("operations_setup", "operations_triage", "connector_maintenance",
                         "operations_support", "failure_recovery")
VOLUME_STRATA = ("quiet", "ordinary", "burst")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("evidence timestamps must include a timezone")
    return result


def number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def percentile(values, fraction):
    """Nearest-rank percentile; no interpolation can hide a single burst week."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def distribution(values, denominator, unit, *, complete=True):
    known = [value for value in values if number(value)]
    valid = complete and len(known) == denominator and denominator > 0
    return {"median": median(known) if valid else None,
            "p90": percentile(known, .9) if valid else None,
            "numerator": sum(known), "denominator": denominator, "observed_count": len(known),
            "unit": unit, "status": "measured" if valid else "insufficient_evidence",
            "derivation": "median and nearest-rank p90 of complete observations; missing is never zero"}


def rate(numerator, denominator, unit, *, complete=True):
    return {"value": numerator / denominator if complete and denominator else None,
            "numerator": numerator, "denominator": denominator, "unit": unit,
            "status": "measured" if complete and denominator else "insufficient_evidence"}


def has_attributed_evidence(value):
    """Require both a named actor and retained reference for a human assertion."""
    return bool(value.get("evidence_reference") and value.get("actor"))


def project_key(declaration):
    return "|".join(str(declaration[k]) for k in
                    ("customer_id", "environment", "database_identity", "project_id"))


def _baseline(log, declaration):
    if not log:
        return {"adequate": False, "weekly_minutes": None, "reason": "approved baseline log missing"}
    start, end = instant(log["start"]), instant(log["end"])
    adopted = instant(log["adopted_at"])
    historical = log.get("matched_historical_event", {})
    historical_valid = (has_attributed_evidence(historical) and historical.get("same_coordinator") is True
                        and historical.get("comparable_revision") is True
                        and historical.get("justification")
                        and instant(historical["agreed_at"]) < adopted)
    representative = (log.get("complete_reporting_cycle") is True
                      and log.get("ordinary_work") is True and log.get("no_change_work") is True
                      and (log.get("substantive_revision") is True or historical_valid))
    entries = log.get("work", [])
    configured = set(declaration["previously_performed_artifacts"])
    valid_entries = (bool(entries) and all(has_attributed_evidence(e) and number(e.get("minutes"))
                     and e.get("category") in {"record_maintenance", "report_preparation"}
                     and (e["category"] != "report_preparation" or e.get("artifact_type") in configured)
                     for e in entries))
    adequate = bool(has_attributed_evidence(log) and log.get("approved_by") and start < end <= adopted
                    and instant(log["approved_at"]) <= adopted
                    and (end - start).total_seconds() >= 14 * 86400 and representative
                    and log.get("complete_work_log") is True and valid_entries)
    minutes = sum(e["minutes"] for e in entries) if valid_entries else None
    return {"adequate": adequate, "weekly_minutes": minutes * 7 * 86400 / (end-start).total_seconds()
            if adequate else None, "recorded_minutes": minutes, "evidence_reference": log.get("evidence_reference"),
            "start": log["start"], "end": log["end"], "representative": bool(representative),
            "matched_historical_event": historical or None,
            "extension": log.get("extension"), "work": entries,
            "reason": None if adequate else "baseline approval, floor, representative work or complete comparable log missing"}


def _work(row, attestation):
    entries = [o for o in row["observations"] if o["family"] in {"work_observation", "artifact_repair"}]
    categories = set(attestation.get("complete_time_categories", [])) if has_attributed_evidence(attestation) else set()
    # The declaration explicitly attests disjoint measured intervals. Otherwise
    # review/repair could already sit inside maintenance and be counted twice.
    disjoint = attestation.get("disjoint_time_entries") is True
    known = defaultdict(list)
    same, new, unavailable = [], [], []
    prior = set(row["declaration"]["previously_performed_artifacts"])
    for observation in entries:
        p = observation["payload"]
        category = p.get("category", "manual_repair")
        if category not in COORDINATOR_CATEGORIES + OPERATIONS_CATEGORIES:
            continue
        if not has_attributed_evidence(p) or not number(p.get("minutes")):
            unavailable.append(observation["event_id"])
            continue
        known[category].append(p["minutes"])
        if category in COORDINATOR_CATEGORIES:
            artifact = p.get("artifact_type")
            if category == "report_preparation" and not artifact:
                unavailable.append(observation["event_id"])
            elif artifact and artifact not in prior:
                new.append(p["minutes"])
            else:
                same.append(p["minutes"])
    complete = disjoint and not unavailable
    return {"same_work_minutes": sum(same) if complete and set(COORDINATOR_CATEGORIES) <= categories else None,
            "new_output_minutes": sum(new) if complete and set(COORDINATOR_CATEGORIES) <= categories else None,
            "review_minutes": sum(known["coordinator_review"]) if complete and "coordinator_review" in categories else None,
            "operations_minutes": sum(sum(known[k]) for k in OPERATIONS_CATEGORIES)
            if complete and set(OPERATIONS_CATEGORIES) <= categories else None,
            "attestation": attestation, "unavailable_entries": unavailable,
            "derivation": "sum disjoint logged intervals; all coordinator categories included; new artifact work separate"}


def _cohort_packets(rows):
    frozen = {}
    for row in sorted(rows, key=lambda r: instant(r["declaration"]["start"])):
        observations = {o["event_id"]: o for o in row["observations"]}
        for packet in row["packets"]:
            shown = observations.get(packet["event_id"], {}).get("payload", {})
            context = {key: shown.get(key) for key in (
                "accepted_revision_id", "observed_accepted_revision_id", "issue_profile_identity",
                "issue_profile_version", "issue_profile_sha256", "coverage_declaration_id")}
            key = digest({"project": project_key(row["declaration"]), "item_key": packet["item_key"],
                          "children": sorted(packet["child_ids"]), "context": context,
                          "interrupting": packet["interrupting"]})
            selected = frozen.setdefault(key, {**packet, "frozen_identity": key, "project_key": project_key(row["declaration"]), "exposures": [],
                                                "judgments": [], "reconstruction_readings": []})
            selected["exposures"].append({"period_id": row["declaration"]["period_id"],
                "event_id": packet["event_id"], "surfaced_at": packet["surfaced_at"], "outcomes": packet["outcomes"]})
            if type(packet.get("necessary")) is bool:
                selected["judgments"].append(packet["necessary"])
            selected["reconstruction_readings"].append(packet["manual_reconstruction"]["minutes"])
    # A judgment can occur after the initial week without another surfacing.
    # Join it only when the selected history contains exactly one matching
    # frozen question, or the observation explicitly names its frozen identity.
    for row in rows:
        invalid = set(row["sampling"]["invalid_observations"])
        for observation in row["observations"]:
            p = observation["payload"]
            if (observation["event_id"] in invalid or p.get("sample_kind") != "packet_usefulness"
                    or not has_attributed_evidence(p) or type(p.get("necessary")) is not bool):
                continue
            matches = [packet for key, packet in frozen.items()
                if packet["project_key"] == project_key(row["declaration"]) and packet["item_key"] == p.get("item_key")
                and (not p.get("frozen_packet_identity") or p["frozen_packet_identity"] == key)
                and instant(observation["occurred_at"]) >= instant(packet["surfaced_at"])]
            if len(matches) == 1:
                matches[0]["judgments"].append(p["necessary"])
    for packet in frozen.values():
        judgments = set(packet["judgments"])
        packet["necessary"] = next(iter(judgments)) if len(judgments) == 1 else None
        packet["conflicting_judgments"] = len(judgments) > 1
        readings = packet["reconstruction_readings"]
        packet["manual_reconstruction"] = {"minutes": sum(readings) if all(number(n) for n in readings) else None,
                                              "derivation": "sum recorded reconstruction across the frozen packet's exposures; missing remains unavailable"}
    return list(frozen.values())


def _packet_rates(packets, complete):
    interrupting = [p for p in packets if p["interrupting"]]
    def one(selected):
        result = rate(sum(p.get("necessary") is True for p in selected), len(selected), "packets",
                      complete=complete and all(type(p.get("necessary")) is bool for p in selected))
        result["unjudged"] = sum(p.get("necessary") is None for p in selected)
        result["derivation"] = "necessary at triage / every selected surfaced packet through close; open, ignored, superseded and deferred remain"
        return result
    return {"all_surfaced": one(packets), "interrupting": one(interrupting)}


def _latencies(children):
    keys = ("arrival_to_capture", "capture_to_delta", "delta_to_surfacing",
            "surfacing_to_decision", "decision_to_authorized_issue", "arrival_to_authorized_issue")
    result = {key: distribution([c.get("latency_seconds", {}).get(key) for c in children], len(children), "delta-seconds")
              for key in keys}
    arrival_to_delta, decision = [], []
    for child in children:
        timings = child.get("latency_seconds", {})
        a, c = timings.get("arrival_to_capture"), timings.get("capture_to_delta")
        arrival_to_delta.append(a + c if number(a) and number(c) else None)
        d, s = timings.get("delta_to_surfacing"), timings.get("surfacing_to_decision")
        decision.append(d + s if number(d) and number(s) else None)
    result["arrival_to_delta"] = distribution(arrival_to_delta, len(children), "delta-seconds")
    result["delta_to_decision"] = distribution(decision, len(children), "delta-seconds")
    return result


def _source_latencies(rows, calendar, history_rows):
    readings = []
    for row in rows:
        observations = row["observations"]
        arrivals = {}
        history = [o for r in history_rows if project_key(r["declaration"]) == project_key(row["declaration"]) for o in r["observations"]]
        for obs in sorted(history, key=lambda o: (o["occurred_at"], o["event_id"])):
            p = obs["payload"]
            if obs["family"] == "source_arrival" and p.get("source_identity"):
                arrivals.setdefault(p["source_identity"], obs)
        seen = set()
        for obs in observations:
            p = obs["payload"]
            if obs["family"] != "proposed_delta_creation" or p.get("outcome") not in {None, "created"}:
                continue
            if row["declaration"]["domain_receipts_complete"] and not p.get("receipt"):
                continue
            arrival = arrivals.get(p.get("source_identity"))
            for identity in p.get("delta_ids", [p.get("delta_id")]):
                if identity is None or identity in seen:
                    continue
                seen.add(identity)
                a = instant(arrival["occurred_at"]) if arrival else None
                d = instant(obs["occurred_at"])
                seconds = (d-a).total_seconds() if a and a <= d else None
                readings.append({"delta_id": identity, "period_id": row["declaration"]["period_id"],
                    "source_class": p.get("source_class"), "seconds": seconds,
                    "business_days": _business_days(a, d, calendar) if seconds is not None else None,
                    "arrival_event_id": arrival["event_id"] if arrival else None, "delta_event_id": obs["event_id"]})
    return {"calendar_seconds": distribution([r["seconds"] for r in readings], len(readings), "delta-seconds"),
            "business_days": distribution([r["business_days"] for r in readings], len(readings), "delta-business-days"),
            "calendar": calendar, "observations": readings}


def _business_days(start, end, calendar):
    if not calendar or calendar.get("rule") != "elapsed_weekdays_excluding_declared_holidays":
        return None
    zone = ZoneInfo(calendar["timezone"])
    holidays = set(calendar["holidays"])
    current, finish = start.astimezone(zone), end.astimezone(zone)
    total = 0.0
    while current < finish:
        next_midnight = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        stop = min(next_midnight, finish)
        if current.weekday() < 5 and current.date().isoformat() not in holidays:
            total += (stop-current).total_seconds() / 86400
        current = stop
    return total


def _packages(rows):
    releases = [r for row in rows for r in row["releases"]]
    result = rate(sum(r["client_ready"] is True for r in releases), len(releases), "prepared candidates",
                  complete=all(r["client_ready"] is not None for r in releases))
    result.update(candidates=releases,
        missing_artifact_candidates=sum(not r["configured_membership_matches"] for r in releases),
        mixed_or_unproven_revision_candidates=sum(not r["common_release_context"] for r in releases),
        stale_candidate_failures=[o["event_id"] for row in rows for o in row["observations"]
            if o["family"] in {"release_authorization", "preparation_attempt"}
            and "stale" in str(o["payload"].get("refusal_code", o["payload"].get("outcome", ""))).lower()],
        derivation="authorized client-ready candidates / every prepared candidate; all configured artifacts, one accepted revision")
    return result


def _aggregate(rows, work, baselines, calendar, history_rows):
    packets = _cohort_packets(rows)
    # Repeated presentations may duplicate a child; a delta is one diagnostic
    # within its project/period, never one count per containing packet.
    children = list({(project_key(row["declaration"]), row["declaration"]["period_id"], c["delta_id"]): c
                     for row in rows for c in row["children"]}.values())
    complete = all(row["interaction_capture_complete"] and row["cohort_status"] == "bound" for row in rows)
    weeks = len(rows)
    full_weeks = all(instant(r["declaration"]["end"]) - instant(r["declaration"]["start"]) == timedelta(days=7) for r in rows)
    costs = [float(row["provider_cost"]["actual_cost_usd"]) if row["provider_cost"]["actual_cost_usd"] is not None
             and work[row["declaration"]["period_id"]]["attestation"].get("all_provider_costs_complete") is True
             and has_attributed_evidence(work[row["declaration"]["period_id"]]["attestation"]) else None for row in rows]
    current = [work[row["declaration"]["period_id"]]["same_work_minutes"] for row in rows]
    baseline = [baselines[row["declaration"]["period_id"]]["weekly_minutes"] for row in rows]
    net = rate(sum(x for x in current if number(x)), sum(x for x in baseline if number(x)), "same-work minutes",
               complete=bool(rows) and full_weeks and all(number(x) for x in current + baseline))
    net.update(baseline_minutes=sum(x for x in baseline if number(x)),
               corridor_minutes=sum(x for x in current if number(x)),
               new_output_minutes=[work[row["declaration"]["period_id"]]["new_output_minutes"] for row in rows],
               derivation="Corridor comparable coordinator minutes / baseline weekly minutes for the same project-weeks; new output earns no savings")
    coverage = [c for row in rows for c in row["coverage"]]
    source_coverage = rate(sum(c["captured_within_window"] for c in coverage), sum(c["arrival_denominator"] for c in coverage),
                           "connected-channel source arrivals", complete=bool(rows) and all(c["population_complete"] for c in coverage))
    source_coverage["missing_or_late"] = [m for c in coverage for m in c["missing_or_late"]]
    resolved = [c for c in children if c["outcome"] in {"apply", "edit", "keep current"}]
    def acceptance(selected):
        return rate(sum(c["outcome"] == "apply" for c in selected), len(selected), "resolved deltas",
                    complete=all(c["decision_evidence_status"] == "receipt" for c in selected))
    interrupting = [p for p in packets if p["interrupting"]]
    result = {"project_week_denominator": weeks, "period_ids": [r["declaration"]["period_id"] for r in rows],
        "net_coordinator_time": net,
        "review_burden": distribution([work[r["declaration"]["period_id"]]["review_minutes"] for r in rows], weeks, "project-week minutes", complete=full_weeks),
        "operations_time": distribution([work[r["declaration"]["period_id"]]["operations_minutes"] for r in rows], weeks, "project-week minutes", complete=full_weeks),
        "provider_cost": distribution(costs, weeks, "USD/project-week", complete=full_weeks), "coverage": source_coverage,
        "packet_precision": _packet_rates(packets, complete), "frozen_packets": packets, "latency": _latencies(children),
        "routine_accept_without_edit": acceptance([c for c in resolved if not c["material_field"]]),
        "material_accept_without_edit": {field: acceptance([c for c in resolved if c["target_field"] == field])
            for field in sorted({f for r in rows for f in r["declaration"]["material_fields"]})},
        "child_usefulness": rate(sum(c.get("necessary") is True for c in children), len(children), "child deltas",
                                  complete=all(type(c.get("necessary")) is bool for c in children)),
        "packages": _packages(rows),
        "manual_reconstruction": distribution([p["manual_reconstruction"]["minutes"] for p in interrupting], len(interrupting), "interrupting-packet minutes"),
        "source_latency": _source_latencies(rows, calendar, history_rows),
        "source_class": {}, "configuration_bindings": [r["declaration"] for r in rows]}
    observed_classes = {(o["payload"].get("source_class") or "unattributed") for r in rows for o in r["observations"]}
    for source in sorted({c["source_class"] for c in children if c["source_class"]} | {c["source_class"] for c in coverage} | observed_classes):
        sc = [c for c in coverage if c["source_class"] == source]
        child = [c for c in children if c["source_class"] == source]
        result["source_class"][source] = {
            "coverage": rate(sum(c["captured_within_window"] for c in sc), sum(c["arrival_denominator"] for c in sc), "source arrivals", complete=all(c["population_complete"] for c in sc)),
            "latency": _latencies(child), "resolved_deltas": acceptance([c for c in child if c in resolved]),
            "material_fields": {field: acceptance([c for c in child if c in resolved and c["target_field"] == field])
                                for field in sorted({c["target_field"] for c in child if c["material_field"]})},
            "observations": [o for r in rows for o in r["observations"] if o["payload"].get("source_class", "unattributed") == source],
            "baseline_work": [entry for key in {r["declaration"]["period_id"] for r in rows}
                              for entry in baselines[key].get("work", []) if entry.get("source_class", "unattributed") == source],
            "time_by_category": {category: {
                "recorded_minutes": sum(o["payload"]["minutes"] for r in rows for o in r["observations"]
                    if o["family"] == "work_observation" and o["payload"].get("category") == category
                    and o["payload"].get("source_class", "unattributed") == source and number(o["payload"].get("minutes"))),
                "derivation": "only explicitly attributed logged minutes; shared or unknown source work remains unattributed"}
                for category in COORDINATOR_CATEGORIES + OPERATIONS_CATEGORIES},
            "cost": [b for r in rows for b in r["provider_cost"]["breakdown"] if b["source_class"] == source]}
    return result


def _validate_onboarding(cohort, by_id, live_ids):
    onboarding = cohort.get("onboarding_period_ids", [])
    if (len(set(onboarding)) != len(onboarding) or not set(onboarding) <= set(cohort["period_ids"])
            or set(onboarding) & set(live_ids)):
        raise ValueError("onboarding periods must belong to this cohort and be disjoint from live periods")
    projects = defaultdict(list)
    live_projects = defaultdict(list)
    for identity in live_ids:
        live_projects[project_key(by_id[identity]["declaration"])].append(by_id[identity]["declaration"])
    for identity in onboarding:
        d = by_id[identity]["declaration"]
        if project_key(d) not in live_projects:
            raise ValueError("onboarding project must also belong to the selected live cohort")
        if any(live["partner_id"] != d["partner_id"] for live in live_projects[project_key(d)]):
            raise ValueError("onboarding partner must match the selected live project")
        projects[project_key(d)].append(d)
    for key, periods in projects.items():
        ordered = sorted(periods, key=lambda d: instant(d["start"]))
        first_live = min(instant(d["start"]) for d in live_projects[key])
        if (any(instant(d["end"])-instant(d["start"]) != timedelta(days=7) for d in ordered)
                or any(instant(a["end"]) != instant(b["start"]) for a, b in zip(ordered, ordered[1:]))
                or instant(ordered[-1]["end"]) != first_live):
            raise ValueError("onboarding must be contiguous full weeks immediately preceding live observation")


def _validate_configuration(cohort, rows):
    by_project = defaultdict(list)
    for row in rows:
        by_project[project_key(row["declaration"])].append(row["declaration"])
    for key, declarations in by_project.items():
        configs = set()
        ordered = sorted(declarations, key=lambda d: instant(d["start"]))
        for d in ordered:
            configuration = {k: d[k] for k in ("issue_profile_identity", "issue_profile_version", "issue_profile_sha256",
                "required_artifacts", "previously_performed_artifacts", "quiet_max", "ordinary_max")}
            configuration["binding"] = {k: v for k, v in d["binding"].items() if k not in {"code_revision", "product_revision"}}
            configs.add(digest(configuration))
        if len(configs) > 1:
            raise ValueError("material configuration changes require separate cohorts")
        for before, after in zip(ordered, ordered[1:]):
            prior, current = before["binding"], after["binding"]
            if all(prior[k] == current[k] for k in ("code_revision", "product_revision")):
                continue
            changes = [change for change in cohort.get("nonstructural_changes", [])
                if change.get("project_key") == key and change.get("prior_code_revision") == prior["code_revision"]
                and change.get("code_revision") == current["code_revision"]
                and change.get("prior_product_revision") == prior["product_revision"]
                and change.get("product_revision") == current["product_revision"]]
            if not any(has_attributed_evidence(change) and change.get("classification") in {
                    "security_fix", "crash_fix", "data_loss_fix", "accessibility_fix", "behavior_restoring_fix"}
                    and change.get("affected_criteria") and change.get("reason")
                    and instant(before["end"]) <= instant(change["occurred_at"]) <= instant(after["start"])
                    for change in changes):
                raise ValueError("code revision changes require an attributed nonstructural change log or separate cohorts")


def derive_report(measurement, declaration, evidence):
    """Build the frozen #424 input to #498; never infer customer performance."""
    if measurement.get("schema_version") != "pilot-measurement-v1":
        raise ValueError("report requires the #532 measurement schema")
    if declaration.get("schema_version") != "pilot-contract-v1":
        raise ValueError("report requires a predeclared pilot contract")
    declared = instant(declaration["declared_at"])
    if declaration.get("packet_precision_basis") not in {"all_surfaced", "interrupting"}:
        raise ValueError("predeclare all_surfaced or interrupting packet precision; never silently change denominators")
    by_id = {r["declaration"]["period_id"]: r for r in measurement["periods"]}
    if len(by_id) != len(measurement["periods"]):
        raise ValueError("duplicate measurement period")
    assigned = [p for c in declaration["cohorts"] for p in c["period_ids"]]
    if len(set(assigned)) != len(assigned) or set(assigned) != set(by_id):
        raise ValueError("every measured period must belong to exactly one cohort")
    cohort_ids = [c["id"] for c in declaration["cohorts"]]
    if len(set(cohort_ids)) != len(cohort_ids):
        raise ValueError("duplicate cohort identity")
    baselines = {}
    logs = evidence.get("baselines", [])
    if len({b["project_key"] for b in logs}) != len(logs):
        raise ValueError("one approved baseline per exact project origin")
    attestations = evidence.get("period_attestations", {})
    work = {}
    for row in by_id.values():
        d = row["declaration"]
        if declared > instant(d["start"]):
            raise ValueError("pilot contract must predate measured periods")
        key = project_key(d)
        baselines[d["period_id"]] = {"project_key": key, **_baseline(next((b for b in logs if b["project_key"] == key), None), d)}
        work[d["period_id"]] = _work(row, attestations.get(d["period_id"], {}))
    cohorts = []
    for cohort in declaration["cohorts"]:
        live_ids = cohort.get("live_period_ids", cohort["period_ids"])
        if len(set(live_ids)) != len(live_ids) or not set(live_ids) <= set(cohort["period_ids"]):
            raise ValueError("live measurement periods must be unique members of their cohort")
        rows = [by_id[p] for p in live_ids]
        _validate_onboarding(cohort, by_id, live_ids)
        _validate_configuration(cohort, rows)
        partners = {}
        for partner in sorted({r["declaration"]["partner_id"] for r in rows} | set(declaration.get("shape", {}).get("partners", {}))):
            owned = [r for r in rows if r["declaration"]["partner_id"] == partner]
            partners[partner] = {"pooled": _aggregate(owned, work, baselines, declaration.get("business_calendar"), measurement["periods"]),
                "volume_strata": {s: _aggregate([r for r in owned if r["volume_stratum"] == s], work, baselines, declaration.get("business_calendar"), measurement["periods"]) for s in VOLUME_STRATA},
                "revision_slices": [{"code_revision": code, "product_revision": product,
                    "measures": _aggregate([r for r in owned if (r["declaration"]["binding"]["code_revision"], r["declaration"]["binding"]["product_revision"]) == (code, product)],
                        work, baselines, declaration.get("business_calendar"), measurement["periods"])}
                    for code, product in sorted({(r["declaration"]["binding"]["code_revision"], r["declaration"]["binding"]["product_revision"]) for r in owned})]}
        cohorts.append({"declaration": cohort, "partners": partners})
    samples = {}
    native_populations = {}
    canonical_classes = {"matrix": "ucm_revision", "schedule": "schedule_export", "ucm_revision": "ucm_revision",
                         "schedule_export": "schedule_export", "email": "email", "minutes": "minutes"}
    for cohort in declaration["cohorts"]:
        population = {}
        for identity in cohort.get("live_period_ids", cohort["period_ids"]):
            row = by_id[identity]
            d = row["declaration"]
            for obs in row["observations"]:
                p = obs["payload"]
                if obs["family"] != "source_arrival" or not p.get("source_identity") or p.get("outcome") == "replayed":
                    continue
                if d["domain_receipts_complete"] and not p.get("receipt"):
                    continue
                if p.get("source_class") not in canonical_classes:
                    continue
                key = project_key(d) + "|" + p["source_identity"]
                population[key] = {"id": key, "partner": d["partner_id"], "week": row["project_week"],
                                   "source_class": canonical_classes[p["source_class"]]}
        native_populations[cohort["id"]] = sorted(population.values(), key=lambda p: p["id"])
    for sample in evidence.get("material_sampling", []):
        cohort = sample["cohort_id"]
        if cohort not in cohort_ids or cohort in samples:
            raise ValueError("material sample requires one exact declared cohort")
        if sorted(sample["arrivals"], key=lambda p: p["id"]) != native_populations[cohort]:
            raise ValueError("material sample must use the complete native cohort arrival population")
        policy = ComparisonPolicy(**sample["policy"])
        if sample.get("declared_at") is None or instant(sample["declared_at"]) > min(
            instant(by_id[p]["declaration"]["start"]) for c in declaration["cohorts"] if c["id"] == cohort for p in c["period_ids"]):
            raise ValueError("material sample seed and policy must predate the measured cohort")
        if policy.minimum_material_cases < 30:
            raise ValueError("pilot material-change minimum cannot be less than 30")
        samples[cohort] = assess_material_sample(sample["arrivals"], policy=policy, inspections=sample["inspections"])
        member_ids = next(c.get("live_period_ids", c["period_ids"]) for c in declaration["cohorts"] if c["id"] == cohort)
        if any(not by_id[p]["declaration"]["source_population_complete"] for p in member_ids):
            for finding in samples[cohort]["partners"].values():
                finding.update(status="insufficient_evidence", material_miss_rate=None,
                               evidence_limit="connected-channel census is incomplete")
    snapshot = {"measurement": measurement, "declaration": declaration, "evidence": evidence}
    return {"schema_version": "pilot-report-v1", "input_sha256": digest(snapshot),
            "measurement_sha256": measurement["input_sha256"], "declaration": declaration,
            "baselines": baselines, "work": work, "cohorts": cohorts, "material_sampling": samples,
            "periods": measurement["periods"], "evidence": evidence, "native_sampling_populations": native_populations,
            "limits": ["Working-reference agreement is not independently adjudicated accuracy.",
                       "Fixture evidence proves software only; no customer outcome or authority follows.",
                       "Time completeness and baseline logs require the named human's approved evidence."]}
