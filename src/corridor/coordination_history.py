"""Native Coordination Decision writes and current/as-of reads (ADR-0082).

These values are human coordination decisions, never Source Facts. The native
reader uses independent subject identities and native decision/revision rows.
An explicit compatibility adapter returns old IDs while existing callers move;
the old IDs never identify the authoritative native subject or decision.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
import json
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.legacy_history import HistoryBatch


@dataclass(frozen=True)
class CoordinationRecordDecision:
    id: int
    project_id: int
    subject_id: str
    revision_id: int
    field: str
    decision_type: str
    value_text: str | None
    action_due_date: date | None
    action_due_date_reason: str | None
    milestone_ids: tuple[int, ...]
    deferral_reason: str | None
    deferral_return_date: date | None
    no_follow_up_reason: str | None
    cancellation_reason: str | None
    note: str | None
    recorded_by: str
    recorded_at: datetime
    predecessor_id: int | None


@contextmanager
def _operation(session):
    key = "corridor.coordination_operation"
    previous = session.info.get(key)
    if previous is None:
        session.info[key] = f"coordinate:{uuid4()}"
    try:
        yield session.info[key]
    finally:
        if previous is None:
            session.info.pop(key, None)


def coordination_operation(function):
    """Nested field commands share their outer Save's one revision identity."""
    @wraps(function)
    def wrapped(session, *args, **kwargs):
        with _operation(session):
            return function(session, *args, **kwargs)
    return wrapped


def mirror_coordination_decision(session: Session, *, project_id: int, legacy_decision_id: int):
    """Maintain the migrated native chain inside the existing atomic command."""
    with _operation(session) as operation:
        return session.scalar(text("select mirror_coordination_decision(:project,:decision,:operation)"),
            {"project": project_id, "decision": legacy_decision_id, "operation": operation})


def migrate_coordination_history(session: Session, batch: HistoryBatch) -> tuple[CoordinationRecordDecision, ...]:
    """Backfill a reviewed batch, preserving original actors, times and groups."""
    session.execute(text("select migrate_coordination_history(:project,:batch)"),
                    {"project": batch.project_id, "batch": batch.id})
    return read_coordination_record(session, batch.project_id)


def sync_coordination_reversals(session: Session, project_id: int):
    session.execute(text("select sync_coordination_reversals(:project)"), {"project": project_id})


def read_coordination_record(session: Session, project_id: int, *, at: datetime | None = None) -> tuple[CoordinationRecordDecision, ...]:
    """Read native accepted coordination values at their original decision time."""
    if at is not None and at.tzinfo is None:
        raise ValueError("Coordination Decision as-of time must be timezone-aware")
    if at is None:
        query = "select d.* from current_coordination_record d where project_id=:project order by subject_id,field"
    else:
        query = """
            select d.* from coordination_record_decisions d where d.project_id=:project and d.recorded_at<=:at
            and not exists(select 1 from coordination_record_decisions s where s.predecessor_id=d.id and s.recorded_at<=:at)
            and not exists(select 1 from coordination_record_reversals r where r.decision_id=d.id and r.recorded_at<=:at)
            order by d.subject_id,d.field
        """
    rows = session.execute(text(query), {"project": project_id, "at": at}).mappings()
    return tuple(_decision(row) for row in rows)



def read_coordination_record_as_of_revision(session: Session, project_id: int,
                                             revision_id: int) -> tuple[CoordinationRecordDecision, ...]:
    """Project the same accepted revision boundary used by source Fact readers.

    Original decision time remains provenance. Revision identity controls this
    reading, so a grouped Save appears atomically and a future import cannot
    become part of an already-published earlier revision.
    """
    if not session.scalar(text("select exists(select 1 from project_record_revisions where id=:revision and project_id=:project)"),
                          {"project": project_id, "revision": revision_id}):
        raise ValueError("Coordination revision does not belong to this project")
    rows = session.execute(text("""
        select d.* from coordination_record_decisions d
        where d.project_id=:project and d.revision_id<=:revision
          and not exists(select 1 from coordination_record_decisions s
                         where s.predecessor_id=d.id and s.revision_id<=:revision)
          and not exists(select 1 from coordination_record_reversals r
                         where r.decision_id=d.id and r.revision_id<=:revision)
        order by d.subject_id,d.field
    """), {"project": project_id, "revision": revision_id}).mappings()
    return tuple(_decision(row) for row in rows)

def _decision(row):
    values = dict(row)
    values["subject_id"] = str(values["subject_id"])
    values["milestone_ids"] = tuple(values["milestone_ids"])
    return CoordinationRecordDecision(**values)


