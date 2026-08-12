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

from corridor.merge import rank_matches
from corridor.models import Candidate, Dependency, DocPage, Document
from corridor.storage import stored_pdf
from corridor.supersession import actionable_candidate_query


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
    # What the extractor recorded about *how* it read this row. All of it
    # already sat in the payload; none of it was visible where adjudication
    # happens, so investigating it meant SQL against `payload_json` — which
    # is how #85 and #97 were actually investigated.
    tier: str | None = None
    text_source: str | None = None
    # Printed headers the model saw and the canonical vocabulary could not
    # place. The queue telling a human "the document says something the
    # Ledger has no field for", which is the trigger for a deliberate
    # vocabulary extension rather than an extractor's improvisation.
    unmapped_columns: list[str] = field(default_factory=list)
    # Named, so "unverified" points at the suspect value instead of only
    # sinking the row. A reviewer who cannot see which field is unsupported
    # has to re-verify all of them.
    unverified_fields: list[str] = field(default_factory=list)
    low_confidence_tokens: list[str] = field(default_factory=list)
    # Acceptance builds a Dependency, and only a `dependency` Candidate
    # carries what one needs. The queue says so rather than offering a
    # button that fabricates the record — the same treatment merge already
    # gets when there is nothing to merge into.
    accept_refused: str = ""
    # Present only when the reviewer explicitly opened one superseded
    # document. Forms carry it so the write boundary sees the same scope.
    historical_document_id: int | None = None


@dataclass(frozen=True)
class SupersessionReviewView:
    """Display data for ordinary work that has no actionable Candidate."""

    review: object
    dependency: Dependency | None
    predecessor: Document | None
    successor: Document | None
    successor_candidate: Candidate | None
    successor_candidate_id: int | None
    status_label: str
    status_detail: str
    refusal_detail: str


def build_supersession_review_view(
    session: Session, review: object
) -> SupersessionReviewView:
    """Resolve names around a live worklist item without persisting UI state."""

    status = str(getattr(review, "status", "blocked"))
    details = {
        "awaiting_extraction": "No successor extraction attempt exists.",
        "extraction_failed": (
            "The successor extraction failed; a completed Active Run is required "
            "before this Dependency can be reviewed."
        ),
        "awaiting_active_run": (
            "A successor extraction completed, but no Active Run is declared."
        ),
        "awaiting_comparison": (
            "The successor has an Active Run, but its Revision Comparison is not ready."
        ),
        "blocked": "This Dependency cannot be routed safely from the current lineage.",
    }
    reason = getattr(review, "reason", None)
    successor_candidate_ids = tuple(
        getattr(review, "successor_candidate_ids", ())
    )
    successor_candidate_id = (
        successor_candidate_ids[0]
        if len(successor_candidate_ids) == 1
        else None
    )
    refusal_detail = (
        str(reason).replace("_", " ").capitalize()
        if reason
        else "The successor is not a mechanically verified unchanged match."
    )
    return SupersessionReviewView(
        review=review,
        dependency=session.get(Dependency, getattr(review, "dependency_id", None)),
        predecessor=session.get(
            Document, getattr(review, "predecessor_document_id", None)
        ),
        successor=session.get(
            Document, getattr(review, "successor_document_id", None)
        ),
        successor_candidate=(
            session.get(Candidate, successor_candidate_id)
            if successor_candidate_id is not None
            else None
        ),
        successor_candidate_id=successor_candidate_id,
        status_label=status.replace("_", " ").capitalize(),
        status_detail=details.get(
            status,
            "This revision needs ordinary Candidate Adjudication before support can move.",
        ),
        refusal_detail=refusal_detail,
    )


def pending_counts(
    session: Session,
    project_id: int,
    *,
    historical_document_id: int | None = None,
    allowed_candidate_ids: frozenset[int] | None = None,
) -> tuple[int, int]:
    """(total pending, pending with verified citations)."""
    scoped = actionable_candidate_query(
        project_id, historical_document_id=historical_document_id
    ).where(Candidate.kind == "dependency")
    if allowed_candidate_ids is not None:
        scoped = scoped.where(Candidate.id.in_(allowed_candidate_ids))
    verified = session.scalar(
        scoped.with_only_columns(func.count())
        .where(Candidate.citations_verified.is_(True))
        .order_by(None)
    )
    total = session.scalar(scoped.with_only_columns(func.count()).order_by(None))
    return total or 0, verified or 0


def next_candidate(
    session: Session,
    project_id: int,
    *,
    historical_document_id: int | None = None,
    allowed_candidate_ids: frozenset[int] | None = None,
) -> Candidate | None:
    # Dependency Candidates only. Adjudication refuses an event — it must
    # attach to a Dependency instead — so serving one here offers a button
    # that cannot work, and 1,629 of them stood in front of the rows that
    # can (#209).
    query = actionable_candidate_query(
        project_id, historical_document_id=historical_document_id
    ).where(Candidate.kind == "dependency")
    if allowed_candidate_ids is not None:
        query = query.where(Candidate.id.in_(allowed_candidate_ids))
    return session.scalars(
        query
        # Unverified citations sink. Never filtered out — a candidate whose
        # quote could not be found is a signal, not noise.
        .order_by(Candidate.citations_verified.desc(), Candidate.id)
        .limit(1)
    ).first()


