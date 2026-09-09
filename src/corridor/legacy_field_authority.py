"""Read protected native field decisions through exact legacy identity aliases.

Generic AuditLog and Candidate rows are runtime-writable; even consistent
admission labels do not authenticate a historical accepted-value decision.
This reader never migrates those labels or invents a FactDecision. It joins an
already accepted source identifier's exact segment to an attributable alias,
then reads accepted scalar Facts from that exact document/run/source subject.
Identity and value decisions keep their separate actors and revisions. This
proves only the fields returned, not legacy population membership or missing
history; callers must retain the explicit gaps before claiming cutover.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.fact_types import SINGLE_VALUED_FACT_TYPES


@dataclass(frozen=True)
class LegacyFieldBinding:
    project_id: int
    record_subject_key: str
    legacy_dependency_id: int
    field: str
    fact_id: int
    source_subject_key: str
    source_document_id: int
    extraction_run_id: int
    source_fact_decision_id: int
    revision_id: int
    original_actor: str
    original_time: datetime
    authority_kind: str
    value: Any
    identifier_fact_decision_id: int
    identifier_revision_id: int
    subject_resolution_decision_id: int
    subject_resolution_revision_id: int
    subject_resolution_actor: str
    subject_resolution_time: datetime
    source_segment_ids: tuple[int, ...]


@dataclass(frozen=True)
class LegacyFieldGap:
    legacy_dependency_id: int
    field: str | None
    reason: str


@dataclass(frozen=True)
class LegacyAcceptedFieldMap:
    project_id: int
    revision_id: int | None
    bindings: tuple[LegacyFieldBinding, ...]
    gaps: tuple[LegacyFieldGap, ...]


def read_legacy_field_authority(
    session: Session, project_id: int, revision_id: int | None = None,
) -> LegacyAcceptedFieldMap:
    """Return existing native authority, never the Dependency's scalar values.

    A matching value, Candidate.merged_into, alias text, or migration executor
    cannot choose a source. Multiple exact accepted source bindings refuse the
    field even if their values are equal. An as-of request bounds identifier,
    alias and value decisions independently to the same selected revision.
    """
    if revision_id is not None and not session.scalar(text(
        "select exists(select 1 from project_record_revisions where project_id=:p and id=:r)"
    ), {"p": project_id, "r": revision_id}):
        raise ValueError("selected revision does not belong to project")
    rows = session.execute(text("""
        with selected as (
          select distinct on (d.fact_id) d.* from fact_decisions d
          where d.project_id=:p and (cast(:r as bigint) is null or d.revision_id<=:r)
          order by d.fact_id,d.revision_id desc,d.id desc
        ), accepted as (
          select d.* from selected d where d.disposition='include'
        ), aliases as (
          select distinct s.id as alias_id,s.dependency_id,s.revision_id as alias_revision,
            s.recorded_by as alias_actor,s.created_at as alias_time,
            identifier.id as identifier_fact_id,di.id as identifier_decision,
            di.revision_id as identifier_revision,identifier.document_id,
            identifier.extraction_run_id,identifier.subject_key
          from subject_resolution_decisions s
          join project_record_revisions ar on ar.id=s.revision_id and ar.project_id=s.project_id
            and ar.human_principal=s.recorded_by
          join fact_sources fs on fs.project_id=s.project_id and fs.source_segment_id=s.source_segment_id
            and fs.document_id=s.source_document_id and fs.role='value_source'
          join facts identifier on identifier.id=fs.fact_id and identifier.project_id=s.project_id
            and identifier.document_id=s.source_document_id and identifier.fact_type='utility_id'
            and identifier.text_value=s.raw_reference
          join accepted di on di.fact_id=identifier.id
          where s.project_id=:p and s.subject_type='constraint' and s.reference_kind='source_identifier'
            and (cast(:r as bigint) is null or s.revision_id<=:r)
        )
        select a.*,l.subject_id::text as native_subject, f.id as fact_id,f.fact_type,
          d.id as decision_id,d.revision_id,coalesce(vr.human_principal,vr.released_policy) as actor,
          vr.recorded_at,case when vr.human_principal is null then 'released_policy' else 'human' end as authority_kind,
          f.text_value,f.date_value,f.external_org_value_id,f.document_value_id,
          array(select fs.source_segment_id from fact_sources fs where fs.fact_id=f.id
            and fs.role='value_source' order by fs.ordinal) as source_segments
        from aliases a
        join facts f on f.project_id=:p and f.document_id=a.document_id
          and f.extraction_run_id=a.extraction_run_id and f.subject_key=a.subject_key
        join accepted d on d.fact_id=f.id
        join project_record_revisions vr on vr.id=d.revision_id and vr.project_id=d.project_id
        left join coordination_subject_lineage l on l.project_id=:p and l.legacy_dependency_id=a.dependency_id
        order by a.dependency_id,f.fact_type,d.id,a.alias_id
    """), {"p": project_id, "r": revision_id}).mappings().all()
    dependency_ids = tuple(session.scalars(text(
        "select id from dependencies where project_id=:p order by id"
    ), {"p": project_id}))
    grouped = {}
    for row in rows:
        if row["fact_type"] in SINGLE_VALUED_FACT_TYPES:
            grouped.setdefault((row["dependency_id"], row["fact_type"]), []).append(row)
    bindings = []
    gaps = []
    mapped = set()
    for (dependency_id, field), choices in grouped.items():
        # Deduplicate the same exact decision and identity proof only. Two
        # accepted sources cannot be reduced by equality or a newest-wins rule.
        choices = { (row["decision_id"], row["identifier_decision"], row["alias_id"]): row
                    for row in choices }
        if len(choices) != 1:
            gaps.append(LegacyFieldGap(dependency_id, field, "ambiguous_accepted_source_identity"))
            continue
        row = next(iter(choices.values()))
        if row["native_subject"] is None:
            gaps.append(LegacyFieldGap(dependency_id, field, "missing_native_record_subject_identity"))
            continue
        if not row["source_segments"]:
            gaps.append(LegacyFieldGap(dependency_id, field, "missing_value_source_locator"))
            continue
        value = next((row[key] for key in ("text_value", "date_value", "external_org_value_id", "document_value_id")
                      if row[key] is not None), None)
        bindings.append(LegacyFieldBinding(
            project_id, row["native_subject"], dependency_id, field, row["fact_id"], row["subject_key"],
            row["document_id"], row["extraction_run_id"], row["decision_id"], row["revision_id"],
            row["actor"], row["recorded_at"], row["authority_kind"], value,
            row["identifier_decision"], row["identifier_revision"], row["alias_id"], row["alias_revision"],
            row["alias_actor"], row["alias_time"], tuple(row["source_segments"]),
        ))
        mapped.add(dependency_id)
    for dependency_id in dependency_ids:
        if dependency_id not in mapped:
            gaps.append(LegacyFieldGap(dependency_id, None, "missing_protected_accepted_source_identity"))
        # An alias establishes what a source identifier refers to. It does not
        # establish record membership, migrate dismissals, or prove that an
        # unrecorded legacy override never occurred.
        gaps.append(LegacyFieldGap(dependency_id, None, "legacy_population_and_original_field_history_unproven"))
    return LegacyAcceptedFieldMap(project_id, revision_id, tuple(bindings), tuple(gaps))
