"""Immutable accepted UCM populations for production readers (#458).

The previous reader copied Dependency rows and replaced two station fields.
That preserved legacy population and current-value authority even when output
matched. Here an adopted source's explicit source-row-to-record-subject mapping
owns identity, and each field names the exact native FactDecision and revision.
Unmapped source subjects and unsupported authority classes refuse native reading;
no grouping by legacy dependency ID or last-row-wins policy is permitted.
The Source Passage Check on each value source is ``locator_validation``'s
answer and its caption is ``source_segments``' one locator wording, not a
second ladder and a fifth spelling kept here; this reader adds only what it
alone knows: that the source belongs to the reading's project, and whether the
registered bytes were available to it.
"""
from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields, is_dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from corridor.adjudicate import RESOLUTION_VOCABULARIES
from corridor.fact_types import SINGLE_VALUED_FACT_TYPES
from corridor.models import (
    BaselineSource, BaselineSourceRow, Document, FactSource, Project,
    ProjectRecordRevision, SourceSegment, ProposedDelta, DeltaRecordDecision,
    Fact, FactDecision, DeltaReviewPacketChild, DeltaReviewPacketReversal,
    SupportAssessment, SupportAssessmentSource,
    ScheduleLinkReceipt, ScheduleGoverningDerivation, Milestone, MilestoneRegistration,
)
from corridor.record_projection import read_native_record_values, record_value_payload
from corridor.native_follow_up_reading import AcceptedFollowUpPlan, read_adopted_follow_up_plans
from corridor.source_segments import source_segment_locator_words
from corridor.storage import stored_file
from corridor.accepted_statement_reading import AcceptedStatement, read_native_statements
from corridor.locator_validation import INVALID, NOT_CHECKED, source_segment_locator_validation
from corridor.object_storage import StorageError, DigestMismatch


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
    def external_org_id(self):
        field = self.fields.get("external_org")
        return field.external_org_value_id if field else None

    @property
    def org_name(self):
        return self.value("external_org")

    @property
    def station_from(self):
        return self.value("station_from")

    @property
    def station_to(self):
        return self.value("station_to")

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
        """The passages whose Source Passage Check passed.

        This is a mechanical, replayable fact about citations and it is
        published as one. It is **not** whether the record stands on anything:
        ADR-0082 separated the two, and ADR-0090 re-based MISSING_EVIDENCE off
        this onto the Support Assessment relation
        (``accepted_support_in_use``). Nothing may read this as support.
        """
        return tuple(source for source in self.source_passages if source.locator_validation_status == "valid")

    # No coordination, schedule-link, deferral or legacy support value is
    # hard-coded here any more. Eleven class attributes fixed to ``None`` used to
    # let a legacy reader duck-type this record, and every one of them turned
    # "this record has no such value" into "this value is empty" — which fired
    # MISSING_OWNER, MISSING_ACTION and ORPHAN on every accepted record until
    # each rule was patched separately. ``constraint_reading.ConstraintReading``
    # carries those fields as declared ``NotAvailable`` markers instead, so a
    # reader that wants one learns why it is absent (ADR-0084 §3, ADR-0090).


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
    for link, segment, document in rows:
        value = identities[link.fact_id]
        if segment.project_id != value.project_id or document.project_id != value.project_id:
            raise NativeReadingRefused("native value source belongs to another project")
        try:
            path = stored_file(document)
            check = source_segment_locator_validation(document, segment, path)
        except DigestMismatch as error:
            status, limitation = INVALID, str(error)
        except (OSError, ValueError, StorageError) as error:
            status, limitation = NOT_CHECKED, f"Source Passage Check unavailable: {error}"
        else:
            status, limitation = check.status, check.reason
            if path is None and status == NOT_CHECKED:
                limitation = "registered source bytes are unavailable for the Source Passage Check"
        result.setdefault(link.fact_id, []).append(AcceptedSourcePassage(
            value.fact_id, value.decision_id, value.revision_id, segment.id,
            document.id, document.filename, source_segment_locator_words(segment), segment.exact_text, segment.page_no,
            document.doc_date, status, limitation))
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


