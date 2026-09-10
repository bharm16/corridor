"""Read accepted statement subjects independently of adopted workbook rows.

The native UCM reader cannot classify email, minutes or verbal statement facts
by a workbook row map. This reader uses their accepted statement_wording anchor
and exact native decisions, retaining source timing and scope at the requested
revision. Source replay and support assessment remain provenance, never
acceptance authority; neither can manufacture a published date or readiness.
The Source Passage Check on each source is ``locator_validation``'s answer,
not a second ladder kept here; this reader adds only what it alone knows: the
verbal segment's agreement with its recorder's attestation, and whether the
registered bytes were available to it.
"""

from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import select

from corridor.locator_validation import (
    INVALID,
    NOT_CHECKED,
    VALID,
    recorded_verbal_statement_locator_validation,
    source_passage_checks,
)
from corridor.models import Document, Fact, FactDecision, FactSource, ProjectRecordRevision, RecordedVerbalOrigin, SourceSegment
from corridor.object_storage import DigestMismatch, StorageError
from corridor.record_projection import CurrentStatementTiming, read_native_record_values, record_value_payload
from corridor.source_segments import source_segment_locator_words
from corridor.storage import stored_file
from corridor.support_assessments import FactProposition, assessed_source_segments, support_assessment_history


class AcceptedStatementReadingRefused(ValueError):
    """Native accepted statement identity or field ownership is inconsistent."""


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class AcceptedStatementSource:
    source_segment_id: int
    kind: str
    role: str
    document_id: int | None
    document_sha256: str | None
    filename: str | None
    page_no: int | None
    locator: str
    location: Mapping[str, Any]
    exact_text: str
    content_sha256: str
    locator_validation_status: str
    limitation: str | None
    recorded_verbal_origin_id: int | None = None
    recorded_by: str | None = None
    recorded_at: datetime | None = None
    conversation_date: date | None = None
    source_class: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "location", _freeze(self.location))

    @property
    def reference(self):
        return f"source_segment:{self.source_segment_id}"

    @property
    def quote(self):
        return self.exact_text


@dataclass(frozen=True)
class AcceptedStatementAssessment:
    assessment_id: int
    evidence_role: str
    assessment: str
    actor: str
    released_policy: str | None
    assessed_at: datetime
    recorded_at: datetime
    superseded_by: int | None
    source_segment_ids: tuple[int, ...]


@dataclass(frozen=True)
class AcceptedStatementField:
    fact_type: str
    value: Any
    fact_id: int
    fact_subject_key: str
    decision_id: int
    revision_id: int
    actor: str
    released_policy: str | None
    decided_at: datetime
    revision_recorded_at: datetime
    fact_recorded_by: str
    fact_recorded_at: datetime
    sources: tuple[AcceptedStatementSource, ...]
    assessments: tuple[AcceptedStatementAssessment, ...]
    coverage_blockers: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "value", _freeze(self.value))

    @property
    def origin(self):
        return f"native_decision:{self.decision_id}"


@dataclass(frozen=True)
class AcceptedStatement:
    project_id: int
    subject_key: str
    reading_revision_id: int
    fields: Mapping[str, AcceptedStatementField]
    timings: tuple[CurrentStatementTiming, ...]
    applies_to_subject_keys: tuple[str, ...]
    applies_to_dependency_ids: tuple[int, ...]
    applies_to_mode: str
    closure_kind: str | None
    coverage_blockers: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @property
    def wording(self):
        return self.fields["statement_wording"].value

    @property
    def sources(self):
        found = {(source.source_segment_id, source.role): source for field in self.fields.values() for source in field.sources}
        return tuple(found[key] for key in sorted(found))

    @property
    def source_passages(self):
        return self.sources

    @property
    def source_classes(self):
        return tuple(sorted({source.source_class for source in self.sources if source.source_class}))


