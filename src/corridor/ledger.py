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

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    Assertion,
    Dependency,
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

    @property
    def contradictions(self) -> list[FieldView]:
        return [f for f in self.fields if f.contradicted]


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
