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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import (
    Assertion,
    AuditLog,
    Dependency,
    DependencyEvent,
    Document,
    EvidenceLink,
    ExternalOrg,
)


@dataclass
class AssertionView:
    field_name: str
    value: str | None
    document_id: int | None
    filename: str | None
    page_no: int | None
    quote: str | None
    verified: bool


@dataclass
class FieldView:
    name: str
    assertions: list[AssertionView] = field(default_factory=list)

    @property
    def values(self) -> list[str]:
        return sorted({a.value for a in self.assertions if a.value})

    @property
    def contradicted(self) -> bool:
        """Two or more verified assertions claiming different values.

        Only verified assertions count: an unverified claim is not evidence
        of disagreement, it is evidence of a bad citation.
        """
        verified = {a.value for a in self.assertions if a.verified and a.value}
        return len(verified) > 1


@dataclass
class DependencyView:
    dependency: Dependency
    org_name: str | None
    fields: list[FieldView]
    evidence: list[tuple[EvidenceLink, Document]]
    is_ready: bool
    last_evidenced_at: date | None
    events: list[DependencyEvent] = field(default_factory=list)
    audit: list[AuditLog] = field(default_factory=list)

    @property
    def contradictions(self) -> list[FieldView]:
        return [f for f in self.fields if f.contradicted]


@dataclass
class LedgerRow:
    dependency: Dependency
    org_name: str | None
    is_ready: bool
    evidence_count: int
    assertion_count: int
    contradicted: bool


def browse(
    session: Session,
    project_id: int,
    *,
    status: str | None = None,
    org_id: int | None = None,
    criticality: str | None = None,
    ready: bool | None = None,
    limit: int = 200,
) -> list[LedgerRow]:
    """The ledger, filterable.

    Readiness is computed per row rather than stored, so filtering on it
    happens here rather than in SQL (ADR-0002).
    """
    query = select(Dependency).where(Dependency.project_id == project_id)
    if status:
        query = query.where(Dependency.status == status)
    if org_id:
        query = query.where(Dependency.external_org_id == org_id)
    if criticality:
        query = query.where(Dependency.criticality == criticality)

    dependencies = session.scalars(query.order_by(Dependency.ref_code)).all()
    ids = [d.id for d in dependencies] or [0]

    orgs = {
        o.id: o.name
        for o in session.scalars(select(ExternalOrg))
    }
    evidence_counts = dict(
        session.execute(
            select(EvidenceLink.dependency_id, func.count())
            .where(EvidenceLink.dependency_id.in_(ids))
            .group_by(EvidenceLink.dependency_id)
        ).all()
    )
    assertion_counts = dict(
        session.execute(
            select(Assertion.dependency_id, func.count())
            .where(Assertion.dependency_id.in_(ids))
            .group_by(Assertion.dependency_id)
        ).all()
    )
    contradicted = _contradicted_ids(session, ids)
    ready_ids = _ready_ids(session, ids)

    rows = [
        LedgerRow(
            dependency=d,
            org_name=orgs.get(d.external_org_id),
            is_ready=d.id in ready_ids,
            evidence_count=evidence_counts.get(d.id, 0),
            assertion_count=assertion_counts.get(d.id, 0),
            contradicted=d.id in contradicted,
        )
        for d in dependencies
    ]
    if ready is not None:
        rows = [r for r in rows if r.is_ready is ready]
    return rows[:limit]


def _ready_ids(session: Session, ids: list[int]) -> set[int]:
    return set(
        session.scalars(
            select(EvidenceLink.dependency_id)
            .where(
                EvidenceLink.dependency_id.in_(ids),
                EvidenceLink.verified.is_(True),
                EvidenceLink.satisfies_requirement.is_(True),
            )
            .distinct()
        ).all()
    )


