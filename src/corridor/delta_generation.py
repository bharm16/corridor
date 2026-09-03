"""Turn newly captured Source Facts into Proposed Deltas on a schedule (#488, #518).

ADR-0076 puts one step between what a source says and what the record says: a
typed difference from the accepted record, open until a human resolves it.
``proposed_deltas`` owns that identity, grouping, and lifecycle; nothing was
producing them, so a captured Source Fact and the accepted value it disagrees
with simply sat in two tables. This module is the recurring pass that compares
them.

Two rules shape it. The accepted record is read, never written: every write
goes through ``append_proposed_deltas``, the source-append command the runtime
role is allowed to call, so an adopted-baseline project (#520) cannot have an
accepted value replaced by this pass on any path. And a genuinely new subject
becomes **one** delta carrying its initial fields rather than an unrelated
delta per field, which is what ADR-0082 means by one atomic source change.

Where the pass resumes was the design question. A watermark column was
rejected: the migration window is closed (``corridor.migrations.policy``), and
the runtime already retains one durable, append-only, ordered receipt per
attempt. The highest Source Fact this pass considered is returned in the
handler result and stands on the completed receipt, so a crash before the
receipt re-considers facts whose deltas are already appended — and the append
command is content-addressed, so re-considering them creates nothing new.

This module owns no schedule, timer, or clock. The one supervised Due Work
runtime (#332) discovers, claims, and retries occurrences.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from corridor.fact_types import SINGLE_VALUED_FACT_TYPES
from corridor.models import (
    Document,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    Fact,
    ProjectRecordRevision,
    ProposedDelta,
)
from corridor.operating_mode import project_operating_mode
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    ProposedSubjectTarget,
    create_proposed_delta_group,
)

# The one server-owned handler key this module's work runs under.  It matches
# ``due_work.HANDLER_DELTA_GENERATION``; the constant lives here because this
# module is the lower layer and the runtime imports its execution, never the
# reverse.
HANDLER_KEY = "delta_generation"

_RESULT_SCHEMA_VERSION = "delta-generation-result-v1"

# The rule this pass compares under, recorded on every delta it appends so a
# later comparison change is visible rather than retroactive.
COMPARISON_RULE_VERSION = "structured-cell-value-comparison-v1"

# Only single-valued structured cells are compared. The satellite types
# (`applies_to`, `closure_result`) and the prose/timing types carry their own
# structures, and a scalar comparison of them would be a guess.
COMPARABLE_FACT_TYPES = frozenset(SINGLE_VALUED_FACT_TYPES)

# One pass appends at most this many deltas, so a first run over a large
# backlog stays inside its declared deadline and the next slot continues from
# the watermark it retained.
DELTA_BUDGET = 500


class DeltaGenerationRefusal(ValueError):
    """A delta-generation pass cannot read its scope safely."""


def last_considered_fact_id(session: Session, schedule_id: int) -> int:
    """The highest Source Fact this schedule's newest completed attempt saw.

    Ordered by receipt identity rather than ``finished_at``: the identifier is
    monotonic in insertion order no matter what any clock said, and "newest"
    here must mean the last one retained.
    """

    result = session.scalars(
        select(DueWorkReceipt.handler_result_json)
        .join(
            DueWorkOccurrence,
            DueWorkOccurrence.id == DueWorkReceipt.occurrence_id,
        )
        .where(
            DueWorkOccurrence.scheduled_job_id == schedule_id,
            DueWorkReceipt.handler_key == HANDLER_KEY,
            DueWorkReceipt.execution_outcome == "completed",
        )
        .order_by(DueWorkReceipt.id.desc())
        .limit(1)
    ).first()
    return int((result or {}).get("through_fact_id") or 0)


def execute_delta_generation(
    session_factory, *, schedule_id: int, clock
) -> dict[str, Any]:
    """Compare one project's new Source Facts to its accepted record, once."""

    observed_at = _aware_utc(clock.now())
    with session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, schedule_id)
        if schedule is None:
            raise DeltaGenerationRefusal("delta-generation schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        watermark = last_considered_fact_id(reading, schedule_id)

    with session_factory() as working:
        with working.begin():
            accepted = _accepted_values(working, project_id)
            accepted_subjects = {subject for subject, _ in accepted}
            baseline_revision = working.scalar(
                select(func.max(ProjectRecordRevision.id)).where(
                    ProjectRecordRevision.project_id == project_id
                )
            )
            facts = working.scalars(
                select(Fact)
                .where(
                    Fact.project_id == project_id,
                    Fact.id > watermark,
                    Fact.document_id.is_not(None),
                    Fact.fact_type.in_(sorted(COMPARABLE_FACT_TYPES)),
                )
                .order_by(Fact.id)
                .limit(DELTA_BUDGET)
            ).all()

            considered = 0
            agreed = 0
            created = 0
            existing = 0
            groups = 0
            through = watermark
            for document_id, document_facts in _by_document(facts):
                lineage = _lineage(working, project_id, document_id)
                if lineage is None:
                    continue
                source_family, source_revision = lineage
                proposals: list[ProposedDeltaValues] = []
                for subject_key, subject_facts in _by_subject(document_facts):
                    considered += len(subject_facts)
                    if subject_key not in accepted_subjects:
                        proposals.append(
                            _proposed_subject(
                                subject_key, subject_facts, baseline_revision
                            )
                        )
                        continue
                    for fact in subject_facts:
                        proposed_value = _fact_value(fact)
                        key = (subject_key, fact.fact_type)
                        if key in accepted:
                            if accepted[key] == proposed_value:
                                agreed += 1
                                continue
                            change_type = "modify"
                        else:
                            change_type = "add"
                        proposals.append(
                            ProposedDeltaValues(
                                change_type=change_type,
                                target=ExistingSubjectTarget(
                                    subject_identity=subject_key,
                                    field=fact.fact_type,
                                ),
                                accepted_value=accepted.get(key),
                                proposed_value=proposed_value,
                                comparison_rule_version=COMPARISON_RULE_VERSION,
                                accepted_baseline_revision=_revision_label(
                                    baseline_revision
                                ),
                            )
                        )
                if not proposals:
                    continue
                # The Proposed Delta's identity is the database's since #457,
                # so the append converges on the rows a replay already wrote
                # and this pass reads back which of them it created. There is
                # no second, weaker definition of that identity here: the one
                # that lived in ``_appended_signatures`` ignored
                # ``accepted_value`` while the delta digest includes it, and
                # ADR-0089 removed it rather than let two definitions of one
                # identity disagree.
                before = working.scalar(select(func.max(ProposedDelta.id))) or 0
                appended = create_proposed_delta_group(
                    working,
                    project_id=project_id,
                    source_family=source_family,
                    source_revision=source_revision,
                    document_id=document_id,
                    deltas=proposals,
                )
                fresh = [row for row in appended if row.id > before]
                existing += len(appended) - len(fresh)
                if not fresh:
                    continue
                groups += 1
                created += len(fresh)
            if facts:
                # The whole batch is either compared or unusable (its document
                # is gone), and the append is one all-or-nothing transaction,
                # so the watermark moves to the end of what was read. Leaving
                # it behind would re-read the same rows on every slot forever.
                through = max(through, facts[-1].id)
            operating_mode = project_operating_mode(working, project_id)

    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "project_id": project_id,
        "configuration_version": configuration_version,
        "observed_at": _iso(observed_at),
        "health": (
            "healthy"
            if baseline_revision is not None
            else "delta_generation_attention_required"
        ),
        "operating_mode": operating_mode,
        "accepted_baseline_revision": _revision_label(baseline_revision) or "",
        "facts_considered": considered,
        "facts_agreed": agreed,
        "groups_created": groups,
        "deltas_created": created,
        "deltas_already_present": existing,
        "through_fact_id": through,
    }


