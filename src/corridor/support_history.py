"""Migrate publication support to existing native document relationships.

The relationship Fact already exists in ADR-0074 and needs no invented source
passage. Publication citation routing additionally requires an exact retained
Source Segment. Missing actor/locator proof stays an explicit compatibility gap;
no readiness conclusion or support assessment is inferred from locator equality.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.facts import append_supporting_documentation_fact
from corridor.legacy_history import HistoryBatch
from corridor.models import OperativeSupport


@dataclass(frozen=True)
class NativePublicationSupport:
    dependency_id: int
    field_name: str | None
    subject_id: str
    document_id: int
    evidence_lineage_id: int
    source_segment_id: int
    exact_text: str
    fact_decision_id: int
    revision_id: int
    original_actor: str
    original_time: datetime
    decision_actor: str
    decision_time: datetime
    authority_kind: str


def _bind(session, *, project_id, scope_id, batch_id=None):
    scope = session.scalar(text("select support_scope_source(:project,:scope)"),
                           {"scope": scope_id, "project": project_id})
    if scope is None:
        raise ValueError("publication support scope is outside project")
    original = scope["original"]
    link = scope["evidence"]
    segment_id = scope["segments"][0] if len(scope["segments"]) == 1 else None
    source_key = f"dependency:{original['dependency_id']}"
    if original["field_name"] is not None:
        source_key += f":field:{original['field_name']}"
    fact = None
    if segment_id is not None and original["designated_by"].strip():
        fact = append_supporting_documentation_fact(session, project_id=project_id,
            subject_key=source_key, document_id=link["document_id"], recorded_by=original["designated_by"])
    if batch_id is None:
        command = "select refresh_support_scope(:project,:scope,:fact,:segment,:digest)"
    else:
        command = "select migrate_support_scope(:project,:batch,:scope,:fact,:segment,:digest)"
    return session.scalar(text(command), {"project": project_id, "batch": batch_id,
        "scope": scope_id, "fact": fact.id if fact else None, "segment": segment_id,
        "digest": scope["digest"]})


def migrate_support_history(session: Session, batch: HistoryBatch) -> tuple[dict, ...]:
    """Migrate the reviewed publication class, recording every native result/gap."""
    if session.scalar(text("show transaction_isolation")) != "read committed":
        raise ValueError("support migration requires read committed post-lock visibility")
    with session.begin_nested():
        for original in batch.classes["operative_support"]["rows"]:
            if original["role"] == "publication":
                _bind(session, project_id=batch.project_id, scope_id=original["id"], batch_id=batch.id)
    return tuple(dict(row) for row in session.execute(text("""
        select id,legacy_support_id,fact_decision_id,source_segment_id,outcome,reason,original_actor,original_time
        from support_history_receipts where batch_id=:batch and project_id=:project order by legacy_support_id
    """), {"batch": batch.id, "project": batch.project_id}).mappings())


def refresh_migrated_publication_support(session: Session, *, project_id: int, scope_id: int):
    """Refresh only an explicitly migrated scope inside its existing write act."""
    known = session.scalar(text("""
        select exists(select 1 from support_scope_lineage l join operative_support s
        on l.legacy_dependency_id=s.dependency_id and l.field_name is not distinct from s.field_name
        where l.project_id=:project and s.id=:scope
          and exists(select 1 from support_history_receipts r where r.scope_id=l.id and r.batch_id is not null
            and r.outcome='native' and not exists(select 1 from legacy_history_reversals v where v.batch_id=r.batch_id)))
    """), {"project": project_id, "scope": scope_id})
    if known:
        _bind(session, project_id=project_id, scope_id=scope_id)


def refresh_migrated_support_for_dependencies(session: Session, project_id: int, dependency_ids):
    ids = tuple(dependency_ids)
    if not ids:
        return
    for scope in session.scalars(select(OperativeSupport).where(
        OperativeSupport.dependency_id.in_(ids), OperativeSupport.role == "publication")):
        refresh_migrated_publication_support(session, project_id=project_id, scope_id=scope.id)


def native_publication_support(session: Session, dependency_ids) -> dict[tuple[int, str | None], NativePublicationSupport]:
    """Read accepted document choices and words natively; refuse stale coverage.

    The explicit compatibility boundary checks only a source-state fingerprint.
    A missed legacy writer or newly unsupported source therefore cannot leave an
    old native choice looking current. The chosen document/decision/words come
    from native Facts, decisions and Source Segments, never copied current fields.
    """
    ids = list(dependency_ids)
    if not ids:
        return {}
    rows = session.execute(text("""
        select l.legacy_dependency_id as dependency_id,l.field_name,l.subject_id,
          f.document_value_id as document_id,r.legacy_evidence_link_id as evidence_lineage_id,
          s.id as source_segment_id,s.exact_text,d.id as fact_decision_id,d.revision_id,
          r.original_actor,r.original_time,
          coalesce(authority.human_principal,authority.released_policy) as decision_actor,
          d.decided_at as decision_time,
          case when authority.human_principal is not null then 'human' else 'released_policy' end as authority_kind
        from support_scope_lineage l
        join lateral (select r.* from support_history_receipts r where r.scope_id=l.id
          and (r.batch_id is null or not exists(select 1 from legacy_history_reversals v where v.batch_id=r.batch_id))
          order by r.id desc limit 1) r on true
        join fact_decisions d on d.id=r.fact_decision_id and d.superseded_by is null and d.disposition='include'
        join facts f on f.id=d.fact_id and f.project_id=l.project_id and d.project_id=l.project_id
          and f.subject_key=l.fact_subject_key and f.fact_type='supporting_documentation_in_use'
        join project_record_revisions authority on authority.id=d.revision_id and authority.project_id=l.project_id
        join source_segments s on s.id=r.source_segment_id and s.document_id=f.document_value_id and s.project_id=l.project_id
        where l.legacy_dependency_id=any(cast(:ids as bigint[])) and r.outcome='native'
          and exists(select 1 from support_history_receipts admitted where admitted.scope_id=l.id and admitted.batch_id is not null
            and admitted.outcome='native' and not exists(select 1 from legacy_history_reversals v where v.batch_id=admitted.batch_id))
          and support_scope_source(l.project_id,r.legacy_support_id)->>'digest'=r.original_scope_sha256
          and jsonb_array_length(support_scope_source(l.project_id,r.legacy_support_id)->'segments')=1
          and s.content_sha256=encode(sha256(convert_to(s.exact_text,'UTF8')),'hex')
    """), {"ids": ids}).mappings()
    result = {}
    for row in rows:
        values = dict(row)
        values["subject_id"] = str(values["subject_id"])
        result[(row["dependency_id"], row["field_name"])] = NativePublicationSupport(**values)
    return result


@dataclass(frozen=True)
class SupportScopeIdentity:
    scope_id: int
    project_id: int
    subject_id: str
    legacy_dependency_id: int
    field_name: str | None
    outcome: str
    reason: str
    native_route_active: bool


def native_publication_support_as_of_revision(session: Session, project_id: int,
                                               revision_id: int) -> dict[tuple[int, str | None], NativePublicationSupport]:
    """Read retained source context at one exact accepted revision, never current.

    Missing context is omitted. Historical mutable support rows cannot fill a
    gap before their original recorded time, and a future native import cannot
    become part of an earlier revision. Withdrawing routing preserves history.
    """
    if not session.scalar(text("select exists(select 1 from project_record_revisions where id=:revision and project_id=:project)"),
                          {"project": project_id, "revision": revision_id}):
        raise ValueError("support revision does not belong to this project")
    rows = session.execute(text("""
        select l.legacy_dependency_id as dependency_id,l.field_name,l.subject_id,
          f.document_value_id as document_id,r.legacy_evidence_link_id as evidence_lineage_id,
          s.id as source_segment_id,s.exact_text,d.id as fact_decision_id,d.revision_id,
          r.original_actor,r.original_time,
          coalesce(authority.human_principal,authority.released_policy) as decision_actor,
          d.decided_at as decision_time,
          case when authority.human_principal is not null then 'human' else 'released_policy' end as authority_kind
        from support_scope_lineage l
        join project_record_revisions boundary on boundary.id=:revision and boundary.project_id=l.project_id
        join lateral (
          select r.* from support_history_receipts r left join fact_decisions recorded on recorded.id=r.fact_decision_id
          where r.scope_id=l.id and r.original_time<=boundary.recorded_at
            and (r.outcome='retained_compatibility' or (recorded.revision_id<=:revision
              and not exists(select 1 from fact_decisions successor where successor.id=recorded.superseded_by and successor.revision_id<=:revision)))
          order by r.original_time desc,r.id desc limit 1
        ) r on true
        join fact_decisions d on d.id=r.fact_decision_id and d.disposition='include' and d.project_id=l.project_id
        join facts f on f.id=d.fact_id and f.project_id=l.project_id and f.subject_key=l.fact_subject_key
        join project_record_revisions authority on authority.id=d.revision_id and authority.project_id=l.project_id
        join source_segments s on s.id=r.source_segment_id and s.project_id=l.project_id and s.document_id=f.document_value_id
        where l.project_id=:project and f.fact_type='supporting_documentation_in_use'
          and s.content_sha256=encode(sha256(convert_to(s.exact_text,'UTF8')),'hex')
    """), {"project": project_id, "revision": revision_id}).mappings()
    return {(row["dependency_id"], row["field_name"]): NativePublicationSupport(
        **{**dict(row), "subject_id": str(row["subject_id"])}) for row in rows}


def support_scope_identities(session: Session, project_id: int, *, revision_id: int | None = None) -> tuple[SupportScopeIdentity, ...]:
    """Enumerate native support scope identities and precise coverage omissions."""
    rows = session.execute(text("""
        select l.id as scope_id,l.project_id,l.subject_id,l.legacy_dependency_id,l.field_name,
          coalesce(r.outcome,'retained_compatibility') as outcome,
          coalesce(r.reason,'no_retained_support_reading') as reason
        from support_scope_lineage l
        left join lateral (select * from support_history_receipts r where r.scope_id=l.id order by r.id desc limit 1) r on true
        where l.project_id=:project order by l.id
    """), {"project": project_id}).mappings().all()
    if revision_id is None:
        available = native_publication_support(session, {row["legacy_dependency_id"] for row in rows})
    else:
        available = native_publication_support_as_of_revision(session, project_id, revision_id)
    result = []
    for row in rows:
        key = (row["legacy_dependency_id"], row["field_name"])
        active = key in available
        reason = row["reason"]
        if not active and row["outcome"] == "native":
            reason = ("no_retained_source_context_at_selected_revision" if revision_id is not None
                      else "migration_withdrawn_or_source_or_decision_changed")
        result.append(SupportScopeIdentity(row["scope_id"], project_id, str(row["subject_id"]),
            row["legacy_dependency_id"], row["field_name"], "native" if active else "retained_compatibility", reason, active))
    return tuple(result)