# The Support Assessment outcomes that make a Source Segment Supporting
# Documentation. "Contradicted", "unclear" and "not assessed" are readings too,
# and none of them is support (ADR-0082).
SUPPORTING_OUTCOMES = ("supported", "partially_supported")


@dataclass(frozen=True)
class SupportInUse:
    """What one accepted value's Supporting Documentation in Use rests on.

    ``stands_on_current`` is true as soon as one segment in use sits in a
    Document Revision that has not been replaced. A Recorded Verbal Statement is
    not a Document Revision at all, so it can never be superseded and counts
    here. ``replaced_on`` is the authority's own replacement date for the
    earliest superseded revision in use, and is ``None`` when none is in use —
    or, for a pre-constraint row, when the registry never recorded one; a
    document, retrieval, or ingestion date is never substituted for it
    (ADR-0016).
    """

    assessment_ids: tuple[int, ...]
    stands_on_current: bool
    replaced_on: date | None

    @property
    def depends_on_superseded(self) -> bool:
        return not self.stands_on_current


def accepted_support_in_use(session: Session, *, project_id: int, fact_ids) -> dict[int, SupportInUse]:
    """The Supporting Documentation in Use for a set of accepted values, once.

    ADR-0017 requires **one** resolver for "what does this record stand on", and
    it lives here because two readers need it: the issued Coordination Report's
    alert lines and the alert engine's accepted-record adapter. They used to be
    two copies of one query, and ADR-0090 ports two rules onto it — a second
    copy is a second definition of whether the accepted record stands on
    anything.

    Support is the Support Assessment relation: an effective assessment whose
    outcome is supported or partially supported. A passed Source Passage Check is
    never consulted, because a passage being where it was cited says nothing
    about whether it supports the value beside it (ADR-0082).

    A Fact with no entry in the result has no Supporting Documentation in use at
    all. The absence is expressed by the key being missing rather than by an
    empty record, so a caller cannot read "stands on nothing" and "stands on a
    replaced revision" off one value at once.
    """
    wanted = tuple({int(fact_id) for fact_id in fact_ids})
    if not wanted:
        return {}
    rows = session.execute(
        select(
            SupportAssessment.fact_id,
            SupportAssessment.id,
            Document.superseded_by,
            Document.superseded_on,
        )
        .join(
            SupportAssessmentSource,
            SupportAssessmentSource.support_assessment_id == SupportAssessment.id,
        )
        .join(SourceSegment, SourceSegment.id == SupportAssessmentSource.source_segment_id)
        .outerjoin(Document, Document.id == SourceSegment.document_id)
        .where(
            SupportAssessment.project_id == project_id,
            SupportAssessment.proposition_kind == "source_fact",
            SupportAssessment.fact_id.in_(wanted),
            SupportAssessment.superseded_by.is_(None),
            SupportAssessment.assessment.in_(SUPPORTING_OUTCOMES),
        )
    ).all()
    assessments: dict[int, set[int]] = {}
    current: set[int] = set()
    replaced: dict[int, date] = {}
    for fact_id, assessment_id, superseded_by, superseded_on in rows:
        assessments.setdefault(fact_id, set()).add(assessment_id)
        if superseded_by is None:
            current.add(fact_id)
        elif superseded_on is not None:
            held = replaced.get(fact_id)
            if held is None or superseded_on < held:
                replaced[fact_id] = superseded_on
    return {
        fact_id: SupportInUse(
            assessment_ids=tuple(sorted(found)),
            stands_on_current=fact_id in current,
            replaced_on=replaced.get(fact_id),
        )
        for fact_id, found in assessments.items()
    }


def accepted_population_support(session: Session, population: AcceptedFieldPopulation) -> dict[int, SupportInUse]:
    """The same resolver over every accepted value one population published."""
    return accepted_support_in_use(
        session,
        project_id=population.project_id,
        fact_ids=(field.fact_id for record in population.records for field in record.fields.values()),
    )


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