def _accepted_values(session: Session, project_id: int) -> dict[tuple[str, str], Any]:
    """The accepted record as one comparable scalar per subject and field.

    The projection is read through ``current_project_record``, the view
    ``current_record`` proves; only the scalar the comparison needs is taken,
    so this pass does not pull the report and export stack behind it.
    """

    rows = session.execute(
        text(
            "select subject_key, fact_type, text_value, date_value, "
            "external_org_value_id, document_value_id "
            "from current_project_record where project_id = :project_id"
        ),
        {"project_id": project_id},
    ).all()
    return {
        (subject_key, fact_type): _typed_value(
            text_value, date_value, external_org_value_id, document_value_id
        )
        for (
            subject_key,
            fact_type,
            text_value,
            date_value,
            external_org_value_id,
            document_value_id,
        ) in rows
    }


def _lineage(
    session: Session, project_id: int, document_id: int
) -> tuple[str, str] | None:
    """One source family and the exact revision of it these facts came from."""

    row = session.execute(
        select(Document.registry_id, Document.sha256).where(
            Document.id == document_id, Document.project_id == project_id
        )
    ).first()
    if row is None:
        return None
    registry_id, sha256 = row
    family = (registry_id or f"document:{document_id}")[:64]
    return family, str(sha256)[:128]



def _proposed_subject(
    subject_key: str, facts: list[Fact], baseline_revision: int | None
) -> ProposedDeltaValues:
    """One delta carrying a new subject's initial fields, not one per field."""

    fields = tuple(sorted({fact.fact_type for fact in facts}))
    return ProposedDeltaValues(
        change_type="add",
        target=ProposedSubjectTarget(
            subject_identity=subject_key, proposed_fields=fields
        ),
        proposed_value={fact.fact_type: _fact_value(fact) for fact in facts},
        comparison_rule_version=COMPARISON_RULE_VERSION,
        accepted_baseline_revision=_revision_label(baseline_revision),
    )


def _by_document(facts) -> list[tuple[int, list[Fact]]]:
    grouped: dict[int, list[Fact]] = {}
    for fact in facts:
        grouped.setdefault(int(fact.document_id), []).append(fact)
    return sorted(grouped.items())


def _by_subject(facts: list[Fact]) -> list[tuple[str, list[Fact]]]:
    grouped: dict[str, list[Fact]] = {}
    for fact in facts:
        grouped.setdefault(fact.subject_key, []).append(fact)
    return sorted(grouped.items())


def _fact_value(fact: Fact) -> Any:
    return _typed_value(
        fact.text_value,
        fact.date_value,
        fact.external_org_value_id,
        fact.document_value_id,
    )


def _typed_value(
    text_value, date_value, external_org_value_id, document_value_id
) -> Any:
    if text_value is not None:
        return text_value
    if date_value is not None:
        return date_value.isoformat()
    if external_org_value_id is not None:
        return {"external_org_id": int(external_org_value_id)}
    if document_value_id is not None:
        return {"document_id": int(document_value_id)}
    return None


def _revision_label(revision_id: int | None) -> str | None:
    return f"revision:{revision_id}" if revision_id is not None else None


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DeltaGenerationRefusal(
            "delta-generation clock must supply an aware datetime"
        )
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()
