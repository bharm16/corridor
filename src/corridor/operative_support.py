"""One authoritative reading of the Evidence that is operative now.

Publication support is an explicit human designation. Readiness support is
the separate ``satisfies_requirement`` judgment. This resolver combines the
roles for readers without turning either into the other or inferring support
from Evidence insertion order (ADR-0017).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from corridor.models import Dependency, Document, EvidenceLink, OperativeSupport
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project


class NoSuchSupportEvidence(ValueError):
    """The requested EvidenceLink does not belong to the Dependency."""


@dataclass(frozen=True)
class EvidenceSupport:
    evidence_link_id: int
    dependency_id: int
    document_id: int
    filename: str
    page_no: int
    quote: str
    verified: bool
    satisfies_requirement: bool
    evidence_date: date | None
    superseded_by: int | None
    superseded_on: date | None

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None


@dataclass(frozen=True)
class SupersededOperativeScope:
    """One exact support role that still cites a superseded revision."""

    role: str
    field_name: str | None
    evidence: EvidenceSupport

    @property
    def label(self) -> str:
        if self.role == "publication" and self.field_name is not None:
            return f"publication field {self.field_name}"
        if self.role == "publication":
            return "record publication"
        return self.role


@dataclass(frozen=True)
class ResolvedSupport:
    dependency_id: int
    publication: EvidenceSupport | None
    publication_by_field: tuple[tuple[str, EvidenceSupport], ...]
    readiness: tuple[EvidenceSupport, ...]
    current_readiness: tuple[EvidenceSupport, ...]
    is_ready: bool
    superseded_scopes: tuple[SupersededOperativeScope, ...]
    verified_evidence_count: int
    last_evidenced_at: date | None

    @property
    def superseded_roles(self) -> frozenset[str]:
        return frozenset(scope.role for scope in self.superseded_scopes)

    def publication_for(self, field_name: str | None = None) -> EvidenceSupport | None:
        if field_name is None:
            return self.publication
        return dict(self.publication_by_field).get(field_name)

    @classmethod
    def empty(cls, dependency_id: int) -> "ResolvedSupport":
        return cls(
            dependency_id=dependency_id,
            publication=None,
            publication_by_field=(),
            readiness=(),
            current_readiness=(),
            is_ready=False,
            superseded_scopes=(),
            verified_evidence_count=0,
            last_evidenced_at=None,
        )


def designate_publication_support(
    session: Session,
    dependency_id: int,
    evidence_link_id: int,
    *,
    principal: HumanPrincipal,
    field_name: str | None = None,
) -> OperativeSupport:
    """Set the human-adjudicated publication support for one record scope."""
    principal = require_human_principal(principal)
    if field_name is not None:
        field_name = field_name.strip()
        if not field_name:
            raise ValueError("field_name must be non-empty when provided")
        if len(field_name) > 64:
            raise ValueError("field_name is longer than 64 characters")

    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise NoSuchSupportEvidence(f"no dependency {dependency_id}")
    # Reconfirmation re-derives every operative scope under this same lock.
    # A direct designation must join that ordering or it can race the
    # all-or-none transfer and silently overwrite the reviewer's newer act.
    session.flush()
    lock_project(session, dependency.project_id)
    session.expire_all()
    link = session.scalar(
        select(EvidenceLink).where(
            EvidenceLink.id == evidence_link_id,
            EvidenceLink.dependency_id == dependency_id,
        )
    )
    if link is None:
        raise NoSuchSupportEvidence(
            f"no evidence {evidence_link_id} on dependency {dependency_id}"
        )

    scope = select(OperativeSupport).where(
        OperativeSupport.dependency_id == dependency_id,
        OperativeSupport.role == "publication",
        (
            OperativeSupport.field_name.is_(None)
            if field_name is None
            else OperativeSupport.field_name == field_name
        ),
    )
    designation = session.scalar(scope)
    if designation is None:
        designation = OperativeSupport(
            dependency_id=dependency_id,
            evidence_link_id=evidence_link_id,
            role="publication",
            field_name=field_name,
            designated_by=principal.subject,
        )
        session.add(designation)
    else:
        session.execute(
            update(OperativeSupport)
            .where(OperativeSupport.id == designation.id)
            .values(
                evidence_link_id=evidence_link_id,
                designated_by=principal.subject,
                designated_at=func.now(),
            )
        )
        session.expire(designation)
    session.flush()
    return designation


def resolve_operative_support(
    session: Session, dependency_ids: Iterable[int]
) -> dict[int, ResolvedSupport]:
    """Resolve every support role in batched reads, with no fallback.

    Readiness keeps every verified human sufficiency judgment as history, but
    only the undominated document frontier is actionable when all such support
    is superseded. Domination follows declared registry edges transitively --
    including through documents without satisfying Evidence -- never dates,
    filenames, or database ids.
    """
    ids = tuple(dict.fromkeys(dependency_ids))
    if not ids:
        return {}

    evidence_by_dependency: dict[int, list[EvidenceSupport]] = {
        dependency_id: [] for dependency_id in ids
    }
    evidence_by_id: dict[int, EvidenceSupport] = {}
    project_ids: set[int] = set()
    for link, document in session.execute(
        select(EvidenceLink, Document)
        .join(Document, EvidenceLink.document_id == Document.id)
        .where(EvidenceLink.dependency_id.in_(ids))
        .order_by(EvidenceLink.id)
    ).all():
        project_ids.add(document.project_id)
        evidence_date = document.doc_date
        if evidence_date is None and document.retrieved_at is not None:
            evidence_date = document.retrieved_at.date()
        support = EvidenceSupport(
            evidence_link_id=link.id,
            dependency_id=link.dependency_id,
            document_id=document.id,
            filename=document.filename,
            page_no=link.page_no,
            quote=link.quote,
            verified=bool(link.verified),
            satisfies_requirement=bool(link.satisfies_requirement),
            evidence_date=evidence_date,
            superseded_by=document.superseded_by,
            superseded_on=document.superseded_on,
        )
        evidence_by_dependency.setdefault(link.dependency_id, []).append(support)
        evidence_by_id[link.id] = support

    successor_by_document = dict(
        session.execute(
            select(Document.id, Document.superseded_by).where(
                Document.project_id.in_(project_ids)
            )
        ).all()
    ) if project_ids else {}

    designations: dict[int, list[OperativeSupport]] = {
        dependency_id: [] for dependency_id in ids
    }
    for designation in session.scalars(
        select(OperativeSupport)
        .where(OperativeSupport.dependency_id.in_(ids))
        .order_by(OperativeSupport.id)
    ):
        designations.setdefault(designation.dependency_id, []).append(designation)

    resolved: dict[int, ResolvedSupport] = {}
    for dependency_id in ids:
        evidence = evidence_by_dependency.get(dependency_id, [])
        verified = tuple(item for item in evidence if item.verified)
        readiness = tuple(
            item for item in verified if item.satisfies_requirement
        )
        current_readiness = tuple(item for item in readiness if item.is_current)

        publication = None
        by_field: dict[str, EvidenceSupport] = {}
        superseded_scopes: list[SupersededOperativeScope] = []
        for designation in designations.get(dependency_id, []):
            support = evidence_by_id.get(designation.evidence_link_id)
            # A human judgment cannot make a mechanically unverified quote
            # citable. Preserve the designation row, but it is not operative.
            if support is None or not support.verified:
                continue
            if designation.field_name is None:
                publication = support
                if not support.is_current:
                    superseded_scopes.append(
                        SupersededOperativeScope(
                            role="publication",
                            field_name=None,
                            evidence=support,
                        )
                    )
            else:
                by_field[designation.field_name] = support
                if not support.is_current:
                    superseded_scopes.append(
                        SupersededOperativeScope(
                            role="publication",
                            field_name=designation.field_name,
                            evidence=support,
                        )
                    )

        if readiness and not current_readiness:
            superseded_scopes.extend(
                SupersededOperativeScope(
                    role="readiness",
                    field_name=None,
                    evidence=support,
                )
                for support in _readiness_frontier(
                    readiness, successor_by_document
                )
            )
        dates = tuple(item.evidence_date for item in verified if item.evidence_date)
        resolved[dependency_id] = ResolvedSupport(
            dependency_id=dependency_id,
            publication=publication,
            publication_by_field=tuple(sorted(by_field.items())),
            readiness=readiness,
            current_readiness=current_readiness,
            is_ready=bool(current_readiness),
            superseded_scopes=tuple(superseded_scopes),
            verified_evidence_count=len(verified),
            last_evidenced_at=max(dates) if dates else None,
        )
    return resolved


def _readiness_frontier(
    readiness: tuple[EvidenceSupport, ...],
    successor_by_document: dict[int, int | None],
) -> tuple[EvidenceSupport, ...]:
    """Return satisfying support not dominated by a later satisfying doc.

    Any missing registry target or cycle makes reachability untrustworthy. In
    that case every scope is retained so callers fail closed rather than
    silently dropping review work.
    """

    satisfying_documents = {support.document_id for support in readiness}
    reachable_by_document: dict[int, frozenset[int]] = {}
    for source_document_id in satisfying_documents:
        current_document_id = source_document_id
        seen = {source_document_id}
        reachable: set[int] = set()
        while True:
            if current_document_id not in successor_by_document:
                return readiness
            successor_document_id = successor_by_document[current_document_id]
            if successor_document_id is None:
                break
            if (
                successor_document_id in seen
                or successor_document_id not in successor_by_document
            ):
                return readiness
            reachable.add(successor_document_id)
            seen.add(successor_document_id)
            current_document_id = successor_document_id
        reachable_by_document[source_document_id] = frozenset(reachable)

    dominated_documents = {
        source_document_id
        for source_document_id, reachable in reachable_by_document.items()
        if reachable.intersection(satisfying_documents)
    }
    return tuple(
        support
        for support in readiness
        if support.document_id not in dominated_documents
    )
