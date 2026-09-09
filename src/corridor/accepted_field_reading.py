"""Immutable accepted UCM populations for production readers (#458).

The previous reader copied Dependency rows and replaced two station fields.
That preserved legacy population and current-value authority even when output
matched. Here an adopted source's explicit source-row-to-record-subject mapping
owns identity, and each field names the exact native FactDecision and revision.
Unmapped source subjects and unsupported authority classes refuse native reading;
no grouping by legacy dependency ID or last-row-wins policy is permitted.
"""
from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields, is_dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from types import MappingProxyType
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from corridor.adjudicate import RESOLUTION_VOCABULARIES
from corridor.fact_types import SINGLE_VALUED_FACT_TYPES
from corridor.models import (
    BaselineSource, BaselineSourceRow, Document, FactSource, Project,
    ProjectRecordRevision, SourceSegment, ProposedDelta, DeltaRecordDecision,
    Fact, FactDecision, DeltaReviewPacketChild, DeltaReviewPacketReversal,
    ScheduleLinkReceipt, ScheduleGoverningDerivation, Milestone, MilestoneRegistration,
)
from corridor.record_projection import read_native_record_values, record_value_payload
from corridor.object_storage import content_store
from corridor.native_follow_up_reading import AcceptedFollowUpPlan, read_adopted_follow_up_plans
from corridor.source_segments import spreadsheet_replay
from corridor.accepted_statement_reading import AcceptedStatement, read_native_statements
from corridor.locator_validation import source_segment_locator_validation_status
from corridor.object_storage import StorageError, DigestMismatch
from corridor.source_segment_errors import SourceSegmentIntegrityError


class NativeReadingRefused(ValueError):
    """The available native authority cannot supply the complete requested reading."""


@dataclass(frozen=True)
class AcceptedSourcePassage:
    fact_id: int
    decision_id: int
    revision_id: int
    source_segment_id: int
    document_id: int
    filename: str
    locator: str
    quote: str
    page_no: int | None
    document_date: date | None
    locator_validation_status: str = "valid"
    limitation: str | None = None

    @property
    def reference(self):
        return f"source_segment:{self.source_segment_id}"


@dataclass(frozen=True)
class AcceptedField:
    fact_type: str
    fact_subject_key: str
    value: Any
    fact_id: int
    decision_id: int
    revision_id: int
    sources: tuple[AcceptedSourcePassage, ...]
    external_org_value_id: int | None = None
    decision_kind: str = "fact_decision"
    actor: str | None = None
    decided_at: datetime | None = None
    released_policy: str | None = None
    source_fact_decision_id: int | None = None

    @property
    def origin(self):
        return f"native_decision:{self.decision_kind}:{self.decision_id}"


@dataclass(frozen=True)
class AcceptedConstraint:
    project_id: int
    subject_key: str
    source_row_key: str
    baseline_row_id: int | None
    identity_revision_id: int
    fields: Mapping[str, AcceptedField]
    resolution_strategy: str | None
    identity_decision_id: int | None = None
    removal_decision_id: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @property
    def id(self) -> str:
        """Native identity; this is never a fabricated Dependency integer."""
        return self.subject_key

    @property
    def ref_code(self):
        return self.subject_key

    def value(self, name):
        field = self.fields.get(name)
        return field.value if field else None

    @property
    def source_ref(self):
        return self.value("utility_id")

    @property
    def external_org_id(self):
        field = self.fields.get("external_org")
        return field.external_org_value_id if field else None

    @property
    def org_name(self):
        return self.value("external_org")

    @property
    def title(self):
        # The existing matrix materialization policy: utility type and party,
        # never conflict_description reinterpreted as a title.
        utility = self.value("utility_type") or "Utility"
        return f"{utility} — {self.org_name}" if self.org_name else utility

    @property
    def dep_type(self):
        return "utility_relocation"

    @property
    def location_desc(self):
        return " / ".join(value for name in ("alignment", "location_start", "location_end")
                          if (value := self.value(name))) or None

    @property
    def station_from(self):
        return self.value("station_from")

    @property
    def station_to(self):
        return self.value("station_to")

    @property
    def external_contact(self):
        return self.value("external_org_contact")

    @property
    def notes(self):
        return self.value("notes")

    @property
    def committed_date(self):
        return self.value("committed_date")

    @property
    def need_date(self):
        return self.value("need_date")

    @property
    def closure_kind(self):
        value = self.value("closure_result")
        return value.get("closure_kind") if value else None

    @property
    def is_closed(self):
        return self.closure_kind == "constraint_closed"

    @property
    def source_passages(self):
        by_id = {source.source_segment_id: source for field in self.fields.values() for source in field.sources}
        return tuple(by_id[key] for key in sorted(by_id))

    @property
    def checked_source_passages(self):
        return tuple(source for source in self.source_passages if source.locator_validation_status == "valid")

    # Selected coordination/schedule/support authority is explicitly checked
    # below and refused until its exact native subject adapter exists. A source's
    # action_due_date is never substituted for an internal Coordination Decision.
    internal_owner = None
    next_action = None
    action_due_date = None
    action_due_date_reason = None
    deferral_reason = None
    deferral_return_date = None
    milestone_id = None
    milestone_registration_id = None
    cost_responsibility = None
    evidence_required = None
    dismissed_at = None