def _contradicted_ids(session: Session, ids: list[int]) -> set[int]:
    """Fields with two or more distinct verified values.

    Only verified assertions count: an unverified claim is a bad citation,
    not evidence that sources disagree.
    """
    rows = session.execute(
        select(Assertion.dependency_id, Assertion.field_name)
        .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
        .where(
            Assertion.dependency_id.in_(ids),
            EvidenceLink.verified.is_(True),
            Assertion.asserted_value.is_not(None),
        )
        .group_by(Assertion.dependency_id, Assertion.field_name)
        .having(func.count(func.distinct(Assertion.asserted_value)) > 1)
    ).all()
    return {dependency_id for dependency_id, _ in rows}


def load_dependency(session: Session, dependency_id: int) -> DependencyView:
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise LookupError(f"no dependency {dependency_id}")

    org_name = None
    if dependency.external_org_id:
        org = session.get(ExternalOrg, dependency.external_org_id)
        org_name = org.name if org else None

    rows = session.execute(
        select(Assertion, EvidenceLink, Document)
        .outerjoin(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
        .outerjoin(Document, EvidenceLink.document_id == Document.id)
        .where(Assertion.dependency_id == dependency_id)
        .order_by(Assertion.field_name, Assertion.id)
    ).all()

    by_field: dict[str, FieldView] = {}
    for assertion, link, document in rows:
        view = by_field.setdefault(
            assertion.field_name, FieldView(assertion.field_name)
        )
        view.assertions.append(
            AssertionView(
                field_name=assertion.field_name,
                value=assertion.asserted_value,
                document_id=document.id if document else None,
                filename=document.filename if document else None,
                page_no=link.page_no if link else None,
                quote=link.quote if link else None,
                verified=bool(link.verified) if link else False,
            )
        )

    evidence = session.execute(
        select(EvidenceLink, Document)
        .join(Document, EvidenceLink.document_id == Document.id)
        .where(EvidenceLink.dependency_id == dependency_id)
        .order_by(EvidenceLink.id)
    ).all()

    return DependencyView(
        dependency=dependency,
        org_name=org_name,
        fields=sorted(by_field.values(), key=lambda f: f.name),
        evidence=[(link, doc) for link, doc in evidence],
        is_ready=is_ready(session, dependency_id),
        last_evidenced_at=last_evidenced_at(session, dependency_id),
        events=session.scalars(
            select(DependencyEvent)
            .where(DependencyEvent.dependency_id == dependency_id)
            .order_by(DependencyEvent.event_date, DependencyEvent.id)
        ).all(),
        audit=session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id == dependency_id,
            )
            .order_by(AuditLog.ts)
        ).all(),
    )


def is_ready(session: Session, dependency_id: int) -> bool:
    """Verified evidence that a reviewer marked as meeting the bar.

    Both halves are required. `verified` alone means the quote is really on
    the page; it says nothing about whether the quote closes anything.
    """
    return (
        session.scalars(
            select(EvidenceLink.id).where(
                EvidenceLink.dependency_id == dependency_id,
                EvidenceLink.verified.is_(True),
                EvidenceLink.satisfies_requirement.is_(True),
            )
        ).first()
        is not None
    )


def last_evidenced_at(session: Session, dependency_id: int) -> date | None:
    """Most recent date on which a document said anything about this record.

    Drives STALE, which measures document silence rather than reviewer
    attention. Falls back to retrieval date when a document carries no date
    of its own — otherwise an undated source would read as infinitely stale.
    """
    rows = session.execute(
        select(Document.doc_date, Document.retrieved_at)
        .join(EvidenceLink, EvidenceLink.document_id == Document.id)
        .where(
            EvidenceLink.dependency_id == dependency_id,
            EvidenceLink.verified.is_(True),
        )
    ).all()

    dates = []
    for doc_date, retrieved in rows:
        if doc_date:
            dates.append(doc_date)
        elif retrieved:
            dates.append(retrieved.date())
    return max(dates) if dates else None
