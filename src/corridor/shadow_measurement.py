"""Compare two sealed native UCM shadow runs through the #499 pure engine.

The engine's caller-supplied snapshots cannot establish native identity or
chronology. This read-only adapter starts with database-sealed shadow receipts,
checks their native source/delta/accepted-baseline joins, and expands typed
fields without inventing another subject identity. A reference must account
for its complete declared UCM mapping; partial or unresolved sources cannot
turn missing rows into apparent removals. No result is accepted-record authority
or independent semantic truth.
"""

from collections import defaultdict
from datetime import datetime

from sqlalchemy import select, text

from corridor import audit
from corridor.delta_generation import COMPARABLE_FACT_TYPES, revision_label
from corridor.fact_values import scalar_fact_value
from corridor.field_mapping_manifest import ABSENT_IS_BLANK, ONE_VALUE_PER_COLUMN, manifest_from_declaration
from corridor.materializer import materialize_segment_value
from corridor.models import (
    AuditLog, BaselineAdoption, BaselineFormat, BaselineFormatManifest, BaselineSource,
    Document, Fact, FactSource, ProjectRecordRevision, ProposedDelta, SourceDelivery, SourceSegment,
)
from corridor.record_projection import read_project_record_as_of_revision
from corridor.shadow_capabilities import ShadowRefused, verify_runtime
from corridor.shadow_comparison import ComparisonPolicy, FrozenRevision, Prediction, compare_revisions, freeze_predictions
from corridor.shadow_receipts import read_shadow_run


class NativeShadowComparisonRefused(ShadowRefused):
    """Native evidence cannot support this exact bounded comparison."""


def _require(condition, reason):
    if not condition:
        raise NativeShadowComparisonRefused(reason)


def _at(value):
    result = datetime.fromisoformat(value)
    _require(result.tzinfo is not None and result.utcoffset() is not None, "native receipt time is not timezone-aware")
    return result


def _mapping(session, payload, policy):
    identity = payload["mapping"]
    registration = session.scalar(select(BaselineFormat).where(
        BaselineFormat.project_id == payload["project_id"], BaselineFormat.format_kind == "field_mapping",
        BaselineFormat.format_identity == identity["identity"], BaselineFormat.format_version == identity["version"],
        BaselineFormat.content_sha256 == identity["content_sha256"]))
    _require(registration is not None and registration.registered_at <= _at(payload["frozen_at"]),
             "shadow mapping has no earlier native registration")
    if registration.superseded_by is not None:
        successor = session.get(BaselineFormat, registration.superseded_by)
        _require(successor is not None and successor.registered_at > _at(payload["frozen_at"]),
                 "shadow mapping was superseded before capture")
    stored = session.get(BaselineFormatManifest, registration.id)
    _require(stored is not None, "shadow mapping has no retained declaration")
    manifest = manifest_from_declaration(stored.declaration)
    _require(manifest.content_sha256 == identity["content_sha256"], "shadow mapping digest differs from native declaration")
    supported = {field for mapping in manifest.mappings
                 if mapping.composition == ONE_VALUE_PER_COLUMN and mapping.blank_behaviour == ABSENT_IS_BLANK
                 for field in mapping.target_fields}
    _require(set(policy.fields) <= supported and set(policy.fields) <= COMPARABLE_FACT_TYPES,
             "comparison fields need a registered scalar mapping with explicit absent-is-blank semantics")
    return manifest


