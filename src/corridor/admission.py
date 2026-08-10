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

from sqlalchemy.orm import Session

from corridor.dependency_admission import (
    DependencyAdmissionResult,
    run_dependency_admission,
)
from corridor.event_admission import EventAdmissionResult, run_event_admission
from corridor.extraction_runs import declare_single_run_documents_by_policy
from corridor.models import Project


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
    events = run_event_admission(session, project_id)
    return LoadResult(
        declared_documents=len(declarations.declared),
        ambiguous_documents=declarations.ambiguous,
        dependencies=dependencies,
        events=events,
    )


def load_and_report(session: Session, project: Project) -> str:
    """Load the project and say what happened, for a command's last line.

    The commit belongs here rather than at the caller because the load is
    what the extraction was for: a read whose results never reached the
    record is not a finished command.
    """
    result = load_project(session, project.id)
    session.commit()
    lines = [
        f"{result.dependencies.admitted_count} conflicts and "
        f"{result.events.admitted_count} statements on the record; "
        f"{result.waiting_count} waiting for you"
    ]
    if result.ambiguous_documents:
        lines.append(
            "these documents hold several completed readings and show "
            "nothing until one is declared: "
            + ", ".join(result.ambiguous_documents)
        )
    return "\n".join(lines)
