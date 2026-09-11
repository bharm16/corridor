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

**The row now reads what a project prepared, not what it happens to hold**
(#636). #537 shipped with #529 and #533 unbuilt, so "the issue to approve for
sharing" was approximated by "an accepted revision exists". Nearly every
meaningful adopted project has an accepted revision, so that rung was
permanently occupied and ``FOLLOW_UP_DUE`` — the rung below it — could not be
reached from any database state at all. Both modules exist now, so
``candidate_awaits_authorization`` asks the real question through their own
readers: a current candidate that is not blocked, not stale, still rendered
through the registered template and mapping, and describing an issue this
project has not already sent. Nothing about the precedence changed; the
predicate above accepted follow-up did.

**A due follow-up and a waiting one are different states.** ``FOLLOW_UP_DUE``
counts the plans #425's band rule would put in an actionable band, not every
Follow-up Plan somebody is waiting on: a plan inside the return window it named
is not late, and a coordinator told to chase it would be chasing a person who
still has time. ``follow_up_waiting`` keeps counting all of them beside the
row, because the count is what a coordinator reads and the state is what they
act on.

**Corridor's own work in progress is context, never a sixth state** (#675). A
preparation this project asked for and has not finished asks *nothing* of a
coordinator: there is no act to take while a worker is rendering, and a state
for it would be a row headed by a condition the person reading it cannot
clear. So an in-flight request prints one bounded sentence beside whichever
state the records produced, and a project with nothing else outstanding stays
*no action required* while it prepares. The two ends of that preparation are
already states this reading has: a finished attempt that produced nothing is a
current-issue blocker, through ``issue_readiness``'s own
``PREPARATION_FAILED`` problem rather than a second opinion here, and a
finished attempt that produced a candidate reaches ``ISSUE_READY`` through the
same ``candidate_awaits_authorization`` every other candidate is read by.

**What is deliberately not derived here.** The commitment and retained-request
triggers of #425 — an accepted date that has passed, a retained outgoing
request past its declared boundary — are not part of this row's follow-up
count. Reaching them needs the whole accepted projection of every project
shown, which no bounded cross-project read can afford, and approximating them
would be exactly the second authority this module refuses to hold. A
coordinator sees them on the project's own chase list.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import access
from corridor.analytics import (
    AnalyticsBinding,
    default_binding,
    emit_event,
    portfolio_reading_event,
    project_selection_event,
)
from corridor.baseline_adoption import effective_baseline_formats_by_project
from corridor.follow_up_bundles import actionable_plan_count
from corridor.issue_profile import IssueInventory, effective_issue_inventories
from corridor.models import (
    BLOCKED,
    BaselineAdoption,
    Project,
    ReleaseCandidate,
    ReleasePackage,
)
from corridor.project_workflow import (
    FOLLOW_UP,
    ISSUE,
    REVIEW,
    issue_readiness_by_project,
    outstanding_follow_up_by_project,
)
from corridor.release_authorization import issue_state_differences
from corridor.release_preparation import preparation_standings
from corridor.release_candidate import (
    candidate_staleness_reasons,
    current_candidates_by_project,
    latest_authorized_packages_by_project,
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

# What a row says while Corridor itself is preparing this project's next
# issue. It is secondary context and never a state: #675's own
# ``PreparationStanding`` records that a project being prepared asks nothing of
# a coordinator, so the five above are untouched and this sentence is printed
# beside whichever one the records produced. The words are the Issue section's
# own (`web.issue_section.LABELS[PREPARING]`), not a new label, because the
# same fact must not be spelled two ways on two screens (ADR-0048); a test
# asserts the two strings are still the same one.
PREPARING_NOTE = "Preparing this issue"

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
    follow_up_due: int
    readiness_problems: int
    accepted_revision_id: int | None
    candidate_ready: bool
    preparing: bool
    measurement_context: dict[str, Any] | None = None

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

    @property
    def notes(self) -> tuple[str, ...]:
        """Bounded secondary sentences that are context and not action.

        Empty for almost every row. A preparation in flight is the only one
        today, and it is here rather than in ``state`` because it asks the
        coordinator for nothing: they cannot start it, hurry it, or clear it.
        """

        return (PREPARING_NOTE,) if self.preparing else ()


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
    # One read of what each project's next issue is doing, used twice and
    # asked for once: a finished failure is an issue-readiness problem, an
    # unfinished request is this row's secondary sentence, and both are the
    # same record (#675). ``issue_readiness_by_project`` reads it itself when
    # no caller hands it over, which is what every single-project caller does.
    preparations = preparation_standings(session, project_ids=project_ids)
    readiness = issue_readiness_by_project(
        session,
        project_ids=project_ids,
        as_of=as_of,
        preparations=preparations,
    )
    # What a project has actually prepared, and what it has actually issued.
    # Five more statements, none of them per project, and every one of them a
    # batched sibling of the reader #529 or #533 already owns.
    candidates = current_candidates_by_project(session, project_ids)
    packages = latest_authorized_packages_by_project(session, project_ids)
    inventories = effective_issue_inventories(session, project_ids, as_of)
    formats = effective_baseline_formats_by_project(session, project_ids)

    today = as_of.date()
    return tuple(
        _standing(
            project,
            actionable=sets[project.id].actionable_ids,
            needs=follow_up.get(project.id, ()),
            problems=len(readiness.get(project.id, ())),
            accepted_revision_id=accepted.get(project.id),
            candidate_ready=candidate_awaits_authorization(
                candidates.get(project.id),
                package=packages.get(project.id),
                inventory=inventories.get(project.id),
                newest_revision_id=accepted.get(project.id) or 0,
                formats=formats.get(project.id, {}),
            ),
            preparing=preparations[project.id].in_flight,
            measurement_context=_measurement_context(inventories.get(project.id), formats.get(project.id, {})),
            today=today,
        )
        for project in projects
    )


def candidate_awaits_authorization(
    candidate: ReleaseCandidate | None,
    *,
    package: ReleasePackage | None,
    inventory: IssueInventory | None,
    newest_revision_id: int,
    formats: Mapping[str, Any],
) -> bool:
    """Whether this project is actually holding an issue somebody may authorize.

    This is the predicate #537 did not have and guessed at. It read "an
    accepted revision exists" and called that an issue ready to approve, and
    since nearly every meaningful adopted project has an accepted revision, the
    row above accepted follow-up was permanently occupied and ``FOLLOW_UP_DUE``
    could not be reached from any database state at all. The precedence was
    never wrong; this predicate was missing.

    Three questions, each answered by the module that owns it and none of them
    re-derived here:

    * A candidate exists. Before #529 prepares one there is nothing to
      authorize, however complete the record is.
    * #529 would still stand behind it — ``candidate_staleness_reasons`` for
      the profile, the accepted revision and the comparison baseline, plus the
      candidate's own ``blocked`` readiness, which is the other half of
      ``authorization_blockers``.
      Since #829 that includes the replaced output template and field
      mapping, which used to be #533's own check alone
      (``candidate_format_differences``) and is now one term of the staleness
      rule, so a screen cannot offer an approval the authorization refuses.
    * The current issue state has not already been issued —
      ``issue_state_differences`` (#533). A candidate can be perfectly fresh
      and still describe an issue the customer already has, and offering that
      as work to do would ask somebody to send the same package twice.

    A row that says "ready to authorize" when #533 would refuse the
    authorization is worse than no row, which is why every term here is the
    authorizing module's own and not a portfolio opinion about it.
    """

    if candidate is None:
        return False
    if candidate.readiness == BLOCKED:
        return False
    if candidate_staleness_reasons(
        candidate,
        inventory=inventory,
        newest_revision_id=newest_revision_id,
        current_package_id=None if package is None else int(package.id),
        formats=formats,
    ):
        return False
    return bool(
        issue_state_differences(
            package,
            newest_revision_id=newest_revision_id,
            inventory=inventory,
            formats=formats,
        )
    )


def primary_state(
    *,
    changes_to_review: int,
    follow_up_due: int,
    readiness_problems: int,
    accepted_revision_id: int | None,
    candidate_ready: bool,
) -> str:
    """The one state a project row shows, in the declared precedence order.

    A coverage or technical problem blocks the current issue; a review affects
    the current issue; a prepared candidate is ready for authorization;
    accepted follow-up is due; otherwise no action is required. Exactly one of
    these is shown, and the counts printed beside it never change which.

    **The order is the maintainer's and it is unchanged (#636).** A due
    follow-up does not outrank a candidate waiting for authorization, and the
    reason is not that follow-up matters less. Authorizing a prepared candidate
    is a bounded act Corridor can record and *clear*; the pilot follow-up
    bundle sends no email, records no delivery and establishes nothing about
    whether the external interaction happened, so promoting it would leave a
    project permanently headed by a condition Corridor has no way to clear. A
    due follow-up stays a prominent count beside the ready issue instead.

    ``ISSUE_BLOCKED`` still requires an accepted revision, because #536's own
    issue section counts nothing for a project whose record holds nothing yet:
    there is no issue for a coverage problem to block. ``ISSUE_READY`` needs no
    such test — ``candidate_ready`` is false without a candidate, and a
    candidate cannot exist without the accepted revision it is keyed to. Nor
    does it need its own readiness guard: a project with a hard readiness
    problem and an accepted revision is caught one rung above, and a candidate
    implies that revision.
    """

    issue_exists = accepted_revision_id is not None
    if issue_exists and readiness_problems > 0:
        return ISSUE_BLOCKED
    if changes_to_review > 0:
        return REVIEW_WAITING
    if candidate_ready:
        return ISSUE_READY
    if follow_up_due > 0:
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

    It keeps the accepted-revision test that ``primary_state`` gave up (#636),
    and that is not an oversight. ``ProjectWorkflow``'s own Issue section still
    counts an accepted revision with no readiness problem as one outstanding
    thing (``project_workflow._issue_section``), so a portfolio link that
    landed anywhere else would drop the coordinator somewhere that project's
    own week does not think it starts. When that section learns about prepared
    candidates, this rule follows it — from there, not from here.
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
    candidate_ready: bool,
    preparing: bool,
    today,
    measurement_context: dict[str, Any] | None = None,
) -> ProjectStanding:
    planned = {need.delta_id for need in needs}
    # The same set `ProjectWorkflow.changes_awaiting_decision` names: each
    # actionable delta is offered by exactly one item, so a question is waiting
    # on the coordinator's own judgement exactly when one of these is.
    to_review = sum(1 for delta_id in actionable if delta_id not in planned)
    overdue = sum(1 for need in needs if need.overdue(today=today))
    # "Waiting on somebody" and "due for coordinator action" are different
    # states and this row must not conflate them (#636). #425's own band rule
    # decides which is which; a plan inside the return window it named is
    # counted beside the row and never becomes the row.
    due = actionable_plan_count(needs, today=today)

    state = primary_state(
        changes_to_review=to_review,
        follow_up_due=due,
        readiness_problems=problems,
        accepted_revision_id=accepted_revision_id,
        candidate_ready=candidate_ready,
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
        follow_up_due=due,
        readiness_problems=problems,
        accepted_revision_id=accepted_revision_id,
        candidate_ready=candidate_ready,
        preparing=preparing,
        measurement_context=measurement_context,
    )


def _measurement_context(inventory: IssueInventory | None, formats: Mapping[str, Any]) -> dict[str, Any]:
    """The already-read project configuration, not a global portfolio default."""
    template, mapping = formats.get("output_template"), formats.get("field_mapping")
    return {
        "issue_profile_identity": inventory.profile_identity if inventory else None,
        "issue_profile_version": inventory.profile_version if inventory else None,
        "issue_profile_sha256": inventory.content_sha256 if inventory else None,
        "template_identity": f"{template.format_identity}:{template.format_version}" if template else None,
        "mapping_identity": f"{mapping.format_identity}:{mapping.format_version}" if mapping else None,
    }


# --- the measurement events this reading owes the contract (#558) ----------
#
# The contract names two families for this surface: one presentation event
# carrying the projects shown and the state derived for each, and one selection
# event per project a person actually opened. Nothing is emitted for a project
# that was shown and left alone, so a quiet project's zero-click week is read
# from the presentation record plus the *absence* of a selection naming it —
# which is only a fact if silence is never written.
#
# **The presentation payload is exactly the row, and that is the contract**
# (#532 reads it next). Every field of ``ProjectStanding`` a coordinator can
# see is in it — the primary state, the landing section the link would enter,
# each secondary count, and whether the row also said a preparation was under
# way — so an analyst reconstructs what was on the screen instead of inferring
# it from a state name. Nothing derived later is added: no ranking, no
# duration, no attention score. A field is added here only when the row itself
# starts showing it.
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
    return replace(base, packetizer_rules_version=PARTITION_RULE_VERSION)


def emit_portfolio_reading(
    reading: PortfolioReading, *, binding: AnalyticsBinding | None = None
) -> None:
    """One event per presentation, naming every project shown and its state."""

    emit_event(
        portfolio_reading_event(
            portfolio_binding(binding),
            occurred_at=reading.cutoff,
            principal_subject=reading.principal_subject,
            cutoff=reading.cutoff.isoformat(),
            projects=[
                {
                    "project_id": row.project_id,
                    "state": row.state,
                    "landing": row.landing,
                    "changes_to_review": row.changes_to_review,
                    "follow_up_waiting": row.follow_up_waiting,
                    "follow_up_overdue": row.follow_up_overdue,
                    "follow_up_due": row.follow_up_due,
                    "readiness_problems": row.readiness_problems,
                    "preparing": row.preparing,
                    "measurement_context": row.measurement_context,
                }
                for row in reading.standings
            ],
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
        project_selection_event(
            portfolio_binding(binding),
            occurred_at=at,
            principal_subject=principal_subject,
            project_id=standing.project_id,
            state=standing.state,
            landing=standing.landing,
            measurement_context=standing.measurement_context,
        )
    )
