"""Read model for the adjudication queue.

Ordering is the whole design here. A candidate whose citations failed
verification must not be hidden and must not be adjudicated as though it
were ordinary — it is *sunk*, so a reviewer working top-down sees the
trustworthy ones first and meets the suspect ones deliberately, knowing
what they are.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.docs import stored_pdf
from corridor.merge import rank_matches
from corridor.models import Candidate, DocPage, Document


@dataclass
class Highlight:
    """Quote position on the page, as fractions of page size."""

    left: float
    top: float
    width: float
    height: float


@dataclass
class CandidateView:
    candidate: Candidate
    document: Document
    page: DocPage | None
    quote: str
    page_no: int
    citations_verified: bool
    fields: list[tuple[str, str]] = field(default_factory=list)
    highlights: list[Highlight] = field(default_factory=list)
    remaining: int = 0
    verified_remaining: int = 0
    # Pre-ranked, because the spec is explicit that this search must be good
    # before anything else gets polish: accepting a duplicate instead of
    # merging corrupts the ledger, and a reviewer will not go hunting.
    matches: list = field(default_factory=list)


def pending_counts(session: Session, project_id: int) -> tuple[int, int]:
    """(total pending, pending with verified citations)."""
    total = session.scalar(
        select(func.count())
        .select_from(Candidate)
        .where(Candidate.project_id == project_id, Candidate.state == "pending")
    )
    verified = session.scalar(
        select(func.count())
        .select_from(Candidate)
        .where(
            Candidate.project_id == project_id,
            Candidate.state == "pending",
            Candidate.citations_verified.is_(True),
        )
    )
    return total or 0, verified or 0


def next_candidate(session: Session, project_id: int) -> Candidate | None:
    return session.scalars(
        select(Candidate)
        .where(Candidate.project_id == project_id, Candidate.state == "pending")
        # Unverified citations sink. Never filtered out — a candidate whose
        # quote could not be found is a signal, not noise.
        .order_by(Candidate.citations_verified.desc(), Candidate.id)
        .limit(1)
    ).first()


def build_view(session: Session, candidate: Candidate) -> CandidateView:
    payload = candidate.payload_json or {}
    citation = (payload.get("citations") or [{}])[0]
    page_no = citation.get("page") or 1
    quote = citation.get("quote") or ""

    document = session.get(Document, candidate.source_document_id)
    page = session.scalars(
        select(DocPage).where(
            DocPage.document_id == candidate.source_document_id,
            DocPage.page_no == page_no,
        )
    ).first()

    total, verified = pending_counts(session, candidate.project_id)

    return CandidateView(
        candidate=candidate,
        document=document,
        page=page,
        quote=quote,
        page_no=page_no,
        citations_verified=bool(candidate.citations_verified),
        fields=sorted((payload.get("fields") or {}).items()),
        highlights=locate_quote(document, page_no, quote),
        remaining=total,
        verified_remaining=verified,
        matches=rank_matches(
            session,
            candidate.project_id,
            payload.get("fields") or {},
            limit=5,
            # Without this the queue suggests merging a matrix row into its
            # own siblings — 94 of 96 rows on the AT&T slice (#46).
            source_document_id=candidate.source_document_id,
        ),
    )


def locate_quote(document: Document, page_no: int, quote: str) -> list[Highlight]:
    """Where the quote sits on the page, for overlaying on the page image.

    Best-effort by design: OCR'd pages have no text layer to search, and a
    quote assembled from table cells may not be one contiguous span. A
    missing highlight degrades to showing the page unmarked, which is still
    evidence — it must never block review.
    """
    path = stored_pdf(document)
    if not path or not quote.strip():
        return []

    try:
        import pymupdf

        with pymupdf.open(path) as pdf:
            if page_no < 1 or page_no > pdf.page_count:
                return []
            page = pdf[page_no - 1]
            rect = page.rect
            if not rect.width or not rect.height:
                return []

            found = page.search_for(quote[:180]) or []
            if not found:
                # Fall back to the first distinctive token, which is enough
                # to put the reader's eye in the right place.
                first = quote.split(" ")[0]
                if len(first) >= 4:
                    found = page.search_for(first) or []

            return [
                Highlight(
                    left=r.x0 / rect.width,
                    top=r.y0 / rect.height,
                    width=(r.x1 - r.x0) / rect.width,
                    height=(r.y1 - r.y0) / rect.height,
                )
                for r in found[:40]
            ]
    except Exception:
        return []
