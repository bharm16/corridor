"""Every adopted project a coordinator holds, read once across all of them (#537).

#536 gave one adopted project its own ordered week. A coordinator holding
several projects still had to open each one to find out whether it needed
anything, which is the labour this product exists to remove: the quiet projects
are the majority, and a week spent proving they are quiet is a week spent
reading.

**It is derived, and it stores nothing.** There is no portfolio-work table, no
completion flag, no owner, no cadence, and no deferral of its own. ADR-0085
rejected a stored packet lifecycle, #536 rejected a stored reporting close for
one project, and the same argument retires a stored cross-project queue: a
second authority beside the decisions, plans, cutoff, coverage, and release
records would be stale the moment a source arrived, and it would need someone
to tick it. Nothing here is ticked. A row changes because the records it is
derived from changed.

**It cannot disagree with the project's own week.** Every input is read through
the same functions ``project_workflow`` reads — ``review_packet_reading``'s
``standing_sets`` for what is actionable, ``outstanding_follow_up_by_project``
for the live Follow-up Plans, ``issue_readiness_by_project`` for coverage and
rendering. This module holds no second copy of any of those rules; it holds the
precedence that turns them into one primary state, and the batched loading that
makes them affordable across many projects at once.

**One bounded read, not one round trip per project.** Every query below asks
for all the shown projects at once and the derivation itself is pure Python, so
the statement count is a constant of the reading rather than a multiple of the
portfolio size. ``read_project_workflow`` per project would have been simpler
and is what the first draft did; at a design partner's portfolio it is tens of
round trips to prove that nothing happened.

**No clock.** ``as_of`` is the reporting cutoff the caller declares, exactly as
#536 and #494 require, so a screen and its test agree about what this week is.

**Terminology.** Nothing here coins a customer word. The ticket's own candidate
labels — `This Week`, `Review needed`, `Ready to issue` — are provisional until
the repository terminology procedure approves them, so none of them is written
anywhere in this repository. Each state prints the sentence its section already
prints in #536, built from adopted glossary terms; the state names below are
internal identifiers and never reach a screen.

**What is deliberately not derived here.** ADR-0085's three visible consequence
levels are absent for the reason #536 records: they project onto ADR-0086's
declared content inventory, and ADR-0091's own migration note says that
configured issue set is not modelled. A level printed beside a project would be
a guess wearing a heading.

**What is deliberately not finished here.** The ticket asks that a release
authorization change the project row using the resulting release record. No
completion action exists and none is possible — that half is met — but the row
cannot yet *learn* that an issue went out, because preparing and authorizing a
release candidate for an adopted project are #529 and #533 and are not built.
The only release receipt in the repository, ``ExternalReportRelease``, belongs
to the legacy External Report path and binds no accepted Project Record
revision, so reading it here would be a guess about which issue it covered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import access
from corridor.analytics import (
    AnalyticsBinding,
    AnalyticsEvent,
    EventFamily,
    default_binding,
    emit_event,
)
from corridor.models import BaselineAdoption, Project
from corridor.project_workflow import (
    FOLLOW_UP,
    ISSUE,
    REVIEW,
    issue_readiness_by_project,
    outstanding_follow_up_by_project,
)
from corridor.review_packet_reading import (
    PARTITION_RULE_VERSION,
    accepted_revision_ids_by_project,
    live_deferrals_by_project,
    proposed_deltas_by_project,
    resolved_delta_ids_by_project,
    standing_accepted_revisions_by_project,
    standing_sets,
    superseding_delta_ids_by_project,
)


# The five semantic states, in the ticket's own precedence order. These are
# internal identifiers; SENTENCES below holds the words a screen prints, and no
# screen prints one of these names.
ISSUE_BLOCKED = "issue_blocked"
REVIEW_WAITING = "review_waiting"
ISSUE_READY = "issue_ready"
FOLLOW_UP_DUE = "follow_up_due"
NO_ACTION = "no_action"

# How a project says it was opened from the portfolio rather than typed or
# bookmarked. Being listed is not being opened, and only the request that
# followed a portfolio link can prove the click happened.
PORTFOLIO = "portfolio"

STATE_PRECEDENCE: tuple[str, ...] = (
    ISSUE_BLOCKED,
    REVIEW_WAITING,
    ISSUE_READY,
    FOLLOW_UP_DUE,
    NO_ACTION,
)

# Each state's own words. They are #536's section headings and its quiet
# sentence, unchanged, because the same fact must not be spelled two ways on
# two screens (ADR-0048) and because no new customer label may be coined before
# the terminology procedure has run on it.
SENTENCES: Mapping[str, str] = {
    ISSUE_BLOCKED: "Something must be put right before the next issue",
    REVIEW_WAITING: "Source changes to review",
    ISSUE_READY: "The issue to approve for sharing",
    FOLLOW_UP_DUE: "Follow-up Plans waiting on an answer",
    NO_ACTION: "Nothing needs attention at this cutoff",
}

# The tone each state is drawn in. Colour is redundant reinforcement only; the
# sentence above is what carries the meaning (`ui_primitives`).
TONES: Mapping[str, str] = {
    ISSUE_BLOCKED: "attention",
    REVIEW_WAITING: "attention",
    ISSUE_READY: "neutral",
    FOLLOW_UP_DUE: "attention",
    NO_ACTION: "settled",
}


@dataclass(frozen=True, slots=True)
class ProjectStanding:
    """One adopted project's row: one primary state and bounded counts.

    The counts are secondary. None of them creates an action of its own and
    none of them changes ``state``; they exist so a coordinator can see the
    shape of a project's week without opening it.
    """

    project_id: int
    slug: str
    name: str
    state: str
    landing: str
    changes_to_review: int
    follow_up_waiting: int
    follow_up_overdue: int
    readiness_problems: int
    accepted_revision_id: int | None

    @property
    def sentence(self) -> str:
        return SENTENCES[self.state]

    @property
    def tone(self) -> str:
        return TONES[self.state]

    @property
    def quiet(self) -> bool:
        """Whether this project asks nothing of the coordinator at all."""

        return self.state == NO_ACTION

    @property
    def counts(self) -> tuple[tuple[str, int], ...]:
        """The bounded secondary counts, each named in adopted terms."""

        return (
            ("Questions to decide", self.changes_to_review),
            ("Follow-up Plans waiting on an answer", self.follow_up_waiting),
            ("Past the date the plan named", self.follow_up_overdue),
            ("Problems to put right first", self.readiness_problems),
        )


@dataclass(frozen=True, slots=True)
class PortfolioReading:
    """Every adopted project one principal may coordinate, at one cutoff."""

    cutoff: datetime
    principal_subject: str
    standings: tuple[ProjectStanding, ...]

    @property
    def waiting(self) -> tuple[ProjectStanding, ...]:
        return tuple(row for row in self.standings if not row.quiet)

    @property
    def quiet(self) -> tuple[ProjectStanding, ...]:
        return tuple(row for row in self.standings if row.quiet)

    def standing(self, project_id: int) -> ProjectStanding | None:
        for row in self.standings:
            if row.project_id == project_id:
                return row
        return None


def read_portfolio(
    session: Session,
    *,
    principal_subject: str,
    as_of: datetime,
) -> PortfolioReading:
    """Every adopted project this principal may coordinate, exactly once each.

    ``principal_subject`` is the authorization scope and the only one: a
    project the signed-in person may read but not coordinate is not here, and
    neither is anything belonging to a customer they are not enrolled with.
    """

    projects = _coordinated_adopted_projects(session, principal_subject)
    return PortfolioReading(
        cutoff=as_of,
        principal_subject=principal_subject,
        standings=derive_standings(session, projects=projects, as_of=as_of),
    )


def _coordinated_adopted_projects(
    session: Session, principal_subject: str
) -> tuple[Project, ...]:
    """The authorization scope, resolved live from the roster on every read.

    Two statements, neither of them per project: the roster join that says
    which projects this person may coordinate, and the adoption receipts that
    say which of those are adopted-baseline projects. A legacy project keeps
    ADR-0035's item-per-record Work List and has no week to summarize, so it
    is not shown a state it does not have.
    """

    projects = access.coordinated_projects(session, principal_subject)
    if not projects:
        return ()
    adopted = set(
        session.scalars(
            select(BaselineAdoption.project_id).where(
                BaselineAdoption.project_id.in_(
                    tuple(project.id for project in projects)
                )
            )
        ).all()
    )
    return tuple(project for project in projects if project.id in adopted)


def derive_standings(
    session: Session,
    *,
    projects: Sequence[Project],
    as_of: datetime,
) -> tuple[ProjectStanding, ...]:
    """One standing per project, from a fixed number of batched statements."""

    if not projects:
        return ()
    project_ids = tuple(project.id for project in projects)

    deltas = proposed_deltas_by_project(session, project_ids)
    resolved = resolved_delta_ids_by_project(session, project_ids)
    superseded = superseding_delta_ids_by_project(session, project_ids)
    schedules = live_deferrals_by_project(session, project_ids)
    standing = standing_accepted_revisions_by_project(session, project_ids)
    accepted = accepted_revision_ids_by_project(session, project_ids)

    sets = {
        project_id: standing_sets(
            deltas.get(project_id, ()),
            resolved=resolved.get(project_id, set()),
            superseded_by=superseded.get(project_id, {}),
            schedules=schedules.get(project_id, {}),
            standing=standing.get(project_id, {}),
            as_of=as_of,
        )
        for project_id in project_ids
    }
    follow_up = outstanding_follow_up_by_project(
        session,
        open_delta_ids={
            project_id: sets[project_id].open_ids for project_id in project_ids
        },
    )
    readiness = issue_readiness_by_project(session, project_ids=project_ids)

    today = as_of.date()
    return tuple(
        _standing(
            project,
            actionable=sets[project.id].actionable_ids,
            needs=follow_up.get(project.id, ()),
            problems=len(readiness.get(project.id, ())),
            accepted_revision_id=accepted.get(project.id),
            today=today,
        )
        for project in projects
    )


def primary_state(
    *,
    changes_to_review: int,
    follow_up_waiting: int,
    readiness_problems: int,
    accepted_revision_id: int | None,
) -> str:
    """The one state a project row shows, in the declared precedence order.

    A coverage or technical problem blocks the current issue; a review affects
    the current issue; the customer issue is ready for authorization; accepted
    follow-up is due; otherwise no action is required. Exactly one of these is
    shown, and the counts printed beside it never change which.

    Both issue states require an accepted revision, because #536's own issue
    section counts nothing for a project whose record holds nothing yet — there
    is no issue for a coverage problem to block, and none to authorize.
    """

    issue_exists = accepted_revision_id is not None
    if issue_exists and readiness_problems > 0:
        return ISSUE_BLOCKED
    if changes_to_review > 0:
        return REVIEW_WAITING
    if issue_exists:
        return ISSUE_READY
    if follow_up_waiting > 0:
        return FOLLOW_UP_DUE
    return NO_ACTION


def landing_section(
    *,
    changes_to_review: int,
    follow_up_waiting: int,
    accepted_revision_id: int | None,
) -> str:
    """The section of #536 this project opens on, by #536's own rule.

    Not the same question as the primary state, and deliberately so: the state
    answers "what does this project most need", while this answers "where does
    its own week start". ``ProjectWorkflow.landing`` is the first of the three
    ordered sections still holding work, so this is that rule applied to the
    same numbers rather than a second opinion about them.
    """

    if changes_to_review > 0:
        return REVIEW
    if follow_up_waiting > 0:
        return FOLLOW_UP
    if accepted_revision_id is not None:
        return ISSUE
    return REVIEW


def _standing(
    project: Project,
    *,
    actionable: Sequence[int],
    needs: Sequence,
    problems: int,
    accepted_revision_id: int | None,
    today,
) -> ProjectStanding:
    planned = {need.delta_id for need in needs}
    # The same set `ProjectWorkflow.changes_awaiting_decision` names: each
    # actionable delta is offered by exactly one item, so a question is waiting
    # on the coordinator's own judgement exactly when one of these is.
    to_review = sum(1 for delta_id in actionable if delta_id not in planned)
    overdue = sum(1 for need in needs if need.overdue(today=today))

    state = primary_state(
        changes_to_review=to_review,
        follow_up_waiting=len(needs),
        readiness_problems=problems,
        accepted_revision_id=accepted_revision_id,
    )
    landing = landing_section(
        changes_to_review=to_review,
        follow_up_waiting=len(needs),
        accepted_revision_id=accepted_revision_id,
    )

    return ProjectStanding(
        project_id=project.id,
        slug=project.slug,
        name=project.name,
        state=state,
        landing=landing,
        changes_to_review=to_review,
        follow_up_waiting=len(needs),
        follow_up_overdue=overdue,
        readiness_problems=problems,
        accepted_revision_id=accepted_revision_id,
    )


# --- the measurement events this reading owes the contract (#558) ----------
#
# The contract names two families for this surface: one presentation event
# carrying the projects shown and the state derived for each, and one selection
# event per project a person actually opened. Nothing is emitted for a project
# that was shown and left alone, so a quiet project's zero-click week is read
# from the presentation record plus the *absence* of a selection naming it —
# which is only a fact if silence is never written.
#
# Both events take their instant from the reading's declared cutoff, never from
# a clock, so a test states the moment. Customer and project identity stays in
# the payload; the metric labels carry only the bounded shape of the portfolio
# (#491, #522).


def portfolio_binding(binding: AnalyticsBinding | None = None) -> AnalyticsBinding:
    """Bind portfolio events to the packetizer rule the standings were built by."""

    base = binding or default_binding()
    if base.packetizer_rules_version == PARTITION_RULE_VERSION:
        return base
    return AnalyticsBinding(
        code_revision=base.code_revision,
        product_revision=base.product_revision,
        packetizer_rules_version=PARTITION_RULE_VERSION,
        source_configuration=base.source_configuration,
        connector_configuration=base.connector_configuration,
        template_identity=base.template_identity,
        mapping_identity=base.mapping_identity,
        enabled_feature_flags=base.enabled_feature_flags,
    )


def emit_portfolio_reading(
    reading: PortfolioReading, *, binding: AnalyticsBinding | None = None
) -> None:
    """One event per presentation, naming every project shown and its state."""

    emit_event(
        AnalyticsEvent(
            family=EventFamily.PORTFOLIO_READING,
            binding=portfolio_binding(binding),
            occurred_at=reading.cutoff,
            payload={
                "principal_subject": reading.principal_subject,
                "cutoff": reading.cutoff.isoformat(),
                "project_count": len(reading.standings),
                "projects": [
                    {
                        "project_id": row.project_id,
                        "state": row.state,
                        "changes_to_review": row.changes_to_review,
                        "follow_up_waiting": row.follow_up_waiting,
                        "readiness_problems": row.readiness_problems,
                    }
                    for row in reading.standings
                ],
            },
            metric_labels={
                "surface": "portfolio",
                "status": "presented",
            },
        )
    )


def emit_project_selection(
    standing: ProjectStanding,
    *,
    principal_subject: str,
    at: datetime,
    binding: AnalyticsBinding | None = None,
) -> None:
    """The coordinator opened this project from the portfolio; being shown is not."""

    emit_event(
        AnalyticsEvent(
            family=EventFamily.PROJECT_SELECTION,
            binding=portfolio_binding(binding),
            occurred_at=at,
            payload={
                "principal_subject": principal_subject,
                "project_id": standing.project_id,
                "state": standing.state,
                "landing": standing.landing,
            },
            metric_labels={"surface": "portfolio", "state": standing.state},
        )
    )
