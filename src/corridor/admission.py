"""The stage that runs when a project's documents land.

Before ADR-0029 this was a screen: two checklists an operator signed
before the machine would read anything it had already extracted. The
product is the surfaced conflict and the evidence under it, so the
reading is not something to ask permission for — it is the thing the
user came for. This module is where the asking used to be.

It composes three acts that already existed and now run together, in
order, unattended: name the only reading each document has, admit the
conflicts the matrices can anchor, and attach the statements the minutes
place. Each keeps its own receipt; nothing here decides anything the
policies do not, and the composition is the whole point — one entry
point means one answer to "why is this row on my screen".

Running it twice is safe and nearly free: declaration skips what is
declared, and both policies skip what is already on the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.dependency_admission import (
    DependencyAdmissionResult,
    run_dependency_admission,
)
from corridor.event_admission import EventAdmissionResult, run_event_admission
from corridor.fact_decisions import include_current_stationing_facts
from corridor.extraction_runs import declare_single_run_documents_by_policy
from corridor.models import Project, RecordInclusionRequest
from corridor.record_inclusion import ReconcileResult
from corridor.unreadable_cell_admission import process_unreadable_cell_upgrades


@dataclass(frozen=True)
class LoadResult:
    """What one landing pass put on the record, and what it could not."""

    declared_documents: int
    # Documents holding several completed readings. Nothing was chosen
    # for them, and their conflicts are absent from the list until a
    # human declares which run is operative.
    ambiguous_documents: list[str]
    dependencies: DependencyAdmissionResult
    events: EventAdmissionResult
    stationing_decisions: tuple[object, ...]

    @property
    def admitted_count(self) -> int:
        return self.dependencies.admitted_count + self.events.admitted_count

    @property
    def waiting_count(self) -> int:
        """Rows the machine could not anchor, which a human now holds."""
        return self.dependencies.abstained_count + self.events.abstained_count


def load_project(session: Session, project_id: int) -> LoadResult:
    """Declare what is unambiguous, then admit what can be anchored.

    Dependencies before events, and not by preference: an event attaches
    to the Dependency its statement names, so a statement admitted before
    its conflict would find nothing to attach to and abstain for a reason
    that was only ever about ordering.
    """
    declarations = declare_single_run_documents_by_policy(session, project_id)
    dependencies = run_dependency_admission(session, project_id)
    stationing_decisions = include_current_stationing_facts(session, project_id)
    events = run_event_admission(session, project_id)
    # A corroborating document landing is exactly what upgrades an unconfirmed
    # unreadable-cell reading to corroborated (ADR-0064), and — only when the
    # ADR-0050-gated class is active — admits it. A no-op when the project has no
    # unreadable-cell readings, so ordinary loads are untouched.
    process_unreadable_cell_upgrades(session, project_id)
    return LoadResult(
        declared_documents=len(declarations.declared),
        ambiguous_documents=declarations.ambiguous,
        dependencies=dependencies,
        events=events,
        stationing_decisions=stationing_decisions,
    )


def reconcile_record_inclusion(
    session: Session, project_id: int
) -> ReconcileResult:
    """Run ``load_project`` for a project, but only while its watermark is pending.

    This is the consuming side of the Record Inclusion watermark (see
    :mod:`corridor.record_inclusion`). It lives here, beside the load it gates,
    because unconditional reconciliation is exactly what ``load_project`` must not
    do on an idle tick: each admission pass appends a PolicyRun, so gating on the
    durable ``dirty_seq``/``reconciled_seq`` marker is what keeps an idle sweep
    from growing the receipt log (ADR-0029, #342).

    The watermark row is locked so a concurrent producer's bump serializes behind
    this pass rather than being lost. When the snapshot is not pending this is a
    no-op that appends nothing; when it is, it loads and advances
    ``reconciled_seq`` to the exact snapshot observed under the lock, so a bump
    that arrives during the load keeps the project pending for the next pass.
    """

    row = session.scalar(
        select(RecordInclusionRequest)
        .where(RecordInclusionRequest.project_id == project_id)
        .with_for_update()
    )
    if row is None or row.dirty_seq <= row.reconciled_seq:
        return ReconcileResult(
            project_id=project_id,
            did_load=False,
            reconciled_seq=row.reconciled_seq if row is not None else 0,
            load=None,
        )

    snapshot = row.dirty_seq
    load = load_project(session, project_id)
    row.reconciled_seq = snapshot
    row.reconciled_at = datetime.now(timezone.utc)
    return ReconcileResult(
        project_id=project_id,
        did_load=True,
        reconciled_seq=snapshot,
        load=load,
    )


def load_and_report(session: Session, project: Project) -> str:
    """Load the project and say what happened, for a command's last line.

    The commit belongs here rather than at the caller because the load is
    what the extraction was for: a read whose results never reached the
    record is not a finished command.
    """
    result = load_project(session, project.id)
    session.commit()
    # This first line is also consumed by retained SH 99 acceptance-seal checks.
    lines = [
        f"{result.dependencies.admitted_count} conflicts and "
        f"{result.events.admitted_count} statements on the record; "
        f"{result.waiting_count} waiting for you"
    ]
    if result.ambiguous_documents:
        lines.append(
            "These documents have several completed Extraction Runs. Select a "
            "Current Production Run before their Extracted Proposals can be added: "
            + ", ".join(result.ambiguous_documents)
        )
    return "\n".join(lines)
