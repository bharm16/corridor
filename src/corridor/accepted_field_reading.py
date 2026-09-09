"""Immutable accepted UCM populations for production readers (#458).

The previous reader copied Dependency rows and replaced two station fields.
That preserved legacy population and current-value authority even when output
matched. Here an adopted source's explicit source-row-to-record-subject mapping
owns identity, and each field names the exact native FactDecision and revision.
Unmapped source subjects and unsupported authority classes refuse native reading;
no grouping by legacy dependency ID or last-row-wins policy is permitted.
"""
from __future__ import annotations

from dataclasses import dataclass
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
    ProjectRecordRevision, SourceSegment, DeltaFollowUpPlan, DeltaFollowUpPlanEvidence,
    ProposedDelta, DeltaDisposition, DeltaRecordDecision, DeltaSupersession, SupportAssessmentSource,
)
from corridor.record_projection import read_native_record_values, record_value_payload
from corridor.object_storage import content_store
from corridor.source_segments import spreadsheet_replay


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

    @property
    def origin(self):
        return f"native_decision:{self.decision_id}"


@dataclass(frozen=True)
class AcceptedConstraint:
    project_id: int
    subject_key: str
    source_row_key: str
    baseline_row_id: int
    adoption_revision_id: int
    fields: Mapping[str, AcceptedField]
    resolution_strategy: str | None

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

    # This first cohort has no mapped Coordination Decisions, selected schedule
    # links or readiness authority. The loader proves those populations empty
    # and refuses if a record requiring such an adapter exists. A source's
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
class AcceptedFollowUpPlan:
    plan_id: int
    delta_id: int
    revision_id: int
    target_subject_identity: str
    open_question: str
    responsible_principal: str | None
    responsible_organization: str | None
    return_date: datetime | None
    recorded_by: str
    recorded_at: datetime
    support_assessment_ids: tuple[int, ...]
    source_segment_ids: tuple[int, ...]


