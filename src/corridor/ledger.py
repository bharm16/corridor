"""Reading a Dependency back with its full provenance chain.

A field value shown without the assertions beneath it reproduces exactly the
silent-overwrite behavior this tool exists to replace, so the read model
surfaces every claim and flags where sources disagree.

`ready` and `last_evidenced_at` are computed here rather than stored
(ADR-0002).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import false as sa_false, func, select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.accepted_field_reading import AcceptedConstraint, NativeReadingRefused
from corridor.check_configuration import effective_thresholds
from corridor.dependency_events import (
    CurrentDependencyStatement,
    PublishedDependencyStatement,
    current_dependency_statements,
    current_scope_decision_filter,
    current_statement_evidence_memberships,
)
from corridor.exceptions import (
    Evaluation,
    evaluate_dependency,
)
from corridor.models import (
    CRITICAL_STRATEGIES,
    is_claim,
    is_critical,
    RESOLUTION_STRATEGIES,
    Assertion,
    AuditLog,
    Dependency,
    DependencyEvidenceSufficiency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
)
from corridor.disputes import contradicted_fields, settled_field_names
from corridor.locator_validation import (
    VALID as LOCATOR_VALID,
    evidence_link_locator_validation_status,
    evidence_link_verified,
)
from corridor.operative_support import resolve_operative_support
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project


@dataclass
class AssertionView:
    field_name: str
    value: str | None
    document_id: int | None
    filename: str | None
    page_no: int | None
    quote: str | None
    # The Source Passage Check for this claim's own locator (ADR-0082). A
    # claim with no supporting document has not been checked, which the old
    # boolean could only render as a failure.
    locator_validation_status: str
    document_date: date | None
    text_source: str | None
    document_type: str | None
    image_available: bool

    @property
    def verified(self) -> bool:
        """The retired flag, kept as the projection ADR-0082 defines (#458)."""
        return evidence_link_verified(self.locator_validation_status)


@dataclass
class FieldView:
    name: str
    assertions: list[AssertionView] = field(default_factory=list)
    # Whether a reviewer has said what this field concludes, covering
    # every claim made so far (ADR-0031). Set by the builder from the one
    # settlement query, so this view and the engine cannot disagree.
    settled: bool = False

    @property
    def values(self) -> list[str]:
        return sorted({a.value for a in self.assertions if a.value})

    @property
    def contradicted(self) -> bool:
        """Two or more claims that passed the Source Passage Check and differ.

        Only a passed check counts: a claim whose cited passage is not in its
        source is not evidence of disagreement, it is evidence of a bad
        citation. `is_claim` is the same predicate the engine's query applies,
        so this view and the list page's pill cannot disagree about one record
        again. A settled field is not a disagreement either: the sources still
        say what they said and the record has said what it concludes.
        """
        if self.settled:
            return False
        checked = {
            a.value
            for a in self.assertions
            if a.locator_validation_status == LOCATOR_VALID and is_claim(a.value)
        }
        return len(checked) > 1


@dataclass
class DependencyView:
    dependency: Dependency
    org_name: str | None
    fields: list[FieldView]
    evidence: list[tuple[EvidenceLink, Document]]
    is_ready: bool
    last_evidenced_at: date | None
    events: list[DependencyEvent] = field(default_factory=list)
    # The active Constraints for this record's External Party, so a verbal can
    # be scoped to several of them or to all currently active without leaving
    # the page. Scope stays an explicit human choice, never inferred.
    org_constraints: list[Dependency] = field(default_factory=list)
    audit: list[AuditLog] = field(default_factory=list)
    exceptions: list = field(default_factory=list)
    # The reading these exceptions came from, so the page can stamp the
    # quantities it prints with the clock that produced them (ADR-0003).
    evaluation: Evaluation | None = None
    current_statement: CurrentDependencyStatement | None = None

    @property
    def contradictions(self) -> list[FieldView]:
        return [f for f in self.fields if f.contradicted]

    @property
    def is_critical(self) -> bool:
        """Read from the strategy, never stored beside it (ADR-0009)."""
        return is_critical(self.dependency.resolution_strategy)


@dataclass
class LedgerRow:
    dependency: Dependency
    org_name: str | None
    is_ready: bool
    # Two facts, not one. `evidence_count` is how many links this record
    # carries, which is what the ledger page's Backing column means and
    # what a reviewer chasing a bad citation needs. `verified_evidence_count`
    # is how many of them hold — the question `_ready_ids`,
    # `primary_evidence` and the engine's MISSING_EVIDENCE all ask, and the
    # one a published figure means when it says "evidence".
    evidence_count: int
    verified_evidence_count: int
    assertion_count: int
    contradicted: bool
    committed_statement: PublishedDependencyStatement | None = None
    exceptions: list = field(default_factory=list)
    # ADR-0060 open conditions keeping this row not Ready, shown verbatim with
    # the letter one tap away (#373).
    open_conditions: tuple = ()

    @property
    def committed_date(self) -> date | None:
        """The one scalar-compatible date this Ledger row may render."""
        return (
            self.committed_statement.committed_date
            if self.committed_statement is not None
            else None
        )

    @property
    def committed_event(self) -> DependencyEvent | None:
        """Retain the established Ledger provenance accessor."""
        return self.committed_statement.event if self.committed_statement else None


def browse(
    session: Session,
    project_id: int,
    *,
    evaluation: Evaluation,
    org_id: int | None = None,
    resolution_strategy: str | None = None,
    ready: bool | None = None,
    rule: str | None = None,
    owner: str | None = None,
    limit: int = 200,
) -> list[LedgerRow]:
    """The ledger, filterable, at one evaluation of the project.

    Readiness is computed per row rather than stored, so filtering on it
    happens here rather than in SQL (ADR-0002). Exceptions are computed the
    same way, which is why `evaluation` is part of the interface.

    It is required rather than defaulted. While it defaulted to a fresh
    `evaluate_project`, a caller that forgot it got a second reading against
    a second clock and no error — which is how `make demo` came to publish
    an HTML report, an XLSX and a snapshot from three readings of one
    Ledger. A caller with no evaluation now has to say so.
    """
    if evaluation.project_id != project_id:
        raise ValueError("the evaluation belongs to another project")
    if evaluation.native_population is not None:
        return browse_native_population(evaluation, org_id=org_id, resolution_strategy=resolution_strategy,
                                        ready=ready, rule=rule, owner=owner, limit=limit)
    # A dismissed record is off the working list and stays in history
    # (ADR-0032). Filtered here rather than by every caller, because the
    # list is what "the working list" means.
    query = select(Dependency).where(
        Dependency.project_id == project_id,
        Dependency.dismissed_at.is_(None),
    )
    if owner:
        # `unassigned` is the question a coordinator actually asks first —
        # what has nobody — so it is a value of this filter rather than a
        # separate control. Any other value names a person, matched
        # exactly: an Internal Owner is established by a Work Decision
        # naming someone, and fuzzy-matching people is how a list quietly
        # attributes work to the wrong one.
        if owner == "unassigned":
            query = query.where(Dependency.internal_owner.is_(None))
        else:
            query = query.where(Dependency.internal_owner == owner)
    if org_id:
        query = query.where(Dependency.external_org_id == org_id)
    if resolution_strategy:
        # `critical` is not a strategy — it is the reading of one, and it
        # stays filterable because that is the question a reviewer actually
        # asks. `v0-build-spec.md` names the ledger as browsable by
        # criticality; without this it would take three separate queries.
        #
        # An unknown value matches nothing rather than raising. The column's
        # Enum sets `validate_strings=True`, so comparing it against a
        # string outside the vocabulary raises LookupError at bind time and
        # a hand-edited query string would 500 the page.
        if resolution_strategy == "critical":
            query = query.where(
                Dependency.resolution_strategy.in_(sorted(CRITICAL_STRATEGIES))
            )
        elif resolution_strategy in RESOLUTION_STRATEGIES:
            query = query.where(
                Dependency.resolution_strategy == resolution_strategy
            )
        else:
            query = query.where(sa_false())

    dependencies = session.scalars(query.order_by(Dependency.ref_code)).all()
    ids = [d.id for d in dependencies] or [0]

    orgs = {
        o.id: o.name
        for o in session.scalars(select(ExternalOrg))
    }
    # Both counts in one pass, so they cannot describe different
    # populations — which is the whole failure being fixed: a report cell
    # labelled "With verified evidence" was counting every link.
    evidence_counts = dict(
        session.execute(
            select(EvidenceLink.dependency_id, func.count())
            .where(EvidenceLink.dependency_id.in_(ids))
            .group_by(EvidenceLink.dependency_id)
        ).all()
    )
    event_evidence_counts = current_statement_evidence_memberships(
        session, ids
    ).counts
    assertion_counts = dict(
        session.execute(
            select(Assertion.dependency_id, func.count())
            .where(Assertion.dependency_id.in_(ids))
            .group_by(Assertion.dependency_id)
        ).all()
    )
    contradicted = _contradicted_ids(session, ids)
    support_by_dependency = resolve_operative_support(
        session, (dependency.id for dependency in dependencies)
    )
    publication = evaluation.statement_publication
    if publication is None:
        raise ValueError("the evaluation has no frozen statement publication")
    if publication.project_id != project_id:
        raise ValueError("the statement publication belongs to another project")
    publication_population = set(
        session.scalars(
            select(Dependency.id).where(
                Dependency.project_id == project_id,
                Dependency.dismissed_at.is_(None),
            )
        ).all()
    )
    if set(publication.by_dependency) != publication_population:
        raise ValueError("the statement publication has a different Ledger population")

    # Exceptions are computed, never stored (ADR-0002's reasoning), so they
    # are read off the evaluation the caller published rather than joined.
    by_dependency = evaluation.by_dependency()

    rows = [
        LedgerRow(
            dependency=d,
            org_name=orgs.get(d.external_org_id),
            is_ready=support_by_dependency[d.id].is_ready,
            evidence_count=(
                evidence_counts.get(d.id, 0) + event_evidence_counts.get(d.id, 0)
            ),
            verified_evidence_count=(
                support_by_dependency[d.id].verified_evidence_count
            ),
            assertion_count=assertion_counts.get(d.id, 0),
            contradicted=d.id in contradicted,
            committed_statement=publication.by_dependency[d.id],
            exceptions=by_dependency.get(d.id, []),
            open_conditions=support_by_dependency[d.id].open_conditions,
        )
        for d in dependencies
    ]
    if ready is not None:
        rows = [r for r in rows if r.is_ready is ready]
    if rule:
        rows = [r for r in rows if any(e.rule == rule for e in r.exceptions)]
    return rows[:limit]


def browse_native_population(evaluation: Evaluation, *, org_id=None, resolution_strategy=None,
                             ready=None, rule=None, owner=None, limit=200):
    """Filter accepted native rows without querying legacy current-value columns."""
    population = evaluation.native_population
    if population is None:
        raise NativeReadingRefused("native Constraint Log requires a native evaluation")
    rows = []
    for record in population.open_records:
        if org_id is not None and record.external_org_id != org_id:
            continue
        if owner and ((owner == "unassigned" and record.internal_owner) or
                      (owner != "unassigned" and record.internal_owner != owner)):
            continue
        if resolution_strategy and not (
            (resolution_strategy == "critical" and is_critical(record.resolution_strategy))
            or resolution_strategy == record.resolution_strategy):
            continue
        passages = record.source_passages
        found = evaluation.for_dependency(record.id)
        row = LedgerRow(record, record.org_name, False, len(passages), len(record.checked_source_passages),
            len(record.fields), False, evaluation.statement_publication.by_dependency[record.id], found)
        if ready is not None and row.is_ready is not ready:
            continue
        if rule and not any(item.rule == rule for item in found):
            continue
        rows.append(row)
    return rows[:limit]


@dataclass(frozen=True)
class Evidence:
    """The quote a published value cites, and where it appears."""

    document_id: int
    filename: str
    page_no: int
    quote: str


def primary_evidence(
    session: Session, dependency_ids: list[int]
) -> dict[int, Evidence]:
    """Compatibility read of explicitly designated record publication support."""
    resolved = resolve_operative_support(session, dependency_ids)
    return {
        dependency_id: Evidence(
            document_id=support.publication.document_id,
            filename=support.publication.filename,
            page_no=support.publication.page_no,
            quote=support.publication.quote,
        )
        for dependency_id, support in resolved.items()
        if support.publication is not None
    }


def _ready_ids(session: Session, ids: list[int]) -> set[int]:
    return {
        dependency_id
        for dependency_id, support in resolve_operative_support(session, ids).items()
        if support.is_ready
    }


def _contradicted_ids(session: Session, ids: list[int]) -> set[int]:
    """Which records show the "sources disagree" pill.

    The definition is the engine's, not a second copy of it.
    """
    return set(contradicted_fields(session, ids))


def load_dependency(
    session: Session,
    dependency_id: int,
    *,
    evaluation: Evaluation | None = None,
) -> DependencyView:
    """The record, read against one stated Evaluation.

    A caller that already holds one — a list page rendering a row, a test
    stating its own clock — passes it, and the detail page then agrees
    with the page the reader arrived from. Defaulted rather than required
    so the many callers that only want `is_ready` stay one argument long.
    """
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise LookupError(f"no dependency {dependency_id}")
    # The record page reads under the project's effective declared thresholds,
    # so it agrees with the list and report a reader arrived from.
    evaluation = evaluation or evaluate_dependency(
        session,
        dependency_id,
        thresholds=effective_thresholds(session, dependency.project_id),
    )

    org_name = None
    if dependency.external_org_id:
        org = session.get(ExternalOrg, dependency.external_org_id)
        org_name = org.name if org else None

    rows = session.execute(
        select(Assertion, EvidenceLink, Document, DocPage)
        .outerjoin(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
        .outerjoin(Document, EvidenceLink.document_id == Document.id)
        .outerjoin(
            DocPage,
            (DocPage.document_id == EvidenceLink.document_id)
            & (DocPage.page_no == EvidenceLink.page_no),
        )
        .where(Assertion.dependency_id == dependency_id)
        .order_by(Assertion.field_name, Assertion.id)
    ).all()

    settled = settled_field_names(session, [dependency_id]).get(
        dependency_id, set()
    )
    by_field: dict[str, FieldView] = {}
    for assertion, link, document, page in rows:
        view = by_field.setdefault(
            assertion.field_name,
            FieldView(assertion.field_name, settled=assertion.field_name in settled),
        )
        view.assertions.append(
            AssertionView(
                field_name=assertion.field_name,
                value=assertion.asserted_value,
                document_id=document.id if document else None,
                filename=document.filename if document else None,
                page_no=link.page_no if link else None,
                quote=link.quote if link else None,
                locator_validation_status=evidence_link_locator_validation_status(
                    link
                ),
                document_date=document.doc_date if document else None,
                text_source=page.text_source if page else None,
                document_type=document.doc_type if document else None,
                image_available=bool(page and page.image_path and Path(page.image_path).is_file()),
            )
        )

    direct_evidence = session.execute(
        select(EvidenceLink, Document)
        .join(Document, EvidenceLink.document_id == Document.id)
        .where(EvidenceLink.dependency_id == dependency_id)
        .order_by(EvidenceLink.id)
    ).all()
    event_memberships = current_statement_evidence_memberships(
        session, (dependency_id,)
    ).for_dependency(dependency_id)
    evidence_by_id = {link.id: (link, document) for link, document in direct_evidence}
    for member in event_memberships:
        evidence_by_id.setdefault(
            member.evidence_link.id, (member.evidence_link, member.document)
        )
    evidence = [evidence_by_id[key] for key in sorted(evidence_by_id)]

    support = resolve_operative_support(session, [dependency_id])[dependency_id]
    current_statement = current_dependency_statements(session, [dependency_id]).get(
        dependency_id
    )
    return DependencyView(
        dependency=dependency,
        org_name=org_name,
        fields=sorted(by_field.values(), key=lambda f: f.name),
        evidence=[(link, doc) for link, doc in evidence],
        is_ready=support.is_ready,
        last_evidenced_at=support.last_evidenced_at,
        events=session.scalars(
            select(DependencyEvent)
            .join(DependencyEventScope, DependencyEventScope.event_id == DependencyEvent.id)
            .join(
                DependencyEventScopeDecision,
                DependencyEventScope.scope_decision_id
                == DependencyEventScopeDecision.id,
            )
            .where(
                DependencyEventScope.dependency_id == dependency_id,
                current_scope_decision_filter(),
            )
            .order_by(DependencyEvent.event_date, DependencyEvent.id)
        ).all(),
        org_constraints=(
            session.scalars(
                select(Dependency)
                .where(
                    Dependency.project_id == dependency.project_id,
                    Dependency.external_org_id == dependency.external_org_id,
                    Dependency.dismissed_at.is_(None),
                )
                .order_by(Dependency.ref_code, Dependency.id)
            ).all()
            if dependency.external_org_id is not None
            else []
        ),
        exceptions=evaluation.for_dependency(dependency_id),
        evaluation=evaluation,
        current_statement=current_statement,
        # Including the Candidate's own entries. The reviewer edits before
        # the Dependency exists, so the record of what the extractor
        # originally said is written against the Candidate — and this view
        # queried only `dependency`, which is why the edit trail the route
        # promises survives has never been visible on the record.
        audit=audit.trail_for_dependency(session, dependency_id),
    )


class NoSuchEvidence(Exception):
    """This evidence link does not belong to this Dependency."""


class UnverifiedEvidence(Exception):
    """Readiness cannot rest on a quote that is not on the page."""


def mark_satisfies(
    session: Session,
    dependency_id: int,
    link_id: int,
    *,
    principal: HumanPrincipal,
) -> bool:
    """Mark, or unmark, an Evidence link as meeting the closure bar.

    The only act that makes a Dependency Ready (ADR-0002), so it lives
    beside `is_ready` rather than in a route: the ownership check, the
    verified precondition and the audit entry are the Ledger's rules, and
    a second caller — a CLI, an API — would otherwise have to reimplement
    all three. The template's `disabled` attribute becomes a courtesy
    rather than the second copy of the guard.

    Returns the resulting mark.
    """
    principal = require_human_principal(principal)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise NoSuchEvidence(f"no evidence {link_id} on dependency {dependency_id}")
    # Reconfirmation copies the current readiness judgment while holding the
    # project lock. Readiness toggles must take the same lock so their serial
    # order, rather than timing between two unlocked reads, decides whether
    # the successor support satisfies the bar.
    session.flush()
    lock_project(session, dependency.project_id)
    session.expire_all()
    if dependency.dismissed_at is not None:
        raise ValueError(
            f"{dependency.ref_code} was dismissed — readiness cannot move "
            "on a record nobody is working"
        )
    link = session.get(EvidenceLink, link_id, populate_existing=True)
    event_membership = (
        current_statement_evidence_memberships(session, (dependency_id,)).find(
            dependency_id, link_id
        )
        if link is not None
        else None
    )
    event_scope = (
        event_membership.scope_link_id if event_membership is not None else None
    )
    if link is None or (link.dependency_id != dependency_id and event_scope is None):
        raise NoSuchEvidence(f"no evidence {link_id} on dependency {dependency_id}")
    if evidence_link_locator_validation_status(link) != LOCATOR_VALID:
        raise UnverifiedEvidence(
            "the source passage check has not passed for supporting document "
            f"{link_id}; readiness cannot rest on a quote that is not on the page"
        )

    designation = session.scalar(
        select(DependencyEvidenceSufficiency).where(
            DependencyEvidenceSufficiency.dependency_id == dependency_id,
            DependencyEvidenceSufficiency.evidence_link_id == link_id,
            DependencyEvidenceSufficiency.scope_link_id == event_scope,
        )
    )
    was = designation is not None
    if designation is None:
        session.add(
            DependencyEvidenceSufficiency(
                dependency_id=dependency_id,
                evidence_link_id=link_id,
                scope_link_id=event_scope,
            )
        )
    else:
        session.delete(designation)
    audit.record(
        session,
        principal=principal,
        action=audit.MARK_SATISFIES_REQUIREMENT,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency_id,
        before={"evidence_link_id": link_id, "satisfies": was},
        after={
            "evidence_link_id": link_id,
            "satisfies": not was,
        },
    )
    session.flush()
    return not was


def is_ready(session: Session, dependency_id: int) -> bool:
    """A passed Source Passage Check that a reviewer marked as meeting the bar.

    Both halves are required. A passed check alone means the quote is really
    on the page; it says nothing about whether the quote closes anything.
    """
    return resolve_operative_support(session, [dependency_id])[dependency_id].is_ready


def last_evidenced_at(session: Session, dependency_id: int) -> date | None:
    """Most recent date on which a document said anything about this record.

    Drives STALE, which measures document silence rather than reviewer
    attention. Falls back to retrieval date when a document carries no date
    of its own — otherwise an undated source would read as infinitely stale.
    """
    return resolve_operative_support(session, [dependency_id])[
        dependency_id
    ].last_evidenced_at