def _source(session, segment, role, project_id, checks):
    if segment.project_id != project_id:
        raise AcceptedStatementReadingRefused("statement source belongs to another project")
    document = session.get(Document, segment.document_id) if segment.document_id else None
    origin = session.get(RecordedVerbalOrigin, segment.recorded_verbal_origin_id) if segment.recorded_verbal_origin_id else None
    status, limitation = NOT_CHECKED, None
    if segment.kind == "recorded_verbal_statement":
        check = recorded_verbal_statement_locator_validation(segment)
        status, limitation = check.status, check.reason
        if status == VALID and (origin is None or origin.project_id != project_id or origin.exact_text != segment.exact_text
                                or origin.content_sha256 != segment.content_sha256):
            status, limitation = INVALID, "verbal segment differs from its native recorder attestation"
    elif document is None or document.project_id != project_id:
        status, limitation = INVALID, "statement source has no matching native Document"
    else:
        try:
            path = stored_file(document)
            check = checks.check(document, segment, path)
        except DigestMismatch as error:
            status, limitation = INVALID, str(error)
        except (OSError, ValueError, StorageError) as error:
            status, limitation = NOT_CHECKED, f"Source Passage Check unavailable: {error}"
        else:
            status, limitation = check.status, check.reason
            if path is None and status == NOT_CHECKED:
                limitation = "registered source bytes are unavailable for the Source Passage Check"
    location = {name: getattr(segment, name) for name in (
        "sheet_name", "cell_range", "page_no", "start_offset", "end_offset", "table_index", "cell_row", "cell_column",
        "row_span", "column_span", "span_stream", "rendition_sha256", "reading_sha256", "location_json")}
    return AcceptedStatementSource(segment.id, segment.kind, role, segment.document_id,
        document.sha256 if document else None, document.filename if document else None, segment.page_no,
        source_segment_locator_words(segment), location, segment.exact_text, segment.content_sha256, status, limitation,
        segment.recorded_verbal_origin_id, origin.recorded_by if origin else None,
        origin.recorded_at if origin else None, origin.conversation_date if origin else None,
        "recorded_verbal_statement" if origin else document.doc_type if document else None)


