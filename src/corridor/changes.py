"""What changed since the last report.

A weekly report's most-read section is the one saying what moved. That
section is only trustworthy if it can distinguish two things that look
identical in the numbers:

- the **project** changed — a Committed Date moved, a record became Ready
- the **rules** changed — the ruleset version, or a per-project threshold,
  tightened STALE from 14 days to 10

Those call for opposite responses, so every snapshot records the ruleset
version *and* the effective thresholds that produced it, and a diff across a
ruleset or configuration boundary says so rather than reporting rule-driven
alert churn as project movement.  A boundary suppresses only the threshold-
sensitive exception churn; real source-backed changes — a new record, a
Committed Date Change, a record becoming Ready, a strategy escalation — stay
reported across it.  A historical snapshot that never recorded its thresholds
is treated as an unknown boundary rather than being backfilled from the
current default (ADR-0044).

Since #602 a run also names the accepted Project Record revision it was taken
against, and record-owned fields are read through that reference: since #603
``report_diff_reference.baseline_for_run`` rebuilds the record fields the spine
carries as of the run's revision, resolves each record's Ledger identity rather
than reading the retained one, and reports any field where the retained reading
and the revision disagree.

What a run retains beside that reference is **not** a cache of the record.
ADR-0092 names it the immutable Report Reading payload of one dated occurrence,
retained for as long as the run is and expiring on no cache TTL.
``documentation_requirement_met`` and ``constraint_alerts`` are not record state
at all but a reading of the record under one ruleset, one threshold
configuration and one date, so no revision carries them.
``published_promised_for`` is the date projected from the external party's
current statement, not the record's own ``committed_date`` Fact; the same-named
value on the spine is a different quantity, which is why the payload stopped
using one key for both.  The reading is also the only witness of which records
the report covered that week.  Where the reading and the reference disagree
about a field both can answer, the reading stands — it is what the report
published — and the disagreement is reported on the ``Diff``; for a project in
``legacy`` operating mode the legacy relations are still the accepted record,
and two released legacy commands move an accepted value without writing any
Fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.exceptions import RULESET_VERSION, Evaluation
from corridor.ledger import browse
from corridor.models import (
    DependencyDismissal,
    ExternalReportRelease,
    LegacyLedgerArchive,
    ProjectRecordRevision,
    ReportRun,
    is_critical,
)
from corridor.report_diff_reference import BaselineDrift, baseline_for_run
from corridor.report_reading import PROMISED_FOR_PROJECTION_RULE_VERSION, seal


@dataclass
class Change:
    ref_code: str
    kind: str  # new | closed | dismissed | committed_date_change | escalated | became_ready
    detail: str
    # The Dependency this change is about, so a report cell describing it
    # drills through to the record rather than citing nothing (ADR-0003).
    # None only for a record that left the Ledger before snapshots
    # recorded ids — four such runs are stored.
    dependency_id: int | None = None


@dataclass
class Diff:
    previous_run_id: int | None
    previous_ts: datetime | None
    ruleset_changed: bool = False
    previous_ruleset: str | None = None
    # A threshold configuration change within one ruleset moves alert counts
    # exactly the way a ruleset change does; `configuration_unknown` is the
    # honest case where the baseline never recorded its thresholds, so a
    # comparison cannot rule a change in or out.
    configuration_changed: bool = False
    configuration_unknown: bool = False
    previous_thresholds: dict | None = None
    changes: list[Change] = field(default_factory=list)
    # Where the compared baseline came from, and every field on which the
    # revision reference and the retained copy disagreed (#603). A drift is
    # reported rather than resolved: the reader has to be able to see that the
    # copy and the record it names have parted company.
    baseline_source: str = "no_baseline"
    baseline_drift: tuple[BaselineDrift, ...] = ()

    @property
    def is_first_report(self) -> bool:
        return self.previous_run_id is None

    @property
    def calculation_inputs_changed(self) -> bool:
        """Whether the ruleset or thresholds differ across the baseline.

        The one predicate the exception-churn gate reads: across any such
        boundary a rule appearing may only mean the calculation changed, so it
        is not reported as project movement.
        """
        return (
            self.ruleset_changed
            or self.configuration_changed
            or self.configuration_unknown
        )

    def of_kind(self, kind: str) -> list[Change]:
        return [c for c in self.changes if c.kind == kind]


def _thresholds_snapshot(thresholds) -> dict:
    """The three configured day-counts, in the shape the snapshot stores."""
    return {
        "stale_days": thresholds.stale_days,
        "due_soon_days": thresholds.due_soon_days,
        "action_due_soon_days": thresholds.action_due_soon_days,
    }


def snapshot(
    session: Session,
    project_id: int,
    *,
    evaluation: Evaluation,
    committed_dates: Mapping[int, date | None] | None = None,
) -> dict:
    """The Report Reading payload one report occurrence published (ADR-0092).

    The report passes the evaluation it published, so the payload records the
    Constraint Alerts the reader saw rather than a second reading taken a
    moment later. Required, not defaulted: this reading is what the *next*
    report diffs against, so a second reading here misreports change.

    Version 2 writes the governed names — ``documentation_requirement_met``,
    ``constraint_alerts`` and ``published_promised_for`` — and seals the payload
    with its schema version and content digest. ``published_promised_for`` is
    the statement-projected date, named apart from the record's own
    ``committed_date`` Fact and bound to the statement identity and projection
    rule version that produced it, so one key never carries both quantities.
    """
    if evaluation.project_id != project_id:
        raise ValueError("the evaluation belongs to another project")
    published_committed_dates = (
        dict(committed_dates)
        if committed_dates is not None
        else evaluation.committed_dates
    )
    if published_committed_dates != evaluation.committed_dates:
        raise ValueError(
            "the snapshot and evaluation describe different Committed Date readings"
        )
    rows = browse(session, project_id, limit=100_000, evaluation=evaluation)

    by_dependency = {
        dependency_id: {e.rule for e in found}
        for dependency_id, found in evaluation.by_dependency().items()
    }

    def published_committed_date(row) -> date | None:
        return published_committed_dates.get(row.dependency.id)

    publication = evaluation.statement_publication
    party_statements = () if publication is None else publication.party_statements

    def promised_for_statement(row) -> dict | None:
        """The statement identity ``published_promised_for`` was projected from.

        ``None`` where the reading published no statement for the record, which
        is an answer rather than a missing value: the projected date is ``None``
        there too, and inventing a lineage would be exactly the false reference
        the naming split exists to prevent.
        """
        if publication is None:
            return None
        statement = publication.by_dependency.get(row.dependency.id)
        if statement is None or statement.current_event is None:
            return None
        return {
            "commitment_lineage_id": statement.current_event.commitment_lineage_id,
            "current_event_id": statement.current_event.id,
            "published_event_id": (
                statement.event.id if statement.event is not None else None
            ),
        }

    return seal({
        "ruleset_version": evaluation.ruleset_version,
        # The exact thresholds this reading used, so the next report can tell a
        # settings change apart from project movement. An older payload has no
        # such key; that absence is read as unknown, never as the defaults.
        "thresholds": _thresholds_snapshot(evaluation.thresholds),
        # The rule that turned each External Party Statement into the date this
        # reading published, so a later reader knows which projection produced
        # the value rather than assuming today's.
        "promised_for_projection_rule_version": (
            PROMISED_FOR_PROJECTION_RULE_VERSION
        ),
        "dependencies": {
            row.dependency.ref_code: {
                "id": row.dependency.id,
                "resolution_strategy": row.dependency.resolution_strategy,
                "published_promised_for": (
                    published_committed_date(row).isoformat()
                    if published_committed_date(row)
                    else None
                ),
                "promised_for_statement": promised_for_statement(row),
                "need_date": (
                    row.dependency.need_date.isoformat()
                    if row.dependency.need_date
                    else None
                ),
                "documentation_requirement_met": row.is_ready,
                "constraint_alerts": sorted(
                    by_dependency.get(row.dependency.id, ())
                ),
            }
            for row in rows
        },
        # These statements intentionally have no Dependency key. Keeping their
        # frozen identities beside the record population makes the report's
        # covered population auditable without inventing a Ledger projection.
        "external_party_commitments": {
            str(statement.current_event.commitment_lineage_id): {
                "current_event_id": statement.current_event.id,
                "published_event_id": (
                    statement.event.id if statement.event is not None else None
                ),
                "scope_decision_id": statement.scope_decision.id,
                "unsupported_current": statement.unsupported_current,
            }
            for statement in party_statements
            if not statement.is_closed
        },
    })


def _dismissal_of(session: Session, dependency_id: int | None):
    """The decision that took a record off the working list, if any."""
    if dependency_id is None:
        return None
    return session.scalars(
        select(DependencyDismissal)
        .where(DependencyDismissal.dependency_id == dependency_id)
        .order_by(DependencyDismissal.id.desc())
        .limit(1)
    ).first()


def diff_since_last(
    session: Session,
    project_id: int,
    *,
    evaluation: Evaluation,
    committed_dates: Mapping[int, date | None] | None = None,
    document_only: bool = False,
) -> Diff:
    retirement_boundary = session.scalar(
        select(LegacyLedgerArchive.retired_at).where(
            LegacyLedgerArchive.project_id == project_id
        )
    )
    previous_query = select(ReportRun).where(
        ReportRun.project_id == project_id,
        ReportRun.document_only.is_(document_only),
    )
    if retirement_boundary is not None:
        previous_query = previous_query.where(ReportRun.ts > retirement_boundary)
    previous = session.scalars(
        previous_query.order_by(ReportRun.ts.desc(), ReportRun.id.desc()).limit(1)
    ).first()

    current = snapshot(
        session,
        project_id,
        evaluation=evaluation,
        committed_dates=committed_dates,
    )
    if previous is None:
        # A first report has nothing to compare against, and saying "0
        # changes" would read as "nothing moved" rather than "we have not
        # looked before".
        return Diff(previous_run_id=None, previous_ts=None)

    baseline = baseline_for_run(session, previous)
    before = baseline.dependencies
    after = current["dependencies"]
    previous_ruleset = previous.ruleset_version
    # A snapshot written before thresholds were recorded has no such key. That
    # is unknown, not "the current defaults": backfilling it would claim an old
    # report was computed under a configuration it never saw (ADR-0044).
    previous_thresholds = (previous.snapshot_json or {}).get("thresholds")
    current_thresholds = current["thresholds"]

    diff = Diff(
        previous_run_id=previous.id,
        previous_ts=previous.ts,
        ruleset_changed=previous_ruleset != RULESET_VERSION,
        previous_ruleset=previous_ruleset,
        configuration_unknown=previous_thresholds is None,
        configuration_changed=(
            previous_thresholds is not None
            and previous_thresholds != current_thresholds
        ),
        previous_thresholds=previous_thresholds,
        baseline_source=baseline.source,
        baseline_drift=baseline.drift,
    )

    for ref, now in after.items():
        was = before.get(ref)
        if was is None:
            diff.changes.append(
                Change(
                    ref,
                    "new",
                    "added to the Ledger",
                    now.get("id"),
                )
            )
            continue

        if now["documentation_requirement_met"] and not was[
            "documentation_requirement_met"
        ]:
            diff.changes.append(
                Change(
                    ref,
                    "became_ready",
                    "evidence now meets the closure bar",
                    now.get("id"),
                )
            )

        # A Committed Date Change can move earlier or later; both are factual
        # changes and neither is reduced to the vague legacy term "slip".
        # The compared value is the reading's statement-projected
        # ``published_promised_for``, never the record's committed_date Fact.
        if was["published_promised_for"] and now["published_promised_for"]:
            if now["published_promised_for"] != was["published_promised_for"]:
                diff.changes.append(
                    Change(
                        ref,
                        "committed_date_change",
                        "committed date moved "
                        f"{was['published_promised_for']} → "
                        f"{now['published_promised_for']}",
                        now.get("id"),
                    )
                )
        elif now["published_promised_for"] and not was["published_promised_for"]:
            diff.changes.append(
                Change(
                    ref,
                    "new",
                    f"first committed date: {now['published_promised_for']}",
                    now.get("id"),
                )
            )

        # A snapshot written before #96 has no `resolution_strategy` key at
        # all, and that is not the same as one recording no strategy. Four
        # such runs are stored. Treating absence as "was not critical" would
        # report an escalation for every record that merely became readable,
        # on the first report after the migration — a change in the schema
        # announced as a change in the world.
        if (
            "resolution_strategy" in was
            and is_critical(now["resolution_strategy"])
            and not is_critical(was["resolution_strategy"])
        ):
            diff.changes.append(
                Change(
                    ref,
                    "escalated",
                    "resolution strategy became "
                    f"{now['resolution_strategy']}, which is critical",
                    now.get("id"),
                )
            )

        # Exception churn is only meaningful within one set of calculation
        # inputs. Across a ruleset or threshold-configuration boundary — or one
        # whose thresholds the baseline never recorded — a rule appearing may
        # just mean the calculation changed, so it is not project movement.
        if not diff.calculation_inputs_changed:
            appeared = set(now["constraint_alerts"]) - set(
                was["constraint_alerts"]
            )
            for rule in sorted(appeared):
                diff.changes.append(
                    Change(ref, "escalated", f"new exception: {rule}", now.get("id"))
                )

    for ref in before:
        if ref not in after:
            # A record can leave the working list two ways, and telling an
            # external reader that a utility conflict was resolved when it
            # was thrown out as junk is the one mistake this report cannot
            # afford (ADR-0032). Dismissal is a decision with a reason on
            # it, so the change carries the reason rather than a guess.
            dismissal = _dismissal_of(session, before[ref].get("id"))
            if dismissal is not None:
                diff.changes.append(
                    Change(
                        ref,
                        "dismissed",
                        f"dismissed as {dismissal.reason} by {dismissal.dismissed_by}",
                        before[ref].get("id"),
                    )
                )
            else:
                diff.changes.append(
                    Change(
                        ref,
                        "closed",
                        "no longer in the ledger",
                        before[ref].get("id"),
                    )
                )

    diff.changes.sort(key=lambda c: (c.kind, c.ref_code))
    return diff


def last_released_report(
    session: Session,
    project_id: int,
    *,
    provenance_mode: str,
) -> ExternalReportRelease | None:
    """The predecessor a report compares against: the last released report.

    ADR-0053 fixes one comparison baseline for every report — the last Report
    Approved for Release — and nothing else moves it.  A prepared but unapproved
    PDF, a retained weekly snapshot, or an ad hoc report never advances this
    marker, so a scheduled occurrence records exactly the release its weekly
    window is measured from.  Before a project's first release in this
    provenance mode there is no predecessor and the reading stands on its own.

    The predecessor is matched within one provenance mode because a
    document-only release and an all-supported-sources release describe
    different readings; comparing across them would leak a verbal date into an
    otherwise citation-only surface.
    """
    return session.scalars(
        select(ExternalReportRelease)
        .where(
            ExternalReportRelease.project_id == project_id,
            ExternalReportRelease.provenance_mode == provenance_mode,
        )
        .order_by(
            ExternalReportRelease.released_at.desc(),
            ExternalReportRelease.id.desc(),
        )
        .limit(1)
    ).first()


def accepted_revision_id(session: Session, project_id: int) -> int | None:
    """The accepted Project Record revision a reading taken now is against.

    The newest revision of the project, read once, in the caller's own
    transaction — the same watermark ``report_preparation`` states its weekly
    counts against and ``issue_rendering`` freezes an issue on, so a report and
    a change summary taken together name one revision instead of each asking
    the database a moment apart.

    ``None`` means the project has no accepted revision at all, which is an
    answer rather than a missing value: a legacy project whose accepted record
    is not on the spine has no revision identity to name, and naming one it was
    not produced against would be the false reference #602 exists to prevent.
    The database refuses an unbound new row for every project that does have
    one.
    """

    return session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == project_id
        )
    )


def record_run(
    session: Session,
    project_id: int,
    *,
    evaluation: Evaluation,
    output_path: str | None = None,
    committed_dates: Mapping[int, date | None] | None = None,
    document_only: bool = False,
) -> ReportRun:
    """Store the state this report was published against.

    The binding is resolved here rather than accepted from the caller: a run
    records the revision standing at the moment it is written, in this same
    transaction, and no caller can hand it one taken from an earlier read.
    """
    run = ReportRun(
        project_id=project_id,
        revision_id=accepted_revision_id(session, project_id),
        # The evaluation's own version, not the module constant: the
        # snapshot inside this same row already records the former, and
        # a run that disagrees with its own snapshot is unreadable.
        ruleset_version=evaluation.ruleset_version,
        snapshot_json=snapshot(
            session,
            project_id,
            evaluation=evaluation,
            committed_dates=committed_dates,
        ),
        output_path=output_path,
        document_only=document_only,
    )
    session.add(run)
    session.flush()
    return run