def read_adopted_follow_up_plans(session, project_id, revision_id, *, current):
    """Read questions on unresolved deltas without making them accepted values.

    Needs coordination owns a native Follow-up Plan (#526). Its responsible
    party is not the accepted Constraint's assigned person. Current work also
    excludes source-superseded deltas; historical reads name plans recorded by
    the requested accepted revision and do not infer historical source workflow
    supersession from a later clock.
    """
    query = select(DeltaFollowUpPlan, ProposedDelta.target_subject_identity).join(
        ProposedDelta, (ProposedDelta.id == DeltaFollowUpPlan.delta_id)
        & (ProposedDelta.project_id == DeltaFollowUpPlan.project_id)).where(
        DeltaFollowUpPlan.project_id == project_id, DeltaFollowUpPlan.revision_id <= revision_id)
    if current:
        query = query.where(~DeltaFollowUpPlan.delta_id.in_(select(DeltaDisposition.delta_id)),
                            ~DeltaFollowUpPlan.delta_id.in_(select(DeltaSupersession.prior_delta_id)))
    else:
        query = query.where(~DeltaFollowUpPlan.delta_id.in_(select(DeltaRecordDecision.delta_id).where(
            DeltaRecordDecision.project_id == project_id, DeltaRecordDecision.revision_id <= revision_id)))
    plans = tuple(session.execute(query.order_by(DeltaFollowUpPlan.id)))
    plan_ids = [plan.id for plan, _ in plans]
    evidence = {}
    segments = {}
    if plan_ids:
        for plan_id, assessment_id, segment_id in session.execute(select(
            DeltaFollowUpPlanEvidence.plan_id, DeltaFollowUpPlanEvidence.support_assessment_id,
            SupportAssessmentSource.source_segment_id).outerjoin(SupportAssessmentSource,
                (SupportAssessmentSource.support_assessment_id == DeltaFollowUpPlanEvidence.support_assessment_id)
                & (SupportAssessmentSource.project_id == DeltaFollowUpPlanEvidence.project_id))
            .where(DeltaFollowUpPlanEvidence.project_id == project_id, DeltaFollowUpPlanEvidence.plan_id.in_(plan_ids))
            .order_by(DeltaFollowUpPlanEvidence.ordinal, SupportAssessmentSource.ordinal)):
            evidence.setdefault(plan_id, set()).add(assessment_id)
            if segment_id is not None:
                segments.setdefault(plan_id, set()).add(segment_id)
    return tuple(AcceptedFollowUpPlan(plan.id, plan.delta_id, plan.revision_id, subject,
        plan.open_question, plan.responsible_principal, plan.responsible_organization,
        plan.return_date, plan.recorded_by_principal, plan.recorded_at,
        tuple(sorted(evidence.get(plan.id, ()))), tuple(sorted(segments.get(plan.id, ())))) for plan, subject in plans)


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

    @property
    def open_records(self):
        return tuple(record for record in self.records if not record.is_closed)

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
    for document, segments in by_document.values():
        if any(segment.kind != "spreadsheet_cell" for segment in segments.values()):
            raise NativeReadingRefused("native UCM reader requires a released adapter for non-workbook source support")
        store = content_store()
        key = store.resolve(document.sha256)
        if key is None:
            raise NativeReadingRefused("registered native source bytes are unavailable for the Source Passage Check")
        with TemporaryDirectory(prefix="corridor-native-reading-") as temporary:
            path = Path(temporary) / ("source" + Path(document.filename).suffix)
            store.stage(key, path, sha256=document.sha256)
            with spreadsheet_replay(document, path) as replay:
                for segment in segments.values():
                    replay(segment)
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
            document.doc_date))
    return {key: tuple(items) for key, items in result.items()}


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
    active = {row.source_row_key: row for row in rows if not row.excluded}
    if len(active) != len([row for row in rows if not row.excluded]) or any(not row.record_subject_key for row in active.values()):
        raise NativeReadingRefused("adopted subject mapping is incomplete or ambiguous")
    identity_to_source = {}
    for source_key, row in active.items():
        for declared_key in (source_key, row.record_subject_key):
            if declared_key in identity_to_source and identity_to_source[declared_key] != source_key:
                raise NativeReadingRefused("adopted source and record subject keys are ambiguous")
            identity_to_source[declared_key] = source_key
    grouped = {key: {} for key in active}
    sources = _passages(session, values)
    for value in values:
        if value.subject_key not in identity_to_source:
            raise NativeReadingRefused(f"accepted subject {value.subject_key!r} has no declared adopted-row mapping")
        if value.fact_type not in {*SINGLE_VALUED_FACT_TYPES, "closure_result", "applies_to"}:
            raise NativeReadingRefused(f"accepted {value.fact_type} requires its own native authority adapter")
        held = grouped[identity_to_source[value.subject_key]]
        if value.fact_type in held:
            raise NativeReadingRefused("more than one accepted decision owns this subject field")
        passages = sources.get(value.fact_id, ())
        if value.fact_type in SINGLE_VALUED_FACT_TYPES and not passages:
            raise NativeReadingRefused("accepted field lacks its exact native Source Segment")
        payload = (value.date_value if value.date_value is not None else record_value_payload(value))
        if isinstance(payload, dict):
            payload = MappingProxyType({name: tuple(item) if isinstance(item, list) else item
                                        for name, item in payload.items()})
        held[value.fact_type] = AcceptedField(value.fact_type, value.subject_key, payload, value.fact_id,
                                             value.decision_id, value.revision_id, passages,
                                             value.external_org_value_id)
    vocabulary = RESOLUTION_VOCABULARIES.get(project.slug)
    records = []
    for key, row in active.items():
        fields = grouped[key]
        wording = fields.get("resolution_strategy")
        strategy = vocabulary.read(wording.value or "") if vocabulary and wording else None
        records.append(AcceptedConstraint(project_id, row.record_subject_key, row.source_row_key,
            row.id, baseline.revision_id, fields, strategy))
    records.sort(key=lambda record: record.subject_key)
    identity = {"project": project_id, "revision": boundary, "adoption": baseline.revision_id,
        "records": [{"subject": row.subject_key, "source_row": row.source_row_key,
            "fields": {name: {"decision": field.decision_id, "fact": field.fact_id, "fact_subject": field.fact_subject_key,
                "revision": field.revision_id, "sources": [source.source_segment_id for source in field.sources]}
                for name, field in sorted(row.fields.items())}} for row in records],
        "excluded": [row.source_row_key for row in rows if row.excluded]}
    plans = read_adopted_follow_up_plans(session, project_id, boundary, current=revision_id is None)
    plan_scope = "current_open_deltas" if revision_id is None or not plans else "plans_recorded_through_revision"
    identity["follow_up_plans"] = [{"plan": plan.plan_id, "revision": plan.revision_id,
        "assessments": list(plan.support_assessment_ids), "sources": list(plan.source_segment_ids)} for plan in plans]
    identity["follow_up_scope"] = plan_scope
    fingerprint = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return AcceptedFieldPopulation(project_id, boundary, baseline.revision_id, tuple(records),
        tuple(identity["excluded"]), fingerprint, plans, plan_scope)


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