@dataclass(frozen=True)
class AcceptedDecisionExclusion:
    """An accepted source decision displaced by an exact canonical target act."""

    decision_id: int
    fact_id: int
    fact_subject_key: str
    fact_type: str
    revision_id: int
    replacement_decision_id: int
    delta_record_decision_id: int
    record_subject_key: str
    reason: str = "replaced_by_explicit_record_target_decision"


@dataclass(frozen=True)
class AcceptedFieldPopulation:
    project_id: int
    revision_id: int
    adoption_revision_id: int
    records: tuple[AcceptedConstraint, ...]
    excluded_source_rows: tuple[str, ...]
    fingerprint: str
    follow_up_plans: tuple[AcceptedFollowUpPlan, ...] = ()
    follow_up_scope: str = "current_open_deltas"
    statements: tuple[AcceptedStatement, ...] = ()
    coverage_blockers: tuple[str, ...] = ()
    withdrawn_subjects: tuple[tuple[str, int], ...] = ()
    excluded_accepted_decisions: tuple[AcceptedDecisionExclusion, ...] = ()

    @property
    def open_records(self):
        return tuple(record for record in self.records if not record.is_closed and record.removal_decision_id is None)

    @property
    def record_ids(self):
        return tuple(record.id for record in self.open_records)


def _passages(session, values):
    identities = {value.fact_id: value for value in values}
    result = {}
    if not identities:
        return result
    rows = session.execute(select(FactSource, SourceSegment, Document)
        .join(SourceSegment, SourceSegment.id == FactSource.source_segment_id)
        .join(Document, Document.id == SourceSegment.document_id)
        .where(FactSource.fact_id.in_(identities), FactSource.role == "value_source")
        .order_by(FactSource.fact_id, FactSource.ordinal)).all()
    by_document = {}
    for link, segment, document in rows:
        by_document.setdefault(document.id, (document, {}))[1][segment.id] = segment
    checks = {}
    for document, segments in by_document.values():
        try:
            store = content_store()
            key = store.resolve(document.sha256)
            if key is None:
                for segment in segments.values():
                    checks[segment.id] = ("not_checked", "registered source bytes are unavailable")
                continue
            with TemporaryDirectory(prefix="corridor-native-reading-") as temporary:
                path = Path(temporary) / ("source" + Path(document.filename).suffix)
                store.stage(key, path, sha256=document.sha256)
                if all(segment.kind == "spreadsheet_cell" for segment in segments.values()):
                    with spreadsheet_replay(document, path) as replay:
                        for segment in segments.values():
                            replay(segment)
                            checks[segment.id] = ("valid", None)
                else:
                    for segment in segments.values():
                        status = source_segment_locator_validation_status(document, segment, path)
                        checks[segment.id] = (status, None if status == "valid" else "Source Passage Check could not establish this locator")
        except (SourceSegmentIntegrityError, DigestMismatch) as error:
            for segment in segments.values():
                checks[segment.id] = ("invalid", str(error))
        except (StorageError, OSError, ValueError) as error:
            for segment in segments.values():
                checks[segment.id] = ("not_checked", str(error))
    for link, segment, document in rows:
        value = identities[link.fact_id]
        if segment.project_id != value.project_id or document.project_id != value.project_id:
            raise NativeReadingRefused("native value source belongs to another project")
        if segment.kind == "spreadsheet_cell" and segment.sheet_name and segment.cell_range:
            locator = f"{segment.sheet_name}!{segment.cell_range}"
        elif segment.kind in {"pdf_span", "pdf_cell", "prose_span"} and segment.page_no is not None:
            locator = f"page {segment.page_no}"
        else:
            raise NativeReadingRefused("native publication needs an explicit supported source locator")
        result.setdefault(link.fact_id, []).append(AcceptedSourcePassage(
            value.fact_id, value.decision_id, value.revision_id, segment.id,
            document.id, document.filename, locator, segment.exact_text, segment.page_no,
            document.doc_date, *checks.get(segment.id, ("not_checked", "locator was not checked"))))
    return {key: tuple(items) for key, items in result.items()}


