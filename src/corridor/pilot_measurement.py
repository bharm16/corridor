"""Read product/pilot measurements from events and immutable receipts (#532).

#558 gave each built interaction one versioned event, but counting only saved
packets would quietly discard every interruption nobody answered. This reader
starts from presentations and retains their exact children through a declared
period close. It never writes a workflow, decision, record, or release.

Periods and sampling observations are declared analytical inputs, not product
authority. Missing timing, cost, capture or cohort evidence stays unavailable.
The report is input to #424 and #498; a fixture proves the software and cannot
establish customer savings or a pilot verdict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Iterable
from collections import Counter
from decimal import Decimal, InvalidOperation
import math

from corridor.analytics import AnalyticsBinding, AnalyticsEvent, EventFamily
from corridor.measurement_collection import TIME_CATEGORIES, select_sampling_observations
from corridor.project_portfolio import NO_ACTION


REPORT_VERSION = "pilot-measurement-v1"


@dataclass(frozen=True)
class MeasurementPeriod:
    """A predeclared project observation window and the cohort it measures.

    Windows are half-open [start, end). Every material configuration change
    needs another window. Identity is structured customer data, never a metric
    label. Retention and access names identify the customer's existing policy.
    """

    period_id: str
    project_id: int
    partner_id: str
    customer_id: str
    environment: str
    database_identity: str
    start: datetime
    end: datetime
    declared_at: datetime
    binding: AnalyticsBinding
    issue_profile_identity: str
    issue_profile_version: int
    issue_profile_sha256: str
    required_artifacts: tuple[str, ...]
    previously_performed_artifacts: tuple[str, ...]
    quiet_max: int
    ordinary_max: int
    source_population_complete: bool
    interaction_capture_complete: bool
    retention_policy: str
    access_policy: str
    domain_receipts_complete: bool = False
    capture_window_seconds: int = 86400
    material_fields: tuple[str, ...] = (
        "external_org", "utility_id", "conflict_description", "committed_date", "need_date",
        "closure_result", "applies_to", "agreement_status", "permit_status", "cost_responsibility",
        "utility_owner", "conflict_identity", "promised_for", "required_by", "closure",
    )

    def __post_init__(self) -> None:
        for instant in (self.start, self.end, self.declared_at):
            if instant.tzinfo is None:
                raise ValueError("measurement periods require timezone-aware instants")
        if self.start >= self.end or self.declared_at > self.start:
            raise ValueError("periods and volume boundaries must be declared before measurement")
        if (self.start.astimezone(timezone.utc).isocalendar()[:2]
                != (self.end - timedelta(microseconds=1)).astimezone(timezone.utc).isocalendar()[:2]):
            raise ValueError("periods must fit within one UTC project-week; split weeks into separate periods")
        if not 0 <= self.quiet_max < self.ordinary_max:
            raise ValueError("quiet and ordinary boundaries must be increasing counts")
        if self.capture_window_seconds < 1:
            raise ValueError("the capture latency window must be positive")
        if not self.retention_policy or not self.access_policy or not self.partner_id:
            raise ValueError("customer identity and retention/access policies are required")
        if not self.customer_id or not self.environment or not self.database_identity:
            raise ValueError("measurement requires customer, environment and database origin")
        if ("updated_ucm" not in self.required_artifacts
                or len(set(self.required_artifacts)) != len(self.required_artifacts)):
            raise ValueError("configured artifact obligation requires exactly one updated UCM")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MeasurementPeriod:
        fields = dict(value)
        for name in ("start", "end", "declared_at"):
            fields[name] = datetime.fromisoformat(fields[name])
        fields["binding"] = AnalyticsBinding.from_dict(fields["binding"])
        for name in ("required_artifacts", "previously_performed_artifacts", "material_fields"):
            if name not in fields:
                continue
            fields[name] = tuple(fields[name])
        return cls(**fields)

    def as_dict(self) -> dict[str, Any]:
        fields = asdict(self)
        fields["binding"] = self.binding.as_dict()
        for name in ("start", "end", "declared_at"):
            fields[name] = getattr(self, name).isoformat()
        return fields


def derive_measurement(
    periods: Iterable[MeasurementPeriod], events: Iterable[AnalyticsEvent]
) -> dict[str, Any]:
    """Produce a reproducible read-only report with independent denominator units."""

    periods = sorted(periods, key=lambda p: (_period_origin(p), p.project_id, p.start, p.period_id))
    for left, right in zip(periods, periods[1:]):
        if _period_origin(left) == _period_origin(right) and left.project_id == right.project_id and right.start < left.end:
            raise ValueError("measurement periods for one project must not overlap")
    if len({p.period_id for p in periods}) != len(periods):
        raise ValueError("measurement period identities must be unique")
    customer_by_database: dict[tuple, str] = {}
    for p in periods:
        key = (p.environment, p.database_identity)
        if key in customer_by_database and customer_by_database[key] != p.customer_id:
            raise ValueError("one database origin cannot be relabeled as another customer")
        customer_by_database[key] = p.customer_id
    unique: dict[tuple, AnalyticsEvent] = {}
    for event in events:
        if event.occurred_at.tzinfo is None:
            raise ValueError("measurement events require timezone-aware instants")
        if event.version not in {"1.0", "1.1"}:
            raise ValueError("unsupported measurement event version")
        origin = _event_origin(event)
        event_key = (*origin, event.event_id)
        if (origin[1], origin[2]) in customer_by_database and customer_by_database[(origin[1], origin[2])] != origin[0]:
            raise ValueError("event customer does not match the declared database origin")
        if origin not in {_period_origin(p) for p in periods}:
            raise ValueError("event database origin has no declared customer measurement period")
        if event_key in unique and event.as_dict() != unique[event_key].as_dict():
            raise ValueError("conflicting copies of one event identity")
        unique[event_key] = event
    ordered = sorted(unique.values(), key=lambda e: (_event_origin(e), e.occurred_at, e.event_id))
    return {
        "schema_version": REPORT_VERSION,
        "input_sha256": _digest({
            "periods": [p.as_dict() for p in periods],
            "events": [e.as_dict() for e in ordered],
        }),
        "customer_findings": "insufficient_evidence",
        "purpose": "measurement inputs for #424/#498; not a customer outcome verdict",
        "periods": [_period_report(p, ordered) for p in periods],
    }


def _period_report(period: MeasurementPeriod, events: list[AnalyticsEvent]) -> dict[str, Any]:
    observed = [e for e in events if _event_origin(e) == _period_origin(period)
               and _applies_to_project(e, period.project_id)
               and e.occurred_at < period.end]
    # In a verified DB export, a successful log emitted before a rolled-back
    # transaction cannot establish a decision, source, request or release.
    # Logs remain interaction evidence in observations and attempt counts.
    history = [e for e in observed if not period.domain_receipts_complete
               or e.family not in DOMAIN_FAMILIES or e.payload.get("receipt")]
    relevant = [e for e in history if period.start <= e.occurred_at]
    sample_selection = select_sampling_observations(history)
    valid_samples = [e for e in sample_selection.valid if e.occurred_at >= period.start]
    invalid_samples = [e for e in sample_selection.invalid if e.occurred_at >= period.start]
    problems = _cohort_problems(period, relevant)
    surfaced: dict[str, AnalyticsEvent] = {}
    for event in relevant:
        if event.family == EventFamily.PACKET_SURFACING:
            surfaced.setdefault(event.payload["item_key"], event)
    packets: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    for key, shown in surfaced.items():
        opened = [e for e in relevant if e.family == EventFamily.PACKET_OPENING
                  and e.payload.get("item_key") == key]
        presentations = [e for e in relevant if e.family == EventFamily.PACKET_SURFACING
                         and e.payload.get("item_key") == key]
        child_ids = sorted({c["delta_id"] for e in presentations
                            for c in e.payload.get("child_consequences", [])})
        packet_children = [_child(period, key, i, history) for i in child_ids]
        for child in packet_children:
            child["latency_seconds"] = _latencies(child, shown, history)
            child["material_field"] = child["target_field"] in period.material_fields
        packets.append({
            "item_key": key, "surfaced_at": shown.occurred_at.isoformat(),
            "event_id": shown.event_id, "child_ids": child_ids,
            "presentation_event_ids": [e.event_id for e in presentations],
            "opened": bool(opened),
            "outcomes": sorted({c["outcome"] for c in packet_children}) or ["ignored/open"],
            "interrupting": shown.payload.get("consequence_level") == "must_handle_before_issue",
        })
        children.extend(packet_children)
    arrivals = _source_events(relevant, EventFamily.SOURCE_ARRIVAL)
    captures = _source_events(history, EventFamily.SOURCE_CAPTURE)
    captured_count = len(set(arrivals) & set(captures))
    stratum = ("unavailable" if not period.source_population_complete else
               "quiet" if captured_count <= period.quiet_max else
               "ordinary" if captured_count <= period.ordinary_max else "burst")
    releases = _releases(period, relevant, history, sample_selection.valid)
    sampling = _sampling(valid_samples, invalid_samples, children)
    for packet in packets:
        judges = [e for e in valid_samples if e.payload.get("sample_kind") == "packet_usefulness"
                  and e.payload.get("item_key") == packet["item_key"]]
        packet["necessary"] = judges[0].payload.get("necessary") if judges else None
        packet["manual_reconstruction"] = _time_reading([
            e for e in relevant if e.payload.get("item_key") == packet["item_key"]
        ], "manual_reconstruction")
    return {
        "declaration": period.as_dict(), "packets": packets, "children": children,
        "project_week": _week(period.start),
        "packet_denominator": len(packets),
        "interrupting_packet_denominator": sum(p["interrupting"] for p in packets),
        "necessary_interrupting_packets": sum(p["interrupting"] and p["necessary"] is True for p in packets),
        "unjudged_interrupting_packets": sum(p["interrupting"] and p["necessary"] is None for p in packets),
        "child_diagnostic_denominator": len(children),
        "cohort_status": "insufficient_evidence" if problems else "bound",
        "cohort_problems": problems,
        "volume_stratum": stratum,
        "captured_source_arrivals": captured_count,
        "time": {category: _time_reading(relevant, category) for category in TIME_CATEGORIES},
        "interaction_counts": _interaction_counts([e for e in observed if e.occurred_at >= period.start]),
        "committed_decision_count": len({_logical_key(e) for e in relevant
                                         if e.family == EventFamily.CHILD_DECISION
                                         and e.payload.get("receipt")
                                         and e.payload.get("outcome") in {"saved", "resolved", "deferred"}}),
        "domain_receipt_coverage": "complete export" if period.domain_receipts_complete else "not asserted; event-only outcomes are not proof of commit",
        "interaction_capture_complete": period.interaction_capture_complete,
        "coverage": _coverage(period, arrivals, captures),
        "preparations": _preparations(relevant, history),
        "releases": releases,
        "prepared_candidate_denominator": len(releases),
        "client_ready_candidate_numerator": sum(r["client_ready"] is True for r in releases),
        "provider_cost": _provider_cost(relevant),
        "sampling": sampling,
        "portfolio": _portfolio(period, events),
        "observations": [_observation(period, e) for e in observed if e.occurred_at >= period.start],
        "time_saved_minutes": None,
        "time_saved_reason": "#424 needs matched partner work and complete recorded time; no savings inferred",
    }


_OUTCOMES = {
    "accept": "apply", "apply": "apply", "edit_and_apply": "edit", "edit": "edit",
    "reject": "keep current", "keep_current": "keep current", "defer": "deferred",
    "needs_coordination": "needs coordination",
}
DOMAIN_FAMILIES = frozenset({
    EventFamily.SOURCE_ARRIVAL, EventFamily.SOURCE_CAPTURE, EventFamily.PROPOSED_DELTA_CREATION,
    EventFamily.CHILD_DECISION, EventFamily.DELTA_SUPERSESSION, EventFamily.PACKET_SAVE,
    EventFamily.FOLLOW_UP_PLAN_CREATION, EventFamily.COVERAGE_CONFIRMATION,
    EventFamily.PREPARATION_REQUEST, EventFamily.PREPARATION_ATTEMPT,
    EventFamily.RELEASE_CANDIDATE_PREPARATION, EventFamily.RELEASE_AUTHORIZATION,
})


def _child(period: MeasurementPeriod, key: str, delta_id: int,
           history: list[AnalyticsEvent]) -> dict[str, Any]:
    decisions = [e for e in history if e.family == EventFamily.CHILD_DECISION
                 and e.payload.get("delta_id") == delta_id
                 and e.payload.get("outcome") in {"saved", "resolved", "deferred"}]
    supersessions = [e for e in history if e.family == EventFamily.DELTA_SUPERSESSION
                    and e.payload.get("prior_delta_id") == delta_id]
    creations = [e for e in history if e.family == EventFamily.PROPOSED_DELTA_CREATION
                 and e.payload.get("outcome") in {None, "created"} and not e.payload.get("refusal_code")
                 and (e.payload.get("delta_id") == delta_id
                      or delta_id in e.payload.get("delta_ids", []))]
    last = decisions[-1] if decisions else None
    outcome = _OUTCOMES.get(str(last.payload.get("action")), "ignored/open") if last else "ignored/open"
    if supersessions and (last is None or supersessions[-1].occurred_at > last.occurred_at):
        outcome = "superseded"
    origin = creations[0].payload if creations else {}
    return {
        "item_key": key, "delta_id": delta_id, "outcome": outcome,
        "source_class": origin.get("source_class", origin.get("source_family")),
        "target_field": origin.get("target_field"),
        "decision_at": last.occurred_at.isoformat() if last else None,
        "decision_evidence": _reference(last) if last else None,
        "decision_evidence_status": ("receipt" if last and last.payload.get("receipt") else
                                     "event only" if last else "unavailable"),
        "supersession_evidence": _reference(supersessions[-1]) if supersessions else None,
        "created_at": creations[0].occurred_at.isoformat() if creations else None,
    }


def _interaction_counts(events: list[AnalyticsEvent]) -> dict[str, int]:
    families = Counter(e.family.value for e in events)
    return {
        "project_openings": families[EventFamily.PROJECT_OPENING.value],
        "portfolio_project_selections": families[EventFamily.PROJECT_SELECTION.value],
        "packet_presentations": families[EventFamily.PACKET_SURFACING.value],
        "packet_openings": families[EventFamily.PACKET_OPENING.value],
        "evidence_openings": families[EventFamily.EVIDENCE_OPENING.value],
        "child_decisions": len({_logical_key(e) for e in events
                                if e.family == EventFamily.CHILD_DECISION
                                and e.payload.get("outcome") in {"saved", "resolved", "deferred"}}),
        "packet_saves": len({_logical_key(e) for e in events
                             if e.family == EventFamily.PACKET_SAVE
                             and e.payload.get("outcome") == "saved"}),
        "bounded_interactions": len({_logical_key(e) for e in events if e.family in (
            EventFamily.PROJECT_OPENING, EventFamily.PACKET_OPENING, EventFamily.EVIDENCE_OPENING,
            EventFamily.COVERAGE_CONFIRMATION, EventFamily.PREPARATION_REQUEST,
            EventFamily.PACKET_SAVE, EventFamily.RELEASE_AUTHORIZATION)}),
    }


def _logical_key(event: AnalyticsEvent) -> tuple:
    p = event.payload
    if event.family == EventFamily.CHILD_DECISION:
        return (event.family.value, p.get("delta_id"), _OUTCOMES.get(str(p.get("action"))), event.occurred_at)
    for key in ("request_id", "coverage_declaration_id", "package_identity"):
        if p.get(key) is not None:
            return event.family.value, key, p[key], event.occurred_at
    return (event.family.value, p.get("receipt_id", event.event_id))


def _reference(event: AnalyticsEvent) -> dict[str, Any]:
    return {"event_id": event.event_id, "event_version": event.version,
            "receipt": event.payload.get("receipt")}


def _cohort_problems(period: MeasurementPeriod, events: list[AnalyticsEvent]) -> list[str]:
    problems: set[str] = set()
    expected = period.binding.as_dict()
    expected.update(issue_profile_identity=period.issue_profile_identity,
                    issue_profile_version=period.issue_profile_version,
                    issue_profile_sha256=period.issue_profile_sha256)
    expected.update(customer_id=period.customer_id, environment=period.environment,
                    database_identity=period.database_identity)
    placeholders = {"git:current", "default-ucm-v1", "mapping-v1", "packetizer-v1"}
    for event in events:
        for key in event.payload.get("unavailable_binding_fields", []):
            problems.add(f"{key} was not retained on {_reference(event)}")
        if event.payload.get("customer_binding_verified") is False:
            problems.add("customer association unavailable on the database receipts; requires customer routing evidence")
        actual = event.binding.as_dict()
        for key in ("issue_profile_identity", "issue_profile_version", "issue_profile_sha256",
                    "template_identity", "mapping_identity"):
            if key in event.payload:
                actual[key] = event.payload[key]
        if event.family == EventFamily.PORTFOLIO_READING:
            row = next(p for p in event.payload["projects"] if p["project_id"] == period.project_id)
            context = row.get("measurement_context") or {}
            # A process-wide issue profile/template cannot describe several
            # projects. Missing project context remains missing even when the
            # enclosing event happened to carry a populated global binding.
            for key in ("issue_profile_identity", "issue_profile_version", "issue_profile_sha256",
                        "template_identity", "mapping_identity"):
                actual[key] = context.get(key)
        for key in ("source_configuration", "connector_configuration"):
            if event.payload.get("receipt") and event.payload.get(key):
                for name, retained in event.payload[key].items():
                    if name not in expected[key]:
                        problems.add(f"retained {key}.{name} is not bound by period {period.period_id}")
                    elif expected[key][name] != retained:
                        raise ValueError(f"cohort change in retained {key}.{name}; declare a separate measurement period")
        for key, value in expected.items():
            observed = actual.get(key)
            if key != "enabled_feature_flags" and (observed in (None, "", {})
                    or isinstance(observed, str) and observed in placeholders):
                problems.add(f"{key} unavailable on {event.event_id}")
            elif observed != value:
                raise ValueError(f"cohort change in {key} on {event.event_id}; declare a separate measurement period")
    return sorted(problems)


def _observation(period: MeasurementPeriod, event: AnalyticsEvent) -> dict[str, Any]:
    return {"project_id": period.project_id, "partner_id": period.partner_id,
            "period_id": period.period_id, "week": _week(event.occurred_at),
            **event.as_dict()}


def _period_origin(period: MeasurementPeriod) -> tuple[str, str, str]:
    return period.customer_id, period.environment, period.database_identity


def _applies_to_project(event: AnalyticsEvent, project_id: int) -> bool:
    if event.family == EventFamily.PORTFOLIO_READING:
        return any(p.get("project_id") == project_id for p in event.payload.get("projects", []))
    return event.payload.get("project_id") == project_id


def _event_origin(event: AnalyticsEvent) -> tuple[str, str, str]:
    b = event.binding
    if not b.customer_id or not b.environment or not b.database_identity:
        raise ValueError("event customer/environment/database origin is unavailable; refusing an ambiguous project ID")
    if event.payload.get("customer_id") is not None and event.payload["customer_id"] != b.customer_id:
        raise ValueError("event payload customer conflicts with its database origin")
    return b.customer_id, b.environment, b.database_identity


def _week(at: datetime) -> str:
    year, week, _ = at.astimezone(timezone.utc).isocalendar()
    return f"{year}-W{week:02}"


def _source_key(event: AnalyticsEvent) -> str | None:
    return event.payload.get("source_identity") or (
        event.payload.get("content_sha256")
        if event.family in {EventFamily.SOURCE_ARRIVAL, EventFamily.SOURCE_CAPTURE}
        else event.payload.get("source_sha256")
    )


def _source_events(events: list[AnalyticsEvent], family: EventFamily) -> dict[str, AnalyticsEvent]:
    values: dict[str, AnalyticsEvent] = {}
    for event in events:
        if event.payload.get("outcome") == "replayed":
            continue
        if family == EventFamily.SOURCE_CAPTURE:
            outcome = event.payload.get("outcome")
            if outcome not in {None, "captured"} or (event.payload.get("receipt") and outcome != "captured"):
                continue
        if event.family == family and (key := _source_key(event)):
            values.setdefault(key, event)
    return values


def _coverage(period, arrivals, captures) -> list[dict[str, Any]]:
    classes = sorted({e.payload.get("source_class", "unavailable") for e in arrivals.values()})
    rows = []
    for source_class in classes:
        members = [e for e in arrivals.values() if e.payload.get("source_class", "unavailable") == source_class]
        on_time, missing = [], []
        for arrival in members:
            capture = captures.get(_source_key(arrival))
            delay = _seconds(arrival.occurred_at, capture.occurred_at if capture else None)
            if delay is not None and delay <= period.capture_window_seconds:
                on_time.append(arrival.event_id)
            else:
                missing.append({"source_identity": _source_key(arrival),
                                "reason": arrival.payload.get("refusal_reason")
                                or ("late capture" if delay is not None else "capture not evidenced"),
                                "arrival_evidence": _reference(arrival)})
        rows.append({"source_class": source_class, "arrival_denominator": len(members),
                     "captured_within_window": len(on_time), "missing_or_late": missing,
                     "population_complete": period.source_population_complete,
                     "coverage_rate": len(on_time) / len(members) if period.source_population_complete else None})
    return rows


def _latencies(child, shown, events) -> dict[str, float | None]:
    created = next((e for e in events if e.family == EventFamily.PROPOSED_DELTA_CREATION
                    and e.payload.get("outcome") in {None, "created"} and not e.payload.get("refusal_code")
                    and (e.payload.get("delta_id") == child["delta_id"]
                         or child["delta_id"] in e.payload.get("delta_ids", []))), None)
    key = (_source_key(created) if created else None) or ""
    arrival = _source_events(events, EventFamily.SOURCE_ARRIVAL).get(key)
    capture = _source_events(events, EventFamily.SOURCE_CAPTURE).get(key)
    decisions = [e for e in events if e.family == EventFamily.CHILD_DECISION
                 and e.payload.get("delta_id") == child["delta_id"]
                 and e.payload.get("outcome") in {"saved", "resolved"}
                 and e.payload.get("action") not in {"defer", "needs_coordination"}]
    decision = decisions[-1] if decisions else None
    authorized = next((e for e in events if _authorized(e) and decision
                       and e.occurred_at >= decision.occurred_at
                       and e.payload.get("accepted_revision_id") is not None
                       and decision.payload.get("revision_id") is not None
                       and e.payload["accepted_revision_id"] >= decision.payload["revision_id"]), None)
    a, c, d, h, r = [one.occurred_at if one else None for one in (arrival, capture, created, decision, authorized)]
    return {"arrival_to_capture": _seconds(a, c), "capture_to_delta": _seconds(c, d),
            "delta_to_surfacing": _seconds(d, shown.occurred_at),
            "surfacing_to_decision": _seconds(shown.occurred_at, h),
            "decision_to_authorized_issue": _seconds(h, r),
            "arrival_to_authorized_issue": _seconds(a, r)}


def _seconds(start, finish) -> float | None:
    if start is None or finish is None or finish < start:
        return None
    return (finish - start).total_seconds()


def _time_reading(events, category) -> dict[str, Any]:
    entries = [e for e in events if e.family in {EventFamily.WORK_OBSERVATION, EventFamily.ARTIFACT_REPAIR}
               and e.payload.get("category", "manual_repair") == category]
    known, unavailable = [], []
    for event in entries:
        value = event.payload.get("minutes")
        if (event.payload.get("actor") and event.payload.get("evidence_reference")
                and isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            known.append(value)
        else:
            unavailable.append(event.event_id)
    complete = bool(entries) and not unavailable
    return {"minutes": sum(known) if complete else None, "recorded_minutes": sum(known),
            "status": "recorded" if complete else "unavailable", "entries": len(entries),
            "unavailable_entries": unavailable,
            "evidence": [_reference(e) for e in entries]}


def _authorized(event: AnalyticsEvent) -> bool:
    return (event.family == EventFamily.RELEASE_AUTHORIZATION and bool(event.payload.get("package_identity"))
            and event.payload.get("status", event.metric_labels.get("status")) not in {"refused", "failed"})


def _same_candidate(left: dict, right: dict) -> bool:
    return any(left.get(key) is not None and left.get(key) == right.get(key)
               for key in ("candidate_id", "candidate_identity"))


def _releases(period, relevant, history, valid_samples) -> list[dict[str, Any]]:
    candidates: dict[Any, AnalyticsEvent] = {}
    for event in relevant:
        if event.family != EventFamily.RELEASE_CANDIDATE_PREPARATION:
            continue
        p = event.payload
        key = p.get("candidate_identity") or p.get("candidate_id")
        if key and not p.get("refusal_code"):
            # A retained receipt enriches an emitted event; it never creates a
            # second candidate denominator on a replay or on a log/receipt join.
            prior = candidates.get(key)
            if prior is None or p.get("receipt"):
                candidates[key] = event
    rows = []
    for candidate in candidates.values():
        p = candidate.payload
        authorizations = [e for e in history if _authorized(e) and _same_candidate(p, e.payload)]
        required = p.get("configured_artifact_types", [])
        artifacts = p.get("artifacts", [])
        actual = [a["artifact_type"] for a in artifacts]
        membership_matches = (set(required) == set(period.required_artifacts)
                              and sorted(actual) == sorted(required)
                              and all(a.get("content_sha256") for a in artifacts))
        authorization = authorizations[0] if authorizations else None
        context_matches = bool(authorization and p.get("accepted_revision_id") is not None
                               and authorization.payload.get("accepted_revision_id") == p["accepted_revision_id"])
        readiness = [e for e in valid_samples if e.payload.get("sample_kind") == "release_readiness"
                     and e.occurred_at >= candidate.occurred_at
                     and _same_candidate(p, e.payload)]
        repairs = [e for e in history if (e.family == EventFamily.ARTIFACT_REPAIR
                   or (e.family == EventFamily.WORK_OBSERVATION
                       and e.payload.get("category") in {"manual_repair", "manual_reconstruction"}))
                   and e.occurred_at >= candidate.occurred_at and _same_candidate(p, e.payload)]
        judged = readiness[-1].payload if readiness else {}
        client_ready = (False if repairs or judged.get("client_ready") is False
                        or judged.get("manual_repair_required") is True else
                        True if judged.get("client_ready") is True
                        and judged.get("manual_repair_required") is False
                        and membership_matches and context_matches else None)
        rows.append({"candidate_id": p.get("candidate_id"), "candidate_identity": p.get("candidate_identity"),
                     "prepared_at": candidate.occurred_at.isoformat(), "accepted_revision_id": p.get("accepted_revision_id"),
                     "coverage_declaration_id": p.get("coverage_declaration_id"),
                     "required_artifacts": required, "artifacts": artifacts,
                     "configured_membership_matches": membership_matches,
                     "common_release_context": context_matches,
                     "authorized_package_identity": authorization.payload.get("package_identity") if authorization else None,
                     "client_ready": client_ready,
                     "manual_repair": _time_reading(repairs, "manual_repair"),
                     "manual_reconstruction": _time_reading(repairs, "manual_reconstruction"),
                     "savings_eligible_artifacts": sorted(set(required) & set(period.previously_performed_artifacts)),
                     "evidence": _reference(candidate)})
    return rows


def _preparations(relevant, history) -> list[dict[str, Any]]:
    requests = {e.payload["request_id"]: e for e in relevant
                if e.family == EventFamily.PREPARATION_REQUEST and e.payload.get("request_id")}
    rows = []
    for request_id, request in requests.items():
        attempts = [e for e in history if e.family == EventFamily.PREPARATION_ATTEMPT
                    and e.payload.get("request_id") == request_id]
        attempt = attempts[-1] if attempts else None
        candidate = next((e for e in history if e.family == EventFamily.RELEASE_CANDIDATE_PREPARATION
                          and attempt and _same_candidate(e.payload, attempt.payload)), None)
        rows.append({"request_id": request_id, "requested_at": request.occurred_at.isoformat(),
                     "coverage_declaration_id": request.payload.get("coverage_declaration_id"),
                     "issue_profile_identity": request.payload.get("issue_profile_identity"),
                     "issue_profile_version": request.payload.get("issue_profile_version"),
                     "issue_profile_sha256": request.payload.get("issue_profile_sha256"),
                     "outcome": attempt.payload.get("outcome") if attempt else "open",
                     "started_at": attempt.payload.get("started_at") if attempt else None,
                     "attempt_id": attempt.payload.get("attempt_id") if attempt else None,
                     "candidate_id": attempt.payload.get("candidate_id") if attempt else None,
                     "request_to_candidate_seconds": _seconds(
                         request.occurred_at,
                         attempt.occurred_at if attempt and attempt.payload.get("outcome") == "prepared" and candidate else None),
                     "annotation_count": request.payload.get("annotation_count"),
                     "unchanged_declaration_reused": request.payload.get("unchanged_declaration_reused"),
                     "evidence": _reference(request)})
    return rows


def _provider_cost(events) -> dict[str, Any]:
    modes = ("actual_call", "retry", "cache_reuse", "historical_experiment")
    usage: dict[Any, AnalyticsEvent] = {}
    for event in _reconciled_provider_usage(events):
        if event.family == EventFamily.PROVIDER_USAGE:
            key = event.payload.get("usage_id", event.event_id)
            if key in usage and usage[key].payload != event.payload:
                raise ValueError("conflicting usage receipts; reconcile once before measurement")
            usage[key] = event
    counts = Counter(e.payload.get("mode") for e in usage.values())
    current, historical, missing = Decimal(0), Decimal(0), []
    breakdown: dict[tuple, dict] = {}
    for event in usage.values():
        p = event.payload
        dimensions = ("purpose", "source_class", "provider", "model", "prompt_version", "policy_version")
        key = tuple(p.get(name) for name in dimensions) + (p.get("mode"),)
        row = breakdown.setdefault(key, {**dict(zip(dimensions, key)), "mode": p.get("mode"),
                                         "actual_cost_usd": Decimal(0), "entries": 0,
                                         "unavailable_entries": 0, "native_receipts": [], "billing_event_ids": []})
        row["entries"] += 1
        native = p.get("native_receipt", p.get("receipt"))
        if native and native not in row["native_receipts"]:
            row["native_receipts"].append(native)
        row["billing_event_ids"].extend(p.get("billing_event_ids", [event.event_id] if not p.get("receipt") else []))
        try:
            amount = Decimal(str(p.get("actual_cost_usd")))
        except InvalidOperation:
            amount = Decimal("NaN")
        valid = (all(_known_usage_value(p.get(name)) for name in dimensions) and p.get("mode") in modes
                 and _known_usage_value(p.get("evidence_reference")) and amount.is_finite() and amount >= 0)
        if not valid:
            missing.append(event.event_id)
            row["unavailable_entries"] += 1
            continue
        row["actual_cost_usd"] += amount
        if p["mode"] == "historical_experiment":
            historical += amount
        else:
            current += amount
    for row in breakdown.values():
        row["actual_cost_usd"] = (None if row["unavailable_entries"] else
                                  _money(row["actual_cost_usd"]))
    return {"actual_cost_usd": _money(current) if usage and not missing else None,
            "recorded_actual_cost_usd": _money(current),
            "historical_experiment_cost_usd": _money(historical),
            "counts": {mode: counts[mode] for mode in modes}, "unavailable_entries": missing,
            "breakdown": list(breakdown.values()), "status": "recorded" if usage and not missing else "unavailable"}


def _known_usage_value(value) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and value.strip().lower() not in {"unavailable", "unknown", "unclassified_usage"})


def _usage_run_id(event: AnalyticsEvent) -> int | None:
    p = event.payload
    explicit = p.get("extraction_run_id")
    identity = str(p.get("usage_id", ""))
    canonical = int(identity.removeprefix("extraction_run:")) if (
        identity.startswith("extraction_run:") and identity.removeprefix("extraction_run:").isdigit()
    ) else None
    if explicit is not None and (type(explicit) is not int or explicit <= 0):
        raise ValueError("extraction_run_id must name one retained native usage record")
    if explicit is not None and canonical is not None and explicit != canonical:
        raise ValueError("provider usage identity names conflicting extraction runs")
    return explicit if explicit is not None else canonical


def _reconciled_provider_usage(events) -> list[AnalyticsEvent]:
    """Join billing to the native usage unit without repricing or duplicating it.

    A billing receipt must name the exact extraction run (its canonical usage
    identity or an explicit extraction_run_id). Similar timestamps, documents,
    source classes or model names are never a join. The raw native receipt and
    bill remain in observations; only this derived cost reading is combined.
    """
    usage = [e for e in events if e.family == EventFamily.PROVIDER_USAGE]
    native = [e for e in usage if e.payload.get("receipt", {}).get("table") == "extraction_runs"]
    by_usage_id: dict[Any, AnalyticsEvent] = {}
    bill_ids: dict[Any, list[str]] = {}
    for event in (e for e in usage if e not in native):
        identity = event.payload.get("usage_id", event.event_id)
        if identity in by_usage_id and by_usage_id[identity].payload != event.payload:
            raise ValueError("conflicting billing observations for one usage identity")
        by_usage_id.setdefault(identity, event)
        bill_ids.setdefault(identity, []).append(event.event_id)
    bills = list(by_usage_id.values())
    combined, consumed = [], set()
    for receipt in native:
        run_id = _usage_run_id(receipt)
        matching = [b for b in bills if run_id is not None and _usage_run_id(b) == run_id]
        if not matching:
            combined.append(receipt)
            continue
        for bill in matching:
            original, observed = receipt.payload, bill.payload
            for field in ("model", "source_class", "prompt_version", "policy_version", "provider", "purpose", "mode"):
                if (_known_usage_value(original.get(field)) and _known_usage_value(observed.get(field))
                        and original[field] != observed[field]):
                    raise ValueError(f"provider billing contradicts retained {field}")
            merged = dict(original)
            referenced = _known_usage_value(observed.get("evidence_reference"))
            for field in ("model", "source_class", "prompt_version", "policy_version", "provider", "purpose", "mode"):
                if referenced and not _known_usage_value(merged.get(field)) and _known_usage_value(observed.get(field)):
                    merged[field] = observed[field]
            if referenced and merged.get("actual_cost_usd") is None:
                merged["actual_cost_usd"] = observed.get("actual_cost_usd")
            merged.update(usage_id=observed.get("usage_id", bill.event_id), extraction_run_id=run_id,
                          evidence_reference=observed.get("evidence_reference"),
                          native_receipt=original["receipt"],
                          billing_event_ids=bill_ids[observed.get("usage_id", bill.event_id)])
            combined.append(replace(bill, payload=merged))
            consumed.add(bill.event_id)
    return combined + [b for b in bills if b.event_id not in consumed]


def _money(value: Decimal) -> str:
    # Actual sub-cent provider costs must not turn into a recorded zero.
    exponent = value.as_tuple().exponent
    return format(value, "f") if isinstance(exponent, int) and exponent < -2 else format(value, ".2f")


def _sampling(valid, invalid_samples, children) -> dict[str, Any]:
    invalid = [e.event_id for e in invalid_samples]
    groups: dict[str, dict[str, Any]] = {}
    for event in valid:
        p = event.payload
        source = p.get("source_class", "unavailable")
        group = groups.setdefault(source, {"source_class": source, "material_change_samples": 0,
                                            "confirmed_material_misses": 0, "checked_populated_material_fields": 0,
                                            "correct_populated_material_fields": 0, "confirmed_material_false_writes": 0})
        kind = p.get("sample_kind")
        if kind == "material_change" and p.get("material") is True:
            group["material_change_samples"] += 1
            group["confirmed_material_misses"] += p.get("confirmed_miss") is True
        elif kind == "baseline_field" and p.get("populated") is True and p.get("material") is True:
            group["checked_populated_material_fields"] += 1
            group["correct_populated_material_fields"] += p.get("correct") is True
        elif kind == "false_write" and p.get("material") is True and p.get("automatic") is True:
            group["confirmed_material_false_writes"] += (p.get("classification") == "confirmed_policy_error"
                                                         and p.get("reversal_receipt") is not None)
    for child in children:
        own = [e.payload for e in valid if e.payload.get("sample_kind") == "child_usefulness"
               and e.payload.get("delta_id") == child["delta_id"]]
        child["necessary"] = own[0].get("necessary") if own else None
        child["diagnostic_outcome"] = ("necessary" if child["necessary"] is True else
                                        "edited" if child["outcome"] == "edit" else
                                        "rejected/kept current" if child["outcome"] == "keep current" else
                                        child["outcome"])
    strata = []
    for source in sorted({c["source_class"] for c in children if c["source_class"]}):
        for material in (False, True):
            members = {c["delta_id"]: c for c in children if c["source_class"] == source
                       and c["material_field"] == material and c["outcome"] in {"apply", "edit", "keep current"}}
            if members:
                strata.append({"source_class": source, "material_field": material,
                               "resolved_delta_denominator": len(members),
                               "accept_without_edit": sum(c["outcome"] == "apply" for c in members.values())})
    return {"by_source_class": list(groups.values()), "accept_without_edit_strata": strata,
            "confirmed_misses": [e.payload for e in valid if e.payload.get("confirmed_miss") is True],
            "invalid_observations": invalid, "status": "recorded" if valid and not invalid else "unavailable"}


def _portfolio(period, events) -> dict[str, Any]:
    events = [e for e in events if _event_origin(e) == _period_origin(period)]
    presented = [e for e in events if e.family == EventFamily.PORTFOLIO_READING
                 and period.start <= e.occurred_at < period.end
                 and any(p.get("project_id") == period.project_id for p in e.payload.get("projects", []))]
    openings = [e for e in events if e.family in {EventFamily.PROJECT_OPENING, EventFamily.PROJECT_SELECTION}
                and e.payload.get("project_id") == period.project_id and presented
                and period.start <= e.occurred_at < period.end]
    states = sorted({p.get("state", "unavailable") for e in presented for p in e.payload.get("projects", [])
                     if p.get("project_id") == period.project_id})
    context_valid = not _cohort_problems(period, presented)
    zero_click = not openings if presented and period.interaction_capture_complete and context_valid else None
    return {"presentation_event_ids": [e.event_id for e in presented],
            "project_open_event_ids": [e.event_id for e in openings],
            "presented_states": states, "zero_click": zero_click,
            "quiet_zero_click": zero_click if states == [NO_ACTION] else None,
            "observation_start": period.start.isoformat(), "observation_end": period.end.isoformat()}


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             default=str).encode()).hexdigest()
