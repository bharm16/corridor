"""The technical-operations screen's reading, and none of its decisions.

#344 built the operations surface as a composite read inside `web/app.py`, and
its own docstring said why that read had to stay in one place: "keeping this
composite read here prevents an operator UI from reimplementing any of their
safety decisions". It then reimplemented two of them. ``explanation_offered``
was ``len(competing) >= 2`` in the adapter, beside the identical ``< 2``
refusal in ``production_run_explanation``; ``record_inclusion_pending`` was
``handoff.dirty_seq > handoff.reconciled_seq`` in the adapter, beside the
identical predicate in ``record_inclusion``. Both are now read from their
owners, and this module is the reading that consumes them — the counterpart of
``corridor.web.follow_up_view`` and ``corridor.web.issue_section`` for the
operator's screen.

**Every safety decision belongs to the module that owns the record.** Active
Run declarations own the chosen reading, Event Admission owns effective policy
status, Due Work owns recovery state, ``production_run_explanation`` owns
whether runs compete, ``extraction_failure_diagnosis`` owns which attempts are
failures, and ``record_inclusion`` owns whether a load is pending. Nothing here
decides any of that; it reads them once each and arranges the answers.

**A stale-state token is not an authorization token.** Each token below binds a
control to the exact facts the screen rendered, so a changed declaration chain,
run set, page, or policy state makes an obsolete submission refuse rather than
apply. Authority is still obtained server-side by the route that writes. The
tokens are computed by the modules that define their payloads, from rows this
reading already loaded, so the digest a control carries is the digest the
refusal recomputes.

**Bounded queries, not a query per document.** The screen used to issue eight
project-wide selects and then, per document, one for competing runs, one for
failed runs, one for the declaration tail, one for the offer digest, one for the
explain digest, and one per failed attempt for the diagnosis digest — so a
project with twenty documents read its runs twenty-one times. Documents,
quarantines, declarations, declaration history and runs are read once each here
and every per-document answer is composed from them.

**No clock.** Nothing here reads the day. Recovery occurrences carry their own
retained instants.

Terminology: nothing here coins a customer word. Current Production Run, Record
Inclusion, Held input and Source Document are the adopted internal operations
words the screen already prints.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from hashlib import sha256
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.due_work import (
    HANDLER_PROJECT_PROCESSING,
    HANDLER_REVISION_RECONCILIATION,
)
from corridor.event_admission import read_event_admission_policy_status
from corridor.extraction_failure_diagnosis import (
    PROMPT_VERSION as FAILURE_DIAGNOSIS_PROMPT_VERSION,
    current_configuration as current_failure_diagnosis_configuration,
    failed_runs_among,
    failure_diagnosis_state_tokens,
)
from corridor.models import (
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Document,
    DocumentQuarantine,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    ExtractionRun,
)
from corridor.production_run_explanation import (
    PROMPT_VERSION as RUN_EXPLANATION_PROMPT_VERSION,
    competing_runs_among,
    competing_runs_token,
    current_configuration as current_run_explanation_configuration,
    runs_compete,
)
from corridor.record_inclusion import record_inclusion_pending
from corridor.support_update_routing import operations_consequences

# The most recent recovery occurrences the screen lists. Recovery history is
# unbounded; the screen is not.
RECOVERY_OCCURRENCE_LIMIT = 50


def _state_fingerprint(value: dict) -> str:
    """Bind an operations form to the exact facts its screen rendered.

    A stale-state guard, not an authorization token: every mutation still
    obtains its scope and authority server-side. A canonical digest makes a
    changed declaration chain, completed-run set, proof, or permitted policy
    action refuse rather than silently applying an obsolete choice.
    """
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def policy_offer_state(status: Any) -> str:
    """The digest a policy control carries, over the status it was offered under."""
    return _state_fingerprint(
        {
            "status": status.status,
            "proof_status": status.proof_status,
            "latest_receipt_id": status.latest_receipt_id,
            "latest_action_id": status.latest_action_id,
            "allowed_operations": list(status.allowed_operations),
        }
    )


@dataclass(frozen=True, slots=True)
class DocumentRow:
    """One source document, its attempts, and the controls each attempt offers.

    ``offer_state`` and ``explain_state`` are the same digest over the same
    facts, because declaring a run and explaining the choice between runs are
    offered against one state: the document's completed runs and its current
    declaration. They are named separately because the two controls submit them
    to different routes, and a later divergence should be a visible change here
    rather than a silent one in a template.
    """

    document: Document
    active_run_id: int | None
    runs: tuple[ExtractionRun, ...]
    history: tuple[ActiveRunDeclaration, ...]
    quarantine: DocumentQuarantine | None
    offer_state: str
    competing_run_ids: tuple[int, ...]
    explanation_offered: bool
    explain_state: str
    diagnosis_offered: bool
    failed_run_tokens: dict[int, str]


@dataclass(frozen=True, slots=True)
class RecoveryRow:
    """One recovery occurrence and every attempt receipt retained under it."""

    occurrence: DueWorkOccurrence
    receipts: tuple[DueWorkReceipt, ...]


@dataclass(frozen=True, slots=True)
class OperationsView:
    """One project's bounded processing operations, as the operator reads them."""

    project_id: int
    documents: tuple[DocumentRow, ...]
    event_policy: Any
    policy_offer_state: str
    record_inclusion_pending: bool
    schedules: tuple[DueWorkSchedule, ...]
    recovery_rows: tuple[RecoveryRow, ...]
    run_explanation_configuration: Any
    failure_diagnosis_configuration: Any
    support_update_failures: tuple[Any, ...]
    run_explanation_prompt_version: str = RUN_EXPLANATION_PROMPT_VERSION
    failure_diagnosis_prompt_version: str = FAILURE_DIAGNOSIS_PROMPT_VERSION

    @property
    def scheduled_recovery_enabled(self) -> bool:
        """Whether a retained gate-7 configuration names this project."""
        return bool(self.schedules)


