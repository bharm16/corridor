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
)

RULESET_VERSION = "v0.1"

# Fixed by v0-build-spec.md §9. Overridable per project, but the defaults are
# documented and a drift changes every count ever recorded.
STALE_DAYS = 14
DUE_SOON_DAYS = 30

# Rule severity before criticality is applied.
RULES: dict[str, float] = {
    "MISSING_EVIDENCE": 5.0,
    "CONTRADICTION": 5.0,
    "OVERDUE": 4.0,
    "DUE_SOON": 3.0,
    "MISSING_DATE": 3.0,
    "STALE": 2.0,
    "MISSING_OWNER": 2.0,
    "ORPHAN": 1.0,
}

CRITICALITY_WEIGHT = {"critical": 3.0, "high": 2.0, "normal": 1.0}

# Neither of these is "on track", so time-based rules stay quiet on them.
SETTLED_STATUSES = ("closed",)


@dataclass(frozen=True)
class Thresholds:
    stale_days: int = STALE_DAYS
    due_soon_days: int = DUE_SOON_DAYS


@dataclass(frozen=True)
class Exception_:
    dependency_id: int
    ref_code: str
    rule: str
    severity: float
    detail: str


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
    found.sort(key=lambda e: (-e.severity, e.ref_code, e.rule))
    return found


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

    found: list[tuple[str, str]] = []

    if not dependency.internal_owner and live:
        found.append(("MISSING_OWNER", "no internal owner assigned"))

    if not dependency.committed_date and dependency.status in (
        "identified",
        "in_progress",
        "committed",
    ):
        found.append(("MISSING_DATE", "no committed date from the external party"))

    if not facts.has_verified_evidence and not settled:
        found.append(("MISSING_EVIDENCE", "no verified evidence on this record"))

    if live:
        if facts.last_evidenced_at is None:
            found.append(("STALE", "no dated evidence at all"))
        else:
            age = (today - facts.last_evidenced_at).days
            if age > thresholds.stale_days:
                found.append(
                    (
                        "STALE",
                        f"no document has spoken to this in {age} days "
                        f"(last {facts.last_evidenced_at})",
                    )
                )

    if live and dependency.need_date:
        days = (dependency.need_date - today).days
        if 0 <= days <= thresholds.due_soon_days:
            found.append(("DUE_SOON", f"needed in {days} days ({dependency.need_date})"))

    if dependency.committed_date and dependency.committed_date < today:
        if not facts.has_closure:
            days = (today - dependency.committed_date).days
            found.append(
                (
                    "OVERDUE",
                    f"committed {dependency.committed_date}, {days} days ago, "
                    "with no closure event",
                )
            )

    if facts.contradicted_fields:
        found.append(
            (
                "CONTRADICTION",
                "sources disagree on " + ", ".join(sorted(facts.contradicted_fields)),
            )
        )

    if dependency.milestone_id is None and not settled:
        found.append(("ORPHAN", "not linked to any milestone"))

    weight = CRITICALITY_WEIGHT.get(dependency.criticality, 1.0)
    return [
        Exception_(
            dependency_id=dependency.id,
            ref_code=dependency.ref_code,
            rule=rule,
            severity=RULES[rule] * weight,
            detail=detail,
        )
        for rule, detail in found
    ]


def main(argv: list[str]) -> int:
    """`make exceptions ARGS="<slug>"`"""
    import collections
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
        counts = collections.Counter(e.rule for e in found)
        affected = len({e.dependency_id for e in found})
        print(
            f"{project.name} — ruleset {RULESET_VERSION}: "
            f"{len(found)} exceptions across {affected} dependencies",
            flush=True,
        )
        for rule, n in counts.most_common():
            print(f"  {n:>4}  {rule}")
        print("\nworst 10:")
        for e in found[:10]:
            print(f"  sev {e.severity:>5.1f}  {e.ref_code:<12} {e.rule:<16} {e.detail[:60]}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
