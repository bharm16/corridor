"""Disputes: what the revisions disagree about, and settling it.

A Dispute is not a state a row is put into. It is what you see when two
revisions of a matrix both state a field and state it differently — a
query over Assertions, exactly as an Exception is a query over the
Ledger. That is why a disputed row is an ordinary workable row: nothing
was withheld to create the Dispute, so nothing has to be resolved to
undo it.

Settling one is Adjudication in the narrow sense (ADR-0031): a reviewer
says what the record concludes for that field. The losing claim is never
erased — Assertions preserve every claim — so the settlement records the
conclusion beside them, and records how far its judgment reaches. A
revision that arrives afterwards and disagrees again reopens the Dispute
on its own, because its Assertion postdates what the reviewer saw.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import (
    Assertion,
    Dependency,
    DisputeSettlement,
    Document,
    EvidenceLink,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project


class NoSuchDispute(ValueError):
    """The field is not in dispute, so there is nothing to settle."""


@dataclass(frozen=True)
class DisputedClaim:
    """One revision's claim about a disputed field, with its page."""

    assertion_id: int
    value: str | None
    document_id: int
    document_filename: str
    doc_date: object
    page_no: int | None
    quote: str | None


@dataclass(frozen=True)
class Dispute:
    """One field several revisions state differently."""

    dependency_id: int
    field_name: str
    claims: tuple[DisputedClaim, ...]

    @property
    def values(self) -> tuple[str | None, ...]:
        seen: list[str | None] = []
        for claim in self.claims:
            if claim.value not in seen:
                seen.append(claim.value)
        return tuple(seen)


def settled_field_names(
    session: Session, dependency_ids: list[int]
) -> dict[int, set[str]]:
    """Fields whose newest settlement still covers every claim made.

    A settlement covers the claims that existed when it was made. The
    comparison is against the newest Assertion id for that field now, so
    a claim that arrived later leaves the field unsettled again without
    anything having to notice the arrival.
    """
    if not dependency_ids:
        return {}

    newest_claim = (
        select(
            Assertion.dependency_id.label("dependency_id"),
            Assertion.field_name.label("field_name"),
            func.max(Assertion.id).label("newest_assertion_id"),
        )
        .where(Assertion.dependency_id.in_(dependency_ids))
        .group_by(Assertion.dependency_id, Assertion.field_name)
        .subquery()
    )
    newest_settlement = (
        select(
            DisputeSettlement.dependency_id.label("dependency_id"),
            DisputeSettlement.field_name.label("field_name"),
            func.max(DisputeSettlement.covers_assertion_id).label("covers"),
        )
        .where(DisputeSettlement.dependency_id.in_(dependency_ids))
        .group_by(DisputeSettlement.dependency_id, DisputeSettlement.field_name)
        .subquery()
    )
    rows = session.execute(
        select(newest_settlement.c.dependency_id, newest_settlement.c.field_name)
        .join(
            newest_claim,
            (newest_claim.c.dependency_id == newest_settlement.c.dependency_id)
            & (newest_claim.c.field_name == newest_settlement.c.field_name),
        )
        .where(newest_settlement.c.covers >= newest_claim.c.newest_assertion_id)
    ).all()

    settled: dict[int, set[str]] = {}
    for dependency_id, field_name in rows:
        settled.setdefault(dependency_id, set()).add(field_name)
    return settled


def disputes_for(session: Session, dependency_id: int) -> list[Dispute]:
    """Every standing Dispute on one record, with both pages to read."""
    from corridor.exceptions import contradicted_fields

    names = contradicted_fields(session, [dependency_id]).get(dependency_id, [])
    if not names:
        return []

    found = []
    for name in sorted(names):
        rows = session.execute(
            select(Assertion, EvidenceLink, Document)
            .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
            .join(Document, EvidenceLink.document_id == Document.id)
            .where(
                Assertion.dependency_id == dependency_id,
                Assertion.field_name == name,
                EvidenceLink.verified.is_(True),
            )
            .order_by(Document.doc_date.asc().nulls_first(), Assertion.id.asc())
        ).all()
        found.append(
            Dispute(
                dependency_id=dependency_id,
                field_name=name,
                claims=tuple(
                    DisputedClaim(
                        assertion_id=assertion.id,
                        value=assertion.asserted_value,
                        document_id=document.id,
                        document_filename=document.filename,
                        doc_date=document.doc_date,
                        page_no=link.page_no,
                        quote=link.quote,
                    )
                    for assertion, link, document in rows
                ),
            )
        )
    return found


def settle_dispute(
    session: Session,
    dependency_id: int,
    field_name: str,
    *,
    value: str | None,
    principal: HumanPrincipal,
) -> DisputeSettlement:
    """Record what the record concludes for one disputed field.

    The value need not be one of the claims: a reviewer reading both
    pages may conclude a third thing, which is the same latitude
    edit-then-accept has always given. What it may not be is unattributed
    — settling is a human act on the Ledger.
    """
    settler = require_human_principal(principal)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise ValueError(f"dependency {dependency_id} does not exist")
    lock_project(session, dependency.project_id)

    from corridor.exceptions import contradicted_fields

    standing = contradicted_fields(session, [dependency_id]).get(
        dependency_id, []
    )
    if field_name not in standing:
        raise NoSuchDispute(
            f"{field_name!r} is not in dispute on this record — nothing to "
            "settle"
        )

    newest = session.scalar(
        select(func.max(Assertion.id)).where(
            Assertion.dependency_id == dependency_id,
            Assertion.field_name == field_name,
        )
    )
    settlement = DisputeSettlement(
        dependency_id=dependency_id,
        field_name=field_name,
        settled_value=value,
        settled_by=settler.subject,
        covers_assertion_id=newest,
    )
    session.add(settlement)
    session.flush([settlement])

    # Where the record carries a column for the field, the conclusion is
    # projected onto it — the same shape the Committed Date projection
    # takes. Fields with no column live on in the settlement alone.
    if hasattr(dependency, field_name) and field_name in _PROJECTED_FIELDS:
        setattr(dependency, field_name, value)

    audit.record(
        session,
        principal=settler,
        action=audit.SETTLE_DISPUTE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency_id,
        after={
            "field_name": field_name,
            "settled_value": value,
            "covers_assertion_id": newest,
            "dispute_settlement_id": settlement.id,
        },
    )
    session.flush()
    return settlement


# The Dependency columns a settled field writes through to. Named rather
# than derived from `hasattr`, so settling `notes` can never silently
# overwrite an unrelated attribute that happens to share a name.
_PROJECTED_FIELDS = frozenset(
    {"station_from", "station_to", "title", "location_desc"}
)
