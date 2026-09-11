"""One adopted project's week, read as three ordered sections (#536).

A coordinator opening an adopted-baseline project has three kinds of work
waiting and they happen in one order: decide the record changes sources have
proposed, chase the answers the project already committed to needing, then
approve what goes back to the customer. Before this module the first of those
had a screen (#527, #528) with **no way in** — `/review/{slug}` was reachable
only by typing it — and the other two had nowhere at all, so "what needs
attention this week" was answered by visiting the matrix, the source register,
or the audit history and adding it up by hand.

**It is a reading, and it stores nothing.** ADR-0085 rejected a stored packet
lifecycle for the review half, and the same argument retires a "reporting
close" record for the whole week: a stored close status, owner, cadence, or
completion flag would be a second authority competing with the decisions,
plans, cutoff, coverage, and release records that already exist, and it would
be stale the moment a source arrived. Every section's state here is derived
from those, so two readings of the same state land on the same section.

**Nothing here grows a second decision control.** ``review_packet_reading``
guarantees each open Proposed Delta is actionable in exactly one item, and this
module deliberately does not repeat that partition, re-offer a delta, or expose
any outcome token. It reports *how much* is waiting and names the one surface
that owns each act; the acts themselves stay where they already are.

The one thing it now reports *individually* is deferred work (#835). That is
not an exception to the rule above: ADR-0084 makes a deferral Work List
scheduling rather than a disposition, so a deferred change has no decision
waiting on this page and none is offered — only when it comes back, and the
chance to move that date. Deciding it is still the review screen's, and the
route behind the scheduling control refuses a change that is not already
deferred so that this page can never start a deferral either. A
follow-up need is listed with the words its Follow-up Plan recorded and no
control at all, because nothing in this repository yet acts on a spine
Follow-up Plan (#425).

**No clock.** ``as_of`` is the reporting cutoff the caller declares, exactly as
the review reading below it requires, so a screen and its test agree about what
"this week" means.

**Why source coverage and rendering are issue readiness, not review items.**
A document Corridor could not read, and a project with no registered output
template, are both facts about whether the customer's issue can be produced
honestly. Neither is a difference between a source and the accepted record, so
neither is a Proposed Delta and neither can reach a record decision. Putting
them in front of a coordinator as record-decision items would ask a person to
"decide" an operations failure; ADR-0086 keeps them where they belong, as what
blocks or qualifies the issue. Each one also names **who puts it right and
what they do next**, because a blocker that says only what is wrong leaves
the person reading it nowhere to go (#840).

**ADR-0085's three visible consequence levels are derived, and derived once
(#641).** "Must handle before this issue", "Affects this issue", and "Can wait"
are a projection of a proposed difference onto the *next issue's declared
content* — which artifacts that project issues, what each states, and which
customer policy waits on a decision first. #640 modelled that configuration and
``issue_content`` resolves it, so this section no longer refuses the projection:
it reads the level ``packet_review`` already derived per item and prints it.
Nothing here derives one, which is exactly why this page and the review screen
cannot disagree about a packet. Every internal Attention Reason still appears
beside it, unchanged: the level is the heading, never a replacement for the
reasons (ADR-0085, ADR-0010).

**A profile Corridor cannot execute is Issue readiness, not a Review child.** An
unregistered renderer revision, a coverage evaluator this release does not run,
and a ``required_decision`` that is prose rather than a typed selector are all
facts about whether the customer's issue can be produced honestly. None of them
is a difference between a source and the accepted record, so none reaches a
record decision, and none of them turns into a blanket "Must handle before this
issue" either — an unconfigured level is absent and explained, never guessed.

Terminology: nothing here coins a customer word. Follow-up Plan, Proposed
Delta, Project Record, Attention Reason, and Defer are the adopted glossary's,
and ``Release Package`` and ``Review Packet`` stay the internal technical names
they are — no section is named after either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.baseline_adoption import effective_baseline_formats_by_project
from corridor.issue_content import effective_issue_contents
from corridor.issue_profile import effective_issue_inventories
from corridor.models import (
    BaselineSourceRow,
    DeltaFollowUpPlan,
    Document,
    ProposedDelta,
)
from corridor.native_follow_up_reading import undone_follow_up_plan_ids
from corridor.packet_review import ItemReading, ReviewReading, read_review_items
from corridor.presentation import field_label
from corridor.release_preparation import PreparationStanding, preparation_standings


# The three sections, in the order the week runs. The names are internal
# identifiers; the headings each section prints are in `HEADINGS`, built from
# glossary terms already adopted rather than from a new label for the workflow
# or any part of it.
REVIEW = "review"
FOLLOW_UP = "follow_up"
ISSUE = "issue"
SECTION_ORDER: tuple[str, ...] = (REVIEW, FOLLOW_UP, ISSUE)

HEADINGS: Mapping[str, str] = {
    REVIEW: "Source changes to review",
    FOLLOW_UP: "Follow-up Plans waiting on an answer",
    ISSUE: "The issue to approve for sharing",
}

# What each readiness problem is, named once so a screen and a test agree.
NO_OUTPUT_TEMPLATE = "no_output_template"
UNREAD_SOURCE = "unread_source"
# The last attempt at preparing this project's issue did not produce one
# (#675). It is a technical blocker on the *current* issue and deliberately
# not a state of its own: a failed preparation is something to put right
# before this issue goes out, which is exactly what this list is for, and the
# portfolio's five primary states stay five.
PREPARATION_FAILED = "preparation_failed"

# Who puts each kind of readiness problem right. Neither name is coined here.
# ``Corridor Operations`` is the bounded context `CONTEXT-MAP.md` already names
# and `internal_report.html` already prints on a screen; the other is the person
# reading the page, addressed the way the Issue section already addresses them
# ("nothing is needed from you", "the coverage you confirmed").
#
# Neither is a designation claim, and that distinction now has to be read
# carefully. Until #839, nothing in the write path required a designation to
# confirm coverage or ask for a preparation, and these names were chosen so as
# not to state a rule the database did not enforce. PostgreSQL now refuses both
# acts to a member without the project-coordination designation, so the rule
# exists -- but ``You, on this page`` still names the reader rather than the
# designation, and ``Corridor Operations`` is a bounded context rather than a
# designation at all (``access.py`` has no such name; it has
# ``TECHNICAL_OPERATIONS``). Whether either becomes a designation name is a
# customer-facing naming decision, not something to infer from the guard
# landing.
OPERATIONS_OWNER = "Corridor Operations"
COORDINATOR_OWNER = "You, on this page"


@dataclass(frozen=True, slots=True)
class FollowUpNeed:
    """One accepted Follow-up Plan still waiting on the answer it named.

    Every field is what the plan itself recorded (#526). Nothing is inferred
    from the delta, and no unresolved evidence without a plan appears here:
    ADR-0084 forbids treating an open Proposed Delta as an external ask.
    """

    plan_id: int
    delta_id: int
    revision_id: int
    subject_name: str
    field_name: str
    open_question: str
    responsible: str
    return_date: date | None

    def overdue(self, *, today: date) -> bool:
        """Whether the date the plan itself named has already passed."""

        return self.return_date is not None and self.return_date < today


@dataclass(frozen=True, slots=True)
class DeferredWork:
    """One Proposed Delta held out of immediate work, and when it comes back.

    ADR-0084 makes Defer scheduling: the delta is still open and still counted,
    and the only thing that changed is when the coordinator wants to see it.
    Before #835 this page printed how many there were and nothing else, so the
    date a coordinator had recorded — and any wrong date they had recorded —
    was reachable only through the record history or a database query.

    Every value is the scheduling receipt's own. Nothing here is a status: the
    reading decides what is deferred (#494), and this names it in the words the
    project uses.
    """

    delta_id: int
    subject_name: str
    field_name: str
    returns_at: date | None
    wake_condition: str | None

    @property
    def return_sentence(self) -> str:
        """When this comes back, in the terms the receipt actually recorded."""

        if self.returns_at is not None:
            return f"Comes back on {self.returns_at.isoformat()}"
        return f"Comes back when {self.wake_condition}"


@dataclass(frozen=True, slots=True)
class ReadinessProblem:
    """One reason this project's next issue cannot yet be produced honestly.

    ``owner`` and ``next_action`` say who puts it right and what they do about
    it. They are part of the derivation rather than a lookup some screen
    performs, because the code is what knows them: the customer-journey audit
    found blockers that explained why an issue could not proceed and left the
    person reading them nowhere to go, and a second table keyed by code, kept
    somewhere else, is how that drifts back apart (#840).
    """

    code: str
    sentence: str
    owner: str
    next_action: str


@dataclass(frozen=True, slots=True)
class WorkflowSection:
    """One section of the week, with what is waiting in it and why."""

    name: str
    heading: str
    outstanding: int
    summary: str
    counts: tuple[tuple[str, Any], ...]

    @property
    def waiting(self) -> bool:
        """Whether this section still holds work for the coordinator."""

        return self.outstanding > 0


@dataclass(frozen=True, slots=True)
class ProjectWorkflow:
    """One adopted project's three sections, relative to one declared cutoff."""

    project_id: int
    cutoff: datetime
    rule_version: str
    accepted_revision_id: int | None
    review: ReviewReading
    undecided: tuple[ItemReading, ...]
    follow_up: tuple[FollowUpNeed, ...]
    deferred: tuple[DeferredWork, ...]
    readiness: tuple[ReadinessProblem, ...]
    sections: tuple[WorkflowSection, ...]

    @property
    def landing(self) -> str:
        """The first section still holding work, or the first section.

        A project with nothing waiting anywhere lands at the top of its own
        week and is told so, rather than being dropped at the end of a workflow
        it has no reason to walk.
        """

        for section in self.sections:
            if section.waiting:
                return section.name
        return self.sections[0].name

    @property
    def changes_awaiting_decision(self) -> tuple[int, ...]:
        """Every actionable proposed change with no live Follow-up Plan.

        The same set the review section counts questions over: each actionable
        delta is offered by exactly one item (#494), so a question is still
        waiting on the coordinator's own judgement exactly when one of these
        is. It is named here so the cross-project reading (#537) counts the
        same thing rather than a second thing that resembles it.
        """

        planned = {need.delta_id for need in self.follow_up}
        return tuple(
            delta_id
            for delta_id in self.review.reading.actionable_delta_ids
            if delta_id not in planned
        )

    def section(self, name: str) -> WorkflowSection | None:
        for section in self.sections:
            if section.name == name:
                return section
        return None


def read_project_workflow(
    session: Session, *, project_id: int, as_of: datetime
) -> ProjectWorkflow:
    """Assemble one project's ordered week from the records that already exist.

    ``as_of`` is the reporting cutoff. It bounds the review reading exactly as
    #494 requires, and it is the date every section is described relative to.
    """

    review = read_review_items(session, project_id=project_id, as_of=as_of)
    follow_up = outstanding_follow_up(
        session, project_id=project_id, open_delta_ids=review.reading.open_delta_ids
    )
    # Which section a question is *described* under, never which questions
    # belong together. #494 owns the partition and is not consulted twice: a
    # change the coordinator already answered Needs coordination is waiting on
    # the person their plan named, not on them, so it is counted in the
    # follow-up section. The review screen still offers every actionable item,
    # each change on exactly one of them, and nothing here offers anything.
    planned = {need.delta_id for need in follow_up}
    undecided = tuple(
        item
        for item in review.items
        if any(child.delta_id not in planned for child in item.children)
    )
    deferred = deferred_work(session, project_id=project_id, review=review)
    readiness = issue_readiness(session, project_id=project_id, as_of=as_of)
    sections = (
        _review_section(review, undecided, planned),
        _follow_up_section(follow_up, today=as_of.date()),
        _issue_section(review, readiness),
    )
    return ProjectWorkflow(
        project_id=project_id,
        cutoff=as_of,
        rule_version=review.rule_version,
        accepted_revision_id=review.accepted_revision_id,
        review=review,
        undecided=undecided,
        follow_up=follow_up,
        deferred=deferred,
        readiness=readiness,
        sections=sections,
    )


def deferred_work(
    session: Session, *, project_id: int, review: ReviewReading
) -> tuple[DeferredWork, ...]:
    """Every deferred Proposed Delta of this project, in the reading's order.

    The reading decides which deltas are deferred and what each one's schedule
    says (#494, ADR-0084); this only puts the customer's own words in front of
    them, exactly as ``outstanding_follow_up`` does for a plan.
    """

    schedules = {
        standing.delta_id: standing for standing in review.reading.deferred
    }
    if not schedules:
        return ()
    deltas = tuple(
        session.scalars(
            select(ProposedDelta).where(
                ProposedDelta.project_id == project_id,
                ProposedDelta.id.in_(tuple(schedules)),
            )
        ).all()
    )
    names = _subject_names(
        session, (project_id,), {delta.target_subject_identity for delta in deltas}
    )
    by_id = {delta.id: delta for delta in deltas}
    return tuple(
        DeferredWork(
            delta_id=standing.delta_id,
            subject_name=names.get(
                (project_id, delta.target_subject_identity),
                delta.target_subject_identity,
            ),
            field_name=(
                field_label(delta.target_field)
                if delta.target_field
                else "the whole record row"
            ),
            returns_at=(
                standing.returns_at.date()
                if standing.returns_at is not None
                else None
            ),
            wake_condition=standing.wake_condition,
        )
        for standing in review.reading.deferred
        if (delta := by_id.get(standing.delta_id)) is not None
    )


# --- accepted follow-up ----------------------------------------------------


def outstanding_follow_up(
    session: Session, *, project_id: int, open_delta_ids: Sequence[int]
) -> tuple[FollowUpNeed, ...]:
    """Every Follow-up Plan whose question is still live, oldest first.

    A plan leaves this reading for one of two reasons, neither of them a stored
    status: the Proposed Delta it was raised on stopped being open, so the
    question it named was settled or replaced; or the one packet act that
    recorded it was undone, and an act that never stood raises no ask.
    """

    return outstanding_follow_up_by_project(
        session, open_delta_ids={project_id: tuple(open_delta_ids)}
    ).get(project_id, ())


def outstanding_follow_up_by_project(
    session: Session, *, open_delta_ids: Mapping[int, Sequence[int]]
) -> dict[int, tuple[FollowUpNeed, ...]]:
    """``outstanding_follow_up`` for several projects, in three statements.

    The cross-project reading (#537) must not ask this once per project, and
    it must not answer it differently either, so the single-project reader
    above is this function over one project.
    """

    found: dict[int, tuple[FollowUpNeed, ...]] = {
        project_id: () for project_id in open_delta_ids
    }
    live = {
        project_id: tuple(delta_ids)
        for project_id, delta_ids in open_delta_ids.items()
        if delta_ids
    }
    if not live:
        return found
    project_ids = tuple(live)
    every_open = tuple({delta_id for ids in live.values() for delta_id in ids})
    # Whether the one act that recorded a plan still stands is decided in
    # ``native_follow_up_reading``, where the as-of reader decides it too. The
    # same join written twice here and there is how two renderings of one plan
    # came to be able to disagree about a reversal.
    undone = undone_follow_up_plan_ids(project_ids)
    rows = session.execute(
        select(DeltaFollowUpPlan, ProposedDelta)
        .join(
            ProposedDelta,
            ProposedDelta.id == DeltaFollowUpPlan.delta_id,
        )
        .where(
            DeltaFollowUpPlan.project_id.in_(project_ids),
            ProposedDelta.project_id == DeltaFollowUpPlan.project_id,
            DeltaFollowUpPlan.delta_id.in_(every_open),
            DeltaFollowUpPlan.id.not_in(undone),
        )
        .order_by(DeltaFollowUpPlan.id)
    ).all()
    # One project's plan may name a delta that another project has open; the
    # membership test above is deliberately per project, not over the union.
    rows = [
        (plan, delta)
        for plan, delta in rows
        if plan.delta_id in set(live.get(plan.project_id, ()))
    ]
    if not rows:
        return found
    names = _subject_names(
        session,
        project_ids,
        {delta.target_subject_identity for _, delta in rows},
    )
    collected: dict[int, list[FollowUpNeed]] = {
        project_id: [] for project_id in open_delta_ids
    }
    for plan, delta in rows:
        collected[plan.project_id].append(
            FollowUpNeed(
                plan_id=plan.id,
                delta_id=plan.delta_id,
                revision_id=plan.revision_id,
                subject_name=names.get(
                    (plan.project_id, delta.target_subject_identity),
                    delta.target_subject_identity,
                ),
                field_name=(
                    field_label(delta.target_field)
                    if delta.target_field
                    else "the whole record row"
                ),
                open_question=plan.open_question,
                responsible=(
                    plan.responsible_principal or plan.responsible_organization or ""
                ),
                return_date=plan.return_date.date() if plan.return_date else None,
            )
        )
    found.update(
        (project_id, tuple(needs)) for project_id, needs in collected.items()
    )
    return found


def _subject_names(
    session: Session, project_ids: Sequence[int], identities: set[str]
) -> dict[tuple[int, str], str]:
    """The customer's own identifier for each subject, where one was adopted."""

    if not identities or not project_ids:
        return {}
    rows = session.scalars(
        select(BaselineSourceRow).where(
            BaselineSourceRow.project_id.in_(tuple(project_ids)),
            BaselineSourceRow.record_subject_key.in_(tuple(identities)),
        )
    ).all()
    return {
        (row.project_id, row.record_subject_key): row.business_identity
        for row in rows
    }


# --- issue readiness -------------------------------------------------------


def issue_readiness(
    session: Session, *, project_id: int, as_of: datetime
) -> tuple[ReadinessProblem, ...]:
    """What stands between this project and an honest issue, in a fixed order.

    Every condition here is an operations or configuration fact rather than a
    record difference, which is exactly why they belong together: ADR-0086
    blocks a package on integrity, coverage and explicit customer policy, and
    ADR-0091 makes the customer's own updated workbook the one mandatory member
    of every package, so a project with no registered output template cannot
    produce the artifact the issue is defined by.

    ``as_of`` is the same reporting cutoff the rest of the week is read at. The
    issue profile is per-cutoff configuration (#640), so asking what a project
    issues without saying when would answer for a version that may not have
    been in force.
    """

    return issue_readiness_by_project(
        session, project_ids=(project_id,), as_of=as_of
    ).get(project_id, ())


def issue_readiness_by_project(
    session: Session,
    *,
    project_ids: Sequence[int],
    as_of: datetime,
    preparations: Mapping[int, PreparationStanding] | None = None,
) -> dict[int, tuple[ReadinessProblem, ...]]:
    """``issue_readiness`` for several projects, in a fixed number of statements.

    ``preparations`` is the one optional input, and it exists so that a caller
    already holding ``preparation_standings`` for the same projects does not
    make this function ask for them a second time. The cross-project reading
    (#537) holds them because a request still in flight is secondary context on
    its row while a finished failure is a problem here, and those are two
    readings of one record. Left unsupplied, this reads them itself, which is
    what every single-project caller does.
    """

    ids = tuple(dict.fromkeys(int(value) for value in project_ids))
    problems: dict[int, list[ReadinessProblem]] = {
        project_id: [] for project_id in ids
    }
    if not ids:
        return {}
    formats = effective_baseline_formats_by_project(session, ids)
    for project_id in ids:
        if "output_template" not in formats.get(project_id, {}):
            problems[project_id].append(
                ReadinessProblem(
                    NO_OUTPUT_TEMPLATE,
                    "No output template is registered, so the customer's own "
                    "workbook cannot be produced for this issue.",
                    OPERATIONS_OWNER,
                    "Corridor Operations registers this customer's own workbook "
                    "template for this project. Nothing on this page does that.",
                )
            )
    # What the project is configured to issue at this cutoff, and whether this
    # release can execute that configuration at all (#641). A problem here is
    # never rendered as a proposed change: nobody can decide their way out of a
    # renderer version Corridor does not know.
    contents = effective_issue_contents(
        session, effective_issue_inventories(session, ids, as_of)
    )
    for project_id in ids:
        content = contents.get(project_id)
        if content is None:
            continue
        for problem in content.problems:
            problems[project_id].append(
                ReadinessProblem(
                    problem.code,
                    problem.sentence,
                    OPERATIONS_OWNER,
                    "Corridor Operations corrects what this project is "
                    "configured to issue. It is configuration rather than a "
                    "decision about the record, so nothing on this page "
                    "changes it.",
                )
            )
    unread = session.scalars(
        select(Document)
        .where(
            Document.project_id.in_(ids),
            Document.parse_status != "parsed",
        )
        .order_by(Document.id)
    ).all()
    for document in unread:
        problems[int(document.project_id)].append(
            ReadinessProblem(
                UNREAD_SOURCE,
                f"{document.filename} was delivered but could not be read "
                f"({document.parse_status}), so this issue's coverage is "
                "incomplete.",
                OPERATIONS_OWNER,
                "Corridor Operations reads this source again, or asks for a "
                "copy it can read. Nothing on this page retries it, and the "
                "coverage for this issue keeps saying this source was not "
                "read until one of those succeeds.",
            )
        )
    # A preparation that ran and produced nothing (#675). A request still in
    # flight adds nothing here on purpose: while a worker is preparing, no
    # human action is required, and a problem printed for it would ask a
    # coordinator to act on Corridor's own work in progress.
    if preparations is None:
        preparations = preparation_standings(session, project_ids=ids)
    for project_id, standing in preparations.items():
        if project_id not in problems:
            continue
        if not standing.failed:
            continue
        problems[project_id].append(
            ReadinessProblem(
                PREPARATION_FAILED,
                "The last attempt to prepare this issue produced nothing: "
                f"{standing.reason} Nothing partial was written, and asking "
                "for it again is what puts it right.",
                COORDINATOR_OWNER,
                "Confirm the coverage this issue is read under and ask for it "
                "again. That appends a new request rather than reusing the one "
                "that failed, so a worker picks it up even when nothing else "
                "about this issue has moved.",
            )
        )
    return {project_id: tuple(rows) for project_id, rows in problems.items()}


# --- the sections ----------------------------------------------------------


def _review_section(
    review: ReviewReading,
    undecided: Sequence[ItemReading],
    planned: set[int],
) -> WorkflowSection:
    """What is still waiting on the coordinator's own judgement.

    A question every part of which the coordinator already answered Needs
    coordination is not waiting on them; it is waiting on the person their
    Follow-up Plan named, so it is counted in the next section instead. The
    partition itself is untouched — the review screen still offers every
    actionable item, and each proposed change on exactly one of them. This
    only decides which section of the week a question is described under, and
    no section here carries a control of any kind.
    """

    source_questions = len(review.source_questions)
    waiting = len(undecided) + source_questions
    changes = sum(item.child_count for item in undecided)
    return WorkflowSection(
        name=REVIEW,
        heading=HEADINGS[REVIEW],
        outstanding=waiting,
        summary=(f"{source_questions} statement{'s' if source_questions != 1 else ''} need source clarification; "
                 f"{len(undecided)} question{'s' if len(undecided) != 1 else ''} cover {changes} proposed changes."
                 if source_questions else (
            (
                "No source has proposed a change to this project record."
                if not review.items
                else (
                    "Every proposed change now has a recorded Follow-up Plan, "
                    "so nothing is waiting on your own judgement."
                )
            )
            if not waiting
            else (
                f"{waiting} question{'' if waiting == 1 else 's'} to decide, "
                f"covering {changes} proposed change"
                f"{'' if changes == 1 else 's'}. Each proposed change is "
                "offered on exactly one of them."
            )
        )),
        counts=(
            ("Questions to decide", len(undecided)),
            *((("Statements needing clarification", source_questions),) if source_questions else ()),
            ("Proposed changes they cover", changes),
            ("Proposed changes waiting on a Follow-up Plan", len(planned)),
            ("Deferred until a later date", len(review.reading.deferred)),
        ),
    )


def _follow_up_section(
    needs: Sequence[FollowUpNeed], *, today: date
) -> WorkflowSection:
    overdue = sum(1 for need in needs if need.overdue(today=today))
    return WorkflowSection(
        name=FOLLOW_UP,
        heading=HEADINGS[FOLLOW_UP],
        outstanding=len(needs),
        summary=(
            "Nothing is waiting on an outside answer."
            if not needs
            else (
                f"{len(needs)} recorded Follow-up Plan"
                f"{'' if len(needs) == 1 else 's'} name someone who owes an "
                "answer. A proposed change with no recorded plan is not an "
                "outside ask and is not listed here."
            )
        ),
        counts=(
            ("Follow-up Plans waiting", len(needs)),
            ("Past the date the plan named", overdue),
        ),
    )


def _issue_section(
    review: ReviewReading, readiness: Sequence[ReadinessProblem]
) -> WorkflowSection:
    nothing_accepted = review.accepted_revision_id is None
    if nothing_accepted:
        summary = (
            "Nothing has been accepted into this project record yet, so there "
            "is no issue to prepare."
        )
        outstanding = 0
    elif readiness:
        summary = (
            f"{len(readiness)} thing{'' if len(readiness) == 1 else 's'} must "
            "be put right before this project's issue can be produced honestly."
        )
        outstanding = len(readiness)
    else:
        summary = (
            "Nothing stands in the way of this project's next issue. What has "
            "been prepared for the customer, and whether it can be approved "
            "for sharing, is below."
        )
        # Deliberately still 1, and not "1 if there is something to approve".
        # The count is what `landing` walks, and #537's portfolio derives the
        # same landing from the same numbers with a test asserting the two
        # cannot disagree; a project with an accepted record and nothing else
        # waiting opens on its issue whether or not a candidate has been
        # prepared yet, because preparing one is the work it is opening for.
        outstanding = 1
    return WorkflowSection(
        name=ISSUE,
        heading=HEADINGS[ISSUE],
        outstanding=outstanding,
        summary=summary,
        counts=(
            (
                "Accepted Project Record revision",
                review.accepted_revision_id
                if review.accepted_revision_id is not None
                else "none yet",
            ),
            ("Problems to put right first", len(readiness)),
        ),
    )