def _native_capture(session, identity, policy):
    sealed = read_shadow_run(session, identity)
    payload = sealed.payload
    _require(payload.get("version") == "shadow-ucm-v1", "only native UCM shadow receipts are supported")
    project_id = payload["project_id"]
    verify_runtime(session, project_id=project_id, customer=payload["customer"], environment=payload["environment"])
    document, delivery = session.get(Document, payload["document_id"]), session.get(SourceDelivery, payload["delivery_id"])
    _require(document is not None and delivery is not None and document.doc_type == "matrix"
             and document.project_id == delivery.project_id == project_id and document.source_delivery_id == delivery.id
             and document.sha256 == delivery.content_sha256 == payload["source_sha256"]
             and delivery.disposition == "stored" and delivery.customer == payload["customer"],
             "shadow receipt does not name one native stored UCM document/delivery")
    frozen_at = _at(payload["frozen_at"])
    _require(delivery.received_at <= frozen_at and document.created_at <= frozen_at,
             "shadow receipt predates its native delivery or capture")
    recorded_at = session.scalar(text("select recorded_at from shadow_runs where identity=:identity"), {"identity": identity})
    _require(recorded_at == frozen_at, "shadow freeze differs from its database receipt time")
    _mapping(session, payload, policy)
    captures = session.scalars(select(AuditLog).where(AuditLog.action == audit.CAPTURE_LATER_SOURCE_REVISION,
        AuditLog.entity_type == audit.DOCUMENT, AuditLog.entity_id == document.id, AuditLog.ts <= frozen_at)).all()
    expected = {"content_sha256": payload["source_sha256"], "field_mapping": {"format_kind": payload["mapping"]["kind"], "format_identity": payload["mapping"]["identity"],
            "format_version": payload["mapping"]["version"], "content_sha256": payload["mapping"]["content_sha256"]},
        "accepted_baseline_revision": payload["accepted_baseline_revision"],
        "is_complete_enumerative_source": payload["is_complete_enumerative_source"],
        "row_accounting_sealed": payload["row_accounting_sealed"], "row_accounting": payload["accounting"],
        "proposed_deltas": [d["id"] for d in payload["deltas"]], "captured_facts": len(payload["fact_ids"])}
    _require(any(all(log.after_json.get(key) == value for key, value in expected.items()) for log in captures if log.after_json),
             "shadow accounting does not match its native capture audit")
    native_deltas = session.scalars(select(ProposedDelta).where(ProposedDelta.id.in_([d["id"] for d in payload["deltas"]]))).all()
    _require(len(native_deltas) == len(payload["deltas"]), "a frozen native delta disappeared")
    by_id = {d.id: d for d in native_deltas}
    for item in payload["deltas"]:
        native = by_id[item["id"]]
        _require(all(getattr(native, column.name) == (_at(item[column.name]) if column.name == "created_at" else item[column.name])
                     for column in ProposedDelta.__table__.columns), "frozen delta differs from its native immutable values")
        _require(native.project_id == project_id and native.accepted_baseline_revision == payload["accepted_baseline_revision"]
                 and native.source_revision == payload["source_sha256"] and native.created_at <= frozen_at,
                 "delta project, source or accepted-baseline revision is inconsistent")
        group = next((g for g in payload["groups"] if g["id"] == native.group_id), None)
        _require(group is not None and group["project_id"] == project_id and group["document_id"] == document.id,
                 "delta is not part of this document's frozen native group")
    facts = session.scalars(select(Fact).where(Fact.project_id == project_id, Fact.document_id == document.id)).all()
    _require(sorted(f.id for f in facts) == sorted(payload["fact_ids"]), "frozen fact list is not the complete native source capture")
    links = session.scalars(select(FactSource).where(FactSource.project_id == project_id, FactSource.document_id == document.id)).all()
    frozen_links = {(p["fact_id"], p["role"], p["ordinal"], p["segment"]["id"]): p["segment"] for p in payload["source_provenance"]}
    _require(len(frozen_links) == len(payload["source_provenance"]) and set(frozen_links) == {
        (link.fact_id, link.role, link.ordinal, link.source_segment_id) for link in links}, "frozen provenance differs from native Fact Sources")
    by_fact = defaultdict(list)
    for link in links:
        segment = session.get(SourceSegment, link.source_segment_id)
        frozen = frozen_links[link.fact_id, link.role, link.ordinal, link.source_segment_id]
        _require(segment is not None and segment.project_id == project_id and segment.document_id == document.id
            and all(getattr(segment, field) == frozen.get(field) for field in (
                "id", "kind", "exact_text", "content_sha256", "sheet_name", "cell_range")),
            "a frozen source segment differs from its exact native locator/value")
        if link.role == "value_source":
            by_fact[link.fact_id].append(segment)
    values, references = defaultdict(dict), {}
    for fact in facts:
        _require(fact.fact_type in COMPARABLE_FACT_TYPES and len(by_fact[fact.id]) == 1,
                 "UCM comparison requires one supported native scalar cell per Fact")
        materialized = materialize_segment_value(session, fact.fact_type, by_fact[fact.id][0])
        _require(scalar_fact_value(materialized) == scalar_fact_value(fact), "native Fact value cannot be reproduced from its frozen cell")
        _require(fact.fact_type not in values[fact.subject_key], "duplicate native subject/field facts are not a definitive reference")
        values[fact.subject_key][fact.fact_type] = scalar_fact_value(fact)
        references[fact.subject_key, fact.fact_type] = f"document:{document.id}/fact:{fact.id}/segment:{by_fact[fact.id][0].id}"
    return sealed, delivery, dict(values), references