def _constraint_authority(session, baseline, rows, boundary, values):
    """Follow declared baseline, decision predecessors and Resolve Delta acts.

    Matching later Fact subject strings to a baseline row is not authority.
    Baseline fields are seeded only from that adoption's document/revision;
    later fields must descend from its exact decision or be emitted by a typed
    Resolve Delta targeting that declared subject. Separate conflicting acts
    cannot become last-value-wins merely because they share a text key.
    """
    metadata = tuple(session.execute(select(FactDecision, Fact.document_id, Fact.subject_kind, Fact.subject_key)
        .join(Fact, Fact.id == FactDecision.fact_id).where(FactDecision.project_id == baseline.project_id)))
    decisions = {row.id: row for row, *_ in metadata}
    kinds = {row.id: kind for row, _, kind, _ in metadata}
    by_revision_subject = {}
    for row, *_ in metadata:
        by_revision_subject.setdefault((row.revision_id, row.subject_key), []).append(row)
    origins, aliases, owners, recognized, removals = {}, {}, {}, set(), {}
    displaced = {}
    for row in rows:
        if row.excluded:
            continue
        if not row.record_subject_key or row.record_subject_key in origins:
            raise NativeReadingRefused("adopted subject mapping is incomplete or ambiguous")
        origins[row.record_subject_key] = {"source_row_key": row.source_row_key, "baseline_row_id": row.id,
                                         "revision_id": baseline.revision_id, "decision_id": None}
        for key in (row.source_row_key, row.record_subject_key):
            if key in aliases and aliases[key] != row.record_subject_key:
                raise NativeReadingRefused("adopted source and record subject keys are ambiguous")
            aliases[key] = row.record_subject_key
    for decision, document_id, kind, source_key in metadata:
        if decision.revision_id != baseline.revision_id or decision.disposition != "include":
            continue
        if document_id != baseline.document_id or kind != "source_row":
            raise NativeReadingRefused("baseline authority does not bind its declared source document and class")
        row = next((item for item in rows if not item.excluded and item.source_row_key == source_key), None)
        if row is None:
            raise NativeReadingRefused("baseline decision has no declared adopted-row mapping")
        slot = (row.record_subject_key, decision.fact_type)
        if slot in owners:
            raise NativeReadingRefused("more than one baseline decision owns a subject field")
        owners[slot] = decision
        recognized.add(decision.id)

    def follow(decision, subject, through):
        seen = set()
        while True:
            if decision.id in seen:
                raise NativeReadingRefused("accepted decision predecessor chain is cyclic")
            seen.add(decision.id)
            recognized.add(decision.id)
            successor = decisions.get(decision.superseded_by)
            if successor is None or successor.revision_id > through:
                return decision
            if (successor.fact_type != decision.fact_type or kinds.get(successor.id) != "source_row"
                    or (successor.subject_key in aliases and aliases[successor.subject_key] != subject)):
                raise NativeReadingRefused("accepted predecessor crosses a field or declared record identity")
            decision = successor

    reversed_decisions = dict(session.execute(select(DeltaReviewPacketChild.decision_id, DeltaReviewPacketReversal.id)
        .join(DeltaReviewPacketReversal, DeltaReviewPacketReversal.receipt_id == DeltaReviewPacketChild.receipt_id)
        .where(DeltaReviewPacketChild.project_id == baseline.project_id,
               DeltaReviewPacketReversal.revision_id <= boundary)).all())
    acts = tuple(session.execute(select(DeltaRecordDecision, ProposedDelta)
        .join(ProposedDelta, ProposedDelta.id == DeltaRecordDecision.delta_id)
        .where(DeltaRecordDecision.project_id == baseline.project_id,
               DeltaRecordDecision.revision_id <= boundary,
               DeltaRecordDecision.disposition.in_(("accept", "edit")))
        .order_by(DeltaRecordDecision.revision_id, DeltaRecordDecision.id)))
    withdrawn = {}
    for act, delta in acts:
        emitted = [row for row in by_revision_subject.get((act.revision_id, delta.target_subject_identity), ())
                   if kinds.get(row.id) == "source_row" and
                   (delta.target_type == "proposed_subject" or row.fact_type == delta.target_field
                    or (act.effect_kind == "apparent_removal" and row.disposition == "do_not_add"))]
        if not emitted:
            # A statement-class act belongs to the independent statement reader.
            continue
        if act.id in reversed_decisions:
            if act.effect_kind == "new_subject":
                withdrawn[delta.target_subject_identity] = reversed_decisions[act.id]
            continue
        subject = aliases.get(delta.target_subject_identity)
        if act.effect_kind == "new_subject":
            if subject is not None:
                raise NativeReadingRefused("new-subject act collides with an existing declared record identity")
            subject = delta.target_subject_identity
            withdrawn.pop(subject, None)
            origins[subject] = {"source_row_key": delta.target_subject_identity, "baseline_row_id": None,
                                "revision_id": act.revision_id, "decision_id": act.id}
            aliases[subject] = subject
        if subject is None:
            raise NativeReadingRefused("Resolve Delta target has no declared record identity")
        touched = set()
        for decision in emitted:
            slot = (subject, decision.fact_type)
            if slot in touched:
                raise NativeReadingRefused("Resolve Delta emits competing decisions for one field")
            touched.add(slot)
            prior = owners.get(slot)
            if prior is not None:
                prior = follow(prior, subject, act.revision_id - 1)
                if prior.revision_id >= act.revision_id or prior.revision_id > (act.observed_accepted_revision_id or 0):
                    raise NativeReadingRefused("Resolve Delta does not bind the accepted canonical field predecessor")
                displaced[prior.id] = (decision.id, act.id, subject)
            owners[slot] = decision
            recognized.add(decision.id)
        if act.effect_kind == "apparent_removal":
            explicit = tuple(row.id for row in emitted if row.disposition == "do_not_add")
            if not explicit:
                raise NativeReadingRefused("accepted apparent removal has no explicit suppression effect")
            removals[subject] = (act.id, explicit)
    current = {value.decision_id: value for value in values}
    selected = {key: {} for key in origins}
    selected_targets = {}
    for (subject, field), decision in owners.items():
        effective = follow(decision, subject, boundary)
        if effective.disposition != "include":
            continue
        if effective.id not in current:
            continue  # a separate accepted statement suppression may withhold its fields
        if effective.id in selected_targets and selected_targets[effective.id] != subject:
            raise NativeReadingRefused("one accepted field decision ambiguously supplies several record identities")
        selected_targets[effective.id] = subject
        selected[subject][field] = current[effective.id]
    unknown = [value for value in values if value.subject_kind == "source_row" and value.decision_id not in recognized]
    if unknown:
        raise NativeReadingRefused("accepted source-row fields have no declared baseline, predecessor or Resolve Delta authority mapping")
    removed = {}
    for subject, (act_id, effects) in removals.items():
        if any(follow(decisions[identity], subject, boundary).id == identity for identity in effects):
            removed[subject] = act_id
    revisions = {row.id: row for row in session.scalars(select(ProjectRecordRevision).where(
        ProjectRecordRevision.project_id == baseline.project_id))}
    authority = {value.decision_id: (revisions[value.revision_id].human_principal or revisions[value.revision_id].released_policy,
                revisions[value.revision_id].released_policy, decisions[value.decision_id].decided_at)
                for fields in selected.values() for value in fields.values()}
    return origins, selected, removed, authority, tuple(sorted(withdrawn.items())), displaced


