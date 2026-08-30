"""Read one disputed field's cited history without acquiring decision authority.

ADR-0061's narrative layer is deliberately a display packet.  The packet is
bound to one Dependency and one field, re-verifies every quote against the
registered page before it is included, and exposes neither a conclusion nor a
coordination plan.  A future model runtime may draft richer wording over this
same bounded input; this deterministic builder is the safe packet contract and
the visible fallback when no runtime is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Assertion, Dependency, DocPage, Document, EvidenceLink
from corridor.verify import literal_quote_on_page


@dataclass(frozen=True)
class TimelineMention:
    """One quote-verified statement of the value on the bound row only."""

    assertion_id: int
    value: str | None
    document_id: int
    document_name: str
    document_date: date | None
    page_no: int
    quote: str
    page_text: str
    classification: str
    speaker: str | None = None


@dataclass(frozen=True)
class DisputeTimelinePacket:
    """Machine-readable evidence order, explicitly not a settlement packet."""

    dependency_id: int
    field_name: str
    mentions: tuple[TimelineMention, ...]
    drafted_reading: str
    apparent_current_position: str | None


def build_dispute_timeline(
    session: Session, dependency_id: int, field_name: str
) -> DisputeTimelinePacket:
    """Assemble the bounded timeline; invalid or foreign quotes are omitted.

    ``EvidenceLink.verified`` is necessary but not sufficient here: a page can
    later be corrected, and a model-visible packet must not resurrect a quote
    that no longer literally appears in the stored source.  Filtering by the
    Dependency's project closes the otherwise possible cross-project foreign
    key mismatch before any source text is returned.
    """
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise ValueError(f"dependency {dependency_id} does not exist")
    if not field_name.strip():
        raise ValueError("a timeline needs one field name")

    rows = session.execute(
        select(Assertion, EvidenceLink, Document, DocPage)
        .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
        .join(Document, EvidenceLink.document_id == Document.id)
        .join(
            DocPage,
            (DocPage.document_id == EvidenceLink.document_id)
            & (DocPage.page_no == EvidenceLink.page_no),
        )
        .where(
            Assertion.dependency_id == dependency_id,
            Assertion.field_name == field_name,
            EvidenceLink.verified.is_(True),
            Document.project_id == dependency.project_id,
        )
        .order_by(Document.doc_date.asc().nulls_last(), Assertion.id.asc())
    ).all()

    mentions: list[TimelineMention] = []
    for assertion, link, document, page in rows:
        literal = literal_quote_on_page(link.quote, page.text)
        if literal is None:
            continue
        mentions.append(
            TimelineMention(
                assertion_id=assertion.id,
                value=assertion.asserted_value,
                document_id=document.id,
                document_name=document.filename,
                document_date=document.doc_date,
                page_no=link.page_no,
                quote=literal,
                page_text=page.text,
                classification=_classification(
                    document.doc_type,
                    assertion.asserted_value,
                    mentions[-1].value if mentions else None,
                ),
            )
        )
    return DisputeTimelinePacket(
        dependency_id=dependency_id,
        field_name=field_name,
        mentions=tuple(mentions),
        drafted_reading=_drafted_reading(mentions),
        apparent_current_position=mentions[-1].value if mentions else None,
    )


def _classification(
    document_type: str, value: str | None, previous_value: str | None
) -> str:
    if previous_value is not None and value == previous_value:
        return "restatement"
    if previous_value is not None and value != previous_value:
        return "correction"
    if document_type == "matrix":
        return "measurement"
    return "casual_mention"


def _drafted_reading(mentions: list[TimelineMention]) -> str:
    """Conservative provenance-labelled wording for display without a model."""
    if not mentions:
        return "Machine reading: no currently quote-verified mentions are available."
    values: list[str] = []
    for mention in mentions:
        rendered = mention.value or "no value"
        if not values or values[-1] != rendered:
            values.append(rendered)
    path = " → ".join(values)
    current = values[-1]
    return (
        "Machine reading, not a conclusion: cited mentions progress "
        f"{path}; apparent current position {current}."
    )
