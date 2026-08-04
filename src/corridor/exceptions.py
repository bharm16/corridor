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

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import (
    Assertion,
    Dependency,
    DependencyEvent,
    Document,
    EvidenceLink,
    is_critical,
)

# v0.2: the score died (ADR-0010, #115). Exceptions carry quantities and a
# criticality flag instead of a severity, so every ordering published under
# v0.1 changed meaning — which is exactly what this version exists to make
# attributable.
RULESET_VERSION = "v0.2"

# Fixed by v0-build-spec.md §9. Overridable per project, but the defaults are
# documented and a drift changes every count ever recorded.
#
# Thresholds survive the score they used to feed: each defines a predicate
# whose fired fact carries a unit ("no document in 14 days"), where a weight
# defined nothing but a position (ADR-0010).
STALE_DAYS = 14
DUE_SOON_DAYS = 30

# The eight rules. Names only — the weights that used to sit beside them
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
)

# The rules whose fact carries a number of days. The rest state absences,
# and an absence has no quantity — inventing 0 or infinity for one would be
# the scalar sneaking back in.
QUANTITY_RULES = frozenset({"OVERDUE", "DUE_SOON", "STALE"})

# Neither of these is "on track", so time-based rules stay quiet on them.
SETTLED_STATUSES = ("closed",)


@dataclass(frozen=True)
class Thresholds:
    stale_days: int = STALE_DAYS
    due_soon_days: int = DUE_SOON_DAYS


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
    has_verified_evidence: bool
    last_evidenced_at: date | None
    has_closure: bool
    contradicted_fields: list[str]


def exceptions_for(
    session: Session,
    dependency_id: int,
    *,
    today: date | None = None,
    thresholds: Thresholds | None = None,
) -> list[Exception_]:
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise LookupError(f"no dependency {dependency_id}")
    return _apply(_gather(session, dependency), today or date.today(), thresholds or Thresholds())


def evaluate(
    session: Session,
    project_id: int,
    *,
    today: date | None = None,
    thresholds: Thresholds | None = None,
) -> list[Exception_]:
    """Every exception on every dependency in the project, worst first."""
    today = today or date.today()
    thresholds = thresholds or Thresholds()

    dependencies = session.scalars(
        select(Dependency).where(Dependency.project_id == project_id)
    ).all()

    found: list[Exception_] = []
    for dependency in dependencies:
        found.extend(_apply(_gather(session, dependency), today, thresholds))
    # A filing order, not a verdict: stable so two runs render identically,
    # and claiming nothing — "worst first" belongs to the facet view, where
    # the ordering fact is named (ADR-0010).
    found.sort(key=lambda e: (e.ref_code, e.rule))
    return found


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


def _gather(session: Session, dependency: Dependency) -> _Facts:
    links = session.execute(
        select(EvidenceLink, Document)
        .outerjoin(Document, EvidenceLink.document_id == Document.id)
        .where(EvidenceLink.dependency_id == dependency.id)
    ).all()

    verified = [(link, doc) for link, doc in links if link.verified]
    dates = []
    for link, doc in verified:
        if doc is None:
            continue
        if doc.doc_date:
            dates.append(doc.doc_date)
        elif doc.retrieved_at:
            # An undated document is not infinitely stale; fall back to when
            # we retrieved it.
            dates.append(doc.retrieved_at.date())

    has_closure = (
        session.scalars(
            select(DependencyEvent.id).where(
                DependencyEvent.dependency_id == dependency.id,
                DependencyEvent.event_type == "closure",
            )
        ).first()
        is not None
    )

    contradicted = [
        name
        for name, in session.execute(
            select(Assertion.field_name)
            .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
            .where(
                Assertion.dependency_id == dependency.id,
                EvidenceLink.verified.is_(True),
                # A null is an absent column, not a competing value — the
                # matrix revisions add and drop columns between editions.
                Assertion.asserted_value.is_not(None),
            )
            .group_by(Assertion.field_name)
            .having(func.count(func.distinct(Assertion.asserted_value)) > 1)
        ).all()
    ]

    return _Facts(
        dependency=dependency,
        is_ready=any(
            link.verified and link.satisfies_requirement for link, _ in links
        ),
        has_verified_evidence=bool(verified),
        last_evidenced_at=max(dates) if dates else None,
        has_closure=has_closure,
        contradicted_fields=contradicted,
    )


def _apply(facts: _Facts, today: date, thresholds: Thresholds) -> list[Exception_]:
    dependency = facts.dependency
    settled = dependency.status in SETTLED_STATUSES
    # "On track" for time-based rules means neither proven done nor closed.
    live = not facts.is_ready and not settled

    # (rule, detail, quantity_days) — the quantity is the rule's own
    # number, and None where the fact is an absence.
    found: list[tuple[str, str, int | None]] = []

    if not dependency.internal_owner and live:
        found.append(("MISSING_OWNER", "no internal owner assigned", None))

    if not dependency.committed_date and dependency.status in (
        "identified",
        "in_progress",
        "committed",
    ):
        found.append(
            ("MISSING_DATE", "no committed date from the external party", None)
        )

    if not facts.has_verified_evidence and not settled:
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

    if dependency.committed_date and dependency.committed_date < today:
        if not facts.has_closure:
            days = (today - dependency.committed_date).days
            found.append(
                (
                    "OVERDUE",
                    f"committed {dependency.committed_date}, {days} days ago, "
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

    if dependency.milestone_id is None and not settled:
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
        project = session.scalars(
            select(Project).where(Project.slug == slug)
        ).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        found = evaluate(session, project.id)
        affected = len({e.dependency_id for e in found})
        critical = len({e.dependency_id for e in found if e.critical})
        print(
            f"{project.name} — ruleset {RULESET_VERSION}: "
            f"{len(found)} exceptions across {affected} dependencies "
            f"({critical} critical)",
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
                print(f"  {facet.count:>4}  {facet.rule:<16} {note}")
                continue
            print(f"  {facet.count:>4}  {facet.rule}, by days:")
            for e in facet.exceptions[:5]:
                days = f"{e.quantity_days}d" if e.quantity_days is not None else "—"
                mark = " critical" if e.critical else ""
                print(f"        {days:>5}  {e.ref_code:<12}{mark}  {e.detail[:56]}")
            if facet.count > 5:
                print(f"        … and {facet.count - 5} more")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