def _refuse_unmapped_selected_authority(session, project_id, values, *, historical_boundary=None):
    # These receipts predate the revision spine and have no revision foreign key.
    # At a historical boundary, their immutable recorded times can establish
    # before/after; an equal timestamp cannot establish their order and refuses.
    # Current reads census every present receipt, including post-head imports.
    checks = (
        (ScheduleLinkReceipt, select(ScheduleLinkReceipt.id).where(
            ScheduleLinkReceipt.project_id == project_id), "selected schedule-link authority"),
        (ScheduleGoverningDerivation, select(ScheduleGoverningDerivation.id).where(
            ScheduleGoverningDerivation.project_id == project_id,
            ScheduleGoverningDerivation.method.in_(("coded", "human_pick"))), "governing schedule authority"),
        (MilestoneRegistration, select(MilestoneRegistration.id).join(
            Milestone, Milestone.id == MilestoneRegistration.milestone_id).where(
            Milestone.project_id == project_id), "registered key-date authority"),
    )
    for model, query, description in checks:
        if historical_boundary is not None:
            if session.scalar(query.where(model.created_at == historical_boundary.recorded_at).limit(1)):
                raise NativeReadingRefused(f"{description} has no provable order relative to this Project Record revision")
            query = query.where(model.created_at < historical_boundary.recorded_at)
        if session.scalar(query.limit(1)):
            raise NativeReadingRefused(f"{description} requires an exact native subject adapter")
    if any(value.fact_type == "supporting_documentation_in_use" for value in values):
        raise NativeReadingRefused("selected supporting-document authority requires an exact native subject adapter")