def operations_view(session: Session, *, project_id: int) -> OperationsView:
    """Read this project's operations facts, each from the module that owns it."""

    documents = tuple(
        session.scalars(
            select(Document)
            .where(Document.project_id == project_id)
            .order_by(Document.doc_date, Document.id)
        )
    )
    quarantines = {
        row.document_id: row
        for row in session.scalars(
            select(DocumentQuarantine)
            .join(Document, Document.id == DocumentQuarantine.document_id)
            .where(Document.project_id == project_id)
        )
    }
    declared = {
        row.document_id: row
        for row in session.scalars(
            select(ActiveExtractionRun)
            .join(Document, Document.id == ActiveExtractionRun.document_id)
            .where(Document.project_id == project_id)
        )
    }
    history_by_document: dict[int, list[ActiveRunDeclaration]] = {}
    for declaration in session.scalars(
        select(ActiveRunDeclaration)
        .join(Document, Document.id == ActiveRunDeclaration.document_id)
        .where(Document.project_id == project_id)
        .order_by(ActiveRunDeclaration.document_id, ActiveRunDeclaration.id)
    ):
        history_by_document.setdefault(declaration.document_id, []).append(declaration)
    runs_by_document: dict[int, list[ExtractionRun]] = {}
    for run in session.scalars(
        select(ExtractionRun)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project_id)
        .order_by(ExtractionRun.document_id, ExtractionRun.id)
    ):
        runs_by_document.setdefault(run.document_id, []).append(run)
    # Every failed attempt's token, in one further query for the pages they all
    # need, computed by the module whose payload it is.
    failure_tokens = failure_diagnosis_state_tokens(
        session,
        documents=documents,
        quarantines=quarantines,
        runs_by_document=runs_by_document,
    )

    document_rows = tuple(
        _document_row(
            document,
            active=declared.get(document.id),
            history=history_by_document.get(document.id, []),
            runs=runs_by_document.get(document.id, []),
            quarantine=quarantines.get(document.id),
            failed_run_tokens=failure_tokens.get(document.id, {}),
        )
        for document in documents
    )

    event_policy = read_event_admission_policy_status(session, project_id)
    return OperationsView(
        project_id=project_id,
        documents=document_rows,
        event_policy=event_policy,
        policy_offer_state=policy_offer_state(event_policy),
        # The handoff's own predicate, not a second reading of its two counters.
        record_inclusion_pending=record_inclusion_pending(session, project_id),
        schedules=(schedules := _schedules(session, project_id)),
        recovery_rows=_recovery_rows(session, project_id, schedules),
        run_explanation_configuration=current_run_explanation_configuration(
            session, project_id
        ),
        failure_diagnosis_configuration=current_failure_diagnosis_configuration(
            session, project_id
        ),
        # Technical replacement-support failures — a missing or failed
        # extraction, an undeclared run, a missing, corrupt, or duplicate
        # comparison, or broken admission lineage — are operations problems,
        # never a customer question (ADR-0034). They surface here with their
        # plain project consequence.
        support_update_failures=tuple(operations_consequences(session, project_id)),
    )