def _composite(decision):
    if decision.field == "internal_owner":
        return decision.value_text
    if decision.field == "next_action":
        if decision.value_text is None and decision.action_due_date is None:
            return None
        value = {"action": decision.value_text, "due_date": decision.action_due_date.isoformat() if decision.action_due_date else None}
    elif decision.field == "milestone_impact":
        if decision.value_text is None:
            return None
        value = {"state": decision.value_text, "milestone_ids": list(decision.milestone_ids)}
    else:
        if decision.deferral_reason is None and decision.deferral_return_date is None:
            return None
        value = {"reason": decision.deferral_reason, "return_date": decision.deferral_return_date.isoformat()}
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compatibility_coordination_tail(session: Session, *, dependency_id: int | None,
                                   commitment_lineage_id: int | None, field: str):
    """Return (route-known, legacy-shaped native reading) for old consumers.

    Only the identity adapter consults compatibility mappings. Every displayed
    value, actor, time and predecessor value comes from native decision rows.
    A withdrawn migration routes back to retained legacy history explicitly.
    """
    subject = session.execute(text("""
        select * from coordination_subject_lineage l
        where (legacy_dependency_id=:dependency or legacy_commitment_lineage_id=:lineage)
        and exists(select 1 from coordination_history_activations a where a.subject_id=l.subject_id
            and not exists(select 1 from legacy_history_reversals r where r.batch_id=a.history_batch_id))
    """), {"dependency": dependency_id, "lineage": commitment_lineage_id}).mappings().one_or_none()
    if subject is None:
        return False, None
    row = session.execute(text("""
        select d.* from current_coordination_record d where subject_id=:subject and field=:field
    """), {"subject": subject["subject_id"], "field": field}).mappings().one_or_none()
    if row is None:
        return True, None
    decision = _decision(row)
    lineage = session.execute(text("select * from coordination_decision_lineage where decision_id=:id"),
                              {"id": decision.id}).mappings().one()
    previous = None
    previous_legacy = None
    if decision.predecessor_id is not None:
        prior = session.execute(text("select * from coordination_record_decisions where id=:id"),
                                {"id": decision.predecessor_id}).mappings().one()
        previous = _decision(prior)
        previous_legacy = session.scalar(text("select legacy_work_decision_id from coordination_decision_lineage where decision_id=:id"),
                                         {"id": decision.predecessor_id})
    return True, SimpleNamespace(
        id=lineage["legacy_work_decision_id"], native_decision_id=decision.id,
        dependency_id=subject["legacy_dependency_id"], commitment_lineage_id=subject["legacy_commitment_lineage_id"],
        field=decision.field, decision_type=decision.decision_type,
        before_value=_composite(previous) if previous else None, after_value=_composite(decision),
        recorded_by=decision.recorded_by, recorded_at=decision.recorded_at,
        predecessor_decision_id=previous_legacy, action_due_date_reason=decision.action_due_date_reason,
        no_follow_up_reason=decision.no_follow_up_reason, cancellation_reason=decision.cancellation_reason,
        note=decision.note, deferral_reason=decision.deferral_reason, deferral_return_date=decision.deferral_return_date,
        **lineage["observation_lineage"],
    )


def compatibility_statement_tails(session: Session, lineage_ids, fields):
    """Override every migrated subject, including an explicitly empty native tail."""
    ids = tuple(lineage_ids)
    if not ids:
        return frozenset(), {}
    known = frozenset(session.scalars(text("""
        select legacy_commitment_lineage_id from coordination_subject_lineage l
        where legacy_commitment_lineage_id=any(cast(:ids as bigint[]))
        and exists(select 1 from coordination_history_activations a where a.subject_id=l.subject_id
            and not exists(select 1 from legacy_history_reversals r where r.batch_id=a.history_batch_id))
    """), {"ids": list(ids)}))
    readings = {}
    for lineage_id in known:
        for field in fields:
            _known, decision = compatibility_coordination_tail(session, dependency_id=None,
                commitment_lineage_id=lineage_id, field=field)
            if decision is not None:
                readings[(lineage_id, field)] = decision
    return known, readings


def coordination_migration_gaps(session: Session, batch: HistoryBatch) -> tuple[dict, ...]:
    """Declare exact rows still served as compatibility history, never relabel actors."""
    return tuple(dict(row) for row in session.execute(text("""
        select (r->>'id')::bigint as legacy_work_decision_id,
               r->>'recorded_by' as original_actor,
               'Subject history has an unattributed or conflicting actor; retain compatibility history.' as reason
        from legacy_history_batches b,
             jsonb_array_elements(b.payload->'classes'->'work_decisions'->'rows') r
        where b.id=:batch and b.project_id=:project
          and not exists(select 1 from coordination_decision_lineage l where l.legacy_work_decision_id=(r->>'id')::bigint)
        order by (r->>'id')::bigint
    """), {"batch": batch.id, "project": batch.project_id}).mappings())