def _accepted_decision_census(values, selected_values, statements, displaced):
    """Every accepted decision is consumed or has an explicit authority exclusion."""
    consumed = {value.decision_id for value in selected_values}
    consumed.update(field.decision_id for statement in statements for field in statement.fields.values())
    exclusions = []
    for value in values:
        if value.decision_id in consumed:
            continue
        replacement = displaced.get(value.decision_id)
        if replacement is not None:
            replacement_id, act_id, subject = replacement
            exclusions.append(AcceptedDecisionExclusion(value.decision_id, value.fact_id,
                value.fact_subject_key or value.subject_key, value.fact_type, value.revision_id,
                replacement_id, act_id, subject))
            continue
        raise NativeReadingRefused(f"accepted decision {value.decision_id} ({value.subject_kind}/{value.fact_type}, "
            f"Fact {value.fact_id}) has no native reader or declared authority exclusion")
    return tuple(exclusions)


def read_accepted_field_population(session: Session, project_id: int, *, revision_id: int | None = None):
    """Read the supported adopted-UCM cohort or explicitly refuse its gaps.

    Existing legacy identities are not a fallback. Their exact migrated
    accepted-field owner map must be supplied before this reader can route them.
    """
    baseline = session.scalar(select(BaselineSource).where(BaselineSource.project_id == project_id))
    if baseline is None:
        raise NativeReadingRefused("legacy accepted-field subject ownership has no complete native mapping")
    if baseline.source_kind != "ucm_workbook":
        raise NativeReadingRefused("native reader requires the released UCM source-population mapping")
    project = session.get(Project, project_id)
    if project is None:
        raise NativeReadingRefused("project does not exist")
    boundary = revision_id if revision_id is not None else session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(ProjectRecordRevision.project_id == project_id))
    if boundary is None or boundary < baseline.revision_id:
        raise NativeReadingRefused("reading precedes this subject population's adoption authority")
    values = read_native_record_values(session, project_id, boundary)
    rows = tuple(session.scalars(select(BaselineSourceRow).where(
        BaselineSourceRow.project_id == project_id, BaselineSourceRow.baseline_source_id == baseline.id
    ).order_by(BaselineSourceRow.source_row_key)))
    # Native coordination identities currently have explicit legacy mappings,
    # but no adopted-baseline subject mapping. Their presence is a blocker, not
    # permission to attach their values to similarly named UCM rows.
    if session.scalar(text("select exists(select 1 from coordination_record_decisions where project_id=:project and revision_id<=:revision)"),
                      {"project": project_id, "revision": boundary}):
        raise NativeReadingRefused("native Coordination Decisions lack an explicit adopted-subject binding")
    _refuse_unmapped_selected_authority(session, project_id, values, historical_boundary=(
        session.get(ProjectRecordRevision, boundary) if revision_id is not None else None))
    statements = read_native_statements(session, project_id, boundary, values=values)
    origins, selected, removed, authority, withdrawn, displaced = _constraint_authority(session, baseline, rows, boundary, values)
    selected_values = tuple(value for fields in selected.values() for value in fields.values())
    exclusions = _accepted_decision_census(values, selected_values, statements, displaced)
    grouped = {key: {} for key in origins}
    sources = _passages(session, selected_values)
    for subject, fields in selected.items():
        for value in fields.values():
            if value.fact_type not in {*SINGLE_VALUED_FACT_TYPES, "closure_result", "applies_to"}:
                raise NativeReadingRefused(f"accepted {value.fact_type} requires its own native authority adapter")
            passages = sources.get(value.fact_id, ())
            if value.fact_type in SINGLE_VALUED_FACT_TYPES and not passages:
                raise NativeReadingRefused("accepted field lacks its exact native Source Segment")
            payload = value.date_value if value.date_value is not None else record_value_payload(value)
            if isinstance(payload, dict):
                payload = MappingProxyType({name: tuple(item) if isinstance(item, list) else item
                                            for name, item in payload.items()})
            if value.fact_subject_key is None:
                raise NativeReadingRefused("native accepted field has no original source-subject identity")
            grouped[subject][value.fact_type] = AcceptedField(value.fact_type, value.fact_subject_key, payload,
                value.fact_id, value.decision_id, value.revision_id, passages, value.external_org_value_id,
                "fact_decision", authority[value.decision_id][0], authority[value.decision_id][2],
                authority[value.decision_id][1], value.decision_id)
    vocabulary = RESOLUTION_VOCABULARIES.get(project.slug)
    records = []
    for subject, origin in origins.items():
        fields = grouped[subject]
        wording = fields.get("resolution_strategy")
        strategy = vocabulary.read(wording.value or "") if vocabulary and wording else None
        records.append(AcceptedConstraint(project_id, subject, origin["source_row_key"],
            origin["baseline_row_id"], origin["revision_id"], fields, strategy,
            origin["decision_id"], removed.get(subject)))
    records.sort(key=lambda record: record.subject_key)
    identity = {"project": project_id, "revision": boundary, "adoption": baseline.revision_id,
        "records": [{"subject": row.subject_key, "source_row": row.source_row_key,
            "identity_revision": row.identity_revision_id, "identity_decision": row.identity_decision_id,
            "removal_decision": row.removal_decision_id,
            "fields": {name: {"decision": field.decision_id, "fact": field.fact_id, "fact_subject": field.fact_subject_key,
                "revision": field.revision_id, "sources": [(source.source_segment_id, source.locator_validation_status) for source in field.sources]}
                for name, field in sorted(row.fields.items())}} for row in records],
        "excluded": [row.source_row_key for row in rows if row.excluded]}
    plans = read_adopted_follow_up_plans(session, project_id, boundary, current=revision_id is None)
    plan_scope = "current_open_deltas" if revision_id is None or not plans else "plans_recorded_through_revision"
    identity["follow_up_plans"] = [{"plan": plan.plan_id, "revision": plan.revision_id,
        "assessments": list(plan.support_assessment_ids), "sources": list(plan.source_segment_ids)} for plan in plans]
    identity["follow_up_scope"] = plan_scope
    identity["withdrawn_subjects"] = withdrawn
    identity["excluded_accepted_decisions"] = [item.__dict__ for item in exclusions]
    identity["statements"] = [{"subject": statement.subject_key,
        "fields": {name: {"decision": field.decision_id, "revision": field.revision_id,
            "sources": [(source.source_segment_id, source.locator_validation_status) for source in field.sources]}
            for name, field in statement.fields.items()}} for statement in statements]
    blockers = {blocker for statement in statements for blocker in statement.coverage_blockers}
    for record in records:
        blockers.update(source.limitation for source in record.source_passages if source.limitation)
    anchored = {statement.subject_key for statement in statements}
    blockers.update(f"Statement subject {value.subject_key} has no accepted wording anchor"
                    for value in values if value.subject_kind == "statement_candidate" and value.subject_key not in anchored)
    fingerprint = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return AcceptedFieldPopulation(project_id, boundary, baseline.revision_id, tuple(records),
        tuple(identity["excluded"]), fingerprint, plans, plan_scope, statements, tuple(sorted(blockers)), withdrawn, exclusions)