def _document_row(
    document: Document,
    *,
    active: ActiveExtractionRun | None,
    history: Sequence[ActiveRunDeclaration],
    runs: Sequence[ExtractionRun],
    quarantine: DocumentQuarantine | None,
    failed_run_tokens: dict[int, str],
) -> DocumentRow:
    competing = competing_runs_among(runs)
    # The declaration tail, from the history this reading already holds: the
    # rows are ordered by the append-only identifier, so the last one is the
    # current declaration.
    token = competing_runs_token(
        document_id=document.id,
        active_run_id=None if active is None else active.extraction_run_id,
        declaration_id=history[-1].id if history else None,
        completed_run_ids=[run.id for run in competing],
    )
    return DocumentRow(
        document=document,
        active_run_id=None if active is None else active.extraction_run_id,
        runs=tuple(runs),
        history=tuple(history),
        quarantine=quarantine,
        offer_state=token,
        competing_run_ids=tuple(run.id for run in competing),
        # An explanation is offered only when there is an actual choice, and
        # `production_run_explanation` is the one place that says what a choice
        # is — the same answer its request refusal gives.
        explanation_offered=runs_compete(competing),
        explain_state=token,
        # A diagnosis is offered per failed attempt; the tokens bind the exact
        # run and failure context the operator is looking at.
        diagnosis_offered=bool(failed_runs_among(runs)),
        failed_run_tokens=failed_run_tokens,
    )


def _schedules(session: Session, project_id: int) -> tuple[DueWorkSchedule, ...]:
    return tuple(
        session.scalars(
            select(DueWorkSchedule)
            .where(
                DueWorkSchedule.project_id == project_id,
                DueWorkSchedule.handler_key.in_(
                    (HANDLER_PROJECT_PROCESSING, HANDLER_REVISION_RECONCILIATION)
                ),
                DueWorkSchedule.disabled_at.is_(None),
            )
            .order_by(DueWorkSchedule.handler_key, DueWorkSchedule.id.desc())
        )
    )


def _recovery_rows(
    session: Session, project_id: int, schedules: Sequence[DueWorkSchedule]
) -> tuple[RecoveryRow, ...]:
    """The newest recovery occurrences of this project's own schedules."""
    schedule_ids = [schedule.id for schedule in schedules]
    if not schedule_ids:
        return ()
    receipts_by_occurrence: dict[int, list[DueWorkReceipt]] = {}
    for receipt in session.scalars(
        select(DueWorkReceipt)
        .where(DueWorkReceipt.project_id == project_id)
        .order_by(DueWorkReceipt.occurrence_id, DueWorkReceipt.attempt_number)
    ):
        receipts_by_occurrence.setdefault(receipt.occurrence_id, []).append(receipt)
    return tuple(
        RecoveryRow(
            occurrence=occurrence,
            receipts=tuple(receipts_by_occurrence.get(occurrence.id, ())),
        )
        for occurrence in session.scalars(
            select(DueWorkOccurrence)
            .where(DueWorkOccurrence.scheduled_job_id.in_(schedule_ids))
            .order_by(DueWorkOccurrence.id.desc())
            .limit(RECOVERY_OCCURRENCE_LIMIT)
        )
    )
