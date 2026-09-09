"""Compare frozen Proposed Delta values with a frozen customer working reference.

#499 needs a reproducible field comparison, not a second accepted-record writer.
The input adapter supplies exact native delta IDs, source references and typed
values. This engine freezes them before accepting a successor, retains every
disagreement, and keeps unresolved comparisons out of definitive accuracy claims.
It writes no Project Record and treats supplied timestamps as receipt evidence,
not as independent proof that a customer had not previously seen a revision.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import random
from types import MappingProxyType
from typing import Any


SOURCE_CLASSES = ("ucm_revision", "email", "minutes", "schedule_export")


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _canonical(value) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _immutable(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_immutable(item) for item in value)
    return value


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("comparison receipts require timezone-aware times")


@dataclass(frozen=True)
class FrozenRevision:
    identity: str
    project_id: int
    source_sha256: str
    seen_at: datetime
    values: Mapping[str, Mapping[str, Any]]

    def __post_init__(self):
        _aware(self.seen_at)
        if not self.identity or self.project_id <= 0 or len(self.source_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.source_sha256):
            raise ValueError("reference revision requires project, identity and exact source digest")
        if not all(isinstance(key, str) and key and isinstance(row, Mapping) for key, row in self.values.items()):
            raise ValueError("reference values require explicit subject identities")
        object.__setattr__(self, "values", _immutable(json.loads(_canonical(self.values))))

    def payload(self):
        return {"identity": self.identity, "project_id": self.project_id, "source_sha256": self.source_sha256,
                "seen_at": self.seen_at.isoformat(), "values": _plain(self.values)}


@dataclass(frozen=True)
class ComparisonPolicy:
    identity: str
    fields: tuple[str, ...]
    material_fields: tuple[str, ...]
    sampling_seed: str
    minimum_material_cases: int

    def __post_init__(self):
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "material_fields", tuple(self.material_fields))
        if (not self.identity or not self.fields or len(set(self.fields)) != len(self.fields)
                or not set(self.material_fields) <= set(self.fields) or not self.sampling_seed
                or self.minimum_material_cases <= 0):
            raise ValueError("comparison needs predeclared fields, materiality, seed and material-case minimum")

    def payload(self):
        return {"identity": self.identity, "fields": list(self.fields), "material_fields": list(self.material_fields),
                "sampling_seed": self.sampling_seed, "minimum_material_cases": self.minimum_material_cases,
                "field_matching": "exact subject identity, field and typed value; no fuzzy matching",
                "denominator_rule": "distinct subject/field change questions; ambiguous questions withhold rates"}


@dataclass(frozen=True)
class Prediction:
    delta_id: int
    subject: str
    field: str
    value: Any
    created_at: datetime
    source_class: str
    source_reference: str
    present: bool = True

    def __post_init__(self):
        _aware(self.created_at)
        if self.delta_id <= 0 or not self.subject or not self.field or not self.source_reference:
            raise ValueError("a prediction requires its native delta and exact source reference")
        if type(self.present) is not bool or (not self.present and self.value is not None):
            raise ValueError("a removal must explicitly omit its value")
        object.__setattr__(self, "value", _immutable(json.loads(_canonical(self.value))))

    def payload(self):
        return {"delta_id": self.delta_id, "subject": self.subject, "field": self.field, "value": _plain(self.value),
                "created_at": self.created_at.isoformat(), "source_class": self.source_class,
                "source_reference": self.source_reference, "present": self.present}


@dataclass(frozen=True)
class PredictionFreeze:
    baseline: FrozenRevision
    policy: ComparisonPolicy
    predictions: tuple[Prediction, ...]
    frozen_at: datetime
    excluded_delta_ids: tuple[int, ...]
    excluded_predictions: tuple[Prediction, ...] = ()

    def payload(self):
        return {"schema_version": "shadow-prediction-freeze-v1", "baseline": self.baseline.payload(),
                "policy": self.policy.payload(), "predictions": [p.payload() for p in sorted(
                    self.predictions + self.excluded_predictions, key=lambda p: (p.delta_id, p.field))],
                "frozen_at": self.frozen_at.isoformat(), "excluded_delta_ids": list(self.excluded_delta_ids)}

    @property
    def content_sha256(self):
        return sha256(_canonical(self.payload()).encode()).hexdigest()


def freeze_predictions(baseline: FrozenRevision, policy: ComparisonPolicy,
                       predictions: Sequence[Prediction], *, frozen_at: datetime) -> PredictionFreeze:
    """Freeze exact native delta/field values without accepting a successor input."""
    _aware(frozen_at)
    if baseline.seen_at > frozen_at:
        raise ValueError("baseline must be seen before prediction freeze")
    selected, excluded, identities = [], [], set()
    for prediction in predictions:
        identity = prediction.delta_id, prediction.field
        if identity in identities:
            raise ValueError("a native delta/field prediction may occur only once")
        identities.add(identity)
        if prediction.created_at > frozen_at:
            raise ValueError("a prediction cannot be created after its freeze")
        if prediction.field not in policy.fields:
            raise ValueError("prediction field lies outside predeclared comparison")
        if prediction.source_class == "recorded_verbal":
            excluded.append(prediction)
        elif prediction.source_class not in SOURCE_CLASSES:
            raise ValueError("prediction source class lies outside the initial population")
        else:
            row = baseline.values.get(prediction.subject, {})
            if (prediction.field in row) == prediction.present and _canonical(row.get(prediction.field)) == _canonical(prediction.value):
                raise ValueError("unchanged values are not Proposed Delta comparison questions")
            selected.append(prediction)
    return PredictionFreeze(baseline, policy, tuple(sorted(selected, key=lambda p: (p.delta_id, p.field))),
                            frozen_at, tuple(sorted({p.delta_id for p in excluded})), tuple(excluded))


def compare_revisions(frozen: PredictionFreeze, successor: FrozenRevision, *, reference_dataset_id: str) -> dict:
    """Classify every field change and prediction; ambiguity remains review work."""
    if not reference_dataset_id or frozen.baseline.project_id != successor.project_id:
        raise ValueError("comparison reference must name the same project and dataset")
    if frozen.frozen_at >= successor.seen_at:
        raise ValueError("predictions must be frozen before the successor is seen")
    by_target = defaultdict(list)
    for prediction in frozen.predictions:
        by_target[prediction.subject, prediction.field].append(prediction)
    targets = set(by_target)
    for subject in set(frozen.baseline.values) | set(successor.values):
        targets.update((subject, field) for field in frozen.policy.fields)
    findings = []
    for subject, field in sorted(targets):
        old = frozen.baseline.values.get(subject, {}).get(field)
        new = successor.values.get(subject, {}).get(field)
        old_present = field in frozen.baseline.values.get(subject, {})
        new_present = field in successor.values.get(subject, {})
        predicted = by_target.get((subject, field), [])
        changed = (old_present, _canonical(old)) != (new_present, _canonical(new))
        if not changed and not predicted:
            continue
        distinct = {(p.present, _canonical(p.value)) for p in predicted}
        if len(distinct) > 1 or (changed and predicted and (new_present, _canonical(new)) not in distinct):
            classification = "ambiguous"
        elif changed and predicted:
            classification = "matched"
        elif changed:
            classification = "customer_only"
        else:
            classification = "corridor_only"
        findings.append({"subject": subject, "field": field, "classification": classification,
            "material": field in frozen.policy.material_fields, "reference_changed": changed,
            "baseline_value": _plain(old), "reference_value": _plain(new),
            "baseline_present": old_present, "reference_present": new_present,
            "predictions": [p.payload() for p in predicted],
            "days_earlier": ((successor.seen_at - min(p.created_at for p in predicted)).total_seconds() / 86400
                             if classification == "matched" else None),
            "cause": "unreviewed" if classification == "customer_only" else None,
            "handling_outcome": "unreviewed" if classification == "corridor_only" else None,
            "human_review_required": classification != "matched"})
    strata = {}
    for field in frozen.policy.fields:
        rows = [r for r in findings if r["field"] == field]
        matched = sum(r["classification"] == "matched" for r in rows)
        predictions = sum(bool(r["predictions"]) for r in rows)
        reference = sum(r["reference_changed"] for r in rows)
        ambiguous = sum(r["classification"] == "ambiguous" for r in rows)
        strata[field] = {"matched_numerator": matched, "prediction_question_denominator": predictions,
            "reference_change_denominator": reference, "ambiguous_count": ambiguous,
            "precision": matched / predictions if predictions and not ambiguous else None,
            "recall": matched / reference if reference and not ambiguous else None,
            "basis": "agreement with working reference, not independently adjudicated accuracy"}
    return {"schema_version": "shadow-comparison-v1", "measurement_type": "Extraction Measurement",
        "reference_dataset_id": reference_dataset_id, "reference_is_semantic_gold": False,
        "prediction_freeze_sha256": frozen.content_sha256, "baseline": frozen.baseline.payload(),
        "successor": successor.payload(), "policy": frozen.policy.payload(), "findings": findings,
        "strata": strata, "source_population": list(SOURCE_CLASSES),
        "excluded_delta_ids": list(frozen.excluded_delta_ids),
        "limits": ["Exact typed field matching only; supplied identities and receipt times require upstream verification.",
                   "Customer-only differences are candidate misses until independently reviewed.",
                   "This comparison does not establish the source-arrival sample or a pilot verdict."]}


def select_source_sample(arrivals: Sequence[Mapping[str, str]], *, seed: str) -> dict:
    """Predeclared partner-week sampling: all up to 20, otherwise 20 without replacement."""
    if not seed:
        raise ValueError("sampling requires its predeclared seed")
    groups, identities, excluded = defaultdict(list), set(), []
    for arrival in arrivals:
        identity = arrival["id"]
        if not identity or identity in identities:
            raise ValueError("source arrival identities must be nonempty and unique")
        identities.add(identity)
        if arrival["source_class"] == "recorded_verbal":
            excluded.append(identity)
            continue
        if arrival["source_class"] not in SOURCE_CLASSES or not arrival["partner"] or not arrival["week"]:
            raise ValueError("source arrival lies outside the declared population")
        groups[arrival["partner"], arrival["week"]].append(identity)
    selected = []
    populations = []
    for key, population in sorted(groups.items()):
        population = sorted(population)
        sample = population if len(population) <= 20 else sorted(random.Random(_canonical([seed, *key])).sample(population, 20))
        selected.extend(sample)
        populations.append({"partner": key[0], "week": key[1], "eligible_count": len(population), "selected_count": len(sample)})
    return {"seed": seed, "selected_ids": selected, "excluded_ids": sorted(excluded),
            "populations": populations, "rule": "all when at most 20 per partner-week; otherwise 20 without replacement"}


def assess_material_sample(arrivals: Sequence[Mapping[str, str]], *, policy: ComparisonPolicy,
                           inspections: Sequence[Mapping[str, Any]]) -> dict:
    """Retain independent review coverage and the material-case minimum per partner."""
    sample = select_source_sample(arrivals, seed=policy.sampling_seed)
    selected = set(sample["selected_ids"])
    origins = {row["id"]: row for row in arrivals}
    seen, cases, by_partner, failed_classes = set(), set(), defaultdict(list), set()
    for inspection in inspections:
        arrival_id = inspection["arrival_id"]
        if arrival_id not in selected or arrival_id in seen:
            raise ValueError("inspection must name one unique selected arrival")
        resolvers = inspection.get("resolvers")
        if (not inspection.get("reviewer") or not isinstance(resolvers, (list, tuple, set, frozenset))
                or any(not isinstance(person, str) or not person for person in resolvers)
                or inspection["reviewer"] in resolvers):
            raise ValueError("material-change inspection needs an independent named reviewer")
        seen.add(arrival_id)
        for case in inspection["material_cases"]:
            if not case["id"] or case["id"] in cases or type(case.get("miss")) is not bool:
                raise ValueError("material case identity and miss finding must be explicit")
            if case["miss"] and not case.get("cause"):
                raise ValueError("a confirmed material miss needs its cause")
            cases.add(case["id"])
            by_partner[origins[arrival_id]["partner"]].append(case)
            if case.get("confirmed_material_automatic_false_write") is True:
                if not case.get("policy_class"):
                    raise ValueError("a material automatic false write must name its policy class")
                failed_classes.add((origins[arrival_id]["partner"], case["policy_class"]))
    partners = {}
    for partner in sorted({row["partner"] for row in arrivals}):
        required = {identity for identity in selected if origins[identity]["partner"] == partner}
        material = by_partner[partner]
        enough = required <= seen and len(material) >= policy.minimum_material_cases
        misses = sum(case["miss"] for case in material)
        partners[partner] = {"selected_arrivals": len(required), "inspected_arrivals": len(required & seen),
            "material_case_denominator": len(material), "material_miss_numerator": misses,
            "minimum_material_cases": policy.minimum_material_cases,
            "material_miss_rate": misses / len(material) if enough else None,
            "status": "measured" if enough else "insufficient_evidence",
            "failed_policy_classes": sorted(value for owner, value in failed_classes if owner == partner)}
    return {"sample": sample, "partners": partners}