def accepted_field_text(field: AcceptedField):
    """Render only the released scalar/reference/closure value shapes."""
    value = field.value
    if isinstance(value, Mapping):
        if field.fact_type == "applies_to":
            return ", ".join(value["subject_keys"]) or "not yet known"
        if field.fact_type == "closure_result":
            return (value.get("closure_kind") or "not yet known").replace("_", " ")
        raise NativeReadingRefused("accepted structured field has no released display rule")
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def native_field_visible(field, *, document_only: bool) -> bool:
    """Apply the existing documentary audience filter, never an acceptance rule."""
    return not document_only or any(source.document_id is not None for source in field.sources)


def visible_native_statements(population, *, document_only: bool):
    return tuple(statement for statement in population.statements
                 if native_field_visible(statement.fields["statement_wording"], document_only=document_only))


def native_reader_input_manifest(population: AcceptedFieldPopulation, *, document_only: bool = False) -> dict:
    """Serialize this frozen reading, preserving authority separately from replay.

    This is a readback of inputs actually used by readers, not a coverage claim.
    Every field retains its typed value and native decision namespace. Audience
    filtering is identical to the report's and never changes accepted state.
    """
    def serial(value):
        if is_dataclass(value):
            return {item.name: serial(getattr(value, item.name)) for item in dataclass_fields(value)}
        if isinstance(value, Mapping):
            return {str(key): serial(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [serial(item) for item in value]
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    def field_manifest(field):
        result = serial(field)
        result["decision_kind"] = "fact_decision"
        result["origin"] = f"native_decision:fact_decision:{field.decision_id}"
        return result

    return {
        "project_id": population.project_id, "revision_id": population.revision_id,
        "adoption_revision_id": population.adoption_revision_id,
        "population_sha256": population.fingerprint, "document_only": document_only,
        "records": [{"subject_key": record.subject_key, "source_row_key": record.source_row_key,
            "baseline_row_id": record.baseline_row_id, "identity_revision_id": record.identity_revision_id,
            "identity_decision_id": record.identity_decision_id, "removal_decision_id": record.removal_decision_id,
            "state": "removed" if record.removal_decision_id else "closed" if record.is_closed else "open",
            "fields": {name: field_manifest(field) for name, field in record.fields.items()}}
            for record in population.records],
        "statements": [{"subject_key": statement.subject_key,
            "reading_revision_id": statement.reading_revision_id,
            "fields": {name: field_manifest(field) for name, field in statement.fields.items()
                if native_field_visible(field, document_only=document_only)},
            "coverage_blockers": list(statement.coverage_blockers)}
            for statement in visible_native_statements(population, document_only=document_only)],
        "follow_up_plans": serial(population.follow_up_plans),
        "follow_up_scope": population.follow_up_scope,
        "excluded_accepted_decisions": serial(population.excluded_accepted_decisions),
        "withdrawn_subjects": [{"subject_key": subject, "decision_kind": "delta_review_packet_reversal", "decision_id": identity}
            for subject, identity in population.withdrawn_subjects],
        "excluded_source_rows": list(population.excluded_source_rows),
        "coverage_blockers": list(population.coverage_blockers),
    }