def read_native_statements(session, project_id: int, revision_id: int, *, values=None) -> tuple[AcceptedStatement, ...]:
    """Read accepted statement classes, keeping source timing distinct from published dates.

    ``values`` may bind the complete snapshot used by the caller's constraint
    reader. It is corroborated against a fresh native read at the fixed revision;
    caller-supplied payloads never establish values or population. No sources,
    candidates, model outputs or later lifecycle state create accepted members.
    """
    boundary = session.get(ProjectRecordRevision, revision_id)
    if boundary is None or boundary.project_id != project_id:
        raise AcceptedStatementReadingRefused("accepted revision does not belong to the requested project")
    with session.no_autoflush:
        native_values = tuple(read_native_record_values(session, project_id, revision_id))
        if values is not None and tuple(values) != native_values:
            raise AcceptedStatementReadingRefused("supplied values differ from the complete native snapshot at this revision")
        values = native_values
        facts = {fact.id: fact for fact in session.scalars(select(Fact).where(
            Fact.project_id == project_id, Fact.id.in_([value.fact_id for value in values])))}
        if any(value.project_id != project_id or value.revision_id > revision_id or value.fact_id not in facts for value in values):
            raise AcceptedStatementReadingRefused("shared native values are outside the selected project/revision")
        if any(getattr(value, "subject_kind", None) not in {None, facts[value.fact_id].subject_kind}
               or getattr(value, "fact_subject_key", None) not in {None, facts[value.fact_id].subject_key} for value in values):
            raise AcceptedStatementReadingRefused("shared native source-subject metadata disagrees with its Fact")
        statement_values = [value for value in values if value.fact_id in facts and facts[value.fact_id].subject_kind == "statement_candidate"]
        subjects = {value.subject_key for value in statement_values if value.fact_type == "statement_wording"}
        selected = [value for value in statement_values if value.subject_key in subjects]
        identities = [value.fact_id for value in selected]
        links = session.execute(select(FactSource, SourceSegment).join(SourceSegment, SourceSegment.id == FactSource.source_segment_id)
            .where(FactSource.project_id == project_id, FactSource.fact_id.in_(identities))
            .order_by(FactSource.fact_id, FactSource.role, FactSource.ordinal)).all()
        sources, cache = {}, {}
        with source_passage_checks() as checks:
            for link, segment in links:
                key = (segment.id, link.role)
                if key not in cache:
                    cache[key] = _source(session, segment, link.role, project_id, checks)
                sources.setdefault(link.fact_id, []).append(cache[key])
        grouped, projected = {}, {}
        for value in selected:
            fact = facts[value.fact_id]
            decision = session.get(FactDecision, value.decision_id)
            revision = session.get(ProjectRecordRevision, value.revision_id)
            if (decision is None or revision is None or decision.project_id != project_id or revision.project_id != project_id
                    or decision.fact_id != value.fact_id or decision.subject_key != value.subject_key
                    or decision.fact_type != value.fact_type or decision.revision_id != value.revision_id
                    or value.revision_id > revision_id or decision.disposition != "include"):
                raise AcceptedStatementReadingRefused("statement field is not bound to its exact accepted decision")
            if decision.superseded_by is not None:
                successor = session.get(FactDecision, decision.superseded_by)
                if successor is None or successor.revision_id <= revision_id:
                    raise AcceptedStatementReadingRefused("shared statement decision is no longer effective at this revision")
            fields = grouped.setdefault(value.subject_key, {})
            if value.fact_type in fields:
                raise AcceptedStatementReadingRefused("more than one accepted decision owns a statement field")
            projected[value.subject_key, value.fact_type] = value
            passages = tuple(sources.get(value.fact_id, ()))
            blockers = [source.limitation for source in passages if source.limitation]
            if not passages:
                blockers.append("accepted statement field has no native Source Segment reference")
            # Support has its own assessed/recorded time axis. Retain available
            # history without relabeling a later assessment as decision authority.
            assessment_history = tuple(row for row in support_assessment_history(session, project_id, FactProposition(fact.id))
                                       if row.recorded_at <= boundary.recorded_at)
            assessment_ids = {row.id for row in assessment_history}
            assessments = tuple(AcceptedStatementAssessment(row.id, row.evidence_role, row.assessment,
                row.human_principal or row.released_policy, row.released_policy, row.assessed_at, row.recorded_at,
                row.superseded_by if row.superseded_by in assessment_ids else None,
                tuple(segment.id for segment in assessed_source_segments(session, row))) for row in assessment_history)
            if any(source.document_id is not None for source in passages) and not assessments:
                blockers.append("no Support Assessment is recorded for this field at the reading boundary")
            payload = record_value_payload(value)
            if value.fact_type == "applies_to":
                payload = {"mode": "selected" if value.applies_to_subject_keys or value.applies_to_dependency_ids else "unknown",
                           "subject_keys": value.applies_to_subject_keys, "legacy_dependency_ids": value.applies_to_dependency_ids}
            fields[value.fact_type] = AcceptedStatementField(value.fact_type, payload, fact.id, fact.subject_key, decision.id, revision.id,
                revision.human_principal or revision.released_policy, revision.released_policy, decision.decided_at,
                revision.recorded_at, fact.recorded_by, fact.recorded_at, passages, assessments, tuple(sorted(set(blockers))))
        statements = []
        for subject, fields in sorted(grouped.items()):
            timing = projected.get((subject, "statement_timing"))
            scope = projected.get((subject, "applies_to"))
            closure = projected.get((subject, "closure_result"))
            blockers = {blocker for field in fields.values() for blocker in field.coverage_blockers}
            if scope is None:
                blockers.add("accepted Applies To information has not been recorded")
            statements.append(AcceptedStatement(project_id, subject, revision_id, fields,
                timing.statement_timings if timing else (), scope.applies_to_subject_keys if scope else (),
                scope.applies_to_dependency_ids if scope else (), fields["applies_to"].value["mode"] if scope else "not_recorded",
                closure.closure_kind if closure else None, tuple(sorted(blockers))))
        return tuple(statements)
