"""The exception engine.

Every rule is **computed at read time, never stored**. A stored flag drifts
away from the facts that justify it — the same reasoning that makes
readiness a derived predicate (ADR-0002). Ask the question when you need
the answer and it cannot be stale.

`MISSING_EVIDENCE` cannot fire on a ready Dependency, and that is true by
construction rather than by a guard: readiness requires verified evidence,
so a ready record has some. There is a test asserting it anyway, because if
it ever fails, readiness has become reachable some other way and that is
worth hearing about loudly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import Session, undefer

from corridor.dependency_events import (
    StatementPublication,
    StatementPublicationFingerprint,
    current_dependency_statements,
    published_dependency_statements,
)
from corridor.disputes import contradicted_fields
from corridor.models import (
    Dependency,
    is_critical,
)
from corridor.operative_support import (
    SupersededOperativeScope,
    resolve_operative_support,
)
from corridor.presentation import (
    exception_label as _display_exception_label,
    exception_name as _display_exception_name,
)

# v0.4 adds the coordination rules (#176): MISSING_ACTION beside the
# redefined MISSING_OWNER — both absences of a current Work Decision — and
# ACTION_DUE_SOON/ACTION_OVERDUE over the Action Due Date, kept apart from
# the Need Date and Committed Date lanes so a date the project set for
# itself never masquerades as an External Party's commitment.
# A ruleset change can move published counts without a data edit, which is
# exactly what this version exists to make attributable.
RULESET_VERSION = "v0.4"

# Fixed by v0-build-spec.md §9. Overridable per project, but the defaults are
# documented and a drift changes every count ever recorded.
#
# Thresholds survive the score they used to feed: each defines a predicate
# whose fired fact carries a unit ("no document in 14 days"), where a weight
# defined nothing but a position (ADR-0010).
STALE_DAYS = 14
DUE_SOON_DAYS = 30
# The coordination cadence, not the schedule's: a Next Action due date
# works on the weekly meeting cycle, so its warning horizon is one week
# where the Need Date's is thirty days. Per-project overridable like the
# rest (#176).
ACTION_DUE_SOON_DAYS = 7

# The nine rules. Names only — the weights that used to sit beside them
# (5.0, 4.0, ×3 for criticality) had no source a reader could check, which
# is ADR-0009's finding one layer up, and ADR-0010 abolished them. Tuple
# order carries no meaning and nothing renders it — the facet view orders
# by count and the web filter sorts alphabetically.
RULES: tuple[str, ...] = (
    "MISSING_EVIDENCE",
    "MISSING_DATE",
    "MISSING_OWNER",
    "OVERDUE",
    "DUE_SOON",
    "STALE",
    "CONTRADICTION",
    "ORPHAN",
    "SUPERSEDED_CITATION",
    "MISSING_ACTION",
    "ACTION_DUE_SOON",
    "ACTION_OVERDUE",
)

CUSTOMER_RULE_NAMES = {rule: _display_exception_name(rule) for rule in RULES}

# The rules whose fact carries a number of days. The rest state absences,
# and an absence has no quantity — inventing 0 or infinity for one would be
# the scalar sneaking back in.
QUANTITY_RULES = frozenset(
    {
        "OVERDUE",
        "DUE_SOON",
        "STALE",
        "SUPERSEDED_CITATION",
        "ACTION_DUE_SOON",
        "ACTION_OVERDUE",
    }
)


# Neither of these is "on track", so time-based rules stay quiet on them.
@dataclass(frozen=True)
class Thresholds:
    stale_days: int = STALE_DAYS
    due_soon_days: int = DUE_SOON_DAYS
    action_due_soon_days: int = ACTION_DUE_SOON_DAYS


@dataclass(frozen=True)
class Exception_:
    """One fact about one Dependency, carrying only what a reader can check.

    `quantity_days` is the rule's own number — days past the Committed
    Date, days until the Need Date, days of document silence — and None
    where the fact is an absence. `critical` is the Criticality reading
    (ADR-0009), carried so a view can filter without re-deriving it; it
    never orders and never multiplies (ADR-0010).
    """

    dependency_id: int
    ref_code: str
    rule: str
    detail: str
    quantity_days: int | None
    critical: bool

    @property
    def label(self) -> str:
        """The canonical reader-facing label, exposed to Jinja templates."""
        return format_exception_label(self)


def format_exception_name(rule: str) -> str:
    """Name a rule without making provenance review look like lateness."""
    return _display_exception_name(rule)


def format_exception_label(exception: Exception_) -> str:
    """The reader-facing label for one exception fact.

    The rule name is the finding; the day count, when present, is that
    rule's own quantity rather than a derived or weighted score.
    """
    return _display_exception_label(exception)


@dataclass(frozen=True)
class RuleFacet:
    """One rule's bucket of the facet view: the finding, counted.

    `exceptions` are ordered by the rule's own quantity, most days first,
    absent-quantity rows after, ref-code as the stable final key.
    `has_quantities` is the honest-empty marker: False means there is
    nothing to order by, and a view prints "no dates known" instead of
    ref-code order dressed as a ranking.
    """

    rule: str
    exceptions: tuple[Exception_, ...]
    count: int
    has_quantities: bool


@dataclass
class _Facts:
    """Everything the rules need, gathered once per dependency."""

    dependency: Dependency
    is_ready: bool
    readiness_lapsed: bool
    has_verified_evidence: bool
    last_evidenced_at: date | None
    has_closure: bool
    contradicted_fields: list[str]
    superseded_scopes: tuple[SupersededOperativeScope, ...]


def _is_dismissed(session: Session, dependency: Dependency) -> bool:
    """A record nobody is working raises no findings about nobody working
    it (ADR-0032). The project-wide `evaluate` filters in SQL; the
    per-record path has to ask the same question of its one row."""
    return dependency.dismissed_at is not None


def exceptions_for(
    session: Session,
    dependency_id: int,
    *,
    today: date | None = None,
    thresholds: Thresholds | None = None,
) -> list[Exception_]:
    """The bare list, for callers that state their own clock in the call.

    A publisher wants `evaluate_dependency` instead: the same computation,
    returned with the clock, thresholds and ruleset version that produced
    it, so the page can stamp what it printed.
    """
    return list(
        evaluate_dependency(
            session, dependency_id, today=today, thresholds=thresholds
        ).found
    )


def evaluate(
    session: Session,
    project_id: int,
    *,
    today: date | None = None,
    thresholds: Thresholds | None = None,
    committed_dates: Mapping[int, date | None] | None = None,
    statement_publication: StatementPublication | None = None,
) -> list[Exception_]:
    """Every exception on every dependency in the project, worst first."""
    today = today or date.today()
    thresholds = thresholds or Thresholds()

    dependencies = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project_id,
            # A record nobody is working raises no exceptions about
            # nobody working it (ADR-0032).
            Dependency.dismissed_at.is_(None),
        )
    ).all()
    if statement_publication is not None:
        if statement_publication.project_id != project_id:
            raise ValueError("the statement publication belongs to another project")
        if set(statement_publication.by_dependency) != {
            dependency.id for dependency in dependencies
        }:
            raise ValueError(
                "the statement publication has a different Ledger population"
            )
        current_statements = None
    else:
        current_statements = current_dependency_statements(
            session, (dependency.id for dependency in dependencies)
        )

    committed_dates_by_dependency: dict[int, date | None] = {}
    closed_by_dependency: dict[int, bool] = {}
    for dependency in dependencies:
        if statement_publication is not None:
            published = statement_publication.by_dependency[dependency.id]
            projected_date = (
                published.committed_date
                if published.current_event is not None
                else dependency.committed_date
            )
            is_closed = published.is_closed
        else:
            statement = current_statements.get(dependency.id)
            projected_date = statement.effective_date if statement is not None else None
            is_closed = statement.is_closed if statement is not None else False
        committed_dates_by_dependency[dependency.id] = (
            projected_date
            if committed_dates is None
            else committed_dates.get(dependency.id, projected_date)
        )
        closed_by_dependency[dependency.id] = is_closed

    facts_by_dependency = _gather_many(
        session,
        dependencies,
        closed_by_dependency=closed_by_dependency,
    )
    found: list[Exception_] = []
    for dependency in dependencies:
        found.extend(
            _apply(
                facts_by_dependency[dependency.id],
                today,
                thresholds,
                committed_date=committed_dates_by_dependency[dependency.id],
            )
        )
    # A filing order, not a verdict: stable so two runs render identically,
    # and claiming nothing — "worst first" belongs to the facet view, where
    # the ordering fact is named (ADR-0010).
    found.sort(key=lambda e: (e.ref_code, e.rule))
    return found


@dataclass(frozen=True)
class Evaluation:
    """One project's exceptions, computed once against a stated clock.

    A bare `list[Exception_]` does not say which `today` produced it, so
    every consumer that wanted a date supplied its own — and a view that
    re-derives "days overdue" against a clock the engine never saw
    publishes two numbers for one fact. The clock, the thresholds and the
    ruleset version travel with the facts instead.

    `thresholds` reaches a caller here for the first time: it is the
    configuration ADR-0010 kept when it abolished the weights. The engine
    stays foundational and applies the supported defaults when a caller
    states none; a reader that wants a project's declared configuration
    resolves it (`check_configuration.effective_thresholds`) and passes it
    in, so a configured value cannot be one the reader ignores.
    """

    project_id: int
    today: date
    thresholds: Thresholds
    ruleset_version: str
    found: tuple[Exception_, ...]
    # The exact statement-date reading used by the rules. Publishers pair
    # this with StatementPublication so a withheld date cannot still fire a
    # date-derived Exception beside an empty cell.
    committed_dates: Mapping[int, date | None]
    statement_publication_fingerprint: StatementPublicationFingerprint | None = None
    statement_publication: StatementPublication | None = None

    def __post_init__(self) -> None:
        """Preserve the exact input map the Exceptions were computed against."""
        object.__setattr__(
            self, "committed_dates", MappingProxyType(dict(self.committed_dates))
        )

    def by_dependency(self) -> dict[int, list[Exception_]]:
        """The other grouping every consumer needs, beside `facets`.

        The ledger row, the export, the snapshot and the report each built
        this by hand; the ordering is the engine's own filing order, so no
        consumer invents one.
        """
        grouped: dict[int, list[Exception_]] = {}
        for exception in self.found:
            grouped.setdefault(exception.dependency_id, []).append(exception)
        return grouped

    def for_dependency(self, dependency_id: int) -> list[Exception_]:
        return [e for e in self.found if e.dependency_id == dependency_id]

    def facets(self) -> list[RuleFacet]:
        return facets(list(self.found))


def evaluate_project(
    session: Session,
    project_id: int,
    *,
    today: date | None = None,
    thresholds: Thresholds | None = None,
    committed_dates: Mapping[int, date | None] | None = None,
    statement_publication: StatementPublication | None = None,
) -> Evaluation:
    """Evaluate a project once, and hand back the clock along with the facts."""
    today = today or date.today()
    thresholds = thresholds or Thresholds()
    dependencies = session.scalars(
        select(Dependency)
        .options(undefer(Dependency.cost_responsibility))
        .where(
            Dependency.project_id == project_id,
            Dependency.dismissed_at.is_(None),
        )
    ).all()
    dependency_ids = [dependency.id for dependency in dependencies]
    publication = statement_publication or published_dependency_statements(
        session, dependency_ids, project_id=project_id
    )
    if publication.project_id != project_id:
        raise ValueError("the statement publication belongs to another project")
    if set(publication.by_dependency) != set(dependency_ids):
        raise ValueError("the statement publication has a different Ledger population")
    evaluated_committed_dates = {
        dependency.id: (
            publication.by_dependency[dependency.id].committed_date
            if publication.by_dependency[dependency.id].current_event is not None
            else dependency.committed_date
        )
        for dependency in dependencies
    }
    if committed_dates is not None:
        evaluated_committed_dates.update(
            {
                dependency_id: committed_dates[dependency_id]
                for dependency_id in dependency_ids
                if dependency_id in committed_dates
            }
        )
    if statement_publication is not None and committed_dates is None:
        evaluated_committed_dates = dict(publication.committed_dates)
    return Evaluation(
        project_id=project_id,
        today=today,
        thresholds=thresholds,
        ruleset_version=RULESET_VERSION,
        found=tuple(
            evaluate(
                session,
                project_id,
                today=today,
                thresholds=thresholds,
                committed_dates=evaluated_committed_dates,
                statement_publication=publication,
            )
        ),
        committed_dates=evaluated_committed_dates,
        statement_publication_fingerprint=publication.fingerprint,
        statement_publication=publication,
    )


def evaluate_dependency(
    session: Session,
    dependency_id: int,
    *,
    today: date | None = None,
    thresholds: Thresholds | None = None,
    committed_dates: Mapping[int, date | None] | None = None,
    statement_publication: StatementPublication | None = None,
) -> Evaluation:
    """One dependency's exceptions, computed once against a stated clock.

    The detail view publishes "40 days overdue" exactly as the report and
    the export do, and had no way to say which clock produced it: the
    record page printed a quantity with no evaluated date and no ruleset
    version beside it. Same stamp, one record's worth of facts.
    """
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise LookupError(f"no dependency {dependency_id}")
    today = today or date.today()
    thresholds = thresholds or Thresholds()
    publication = statement_publication or published_dependency_statements(
        session, (dependency_id,), project_id=dependency.project_id
    )
    if publication.project_id != dependency.project_id:
        raise ValueError("the statement publication belongs to another project")
    if set(publication.by_dependency) != {dependency_id}:
        raise ValueError("the statement publication has a different Ledger population")
    statement = publication.by_dependency[dependency_id]
    projected_date = (
        statement.committed_date
        if statement.current_event is not None
        else dependency.committed_date
    )
    committed_date = (
        projected_date
        if committed_dates is None
        else committed_dates.get(dependency_id, projected_date)
    )
    return Evaluation(
        project_id=dependency.project_id,
        today=today,
        thresholds=thresholds,
        ruleset_version=RULESET_VERSION,
        found=(
            ()
            if _is_dismissed(session, dependency)
            else tuple(
                _apply(
                    _gather(
                        session,
                        dependency,
                        is_closed=statement.is_closed,
                    ),
                    today,
                    thresholds,
                    committed_date=(committed_date),
                )
            )
        ),
        committed_dates={dependency_id: committed_date},
        statement_publication_fingerprint=publication.fingerprint,
        statement_publication=publication,
    )


def facets(found: list[Exception_]) -> list[RuleFacet]:
    """The one grouped view every consumer renders (ADR-0010, #115).

    Computed here, beside the engine, so every consumer renders one
    structure and none re-derives grouping or invents an order — the CLI
    today; #116 and #117 move the report and the web views onto it. Buckets come out largest first — a count is a fact — with the
    rule name breaking ties; within a bucket, the rule's own quantity
    orders, most days first, absent rows after, ref-code last for
    stability.
    """
    by_rule: dict[str, list[Exception_]] = {}
    for exception in found:
        by_rule.setdefault(exception.rule, []).append(exception)

    view = []
    for rule, bucket in by_rule.items():
        bucket.sort(
            key=lambda e: (
                e.quantity_days is None,
                -(e.quantity_days or 0),
                e.ref_code,
            )
        )
        view.append(
            RuleFacet(
                rule=rule,
                exceptions=tuple(bucket),
                count=len(bucket),
                has_quantities=any(e.quantity_days is not None for e in bucket),
            )
        )
    view.sort(key=lambda f: (-f.count, f.rule))
    return view


def _gather(session: Session, dependency: Dependency, *, is_closed: bool) -> _Facts:
    return _gather_many(
        session,
        (dependency,),
        closed_by_dependency={dependency.id: is_closed},
    )[dependency.id]


def _gather_many(
    session: Session,
    dependencies: Iterable[Dependency],
    *,
    closed_by_dependency: Mapping[int, bool],
) -> dict[int, _Facts]:
    """Gather the project-wide inputs once before pure rule application."""
    rows = tuple(dependencies)
    ids = tuple(dependency.id for dependency in rows)
    support_by_dependency = resolve_operative_support(session, ids)
    contradicted_by_dependency = contradicted_fields(session, ids)

    return {
        dependency.id: _Facts(
            dependency=dependency,
            is_ready=support_by_dependency[dependency.id].is_ready,
            readiness_lapsed=bool(
                support_by_dependency[dependency.id].readiness
                and not support_by_dependency[dependency.id].current_readiness
            ),
            has_verified_evidence=bool(
                support_by_dependency[dependency.id].verified_evidence_count
            ),
            last_evidenced_at=support_by_dependency[dependency.id].last_evidenced_at,
            has_closure=closed_by_dependency.get(dependency.id, False),
            contradicted_fields=contradicted_by_dependency.get(dependency.id, []),
            superseded_scopes=support_by_dependency[dependency.id].superseded_scopes,
        )
        for dependency in rows
    }


def _apply(
    facts: _Facts,
    today: date,
    thresholds: Thresholds,
    *,
    committed_date: date | None = None,
) -> list[Exception_]:
    dependency = facts.dependency
    # "On track" for time-based rules means neither proven Ready nor closed
    # by an attributable External Party fact.
    # Supersession can lapse currency without changing the underlying
    # schedule fact. The record needs provenance review, but registration
    # alone must not manufacture new DUE_SOON/STALE/MISSING_OWNER findings
    # that were suppressed while the same proof was current (ADR-0016).
    live = not facts.is_ready and not facts.readiness_lapsed and not facts.has_closure

    # (rule, detail, quantity_days) — the quantity is the rule's own
    # number, and None where the fact is an absence.
    found: list[tuple[str, str, int | None]] = []

    # Both coordination absences read the projections the Work Decision
    # seam maintains equal to its chain tails and defends with a divergence
    # refusal (ADR-0025) — so each predicate is "no current Work Decision
    # establishes this" without walking the chain per record. Queries,
    # never stored flags.
    if not dependency.internal_owner and live:
        found.append(
            ("MISSING_OWNER", "no Work Decision assigns an internal owner", None)
        )

    if not dependency.next_action and live:
        found.append(("MISSING_ACTION", "no Work Decision sets a next action", None))

    if dependency.next_action and dependency.action_due_date and live:
        days_until = (dependency.action_due_date - today).days
        if days_until < 0:
            found.append(
                (
                    "ACTION_OVERDUE",
                    f"the project's own action was due "
                    f"{dependency.action_due_date}, {-days_until} days ago",
                    -days_until,
                )
            )
        elif days_until <= thresholds.action_due_soon_days:
            found.append(
                (
                    "ACTION_DUE_SOON",
                    f"the project's own action is due in {days_until} days "
                    f"({dependency.action_due_date})",
                    days_until,
                )
            )

    if not committed_date and live:
        found.append(
            ("MISSING_DATE", "no committed date from the external party", None)
        )

    if not facts.has_verified_evidence and not facts.has_closure:
        found.append(("MISSING_EVIDENCE", "no verified evidence on this record", None))

    if live:
        if facts.last_evidenced_at is None:
            # An absence, not an age: an age would be measured from an
            # invented origin, which is how a scalar sneaks back in.
            found.append(("STALE", "no dated evidence at all", None))
        else:
            age = (today - facts.last_evidenced_at).days
            if age > thresholds.stale_days:
                found.append(
                    (
                        "STALE",
                        f"no document has spoken to this in {age} days "
                        f"(last {facts.last_evidenced_at})",
                        age,
                    )
                )

    if live and dependency.need_date:
        days = (dependency.need_date - today).days
        if 0 <= days <= thresholds.due_soon_days:
            found.append(
                ("DUE_SOON", f"needed in {days} days ({dependency.need_date})", days)
            )

    if committed_date and committed_date < today:
        if not facts.has_closure:
            days = (today - committed_date).days
            found.append(
                (
                    "OVERDUE",
                    f"committed {committed_date}, {days} days ago, "
                    "with no closure event",
                    days,
                )
            )

    if facts.contradicted_fields:
        found.append(
            (
                "CONTRADICTION",
                "sources disagree on " + ", ".join(sorted(facts.contradicted_fields)),
                None,
            )
        )

    if facts.superseded_scopes:
        # Registry constraints require the authority's replacement date on
        # every Supersession edge. Keep the fallback quantity absent rather
        # than substituting a document, retrieval, or ingestion date if a
        # pre-constraint row is ever encountered.
        dates = tuple(
            scope.evidence.superseded_on
            for scope in facts.superseded_scopes
            if scope.evidence.superseded_on is not None
        )
        superseded_on = min(dates) if dates else None
        quantity = (today - superseded_on).days if superseded_on else None
        scope_details = set()
        for scope in facts.superseded_scopes:
            replacement_date = scope.evidence.superseded_on or "unavailable"
            scope_details.add(
                f"{scope.label} (authority replacement date {replacement_date})"
            )
        scopes = "; ".join(sorted(scope_details))
        found.append(
            (
                "SUPERSEDED_CITATION",
                f"a supporting document was replaced by a newer revision and is "
                f"no longer current: {scopes}",
                quantity,
            )
        )

    if dependency.milestone_id is None and live:
        found.append(("ORPHAN", "not linked to any milestone", None))

    # The Criticality reading rides along for filtering — a view slices on
    # it, nothing multiplies by it (ADR-0010). A record whose document
    # asserts no strategy reads not-critical, which keeps Project A's
    # 3,235 silent rows out of the critical slice rather than tripling
    # them into it.
    critical = is_critical(dependency.resolution_strategy)
    return [
        Exception_(
            dependency_id=dependency.id,
            ref_code=dependency.ref_code,
            rule=rule,
            detail=detail,
            quantity_days=quantity,
            critical=critical,
        )
        for rule, detail, quantity in found
    ]


def main(argv: list[str]) -> int:
    """`make exceptions ARGS="<slug>"`"""
    import sys

    from corridor.db import Session as SessionFactory
    from corridor.models import Project

    slug = argv[0] if argv else "nhhip-3c2"
    with SessionFactory() as session:
        project = session.scalars(select(Project).where(Project.slug == slug)).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        found = evaluate(session, project.id)
        affected = len({e.dependency_id for e in found})
        critical = len({e.dependency_id for e in found if e.critical})
        print(
            f"{project.name} — ruleset {RULESET_VERSION}: "
            f"{len(found)} constraint alerts across {affected} constraints "
            f"({critical} in the relocation/removal/abandonment group)",
            flush=True,
        )
        # The facet view, not a "worst 10": under v0.1 that list was ten
        # identical sev-3.0 rows whose order was ref-code alphabetical
        # dressed as a ranking (ADR-0010's exhibit). What is wrong and how
        # much of it is the counts; within a rule, the rule's own quantity
        # orders; a bucket with nothing to order by says so.
        for facet in facets(found):
            if not facet.has_quantities:
                note = (
                    "no dates known"
                    if facet.rule in QUANTITY_RULES
                    else "no quantity — the fact is the absence"
                )
                print(
                    f"  {facet.count:>4}  {format_exception_name(facet.rule)} "
                    f"[{facet.rule}] {note}"
                )
                continue
            print(
                f"  {facet.count:>4}  {format_exception_name(facet.rule)} "
                f"[{facet.rule}], by days:"
            )
            for e in facet.exceptions[:5]:
                days = f"{e.quantity_days}d" if e.quantity_days is not None else "—"
                mark = " [relocation/removal/abandonment]" if e.critical else ""
                print(
                    f"        {days:>5}  {e.ref_code:<12}{mark}  "
                    f"technical detail: {e.detail[:56]}"
                )
            if facet.count > 5:
                print(f"        … and {facet.count - 5} more")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
