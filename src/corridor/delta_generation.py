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

The comparison itself — when a stated value is agreed, a modify, an add, or a
new subject — is ``proposed_delta_comparison``, shared with the four capture
paths that also produce deltas. This module owns which Facts are considered,
where the pass resumes, and the rule version it compares under.

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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.due_work_contract import (
    DECLARED_IDENTITY,
    DueWorkRefusal,
    DueWorkScheduling,
    HandlerRegistration,
    ResolvedSchedule,
    ValidatedDeclaration,
    gate7_configuration,
    previous_completed_reading,
    validate_scheduling,
)
from corridor.fact_types import SINGLE_VALUED_FACT_TYPES
from corridor.fact_values import scalar_fact_value
from corridor.models import (
    Document,
    DueWorkSchedule,
    Fact,
    ProjectRecordRevision,
    ProposedDelta,
)
from corridor.operating_mode import project_operating_mode
from corridor.proposed_delta_comparison import (
    StatedSubject,
    accepted_values,
    compare_stated_subjects,
    revision_label,
)
from corridor.proposed_deltas import create_proposed_delta_group

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

    The retained reading is read through the runtime's own seam, so what
    "newest completed attempt" means is decided once for every handler that
    continues from where its last pass stopped.
    """

    previous = previous_completed_reading(
        session, schedule_id=schedule_id, handler_key=HANDLER_KEY
    )
    return int(previous.get("through_fact_id") or 0)


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
            accepted = accepted_values(working, project_id)
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
                stated: list[StatedSubject] = []
                for subject_key, subject_facts in _by_subject(document_facts):
                    considered += len(subject_facts)
                    stated.append(
                        StatedSubject(
                            subject_identity=subject_key,
                            values=tuple(
                                (fact.fact_type, scalar_fact_value(fact))
                                for fact in subject_facts
                            ),
                            # A subject the record does not hold becomes one
                            # delta carrying its initial fields, which is what
                            # ADR-0082 means by one atomic source change; the
                            # comparison owns that shape for every producer.
                            paired=subject_key in accepted_subjects,
                        )
                    )
                comparison = compare_stated_subjects(
                    accepted=accepted,
                    stated=tuple(stated),
                    comparison_rule_version=COMPARISON_RULE_VERSION,
                    accepted_baseline_revision=revision_label(baseline_revision),
                )
                agreed += comparison.values_agreed
                proposals = comparison.deltas
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
        "accepted_baseline_revision": revision_label(baseline_revision) or "",
        "facts_considered": considered,
        "facts_agreed": agreed,
        "groups_created": groups,
        "deltas_created": created,
        "deltas_already_present": existing,
        "through_fact_id": through,
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


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DeltaGenerationRefusal(
            "delta-generation clock must supply an aware datetime"
        )
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()


# --- The Due Work declaration this pass runs under ------------------------
#
# The comparison rule the pass appends under, and the fact that it proposes and
# never accepts, are this module's statements about its own work; the runtime
# keeps the lease and the retries (card 6).


@dataclass(frozen=True)
class DeltaGenerationDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables Proposed Delta generation.

    Scope names the project and the comparison rule the pass appends under, so
    a later change to how values are compared is visible on every delta rather
    than retroactive.  The pass reads no model and appends only through the
    source-append command, so both budgets are a declared zero, concurrency
    stays one, and no destination is authorized: it can propose a difference
    and can never make one effective.
    """

    handler_key: ClassVar[str] = HANDLER_KEY

    comparison_rule_version: str

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        comparison_rule_version: str,
        starts_at: datetime,
    ) -> "DeltaGenerationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            comparison_rule_version=comparison_rule_version,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=900,
            deadline_seconds=600,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


def _validated_declaration(
    declaration: DeltaGenerationDeclaration,
) -> ValidatedDeclaration:
    """Validate one delta-generation declaration."""

    if not DECLARED_IDENTITY.fullmatch(declaration.comparison_rule_version):
        raise DueWorkRefusal("delta-generation comparison rule identity is invalid")
    starts_at = validate_scheduling(declaration, subject="delta-generation")
    scope = {
        "project_id": declaration.project_id,
        "comparison_rule_version": declaration.comparison_rule_version,
    }
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_KEY,
            scope=scope,
            input_identity={
                "kind": "project_proposed_delta_generation-v1",
                **scope,
            },
            idempotency_contract="at_least_once_reconcilable",
            starts_at=starts_at,
            # The pass proposes; it never makes anything effective. Every append
            # goes through the source-append command, so an adopted-baseline
            # project's accepted values are unreachable from here (#520).
            extra={"record_authority": "proposes_only_never_accepts"},
        ),
        input_identity={"handler": HANDLER_KEY, **scope},
    )


def _stored_declaration(stored: ResolvedSchedule) -> DeltaGenerationDeclaration:
    return DeltaGenerationDeclaration(
        **stored.scheduling_fields(),
        comparison_rule_version=stored.scope.get("comparison_rule_version", ""),
    )


def _run_due_work(context) -> dict[str, Any]:
    """Propose one project's new differences from its accepted record (#518).

    The pass commits its Proposed Deltas durably through the session factory
    before the runtime finalizes the claim, and appends only through the
    source-append command, so it can never write an accepted value on any path
    and a recovered re-run appends nothing that is already there.
    """

    return execute_delta_generation(
        context.session_factory,
        schedule_id=context.schedule.schedule_id,
        clock=context.clock,
    )


DUE_WORK_REGISTRATION = HandlerRegistration(
    key=HANDLER_KEY,
    scope_kind="one_project_proposed_delta_generation",
    idempotency_contract="at_least_once_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=0,
    declaration_type=DeltaGenerationDeclaration,
    validate=_validated_declaration,
    stored_declaration=_stored_declaration,
    run_effectful=_run_due_work,
)
