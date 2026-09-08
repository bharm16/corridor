"""Cite the Source Segment that owns an Evidence Link's words, once.

``EvidenceLink.quote`` is a copy.  The same sentence already exists as a
Source Segment with its own digest and typed locator, so the quote column is a
second, unreplayable owner of text that ADR-0068 gave exactly one owner: edit
the segment's source and the copy keeps asserting the old words, with nothing
in the schema noticing.

This module is the reference that replaces the copy for new writes (#605).
``cite_source_segments`` appends ``EvidenceLinkSource`` rows, which hold link,
segment and order and no text at all.  ``evidence_quotation`` reads back the
words a link stands on and *says where they came from*, so a caller can tell a
cited segment from a legacy quote rather than being handed a string that looks
the same either way.

Two things it deliberately does not do.  It never rewrites or deletes a legacy
quote: proving that a cited segment and a stored quote say the same thing
needs a matched corpus and is a separate piece of work, and until that proof
exists a bulk rewrite would be replacing evidence with something merely
believed equivalent.  And ``citable_segment_for_quote`` matches only on exact
equality of the whole segment text on the cited page, refusing a near miss:
the point of the reference is that the segment owns the words, so a citation
minted from a substring or a best guess would put the ambiguity back one table
over.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import EvidenceLink, EvidenceLinkSource, SourceSegment
from corridor.prose_spans import prose_segment_filter


@dataclass(frozen=True)
class EvidenceQuotation:
    """The exact words an Evidence Link stands on, and who owns them."""

    # 'source_segments' when the link cites segments, 'legacy_quote' when the
    # copied column is still the only owner. A caller that must not read a
    # copy branches on this rather than guessing from the text.
    owner: str
    # One passage per cited segment, in citation order. Several segments are
    # not joined into one string: ADR-0069 lets a citation span several
    # pieces, and how they read together is the caller's judgment, not this
    # module's.
    passages: tuple[str, ...]
    source_segment_ids: tuple[int, ...]


def cite_source_segments(
    session: Session,
    link: EvidenceLink,
    segments: tuple[SourceSegment, ...],
) -> tuple[EvidenceLinkSource, ...]:
    """Append the citations naming the segments that own this link's words."""

    if link.id is None:
        raise ValueError("an Evidence Link must exist before it cites a segment")
    if not segments:
        raise ValueError("a citation must name at least one Source Segment")
    if len({segment.id for segment in segments}) != len(segments):
        raise ValueError("an Evidence Link cannot cite the same segment twice")
    for segment in segments:
        if segment.id is None:
            raise ValueError("a cited Source Segment must exist")
        if segment.document_id != link.document_id:
            raise ValueError(
                "a cited Source Segment must belong to the Evidence Link's "
                "document rendition"
            )

    # Citations are append-only, so a later call continues the order rather
    # than renumbering what is already recorded.
    highest = session.scalar(
        select(func.max(EvidenceLinkSource.ordinal)).where(
            EvidenceLinkSource.evidence_link_id == link.id
        )
    )
    ordinal = (highest or 0) + 1
    rows = []
    for segment in segments:
        row = EvidenceLinkSource(
            project_id=segment.project_id,
            document_id=link.document_id,
            evidence_link_id=link.id,
            source_segment_id=segment.id,
            ordinal=ordinal,
        )
        session.add(row)
        rows.append(row)
        ordinal += 1
    session.flush(rows)
    return tuple(rows)


def evidence_quotation(session: Session, link: EvidenceLink) -> EvidenceQuotation:
    """Read one link's words from its cited segments, or its legacy copy."""

    cited = session.execute(
        select(SourceSegment.id, SourceSegment.exact_text)
        .join(
            EvidenceLinkSource,
            EvidenceLinkSource.source_segment_id == SourceSegment.id,
        )
        .where(EvidenceLinkSource.evidence_link_id == link.id)
        .order_by(EvidenceLinkSource.ordinal)
    ).all()
    if cited:
        return EvidenceQuotation(
            owner="source_segments",
            passages=tuple(row.exact_text for row in cited),
            source_segment_ids=tuple(row.id for row in cited),
        )
    return EvidenceQuotation(
        owner="legacy_quote",
        passages=(link.quote,),
        source_segment_ids=(),
    )


def citable_segment_for_quote(
    session: Session, *, document_id: int, page_no: int, quote: str
) -> SourceSegment | None:
    """The one prose segment whose exact text *is* this quote, if there is one.

    Exact equality, and exactly one match, or nothing.  A substring match
    would mean the citation named a piece the segment does not delimit, and
    two matches would mean the citation cannot say which occurrence it read;
    in both cases the honest answer is that this quote has no segment to cite
    yet, and the legacy copy stays the only owner of those words.
    """

    matches = session.scalars(
        select(SourceSegment)
        .where(
            SourceSegment.document_id == document_id,
            prose_segment_filter(SourceSegment),
            SourceSegment.page_no == page_no,
            SourceSegment.exact_text == quote,
        )
        .limit(2)
    ).all()
    return matches[0] if len(matches) == 1 else None
