"""One authoritative reading of the Evidence that is operative now.

Publication support is an explicit human designation. Readiness support is
the separate ``satisfies_requirement`` judgment. This resolver combines the
roles for readers without turning either into the other or inferring support
from Evidence insertion order (ADR-0017).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from corridor import audit
from corridor.dependency_events import current_statement_evidence_memberships
from corridor.models import (
    Dependency,
    DependencyEvidenceSufficiency,
    Document,
    EvidenceLink,
    OperativeSupport,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project


class NoSuchSupportEvidence(ValueError):
    """The requested EvidenceLink does not belong to the Dependency."""


class UnsafeSupportTransfer(ValueError):
    """Current operative scopes no longer match an all-or-none transfer."""


def evidence_is_scoped_to_dependency(
    session: Session,
    evidence: EvidenceLink | None,
    dependency_id: int,
    *,
    require_sufficiency: bool = False,
) -> bool:
    """Whether one Evidence identity belongs to this record's stated scope.

    Direct evidence owns a Dependency. Event evidence owns an event and is
    visible only through its explicit scope; both use the independent,
    per-Dependency sufficiency judgment for readiness.
    """
    if evidence is None:
        return False
    if evidence.dependency_id == dependency_id:
        if not require_sufficiency:
            return True
        return session.scalar(
            select(DependencyEvidenceSufficiency.id).where(
                DependencyEvidenceSufficiency.dependency_id == dependency_id,
                DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
                DependencyEvidenceSufficiency.scope_link_id.is_(None),
            )
        ) is not None
    membership = current_statement_evidence_memberships(
        session, (dependency_id,)
    ).find(dependency_id, evidence.id)
    if membership is None:
        return False
    if not require_sufficiency:
        return True
    return session.scalar(
        select(DependencyEvidenceSufficiency.id).where(
            DependencyEvidenceSufficiency.dependency_id == dependency_id,
            DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
            DependencyEvidenceSufficiency.scope_link_id
            == membership.scope_link_id,
        )
    ) is not None


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
class SupportTransferMutation:
    """Shared mutation result before a human or machine seals its receipt."""

    evidence: EvidenceLink
    before_json: dict[str, Any]
    moved_scopes: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ResolvedSupport:
    dependency_id: int
    publication: EvidenceSupport | None
    publication_by_field: tuple[tuple[str, EvidenceSupport], ...]
    readiness: tuple[EvidenceSupport, ...]
    current_readiness: tuple[EvidenceSupport, ...]
    is_ready: bool
    readiness_history_trusted: bool
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
            readiness_history_trusted=True,
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
    field_name = _validated_field_name(field_name)

    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise NoSuchSupportEvidence(f"no dependency {dependency_id}")
    # Every support-transfer path re-derives the operative scopes under this
    # same lock. A direct designation must join that ordering or it can race
    # the all-or-none transfer and silently overwrite the newer act.
    session.flush()
    lock_project(session, dependency.project_id)
    session.expire_all()
    return _designate_publication_support_under_lock(
        session,
        dependency_id,
        evidence_link_id,
        designated_by=principal.subject,
        field_name=field_name,
    )


def _validated_field_name(field_name: str | None) -> str | None:
    if field_name is not None:
        field_name = field_name.strip()
        if not field_name:
            raise ValueError("field_name must be non-empty when provided")
        if len(field_name) > 64:
            raise ValueError("field_name is longer than 64 characters")
    return field_name


def _designate_publication_support_under_lock(
    session: Session,
    dependency_id: int,
    evidence_link_id: int,
    *,
    designated_by: str,
    field_name: str | None = None,
) -> OperativeSupport:
    """Change one publication owner after the caller serialized the project.

    This is intentionally private.  The only machine caller is the sealed
    all-scope transfer path; a general actor-based designation API would let
    callers bypass the policy and receipt boundary.
    """

    field_name = _validated_field_name(field_name)
    if not isinstance(designated_by, str) or not designated_by.strip():
        raise ValueError("designated_by must be a non-empty actor")
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise NoSuchSupportEvidence(f"no dependency {dependency_id}")
    link = session.get(EvidenceLink, evidence_link_id)
    membership = current_statement_evidence_memberships(
        session, (dependency_id,)
    ).find(dependency_id, evidence_link_id)
    linked_scope = membership.scope_link_id if membership is not None else None
    if link is None or (link.dependency_id != dependency_id and linked_scope is None):
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
            scope_link_id=linked_scope if linked_scope is not None else None,
            role="publication",
            field_name=field_name,
            designated_by=designated_by,
        )
        session.add(designation)
    else:
        session.execute(
            update(OperativeSupport)
            .where(OperativeSupport.id == designation.id)
            .values(
                evidence_link_id=evidence_link_id,
                scope_link_id=linked_scope if linked_scope is not None else None,
                designated_by=designated_by,
                designated_at=func.now(),
            )
        )
        session.expire(designation)
    session.flush()
    return designation


def transfer_operative_scopes_under_lock(
    session: Session,
    *,
    dependency_id: int,
    successor_document_id: int,
    citation: dict[str, Any],
    scopes: Iterable[SupersededOperativeScope],
    designated_by: str,
) -> SupportTransferMutation:
    """Move one complete support set after the caller locks and re-derives it.

    This stays private because an actor string is not authority. The only
    callers are the human Reconfirmation boundary and the policy/receipt-bound
    Automatic Carry-Forward boundary.
    """

    ordered = tuple(
        sorted(
            scopes,
            key=lambda scope: (
                scope.role,
                scope.field_name or "",
                scope.evidence.evidence_link_id,
            ),
        )
    )
    if not ordered:
        raise UnsafeSupportTransfer("operative support scope is empty")
    for scope in ordered:
        if scope.role == "publication":
            continue
        if scope.role != "readiness":
            raise UnsafeSupportTransfer(
                f"unsupported operative support role {scope.role!r}"
            )
        prior_readiness = session.get(
            EvidenceLink, scope.evidence.evidence_link_id
        )
        if not evidence_is_scoped_to_dependency(
            session, prior_readiness, dependency_id, require_sufficiency=True
        ):
            raise UnsafeSupportTransfer("readiness support changed")

    page_no = citation.get("page")
    quote = citation.get("quote")
    if (
        isinstance(page_no, bool)
        or not isinstance(page_no, int)
        or page_no <= 0
        or not isinstance(quote, str)
        or not quote
    ):
        raise UnsafeSupportTransfer("successor citation is malformed")
    new_evidence = EvidenceLink(
        dependency_id=dependency_id,
        document_id=successor_document_id,
        page_no=page_no,
        quote=quote,
        verified=True,
    )
    session.add(new_evidence)
    session.flush([new_evidence])
    if any(scope.role == "readiness" for scope in ordered):
        session.add(
            DependencyEvidenceSufficiency(
                dependency_id=dependency_id,
                evidence_link_id=new_evidence.id,
                scope_link_id=None,
            )
        )

    prior_scopes: list[dict[str, Any]] = []
    moved_scopes: list[dict[str, Any]] = []
    for scope in ordered:
        if scope.role == "publication":
            _designate_publication_support_under_lock(
                session,
                dependency_id,
                new_evidence.id,
                designated_by=designated_by,
                field_name=scope.field_name,
            )
        prior_scopes.append(
            {
                "role": scope.role,
                "field_name": scope.field_name,
                "evidence_link_id": scope.evidence.evidence_link_id,
            }
        )
        moved_scopes.append(
            {
                "role": scope.role,
                "field_name": scope.field_name,
                "from_evidence_link_id": scope.evidence.evidence_link_id,
                "to_evidence_link_id": new_evidence.id,
            }
        )
    return SupportTransferMutation(
        evidence=new_evidence,
        before_json={"operative_scopes": prior_scopes},
        moved_scopes=tuple(moved_scopes),
    )


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
    evidence_by_id: dict[tuple[int, int], EvidenceSupport] = {}
    project_ids: set[int] = set()
    for link, document, sufficiency_id in session.execute(
        select(EvidenceLink, Document, DependencyEvidenceSufficiency.id)
        .join(Document, EvidenceLink.document_id == Document.id)
        .outerjoin(
            DependencyEvidenceSufficiency,
            and_(
                DependencyEvidenceSufficiency.dependency_id
                == EvidenceLink.dependency_id,
                DependencyEvidenceSufficiency.evidence_link_id == EvidenceLink.id,
                DependencyEvidenceSufficiency.scope_link_id.is_(None),
            ),
        )
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
            satisfies_requirement=sufficiency_id is not None,
            evidence_date=evidence_date,
            superseded_by=document.superseded_by,
            superseded_on=document.superseded_on,
        )
        evidence_by_dependency.setdefault(link.dependency_id, []).append(support)
        evidence_by_id[(link.dependency_id, link.id)] = support

    # Event citations have one identity and no direct Dependency owner.  The
    # shared membership seam gives every affected record visibility of the
    # quote; sufficiency remains a separate readiness judgment.
    memberships = current_statement_evidence_memberships(session, ids)
    event_sufficiency = set(
        session.execute(
            select(
                DependencyEvidenceSufficiency.dependency_id,
                DependencyEvidenceSufficiency.evidence_link_id,
                DependencyEvidenceSufficiency.scope_link_id,
            ).where(
                DependencyEvidenceSufficiency.dependency_id.in_(ids),
                DependencyEvidenceSufficiency.scope_link_id.is_not(None),
            )
        ).all()
    )
    for member in memberships.members:
        dependency_id = member.dependency_id
        link = member.evidence_link
        document = member.document
        project_ids.add(document.project_id)
        evidence_date = document.doc_date
        if evidence_date is None and document.retrieved_at is not None:
            evidence_date = document.retrieved_at.date()
        support = EvidenceSupport(
            evidence_link_id=link.id,
            dependency_id=dependency_id,
            document_id=document.id,
            filename=document.filename,
            page_no=link.page_no,
            quote=link.quote,
            verified=bool(link.verified),
            satisfies_requirement=(
                dependency_id, link.id, member.scope_link_id
            ) in event_sufficiency,
            evidence_date=evidence_date,
            superseded_by=document.superseded_by,
            superseded_on=document.superseded_on,
        )
        evidence_by_dependency.setdefault(dependency_id, []).append(support)
        evidence_by_id[(dependency_id, link.id)] = support

    successor_by_document = dict(
        session.execute(
            select(Document.id, Document.superseded_by).where(
                Document.project_id.in_(project_ids)
            )
        ).all()
    ) if project_ids else {}
    readiness_audit_states = audit.readiness_audit_states_for_dependencies(
        session, ids
    )

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
        readiness_frontier_basis = readiness
        readiness_history_trusted = False
        readiness_audit_state = readiness_audit_states.get(dependency_id)
        stored_readiness_ids = {
            support.evidence_link_id for support in readiness
        }
        if readiness_audit_state is not None:
            basis_ids = (
                readiness_audit_state.ever_satisfying_evidence_ids
                | readiness_audit_state.current_evidence_ids
                | stored_readiness_ids
            )
            historical_basis = tuple(
                evidence_by_id[(dependency_id, evidence_link_id)]
                for evidence_link_id in sorted(basis_ids)
                if (
                    (dependency_id, evidence_link_id) in evidence_by_id
                    and evidence_by_id[(dependency_id, evidence_link_id)].verified
                )
            )
            if len(historical_basis) == len(basis_ids):
                readiness_frontier_basis = historical_basis
                readiness_history_trusted = (
                    readiness_audit_state.current_evidence_ids
                    == stored_readiness_ids
                )

        publication = None
        by_field: dict[str, EvidenceSupport] = {}
        superseded_scopes: list[SupersededOperativeScope] = []
        for designation in designations.get(dependency_id, []):
            support = evidence_by_id.get((dependency_id, designation.evidence_link_id))
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

        if readiness_frontier_basis and not current_readiness:
            superseded_scopes.extend(
                SupersededOperativeScope(
                    role="readiness",
                    field_name=None,
                    evidence=support,
                )
                for support in _readiness_frontier(
                    readiness_frontier_basis, successor_by_document
                )
                if (
                    support.satisfies_requirement
                    if readiness_history_trusted
                    else not support.is_current
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
            readiness_history_trusted=readiness_history_trusted,
            superseded_scopes=tuple(superseded_scopes),
            verified_evidence_count=len(verified),
            last_evidenced_at=max(dates) if dates else None,
        )
    return resolved


def readiness_frontier_before_audit(
    session: Session,
    dependency_id: int,
    audit_id: int,
    *,
    known_terminal_document_id: int,
) -> tuple[EvidenceSupport, ...] | None:
    """Rebuild the exact stale-readiness frontier before a human act.

    The comparison successor was necessarily terminal when the support move
    was offered. Later registry edges may extend that document, so replay
    truncates the graph there. ``None`` means the attributable audit state
    or registry reachability cannot be proven and callers must fail closed.
    """

    dependency = session.get(Dependency, dependency_id)
    terminal = session.get(Document, known_terminal_document_id)
    if (
        dependency is None
        or terminal is None
        or terminal.project_id != dependency.project_id
    ):
        return None
    readiness_state = audit.readiness_audit_state_before_audit(
        session, dependency_id, audit_id
    )
    if readiness_state is None:
        return None
    readiness_ids = (
        readiness_state.current_evidence_ids
        | readiness_state.ever_satisfying_evidence_ids
    )
    if not readiness_ids:
        return ()

    memberships = current_statement_evidence_memberships(
        session, (dependency_id,)
    )
    event_evidence_ids = {
        member.evidence_link.id
        for member in memberships.for_dependency(dependency_id)
    }
    rows = session.execute(
        select(EvidenceLink, Document)
        .join(Document, EvidenceLink.document_id == Document.id)
        .where(
            EvidenceLink.id.in_(readiness_ids),
            or_(
                EvidenceLink.dependency_id == dependency_id,
                EvidenceLink.id.in_(event_evidence_ids),
            ),
        )
        .order_by(EvidenceLink.id)
    ).all()
    if len(rows) != len(readiness_ids):
        return None

    readiness: list[EvidenceSupport] = []
    for link, document in rows:
        if (
            document.project_id != dependency.project_id
            or link.verified is not True
        ):
            return None
        evidence_date = document.doc_date
        if evidence_date is None and document.retrieved_at is not None:
            evidence_date = document.retrieved_at.date()
        readiness.append(
            EvidenceSupport(
                evidence_link_id=link.id,
                dependency_id=dependency_id,
                document_id=document.id,
                filename=document.filename,
                page_no=link.page_no,
                quote=link.quote,
                verified=True,
                # Event replay, not today's mutable flag, establishes the
                # as-of state. Ever-true false rows remain in the basis as
                # tombstones so an older true ancestor cannot resurrect.
                satisfies_requirement=(
                    link.id in readiness_state.current_evidence_ids
                ),
                evidence_date=evidence_date,
                superseded_by=document.superseded_by,
                superseded_on=document.superseded_on,
            )
        )

    successor_by_document = dict(
        session.execute(
            select(Document.id, Document.superseded_by).where(
                Document.project_id == dependency.project_id
            )
        ).all()
    )
    if known_terminal_document_id not in successor_by_document:
        return None
    successor_by_document[known_terminal_document_id] = None
    if any(
        support.document_id not in successor_by_document
        for support in readiness
    ):
        return None
    if any(
        support.satisfies_requirement
        and successor_by_document[support.document_id] is None
        for support in readiness
    ):
        return ()
    frontier = _trusted_readiness_frontier(
        tuple(readiness), successor_by_document
    )
    if frontier is None:
        return None
    return tuple(
        support for support in frontier if support.satisfies_requirement
    )


def _readiness_frontier(
    readiness: tuple[EvidenceSupport, ...],
    successor_by_document: dict[int, int | None],
) -> tuple[EvidenceSupport, ...]:
    """Return satisfying support not dominated by a later satisfying doc.

    Any missing registry target or cycle makes reachability untrustworthy. In
    that case every scope is retained so callers fail closed rather than
    silently dropping review work.
    """

    trusted = _trusted_readiness_frontier(readiness, successor_by_document)
    return readiness if trusted is None else trusted


def _trusted_readiness_frontier(
    readiness: tuple[EvidenceSupport, ...],
    successor_by_document: dict[int, int | None],
) -> tuple[EvidenceSupport, ...] | None:
    """Return the frontier, or ``None`` when reachability is unknowable."""

    satisfying_documents = {support.document_id for support in readiness}
    reachable_by_document: dict[int, frozenset[int]] = {}
    for source_document_id in satisfying_documents:
        current_document_id = source_document_id
        seen = {source_document_id}
        reachable: set[int] = set()
        while True:
            if current_document_id not in successor_by_document:
                return None
            successor_document_id = successor_by_document[current_document_id]
            if successor_document_id is None:
                break
            if (
                successor_document_id in seen
                or successor_document_id not in successor_by_document
            ):
                return None
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