def build_evidence(session: Session, candidate: Candidate, *, label: str = "") -> dict:
    """One document's page as evidence for one Candidate.

    Separate from build_view because a disagreement needs two of these and
    only one of them is the Candidate under judgment.
    """
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
    return {
        "document": document,
        "page": page,
        "page_no": page_no,
        "quote": quote,
        "highlights": locate_quote(document, page_no, quote),
        "label": label,
    }


def build_view(
    session: Session,
    candidate: Candidate,
    *,
    historical_document_id: int | None = None,
    allowed_candidate_ids: frozenset[int] | None = None,
) -> CandidateView:
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

    total, verified = pending_counts(
        session,
        candidate.project_id,
        historical_document_id=historical_document_id,
        allowed_candidate_ids=allowed_candidate_ids,
    )

    return CandidateView(
        candidate=candidate,
        document=document,
        page=page,
        quote=quote,
        page_no=page_no,
        citations_verified=bool(candidate.citations_verified),
        fields=sorted((payload.get("fields") or {}).items()),
        tier=payload.get("tier"),
        text_source=payload.get("text_source"),
        unmapped_columns=list(payload.get("unmapped_columns") or []),
        unverified_fields=list(payload.get("unverified_fields") or []),
        low_confidence_tokens=list(payload.get("low_confidence_tokens") or []),
        highlights=locate_quote(document, page_no, quote),
        remaining=total,
        verified_remaining=verified,
        historical_document_id=historical_document_id,
        accept_refused=(
            ""
            if candidate.kind == "dependency"
            else (
                f"This is a {candidate.kind}, not a dependency. Attach it to an "
                "existing Dependency by merging; accepting would create one the "
                "document never described."
            )
        ),
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


@dataclass
class RailEntry:
    """One cohort member in the rehearsal rail."""

    candidate_id: int | None
    utility_id: str
    classification: str
    station: str
    utility_type: str
    state: str
    current: bool


@dataclass
class ChangeEntry:
    """One field's December-to-February movement, from the pinned finding."""

    field: str
    before: str
    after: str


RAIL_GROUPS = (
    ("conflict_flag_n_to_y", "Flipped N → Y"),
    ("verification_blocked", "Verification blocked"),
    ("newly_added", "Newly added"),
)


def build_cohort_rail(
    session: Session, receipt, current_candidate_id: int | None
) -> list[RailEntry]:
    """Every member, grouped by why it is here, with its decision state.

    The rail is navigation and progress, nothing more: membership stays
    the receipt's fact, and the mutation boundary stays server-side at
    accept/edit/merge. A member whose row cannot be resolved to a live
    Candidate is still listed — silently dropping it would misreport the
    cohort — with its state named `unresolved`.
    """
    candidates = session.scalars(
        select(Candidate).where(
            Candidate.extraction_run_id == receipt.successor_extraction_run_id
        )
    ).all()
    by_utility: dict[str, Candidate] = {}
    for candidate in candidates:
        fields = (candidate.payload_json or {}).get("fields", {})
        utility_id = fields.get("utility_id")
        if utility_id is not None:
            by_utility[str(utility_id)] = candidate

    group_order = {key: index for index, (key, _) in enumerate(RAIL_GROUPS)}
    entries = []
    for member in receipt.members:
        candidate = by_utility.get(member["utility_id"])
        fields = (
            (candidate.payload_json or {}).get("fields", {}) if candidate else {}
        )
        entries.append(
            RailEntry(
                candidate_id=candidate.id if candidate else None,
                utility_id=member["utility_id"],
                classification=member["classification"],
                station=fields.get("station_from") or "—",
                utility_type=fields.get("utility_type") or "—",
                state=candidate.state if candidate else "unresolved",
                current=(
                    candidate is not None
                    and candidate.id == current_candidate_id
                ),
            )
        )
    entries.sort(
        key=lambda e: (group_order.get(e.classification, 99), e.utility_id)
    )
    return entries


def change_strip(session, receipt, candidate: Candidate) -> list[ChangeEntry]:
    """What moved between the revisions for this exact row.

    Read from the pinned Comparison finding, never recomputed: the strip
    shows the matcher's persisted before/after so the reviewer and the
    receipt cannot disagree about what changed.
    """
    from corridor.models import RevisionComparisonFinding

    finding = session.scalars(
        select(RevisionComparisonFinding).where(
            RevisionComparisonFinding.revision_comparison_run_id
            == receipt.revision_comparison_run_id,
            RevisionComparisonFinding.state == "changed",
            RevisionComparisonFinding.successor_candidate_ids.contains(
                [candidate.id]
            ),
        )
    ).first()
    if finding is None:
        return []
    return [
        ChangeEntry(
            field=str(change.get("field")),
            before=str(change.get("before") if change.get("before") is not None else "—"),
            after=str(change.get("after") if change.get("after") is not None else "—"),
        )
        for change in finding.field_changes
    ]


def member_classification(receipt, candidate: Candidate) -> str | None:
    """Why this candidate is in the cohort, from the receipt itself."""
    fields = (candidate.payload_json or {}).get("fields", {})
    utility_id = str(fields.get("utility_id"))
    for member in receipt.members:
        if member["utility_id"] == utility_id:
            return member["classification"]
    return None