def _baseline(session, payload, policy):
    adoption = session.scalar(select(BaselineAdoption).where(BaselineAdoption.project_id == payload["project_id"]))
    source = session.scalar(select(BaselineSource).where(BaselineSource.project_id == payload["project_id"]))
    _require(adoption is not None and source is not None and source.source_kind == "ucm_workbook"
             and revision_label(adoption.revision_id) == payload["accepted_baseline_revision"]
             and source.revision_id == adoption.revision_id, "comparison needs the exact human-adopted native UCM baseline")
    revision = session.get(ProjectRecordRevision, adoption.revision_id)
    _require(revision is not None and revision.project_id == payload["project_id"]
             and revision.recorded_at <= _at(payload["frozen_at"]), "accepted baseline revision is outside this shadow freeze")
    values = defaultdict(dict)
    for row in read_project_record_as_of_revision(session, payload["project_id"], adoption.revision_id):
        values.setdefault(row.subject_key, {})
        if row.fact_type in policy.fields:
            fact = session.get(Fact, row.fact_id)
            _require(fact is not None and fact.project_id == payload["project_id"], "accepted baseline Fact disappeared")
            _require(row.fact_type not in values[row.subject_key], "accepted baseline has duplicate subject/field values")
            values[row.subject_key][row.fact_type] = scalar_fact_value(fact)
    _require(bool(values), "accepted baseline has no native subject inventory")
    return FrozenRevision(payload["accepted_baseline_revision"], payload["project_id"], source.content_sha256,
                          revision.recorded_at, dict(values))


def _accounted_rows(payload, values, *, complete):
    accounting = payload["accounting"]
    if complete:
        _require(payload["is_complete_enumerative_source"] is True and payload["row_accounting_sealed"] is True,
                 "reference must be a complete enumerative source with sealed row accounting")
        _require(not accounting["unsupported_values"] and not any(c["populated_cells"] for c in accounting["retained_columns"]),
                 "unmapped or unsupported reference values prevent definitive comparison")
    rows = accounting["rows"]
    _require(len({r["source_row_key"] for r in rows}) == len(rows), "duplicate accounted source rows")
    _require(all(r["disposition"] in {"compared", "new_subject"} and r["subject_identity"]
                 and r["unsupported_count"] == 0 and r["retained_count"] == 0 for r in rows),
             "ambiguous, partial or unmapped rows cannot establish native comparison identities")
    _require(len({r["subject_identity"] for r in rows}) == len(rows) and {r["subject_identity"] for r in rows} == set(values),
             "native facts do not account for every exact source row")
    _require(all(r["value_count"] == len(values[r["subject_identity"]]) for r in rows),
             "captured native fields do not exhaust the accounted row values")


