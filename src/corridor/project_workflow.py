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
that owns each act; the acts themselves stay where they already are. A
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
blocks or qualifies the issue.

**What is deliberately not derived here: ADR-0085's three visible consequence
levels.** "Must handle before this issue", "Affects this issue", and "Can wait"
are a projection of the internal bands onto the *next issue's declared content
inventory* — which artifacts that project issues and what each must state.
ADR-0091 amends ADR-0086 so that inventory is per-project configuration, and
its own migration note records that the configured issue set "is not modelled".
``review_packet_reading`` refused to guess the levels for the same reason and
says so in its docstring. A level printed without the inventory would be a
guess wearing a heading, and ADR-0010 forbids inventing a severity to stand in
for one, so every internal Attention Reason stays exactly where #527 and #528
already print it and no level is asserted anywhere.

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

from corridor.baseline_adoption import effective_baseline_formats
from corridor.models import (
    BaselineSourceRow,
    DeltaFollowUpPlan,
    DeltaReviewPacketChild,
    DeltaReviewPacketReversal,
    Document,
    ProposedDelta,
)
from corridor.packet_review import ItemReading, ReviewReading, read_review_items
from corridor.presentation import field_label


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
class ReadinessProblem:
    """One reason this project's next issue cannot yet be produced honestly."""

    code: str
    sentence: str


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
    readiness = issue_readiness(session, project_id=project_id)
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
        readiness=readiness,
        sections=sections,
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

    if not open_delta_ids:
        return ()
    undone = (
        select(DeltaReviewPacketChild.follow_up_plan_id)
        .join(
            DeltaReviewPacketReversal,
            DeltaReviewPacketReversal.receipt_id == DeltaReviewPacketChild.receipt_id,
        )
        .where(
            DeltaReviewPacketChild.project_id == project_id,
            DeltaReviewPacketChild.follow_up_plan_id.is_not(None),
        )
    )
    rows = session.execute(
        select(DeltaFollowUpPlan, ProposedDelta)
        .join(
            ProposedDelta,
            ProposedDelta.id == DeltaFollowUpPlan.delta_id,
        )
        .where(
            DeltaFollowUpPlan.project_id == project_id,
            ProposedDelta.project_id == project_id,
            DeltaFollowUpPlan.delta_id.in_(tuple(open_delta_ids)),
            DeltaFollowUpPlan.id.not_in(undone),
        )
        .order_by(DeltaFollowUpPlan.id)
    ).all()
    if not rows:
        return ()
    names = _subject_names(
        session,
        project_id,
        {delta.target_subject_identity for _, delta in rows},
    )
    return tuple(
        FollowUpNeed(
            plan_id=plan.id,
            delta_id=plan.delta_id,
            revision_id=plan.revision_id,
            subject_name=names.get(
                delta.target_subject_identity, delta.target_subject_identity
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
        for plan, delta in rows
    )


def _subject_names(
    session: Session, project_id: int, identities: set[str]
) -> dict[str, str]:
    """The customer's own identifier for each subject, where one was adopted."""

    if not identities:
        return {}
    rows = session.scalars(
        select(BaselineSourceRow).where(
            BaselineSourceRow.project_id == project_id,
            BaselineSourceRow.record_subject_key.in_(tuple(identities)),
        )
    ).all()
    return {row.record_subject_key: row.business_identity for row in rows}


# --- issue readiness -------------------------------------------------------


def issue_readiness(
    session: Session, *, project_id: int
) -> tuple[ReadinessProblem, ...]:
    """What stands between this project and an honest issue, in a fixed order.

    Both conditions are operations facts rather than record differences, which
    is exactly why they belong here: ADR-0086 blocks a package on integrity and
    coverage, and ADR-0091 makes the customer's own updated workbook the one
    mandatory member of every package, so a project with no registered output
    template cannot produce the artifact the issue is defined by.
    """

    problems: list[ReadinessProblem] = []
    if "output_template" not in effective_baseline_formats(session, project_id):
        problems.append(
            ReadinessProblem(
                NO_OUTPUT_TEMPLATE,
                "No output template is registered, so the customer's own "
                "workbook cannot be produced for this issue.",
            )
        )
    unread = session.scalars(
        select(Document)
        .where(
            Document.project_id == project_id,
            Document.parse_status != "parsed",
        )
        .order_by(Document.id)
    ).all()
    problems.extend(
        ReadinessProblem(
            UNREAD_SOURCE,
            f"{document.filename} was delivered but could not be read "
            f"({document.parse_status}), so this issue's coverage is incomplete.",
        )
        for document in unread
    )
    return tuple(problems)


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

    waiting = len(undecided)
    changes = sum(item.child_count for item in undecided)
    return WorkflowSection(
        name=REVIEW,
        heading=HEADINGS[REVIEW],
        outstanding=waiting,
        summary=(
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
        ),
        counts=(
            ("Questions to decide", waiting),
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
            "Nothing stands in the way of this project's next issue. Preparing "
            "and approving it are not built yet (#529, #533), so the issue "
            "cannot be approved from here."
        )
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
