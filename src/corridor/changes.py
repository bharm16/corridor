"""What changed since the last report.

A weekly report's most-read section is the one saying what moved. That
section is only trustworthy if it can distinguish two things that look
identical in the numbers:

- the **project** changed — a date slipped, a record closed
- the **rules** changed — STALE tightened from 14 days to 10

Those call for opposite responses, so every snapshot records the ruleset
version that produced it, and a diff across a ruleset boundary says so
rather than reporting rule-driven movement as project movement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.exceptions import RULESET_VERSION, Evaluation, evaluate_project
from corridor.ledger import browse
from corridor.models import ReportRun, is_critical


@dataclass
class Change:
    ref_code: str
    kind: str  # new | closed | slipped | escalated | became_ready
    detail: str


@dataclass
class Diff:
    previous_run_id: int | None
    previous_ts: datetime | None
    ruleset_changed: bool = False
    previous_ruleset: str | None = None
    changes: list[Change] = field(default_factory=list)

    @property
    def is_first_report(self) -> bool:
        return self.previous_run_id is None

    def of_kind(self, kind: str) -> list[Change]:
        return [c for c in self.changes if c.kind == kind]


def snapshot(
    session: Session, project_id: int, *, evaluation: Evaluation | None = None
) -> dict:
    """The state a report was published against, one entry per dependency.

    The report passes the evaluation it published, so the snapshot records
    the exceptions the reader saw rather than a second reading taken a
    moment later.
    """
    if evaluation is None:
        evaluation = evaluate_project(session, project_id)
    rows = browse(session, project_id, limit=100_000, evaluation=evaluation)

    by_dependency = {
        dependency_id: {e.rule for e in found}
        for dependency_id, found in evaluation.by_dependency().items()
    }

    return {
        "ruleset_version": evaluation.ruleset_version,
        "dependencies": {
            row.dependency.ref_code: {
                "status": row.dependency.status,
                "resolution_strategy": row.dependency.resolution_strategy,
                "committed_date": (
                    row.dependency.committed_date.isoformat()
                    if row.dependency.committed_date
                    else None
                ),
                "need_date": (
                    row.dependency.need_date.isoformat()
                    if row.dependency.need_date
                    else None
                ),
                "ready": row.is_ready,
                "exceptions": sorted(by_dependency.get(row.dependency.id, ())),
            }
            for row in rows
        },
    }


def diff_since_last(
    session: Session, project_id: int, *, evaluation: Evaluation | None = None
) -> Diff:
    previous = session.scalars(
        select(ReportRun)
        .where(ReportRun.project_id == project_id)
        .order_by(ReportRun.ts.desc(), ReportRun.id.desc())
        .limit(1)
    ).first()

    current = snapshot(session, project_id, evaluation=evaluation)
    if previous is None:
        # A first report has nothing to compare against, and saying "0
        # changes" would read as "nothing moved" rather than "we have not
        # looked before".
        return Diff(previous_run_id=None, previous_ts=None)

    before = (previous.snapshot_json or {}).get("dependencies", {})
    after = current["dependencies"]
    previous_ruleset = previous.ruleset_version

    diff = Diff(
        previous_run_id=previous.id,
        previous_ts=previous.ts,
        ruleset_changed=previous_ruleset != RULESET_VERSION,
        previous_ruleset=previous_ruleset,
    )

    for ref, now in after.items():
        was = before.get(ref)
        if was is None:
            diff.changes.append(
                Change(ref, "new", f"added to the ledger as {now['status']}")
            )
            continue

        if now["status"] == "closed" and was["status"] != "closed":
            diff.changes.append(Change(ref, "closed", "status moved to closed"))

        if now["ready"] and not was["ready"]:
            diff.changes.append(
                Change(ref, "became_ready", "evidence now meets the closure bar")
            )

        # A slip is the committed date moving *later*. Moving earlier is not
        # a slip and should not be reported as one.
        if was["committed_date"] and now["committed_date"]:
            if now["committed_date"] > was["committed_date"]:
                diff.changes.append(
                    Change(
                        ref,
                        "slipped",
                        f"committed date moved {was['committed_date']} → "
                        f"{now['committed_date']}",
                    )
                )
        elif now["committed_date"] and not was["committed_date"]:
            diff.changes.append(
                Change(ref, "new", f"first committed date: {now['committed_date']}")
            )

        # A snapshot written before #96 has no `resolution_strategy` key at
        # all, and that is not the same as one recording no strategy. Four
        # such runs are stored. Treating absence as "was not critical" would
        # report an escalation for every record that merely became readable,
        # on the first report after the migration — a change in the schema
        # announced as a change in the world.
        if "resolution_strategy" in was and is_critical(
            now["resolution_strategy"]
        ) and not is_critical(was["resolution_strategy"]):
            diff.changes.append(
                Change(
                    ref,
                    "escalated",
                    "resolution strategy became "
                    f"{now['resolution_strategy']}, which is critical",
                )
            )

        # Exception churn is only meaningful within one ruleset. Across a
        # version change, a rule appearing may just mean the rule changed.
        if not diff.ruleset_changed:
            appeared = set(now["exceptions"]) - set(was["exceptions"])
            for rule in sorted(appeared):
                diff.changes.append(
                    Change(ref, "escalated", f"new exception: {rule}")
                )

    for ref in before:
        if ref not in after:
            diff.changes.append(
                Change(ref, "closed", "no longer in the ledger")
            )

    diff.changes.sort(key=lambda c: (c.kind, c.ref_code))
    return diff


def record_run(
    session: Session,
    project_id: int,
    *,
    output_path: str | None = None,
    evaluation: Evaluation | None = None,
) -> ReportRun:
    """Store the state this report was published against."""
    run = ReportRun(
        project_id=project_id,
        ruleset_version=RULESET_VERSION,
        snapshot_json=snapshot(session, project_id, evaluation=evaluation),
        output_path=output_path,
    )
    session.add(run)
    session.flush()
    return run