def _predictions(payload, baseline, policy, source_values, references):
    predictions = []
    for delta in payload["deltas"]:
        subject = delta["target_subject_identity"]
        if delta["change_type"] == "apparent_removal":
            _require(payload["is_complete_enumerative_source"] is True and payload["row_accounting_sealed"] is True
                     and subject in baseline.values and subject not in source_values
                     and any(r["subject_identity"] == subject and r["proposed"] is True for r in payload["accounting"]["removals"]),
                     "apparent removal lacks explicit complete native source accounting")
            _require(isinstance(delta["accepted_value"], dict) and all(
                delta["accepted_value"].get(field) == value for field, value in baseline.values[subject].items()),
                "apparent-removal accepted values differ from the native baseline")
            fields = [(field, None, False) for field in policy.fields if field in baseline.values[subject]]
        elif delta["target_type"] == "proposed_subject":
            _require(subject not in baseline.values and isinstance(delta["proposed_value"], dict), "new-subject delta collides with accepted baseline")
            fields = [(field, value, True) for field, value in delta["proposed_value"].items() if field in policy.fields]
        else:
            fields = [(delta["target_field"], delta["proposed_value"], True)] if delta["target_field"] in policy.fields else []
        for field, value, present in fields:
            if present:
                _require(source_values.get(subject, {}).get(field) == value and field in source_values.get(subject, {}),
                         "delta prediction does not match its exact native source Fact")
                reference = references[subject, field]
            else:
                reference = f"shadow:{payload['identity']}/accounting/removal/{subject}"
            _require(delta["change_type"] == "apparent_removal" or delta["target_type"] == "proposed_subject"
                     or delta["accepted_value"] == baseline.values.get(subject, {}).get(field),
                     "delta accepted value differs from the frozen accepted baseline")
            predictions.append(Prediction(delta["id"], subject, field, value, _at(delta["created_at"]), "ucm_revision", reference, present))
    return predictions


def compare_shadow_runs(session, *, prediction_identity: str, reference_identity: str,
                        policy: ComparisonPolicy, reference_dataset_id: str):
    """Read two native receipts and compare only a later fully accounted UCM.

    The caller supplies the predeclared field policy and Reference Dataset
    identity. This operation emits no events and commits no state. Delivery
    chronology proves only system observation, not when a person saw a source
    elsewhere. Agreement with the working reference remains diagnostic.
    """
    _require(prediction_identity != reference_identity, "prediction and reference need distinct native receipts")
    with session.no_autoflush:
        predicted, _, source_values, references = _native_capture(session, prediction_identity, policy)
        reference, reference_delivery, reference_values, _ = _native_capture(session, reference_identity, policy)
        p, r = predicted.payload, reference.payload
        _require(all(p[key] == r[key] for key in (
            "project_id", "customer", "environment", "source_configuration", "mapping", "accepted_baseline_revision")),
            "prediction and reference differ in project, mapping, source configuration or accepted baseline")
        frozen_at = _at(p["frozen_at"])
        _require(reference_delivery.id > p["source_delivery_watermark"]
                 and reference_delivery.received_at > frozen_at and _at(r["frozen_at"]) > frozen_at,
                 "reference delivery was already observed at the prediction freeze")
        _accounted_rows(p, source_values, complete=False)
        _accounted_rows(r, reference_values, complete=True)
        baseline = _baseline(session, p, policy)
        _require(set(baseline.values) - set(reference_values) == {
            row["subject_identity"] for row in r["accounting"]["removals"] if row["proposed"] is True},
            "missing reference subjects lack explicit native apparent-removal accounting")
        selected = {subject: {field: value for field, value in values.items() if field in policy.fields}
                    for subject, values in reference_values.items()}
        frozen = freeze_predictions(baseline, policy, _predictions(p, baseline, policy, source_values, references), frozen_at=frozen_at)
        successor = FrozenRevision(reference_identity, p["project_id"], r["source_sha256"], reference_delivery.received_at, selected)
        measurement = compare_revisions(frozen, successor, reference_dataset_id=reference_dataset_id)
        measurement["source_population"] = ["ucm_revision"]
        measurement["native_receipts"] = {"prediction": {"identity": prediction_identity, "output_sha256": predicted.output_sha256},
            "reference": {"identity": reference_identity, "output_sha256": reference.output_sha256},
            "accepted_baseline_revision": p["accepted_baseline_revision"], "mapping": p["mapping"],
            "prediction_frozen_at": p["frozen_at"], "prediction_delivery_watermark": p["source_delivery_watermark"],
            "reference_delivery_id": reference_delivery.id, "reference_received_at": reference_delivery.received_at.isoformat(),
            "source_population": ["ucm_revision"], "blank_semantics": ABSENT_IS_BLANK}
        measurement["limits"].append("Native chronology proves observation inside this system only; prior human visibility elsewhere is unknown.")
        return measurement
